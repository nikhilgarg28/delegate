"""Tests for boss/merge.py — merge worker logic.

Tests the new merge flow:
    1. rebase onto main
    2. run tests (skippable)
    3. fast-forward merge
    4. cleanup
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from boss.task import (
    create_task,
    change_status,
    update_task,
    get_task,
)
from boss.config import (
    add_repo, get_repo_approval, get_repo_test_cmd, update_repo_test_cmd, set_boss,
    get_repo_pipeline, set_repo_pipeline, add_pipeline_step, remove_pipeline_step,
)
from boss.merge import merge_task, merge_once, _run_tests, _run_pipeline, _other_unmerged_tasks_on_branch, MergeResult
from boss.bootstrap import bootstrap


SAMPLE_TEAM = "myteam"


@pytest.fixture
def hc_home(tmp_path):
    """Create a fully bootstrapped boss home directory."""
    hc = tmp_path / "hc_home"
    hc.mkdir()
    set_boss(hc, "nikhil")
    bootstrap(hc, SAMPLE_TEAM, manager="edison", agents=["alice", "bob"], qa="sarah")
    return hc


def _make_needs_merge_task(hc_home, title="Task", repo="myrepo", branch="feature/test"):
    """Helper: create a task and advance it to needs_merge status."""
    task = create_task(hc_home, title=title)
    update_task(hc_home, task["id"], repo=repo, branch=branch)
    change_status(hc_home, task["id"], "in_progress")
    change_status(hc_home, task["id"], "review")
    change_status(hc_home, task["id"], "needs_merge")
    return get_task(hc_home, task["id"])


def _setup_git_repo(tmp_path: Path) -> Path:
    """Set up a local git repo with a main branch and initial commit.

    Returns the repo path.
    """
    repo = tmp_path / "source_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "README.md").write_text("# Test repo\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo), capture_output=True)
    return repo


def _make_feature_branch(repo: Path, branch: str, filename: str = "feature.py", content: str = "# New\n"):
    """Create a feature branch with a single commit."""
    subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo), capture_output=True, check=True)
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", f"Add {filename}"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)


def _register_repo_with_symlink(hc_home: Path, name: str, source_repo: Path):
    """Register a repo by creating a symlink in hc_home/repos/."""
    repos_dir = hc_home / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    link = repos_dir / name
    link.symlink_to(source_repo)
    add_repo(hc_home, name, str(source_repo), approval="auto")


# ---------------------------------------------------------------------------
# merge_task tests (with real git)
# ---------------------------------------------------------------------------

class TestMergeTask:
    def test_successful_merge(self, hc_home, tmp_path):
        """Full merge: rebase, skip-tests, ff-merge."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001")
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True
        assert "success" in result.message.lower()

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"
        assert (repo / "feature.py").exists()  # Feature is on main

    def test_rebase_conflict(self, hc_home, tmp_path):
        """Rebase conflict → status becomes 'conflict' and manager notified."""
        repo = _setup_git_repo(tmp_path)

        # Create feature branch that modifies file.txt
        _make_feature_branch(repo, "alice/T0001", filename="file.txt", content="feature version\n")

        # Now modify same file on main
        (repo / "file.txt").write_text("main version\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Diverge main"], cwd=str(repo), capture_output=True, check=True)

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001")
        update_task(hc_home, task["id"], approval_status="approved")

        with patch("boss.merge.notify_conflict") as mock_notify:
            result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)

        assert result.success is False
        assert "conflict" in result.message.lower() or "rebase" in result.message.lower()

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "conflict"
        mock_notify.assert_called_once()

    def test_missing_branch(self, hc_home):
        """Task with no branch should fail."""
        task = create_task(hc_home, title="No branch")
        update_task(hc_home, task["id"], repo="myrepo")
        change_status(hc_home, task["id"], "in_progress")
        change_status(hc_home, task["id"], "review")
        change_status(hc_home, task["id"], "needs_merge")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])
        assert result.success is False
        assert "no branch" in result.message.lower()

    def test_missing_repo(self, hc_home):
        """Task with no repo should fail."""
        task = create_task(hc_home, title="No repo")
        update_task(hc_home, task["id"], branch="some/branch")
        change_status(hc_home, task["id"], "in_progress")
        change_status(hc_home, task["id"], "review")
        change_status(hc_home, task["id"], "needs_merge")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])
        assert result.success is False
        assert "no repo" in result.message.lower()

    def test_merge_removes_worktree_before_rebase(self, hc_home, tmp_path):
        """Merge should remove agent worktree before rebasing so git doesn't
        refuse to rebase a branch checked out in another worktree."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create a worktree for the agent (simulating what happens during task work)
        wt_dir = hc_home / "teams" / SAMPLE_TEAM / "agents" / "alice" / "worktrees"
        wt_dir.mkdir(parents=True, exist_ok=True)
        wt_path = wt_dir / "myrepo-T0001"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(repo), capture_output=True, check=True,
        )
        assert wt_path.exists()

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], assignee="alice", approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"
        assert not wt_path.exists(), "Worktree should have been removed"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"

    def test_merge_succeeds_with_unstaged_changes(self, hc_home, tmp_path):
        """Merge should stash unstaged changes before rebasing and restore after."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create an unstaged change in the repo working directory
        (repo / "untracked_file.js").write_text("// generated\n")

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"

        # The untracked file should still be present after merge
        assert (repo / "untracked_file.js").exists(), "Stashed file should be restored"

    def test_merge_succeeds_with_modified_tracked_file(self, hc_home, tmp_path):
        """Merge should stash modified tracked files before rebasing."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Modify a tracked file without staging it
        (repo / "README.md").write_text("# Modified but not staged\n")

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"

    def test_rebase_onto_with_base_sha(self, hc_home, tmp_path):
        """When base_sha is set on the task, rebase uses --onto to replay
        only the agent's commits (after base_sha) onto current main."""
        repo = _setup_git_repo(tmp_path)

        # Record the initial commit SHA — this will be our base_sha
        base_sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        base_sha = base_sha_result.stdout.strip()

        # Create a feature branch with one commit
        branch = "alice/T0001-onto"
        _make_feature_branch(repo, branch, filename="onto_feature.py", content="# onto\n")

        # Advance main with a non-conflicting commit (simulates main moving forward)
        (repo / "mainfile.txt").write_text("main extra\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Advance main"],
            cwd=str(repo), capture_output=True, check=True,
        )

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved", base_sha=base_sha)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge with --onto failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"
        assert (repo / "onto_feature.py").exists()  # Agent's commit landed

    def test_rebase_fallback_without_base_sha(self, hc_home, tmp_path):
        """When base_sha is empty/None the merge falls back to plain rebase."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001-nobase"
        _make_feature_branch(repo, branch, filename="nobase.py", content="# no base\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        # Explicitly set base_sha to empty string (simulating a task without it)
        update_task(hc_home, task["id"], approval_status="approved", base_sha="")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Fallback merge failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"
        assert (repo / "nobase.py").exists()

    def test_rebase_onto_excludes_reverted_commits(self, hc_home, tmp_path):
        """--onto correctly excludes commits that were reverted from main.

        Scenario:
        - main: M0 → M1 → M2 (base_sha = M2)
        - agent branch: M2 → A1
        - main is then reset to M0 (M1, M2 are reverted)
        - rebase --onto main M2 branch replays only A1 onto M0
        """
        repo = _setup_git_repo(tmp_path)

        # M0 is the initial commit. Add M1 and M2.
        (repo / "m1.txt").write_text("m1\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "M1"], cwd=str(repo), capture_output=True, check=True)

        (repo / "m2.txt").write_text("m2\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "M2"], cwd=str(repo), capture_output=True, check=True)

        # Record base_sha (M2)
        base_sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        base_sha = base_sha_result.stdout.strip()

        # Create agent branch from M2
        branch = "alice/T0001-revert"
        subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo), capture_output=True, check=True)
        (repo / "agent_work.py").write_text("# agent work\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Agent commit A1"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)

        # Reset main back to M0 (removing M1 and M2)
        m0_result = subprocess.run(
            ["git", "rev-parse", "HEAD~2"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        m0_sha = m0_result.stdout.strip()
        subprocess.run(
            ["git", "reset", "--hard", m0_sha], cwd=str(repo),
            capture_output=True, check=True,
        )

        # Verify main no longer has m1.txt or m2.txt
        assert not (repo / "m1.txt").exists()
        assert not (repo / "m2.txt").exists()

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved", base_sha=base_sha)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Rebase --onto with reverted commits failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"

        # Agent's work should be on main
        assert (repo / "agent_work.py").exists()
        # M1 and M2 files should NOT be on main (they were reverted)
        assert not (repo / "m1.txt").exists(), "m1.txt should not be on main (reverted commit)"
        assert not (repo / "m2.txt").exists(), "m2.txt should not be on main (reverted commit)"


# ---------------------------------------------------------------------------
# merge_once tests
# ---------------------------------------------------------------------------

class TestMergeBaseAndTip:
    """Tests for merge_base and merge_tip fields."""

    def test_empty_on_task_creation(self, hc_home):
        """merge_base and merge_tip should be empty strings on new tasks."""
        task = create_task(hc_home, title="New task")
        assert task["merge_base"] == ""
        assert task["merge_tip"] == ""

    def test_set_after_successful_merge(self, hc_home, tmp_path):
        """merge_base and merge_tip should be set after a successful merge."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Record main HEAD before merge (expected merge_base)
        pre_merge = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        expected_base = pre_merge.stdout.strip()

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        updated = get_task(hc_home, task["id"])
        assert updated["merge_base"] == expected_base
        assert updated["merge_tip"] != ""
        assert updated["merge_tip"] != updated["merge_base"]

        # merge_tip should be the current HEAD of main
        post_merge = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        assert updated["merge_tip"] == post_merge.stdout.strip()

    def test_merge_base_tip_give_correct_diff(self, hc_home, tmp_path):
        """git diff merge_base..merge_tip should show exactly the merged changes."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch, filename="new_feature.py", content="# feature code\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        updated = get_task(hc_home, task["id"])
        diff_result = subprocess.run(
            ["git", "diff", f"{updated['merge_base']}..{updated['merge_tip']}"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        assert "new_feature.py" in diff_result.stdout
        assert "# feature code" in diff_result.stdout

    def test_not_set_on_failed_merge(self, hc_home, tmp_path):
        """merge_base and merge_tip should remain empty on failed merges."""
        repo = _setup_git_repo(tmp_path)

        # Create a conflicting scenario
        _make_feature_branch(repo, "alice/T0001", filename="file.txt", content="feature\n")
        (repo / "file.txt").write_text("main conflict\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Conflict on main"], cwd=str(repo), capture_output=True, check=True)

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001")
        update_task(hc_home, task["id"], approval_status="approved")

        with patch("boss.merge.notify_conflict"):
            result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)

        assert result.success is False
        updated = get_task(hc_home, task["id"])
        assert updated["merge_base"] == ""
        assert updated["merge_tip"] == ""


class TestMergeOnce:
    def test_empty_when_no_tasks(self, hc_home):
        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_skips_task_without_repo(self, hc_home):
        """Tasks without a repo field are skipped."""
        task = create_task(hc_home, title="No repo")
        update_task(hc_home, task["id"], branch="some/branch")
        change_status(hc_home, task["id"], "in_progress")
        change_status(hc_home, task["id"], "review")
        change_status(hc_home, task["id"], "needs_merge")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_skips_manual_unapproved(self, hc_home):
        """Manual approval tasks without approval_status='approved' are skipped."""
        add_repo(hc_home, "myrepo", "/fake", approval="manual")
        _make_needs_merge_task(hc_home, title="Unapproved")
        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_auto_merge_processes(self, hc_home, tmp_path):
        """Auto approval tasks should be processed without boss approval."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert len(results) == 1
        assert results[0].success is True

    def test_manual_approved_processes(self, hc_home, tmp_path):
        """Manual tasks with approval_status='approved' should be processed."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")

        repos_dir = hc_home / "repos"
        repos_dir.mkdir(parents=True, exist_ok=True)
        (repos_dir / "myrepo").symlink_to(repo)
        add_repo(hc_home, "myrepo", str(repo), approval="manual")

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001")
        update_task(hc_home, task["id"], approval_status="approved")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert len(results) == 1
        assert results[0].success is True

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"


# ---------------------------------------------------------------------------
# get_repo_approval tests
# ---------------------------------------------------------------------------

class TestGetRepoApproval:
    def test_returns_manual_by_default(self, hc_home):
        assert get_repo_approval(hc_home, "nonexistent") == "manual"

    def test_reads_from_config(self, hc_home):
        add_repo(hc_home, "myrepo", "/tmp/repo", approval="auto")
        add_repo(hc_home, "other", "/tmp/other", approval="manual")

        assert get_repo_approval(hc_home, "myrepo") == "auto"
        assert get_repo_approval(hc_home, "other") == "manual"
        assert get_repo_approval(hc_home, "missing") == "manual"


# ---------------------------------------------------------------------------
# get_repo_test_cmd / update_repo_test_cmd tests
# ---------------------------------------------------------------------------

class TestRepoTestCmd:
    def test_returns_none_by_default(self, hc_home):
        """test_cmd should be None for repos that don't configure it."""
        assert get_repo_test_cmd(hc_home, "nonexistent") is None

    def test_returns_none_when_not_set(self, hc_home):
        """Repo registered without test_cmd should return None."""
        add_repo(hc_home, "myrepo", "/tmp/repo")
        assert get_repo_test_cmd(hc_home, "myrepo") is None

    def test_add_repo_with_test_cmd(self, hc_home):
        """add_repo with test_cmd stores it correctly."""
        add_repo(hc_home, "myrepo", "/tmp/repo", test_cmd="/usr/bin/python -m pytest -x")
        assert get_repo_test_cmd(hc_home, "myrepo") == "/usr/bin/python -m pytest -x"

    def test_update_repo_test_cmd(self, hc_home):
        """update_repo_test_cmd sets/changes the test command for an existing repo."""
        add_repo(hc_home, "myrepo", "/tmp/repo")
        assert get_repo_test_cmd(hc_home, "myrepo") is None

        update_repo_test_cmd(hc_home, "myrepo", "/path/to/venv/bin/python -m pytest -x -q")
        assert get_repo_test_cmd(hc_home, "myrepo") == "/path/to/venv/bin/python -m pytest -x -q"

    def test_update_repo_test_cmd_missing_repo(self, hc_home):
        """update_repo_test_cmd raises KeyError for unknown repo."""
        with pytest.raises(KeyError, match="not found"):
            update_repo_test_cmd(hc_home, "no_such_repo", "pytest")


