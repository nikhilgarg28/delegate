"""Tests for the POST /tasks/{id}/approve and POST /tasks/{id}/reject API endpoints."""

import pytest
from fastapi.testclient import TestClient

from delegate.task import create_task, change_status, get_task, update_task, format_task_id
from delegate.review import get_current_review
from delegate.web import create_app
from delegate.mailbox import read_inbox

TEAM = "testteam"


@pytest.fixture
def client(tmp_team):
    """Create a FastAPI test client with a bootstrapped team root."""
    app = create_app(hc_home=tmp_team)
    return TestClient(app)


@pytest.fixture
def in_approval_task(tmp_team):
    """Create a task in 'in_approval' status for approval/rejection testing."""
    task = create_task(tmp_team, TEAM, title="Feature X")
    change_status(tmp_team, TEAM, task["id"], "in_progress")
    change_status(tmp_team, TEAM, task["id"], "in_review")
    change_status(tmp_team, TEAM, task["id"], "in_approval")
    return get_task(tmp_team, TEAM, task["id"])


# ---------------------------------------------------------------------------
# POST /tasks/{id}/approve
# ---------------------------------------------------------------------------

class TestApproveEndpoint:
    def test_approve_sets_approval_status(self, client, in_approval_task, tmp_team):
        resp = client.post(f"/teams/{TEAM}/tasks/{in_approval_task['id']}/approve")
        assert resp.status_code == 200

        # Verdict is now in the reviews table
        review = get_current_review(tmp_team, TEAM, in_approval_task["id"])
        assert review is not None
        assert review["verdict"] == "approved"

    def test_approve_returns_full_task(self, client, in_approval_task):
        resp = client.post(f"/teams/{TEAM}/tasks/{in_approval_task['id']}/approve")
        data = resp.json()
        assert data["id"] == in_approval_task["id"]
        assert data["title"] == "Feature X"
        assert "status" in data
        assert "created_at" in data

    def test_approve_does_not_change_status(self, client, in_approval_task, tmp_team):
        """Approve only sets approval_status, not the task status itself."""
        resp = client.post(f"/teams/{TEAM}/tasks/{in_approval_task['id']}/approve")
        data = resp.json()
        assert data["status"] == "in_approval"

    def test_approve_nonexistent_task_404(self, client):
        resp = client.post(f"/teams/{TEAM}/tasks/9999/approve")
        assert resp.status_code == 404

    def test_approve_wrong_status_400(self, client, tmp_team):
        """Cannot approve a task that is not in 'in_approval' status."""
        task = create_task(tmp_team, TEAM, title="Todo Task")
        resp = client.post(f"/teams/{TEAM}/tasks/{task['id']}/approve")
        assert resp.status_code == 400
        assert "in_approval" in resp.json()["detail"]

    def test_approve_in_progress_400(self, client, tmp_team):
        """Cannot approve a task in 'in_progress' status."""
        task = create_task(tmp_team, TEAM, title="WIP Task")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        resp = client.post(f"/teams/{TEAM}/tasks/{task['id']}/approve")
        assert resp.status_code == 400

    def test_approve_in_review_400(self, client, tmp_team):
        """Cannot approve a task still in 'in_review' status."""
        task = create_task(tmp_team, TEAM, title="Review Task")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        resp = client.post(f"/teams/{TEAM}/tasks/{task['id']}/approve")
        assert resp.status_code == 400

    def test_approve_logs_event(self, client, in_approval_task, tmp_team):
        client.post(f"/teams/{TEAM}/tasks/{in_approval_task['id']}/approve")

        from delegate.chat import get_messages
        events = get_messages(tmp_team, TEAM, msg_type="event")
        assert any("approved" in e["content"] for e in events)


# ---------------------------------------------------------------------------
# POST /tasks/{id}/reject
# ---------------------------------------------------------------------------

