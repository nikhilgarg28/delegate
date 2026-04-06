"""Tests for the unified runtime dispatch model (agents_with_unread + run_turn).

Replaces the old orchestrator tests — there is no longer any PID tracking,
subprocess spawning, or stale-PID clearing.  The daemon polls
``agents_with_unread()`` and dispatches ``run_turn()`` directly.
"""

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from delegate.mailbox import deliver, Message, agents_with_unread, mark_processed, read_inbox
from delegate.runtime import list_ai_agents, run_turn, TelephoneExchange, _repo_git_dirs
from delegate.telephone import TelephoneUsage

TEAM = "testteam"


def _deliver_msg(tmp_team, to_agent, body="Hello", sender="manager"):
    deliver(tmp_team, TEAM, Message(
        sender=sender,
        recipient=to_agent,
        time="2026-02-08T12:00:00Z",
        body=body,
    ))


# ---------------------------------------------------------------------------
# Mock Telephone for testing
# ---------------------------------------------------------------------------


@dataclass
class _FakeResultMsg:
    """Mimics a claude_agent_sdk ResultMessage."""
    total_cost_usd: float = 0.01
    usage: dict | None = None

    def __post_init__(self):
        if self.usage is None:
            self.usage = {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 10,
            }


class _MockTelephone:
    """Minimal Telephone stand-in for unit testing run_turn().

    When ``send()`` is called, yields a single ``_FakeResultMsg`` and
    updates ``usage`` to mimic what the real Telephone does internally.
    """

    def __init__(self, preamble: str = "", **kwargs):
        self.preamble = preamble
        self._prior = TelephoneUsage()
        self.usage = TelephoneUsage()
        self.allowed_write_paths: list[str] | None = None
        self._effective_write_paths: list[str] | None = None
        self.add_dirs: list = kwargs.get("add_dirs", [])
        self.allowed_domains: list[str] = kwargs.get("allowed_domains", ["*"])
        self.model: str | None = kwargs.get("model", None)
        self.turns = 0

    async def send(self, prompt: str):
        """Async generator mimicking Telephone.send()."""
        self.turns += 1
        msg = _FakeResultMsg()
        self.usage += TelephoneUsage.from_sdk_message(msg)
        yield msg

    def total_usage(self) -> TelephoneUsage:
        return self._prior + self.usage

    async def rotate(self):
        """No-op for tests."""
        self._prior = self._prior + self.usage
        self.usage = TelephoneUsage()

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# agents_with_unread
# ---------------------------------------------------------------------------


