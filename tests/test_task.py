"""Tests for scripts/task.py."""

from unittest.mock import patch, MagicMock
import subprocess

import pytest

from scripts.task import (
    create_task,
    get_task,
    update_task,
    assign_task,
    change_status,
    list_tasks,
    set_task_branch,
    add_task_commit,
    get_task_diff,
    VALID_STATUSES,
    VALID_TRANSITIONS,
    VALID_APPROVAL_STATUSES,
)


class TestCreateTask:
    def test_returns_id(self, tmp_team):
        task = create_task(tmp_team, title="Build API")
        assert task["id"] == 1

    def test_increments_id(self, tmp_team):
        t1 = create_task(tmp_team, title="First")
        t2 = create_task(tmp_team, title="Second")
        assert t2["id"] == t1["id"] + 1

    def test_file_created(self, tmp_team):
        task = create_task(tmp_team, title="Build API")
        path = tmp_team / ".standup" / "tasks" / f"T{task['id']:04d}.yaml"
        assert path.is_file()

    def test_fields_persisted(self, tmp_team):
        task = create_task(
            tmp_team,
            title="Build API",
            description="REST endpoints",
            project="backend",
            priority="high",
        )
        loaded = get_task(tmp_team, task["id"])
        assert loaded["title"] == "Build API"
        assert loaded["description"] == "REST endpoints"
        assert loaded["project"] == "backend"
        assert loaded["priority"] == "high"
        assert loaded["status"] == "open"
        assert loaded["assignee"] == ""
        assert loaded["completed_at"] == ""
        assert loaded["created_at"]
        assert loaded["updated_at"]

    def test_invalid_priority_raises(self, tmp_team):
        with pytest.raises(ValueError, match="Invalid priority"):
            create_task(tmp_team, title="Bad", priority="ultra")


class TestGetTask:
    def test_get_existing(self, tmp_team):
        created = create_task(tmp_team, title="Test")
        loaded = get_task(tmp_team, created["id"])
        assert loaded["title"] == "Test"

    def test_get_nonexistent_raises(self, tmp_team):
        with pytest.raises(FileNotFoundError, match="Task 999"):
            get_task(tmp_team, 999)


class TestUpdateTask:
    def test_update_title(self, tmp_team):
        task = create_task(tmp_team, title="Old Title")
        updated = update_task(tmp_team, task["id"], title="New Title")
        assert updated["title"] == "New Title"
        # Verify persisted
        loaded = get_task(tmp_team, task["id"])
        assert loaded["title"] == "New Title"

    def test_update_description(self, tmp_team):
        task = create_task(tmp_team, title="T", description="old")
        updated = update_task(tmp_team, task["id"], description="new desc")
        assert updated["description"] == "new desc"

    def test_update_advances_updated_at(self, tmp_team):
        task = create_task(tmp_team, title="T")
        original_time = task["updated_at"]
        updated = update_task(tmp_team, task["id"], title="T2")
        assert updated["updated_at"] >= original_time

    def test_update_unknown_field_raises(self, tmp_team):
        task = create_task(tmp_team, title="T")
        with pytest.raises(ValueError, match="Unknown task field"):
            update_task(tmp_team, task["id"], nonexistent="value")

    def test_update_nonexistent_task_raises(self, tmp_team):
        with pytest.raises(FileNotFoundError):
            update_task(tmp_team, 999, title="Nope")


class TestAssignTask:
    def test_assign(self, tmp_team):
        task = create_task(tmp_team, title="Work")
        assigned = assign_task(tmp_team, task["id"], "alice")
        assert assigned["assignee"] == "alice"
        loaded = get_task(tmp_team, task["id"])
        assert loaded["assignee"] == "alice"

    def test_reassign(self, tmp_team):
        task = create_task(tmp_team, title="Work")
        assign_task(tmp_team, task["id"], "alice")
        reassigned = assign_task(tmp_team, task["id"], "bob")
        assert reassigned["assignee"] == "bob"