class TestRejectEndpoint:
    def test_reject_sets_status_and_reason(self, client, in_approval_task, tmp_team):
        resp = client.post(
            f"/teams/{TEAM}/tasks/{in_approval_task['id']}/reject",
            json={"reason": "Code quality issues"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rejected"

        # Verdict is now in the reviews table, not on the task
        loaded = get_task(tmp_team, TEAM, in_approval_task["id"])
        assert loaded["status"] == "rejected"
        review = get_current_review(tmp_team, TEAM, in_approval_task["id"])
        assert review is not None
        assert review["verdict"] == "rejected"
        assert review["summary"] == "Code quality issues"

    def test_reject_returns_full_task(self, client, in_approval_task):
        resp = client.post(
            f"/teams/{TEAM}/tasks/{in_approval_task['id']}/reject",
            json={"reason": "Not ready"},
        )
        data = resp.json()
        assert data["id"] == in_approval_task["id"]
        assert data["title"] == "Feature X"
        assert "created_at" in data

    def test_reject_sends_notification_to_manager(self, client, in_approval_task, tmp_team):
        """Rejecting a task should deliver a notification to the manager's inbox."""
        client.post(
            f"/teams/{TEAM}/tasks/{in_approval_task['id']}/reject",
            json={"reason": "Fails CI checks"},
        )

        # Check manager's inbox for the notification (direct delivery)
        inbox = read_inbox(tmp_team, TEAM, "manager", unread_only=True)
        assert len(inbox) >= 1
        notification = inbox[0]
        assert notification.recipient == "manager"
        assert "rejected" in notification.body.lower()
        assert "Fails CI checks" in notification.body

    def test_reject_notification_includes_task_info(self, client, in_approval_task, tmp_team):
        """The notification should include the task ID and title."""
        client.post(
            f"/teams/{TEAM}/tasks/{in_approval_task['id']}/reject",
            json={"reason": "Needs rework"},
        )

        inbox = read_inbox(tmp_team, TEAM, "manager", unread_only=True)
        notification = inbox[0]
        assert f"{format_task_id(in_approval_task['id'])}" in notification.body
        assert "Feature X" in notification.body

    def test_reject_nonexistent_task_404(self, client):
        resp = client.post(f"/teams/{TEAM}/tasks/9999/reject", json={"reason": "Bad"})
        assert resp.status_code == 404

    def test_reject_invalid_transition_400(self, client, tmp_team):
        """Rejecting a task that's not in 'in_approval' should fail."""
        task = create_task(tmp_team, TEAM, title="Todo Task")
        resp = client.post(
            f"/teams/{TEAM}/tasks/{task['id']}/reject",
            json={"reason": "Not mergeable"},
        )
        assert resp.status_code == 400
        assert "Invalid transition" in resp.json()["detail"]

    def test_reject_missing_reason_422(self, client, in_approval_task):
        """Request body must include the 'reason' field."""
        resp = client.post(f"/teams/{TEAM}/tasks/{in_approval_task['id']}/reject", json={})
        assert resp.status_code == 422

    def test_reject_logs_event(self, client, in_approval_task, tmp_team):
        client.post(
            f"/teams/{TEAM}/tasks/{in_approval_task['id']}/reject",
            json={"reason": "Fails tests"},
        )

        from delegate.chat import get_messages
        events = get_messages(tmp_team, TEAM, msg_type="event")
        assert any("rejected" in e["content"] and "Fails tests" in e["content"] for e in events)


# ---------------------------------------------------------------------------
# Integration: approve then reject cycle
# ---------------------------------------------------------------------------

class TestApprovalWorkflow:
    def test_approve_then_status_still_in_approval(self, client, in_approval_task, tmp_team):
        """After approval, status remains in_approval (daemon does the merge)."""
        client.post(f"/teams/{TEAM}/tasks/{in_approval_task['id']}/approve")
        loaded = get_task(tmp_team, TEAM, in_approval_task["id"])
        assert loaded["status"] == "in_approval"
        review = get_current_review(tmp_team, TEAM, in_approval_task["id"])
        assert review["verdict"] == "approved"

    def test_reject_then_rework_cycle(self, client, in_approval_task, tmp_team):
        """Full cycle: reject -> rework (in_progress) -> in_review -> in_approval."""
        # Reject
        resp = client.post(
            f"/teams/{TEAM}/tasks/{in_approval_task['id']}/reject",
            json={"reason": "Needs cleanup"},
        )
        assert resp.status_code == 200

        # Rework
        task = change_status(tmp_team, TEAM, in_approval_task["id"], "in_progress")
        assert task["status"] == "in_progress"

        # Back through in_review -> in_approval
        change_status(tmp_team, TEAM, in_approval_task["id"], "in_review")
        change_status(tmp_team, TEAM, in_approval_task["id"], "in_approval")

        # Approve this time
        resp = client.post(f"/teams/{TEAM}/tasks/{in_approval_task['id']}/approve")
        assert resp.status_code == 200
        review = get_current_review(tmp_team, TEAM, in_approval_task["id"])
        assert review["verdict"] == "approved"
