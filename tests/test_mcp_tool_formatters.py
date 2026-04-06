"""Tests for MCP tool activity summary formatters.

Verifies that _extract_tool_summary() produces human-readable (category, detail)
tuples for all 13 MCP tools, with correct truncation, optional params,
name capitalization, and graceful fallback on malformed input.
"""

import types

import pytest

from delegate.runtime import _extract_tool_summary, MCP_TOOL_FORMATTERS


def _block(name: str, **kwargs) -> object:
    """Build a minimal tool-call block with the given name and input fields."""
    b = types.SimpleNamespace()
    b.name = name
    b.input = kwargs
    return b


# ---------------------------------------------------------------------------
# Task management tools — category must be "task"
# ---------------------------------------------------------------------------


class TestTaskCreate:
    def test_basic_high_priority(self):
        tool, detail = _extract_tool_summary(
            _block("task_create", title="Fix the bug", priority="high")
        )
        assert tool == "task"
        assert detail == 'create: "Fix the bug" (high)'

    def test_medium_priority_omitted(self):
        # priority "medium" is the default and should NOT appear in detail
        _, detail = _extract_tool_summary(
            _block("task_create", title="Fix the bug", priority="medium")
        )
        assert detail == 'create: "Fix the bug"'
        assert "medium" not in detail

    def test_missing_priority_omitted(self):
        # missing priority treated same as medium — omit
        _, detail = _extract_tool_summary(
            _block("task_create", title="No priority")
        )
        assert "medium" not in detail
        assert "(" not in detail

    def test_title_truncated_to_40_chars(self):
        long_title = "A" * 50
        _, detail = _extract_tool_summary(
            _block("task_create", title=long_title, priority="low")
        )
        assert f'"{"A" * 40}"' in detail
        assert "A" * 41 not in detail

    def test_empty_title(self):
        _, detail = _extract_tool_summary(
            _block("task_create", title="", priority="low")
        )
        assert detail == 'create: "" (low)'

    def test_no_task_id_in_detail(self):
        # task_id is not in task_create input — should never appear in output
        _, detail = _extract_tool_summary(
            _block("task_create", title="Some task", priority="high")
        )
        assert "T0" not in detail


class TestTaskAssign:
    def test_basic(self):
        tool, detail = _extract_tool_summary(
            _block("task_assign", task_id=15, assignee="cubic")
        )
        assert tool == "task"
        assert detail == "assign T0015 to Cubic"

    def test_name_capitalized(self):
        _, detail = _extract_tool_summary(
            _block("task_assign", task_id=5, assignee="porter")
        )
        assert "Porter" in detail
        assert "porter" not in detail

    def test_task_id_zero_padded(self):
        _, detail = _extract_tool_summary(_block("task_assign", task_id=3, assignee="blend"))
        assert "T0003" in detail

    def test_missing_params_fallback(self):
        _, detail = _extract_tool_summary(_block("task_assign"))
        assert detail == "assign T0000 to ?"


class TestTaskStatus:
    def test_basic(self):
        tool, detail = _extract_tool_summary(
            _block("task_status", task_id=15, new_status="in_review")
        )
        assert tool == "task"
        assert detail == "T0015 -> in_review"

    def test_ascii_arrow_not_unicode(self):
        _, detail = _extract_tool_summary(
            _block("task_status", task_id=1, new_status="done")
        )
        assert "->" in detail
        assert "\u2192" not in detail

    def test_missing_params_fallback(self):
        _, detail = _extract_tool_summary(_block("task_status"))
        assert detail == "T0000 -> ?"


class TestTaskComment:
    def test_basic(self):
        tool, detail = _extract_tool_summary(
            _block("task_comment", task_id=22, body="Some long comment text")
        )
        assert tool == "task"
        assert detail == "comment on T0022"

    def test_body_not_included_in_detail(self):
        _, detail = _extract_tool_summary(
            _block("task_comment", task_id=5, body="Very long comment that should not appear")
        )
        assert "long comment" not in detail


class TestTaskShow:
    def test_basic(self):
        tool, detail = _extract_tool_summary(_block("task_show", task_id=7))
        assert tool == "task"
        assert detail == "show T0007"


class TestTaskList:
    def test_basic(self):
        tool, detail = _extract_tool_summary(
            _block("task_list", status="in_progress", assignee="blend")
        )
        assert tool == "task"
        assert detail == "list tasks"

    def test_no_params(self):
        _, detail = _extract_tool_summary(_block("task_list"))
        assert detail == "list tasks"

    def test_filter_params_not_shown(self):
        _, detail = _extract_tool_summary(
            _block("task_list", status="done", assignee="cubic")
        )
        assert "done" not in detail
        assert "cubic" not in detail