# ---------------------------------------------------------------------------
# _run_tests with configured test_cmd
# ---------------------------------------------------------------------------

class TestRunTestsWithConfig:
    def test_uses_configured_test_cmd(self, hc_home, tmp_path):
        """When a test_cmd is configured, _run_tests should use it instead of auto-detection."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Configure a test command that simply succeeds
        update_repo_test_cmd(hc_home, "myrepo", "echo tests-passed")

        ok, output = _run_tests(str(repo), branch, hc_home=hc_home, repo_name="myrepo")
        assert ok is True
        assert "tests-passed" in output

    def test_configured_test_cmd_failure(self, hc_home, tmp_path):
        """When configured test_cmd fails, _run_tests should report failure."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Configure a test command that always fails
        update_repo_test_cmd(hc_home, "myrepo", "false")

        ok, output = _run_tests(str(repo), branch, hc_home=hc_home, repo_name="myrepo")
        assert ok is False

    def test_falls_back_to_auto_detect_when_no_config(self, hc_home, tmp_path):
        """When no test_cmd is configured, _run_tests should fall back to auto-detection."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # No test_cmd configured — repo has no pyproject.toml / package.json / Makefile
        # so auto-detection should skip tests
        ok, output = _run_tests(str(repo), branch, hc_home=hc_home, repo_name="myrepo")
        assert ok is True
        assert "no test runner" in output.lower()

    def test_falls_back_without_hc_home(self, tmp_path):
        """When hc_home is None, _run_tests should use auto-detection only."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)

        # No hc_home — auto-detection only, no test files → skip
        ok, output = _run_tests(str(repo), branch)
        assert ok is True
        assert "no test runner" in output.lower()