class TestAgentsWithUnread:
    def test_no_messages_empty(self, tmp_team):
        assert agents_with_unread(tmp_team, TEAM) == []

    def test_unread_detected(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        result = agents_with_unread(tmp_team, TEAM)
        assert "alice" in result

    def test_multiple_agents(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        _deliver_msg(tmp_team, "bob")
        result = agents_with_unread(tmp_team, TEAM)
        assert set(result) == {"alice", "bob"}

    def test_processed_not_included(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        # Mark it processed
        inbox = read_inbox(tmp_team, TEAM, "alice", unread_only=True)
        assert len(inbox) == 1
        mark_processed(tmp_team, TEAM, inbox[0].id)
        # Now should be empty
        result = agents_with_unread(tmp_team, TEAM)
        assert "alice" not in result


# ---------------------------------------------------------------------------
# list_ai_agents (boss filtering)
# ---------------------------------------------------------------------------


class TestListAIAgents:
    def test_returns_non_boss_agents(self, tmp_team):
        agents = list_ai_agents(tmp_team, TEAM)
        # tmp_team fixture creates manager, alice, bob
        assert "manager" in agents
        assert "alice" in agents
        assert "bob" in agents

    def test_excludes_boss(self, tmp_team):
        """Boss should not appear in AI agent list."""
        agents = list_ai_agents(tmp_team, TEAM)
        # Boss isn't typically in the agents dir, but verify no boss-role agents
        for name in agents:
            assert name != "boss"  # no boss role in the list


# ---------------------------------------------------------------------------
# run_turn — with mock Telephone via TelephoneExchange
# ---------------------------------------------------------------------------


def _make_mock_tel(*args, **kwargs):
    """Factory for _MockTelephone that accepts _create_telephone's signature."""
    preamble = kwargs.pop("preamble", "")
    return _MockTelephone(preamble=preamble, **kwargs)


class TestRunTurn:
    @patch("delegate.runtime.random.random", return_value=1.0)  # no reflection
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    def test_processes_message_and_marks_processed(self, _mock_create, _mock_rng, tmp_team):
        """run_turn should process the oldest unread message and mark it."""
        _deliver_msg(tmp_team, "alice", body="Please do task 1")
        exchange = TelephoneExchange()

        result = asyncio.run(
            run_turn(tmp_team, TEAM, "alice", exchange=exchange)
        )

        assert result.agent == "alice"
        assert result.team == TEAM
        assert result.error is None
        # The mock telephone never calls mailbox_send, so the nudge turn fires
        # and adds a second round of tokens (100 + 100 = 200 in, 50 + 50 = 100 out).
        assert result.tokens_in == 200
        assert result.tokens_out == 100
        assert result.cost_usd == 0.02
        assert result.turns == 1  # nudge doesn't increment turn_num

        # Message should be marked as processed
        remaining = agents_with_unread(tmp_team, TEAM)
        assert "alice" not in remaining

        # Telephone should be stored in exchange
        assert exchange.get(TEAM, "alice") is not None

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    def test_no_messages_returns_early(self, _mock_create, _mock_rng, tmp_team):
        """run_turn with no unread messages should return early with no turns."""
        exchange = TelephoneExchange()

        result = asyncio.run(
            run_turn(tmp_team, TEAM, "alice", exchange=exchange)
        )

        assert result.error is None
        assert result.turns == 0  # no messages -> early return

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_sdk_error_captured(self, _mock_rng, tmp_team):
        """If the Telephone send fails, the error is captured in TurnResult."""
        _deliver_msg(tmp_team, "alice")

        class _FailingTelephone(_MockTelephone):
            async def send(self, prompt: str):
                raise RuntimeError("SDK connection failed")
                yield  # make it an async generator  # noqa: E501

        exchange = TelephoneExchange()
        with patch("delegate.runtime._create_telephone",
                    side_effect=lambda *a, **kw: _FailingTelephone(preamble=kw.get("preamble", ""))):
            result = asyncio.run(
                run_turn(tmp_team, TEAM, "alice", exchange=exchange)
            )

        assert result.error is not None
        assert "SDK connection failed" in result.error

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    def test_worklog_written(self, _mock_create, _mock_rng, tmp_team):
        """run_turn should write a worklog file."""
        _deliver_msg(tmp_team, "alice")

        asyncio.run(
            run_turn(tmp_team, TEAM, "alice", exchange=TelephoneExchange())
        )

        from delegate.paths import agent_dir
        logs_dir = agent_dir(tmp_team, TEAM, "alice") / "logs"
        worklogs = list(logs_dir.glob("*.worklog.md"))
        assert len(worklogs) >= 1

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    def test_session_created_in_db(self, _mock_create, _mock_rng, tmp_team):
        """run_turn should create a session in the database."""
        _deliver_msg(tmp_team, "alice")

        result = asyncio.run(
            run_turn(tmp_team, TEAM, "alice", exchange=TelephoneExchange())
        )

        assert result.session_id > 0

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    def test_cache_tokens_tracked(self, _mock_create, _mock_rng, tmp_team):
        """run_turn should track cache_read and cache_write tokens."""
        _deliver_msg(tmp_team, "alice", body="Work on this")

        result = asyncio.run(
            run_turn(tmp_team, TEAM, "alice", exchange=TelephoneExchange())
        )

        # The mock never calls mailbox_send, so the nudge fires (second turn).
        # Cache tokens are doubled: main (20/10) + nudge (20/10).
        assert result.cache_read == 40
        assert result.cache_write == 20

    @patch("delegate.runtime.random.random", return_value=0.0)  # always reflect
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    def test_reflection_turn_runs_when_due(self, _mock_create, _mock_rng, tmp_team):
        """When reflection coin-flip lands, a second turn runs without marking mail."""
        _deliver_msg(tmp_team, "alice", body="Work on this")

        result = asyncio.run(
            run_turn(tmp_team, TEAM, "alice", exchange=TelephoneExchange())
        )

        assert result.turns == 2
        # The mock never calls mailbox_send, so the nudge fires before reflection.
        # Three total rounds: main (100) + nudge (100) + reflection (100) = 300 in, 150 out.
        assert result.tokens_in == 300
        assert result.tokens_out == 150
        # Cache tokens: 3 rounds * (20 read / 10 write each)
        assert result.cache_read == 60
        assert result.cache_write == 30

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    def test_batch_same_task_id(self, _mock_create, _mock_rng, tmp_team):
        """Messages with the same task_id should be batched together."""
        # Deliver 3 messages with task_id=None (no --task flag)
        _deliver_msg(tmp_team, "alice", body="Hello 1")
        _deliver_msg(tmp_team, "alice", body="Hello 2")
        _deliver_msg(tmp_team, "alice", body="Hello 3")

        result = asyncio.run(
            run_turn(tmp_team, TEAM, "alice", exchange=TelephoneExchange())
        )

        assert result.error is None
        assert result.turns == 1

        # All 3 messages should be processed (same task_id=None)
        remaining = agents_with_unread(tmp_team, TEAM)
        assert "alice" not in remaining

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    @patch("delegate.network.get_allowed_domains", return_value=["*"])
    def test_telephone_reused_across_turns(self, _mock_domains, _mock_create, _mock_rng, tmp_team):
        """The same Telephone should be reused for consecutive turns."""
        exchange = TelephoneExchange()

        _deliver_msg(tmp_team, "alice", body="Turn 1")
        asyncio.run(run_turn(tmp_team, TEAM, "alice", exchange=exchange))
        tel_after_1 = exchange.get(TEAM, "alice")
        assert tel_after_1 is not None

        _deliver_msg(tmp_team, "alice", body="Turn 2")
        asyncio.run(run_turn(tmp_team, TEAM, "alice", exchange=exchange))
        tel_after_2 = exchange.get(TEAM, "alice")

        # Same object -- not recreated
        assert tel_after_2 is tel_after_1
        # _create_telephone should have been called only once
        assert _mock_create.call_count == 1

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    def test_write_paths_set_for_worker(self, _mock_create, _mock_rng, tmp_team):
        """Worker agents should have restricted write paths set."""
        _deliver_msg(tmp_team, "alice", body="Hi")
        exchange = TelephoneExchange()

        asyncio.run(run_turn(tmp_team, TEAM, "alice", exchange=exchange))

        tel = exchange.get(TEAM, "alice")
        # _create_telephone is called with role-based write paths.
        # Verify the mock was called with role in its kwargs.
        call_kw = _mock_create.call_args
        assert "role" in call_kw.kwargs
        assert call_kw.kwargs["role"] == "engineer"


# ---------------------------------------------------------------------------
# Repo .git/ dirs in sandbox add_dirs
# ---------------------------------------------------------------------------


class TestRepoGitDirs:
    """Tests for _repo_git_dirs and .git/ add_dirs in _create_telephone."""

    def test_no_repos_returns_empty(self, tmp_team):
        """Team with no registered repos returns empty list."""
        result = _repo_git_dirs(tmp_team, TEAM)
        assert result == []

    def test_registered_repo_returns_git_dir(self, tmp_team):
        """Registering a real git repo should include its .git/ in the list."""
        from delegate.repo import register_repo

        # Create a bare git repo to register
        repo_path = tmp_team / "_test_repos" / "myrepo"
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True,
                       capture_output=True)

        register_repo(tmp_team, TEAM, str(repo_path), name="myrepo")

        result = _repo_git_dirs(tmp_team, TEAM)
        expected = str((repo_path / ".git").resolve())
        assert expected in result

    def test_multiple_repos_sorted(self, tmp_team):
        """Multiple repos should return sorted .git/ paths."""
        from delegate.repo import register_repo

        repos = []
        for name in ("beta-repo", "alpha-repo"):
            rp = tmp_team / "_test_repos" / name
            rp.mkdir(parents=True)
            subprocess.run(["git", "init", str(rp)], check=True,
                           capture_output=True)
            register_repo(tmp_team, TEAM, str(rp), name=name)
            repos.append(str((rp / ".git").resolve()))

        result = _repo_git_dirs(tmp_team, TEAM)
        assert result == sorted(repos)

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_create_telephone_includes_git_dirs(self, _mock_rng, tmp_team):
        """_create_telephone should include repo .git/ paths in add_dirs."""
        from delegate.repo import register_repo
        from delegate.runtime import _create_telephone

        repo_path = tmp_team / "_test_repos" / "myrepo"
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True,
                       capture_output=True)
        register_repo(tmp_team, TEAM, str(repo_path), name="myrepo")

        tel = _create_telephone(
            tmp_team, TEAM, "alice", preamble="test preamble",
        )
        expected_git = str((repo_path / ".git").resolve())
        add_dirs_strs = [str(d) for d in tel.add_dirs]
        assert expected_git in add_dirs_strs

    def test_sandbox_no_excluded_commands(self, tmp_team):
        """Sandbox config should NOT include excludedCommands."""
        from delegate.runtime import _create_telephone

        tel = _create_telephone(
            tmp_team, TEAM, "alice", preamble="test",
        )
        opts = tel._build_options()
        assert opts.sandbox is not None
        sandbox = opts.sandbox
        assert "excludedCommands" not in sandbox
        assert sandbox["enabled"] is True
        assert sandbox["autoAllowBashIfSandboxed"] is True


# ---------------------------------------------------------------------------
# Phase 3: Narrow sandbox + denied bash patterns
# ---------------------------------------------------------------------------


class TestNarrowSandbox:
    """Phase 3 — verify narrow add_dirs, role-aware .git/, and denied_bash_patterns."""

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_worker_add_dirs_excludes_hc_home(self, _mock_rng, tmp_team):
        """Worker add_dirs should contain team dir + tmpdir + .git, NOT hc_home."""
        from delegate.runtime import _create_telephone

        tel = _create_telephone(
            tmp_team, TEAM, "alice", preamble="test",
        )
        add_dirs_strs = [str(d) for d in tel.add_dirs]
        # Should NOT include hc_home directly
        assert str(tmp_team) not in add_dirs_strs
        # Should include team working dir
        from delegate.paths import team_dir as _td
        assert str(_td(tmp_team, TEAM)) in add_dirs_strs

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_manager_gets_git_dirs(self, _mock_rng, tmp_team):
        """Manager add_dirs SHOULD include .git/ paths (same as workers)."""
        from delegate.repo import register_repo
        from delegate.runtime import _create_telephone
        from delegate.paths import agents_dir

        # Register a repo
        repo_path = tmp_team / "_test_repos" / "myrepo"
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True,
                       capture_output=True)
        register_repo(tmp_team, TEAM, str(repo_path), name="myrepo")

        # Create manager agent directory
        mgr_dir = agents_dir(tmp_team, TEAM) / "delegate"
        mgr_dir.mkdir(parents=True, exist_ok=True)

        tel = _create_telephone(
            tmp_team, TEAM, "delegate", preamble="test", role="manager",
        )
        add_dirs_strs = [str(d) for d in tel.add_dirs]
        git_entries = [d for d in add_dirs_strs if d.endswith("/.git")]
        assert len(git_entries) > 0, f"Manager should get .git/ dirs: {add_dirs_strs}"

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_worker_gets_git_dirs(self, _mock_rng, tmp_team):
        """Worker add_dirs SHOULD include .git/ paths when repos are registered."""
        from delegate.repo import register_repo
        from delegate.runtime import _create_telephone

        repo_path = tmp_team / "_test_repos" / "myrepo"
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True,
                       capture_output=True)
        register_repo(tmp_team, TEAM, str(repo_path), name="myrepo")

        tel = _create_telephone(
            tmp_team, TEAM, "alice", preamble="test", role="engineer",
        )
        add_dirs_strs = [str(d) for d in tel.add_dirs]
        expected_git = str((repo_path / ".git").resolve())
        assert expected_git in add_dirs_strs

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_denied_bash_patterns_wired(self, _mock_rng, tmp_team):
        """_create_telephone should wire denied_bash_patterns."""
        from delegate.runtime import _create_telephone, DENIED_BASH_PATTERNS

        tel = _create_telephone(
            tmp_team, TEAM, "alice", preamble="test",
        )
        for pattern in DENIED_BASH_PATTERNS:
            assert pattern in tel._denied_bash_patterns
        assert "git push" in tel._denied_bash_patterns
        assert "sqlite3 " in tel._denied_bash_patterns
        assert "DROP TABLE" in tel._denied_bash_patterns

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_disallowed_tools_includes_git_branch(self, _mock_rng, tmp_team):
        """DISALLOWED_TOOLS should include git branch after Phase 3."""
        from delegate.runtime import DISALLOWED_TOOLS

        assert "Bash(git branch:*)" in DISALLOWED_TOOLS
        assert "Bash(git remote:*)" in DISALLOWED_TOOLS
        assert "Bash(git filter-branch:*)" in DISALLOWED_TOOLS


