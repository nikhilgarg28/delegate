"""Tests for delegate/sim_boss.py — simulated boss for eval runs."""

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="sim_boss tests disabled — not in active use")
import yaml

from delegate.bootstrap import bootstrap
from delegate.config import set_boss
from delegate.mailbox import send as mailbox_send, read_inbox, deliver, Message
from delegate.sim_boss import (
    sim_boss_respond,
    run_sim_boss,
    start_sim_boss_thread,
    load_task_specs_from_dir,
    _build_prompt,
    _match_task_spec,
    _process_inbox,
    SIM_BOSS_PROMPT,
)

TEAM = "simteam"
BOSS_NAME = "boss"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def team_root(tmp_path):
    """Create a bootstrapped team directory for sim-boss tests."""
    hc_home = tmp_path / "hc"
    hc_home.mkdir()
    set_boss(hc_home, BOSS_NAME)
    bootstrap(hc_home, team_name=TEAM, manager="manager", agents=["alice"])
    return hc_home


@pytest.fixture
def sample_task_specs():
    """Sample task specs dict for testing."""
    return {
        "Fix off-by-one bug": "Fix the pagination helper to use (page - 1) * page_size.",
        "Add CSV reporter": "Create a CSV reporter module that exports task data.",
    }


def _deliver_to_inbox(root, sender, recipient, body):
    """Deliver a message directly to a recipient's inbox (bypass outbox/router)."""
    from datetime import datetime, timezone
    msg = Message(
        sender=sender,
        recipient=recipient,
        time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        body=body,
    )
    deliver(root, TEAM, msg)


async def mock_llm_echo(prompt: str) -> str:
    """Mock LLM that echoes back a summary of the prompt."""
    return f"Sim-boss response to: {prompt[:60]}..."


async def mock_llm_fixed(prompt: str) -> str:
    """Mock LLM that returns a fixed response."""
    return "The task requires fixing the off-by-one error in pagination."


# ---------------------------------------------------------------------------
# Unit tests: prompt building
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Tests for _build_prompt()."""

    def test_includes_task_spec(self):
        """The prompt includes the task spec."""
        prompt = _build_prompt("Fix the bug", "What should I do?")
        assert "Fix the bug" in prompt

    def test_includes_message(self):
        """The prompt includes the manager's message."""
        prompt = _build_prompt("Fix the bug", "What should I do?")
        assert "What should I do?" in prompt

    def test_uses_template_format(self):
        """The prompt starts with the standardized template."""
        prompt = _build_prompt("spec text", "question")
        assert "You are a boss" in prompt
        assert "spec text" in prompt

    def test_manager_message_label(self):
        """The prompt labels the manager's message clearly."""
        prompt = _build_prompt("spec", "How do I start?")
        assert "Manager's message:" in prompt


# ---------------------------------------------------------------------------
# Unit tests: task spec matching
# ---------------------------------------------------------------------------


class TestMatchTaskSpec:
    """Tests for _match_task_spec()."""

    def test_matches_by_title(self, sample_task_specs):
        """Matches when a task title appears in the message."""
        spec = _match_task_spec(
            "I need help with Fix off-by-one bug",
            sample_task_specs,
        )
        assert spec is not None
        assert "Fix off-by-one bug" in spec
        assert "pagination" in spec

    def test_case_insensitive_match(self, sample_task_specs):
        """Matching is case-insensitive."""
        spec = _match_task_spec(
            "What about the fix off-by-one bug task?",
            sample_task_specs,
        )
        assert spec is not None
        assert "pagination" in spec

    def test_no_match_returns_all_specs(self, sample_task_specs):
        """When no title matches, returns all specs as context."""
        spec = _match_task_spec(
            "What should we work on next?",
            sample_task_specs,
        )
        assert spec is not None
        # Should contain both task titles
        assert "Fix off-by-one bug" in spec
        assert "Add CSV reporter" in spec

    def test_empty_specs_returns_none(self):
        """Returns None when there are no task specs."""
        spec = _match_task_spec("hello", {})
        assert spec is None

    def test_returns_title_and_description(self, sample_task_specs):
        """Matched spec includes both title and description."""
        spec = _match_task_spec(
            "Tell me about Add CSV reporter",
            sample_task_specs,
        )
        assert "Add CSV reporter" in spec
        assert "CSV reporter module" in spec


# ---------------------------------------------------------------------------
# Unit tests: sim_boss_respond
# ---------------------------------------------------------------------------