# ---------------------------------------------------------------------------
# Pipeline config CRUD tests
# ---------------------------------------------------------------------------

class TestPipelineConfig:
    def test_get_pipeline_returns_none_by_default(self, hc_home):
        """Repo without pipeline or test_cmd returns None."""
        add_repo(hc_home, "myrepo", "/tmp/repo")
        assert get_repo_pipeline(hc_home, "myrepo") is None

    def test_get_pipeline_returns_none_for_missing_repo(self, hc_home):
        assert get_repo_pipeline(hc_home, "nonexistent") is None

    def test_backward_compat_test_cmd_as_pipeline(self, hc_home):
        """Legacy test_cmd should be returned as single-step pipeline."""
        add_repo(hc_home, "myrepo", "/tmp/repo", test_cmd="pytest -x")
        pipeline = get_repo_pipeline(hc_home, "myrepo")
        assert pipeline is not None
        assert len(pipeline) == 1
        assert pipeline[0]["name"] == "test"
        assert pipeline[0]["run"] == "pytest -x"

    def test_explicit_pipeline_overrides_test_cmd(self, hc_home):
        """When both pipeline and test_cmd exist, pipeline wins."""
        add_repo(hc_home, "myrepo", "/tmp/repo", test_cmd="pytest -x")
        set_repo_pipeline(hc_home, "myrepo", [
            {"name": "lint", "run": "flake8"},
            {"name": "test", "run": "pytest -v"},
        ])
        pipeline = get_repo_pipeline(hc_home, "myrepo")
        assert len(pipeline) == 2
        assert pipeline[0]["name"] == "lint"
        assert pipeline[1]["name"] == "test"

    def test_set_repo_pipeline(self, hc_home):
        add_repo(hc_home, "myrepo", "/tmp/repo")
        steps = [
            {"name": "install", "run": "npm install"},
            {"name": "build", "run": "npm run build"},
            {"name": "test", "run": "npm test"},
        ]
        set_repo_pipeline(hc_home, "myrepo", steps)
        assert get_repo_pipeline(hc_home, "myrepo") == steps

    def test_set_repo_pipeline_missing_repo(self, hc_home):
        with pytest.raises(KeyError, match="not found"):
            set_repo_pipeline(hc_home, "no_such_repo", [])

    def test_add_pipeline_step(self, hc_home):
        add_repo(hc_home, "myrepo", "/tmp/repo")
        add_pipeline_step(hc_home, "myrepo", "lint", "flake8 .")
        add_pipeline_step(hc_home, "myrepo", "test", "pytest -x")
        pipeline = get_repo_pipeline(hc_home, "myrepo")
        assert len(pipeline) == 2
        assert pipeline[0] == {"name": "lint", "run": "flake8 ."}
        assert pipeline[1] == {"name": "test", "run": "pytest -x"}

    def test_add_pipeline_step_migrates_test_cmd(self, hc_home):
        """Adding a step to a repo with legacy test_cmd migrates it first."""
        add_repo(hc_home, "myrepo", "/tmp/repo", test_cmd="pytest -x")
        add_pipeline_step(hc_home, "myrepo", "lint", "flake8 .")
        pipeline = get_repo_pipeline(hc_home, "myrepo")
        assert len(pipeline) == 2
        assert pipeline[0] == {"name": "test", "run": "pytest -x"}  # migrated
        assert pipeline[1] == {"name": "lint", "run": "flake8 ."}

    def test_add_pipeline_step_duplicate_name(self, hc_home):
        add_repo(hc_home, "myrepo", "/tmp/repo")
        add_pipeline_step(hc_home, "myrepo", "test", "pytest")
        with pytest.raises(ValueError, match="already exists"):
            add_pipeline_step(hc_home, "myrepo", "test", "pytest -v")

    def test_add_pipeline_step_missing_repo(self, hc_home):
        with pytest.raises(KeyError, match="not found"):
            add_pipeline_step(hc_home, "no_such_repo", "test", "pytest")

    def test_remove_pipeline_step(self, hc_home):
        add_repo(hc_home, "myrepo", "/tmp/repo")
        set_repo_pipeline(hc_home, "myrepo", [
            {"name": "lint", "run": "flake8"},
            {"name": "test", "run": "pytest"},
        ])
        remove_pipeline_step(hc_home, "myrepo", "lint")
        pipeline = get_repo_pipeline(hc_home, "myrepo")
        assert len(pipeline) == 1
        assert pipeline[0]["name"] == "test"

    def test_remove_pipeline_step_not_found(self, hc_home):
        add_repo(hc_home, "myrepo", "/tmp/repo")
        set_repo_pipeline(hc_home, "myrepo", [{"name": "test", "run": "pytest"}])
        with pytest.raises(KeyError, match="not found"):
            remove_pipeline_step(hc_home, "myrepo", "nonexistent")

    def test_remove_pipeline_step_no_pipeline(self, hc_home):
        add_repo(hc_home, "myrepo", "/tmp/repo")
        with pytest.raises(KeyError, match="No pipeline"):
            remove_pipeline_step(hc_home, "myrepo", "test")

    def test_remove_pipeline_step_missing_repo(self, hc_home):
        with pytest.raises(KeyError, match="not found"):
            remove_pipeline_step(hc_home, "no_such_repo", "test")


