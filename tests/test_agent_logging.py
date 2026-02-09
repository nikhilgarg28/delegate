"""Tests for AgentLogger and rich structured logging in agent sessions.

Verifies that:
- AgentLogger produces correctly structured log lines
- Session lifecycle events are logged (start, end, errors)
- Per-turn logging includes tokens, cost, and tool calls
- Message routing events are logged
- Log levels are appropriate (DEBUG for detailed, INFO for key events)
"""

import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from boss.agent import AgentLogger, _extract_tool_calls


# ---------------------------------------------------------------------------
# AgentLogger unit tests
# ---------------------------------------------------------------------------


class TestAgentLoggerPrefix:
    """Verify the structured [agent:X] [turn:N] prefix format."""

    def test_prefix_includes_agent_name(self):
        alog = AgentLogger("alice")
        assert alog._prefix() == "[agent:alice] [turn:0]"

    def test_prefix_updates_with_turn(self):
        alog = AgentLogger("bob")
        alog.turn = 3
        assert alog._prefix() == "[agent:bob] [turn:3]"

    def test_prefix_after_turn_start(self):
        alog = AgentLogger("carol")
        alog.turn_start(5, "test message")
        assert alog._prefix() == "[agent:carol] [turn:5]"


class TestAgentLoggerLogMethods:
    """Verify that log methods emit correctly formatted messages."""

    @pytest.fixture
    def alog_with_records(self):
        """Create an AgentLogger with a handler that captures log records."""
        alog = AgentLogger("alice")
        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = RecordHandler()
        handler.setLevel(logging.DEBUG)
        alog._logger.addHandler(handler)
        alog._logger.setLevel(logging.DEBUG)
        return alog, records

    def test_info_logs_at_info_level(self, alog_with_records):
        alog, records = alog_with_records
        alog.info("test message %s", "arg1")
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert "[agent:alice]" in records[0].getMessage()
        assert "test message arg1" in records[0].getMessage()

    def test_debug_logs_at_debug_level(self, alog_with_records):
        alog, records = alog_with_records
        alog.debug("debug detail %d", 42)
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG

    def test_warning_logs_at_warning_level(self, alog_with_records):
        alog, records = alog_with_records
        alog.warning("something concerning")
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_error_logs_at_error_level(self, alog_with_records):
        alog, records = alog_with_records
        alog.error("something broke: %s", "details")
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR


class TestSessionLifecycleLogging:
    """Verify session start and end logging."""

    @pytest.fixture
    def alog_with_records(self):
        alog = AgentLogger("alice")
        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = RecordHandler()
        handler.setLevel(logging.DEBUG)
        alog._logger.addHandler(handler)
        alog._logger.setLevel(logging.DEBUG)
        return alog, records

    def test_session_start_log_basic(self, alog_with_records):
        alog, records = alog_with_records
        alog.session_start_log(task_id=None, session_id=1)
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "Session started" in msg
        assert "session_id=1" in msg

    def test_session_start_log_with_task(self, alog_with_records):
        alog, records = alog_with_records
        alog.session_start_log(
            task_id=42, token_budget=100000, session_id=5, max_turns=25,
        )
        msg = records[0].getMessage()
        assert "task=T0042" in msg
        assert "token_budget=100,000" in msg
        assert "max_turns=25" in msg

    def test_session_start_log_with_workspace(self, alog_with_records):
        alog, records = alog_with_records
        alog.session_start_log(
            task_id=1, workspace=Path("/tmp/worktree"), session_id=1,
        )
        msg = records[0].getMessage()
        assert "/tmp/worktree" in msg

    def test_session_end_log(self, alog_with_records):
        alog, records = alog_with_records
        alog.session_end_log(
            turns=3, tokens_in=5000, tokens_out=2000,
            cost_usd=0.1234, exit_reason="idle_timeout",
        )
        msg = records[0].getMessage()
        assert "Session ended" in msg
        assert "reason=idle_timeout" in msg
        assert "turns=3" in msg
        assert "cost=$0.1234" in msg
        # Should have total tokens
        assert "7,000" in msg  # 5000 + 2000

    def test_session_end_log_duration(self, alog_with_records):
        alog, records = alog_with_records
        # Artificially set session_start to check duration is computed
        alog.session_start = time.monotonic() - 120.0  # 2 minutes ago
        alog.session_end_log(
            turns=1, tokens_in=100, tokens_out=50, cost_usd=0.01,
        )
        msg = records[0].getMessage()
        assert "duration=" in msg
        # Should be roughly 120 seconds (within tolerance)
        assert "120" in msg or "119" in msg or "121" in msg