class TestTaskCancel:
    def test_basic(self):
        tool, detail = _extract_tool_summary(_block("task_cancel", task_id=19))
        assert tool == "task"
        assert detail == "cancel T0019"


class TestTaskAttach:
    def test_basic(self):
        tool, detail = _extract_tool_summary(
            _block("task_attach", task_id=15, file_path="/some/deep/path/spec.md")
        )
        assert tool == "task"
        assert detail == "attach spec.md to T0015"

    def test_basename_extracted(self):
        _, detail = _extract_tool_summary(
            _block("task_attach", task_id=1, file_path="/Users/nikhil/teams/shared/design.png")
        )
        assert "design.png" in detail
        assert "/Users" not in detail

    def test_missing_file_path(self):
        _, detail = _extract_tool_summary(_block("task_attach", task_id=1))
        assert "?" in detail


class TestTaskDetach:
    def test_basic(self):
        tool, detail = _extract_tool_summary(
            _block("task_detach", task_id=15, file_path="/some/path/old-spec.md")
        )
        assert tool == "task"
        assert detail == "detach old-spec.md from T0015"

    def test_basename_extracted(self):
        _, detail = _extract_tool_summary(
            _block("task_detach", task_id=3, file_path="/deep/nested/file.txt")
        )
        assert "file.txt" in detail
        assert "/deep" not in detail


# ---------------------------------------------------------------------------
# Communication tools — category must be "message"
# ---------------------------------------------------------------------------


class TestMailboxSend:
    def test_basic_with_short_message(self):
        tool, detail = _extract_tool_summary(
            _block("mailbox_send", recipient="cubic", message="Hello there", task_id=15)
        )
        assert tool == "message"
        assert detail == 'send to Cubic: "Hello there"'

    def test_name_capitalized(self):
        tool, detail = _extract_tool_summary(
            _block("mailbox_send", recipient="porter", message="Hi")
        )
        assert tool == "message"
        assert "Porter" in detail
        assert "porter" not in detail

    def test_message_truncated_to_40_chars(self):
        long_msg = "A" * 50
        _, detail = _extract_tool_summary(
            _block("mailbox_send", recipient="blend", message=long_msg)
        )
        assert '"' + "A" * 40 + '"...' in detail
        assert "A" * 41 not in detail.replace("...", "")

    def test_short_message_no_ellipsis(self):
        _, detail = _extract_tool_summary(
            _block("mailbox_send", recipient="blend", message="Short msg")
        )
        assert "..." not in detail

    def test_missing_recipient(self):
        _, detail = _extract_tool_summary(_block("mailbox_send", message="Hi"))
        assert 'send to ?: "Hi"' == detail

    def test_missing_message(self):
        _, detail = _extract_tool_summary(_block("mailbox_send", recipient="blend"))
        assert detail == 'send to Blend: ""'


class TestMailboxInbox:
    def test_basic(self):
        tool, detail = _extract_tool_summary(_block("mailbox_inbox"))
        assert tool == "message"
        assert detail == "check inbox"

    def test_no_params_needed(self):
        _, detail = _extract_tool_summary(_block("mailbox_inbox"))
        assert detail == "check inbox"


# ---------------------------------------------------------------------------
# Repository tools — category must be "repo"
# ---------------------------------------------------------------------------


class TestRepoList:
    def test_basic(self):
        tool, detail = _extract_tool_summary(_block("repo_list"))
        assert tool == "repo"
        assert detail == "list repos"


# ---------------------------------------------------------------------------
# Git tools — category must be "git"
# ---------------------------------------------------------------------------


class TestRebaseToMain:
    def test_basic(self):
        tool, detail = _extract_tool_summary(_block("rebase_to_main", task_id=16))
        assert tool == "git"
        assert detail == "rebase T0016 to main"

    def test_missing_task_id(self):
        _, detail = _extract_tool_summary(_block("rebase_to_main"))
        assert detail == "rebase T0000 to main"


# ---------------------------------------------------------------------------
# Formatter dict completeness
# ---------------------------------------------------------------------------


class TestFormatterCompleteness:
    EXPECTED_TOOLS = {
        # Task management (9)
        "task_create", "task_assign", "task_status", "task_comment",
        "task_show", "task_list", "task_cancel", "task_attach", "task_detach",
        # Communication (2)
        "mailbox_send", "mailbox_inbox",
        # Repository (2)
        "repo_list", "rebase_to_main",
        # Review (3)
        "task_diff", "task_approve", "task_reject",
    }

    def test_all_tools_covered(self):
        assert set(MCP_TOOL_FORMATTERS.keys()) == self.EXPECTED_TOOLS

    def test_count(self):
        assert len(MCP_TOOL_FORMATTERS) == 16