# ---------------------------------------------------------------------------
# Telephone replacement on repo list change
# ---------------------------------------------------------------------------


class TestRepoChangeReplacement:
    """Tests that Telephone is replaced when the repo list changes."""

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    @patch("delegate.network.get_allowed_domains", return_value=["*"])
    def test_telephone_replaced_when_repos_change(self, _mock_domains, _mock_create, _mock_rng, tmp_team):
        """If a new repo is registered mid-session, Telephone should be replaced."""
        from delegate.repo import register_repo

        exchange = TelephoneExchange()

        # Turn 1 — no repos
        _deliver_msg(tmp_team, "alice", body="Turn 1")
        asyncio.run(run_turn(tmp_team, TEAM, "alice", exchange=exchange))
        tel_1 = exchange.get(TEAM, "alice")
        assert tel_1 is not None
        assert _mock_create.call_count == 1

        # Register a repo (simulates mid-session repo addition)
        repo_path = tmp_team / "_test_repos" / "new-repo"
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True,
                       capture_output=True)
        register_repo(tmp_team, TEAM, str(repo_path), name="new-repo")

        # Turn 2 — repo list changed, should create new Telephone
        _deliver_msg(tmp_team, "alice", body="Turn 2")
        asyncio.run(run_turn(tmp_team, TEAM, "alice", exchange=exchange))
        tel_2 = exchange.get(TEAM, "alice")

        # _create_telephone should have been called a second time
        assert _mock_create.call_count == 2
        assert tel_2 is not tel_1

    @patch("delegate.runtime.random.random", return_value=1.0)
    @patch("delegate.runtime._create_telephone", side_effect=_make_mock_tel)
    @patch("delegate.network.get_allowed_domains", return_value=["*"])
    def test_telephone_not_replaced_when_repos_same(self, _mock_domains, _mock_create, _mock_rng, tmp_team):
        """If repo list hasn't changed, Telephone should be reused."""
        exchange = TelephoneExchange()

        _deliver_msg(tmp_team, "alice", body="Turn 1")
        asyncio.run(run_turn(tmp_team, TEAM, "alice", exchange=exchange))

        _deliver_msg(tmp_team, "alice", body="Turn 2")
        asyncio.run(run_turn(tmp_team, TEAM, "alice", exchange=exchange))

        # Same telephone, only one create call
        assert _mock_create.call_count == 1