class TestTurnLogging:
    """Verify per-turn logging."""

    @pytest.fixture
    def alog_with_records(self):
        alog = AgentLogger("alice")
        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = RecordHandler()
        handler.setLevel(logging.DEBUG)
        alog._logger.addHandler(handler)
        alog._logger.setLevel(logging.DEBUG)
        return alog, records

    def test_turn_start_updates_turn_number(self, alog_with_records):
        alog, records = alog_with_records
        alog.turn_start(3, "Hello world")
        assert alog.turn == 3
        msg = records[0].getMessage()
        assert "Turn 3 started" in msg
        assert "input_preview=" in msg

    def test_turn_start_truncates_long_messages(self, alog_with_records):
        alog, records = alog_with_records
        long_msg = "x" * 200
        alog.turn_start(1, long_msg)
        msg = records[0].getMessage()
        # Should contain "..." for truncation
        assert "..." in msg

    def test_turn_end_logs_tokens_and_cost(self, alog_with_records):
        alog, records = alog_with_records
        alog.turn_end(
            2,
            tokens_in=1000, tokens_out=500, cost_usd=0.05,
            cumulative_tokens_in=3000, cumulative_tokens_out=1500,
            cumulative_cost=0.15,
        )
        msg = records[0].getMessage()
        assert "Turn 2 complete" in msg
        assert "turn_tokens=1,500" in msg  # 1000 + 500
        assert "cumulative_tokens=4,500" in msg  # 3000 + 1500
        assert "turn_cost=$0.0500" in msg
        assert "cumulative_cost=$0.1500" in msg

    def test_turn_end_logs_tool_calls(self, alog_with_records):
        alog, records = alog_with_records
        alog.turn_end(
            1,
            tokens_in=100, tokens_out=50, cost_usd=0.01,
            cumulative_tokens_in=100, cumulative_tokens_out=50,
            cumulative_cost=0.01,
            tool_calls=["Bash", "Read", "Write"],
        )
        msg = records[0].getMessage()
        assert "tools=[Bash, Read, Write]" in msg

    def test_turn_end_no_tools_omits_field(self, alog_with_records):
        alog, records = alog_with_records
        alog.turn_end(
            1,
            tokens_in=100, tokens_out=50, cost_usd=0.01,
            cumulative_tokens_in=100, cumulative_tokens_out=50,
            cumulative_cost=0.01,
            tool_calls=None,
        )
        msg = records[0].getMessage()
        assert "tools=" not in msg


class TestMessageLogging:
    """Verify message routing log events."""

    @pytest.fixture
    def alog_with_records(self):
        alog = AgentLogger("alice")
        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = RecordHandler()
        handler.setLevel(logging.DEBUG)
        alog._logger.addHandler(handler)
        alog._logger.setLevel(logging.DEBUG)
        return alog, records

    def test_message_received_log(self, alog_with_records):
        alog, records = alog_with_records
        alog.message_received("boss", 150)
        msg = records[0].getMessage()
        assert "Message received" in msg
        assert "from=boss" in msg
        assert "length=150 chars" in msg

    def test_message_sent_log(self, alog_with_records):
        alog, records = alog_with_records
        alog.message_sent("manager", 200)
        msg = records[0].getMessage()
        assert "Message sent" in msg
        assert "to=manager" in msg
        assert "length=200 chars" in msg

    def test_mail_marked_read_is_debug(self, alog_with_records):
        alog, records = alog_with_records
        alog.mail_marked_read("msg_001.yaml")
        assert records[0].levelno == logging.DEBUG
        assert "msg_001.yaml" in records[0].getMessage()


class TestToolCallLogging:
    """Verify tool call logging."""

    @pytest.fixture
    def alog_with_records(self):
        alog = AgentLogger("alice")
        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = RecordHandler()
        handler.setLevel(logging.DEBUG)
        alog._logger.addHandler(handler)
        alog._logger.setLevel(logging.DEBUG)
        return alog, records

    def test_tool_call_basic(self, alog_with_records):
        alog, records = alog_with_records
        alog.tool_call("Bash")
        msg = records[0].getMessage()
        assert "Tool call" in msg
        assert "tool=Bash" in msg
        assert records[0].levelno == logging.DEBUG

    def test_tool_call_with_args(self, alog_with_records):
        alog, records = alog_with_records
        alog.tool_call("Write", "/path/to/file.py")
        msg = records[0].getMessage()
        assert "args=/path/to/file.py" in msg

    def test_tool_call_truncates_long_args(self, alog_with_records):
        alog, records = alog_with_records
        long_args = "x" * 200
        alog.tool_call("Edit", long_args)
        msg = records[0].getMessage()
        assert "..." in msg


