"""Tests for the POST /teams/{team}/tasks/{id}/reject notification in delegate/web.py.

These tests focus on the structured TASK_REJECTED notification delivered
to the manager's inbox when a task is rejected.  The endpoint itself
(status transitions, approval_status, log_event, etc.) is also tested
in test_approval_api.py.
"""

import pytest
from fastapi.testclient import TestClient

from delegate.web import create_app
from delegate.task import create_task, change_status, assign_task, get_task, format_task_id
from delegate.mailbox import read_inbox

TEAM = "testteam"


@pytest.fixture
def client(tmp_team):
    """Create a FastAPI test client using a bootstrapped team directory."""
    app = create_app(hc_home=tmp_team)
    return TestClient(app)


def _task_to_needs_merge(root):
    """Create a task and advance it to needs_merge status. Returns task dict."""
    task = create_task(root, TEAM, title="Feature X")
    assign_task(root, TEAM, task["id"], "alice")
    change_status(root, TEAM, task["id"], "in_progress")
    change_status(root, TEAM, task["id"], "review")
    change_status(root, TEAM, task["id"], "needs_merge")
    return get_task(root, TEAM, task["id"])


class TestRejectNotification:
    def test_reject_delivers_notification_to_manager_inbox(self, tmp_team, client):
        task = _task_to_needs_merge(tmp_team)
        resp = client.post(
            f"/teams/{TEAM}/tasks/{task['id']}/reject",
            json={"reason": "Code quality issues"},
        )
        assert resp.status_code == 200

        # Notification should be in manager's inbox (direct delivery)
        inbox = read_inbox(tmp_team, TEAM, "manager", unread_only=True)
        assert len(inbox) >= 1

        msg = inbox[0]
        assert msg.recipient == "manager"

    def test_notification_contains_task_details(self, tmp_team, client):
        task = _task_to_needs_merge(tmp_team)
        client.post(
            f"/teams/{TEAM}/tasks/{task['id']}/reject",
            json={"reason": "Code quality issues"},
        )
        inbox = read_inbox(tmp_team, TEAM, "manager", unread_only=True)
        body = inbox[0].body

        assert "TASK_REJECTED" in body
        assert f"{format_task_id(task['id'])}" in body
        assert "Feature X" in body
        assert "alice" in body
        assert "Code quality issues" in body

    def test_notification_has_suggested_actions(self, tmp_team, client):
        task = _task_to_needs_merge(tmp_team)
        client.post(
            f"/teams/{TEAM}/tasks/{task['id']}/reject",
            json={"reason": "Problems found"},
        )
        inbox = read_inbox(tmp_team, TEAM, "manager", unread_only=True)
        body = inbox[0].body

        assert "Rework" in body
        assert "Reassign" in body
        assert "Discard" in body

    def test_notification_sender_is_boss(self, tmp_team, client):
        task = _task_to_needs_merge(tmp_team)
        client.post(
            f"/teams/{TEAM}/tasks/{task['id']}/reject",
            json={"reason": "Not ready"},
        )
        inbox = read_inbox(tmp_team, TEAM, "manager", unread_only=True)
        msg = inbox[0]
        # Boss name is "nikhil" as set in conftest
        assert msg.sender == "nikhil"

    def test_reject_sets_approval_status(self, tmp_team, client):
        task = _task_to_needs_merge(tmp_team)
        resp = client.post(
            f"/teams/{TEAM}/tasks/{task['id']}/reject",
            json={"reason": "Needs work"},
        )
        data = resp.json()
        assert data["status"] == "rejected"
        assert data["approval_status"] == "rejected"
        assert data["rejection_reason"] == "Needs work"

    def test_reject_returns_full_task_dict(self, tmp_team, client):
        task = _task_to_needs_merge(tmp_team)
        resp = client.post(
            f"/teams/{TEAM}/tasks/{task['id']}/reject",
            json={"reason": "Issues found"},
        )
        data = resp.json()
        assert data["id"] == task["id"]
        assert data["title"] == "Feature X"
        assert "created_at" in data