# ---------------------------------------------------------------------------
# _ensure_task_infra
# ---------------------------------------------------------------------------


class TestEnsureTaskInfra:
    """Tests for daemon-side worktree creation."""

    def test_creates_worktree_for_active_task(self, tmp_team):
        """_ensure_task_infra should create worktrees for active tasks."""
        from delegate.repo import register_repo, get_task_worktree_path
        from delegate.task import create_task
        from delegate.web import _ensure_task_infra

        # Setup: register a real git repo
        repo_path = tmp_team / "_test_repos" / "myrepo"
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True,
                       capture_output=True)
        # Configure git identity (required in CI where global config is absent)
        subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Test"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "test@test.com"],
                       check=True, capture_output=True)
        # Need at least one commit for worktree creation
        (repo_path / "README.md").write_text("# Test")
        subprocess.run(["git", "-C", str(repo_path), "add", "."],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "init"],
                       check=True, capture_output=True)
        # Ensure default branch is named "main" (older Git defaults to "master")
        subprocess.run(["git", "-C", str(repo_path), "branch", "-M", "main"],
                       check=True, capture_output=True)
        register_repo(tmp_team, TEAM, str(repo_path), name="myrepo")

        # Create a task with a repo (no worktree creation — just DB + branch)
        task = create_task(
            tmp_team, TEAM,
            title="Test task",
            assignee="alice",
            repo=["myrepo"],
        )
        task_id = task["id"]

        # Verify worktree doesn't exist yet
        wt = get_task_worktree_path(tmp_team, TEAM, "myrepo", task_id)
        assert not wt.is_dir(), "Worktree should NOT be created by task.create()"

        # Run _ensure_task_infra — should create the worktree
        infra_ready: set[tuple[str, int]] = set()
        _ensure_task_infra(tmp_team, TEAM, infra_ready)

        assert wt.is_dir(), "Worktree should be created by _ensure_task_infra"
        assert (TEAM, task_id) in infra_ready

    def test_skips_already_ready_tasks(self, tmp_team):
        """Tasks already in infra_ready should be skipped."""
        from delegate.web import _ensure_task_infra

        infra_ready: set[tuple[str, int]] = {(TEAM, 999)}
        # Should not crash even with a fake task_id in the cache
        _ensure_task_infra(tmp_team, TEAM, infra_ready)
        assert (TEAM, 999) in infra_ready

    def test_task_without_repos_immediately_ready(self, tmp_team):
        """Tasks without repos should be immediately marked ready."""
        from delegate.task import create_task
        from delegate.web import _ensure_task_infra

        task = create_task(
            tmp_team, TEAM,
            title="No-repo task",
            assignee="alice",
        )

        infra_ready: set[tuple[str, int]] = set()
        _ensure_task_infra(tmp_team, TEAM, infra_ready)

        assert (TEAM, task["id"]) in infra_ready


# ---------------------------------------------------------------------------
# task.create() no longer creates worktrees
# ---------------------------------------------------------------------------


class TestTaskCreateDBOnly:
    """Verify that task.create() no longer calls create_task_worktree."""

    @patch("delegate.repo.create_task_worktree")
    def test_create_does_not_call_worktree(self, mock_wt, tmp_team):
        """task.create() should NOT call create_task_worktree."""
        from delegate.repo import register_repo
        from delegate.task import create_task

        # Register a repo so the task has a repo reference
        repo_path = tmp_team / "_test_repos" / "myrepo"
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], check=True,
                       capture_output=True)
        register_repo(tmp_team, TEAM, str(repo_path), name="myrepo")

        task = create_task(
            tmp_team, TEAM,
            title="Test task",
            assignee="alice",
            repo=["myrepo"],
        )

        # create_task_worktree should NOT have been called
        mock_wt.assert_not_called()

        # But branch should still be recorded
        assert task.get("branch") is not None
        assert "delegate/" in task["branch"]