# ---------------------------------------------------------------------------
# _run_pipeline tests
# ---------------------------------------------------------------------------

class TestRunPipeline:
    def test_pipeline_all_steps_pass(self, hc_home, tmp_path):
        """Pipeline with all passing steps should succeed."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        set_repo_pipeline(hc_home, "myrepo", [
            {"name": "step1", "run": "echo step-one-ok"},
            {"name": "step2", "run": "echo step-two-ok"},
        ])

        ok, output = _run_pipeline(str(repo), branch, hc_home=hc_home, repo_name="myrepo")
        assert ok is True
        assert "step-one-ok" in output
        assert "step-two-ok" in output

    def test_pipeline_stops_on_first_failure(self, hc_home, tmp_path):
        """Pipeline should stop on first failing step and report step name."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        set_repo_pipeline(hc_home, "myrepo", [
            {"name": "install", "run": "echo installing"},
            {"name": "build", "run": "false"},
            {"name": "test", "run": "echo should-not-run"},
        ])

        ok, output = _run_pipeline(str(repo), branch, hc_home=hc_home, repo_name="myrepo")
        assert ok is False
        assert "build" in output.lower()
        assert "should-not-run" not in output

    def test_pipeline_backward_compat_test_cmd(self, hc_home, tmp_path):
        """Legacy test_cmd should work as a single-step pipeline."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        update_repo_test_cmd(hc_home, "myrepo", "echo legacy-test-passed")

        ok, output = _run_pipeline(str(repo), branch, hc_home=hc_home, repo_name="myrepo")
        assert ok is True
        assert "legacy-test-passed" in output

    def test_pipeline_falls_back_to_autodetect(self, hc_home, tmp_path):
        """When no pipeline and no test_cmd, falls back to auto-detection."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # No pipeline, no test_cmd, no pyproject.toml → skip tests
        ok, output = _run_pipeline(str(repo), branch, hc_home=hc_home, repo_name="myrepo")
        assert ok is True
        assert "no test runner" in output.lower()

    def test_merge_with_pipeline_failure(self, hc_home, tmp_path):
        """merge_task should fail when pipeline step fails."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        set_repo_pipeline(hc_home, "myrepo", [
            {"name": "lint", "run": "echo lint-ok"},
            {"name": "test", "run": "false"},
        ])

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        with patch("boss.merge.notify_conflict"):
            result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False
        assert "pipeline failed" in result.message.lower() or "test" in result.message.lower()

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "conflict"

    def test_merge_with_pipeline_success(self, hc_home, tmp_path):
        """merge_task should succeed when all pipeline steps pass."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        set_repo_pipeline(hc_home, "myrepo", [
            {"name": "install", "run": "echo installing"},
            {"name": "test", "run": "echo all-tests-pass"},
        ])

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])
        assert result.success is True

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"