class TestSimBossRespond:
    """Tests for sim_boss_respond() with mocked LLM."""

    @pytest.mark.asyncio
    async def test_returns_string(self, team_root):
        """Returns a string response."""
        result = await sim_boss_respond(
            team_root,
            TEAM,
            task_spec="Fix the pagination bug",
            message="What should I fix?",
            llm_query=mock_llm_echo,
        )
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_passes_spec_to_llm(self, team_root):
        """The task spec is included in the prompt sent to the LLM."""
        captured_prompts = []

        async def capture_llm(prompt):
            captured_prompts.append(prompt)
            return "OK"

        await sim_boss_respond(
            team_root,
            TEAM,
            task_spec="Unique spec content XYZ123",
            message="question",
            llm_query=capture_llm,
        )
        assert len(captured_prompts) == 1
        assert "Unique spec content XYZ123" in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_passes_message_to_llm(self, team_root):
        """The manager's message is included in the prompt sent to the LLM."""
        captured_prompts = []

        async def capture_llm(prompt):
            captured_prompts.append(prompt)
            return "OK"

        await sim_boss_respond(
            team_root,
            TEAM,
            task_spec="spec",
            message="Unique question ABC789",
            llm_query=capture_llm,
        )
        assert "Unique question ABC789" in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_uses_fixed_response(self, team_root):
        """Mock LLM response is returned as-is."""
        result = await sim_boss_respond(
            team_root,
            TEAM,
            task_spec="spec",
            message="question",
            llm_query=mock_llm_fixed,
        )
        assert result == "The task requires fixing the off-by-one error in pagination."


# ---------------------------------------------------------------------------
# Integration tests: inbox polling + response routing
# ---------------------------------------------------------------------------