class TestChangeStatus:
    def test_valid_transition_chain(self, tmp_team):
        """Test a full valid transition chain: open -> in_progress -> review -> done."""
        task = create_task(tmp_team, title="Work")
        assert task["status"] == "open"

        task = change_status(tmp_team, task["id"], "in_progress")
        assert task["status"] == "in_progress"

        task = change_status(tmp_team, task["id"], "review")
        assert task["status"] == "review"

        task = change_status(tmp_team, task["id"], "done")
        assert task["status"] == "done"

    def test_merge_queue_transition_chain(self, tmp_team):
        """Test merge queue path: open -> in_progress -> review -> needs_merge -> merged."""
        task = create_task(tmp_team, title="Repo Work")

        task = change_status(tmp_team, task["id"], "in_progress")
        task = change_status(tmp_team, task["id"], "review")
        task = change_status(tmp_team, task["id"], "needs_merge")
        assert task["status"] == "needs_merge"

        task = change_status(tmp_team, task["id"], "merged")
        assert task["status"] == "merged"

    def test_invalid_status_raises(self, tmp_team):
        task = create_task(tmp_team, title="Work")
        with pytest.raises(ValueError, match="Invalid status"):
            change_status(tmp_team, task["id"], "invalid")

    def test_invalid_transition_raises(self, tmp_team):
        """Cannot skip statuses — e.g. open -> review is invalid."""
        task = create_task(tmp_team, title="Work")
        with pytest.raises(ValueError, match="Invalid transition"):
            change_status(tmp_team, task["id"], "review")

    def test_terminal_status_raises(self, tmp_team):
        """Cannot transition out of terminal statuses (done, merged)."""
        task = create_task(tmp_team, title="Work")
        change_status(tmp_team, task["id"], "in_progress")
        change_status(tmp_team, task["id"], "review")
        change_status(tmp_team, task["id"], "done")
        with pytest.raises(ValueError, match="terminal status"):
            change_status(tmp_team, task["id"], "in_progress")

    def test_completed_at_set_on_done(self, tmp_team):
        task = create_task(tmp_team, title="Work")
        assert task["completed_at"] == ""

        change_status(tmp_team, task["id"], "in_progress")
        change_status(tmp_team, task["id"], "review")
        updated = change_status(tmp_team, task["id"], "done")
        assert updated["completed_at"] != ""
        assert updated["completed_at"].startswith("20")

    def test_completed_at_set_on_merged(self, tmp_team):
        """completed_at should also be set when status becomes 'merged'."""
        task = create_task(tmp_team, title="Repo Work")
        assert task["completed_at"] == ""

        change_status(tmp_team, task["id"], "in_progress")
        change_status(tmp_team, task["id"], "review")
        change_status(tmp_team, task["id"], "needs_merge")
        updated = change_status(tmp_team, task["id"], "merged")
        assert updated["completed_at"] != ""
        assert updated["completed_at"].startswith("20")

    def test_completed_at_not_set_on_other_status(self, tmp_team):
        task = create_task(tmp_team, title="Work")
        updated = change_status(tmp_team, task["id"], "in_progress")
        assert updated["completed_at"] == ""

    def test_needs_merge_to_rejected(self, tmp_team):
        """needs_merge -> rejected is a valid transition."""
        task = create_task(tmp_team, title="Work")
        change_status(tmp_team, task["id"], "in_progress")
        change_status(tmp_team, task["id"], "review")
        change_status(tmp_team, task["id"], "needs_merge")
        updated = change_status(tmp_team, task["id"], "rejected")
        assert updated["status"] == "rejected"

    def test_needs_merge_to_conflict(self, tmp_team):
        """needs_merge -> conflict is a valid transition."""
        task = create_task(tmp_team, title="Work")
        change_status(tmp_team, task["id"], "in_progress")
        change_status(tmp_team, task["id"], "review")
        change_status(tmp_team, task["id"], "needs_merge")
        updated = change_status(tmp_team, task["id"], "conflict")
        assert updated["status"] == "conflict"

    def test_rejected_to_in_progress(self, tmp_team):
        """rejected -> in_progress (rework) is a valid transition."""
        task = create_task(tmp_team, title="Work")
        change_status(tmp_team, task["id"], "in_progress")
        change_status(tmp_team, task["id"], "review")
        change_status(tmp_team, task["id"], "needs_merge")
        change_status(tmp_team, task["id"], "rejected")
        updated = change_status(tmp_team, task["id"], "in_progress")
        assert updated["status"] == "in_progress"

    def test_conflict_to_in_progress(self, tmp_team):
        """conflict -> in_progress (rebase) is a valid transition."""
        task = create_task(tmp_team, title="Work")
        change_status(tmp_team, task["id"], "in_progress")
        change_status(tmp_team, task["id"], "review")
        change_status(tmp_team, task["id"], "needs_merge")
        change_status(tmp_team, task["id"], "conflict")
        updated = change_status(tmp_team, task["id"], "in_progress")
        assert updated["status"] == "in_progress"