# ---------------------------------------------------------------------------
# Error fallback
# ---------------------------------------------------------------------------


class TestFormatterFallback:
    def test_malformed_input_falls_back_gracefully(self, monkeypatch):
        """If a formatter raises, _extract_tool_summary falls back to key list."""

        def bad_formatter(inp):
            raise ValueError("formatter exploded")

        monkeypatch.setitem(MCP_TOOL_FORMATTERS, "task_create", bad_formatter)

        tool, detail = _extract_tool_summary(
            _block("task_create", title="Hello", priority="high")
        )
        # Fallback returns raw tool name as category, key list as detail
        assert tool == "task_create"
        assert "task_create" in detail or "priority" in detail or "title" in detail

    def test_unknown_tool_uses_legacy_fallback(self):
        """Tools not in MCP_TOOL_FORMATTERS still get key-list formatting."""
        tool, detail = _extract_tool_summary(
            _block("some_future_tool", foo="bar", baz="qux")
        )
        assert tool == "some_future_tool"
        assert "some_future_tool" in detail

    def test_non_tool_block_returns_empty(self):
        """Blocks without a .name attribute return empty strings."""
        block = types.SimpleNamespace()  # no .name
        assert _extract_tool_summary(block) == ("", "")

    def test_built_in_tools_unchanged(self):
        """Existing built-in formatters are not affected by MCP changes."""
        tool, detail = _extract_tool_summary(
            _block("Bash", command="echo hello world")
        )
        assert tool == "Bash"
        assert detail == "echo hello world"

        tool, detail = _extract_tool_summary(
            _block("Read", file_path="/some/file.py")
        )
        assert tool == "Read"
        assert detail == "/some/file.py"

        tool, detail = _extract_tool_summary(
            _block("Grep", pattern="def foo")
        )
        assert tool == "Grep"
        assert detail == "def foo"


# ---------------------------------------------------------------------------
# MCP-prefixed tool names (the real names Claude's API sends)
# ---------------------------------------------------------------------------


class TestMCPPrefixStripping:
    """Verify that mcp__delegate__<tool> is resolved to the bare <tool> key
    before the MCP_TOOL_FORMATTERS lookup, so real API names work correctly.
    """

    def test_task_comment_with_prefix(self):
        """mcp__delegate__task_comment maps to the task_comment formatter."""
        tool, detail = _extract_tool_summary(
            _block("mcp__delegate__task_comment", task_id=5, body="some text")
        )
        assert tool == "task"
        assert detail == "comment on T0005"

    def test_mailbox_send_with_prefix(self):
        """mcp__delegate__mailbox_send maps to the mailbox_send formatter."""
        tool, detail = _extract_tool_summary(
            _block("mcp__delegate__mailbox_send", recipient="nikhil", message="Hello")
        )
        assert tool == "message"
        assert detail == 'send to Nikhil: "Hello"'

    def test_task_create_with_prefix(self):
        """mcp__delegate__task_create maps to the task_create formatter."""
        tool, detail = _extract_tool_summary(
            _block("mcp__delegate__task_create", title="New feature", priority="high")
        )
        assert tool == "task"
        assert detail == 'create: "New feature" (high)'

    def test_task_status_with_prefix(self):
        """mcp__delegate__task_status maps to the task_status formatter."""
        tool, detail = _extract_tool_summary(
            _block("mcp__delegate__task_status", task_id=12, new_status="in_review")
        )
        assert tool == "task"
        assert detail == "T0012 -> in_review"

    def test_unknown_mcp_tool_falls_back_to_short_name(self):
        """Unknown MCP tools fall back using the short name (not the full prefix)."""
        tool, detail = _extract_tool_summary(
            _block("mcp__delegate__some_future_tool", foo="bar")
        )
        # short_name is "some_future_tool" -- should NOT contain the mcp__ prefix
        assert "mcp__" not in tool
        assert "mcp__" not in detail
        assert "some_future_tool" in tool or "some_future_tool" in detail

    def test_bare_names_still_work(self):
        """Bare names (no mcp__ prefix) continue to work as before."""
        tool, detail = _extract_tool_summary(
            _block("task_comment", task_id=22, body="text")
        )
        assert tool == "task"
        assert detail == "comment on T0022"