# ---------------------------------------------------------------------------
# Shared-branch safety tests (T0053)
# ---------------------------------------------------------------------------

class TestSharedBranchCleanup:
    """When multiple tasks share a branch, cleanup should only happen once
    the last task on that branch is merged."""

    def test_other_unmerged_tasks_on_branch_helper(self, hc_home):
        """_other_unmerged_tasks_on_branch returns True when another task
        with the same branch is not yet merged."""
        t1 = create_task(hc_home, title="Task 1")
        t2 = create_task(hc_home, title="Task 2")
        update_task(hc_home, t1["id"], branch="shared/branch", repo="myrepo")
        update_task(hc_home, t2["id"], branch="shared/branch", repo="myrepo")
        change_status(hc_home, t1["id"], "in_progress")
        change_status(hc_home, t2["id"], "in_progress")

        # Both in_progress — each should see the other as unmerged
        assert _other_unmerged_tasks_on_branch(hc_home, "shared/branch", t1["id"]) is True
        assert _other_unmerged_tasks_on_branch(hc_home, "shared/branch", t2["id"]) is True

    def test_no_other_unmerged_when_all_merged(self, hc_home, tmp_path):
        """_other_unmerged_tasks_on_branch returns False when the only other
        task on the branch is already merged."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "shared/branch")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        t1 = create_task(hc_home, title="Task 1")
        t2 = create_task(hc_home, title="Task 2")
        update_task(hc_home, t1["id"], branch="shared/branch", repo="myrepo")
        update_task(hc_home, t2["id"], branch="shared/branch", repo="myrepo")

        # Advance t1 to merged
        change_status(hc_home, t1["id"], "in_progress")
        change_status(hc_home, t1["id"], "review")
        change_status(hc_home, t1["id"], "needs_merge")
        change_status(hc_home, t1["id"], "merged")

        # t2 is in_progress — from t2's perspective, t1 is merged, so False
        change_status(hc_home, t2["id"], "in_progress")
        assert _other_unmerged_tasks_on_branch(hc_home, "shared/branch", t2["id"]) is False

    def test_no_other_when_different_branch(self, hc_home):
        """Tasks on different branches do not interfere."""
        t1 = create_task(hc_home, title="Task 1")
        t2 = create_task(hc_home, title="Task 2")
        update_task(hc_home, t1["id"], branch="branch-a", repo="myrepo")
        update_task(hc_home, t2["id"], branch="branch-b", repo="myrepo")
        change_status(hc_home, t1["id"], "in_progress")
        change_status(hc_home, t2["id"], "in_progress")

        assert _other_unmerged_tasks_on_branch(hc_home, "branch-a", t1["id"]) is False
        assert _other_unmerged_tasks_on_branch(hc_home, "branch-b", t2["id"]) is False

    def test_branch_kept_when_sibling_task_unmerged(self, hc_home, tmp_path):
        """Merging one task should NOT delete the branch when a sibling task
        on the same branch is still unmerged."""
        repo = _setup_git_repo(tmp_path)
        branch = "shared/T0001-T0002"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create two tasks sharing the same branch
        t1 = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        t2 = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, t1["id"], approval_status="approved")
        update_task(hc_home, t2["id"], approval_status="approved")

        # Merge the first task
        result = merge_task(hc_home, SAMPLE_TEAM, t1["id"], skip_tests=True)
        assert result.success is True
        assert get_task(hc_home, t1["id"])["status"] == "merged"

        # The branch must still exist because t2 is not merged yet
        branch_check = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch in branch_check.stdout, (
            f"Branch '{branch}' was deleted prematurely — t2 still needs it"
        )

    def test_branch_deleted_when_last_task_merged(self, hc_home, tmp_path):
        """Branch should be deleted after the last task on it is merged."""
        repo = _setup_git_repo(tmp_path)
        branch = "shared/T0001-T0002"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        t1 = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        t2 = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, t1["id"], approval_status="approved")
        update_task(hc_home, t2["id"], approval_status="approved")

        # Merge t1 (branch kept because t2 still unmerged)
        r1 = merge_task(hc_home, SAMPLE_TEAM, t1["id"], skip_tests=True)
        assert r1.success is True

        # Merge t2 — now last task, branch should be cleaned up
        r2 = merge_task(hc_home, SAMPLE_TEAM, t2["id"], skip_tests=True)
        assert r2.success is True

        # Branch should now be deleted
        branch_check = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch not in branch_check.stdout, (
            f"Branch '{branch}' should have been deleted after last task merged"
        )

    def test_single_task_branch_deleted_normally(self, hc_home, tmp_path):
        """When only one task uses a branch, cleanup proceeds normally."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        # Branch should be deleted (only one task)
        branch_check = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch not in branch_check.stdout