class TestListTasks:
    def test_list_empty(self, tmp_team):
        assert list_tasks(tmp_team) == []

    def test_list_all(self, tmp_team):
        create_task(tmp_team, title="A")
        create_task(tmp_team, title="B")
        create_task(tmp_team, title="C")
        assert len(list_tasks(tmp_team)) == 3

    def test_filter_by_status(self, tmp_team):
        t1 = create_task(tmp_team, title="A")
        t2 = create_task(tmp_team, title="B")
        change_status(tmp_team, t1["id"], "in_progress")

        open_tasks = list_tasks(tmp_team, status="open")
        assert len(open_tasks) == 1
        assert open_tasks[0]["id"] == t2["id"]

        ip_tasks = list_tasks(tmp_team, status="in_progress")
        assert len(ip_tasks) == 1
        assert ip_tasks[0]["id"] == t1["id"]

    def test_filter_by_assignee(self, tmp_team):
        t1 = create_task(tmp_team, title="A")
        t2 = create_task(tmp_team, title="B")
        assign_task(tmp_team, t1["id"], "alice")
        assign_task(tmp_team, t2["id"], "bob")

        alice_tasks = list_tasks(tmp_team, assignee="alice")
        assert len(alice_tasks) == 1
        assert alice_tasks[0]["id"] == t1["id"]

    def test_filter_by_project(self, tmp_team):
        create_task(tmp_team, title="A", project="frontend")
        create_task(tmp_team, title="B", project="backend")
        create_task(tmp_team, title="C", project="frontend")

        fe_tasks = list_tasks(tmp_team, project="frontend")
        assert len(fe_tasks) == 2
        assert all(t["project"] == "frontend" for t in fe_tasks)

    def test_combined_filters(self, tmp_team):
        t1 = create_task(tmp_team, title="A", project="fe")
        t2 = create_task(tmp_team, title="B", project="fe")
        assign_task(tmp_team, t1["id"], "alice")
        assign_task(tmp_team, t2["id"], "bob")

        tasks = list_tasks(tmp_team, project="fe", assignee="alice")
        assert len(tasks) == 1
        assert tasks[0]["id"] == t1["id"]


class TestEventLogging:
    """Verify that task operations are logged to the chat event stream."""

    def test_create_task_logs_event(self, tmp_team):
        from scripts.chat import get_messages
        create_task(tmp_team, title="Build API", project="backend", priority="high")
        events = get_messages(tmp_team, msg_type="event")
        assert any("Created T0001:" in e["content"] for e in events)

    def test_assign_task_logs_event(self, tmp_team):
        from scripts.chat import get_messages
        t = create_task(tmp_team, title="Build API")
        assign_task(tmp_team, t["id"], "alice")
        events = get_messages(tmp_team, msg_type="event")
        assert any("assigned to Alice" in e["content"] for e in events)

    def test_change_status_logs_event(self, tmp_team):
        from scripts.chat import get_messages
        t = create_task(tmp_team, title="Build API")
        change_status(tmp_team, t["id"], "in_progress")
        events = get_messages(tmp_team, msg_type="event")
        assert any("Status of T0001 changed from Open" in e["content"] and "In Progress" in e["content"] for e in events)


