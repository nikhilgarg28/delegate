"""Tests for scripts/task.py."""

from unittest.mock import patch, MagicMock
import subprocess

import pytest

from tests.conftest import SAMPLE_TEAM_NAME as TEAM
from delegate.task import (
    create_task,
    get_task,
    update_task,
    assign_task,
    change_status,
    list_tasks,
    set_task_branch,
    get_task_diff,
    add_comment,
    get_comments,
    VALID_STATUSES,
    VALID_TRANSITIONS,
    VALID_APPROVAL_STATUSES,
    format_task_id,
)


class TestCreateTask:
    def test_returns_id(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Build API", assignee="alice")
        assert task["id"] == 1

    def test_increments_id(self, tmp_team):
        t1 = create_task(tmp_team, TEAM, title="First", assignee="alice")
        t2 = create_task(tmp_team, TEAM, title="Second", assignee="alice")
        assert t2["id"] == t1["id"] + 1

    def test_persisted_in_db(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Build API", assignee="alice")
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["title"] == "Build API"

    def test_fields_persisted(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Build API", assignee="alice", description="REST endpoints",
            project="backend",
            priority="high",
        )
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["title"] == "Build API"
        assert loaded["description"] == "REST endpoints"
        assert loaded["project"] == "backend"
        assert loaded["priority"] == "high"
        assert loaded["status"] == "todo"
        assert loaded["assignee"] == "alice"
        assert loaded["dri"] == "alice"
        assert loaded["completed_at"] == ""
        assert loaded["created_at"]
        assert loaded["updated_at"]

    def test_invalid_priority_raises(self, tmp_team):
        with pytest.raises(ValueError, match="Invalid priority"):
            create_task(tmp_team, TEAM, title="Bad", assignee="alice", priority="ultra")

    def test_missing_assignee_raises(self, tmp_team):
        with pytest.raises(ValueError, match="Assignee/DRI is required"):
            create_task(tmp_team, TEAM, title="No Assignee", assignee="")

    def test_whitespace_assignee_raises(self, tmp_team):
        with pytest.raises(ValueError, match="Assignee/DRI is required"):
            create_task(tmp_team, TEAM, title="Whitespace Assignee", assignee="   ")


class TestGetTask:
    def test_get_existing(self, tmp_team):
        created = create_task(tmp_team, TEAM, title="Test", assignee="alice")
        loaded = get_task(tmp_team, TEAM, created["id"])
        assert loaded["title"] == "Test"

    def test_get_nonexistent_raises(self, tmp_team):
        with pytest.raises(FileNotFoundError, match="Task 999"):
            get_task(tmp_team, TEAM, 999)


class TestUpdateTask:
    def test_update_title(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Old Title", assignee="alice")
        updated = update_task(tmp_team, TEAM, task["id"], title="New Title")
        assert updated["title"] == "New Title"
        # Verify persisted
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["title"] == "New Title"

    def test_update_description(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="T", assignee="alice", description="old")
        updated = update_task(tmp_team, TEAM, task["id"], description="new desc")
        assert updated["description"] == "new desc"

    def test_update_advances_updated_at(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="T", assignee="alice")
        original_time = task["updated_at"]
        updated = update_task(tmp_team, TEAM, task["id"], title="T2")
        assert updated["updated_at"] >= original_time

    def test_update_unknown_field_raises(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="T", assignee="alice")
        with pytest.raises(ValueError, match="Unknown task field"):
            update_task(tmp_team, TEAM, task["id"], nonexistent="value")

    def test_update_nonexistent_task_raises(self, tmp_team):
        with pytest.raises(FileNotFoundError):
            update_task(tmp_team, TEAM, 999, title="Nope")


class TestAssignTask:
    def test_assign(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        assigned = assign_task(tmp_team, TEAM, task["id"], "alice")
        assert assigned["assignee"] == "alice"
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["assignee"] == "alice"

    def test_reassign(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        reassigned = assign_task(tmp_team, TEAM, task["id"], "bob")
        assert reassigned["assignee"] == "bob"


@patch("delegate.task._validate_review_gate")
class TestChangeStatus:
    def test_valid_transition_chain(self, _mock_gate, tmp_team):
        """Test a full valid transition chain: todo -> in_progress -> in_review -> in_approval -> merging -> done."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        assert task["status"] == "todo"

        task = change_status(tmp_team, TEAM, task["id"], "in_progress")
        assert task["status"] == "in_progress"

        task = change_status(tmp_team, TEAM, task["id"], "in_review")
        assert task["status"] == "in_review"

        task = change_status(tmp_team, TEAM, task["id"], "in_approval")
        assert task["status"] == "in_approval"

        task = change_status(tmp_team, TEAM, task["id"], "merging")
        assert task["status"] == "merging"

        task = change_status(tmp_team, TEAM, task["id"], "done")
        assert task["status"] == "done"

    def test_merge_queue_transition_chain(self, _mock_gate, tmp_team):
        """Test approval path: todo -> in_progress -> in_review -> in_approval -> merging -> done."""
        task = create_task(tmp_team, TEAM, title="Repo Work", assignee="alice")

        task = change_status(tmp_team, TEAM, task["id"], "in_progress")
        task = change_status(tmp_team, TEAM, task["id"], "in_review")
        task = change_status(tmp_team, TEAM, task["id"], "in_approval")
        assert task["status"] == "in_approval"

        task = change_status(tmp_team, TEAM, task["id"], "merging")
        assert task["status"] == "merging"

        task = change_status(tmp_team, TEAM, task["id"], "done")
        assert task["status"] == "done"

    def test_invalid_status_raises(self, _mock_gate, tmp_team):
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        with pytest.raises(ValueError, match="Invalid status"):
            change_status(tmp_team, TEAM, task["id"], "invalid")

    def test_invalid_transition_raises(self, _mock_gate, tmp_team):
        """Cannot skip statuses — e.g. todo -> in_review is invalid."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        with pytest.raises(ValueError, match="Invalid transition"):
            change_status(tmp_team, TEAM, task["id"], "in_review")

    def test_terminal_status_raises(self, _mock_gate, tmp_team):
        """Cannot transition out of terminal status (done)."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        change_status(tmp_team, TEAM, task["id"], "merging")
        change_status(tmp_team, TEAM, task["id"], "done")
        with pytest.raises(ValueError, match="terminal status"):
            change_status(tmp_team, TEAM, task["id"], "in_progress")

    def test_completed_at_set_on_done(self, _mock_gate, tmp_team):
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        assert task["completed_at"] == ""

        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        change_status(tmp_team, TEAM, task["id"], "merging")
        updated = change_status(tmp_team, TEAM, task["id"], "done")
        assert updated["completed_at"] != ""
        assert updated["completed_at"].startswith("20")

    def test_completed_at_set_on_done_via_approval(self, _mock_gate, tmp_team):
        """completed_at should be set when done via the approval path."""
        task = create_task(tmp_team, TEAM, title="Repo Work", assignee="alice")
        assert task["completed_at"] == ""

        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        change_status(tmp_team, TEAM, task["id"], "merging")
        updated = change_status(tmp_team, TEAM, task["id"], "done")
        assert updated["completed_at"] != ""
        assert updated["completed_at"].startswith("20")

    def test_completed_at_not_set_on_other_status(self, _mock_gate, tmp_team):
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        updated = change_status(tmp_team, TEAM, task["id"], "in_progress")
        assert updated["completed_at"] == ""

    def test_merging_to_done(self, _mock_gate, tmp_team):
        """merging -> done is a valid transition."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        change_status(tmp_team, TEAM, task["id"], "merging")
        task = change_status(tmp_team, TEAM, task["id"], "done")
        assert task["status"] == "done"

    def test_merging_to_merge_failed(self, _mock_gate, tmp_team):
        """merging -> merge_failed is a valid transition."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        change_status(tmp_team, TEAM, task["id"], "merging")
        task = change_status(tmp_team, TEAM, task["id"], "merge_failed")
        assert task["status"] == "merge_failed"

    def test_in_approval_cannot_go_to_done_directly(self, _mock_gate, tmp_team):
        """in_approval -> done is no longer valid (must go through merging)."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        with pytest.raises(ValueError, match="Invalid transition"):
            change_status(tmp_team, TEAM, task["id"], "done")

    def test_in_approval_to_rejected(self, _mock_gate, tmp_team):
        """in_approval -> rejected is a valid transition."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        updated = change_status(tmp_team, TEAM, task["id"], "rejected")
        assert updated["status"] == "rejected"

    def test_in_approval_cannot_go_to_merge_failed_directly(self, _mock_gate, tmp_team):
        """in_approval -> merge_failed is no longer valid (must go through merging)."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        with pytest.raises(ValueError, match="Invalid transition"):
            change_status(tmp_team, TEAM, task["id"], "merge_failed")

    def test_rejected_to_in_progress(self, _mock_gate, tmp_team):
        """rejected -> in_progress (rework) is a valid transition."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        change_status(tmp_team, TEAM, task["id"], "rejected")
        updated = change_status(tmp_team, TEAM, task["id"], "in_progress")
        assert updated["status"] == "in_progress"

    def test_merge_failed_to_in_progress(self, _mock_gate, tmp_team):
        """merge_failed -> in_progress (rework) is a valid transition."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        change_status(tmp_team, TEAM, task["id"], "in_approval")
        change_status(tmp_team, TEAM, task["id"], "merging")
        change_status(tmp_team, TEAM, task["id"], "merge_failed")
        updated = change_status(tmp_team, TEAM, task["id"], "in_progress")
        assert updated["status"] == "in_progress"

    def test_in_review_cannot_go_to_done_directly(self, _mock_gate, tmp_team):
        """in_review -> done is no longer valid (must go through in_approval → merging)."""
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "in_review")
        with pytest.raises(ValueError, match="Invalid transition"):
            change_status(tmp_team, TEAM, task["id"], "done")


class TestListTasks:
    def test_list_empty(self, tmp_team):
        assert list_tasks(tmp_team, TEAM) == []

    def test_list_all(self, tmp_team):
        create_task(tmp_team, TEAM, title="A", assignee="alice")
        create_task(tmp_team, TEAM, title="B", assignee="alice")
        create_task(tmp_team, TEAM, title="C", assignee="alice")
        assert len(list_tasks(tmp_team, TEAM)) == 3

    def test_filter_by_status(self, tmp_team):
        t1 = create_task(tmp_team, TEAM, title="A", assignee="alice")
        t2 = create_task(tmp_team, TEAM, title="B", assignee="alice")
        change_status(tmp_team, TEAM, t1["id"], "in_progress")

        open_tasks = list_tasks(tmp_team, TEAM, status="todo")
        assert len(open_tasks) == 1
        assert open_tasks[0]["id"] == t2["id"]

        ip_tasks = list_tasks(tmp_team, TEAM, status="in_progress")
        assert len(ip_tasks) == 1
        assert ip_tasks[0]["id"] == t1["id"]

    def test_filter_by_assignee(self, tmp_team):
        t1 = create_task(tmp_team, TEAM, title="A", assignee="alice")
        t2 = create_task(tmp_team, TEAM, title="B", assignee="alice")
        assign_task(tmp_team, TEAM, t1["id"], "alice")
        assign_task(tmp_team, TEAM, t2["id"], "bob")

        alice_tasks = list_tasks(tmp_team, TEAM, assignee="alice")
        assert len(alice_tasks) == 1
        assert alice_tasks[0]["id"] == t1["id"]

    def test_filter_by_project(self, tmp_team):
        create_task(tmp_team, TEAM, title="A", assignee="alice", project="frontend")
        create_task(tmp_team, TEAM, title="B", assignee="alice", project="backend")
        create_task(tmp_team, TEAM, title="C", assignee="alice", project="frontend")

        fe_tasks = list_tasks(tmp_team, TEAM, project="frontend")
        assert len(fe_tasks) == 2
        assert all(t["project"] == "frontend" for t in fe_tasks)

    def test_combined_filters(self, tmp_team):
        t1 = create_task(tmp_team, TEAM, title="A", assignee="alice", project="fe")
        t2 = create_task(tmp_team, TEAM, title="B", assignee="alice", project="fe")
        assign_task(tmp_team, TEAM, t1["id"], "alice")
        assign_task(tmp_team, TEAM, t2["id"], "bob")

        tasks = list_tasks(tmp_team, TEAM, project="fe", assignee="alice")
        assert len(tasks) == 1
        assert tasks[0]["id"] == t1["id"]


class TestEventLogging:
    """Verify that task operations are logged to the chat event stream."""

    def test_create_task_logs_event(self, tmp_team):
        from delegate.chat import get_messages
        create_task(tmp_team, TEAM, title="Build API", assignee="alice", project="backend", priority="high")
        events = get_messages(tmp_team, TEAM, msg_type="event")
        assert any("T0001 created" in e["content"] for e in events)

    def test_assign_task_logs_event(self, tmp_team):
        from delegate.chat import get_messages
        t = create_task(tmp_team, TEAM, title="Build API", assignee="alice")
        assign_task(tmp_team, TEAM, t["id"], "alice")
        events = get_messages(tmp_team, TEAM, msg_type="event")
        assert any("assigned to Alice" in e["content"] for e in events)

    def test_change_status_logs_event(self, tmp_team):
        from delegate.chat import get_messages
        t = create_task(tmp_team, TEAM, title="Build API", assignee="alice")
        change_status(tmp_team, TEAM, t["id"], "in_progress")
        events = get_messages(tmp_team, TEAM, msg_type="event")
        assert any("T0001 Todo" in e["content"] and "In Progress" in e["content"] for e in events)

    def test_assign_task_suppress_log(self, tmp_team):
        from delegate.chat import get_messages
        t = create_task(tmp_team, TEAM, title="Build API", assignee="alice")
        events_before = get_messages(tmp_team, TEAM, msg_type="event")
        assign_task(tmp_team, TEAM, t["id"], "bob", suppress_log=True)
        events_after = get_messages(tmp_team, TEAM, msg_type="event")
        # No new events should have been created
        assert len(events_after) == len(events_before)

    def test_change_status_suppress_log(self, tmp_team):
        from delegate.chat import get_messages
        t = create_task(tmp_team, TEAM, title="Build API", assignee="alice")
        events_before = get_messages(tmp_team, TEAM, msg_type="event")
        change_status(tmp_team, TEAM, t["id"], "in_progress", suppress_log=True)
        events_after = get_messages(tmp_team, TEAM, msg_type="event")
        # No new events should have been created
        assert len(events_after) == len(events_before)

    def test_transition_task_combines_messages(self, tmp_team):
        from delegate.chat import get_messages
        from delegate.task import transition_task
        t = create_task(tmp_team, TEAM, title="Build API", assignee="alice")
        events_before = get_messages(tmp_team, TEAM, msg_type="event")

        # Transition status and assignee together
        updated = transition_task(tmp_team, TEAM, t["id"], "in_progress", "bob")

        # Should have created exactly ONE new event
        events_after = get_messages(tmp_team, TEAM, msg_type="event")
        new_events = [e for e in events_after if e not in events_before]
        assert len(new_events) == 1

        # Verify the combined message format
        combined_msg = new_events[0]["content"]
        assert "T0001" in combined_msg
        assert "Todo" in combined_msg
        assert "In Progress" in combined_msg
        assert "assigned to Bob" in combined_msg
        # Check it uses the colon format: "T0001: Todo → In Progress, assigned to Bob"
        assert ":" in combined_msg

    def test_transition_task_updates_both_fields(self, tmp_team):
        from delegate.task import transition_task
        t = create_task(tmp_team, TEAM, title="Build API", assignee="alice")
        updated = transition_task(tmp_team, TEAM, t["id"], "in_progress", "bob")

        # Verify both status and assignee were updated
        assert updated["status"] == "in_progress"
        assert updated["assignee"] == "bob"


class TestBranchAndDiff:
    """Tests for branch fields and diff helper functions."""

    def test_create_task_has_branch(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice")
        assert task["branch"] == ""

    def test_set_task_branch(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice")
        updated = set_task_branch(tmp_team, TEAM, task["id"], "alice/backend/0001-feature-x")
        assert updated["branch"] == "alice/backend/0001-feature-x"
        # Verify persisted
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["branch"] == "alice/backend/0001-feature-x"

    def test_branch_survives_status_update(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice")
        set_task_branch(tmp_team, TEAM, task["id"], "alice/backend/0001-feature-x")
        # Change status
        updated = change_status(tmp_team, TEAM, task["id"], "in_progress")
        assert updated["branch"] == "alice/backend/0001-feature-x"

    def test_get_task_diff_no_branch(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice")
        result = get_task_diff(tmp_team, TEAM, task["id"])
        assert result == {"_default": "(no branch set)"}

    @patch("delegate.task.subprocess.run")
    def test_get_task_diff_with_branch(self, mock_run, tmp_team):
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice")
        set_task_branch(tmp_team, TEAM, task["id"], "alice/backend/0001-feature-x")

        # Mock the three-dot diff succeeding
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "diff --git a/file.py b/file.py\n+new line\n"
        mock_run.return_value = mock_result

        diff = get_task_diff(tmp_team, TEAM, task["id"])
        assert "diff --git" in diff["_default"]
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["git", "diff", "main...alice/backend/0001-feature-x"]

    @patch("delegate.task.subprocess.run")
    def test_get_task_diff_no_diff_available(self, mock_run, tmp_team):
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice")
        set_task_branch(tmp_team, TEAM, task["id"], "alice/backend/0001-feature-x")

        # All calls fail
        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stdout = ""
        mock_run.return_value = fail_result

        diff = get_task_diff(tmp_team, TEAM, task["id"])
        assert diff["_default"] == "(no diff available)"

    def test_new_fields_in_create_task_output(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Test Fields", assignee="alice")
        assert "branch" in task
        assert isinstance(task["branch"], str)


class TestMergeQueueFields:
    """Tests for rejection_reason and approval_status fields."""

    def test_create_task_has_new_fields(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Merge Queue Task", assignee="alice")
        assert task["rejection_reason"] == ""
        assert task["approval_status"] == ""

    def test_new_fields_persisted(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Merge Queue Task", assignee="alice")
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["rejection_reason"] == ""
        assert loaded["approval_status"] == ""

    def test_update_rejection_reason(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        updated = update_task(tmp_team, TEAM, task["id"], rejection_reason="Code quality issues")
        assert updated["rejection_reason"] == "Code quality issues"
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["rejection_reason"] == "Code quality issues"

    def test_update_approval_status(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        updated = update_task(tmp_team, TEAM, task["id"], approval_status="pending")
        assert updated["approval_status"] == "pending"
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["approval_status"] == "pending"

    def test_fields_survive_status_change(self, tmp_team):
        task = create_task(tmp_team, TEAM, title="Work", assignee="alice")
        update_task(tmp_team, TEAM, task["id"], rejection_reason="Needs fixes", approval_status="rejected")
        updated = change_status(tmp_team, TEAM, task["id"], "in_progress")
        assert updated["rejection_reason"] == "Needs fixes"
        assert updated["approval_status"] == "rejected"

    def test_defaults_on_fresh_task(self, tmp_team):
        """Tasks created fresh should have empty defaults for merge queue fields."""
        task = create_task(tmp_team, TEAM, title="Fresh Task", assignee="alice")
        loaded = get_task(tmp_team, TEAM, task["id"])
        assert loaded["rejection_reason"] == ""
        assert loaded["approval_status"] == ""


@patch("delegate.task._validate_review_gate")
class TestBranchMetadataBackfill:
    """Tests that branch and base_sha are backfilled on status transitions."""

    @patch("delegate.task.subprocess.run")
    def test_in_review_backfills_branch_when_empty(self, mock_run, _mock_gate, tmp_team):
        """Transitioning to in_review should backfill branch from assignee + task_id."""
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice", repo="myrepo")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")

        # Mock git merge-base for base_sha backfill
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc123def456" * 3 + "abcd"  # 40 chars
        mock_run.return_value = mock_result

        updated = change_status(tmp_team, TEAM, task["id"], "in_review")
        from delegate.paths import get_team_id
        tid = get_team_id(tmp_team, TEAM)
        assert updated["branch"] == f"delegate/{tid}/{TEAM}/T0001"

    @patch("delegate.task.subprocess.run")
    def test_in_review_backfills_base_sha_when_empty(self, mock_run, _mock_gate, tmp_team):
        """Transitioning to in_review should backfill base_sha via git merge-base."""
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice", repo="myrepo")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        set_task_branch(tmp_team, TEAM, task["id"], "alice/T0001")
        change_status(tmp_team, TEAM, task["id"], "in_progress")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc123def456789012345678901234567890"  # 36 chars
        mock_run.return_value = mock_result

        updated = change_status(tmp_team, TEAM, task["id"], "in_review")
        assert updated["base_sha"] == {"myrepo": "abc123def456789012345678901234567890"}

    @patch("delegate.task.subprocess.run")
    def test_in_review_does_not_overwrite_existing_branch(self, mock_run, _mock_gate, tmp_team):
        """If branch is already set, in_review transition should not overwrite it."""
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice", repo="myrepo")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        set_task_branch(tmp_team, TEAM, task["id"], "alice/custom-branch")
        update_task(tmp_team, TEAM, task["id"], base_sha={"myrepo": "existing_sha"})
        change_status(tmp_team, TEAM, task["id"], "in_progress")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        updated = change_status(tmp_team, TEAM, task["id"], "in_review")
        assert updated["branch"] == "alice/custom-branch"
        assert updated["base_sha"] == {"myrepo": "existing_sha"}

    def test_no_backfill_without_repo(self, _mock_gate, tmp_team):
        """Tasks without a repo should not attempt backfill."""
        task = create_task(tmp_team, TEAM, title="No Repo Task", assignee="alice")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        updated = change_status(tmp_team, TEAM, task["id"], "in_review")
        assert updated["branch"] == ""
        assert updated["base_sha"] == {}

    @patch("delegate.task.subprocess.run")
    def test_in_approval_backfills_branch(self, mock_run, _mock_gate, tmp_team):
        """Transitioning to in_approval should also backfill branch."""
        task = create_task(tmp_team, TEAM, title="Feature X", assignee="alice", repo="myrepo")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")

        # For the review transition
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc123def456789012345678901234567890"
        mock_run.return_value = mock_result

        change_status(tmp_team, TEAM, task["id"], "in_review")

        # Branch was backfilled during in_review; verify it's still there for in_approval
        updated = change_status(tmp_team, TEAM, task["id"], "in_approval")
        from delegate.paths import get_team_id
        tid = get_team_id(tmp_team, TEAM)
        assert updated["branch"] == f"delegate/{tid}/{TEAM}/T0001"


class TestValidTransitions:
    """Tests that verify the VALID_TRANSITIONS map is correct."""

    def test_all_statuses_have_transition_entry(self):
        """Every valid status should have an entry in VALID_TRANSITIONS."""
        for status in VALID_STATUSES:
            assert status in VALID_TRANSITIONS, f"Missing transition entry for '{status}'"

    def test_transition_targets_are_valid(self):
        """All transition targets should be valid statuses."""
        for from_status, targets in VALID_TRANSITIONS.items():
            for target in targets:
                assert target in VALID_STATUSES, (
                    f"Transition target '{target}' from '{from_status}' is not a valid status"
                )


class TestTaskComments:
    """Tests for add_comment() and get_comments()."""

    def test_add_and_get_comment(self, tmp_team):
        """Basic add + get round-trip."""
        task = create_task(tmp_team, TEAM, title="Comment Task", assignee="alice")
        cid = add_comment(tmp_team, TEAM, task["id"], "alice", "Found a bug in the parser")
        assert cid >= 1

        comments = get_comments(tmp_team, TEAM, task["id"])
        assert len(comments) == 1
        assert comments[0]["author"] == "alice"
        assert comments[0]["body"] == "Found a bug in the parser"
        assert comments[0]["task_id"] == task["id"]

    def test_multiple_comments_ordered(self, tmp_team):
        """Comments are returned oldest-first."""
        task = create_task(tmp_team, TEAM, title="Multi Comment", assignee="bob")
        add_comment(tmp_team, TEAM, task["id"], "alice", "First comment")
        add_comment(tmp_team, TEAM, task["id"], "bob", "Second comment")
        add_comment(tmp_team, TEAM, task["id"], "alice", "Third comment")

        comments = get_comments(tmp_team, TEAM, task["id"])
        assert len(comments) == 3
        assert comments[0]["body"] == "First comment"
        assert comments[1]["body"] == "Second comment"
        assert comments[2]["body"] == "Third comment"

    def test_comments_scoped_to_task(self, tmp_team):
        """Comments on one task don't leak to another."""
        t1 = create_task(tmp_team, TEAM, title="Task A", assignee="alice")
        t2 = create_task(tmp_team, TEAM, title="Task B", assignee="alice")
        add_comment(tmp_team, TEAM, t1["id"], "alice", "Comment on A")
        add_comment(tmp_team, TEAM, t2["id"], "bob", "Comment on B")

        c1 = get_comments(tmp_team, TEAM, t1["id"])
        c2 = get_comments(tmp_team, TEAM, t2["id"])
        assert len(c1) == 1
        assert c1[0]["body"] == "Comment on A"
        assert len(c2) == 1
        assert c2[0]["body"] == "Comment on B"

    def test_comment_limit(self, tmp_team):
        """Limit parameter caps the number of returned comments."""
        task = create_task(tmp_team, TEAM, title="Lots of Comments", assignee="alice")
        for i in range(10):
            add_comment(tmp_team, TEAM, task["id"], "alice", f"Comment {i}")

        comments = get_comments(tmp_team, TEAM, task["id"], limit=3)
        assert len(comments) == 3
        assert comments[0]["body"] == "Comment 0"

    def test_comment_on_nonexistent_task(self, tmp_team):
        """Adding a comment to a missing task raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            add_comment(tmp_team, TEAM, 9999, "alice", "This should fail")

    def test_empty_comments(self, tmp_team):
        """get_comments for a task with no comments returns empty list."""
        task = create_task(tmp_team, TEAM, title="No Comments", assignee="alice")
        comments = get_comments(tmp_team, TEAM, task["id"])
        assert comments == []


class TestTaskTimeline:
    """Tests for get_task_timeline() merging comments and events."""

    def test_timeline_includes_events_and_comments(self, tmp_team):
        """Timeline should contain both system events and comments."""
        from delegate.chat import get_task_timeline

        task = create_task(tmp_team, TEAM, title="Timeline Task", assignee="alice")
        # create_task logs a "created" event automatically
        add_comment(tmp_team, TEAM, task["id"], "alice", "Starting work on this")

        timeline = get_task_timeline(tmp_team, TEAM, task["id"])
        types = [item["type"] for item in timeline]
        # Should have at least one event (from creation + comment event) and one comment
        assert "event" in types
        assert "comment" in types

    def test_timeline_chronological_order(self, tmp_team):
        """Timeline items should be ordered by timestamp."""
        from delegate.chat import get_task_timeline

        task = create_task(tmp_team, TEAM, title="Order Task", assignee="alice")
        add_comment(tmp_team, TEAM, task["id"], "alice", "Early comment")
        add_comment(tmp_team, TEAM, task["id"], "bob", "Later comment")

        timeline = get_task_timeline(tmp_team, TEAM, task["id"])
        timestamps = [item["timestamp"] for item in timeline]
        assert timestamps == sorted(timestamps)

    def test_comment_entries_have_correct_shape(self, tmp_team):
        """Comment entries should have type=comment, sender=author, content=body."""
        from delegate.chat import get_task_timeline

        task = create_task(tmp_team, TEAM, title="Shape Test", assignee="alice")
        add_comment(tmp_team, TEAM, task["id"], "alice", "Testing shape")

        timeline = get_task_timeline(tmp_team, TEAM, task["id"])
        comments = [item for item in timeline if item["type"] == "comment"]
        assert len(comments) == 1
        c = comments[0]
        assert c["sender"] == "alice"
        assert c["content"] == "Testing shape"
        assert c["recipient"] == ""
        assert c["task_id"] == task["id"]
