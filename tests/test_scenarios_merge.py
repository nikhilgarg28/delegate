"""Tests for merge worker scenarios from tests/scenarios.md (Tier 1).

Tests 1-5, 7-8: End-to-end merge worker scenarios without LLM agents.
Uses real git repos (tmp_path), mocks only _run_pre_merge to skip actual test execution.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from delegate.task import (
    create_task,
    change_status,
    update_task,
    get_task,
    cancel_task,
    format_task_id,
)
from delegate.config import add_repo, set_boss
from delegate.merge import merge_task, MergeResult, MergeFailureReason
from delegate.repo import create_task_worktree
from delegate.bootstrap import bootstrap
from delegate.paths import team_dir as _team_dir


TEAM = "myteam"


@pytest.fixture
def hc_home(tmp_path):
    """Create a fully bootstrapped delegate home directory."""
    hc = tmp_path / "hc_home"
    hc.mkdir()
    set_boss(hc, "nikhil")
    bootstrap(hc, TEAM, manager="edison", agents=["alice", "bob"])
    return hc


def _setup_git_repo(tmp_path: Path) -> Path:
    """Set up a local git repo with a main branch and initial commit.

    Returns the repo path.
    """
    repo = tmp_path / "source_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "README.md").write_text("# Test repo\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo), capture_output=True)
    return repo


def _make_feature_branch(repo: Path, branch: str, filename: str = "feature.py", content: str = "# New\n"):
    """Create a feature branch with a single commit."""
    subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo), capture_output=True, check=True)
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", f"Add {filename}"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)


def _register_repo_with_symlink(hc_home: Path, name: str, source_repo: Path):
    """Register a repo by creating a symlink in hc_home/teams/<team>/repos/."""
    from delegate.paths import repos_dir
    rd = repos_dir(hc_home, TEAM)
    rd.mkdir(parents=True, exist_ok=True)
    link = rd / name
    if not link.exists():
        link.symlink_to(source_repo)
    add_repo(hc_home, TEAM, name, str(source_repo), approval="auto")


def _make_in_approval_task(hc_home, title="Task", repo="myrepo", branch="feature/test", merging=False, assignee="manager"):
    """Helper: create a task and advance it to in_approval (or optionally merging) status.

    Args:
        merging: If True, advance to merging state (for direct merge_task calls).
                 If False, stop at in_approval (for merge_once tests).
        assignee: The assignee/DRI for the task (default: "manager").
    """
    task = create_task(hc_home, TEAM, title=title, assignee=assignee)
    update_task(hc_home, TEAM, task["id"], repo=repo, branch=branch)
    change_status(hc_home, TEAM, task["id"], "in_progress")
    change_status(hc_home, TEAM, task["id"], "in_review")
    change_status(hc_home, TEAM, task["id"], "in_approval")
    if merging:
        change_status(hc_home, TEAM, task["id"], "merging")
    return get_task(hc_home, TEAM, task["id"])


class TestMergeScenarios:
    """Merge worker scenarios from tests/scenarios.md (Tier 1: Tests 1-5, 7-8)."""

    @patch("delegate.merge._run_pre_merge")
    def test_1_happy_path_end_to_end(self, mock_run_pre_merge, hc_home, tmp_path):
        """Test 1: Happy path (end-to-end).

        Create task -> set up real git repo + feature branch with commits ->
        register repo -> advance task to merging -> call merge_task() -> verify:
        - main branch moved forward (has the feature commit)
        - Agent worktree cleaned up (removed)
        - Feature branch deleted
        - Task status is 'done'
        """
        # Mock pre-merge to always pass
        mock_run_pre_merge.return_value = (True, "Tests passed")

        # Set up repo and feature branch
        repo = _setup_git_repo(tmp_path)
        branch = "delegate/abc123/myteam/T0001"
        _make_feature_branch(repo, branch, filename="new_feature.py", content="# feature\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create worktree (simulating agent work)
        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        wt_path = _team_dir(hc_home, TEAM) / "worktrees" / "myrepo" / format_task_id(task["id"])
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(repo), capture_output=True, check=True,
        )

        # Verify worktree exists before merge
        assert wt_path.exists(), "Worktree should exist before merge"

        # Execute merge
        result = merge_task(hc_home, TEAM, task["id"])

        # Verify success
        assert result.success is True, f"Merge failed: {result.message}"

        # Verify main moved forward
        log_result = subprocess.run(
            ["git", "log", "--oneline", "main"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert "Add new_feature.py" in log_result.stdout, "Feature commit should be in main"

        # Verify agent worktree cleaned up
        assert not wt_path.exists(), "Agent worktree should be removed after merge"

        # Verify feature branch deleted
        branch_result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch not in branch_result.stdout, "Feature branch should be deleted"

        # Verify task status is done
        updated = get_task(hc_home, TEAM, task["id"])
        assert updated["status"] == "done", "Task status should be 'done'"

    @patch("delegate.merge._run_pre_merge")
    def test_2_two_tasks_merging_sequentially(self, mock_run_pre_merge, hc_home, tmp_path):
        """Test 2: Two tasks merging sequentially.

        Create T001 and T002 on same repo, each with different feature branches/files.
        Merge T001 first -> verify main has T001 changes.
        Then merge T002 -> T002 must rebase onto new main -> verify main has both
        sets of commits with linear history (no merge commits).
        """
        mock_run_pre_merge.return_value = (True, "Tests passed")

        # Set up repo
        repo = _setup_git_repo(tmp_path)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create T001 with feature1
        branch1 = "delegate/abc123/myteam/T0001"
        _make_feature_branch(repo, branch1, filename="feature1.py", content="# feature 1\n")
        task1 = _make_in_approval_task(hc_home, title="Task 1", repo="myrepo", branch=branch1, merging=True)

        # Create T002 with feature2
        branch2 = "delegate/abc123/myteam/T0002"
        _make_feature_branch(repo, branch2, filename="feature2.py", content="# feature 2\n")
        task2 = _make_in_approval_task(hc_home, title="Task 2", repo="myrepo", branch=branch2, merging=True)

        # Merge T001
        result1 = merge_task(hc_home, TEAM, task1["id"])
        assert result1.success is True, f"T001 merge failed: {result1.message}"

        # Verify main has feature1
        assert (repo / "feature1.py").exists(), "feature1.py should be in main after T001 merge"

        # Merge T002 (must rebase onto new main)
        result2 = merge_task(hc_home, TEAM, task2["id"])
        assert result2.success is True, f"T002 merge failed: {result2.message}"

        # Verify main has both features
        assert (repo / "feature1.py").exists(), "feature1.py should still be in main"
        assert (repo / "feature2.py").exists(), "feature2.py should be in main after T002 merge"

        # Verify linear history (no merge commits)
        log_result = subprocess.run(
            ["git", "log", "--oneline", "--graph", "main"],
            cwd=str(repo), capture_output=True, text=True,
        )
        # Linear history means no lines starting with "* \" or "* /"
        assert "*   " not in log_result.stdout, "History should be linear (no merge commits)"
        assert "Add feature1.py" in log_result.stdout
        assert "Add feature2.py" in log_result.stdout

    @patch("delegate.merge._run_pre_merge")
    def test_3_merge_conflict(self, mock_run_pre_merge, hc_home, tmp_path):
        """Test 3: Merge conflict.

        Create T001 with feature branch modifying file X. While T001 is pending,
        commit directly to main modifying the SAME lines in file X.
        Call merge_task() -> verify:
        - Returns MergeResult with success=False, reason=SQUASH_CONFLICT
        - Main is untouched (same commit as before merge attempt)
        - Task branch is intact (not deleted)
        - Temp worktree cleaned up
        """
        mock_run_pre_merge.return_value = (True, "Tests passed")

        # Set up repo
        repo = _setup_git_repo(tmp_path)

        # Create feature branch modifying conflict.txt
        branch = "delegate/abc123/myteam/T0001"
        _make_feature_branch(repo, branch, filename="conflict.txt", content="feature version\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Modify same file on main with conflicting content
        (repo / "conflict.txt").write_text("main version\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Conflicting change on main"], cwd=str(repo), capture_output=True, check=True)

        # Record main commit before merge attempt
        pre_merge_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()

        # Create task and attempt merge
        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, TEAM, task["id"])

        # Verify merge failed — rebase fails, squash-reapply also fails on true content conflict
        assert result.success is False, "Merge should fail on conflict"
        assert result.reason == MergeFailureReason.SQUASH_CONFLICT, f"Expected SQUASH_CONFLICT, got {result.reason}"

        # Verify main is untouched
        post_merge_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert post_merge_sha == pre_merge_sha, "Main should be unchanged after conflict"

        # Verify task branch is intact
        branch_result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch in branch_result.stdout, "Feature branch should remain after conflict"

        # Verify temp worktree cleaned up (no _merge directories)
        merge_wt_dir = _team_dir(hc_home, TEAM) / "worktrees" / "_merge"
        if merge_wt_dir.exists():
            remaining = list(merge_wt_dir.rglob("*"))
            assert len(remaining) == 0, f"Temp worktree should be cleaned up, found: {remaining}"

    @patch("delegate.merge._run_pre_merge")
    def test_4_dirty_main(self, mock_run_pre_merge, hc_home, tmp_path):
        """Test 4: Dirty main.

        Create a task ready for merge. Make uncommitted changes in the main repo
        working directory (modify a tracked file without committing).
        Call merge_task() -> verify:
        - Returns MergeResult with success=False, reason=DIRTY_MAIN
        - reason.retryable is True
        - User uncommitted changes are preserved exactly
        """
        mock_run_pre_merge.return_value = (True, "Tests passed")

        # Set up repo and feature branch
        repo = _setup_git_repo(tmp_path)
        branch = "delegate/abc123/myteam/T0001"
        _make_feature_branch(repo, branch, filename="feature.py", content="# feature\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # User is on main (default after setup) with uncommitted changes
        dirty_content = "uncommitted work\n"
        (repo / "dirty_file.txt").write_text(dirty_content)

        # Create task and attempt merge
        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, TEAM, task["id"])

        # Verify merge failed with DIRTY_MAIN
        assert result.success is False, "Merge should fail with dirty main"
        assert result.reason == MergeFailureReason.DIRTY_MAIN, f"Expected DIRTY_MAIN, got {result.reason}"

        # Verify reason is retryable
        assert result.reason.retryable is True, "DIRTY_MAIN should be retryable"

        # Verify user's uncommitted changes are preserved
        assert (repo / "dirty_file.txt").exists(), "Dirty file should be preserved"
        assert (repo / "dirty_file.txt").read_text() == dirty_content, "Dirty file content should be unchanged"

    def test_5_concurrent_agents_separate_worktrees(self, hc_home, tmp_path):
        """Test 5: Concurrent agents (separate worktrees).

        Create two tasks (T001, T002) in same repo. Create worktrees for both using
        create_task_worktree(). Verify:
        - Both worktrees exist at different paths
        - Both have independent branches
        - Can make commits in one worktree without affecting the other
        - No git lock errors
        """
        # Set up repo
        repo = _setup_git_repo(tmp_path)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create two tasks
        task1 = create_task(hc_home, TEAM, title="Task 1", assignee="alice")
        update_task(hc_home, TEAM, task1["id"], repo="myrepo")
        change_status(hc_home, TEAM, task1["id"], "in_progress")

        task2 = create_task(hc_home, TEAM, title="Task 2", assignee="bob")
        update_task(hc_home, TEAM, task2["id"], repo="myrepo")
        change_status(hc_home, TEAM, task2["id"], "in_progress")

        # Create worktrees for both tasks
        wt1 = create_task_worktree(hc_home, TEAM, "myrepo", task1["id"])
        wt2 = create_task_worktree(hc_home, TEAM, "myrepo", task2["id"])

        # Verify both exist at different paths
        assert wt1.exists(), f"Worktree 1 should exist at {wt1}"
        assert wt2.exists(), f"Worktree 2 should exist at {wt2}"
        assert wt1 != wt2, "Worktrees should have different paths"

        # Verify independent branches
        branch1_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(wt1), capture_output=True, text=True,
        )
        branch2_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(wt2), capture_output=True, text=True,
        )
        branch1 = branch1_result.stdout.strip()
        branch2 = branch2_result.stdout.strip()
        assert branch1 != branch2, "Each worktree should have its own branch"
        assert "MYTE-0001" in branch1 or "T0001" in branch1
        assert "MYTE-0002" in branch2 or "T0002" in branch2

        # Make commits in worktree 1 without affecting worktree 2
        (wt1 / "file1.py").write_text("# from wt1\n")
        subprocess.run(["git", "add", "."], cwd=str(wt1), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit in wt1"], cwd=str(wt1), capture_output=True, check=True)

        # Verify wt2 is unaffected
        assert not (wt2 / "file1.py").exists(), "Changes in wt1 should not appear in wt2"

        # Make commits in worktree 2
        (wt2 / "file2.py").write_text("# from wt2\n")
        subprocess.run(["git", "add", "."], cwd=str(wt2), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit in wt2"], cwd=str(wt2), capture_output=True, check=True)

        # Verify no lock errors occurred (both commits succeeded)
        assert (wt1 / "file1.py").exists()
        assert (wt2 / "file2.py").exists()

    def test_7_cancel_mid_work_cleanup(self, hc_home, tmp_path):
        """Test 7: Cancel mid-work (cleanup).

        Create task -> create worktree -> make some commits on the branch.
        Cancel the task via cancel_task(). Verify:
        - Worktree directory removed
        - Feature branch deleted (use git branch --list)
        - Task status is cancelled
        """
        # Set up repo
        repo = _setup_git_repo(tmp_path)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task and worktree
        task = create_task(hc_home, TEAM, title="Task to cancel", assignee="alice")
        change_status(hc_home, TEAM, task["id"], "in_progress")

        # Set repo before creating worktree
        update_task(hc_home, TEAM, task["id"], repo="myrepo")
        wt_path = create_task_worktree(hc_home, TEAM, "myrepo", task["id"])

        # Get branch name from the created worktree
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(wt_path), capture_output=True, text=True,
        )
        branch = branch_result.stdout.strip()

        # Update task with branch info (cancel_task needs this)
        update_task(hc_home, TEAM, task["id"], branch=branch)

        # Make some commits
        (wt_path / "work.py").write_text("# work in progress\n")
        subprocess.run(["git", "add", "."], cwd=str(wt_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Work in progress"], cwd=str(wt_path), capture_output=True, check=True)

        # Verify worktree and branch exist before cancel
        assert wt_path.exists(), "Worktree should exist before cancel"
        branch_check_before = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch in branch_check_before.stdout, "Branch should exist before cancel"

        # Cancel the task
        updated = cancel_task(hc_home, TEAM, task["id"])

        # Verify task status is cancelled
        assert updated["status"] == "cancelled", "Task status should be 'cancelled'"

        # Verify worktree removed
        assert not wt_path.exists(), "Worktree should be removed after cancel"

        # Verify feature branch deleted
        branch_check_after = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch not in branch_check_after.stdout, "Feature branch should be deleted after cancel"

    @patch("delegate.merge._run_pre_merge")
    def test_8_multi_repo_task(self, mock_run_pre_merge, hc_home, tmp_path):
        """Test 8: Multi-repo task.

        Create a task with TWO repos registered. Set up feature branches in both.
        Merge the task. Test the happy path: both repos merge successfully,
        main moves forward in both.

        Note: Current merge.py implementation merges repos sequentially without
        rollback. This test verifies the happy path behavior.
        """
        mock_run_pre_merge.return_value = (True, "Tests passed")

        # Set up two repos (create parent dirs first)
        (tmp_path / "repo1").mkdir(exist_ok=True)
        (tmp_path / "repo2").mkdir(exist_ok=True)
        repo1 = _setup_git_repo(tmp_path / "repo1")
        repo2 = _setup_git_repo(tmp_path / "repo2")
        _register_repo_with_symlink(hc_home, "repo1", repo1)
        _register_repo_with_symlink(hc_home, "repo2", repo2)

        # Create feature branches in both repos
        branch = "delegate/abc123/myteam/T0001"
        _make_feature_branch(repo1, branch, filename="feature1.py", content="# feature in repo1\n")
        _make_feature_branch(repo2, branch, filename="feature2.py", content="# feature in repo2\n")

        # Create task with both repos
        task = create_task(hc_home, TEAM, title="Multi-repo task", assignee="alice")
        update_task(hc_home, TEAM, task["id"], repo=["repo1", "repo2"], branch=branch)
        change_status(hc_home, TEAM, task["id"], "in_progress")
        change_status(hc_home, TEAM, task["id"], "in_review")
        change_status(hc_home, TEAM, task["id"], "in_approval")
        change_status(hc_home, TEAM, task["id"], "merging")

        # Record main SHAs before merge
        main1_before = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo1), capture_output=True, text=True,
        ).stdout.strip()
        main2_before = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo2), capture_output=True, text=True,
        ).stdout.strip()

        # Merge the task
        result = merge_task(hc_home, TEAM, task["id"])

        # Verify merge succeeded
        assert result.success is True, f"Multi-repo merge failed: {result.message}"

        # Verify main moved forward in both repos
        main1_after = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo1), capture_output=True, text=True,
        ).stdout.strip()
        main2_after = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo2), capture_output=True, text=True,
        ).stdout.strip()

        assert main1_after != main1_before, "repo1 main should have moved forward"
        assert main2_after != main2_before, "repo2 main should have moved forward"

        # Verify feature files are in both repos
        log1 = subprocess.run(
            ["git", "log", "--oneline", "main"],
            cwd=str(repo1), capture_output=True, text=True,
        )
        log2 = subprocess.run(
            ["git", "log", "--oneline", "main"],
            cwd=str(repo2), capture_output=True, text=True,
        )
        assert "Add feature1.py" in log1.stdout, "feature1 commit should be in repo1 main"
        assert "Add feature2.py" in log2.stdout, "feature2 commit should be in repo2 main"

        # Verify task is done
        updated = get_task(hc_home, TEAM, task["id"])
        assert updated["status"] == "done", "Task should be done after successful multi-repo merge"