class TestBranchAndCommits:
    """Tests for branch/commits fields and helper functions."""

    def test_create_task_has_branch_and_commits(self, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        assert task["branch"] == ""
        assert task["commits"] == []

    def test_set_task_branch(self, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        updated = set_task_branch(tmp_team, task["id"], "alice/backend/0001-feature-x")
        assert updated["branch"] == "alice/backend/0001-feature-x"
        # Verify persisted
        loaded = get_task(tmp_team, task["id"])
        assert loaded["branch"] == "alice/backend/0001-feature-x"

    def test_add_task_commit(self, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        updated = add_task_commit(tmp_team, task["id"], "abc123")
        assert updated["commits"] == ["abc123"]
        # Add another
        updated = add_task_commit(tmp_team, task["id"], "def456")
        assert updated["commits"] == ["abc123", "def456"]
        # Verify persisted
        loaded = get_task(tmp_team, task["id"])
        assert loaded["commits"] == ["abc123", "def456"]

    def test_add_task_commit_no_duplicates(self, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        add_task_commit(tmp_team, task["id"], "abc123")
        updated = add_task_commit(tmp_team, task["id"], "abc123")
        assert updated["commits"] == ["abc123"]

    def test_branch_survives_status_update(self, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        set_task_branch(tmp_team, task["id"], "alice/backend/0001-feature-x")
        add_task_commit(tmp_team, task["id"], "abc123")
        # Change status
        updated = change_status(tmp_team, task["id"], "in_progress")
        assert updated["branch"] == "alice/backend/0001-feature-x"
        assert updated["commits"] == ["abc123"]

    def test_get_task_diff_no_branch(self, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        result = get_task_diff(tmp_team, task["id"])
        assert result == "(no branch set)"

    @patch("scripts.task.subprocess.run")
    def test_get_task_diff_with_branch(self, mock_run, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        set_task_branch(tmp_team, task["id"], "alice/backend/0001-feature-x")

        # Mock the three-dot diff succeeding
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "diff --git a/file.py b/file.py\n+new line\n"
        mock_run.return_value = mock_result

        diff = get_task_diff(tmp_team, task["id"])
        assert "diff --git" in diff
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["git", "diff", "main...alice/backend/0001-feature-x"]

    @patch("scripts.task.subprocess.run")
    def test_get_task_diff_fallback(self, mock_run, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        set_task_branch(tmp_team, task["id"], "alice/backend/0001-feature-x")

        # First call (three-dot diff) fails, second call (git log) succeeds, third (git show) succeeds
        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stdout = ""

        log_result = MagicMock()
        log_result.returncode = 0
        log_result.stdout = "def456 Second commit\nabc123 First commit\n"

        show_result = MagicMock()
        show_result.returncode = 0
        show_result.stdout = "commit abc123\nfallback diff content\n"

        mock_run.side_effect = [fail_result, log_result, show_result]

        diff = get_task_diff(tmp_team, task["id"])
        assert "fallback diff content" in diff

    @patch("scripts.task.subprocess.run")
    def test_get_task_diff_no_diff_available(self, mock_run, tmp_team):
        task = create_task(tmp_team, title="Feature X")
        set_task_branch(tmp_team, task["id"], "alice/backend/0001-feature-x")

        # All calls fail
        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stdout = ""
        mock_run.return_value = fail_result

        diff = get_task_diff(tmp_team, task["id"])
        assert diff == "(no diff available)"

    def test_new_fields_in_create_task_output(self, tmp_team):
        task = create_task(tmp_team, title="Test Fields")
        assert "branch" in task
        assert "commits" in task
        assert isinstance(task["branch"], str)
        assert isinstance(task["commits"], list)


class TestMergeQueueFields:
    """Tests for rejection_reason and approval_status fields."""

    def test_create_task_has_new_fields(self, tmp_team):
        task = create_task(tmp_team, title="Merge Queue Task")
        assert task["rejection_reason"] == ""
        assert task["approval_status"] == ""

    def test_new_fields_persisted(self, tmp_team):
        task = create_task(tmp_team, title="Merge Queue Task")
        loaded = get_task(tmp_team, task["id"])
        assert loaded["rejection_reason"] == ""
        assert loaded["approval_status"] == ""

    def test_update_rejection_reason(self, tmp_team):
        task = create_task(tmp_team, title="Work")
        updated = update_task(tmp_team, task["id"], rejection_reason="Code quality issues")
        assert updated["rejection_reason"] == "Code quality issues"
        loaded = get_task(tmp_team, task["id"])
        assert loaded["rejection_reason"] == "Code quality issues"

    def test_update_approval_status(self, tmp_team):
        task = create_task(tmp_team, title="Work")
        updated = update_task(tmp_team, task["id"], approval_status="pending")
        assert updated["approval_status"] == "pending"
        loaded = get_task(tmp_team, task["id"])
        assert loaded["approval_status"] == "pending"

    def test_fields_survive_status_change(self, tmp_team):
        task = create_task(tmp_team, title="Work")
        update_task(tmp_team, task["id"], rejection_reason="Needs fixes", approval_status="rejected")
        updated = change_status(tmp_team, task["id"], "in_progress")
        assert updated["rejection_reason"] == "Needs fixes"
        assert updated["approval_status"] == "rejected"

    def test_old_tasks_get_defaults(self, tmp_team):
        """Tasks created before merge queue fields should get defaults via setdefault."""
        import yaml
        task = create_task(tmp_team, title="Old Task")
        # Simulate an old task file without the new fields
        tasks_dir = tmp_team / ".standup" / "tasks"
        path = tasks_dir / f"T{task['id']:04d}.yaml"
        data = yaml.safe_load(path.read_text())
        del data["rejection_reason"]
        del data["approval_status"]
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

        # get_task should fill in defaults
        loaded = get_task(tmp_team, task["id"])
        assert loaded["rejection_reason"] == ""
        assert loaded["approval_status"] == ""


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
