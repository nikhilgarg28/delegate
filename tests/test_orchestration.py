"""Tests for the unified runtime dispatch model (agents_with_unread + run_turn).

Replaces the old orchestrator tests — there is no longer any PID tracking,
subprocess spawning, or stale-PID clearing.  The daemon polls
``agents_with_unread()`` and dispatches ``run_turn()`` directly.
"""

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from delegate.mailbox import deliver, Message, agents_with_unread, mark_processed, read_inbox
from delegate.runtime import list_ai_agents, run_turn

TEAM = "testteam"


def _deliver_msg(tmp_team, to_agent, body="Hello", sender="manager"):
    deliver(tmp_team, TEAM, Message(
        sender=sender,
        recipient=to_agent,
        time="2026-02-08T12:00:00Z",
        body=body,
    ))


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
        mark_processed(tmp_team, TEAM, inbox[0].filename)
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
# run_turn — with mock SDK
# ---------------------------------------------------------------------------


@dataclass
class _FakeResultMsg:
    """Mimics a claude_code_sdk ResultMessage."""
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


@dataclass
class _FakeAssistantMsg:
    """Mimics a claude_code_sdk AssistantMessage."""
    content: list | None = None


class _FakeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


async def _mock_query(prompt: str, options=None):
    """Mock SDK query that yields a result message."""
    yield _FakeResultMsg()


class TestRunTurn:
    @patch("delegate.runtime.random.random", return_value=1.0)  # no reflection
    def test_processes_message_and_marks_processed(self, _mock_rng, tmp_team):
        """run_turn should process the oldest unread message and mark it."""
        _deliver_msg(tmp_team, "alice", body="Please do task 1")

        result = asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=_mock_query,
                sdk_options_class=_FakeOptions,
            )
        )

        assert result.agent == "alice"
        assert result.team == TEAM
        assert result.error is None
        assert result.tokens_in == 100
        assert result.tokens_out == 50
        assert result.cost_usd == 0.01
        assert result.turns == 1

        # Message should be marked as processed
        remaining = agents_with_unread(tmp_team, TEAM)
        assert "alice" not in remaining

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_no_messages_returns_early(self, _mock_rng, tmp_team):
        """run_turn with no unread messages should return early with no turns."""
        result = asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=_mock_query,
                sdk_options_class=_FakeOptions,
            )
        )

        assert result.error is None
        assert result.turns == 0  # no messages → early return

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_sdk_error_captured(self, _mock_rng, tmp_team):
        """If the SDK query fails, the error is captured in TurnResult."""
        _deliver_msg(tmp_team, "alice")

        async def failing_query(prompt: str, options=None):
            raise RuntimeError("SDK connection failed")
            yield  # make it an async generator  # noqa: E501

        result = asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=failing_query,
                sdk_options_class=_FakeOptions,
            )
        )

        assert result.error is not None
        assert "SDK connection failed" in result.error

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_worklog_written(self, _mock_rng, tmp_team):
        """run_turn should write a worklog file."""
        _deliver_msg(tmp_team, "alice")

        asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=_mock_query,
                sdk_options_class=_FakeOptions,
            )
        )

        from delegate.paths import agent_dir
        logs_dir = agent_dir(tmp_team, TEAM, "alice") / "logs"
        worklogs = list(logs_dir.glob("*.worklog.md"))
        assert len(worklogs) >= 1

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_session_created_in_db(self, _mock_rng, tmp_team):
        """run_turn should create a session in the database."""
        _deliver_msg(tmp_team, "alice")

        result = asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=_mock_query,
                sdk_options_class=_FakeOptions,
            )
        )

        assert result.session_id > 0

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_context_md_saved(self, _mock_rng, tmp_team):
        """run_turn should save context.md for the next session."""
        _deliver_msg(tmp_team, "alice")

        asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=_mock_query,
                sdk_options_class=_FakeOptions,
            )
        )

        from delegate.paths import agent_dir
        context = agent_dir(tmp_team, TEAM, "alice") / "context.md"
        assert context.exists()
        text = context.read_text()
        assert "Last session:" in text

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_cache_tokens_tracked(self, _mock_rng, tmp_team):
        """run_turn should track cache_read and cache_write tokens."""
        _deliver_msg(tmp_team, "alice", body="Work on this")

        result = asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=_mock_query,
                sdk_options_class=_FakeOptions,
            )
        )

        assert result.cache_read == 20
        assert result.cache_write == 10

    @patch("delegate.runtime.random.random", return_value=0.0)  # always reflect
    def test_reflection_turn_runs_when_due(self, _mock_rng, tmp_team):
        """When reflection coin-flip lands, a second turn runs without marking mail."""
        _deliver_msg(tmp_team, "alice", body="Work on this")

        result = asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=_mock_query,
                sdk_options_class=_FakeOptions,
            )
        )

        assert result.turns == 2
        # Tokens should be doubled (100 in + 100 in for reflection)
        assert result.tokens_in == 200
        assert result.tokens_out == 100
        # Cache tokens should also be doubled
        assert result.cache_read == 40
        assert result.cache_write == 20

    @patch("delegate.runtime.random.random", return_value=1.0)
    def test_batch_same_task_id(self, _mock_rng, tmp_team):
        """Messages with the same task_id should be batched together."""
        # Deliver 3 messages with task_id=None (no --task flag)
        _deliver_msg(tmp_team, "alice", body="Hello 1")
        _deliver_msg(tmp_team, "alice", body="Hello 2")
        _deliver_msg(tmp_team, "alice", body="Hello 3")

        result = asyncio.run(
            run_turn(
                tmp_team, TEAM, "alice",
                sdk_query=_mock_query,
                sdk_options_class=_FakeOptions,
            )
        )

        assert result.error is None
        assert result.turns == 1

        # All 3 messages should be processed (same task_id=None)
        remaining = agents_with_unread(tmp_team, TEAM)
        assert "alice" not in remaining
