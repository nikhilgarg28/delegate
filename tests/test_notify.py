"""Tests for delegate/notify.py — rejection and conflict notifications."""

from pathlib import Path

import pytest

from delegate.bootstrap import bootstrap
from delegate.config import set_boss
from delegate.mailbox import read_inbox
from delegate.notify import notify_rejection, notify_conflict
from delegate.task import create_task, assign_task, change_status, get_task, format_task_id

TEAM = "notifyteam"


@pytest.fixture
def notify_team(tmp_path):
    """Bootstrap a team with a manager and workers for notification tests."""
    hc_home = tmp_path / "hc_home"
    set_boss(hc_home, "boss")
    bootstrap(hc_home, team_name=TEAM, manager="edison", agents=["alice", "bob"])
    return hc_home


def _make_task_at_needs_merge(hc_home):
    """Create a task and advance it to needs_merge status.

    Returns the current task dict (reloaded after all transitions).
    """
    task = create_task(hc_home, TEAM, title="Build login feature")
    assign_task(hc_home, TEAM, task["id"], "alice")
    change_status(hc_home, TEAM, task["id"], "in_progress")
    change_status(hc_home, TEAM, task["id"], "review")
    change_status(hc_home, TEAM, task["id"], "needs_merge")
    return get_task(hc_home, TEAM, task["id"])


class TestNotifyRejection:
    def test_sends_message_to_manager(self, notify_team):
        task = _make_task_at_needs_merge(notify_team)
        change_status(notify_team, TEAM, task["id"], "rejected")

        result = notify_rejection(
            notify_team, TEAM, task, reason="Code quality issues"
        )
        assert result is not None  # filename returned

        # Check manager's inbox
        inbox = read_inbox(notify_team, TEAM, "edison", unread_only=True)
        assert len(inbox) == 1
        msg = inbox[0]
        assert msg.recipient == "edison"
        assert msg.sender == "boss"

    def test_message_contains_task_details(self, notify_team):
        task = _make_task_at_needs_merge(notify_team)
        change_status(notify_team, TEAM, task["id"], "rejected")

        notify_rejection(
            notify_team, TEAM, task, reason="Missing error handling"
        )

        inbox = read_inbox(notify_team, TEAM, "edison", unread_only=True)
        body = inbox[0].body

        assert "TASK_REJECTED" in body
        assert f"{format_task_id(task['id'])}" in body
        assert "Build login feature" in body
        assert "alice" in body
        assert "Missing error handling" in body

    def test_message_contains_suggested_actions(self, notify_team):
        task = _make_task_at_needs_merge(notify_team)
        change_status(notify_team, TEAM, task["id"], "rejected")

        notify_rejection(notify_team, TEAM, task, reason="Needs work")

        inbox = read_inbox(notify_team, TEAM, "edison", unread_only=True)
        body = inbox[0].body

        assert "Rework" in body
        assert "Reassign" in body
        assert "Discard" in body

    def test_no_reason_shows_placeholder(self, notify_team):
        task = _make_task_at_needs_merge(notify_team)
        change_status(notify_team, TEAM, task["id"], "rejected")

        notify_rejection(notify_team, TEAM, task, reason="")

        inbox = read_inbox(notify_team, TEAM, "edison", unread_only=True)
        body = inbox[0].body
        assert "(no reason provided)" in body

    def test_returns_id_string(self, notify_team):
        """notify_rejection returns the message id as a string."""
        task = _make_task_at_needs_merge(notify_team)
        change_status(notify_team, TEAM, task["id"], "rejected")

        result = notify_rejection(notify_team, TEAM, task, reason="Test")
        assert result is not None
        assert result.isdigit()


class TestNotifyConflict:
    def test_sends_message_to_manager(self, notify_team):
        task = _make_task_at_needs_merge(notify_team)
        task["branch"] = "alice/T0001"
        change_status(notify_team, TEAM, task["id"], "conflict")

        result = notify_conflict(
            notify_team, TEAM, task, conflict_details="Conflict in auth.py"
        )
        assert result is not None

        inbox = read_inbox(notify_team, TEAM, "edison", unread_only=True)
        assert len(inbox) == 1
        msg = inbox[0]
        assert msg.recipient == "edison"
        assert msg.sender == "boss"

    def test_message_contains_task_and_branch(self, notify_team):
        task = _make_task_at_needs_merge(notify_team)
        task["branch"] = "alice/T0001"
        change_status(notify_team, TEAM, task["id"], "conflict")

        notify_conflict(
            notify_team, TEAM, task,
            conflict_details="Conflict in auth.py: both branches modify line 42"
        )

        inbox = read_inbox(notify_team, TEAM, "edison", unread_only=True)
        body = inbox[0].body

        assert "MERGE_CONFLICT" in body
        assert f"{format_task_id(task['id'])}" in body
        assert "Build login feature" in body
        assert "alice/T0001" in body
        assert "Conflict in auth.py" in body

    def test_message_suggests_rebase(self, notify_team):
        task = _make_task_at_needs_merge(notify_team)
        task["branch"] = "alice/T0001"
        change_status(notify_team, TEAM, task["id"], "conflict")

        notify_conflict(notify_team, TEAM, task)

        inbox = read_inbox(notify_team, TEAM, "edison", unread_only=True)
        body = inbox[0].body

        assert "rebase" in body.lower()
        assert "alice" in body

    def test_no_details_shows_placeholder(self, notify_team):
        task = _make_task_at_needs_merge(notify_team)
        task["branch"] = "alice/T0001"
        change_status(notify_team, TEAM, task["id"], "conflict")

        notify_conflict(notify_team, TEAM, task, conflict_details="")

        inbox = read_inbox(notify_team, TEAM, "edison", unread_only=True)
        body = inbox[0].body
        assert "(no details available)" in body

    def test_returns_id_string(self, notify_team):
        """notify_conflict returns the message id as a string."""
        task = _make_task_at_needs_merge(notify_team)
        change_status(notify_team, TEAM, task["id"], "conflict")

        result = notify_conflict(notify_team, TEAM, task, conflict_details="Test")
        assert result is not None
        assert result.isdigit()