class TestProcessInbox:
    """Tests for _process_inbox() — inbox processing logic."""

    @pytest.mark.asyncio
    async def test_processes_unread_messages(self, team_root):
        """Processes messages in the boss's inbox and sends responses."""
        # Send a message from manager to boss
        _deliver_to_inbox(team_root, "manager", BOSS_NAME, "What should I work on?")

        # Process inbox
        processed = await _process_inbox(
            team_root,
            TEAM,
            task_specs={"Fix bug": "Fix the off-by-one bug"},
            boss_name=BOSS_NAME,
            llm_query=mock_llm_fixed,
        )
        assert processed == 1

        # Boss's inbox should now be empty (messages marked read)
        remaining = read_inbox(team_root, TEAM, BOSS_NAME, unread_only=True)
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_sends_response_to_sender(self, team_root):
        """The response is routed back to the message sender."""
        _deliver_to_inbox(team_root, "manager", BOSS_NAME, "How do I start?")

        await _process_inbox(
            team_root,
            TEAM,
            task_specs={"Task": "Description"},
            boss_name=BOSS_NAME,
            llm_query=mock_llm_fixed,
        )

        # With immediate delivery, response is already delivered to manager's inbox
        from delegate.mailbox import read_inbox
        inbox = read_inbox(team_root, TEAM, "manager", unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == BOSS_NAME

    @pytest.mark.asyncio
    async def test_handles_empty_inbox(self, team_root):
        """Returns 0 when no unread messages."""
        processed = await _process_inbox(
            team_root,
            TEAM,
            task_specs={"Task": "Desc"},
            boss_name=BOSS_NAME,
            llm_query=mock_llm_echo,
        )
        assert processed == 0

    @pytest.mark.asyncio
    async def test_processes_multiple_messages(self, team_root):
        """Processes all unread messages in one call."""
        _deliver_to_inbox(team_root, "manager", BOSS_NAME, "Question 1")
        _deliver_to_inbox(team_root, "alice", BOSS_NAME, "Question 2")

        processed = await _process_inbox(
            team_root,
            TEAM,
            task_specs={"Task": "Desc"},
            boss_name=BOSS_NAME,
            llm_query=mock_llm_echo,
        )
        assert processed == 2

    @pytest.mark.asyncio
    async def test_no_specs_returns_fallback(self, team_root):
        """When no specs match and dict is empty, returns fallback message."""
        _deliver_to_inbox(team_root, "manager", BOSS_NAME, "What now?")

        await _process_inbox(
            team_root,
            TEAM,
            task_specs={},
            boss_name=BOSS_NAME,
            llm_query=mock_llm_echo,
        )

        # With immediate delivery, response is already in manager's inbox
        from delegate.mailbox import read_inbox as _ri
        inbox = _ri(team_root, TEAM, "manager", unread_only=True)
        assert len(inbox) == 1
        assert "don't have any task specs" in inbox[0].body


# ---------------------------------------------------------------------------
# Integration tests: run_sim_boss polling loop
# ---------------------------------------------------------------------------


class TestRunSimBoss:
    """Tests for run_sim_boss() — the full polling loop."""

    @pytest.mark.asyncio
    async def test_stops_on_event(self, team_root):
        """The polling loop stops when stop_event is set."""
        import threading as _threading
        stop_event = _threading.Event()

        # Set stop immediately
        stop_event.set()

        # Should exit quickly without error
        await run_sim_boss(
            team_root,
            TEAM,
            task_specs={"Task": "Desc"},
            poll_interval=0.1,
            stop_event=stop_event,
            llm_query=mock_llm_echo,
        )

    @pytest.mark.asyncio
    async def test_processes_messages_before_stopping(self, team_root):
        """Messages arriving before stop are processed."""
        import threading as _threading
        stop_event = _threading.Event()

        # Send a message first
        _deliver_to_inbox(team_root, "manager", BOSS_NAME, "Help me with task")

        # Schedule stop after a short delay via a timer thread
        timer = _threading.Timer(0.3, stop_event.set)
        timer.start()

        await run_sim_boss(
            team_root,
            TEAM,
            task_specs={"Task": "Fix the bug"},
            poll_interval=0.1,
            stop_event=stop_event,
            llm_query=mock_llm_fixed,
        )

        timer.join()

        # Boss inbox should be cleared
        remaining = read_inbox(team_root, TEAM, BOSS_NAME, unread_only=True)
        assert len(remaining) == 0

        # Response should have been delivered to the sender's inbox
        from delegate.mailbox import read_outbox
        outbox = read_outbox(team_root, TEAM, BOSS_NAME, pending_only=False)
        assert len(outbox) >= 1


# ---------------------------------------------------------------------------
# Integration tests: thread-based start
# ---------------------------------------------------------------------------


class TestStartSimBossThread:
    """Tests for start_sim_boss_thread()."""

    def test_starts_and_stops(self, team_root):
        """Thread starts and can be stopped cleanly."""
        thread, stop_event = start_sim_boss_thread(
            team_root,
            TEAM,
            task_specs={"Task": "Desc"},
            poll_interval=0.1,
            llm_query=mock_llm_echo,
        )

        assert thread.is_alive()

        # Stop it
        stop_event.set()
        thread.join(timeout=3.0)
        assert not thread.is_alive()

    def test_processes_messages_in_thread(self, team_root):
        """Messages are processed when running in a thread."""
        import time

        _deliver_to_inbox(team_root, "manager", BOSS_NAME, "Thread test question")

        thread, stop_event = start_sim_boss_thread(
            team_root,
            TEAM,
            task_specs={"Task": "Fix it"},
            poll_interval=0.1,
            llm_query=mock_llm_fixed,
        )

        # Wait a bit for processing
        time.sleep(0.5)

        # Stop the thread
        stop_event.set()
        thread.join(timeout=3.0)

        # Inbox should be cleared
        remaining = read_inbox(team_root, TEAM, BOSS_NAME, unread_only=True)
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# Unit tests: load_task_specs_from_dir
# ---------------------------------------------------------------------------


class TestLoadTaskSpecsFromDir:
    """Tests for load_task_specs_from_dir()."""

    def test_loads_yaml_files(self, tmp_path):
        """Loads task specs from YAML files in a directory."""
        specs_dir = tmp_path / "tasks"
        specs_dir.mkdir()

        spec1 = {
            "title": "Fix bug",
            "description": "Fix the off-by-one error.",
            "timeout_seconds": 120,
            "tags": ["bugfix"],
            "acceptance_criteria": [{"file_exists": {"path": "src/fix.py"}}],
        }
        spec2 = {
            "title": "Add feature",
            "description": "Add a new CSV reporter.",
            "timeout_seconds": 300,
            "tags": ["feature"],
            "acceptance_criteria": [{"file_exists": {"path": "src/csv.py"}}],
        }

        (specs_dir / "fix_bug.yaml").write_text(yaml.dump(spec1))
        (specs_dir / "add_feature.yaml").write_text(yaml.dump(spec2))

        result = load_task_specs_from_dir(specs_dir)
        assert len(result) == 2
        assert "Fix bug" in result
        assert "Add feature" in result
        assert "off-by-one" in result["Fix bug"]

    def test_empty_directory(self, tmp_path):
        """Returns empty dict for empty directory."""
        specs_dir = tmp_path / "empty"
        specs_dir.mkdir()
        result = load_task_specs_from_dir(specs_dir)
        assert result == {}

    def test_nonexistent_directory(self, tmp_path):
        """Returns empty dict for nonexistent directory."""
        result = load_task_specs_from_dir(tmp_path / "nope")
        assert result == {}

    def test_skips_invalid_yaml(self, tmp_path):
        """Skips YAML files that are malformed or missing required fields."""
        specs_dir = tmp_path / "tasks"
        specs_dir.mkdir()

        # Valid spec
        valid = {"title": "Good task", "description": "Do something."}
        (specs_dir / "good.yaml").write_text(yaml.dump(valid))

        # Missing title
        bad = {"description": "No title here."}
        (specs_dir / "bad.yaml").write_text(yaml.dump(bad))

        result = load_task_specs_from_dir(specs_dir)
        assert len(result) == 1
        assert "Good task" in result

    def test_loads_real_benchmarks(self):
        """Loads the actual benchmark tasks from the repo."""
        specs_dir = Path(__file__).parent.parent / "benchmarks" / "tasks"
        if specs_dir.is_dir():
            result = load_task_specs_from_dir(specs_dir)
            assert len(result) > 0
            # Should include our known benchmark task
            assert any("off-by-one" in title.lower() or "pagination" in title.lower()
                       for title in result.keys())