class TestErrorLogging:
    """Verify error and exception logging."""

    @pytest.fixture
    def alog_with_records(self):
        alog = AgentLogger("alice")
        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = RecordHandler()
        handler.setLevel(logging.DEBUG)
        alog._logger.addHandler(handler)
        alog._logger.setLevel(logging.DEBUG)
        return alog, records

    def test_session_error_logs_type_and_message(self, alog_with_records):
        alog, records = alog_with_records
        try:
            raise ValueError("something went wrong")
        except ValueError as exc:
            alog.session_error(exc)
        msg = records[0].getMessage()
        assert "Session error" in msg
        assert "type=ValueError" in msg
        assert "something went wrong" in msg
        assert records[0].levelno == logging.ERROR
        # Should include traceback info
        assert records[0].exc_info is not None


class TestConnectionLogging:
    """Verify client connection event logging."""

    @pytest.fixture
    def alog_with_records(self):
        alog = AgentLogger("alice")
        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = RecordHandler()
        handler.setLevel(logging.DEBUG)
        alog._logger.addHandler(handler)
        alog._logger.setLevel(logging.DEBUG)
        return alog, records

    def test_client_connecting(self, alog_with_records):
        alog, records = alog_with_records
        alog.client_connecting()
        assert "Connecting" in records[0].getMessage()
        assert records[0].levelno == logging.INFO

    def test_client_connected(self, alog_with_records):
        alog, records = alog_with_records
        alog.client_connected()
        assert "connected" in records[0].getMessage()
        assert records[0].levelno == logging.INFO

    def test_client_disconnected(self, alog_with_records):
        alog, records = alog_with_records
        alog.client_disconnected()
        assert "disconnected" in records[0].getMessage()


class TestIdleLogging:
    """Verify idle/waiting event logging."""

    @pytest.fixture
    def alog_with_records(self):
        alog = AgentLogger("alice")
        records = []

        class RecordHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = RecordHandler()
        handler.setLevel(logging.DEBUG)
        alog._logger.addHandler(handler)
        alog._logger.setLevel(logging.DEBUG)
        return alog, records

    def test_waiting_for_mail_is_debug(self, alog_with_records):
        alog, records = alog_with_records
        alog.waiting_for_mail(600)
        assert records[0].levelno == logging.DEBUG
        assert "600" in records[0].getMessage()

    def test_idle_timeout_is_info(self, alog_with_records):
        alog, records = alog_with_records
        alog.idle_timeout(600)
        assert records[0].levelno == logging.INFO
        assert "600" in records[0].getMessage()
        assert "shutting down" in records[0].getMessage()


# ---------------------------------------------------------------------------
# _extract_tool_calls helper tests
# ---------------------------------------------------------------------------

class TestExtractToolCalls:
    """Verify tool call extraction from response messages."""

    def test_extracts_tool_names(self):
        class Block:
            def __init__(self, name):
                self.name = name

        class Msg:
            def __init__(self, blocks):
                self.content = blocks

        msg = Msg([Block("Bash"), Block("Read")])
        assert _extract_tool_calls(msg) == ["Bash", "Read"]

    def test_ignores_text_blocks(self):
        class TextBlock:
            def __init__(self, text):
                self.text = text

        class Msg:
            def __init__(self, blocks):
                self.content = blocks

        msg = Msg([TextBlock("Hello")])
        assert _extract_tool_calls(msg) == []

    def test_handles_no_content(self):
        class Msg:
            pass

        assert _extract_tool_calls(Msg()) == []

    def test_mixed_blocks(self):
        class ToolBlock:
            def __init__(self, name):
                self.name = name

        class TextBlock:
            def __init__(self, text):
                self.text = text

        class Msg:
            def __init__(self, blocks):
                self.content = blocks

        msg = Msg([TextBlock("thinking..."), ToolBlock("Write"), TextBlock("done")])
        assert _extract_tool_calls(msg) == ["Write"]


# ---------------------------------------------------------------------------
# AgentLogger custom logger injection
# ---------------------------------------------------------------------------


class TestAgentLoggerCustomLogger:
    """Verify custom logger injection."""

    def test_uses_custom_logger(self):
        custom_logger = logging.getLogger("custom.test")
        alog = AgentLogger("alice", base_logger=custom_logger)
        assert alog._logger is custom_logger

    def test_default_logger_name(self):
        alog = AgentLogger("bob")
        assert alog._logger.name == "boss.agent.bob"
