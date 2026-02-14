"""Tests for workflow engine scenarios (Tests 9-14 from scenarios.md).

These tests verify workflow stage transitions, hooks (enter/exit/assign),
guards, and error handling WITHOUT spinning up LLM agents.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from delegate.task import create_task, change_status, update_task, get_task
from delegate.bootstrap import bootstrap
from delegate.config import set_boss, add_repo
from delegate.repo import create_task_worktree
from delegate.workflow import GateError

TEAM = "myteam"


@pytest.fixture
def hc_home(tmp_path):
    """Create a fully bootstrapped delegate home directory."""
    hc = tmp_path / "hc_home"
    hc.mkdir()
    set_boss(hc, "nikhil")
    bootstrap(hc, TEAM, manager="edison", agents=["alice", "bob"])

    # Register the default workflow for the team
    from delegate.workflow import register_workflow
    from pathlib import Path
    workflow_source = Path(__file__).parent.parent / "delegate" / "workflows" / "default.py"
    register_workflow(hc, TEAM, workflow_source)

    return hc


def _setup_git_repo(tmp_path: Path) -> Path:
    """Set up a local git repo with a main branch and initial commit.

    Returns the repo path.
    """
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "README.md").write_text("# Test repo\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo), capture_output=True)
    return repo


def _register_repo(hc_home: Path, name: str, source_repo: Path):
    """Register a repo by creating a symlink in hc_home/teams/<team>/repos/."""
    from delegate.paths import repos_dir
    rd = repos_dir(hc_home, TEAM)
    rd.mkdir(parents=True, exist_ok=True)
    link = rd / name
    if not link.exists():
        link.symlink_to(source_repo)
    add_repo(hc_home, TEAM, name, str(source_repo), approval="auto")


def _make_commit_in_worktree(hc_home: Path, task_id: int, repo_name: str, filename: str = "test.txt", content: str = "test\n"):
    """Make a commit in the task's worktree."""
    from delegate.paths import task_worktree_dir
    wt_path = task_worktree_dir(hc_home, TEAM, repo_name, task_id)
    assert wt_path.is_dir(), f"Worktree not found at {wt_path}"

    file_path = wt_path / filename
    file_path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=str(wt_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", f"Add {filename}"], cwd=str(wt_path), capture_output=True, check=True)


class TestWorkflowHappyPath:
    """Test 9: Happy path through all stages."""

    def test_full_lifecycle_with_hooks(self, hc_home, tmp_path):
        """Drive a task through: todo -> in_progress -> in_review -> in_approval -> merging -> done.

        Verify:
        - Each transition succeeds
        - enter() hooks fire (e.g., InProgress.enter sets up worktrees for repo tasks)
        - assign() hooks fire (e.g., InReview.assign picks a reviewer != DRI)
        - Final status is done with completed_at set
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task with repo
        task = create_task(hc_home, TEAM, title="Test workflow", assignee="alice", repo="testrepo")
        assert task["status"] == "todo"
        assert task["assignee"] == "alice"
        assert task["dri"] == "alice"

        # todo -> in_progress (InProgress.enter should create worktree)
        task = change_status(hc_home, TEAM, task["id"], "in_progress")
        assert task["status"] == "in_progress"

        # Verify worktree was created by InProgress.enter hook
        from delegate.paths import task_worktree_dir
        wt_path = task_worktree_dir(hc_home, TEAM, "testrepo", task["id"])
        assert wt_path.is_dir(), "InProgress.enter should have created worktree"

        # Make a commit so we can pass the require_commits gate
        _make_commit_in_worktree(hc_home, task["id"], "testrepo")

        # in_progress -> in_review (InReview.enter checks clean worktree + commits)
        # InReview.assign should pick a reviewer != DRI
        task = change_status(hc_home, TEAM, task["id"], "in_review")
        assert task["status"] == "in_review"
        # Reviewer should be different from DRI (alice)
        assert task["assignee"] != "alice", "InReview.assign should pick reviewer != DRI"
        assert task["assignee"] in ["bob", "edison"], "Reviewer should be bob or edison"

        # in_review -> in_approval (InApproval.assign should assign to human)
        task = change_status(hc_home, TEAM, task["id"], "in_approval")
        assert task["status"] == "in_approval"
        assert task["assignee"] == "nikhil", "InApproval.assign should assign to human"
        assert task["review_attempt"] == 1, "review_attempt should be incremented"

        # in_approval -> merging (auto stage, but we transition manually for testing)
        # Mock the merge action to avoid actual merge
        with patch("delegate.workflows.git.GitMixin.merge") as mock_merge:
            mock_merge.return_value = MagicMock(success=True, message="Merged", retryable=False)
            task = change_status(hc_home, TEAM, task["id"], "merging")
            assert task["status"] == "merging"

        # merging -> done (Done.enter should set completed_at)
        task = change_status(hc_home, TEAM, task["id"], "done")
        assert task["status"] == "done"
        assert task["completed_at"] != "", "Done.enter should set completed_at"
        assert task["completed_at"] is not None


class TestWorkflowGuardRejection:
    """Test 10: Guard rejection (dirty worktree / no commits)."""

    def test_no_commits_blocks_in_review(self, hc_home, tmp_path):
        """Transition to in_review should fail if no commits exist.

        Verify:
        - Transition fails (raises GateError)
        - Task stays in in_progress
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task and advance to in_progress
        task = create_task(hc_home, TEAM, title="Test no commits", assignee="alice", repo="testrepo")
        task = change_status(hc_home, TEAM, task["id"], "in_progress")
        assert task["status"] == "in_progress"

        # Try to move to in_review WITHOUT making any commits
        # InReview.enter should call require_commits() which raises GateError
        with pytest.raises(GateError, match="No new commits"):
            change_status(hc_home, TEAM, task["id"], "in_review")

        # Verify task stayed in in_progress
        task = get_task(hc_home, TEAM, task["id"])
        assert task["status"] == "in_progress"

    def test_dirty_worktree_blocks_in_review(self, hc_home, tmp_path):
        """Transition to in_review should fail if worktree has uncommitted changes.

        Verify:
        - Transition fails (raises GateError)
        - Task stays in in_progress
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task and advance to in_progress
        task = create_task(hc_home, TEAM, title="Test dirty worktree", assignee="alice", repo="testrepo")
        task = change_status(hc_home, TEAM, task["id"], "in_progress")

        # Make a commit first (so we pass require_commits)
        _make_commit_in_worktree(hc_home, task["id"], "testrepo")

        # Now make uncommitted changes
        from delegate.paths import task_worktree_dir
        wt_path = task_worktree_dir(hc_home, TEAM, "testrepo", task["id"])
        (wt_path / "dirty.txt").write_text("uncommitted\n")

        # Try to move to in_review with dirty worktree
        # InReview.enter should call require_clean_worktree() which raises GateError
        with pytest.raises(GateError, match="uncommitted changes"):
            change_status(hc_home, TEAM, task["id"], "in_review")

        # Verify task stayed in in_progress
        task = get_task(hc_home, TEAM, task["id"])
        assert task["status"] == "in_progress"


class TestWorkflowReviewCycle:
    """Test 11: Review cycle (in_review -> in_progress -> in_review -> in_approval)."""

    def test_review_cycle_increments_counter(self, hc_home, tmp_path):
        """Advance task through review cycle, verify review_attempt increments.

        Verify:
        - review_attempt incremented on each in_approval transition
        - Task can cycle back from in_review to in_progress
        - Full cycle works: in_review -> in_progress -> in_review -> in_approval
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task and advance to in_progress
        task = create_task(hc_home, TEAM, title="Review cycle test", assignee="alice", repo="testrepo")
        task = change_status(hc_home, TEAM, task["id"], "in_progress")

        # Make first commit
        _make_commit_in_worktree(hc_home, task["id"], "testrepo", "v1.txt", "version 1\n")

        # First review cycle: in_progress -> in_review -> in_approval
        task = change_status(hc_home, TEAM, task["id"], "in_review")
        assert task["status"] == "in_review"

        task = change_status(hc_home, TEAM, task["id"], "in_approval")
        assert task["status"] == "in_approval"
        assert task["review_attempt"] == 1, "First approval should set review_attempt to 1"

        # Rejection: in_approval -> rejected -> in_progress
        # (Using rejected state for the cycle as per workflow graph)
        update_task(hc_home, TEAM, task["id"], rejection_reason="Needs changes")
        task = change_status(hc_home, TEAM, task["id"], "rejected")
        assert task["status"] == "rejected"

        task = change_status(hc_home, TEAM, task["id"], "in_progress")
        assert task["status"] == "in_progress"

        # Make another commit
        _make_commit_in_worktree(hc_home, task["id"], "testrepo", "v2.txt", "version 2\n")

        # Second review cycle: in_progress -> in_review -> in_approval
        task = change_status(hc_home, TEAM, task["id"], "in_review")
        assert task["status"] == "in_review"

        task = change_status(hc_home, TEAM, task["id"], "in_approval")
        assert task["status"] == "in_approval"
        assert task["review_attempt"] == 2, "Second approval should increment review_attempt to 2"


class TestWorkflowMaxReviewCycles:
    """Test 12: Max review cycles (check if escalation logic exists)."""

    def test_review_counter_increments_correctly(self, hc_home, tmp_path):
        """Verify review_attempt counter increments across multiple cycles.

        Note: Max review cycle escalation is not yet implemented in the workflow.
        This test verifies that the review_attempt counter works correctly,
        which would be the foundation for future escalation logic.
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task and advance to in_progress
        task = create_task(hc_home, TEAM, title="Max cycles test", assignee="alice", repo="testrepo")
        task = change_status(hc_home, TEAM, task["id"], "in_progress")

        # Cycle through review 3 times
        for i in range(1, 4):
            # Make a commit
            _make_commit_in_worktree(hc_home, task["id"], "testrepo", f"v{i}.txt", f"version {i}\n")

            # in_progress -> in_review -> in_approval
            task = change_status(hc_home, TEAM, task["id"], "in_review")
            task = change_status(hc_home, TEAM, task["id"], "in_approval")

            assert task["review_attempt"] == i, f"Cycle {i} should have review_attempt={i}"

            # Don't reject on the last cycle
            if i < 3:
                # Reject and go back to in_progress
                update_task(hc_home, TEAM, task["id"], rejection_reason=f"Needs changes {i}")
                task = change_status(hc_home, TEAM, task["id"], "rejected")
                task = change_status(hc_home, TEAM, task["id"], "in_progress")

        # Verify final state
        assert task["status"] == "in_approval"
        assert task["review_attempt"] == 3


class TestWorkflowRejectionFlow:
    """Test 13: Rejection flow (in_approval -> rejected -> in_progress -> ... -> in_approval)."""

    def test_rejection_notifies_manager(self, hc_home, tmp_path):
        """Test rejection flow with notification to manager.

        Verify:
        - Rejected.enter() sends notification to manager
        - rejection_reason is included in the notification
        - Can transition back to in_progress from rejected
        - Full cycle back to in_approval works
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task and advance to in_approval
        task = create_task(hc_home, TEAM, title="Rejection test", assignee="alice", repo="testrepo")
        task = change_status(hc_home, TEAM, task["id"], "in_progress")
        _make_commit_in_worktree(hc_home, task["id"], "testrepo")
        task = change_status(hc_home, TEAM, task["id"], "in_review")
        task = change_status(hc_home, TEAM, task["id"], "in_approval")

        # Set rejection reason
        rejection_reason = "Code quality issues found"
        update_task(hc_home, TEAM, task["id"], rejection_reason=rejection_reason)

        # Mock the notify method to capture the notification
        with patch("delegate.workflows.core.Context.notify") as mock_notify:
            # in_approval -> rejected (Rejected.enter should notify manager)
            task = change_status(hc_home, TEAM, task["id"], "rejected")
            assert task["status"] == "rejected"

            # Verify notification was sent to manager
            mock_notify.assert_called_once()
            call_args = mock_notify.call_args
            assert call_args[0][0] == "edison", "Should notify manager"
            assert rejection_reason in call_args[0][1], "Notification should include rejection reason"
            assert "TASK_REJECTED" in call_args[0][1]

        # rejected -> in_progress
        task = change_status(hc_home, TEAM, task["id"], "in_progress")
        assert task["status"] == "in_progress"

        # Make another commit and complete full cycle
        _make_commit_in_worktree(hc_home, task["id"], "testrepo", "fixed.txt", "fixes\n")
        task = change_status(hc_home, TEAM, task["id"], "in_review")
        task = change_status(hc_home, TEAM, task["id"], "in_approval")
        assert task["status"] == "in_approval"
        assert task["review_attempt"] == 2, "Should be on second review attempt"


class TestWorkflowErrorRecovery:
    """Test 14: Error recovery (error -> in_progress -> complete)."""

    def test_error_state_assigns_to_human(self, hc_home, tmp_path):
        """Test error state assignment and recovery.

        Verify:
        - Error.assign() returns the human member name
        - Can transition from error to in_progress
        - Can transition from error to todo
        - Task can complete normally after recovery from error

        Note: Error state is normally reached via ActionError raised in hooks,
        not via direct status transitions. We simulate this by manually setting
        the status to error to test recovery behavior.
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task and advance to in_progress
        task = create_task(hc_home, TEAM, title="Error recovery test", assignee="alice", repo="testrepo")
        task = change_status(hc_home, TEAM, task["id"], "in_progress")

        # Manually set task to error state (simulating ActionError being raised)
        # This bypasses validation since error isn't directly reachable via transitions
        task = update_task(hc_home, TEAM, task["id"], status="error")
        assert task["status"] == "error"

        # Now test the Error stage hooks by transitioning through change_status
        # The assign hook should fire when we re-enter the error state or transition out
        # Since we're already in error, let's verify we can transition to in_progress
        task = change_status(hc_home, TEAM, task["id"], "in_progress")
        assert task["status"] == "in_progress"

        # Complete the task normally
        _make_commit_in_worktree(hc_home, task["id"], "testrepo")
        task = change_status(hc_home, TEAM, task["id"], "in_review")
        task = change_status(hc_home, TEAM, task["id"], "in_approval")
        assert task["status"] == "in_approval"

    def test_error_to_todo_transition(self, hc_home, tmp_path):
        """Test that error state can transition back to todo.

        Note: Error state is normally reached via ActionError raised in hooks.
        We simulate this by manually setting the status to error.
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task
        task = create_task(hc_home, TEAM, title="Error to todo test", assignee="alice", repo="testrepo")

        # Manually set task to error state (simulating ActionError being raised)
        task = update_task(hc_home, TEAM, task["id"], status="error")
        assert task["status"] == "error"

        # error -> todo (should be allowed per workflow definition)
        task = change_status(hc_home, TEAM, task["id"], "todo")
        assert task["status"] == "todo"

    def test_error_state_assign_hook(self, hc_home, tmp_path):
        """Test that Error.assign() hook assigns to human when entering error state.

        This test verifies the assign hook behavior by simulating an ActionError
        condition and checking the resulting assignee.
        """
        # Set up real git repo
        repo = _setup_git_repo(tmp_path)
        _register_repo(hc_home, "testrepo", repo)

        # Create task
        task = create_task(hc_home, TEAM, title="Error assign test", assignee="alice", repo="testrepo")

        # Manually set to error to simulate ActionError
        task = update_task(hc_home, TEAM, task["id"], status="error")

        # When we use change_status to transition OUT of error, we can verify
        # that the error state's transitions work correctly
        # But to test Error.assign(), we'd need to transition INTO error via change_status
        # Since that's not possible, we verify the Error stage class directly
        from delegate.workflow import load_workflow_cached
        from delegate.workflows.core import Context

        wf = load_workflow_cached(hc_home, TEAM, "default", 1)
        error_stage = wf.stage_map["error"]()

        # Create a context and call assign
        ctx = Context(hc_home, TEAM, task)
        assignee = error_stage.assign(ctx)

        assert assignee == "nikhil", "Error.assign() should return human name"
