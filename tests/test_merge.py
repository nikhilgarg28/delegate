"""Tests for delegate/merge.py — merge worker logic.

Tests the worktree-based merge flow:
    1. Create disposable worktree + temp branch from feature branch
    2. Rebase temp branch onto main (inside temp worktree)
    3. Run tests (inside temp worktree)
    4. Fast-forward merge via update-ref (ref-only, no checkout)
    5. Clean up temp worktree/branch + feature branch + agent worktree

Key invariants verified:
    - Main repo working directory is never touched
    - Feature branch and agent worktree are never modified during merge
    - Only on success are feature branch and agent worktree cleaned up
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from delegate.task import (
    create_task,
    change_status,
    update_task,
    get_task,
)
from delegate.config import (
    add_repo, get_merge_policy, get_repo_approval, get_repo_test_cmd, update_repo_test_cmd, set_boss,
)
from delegate.merge import merge_task, merge_once, _run_pre_merge, _other_unmerged_tasks_on_branch, _sort_merge_candidates, MergeResult, MergeFailureReason
from delegate.bootstrap import bootstrap
from delegate.paths import team_dir as _team_dir


SAMPLE_TEAM = "myteam"


@pytest.fixture
def hc_home(tmp_path):
    """Create a fully bootstrapped delegate home directory."""
    hc = tmp_path / "hc_home"
    hc.mkdir()
    set_boss(hc, "nikhil")
    bootstrap(hc, SAMPLE_TEAM, manager="edison", agents=["alice", "bob", ("sarah", "qa")])
    return hc


def _make_in_approval_task(hc_home, title="Task", repo="myrepo", branch="feature/test", merging=False, assignee="manager"):
    """Helper: create a task and advance it to in_approval (or optionally merging) status.

    Args:
        merging: If True, advance to merging state (for direct merge_task calls).
                 If False, stop at in_approval (for merge_once tests).
        assignee: The assignee/DRI for the task (default: "manager").
    """
    task = create_task(hc_home, SAMPLE_TEAM, title=title, assignee=assignee)
    update_task(hc_home, SAMPLE_TEAM, task["id"], repo=repo, branch=branch)
    change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")
    change_status(hc_home, SAMPLE_TEAM, task["id"], "in_review")
    change_status(hc_home, SAMPLE_TEAM, task["id"], "in_approval")
    if merging:
        change_status(hc_home, SAMPLE_TEAM, task["id"], "merging")
    return get_task(hc_home, SAMPLE_TEAM, task["id"])


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


def _make_feature_branch_with_failing_premerge(repo: Path, branch: str) -> None:
    """Create a feature branch that includes a failing .delegate/premerge.sh.

    Since the merge worker runs premerge.sh from the merge worktree (which is
    created from the feature branch), the failing script must be committed to
    the feature branch itself — not placed in the agent worktree after the fact.
    """
    subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo), capture_output=True, check=True)
    (repo / "feature.py").write_text("# New\n")
    delegate_dir = repo / ".delegate"
    delegate_dir.mkdir(exist_ok=True)
    (delegate_dir / "premerge.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add feature with failing premerge"],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)


def _register_repo_with_symlink(hc_home: Path, name: str, source_repo: Path):
    """Register a repo by creating a symlink in hc_home/teams/<team>/repos/."""
    from delegate.paths import repos_dir
    rd = repos_dir(hc_home, SAMPLE_TEAM)
    rd.mkdir(parents=True, exist_ok=True)
    link = rd / name
    if not link.exists():
        link.symlink_to(source_repo)
    add_repo(hc_home, SAMPLE_TEAM, name, str(source_repo), approval="auto")


def _create_agent_worktree_for_task(
    hc_home: Path, repo: Path, repo_name: str, branch: str, task_id: int
) -> Path:
    """Create a git worktree for a task at the standard path.

    Simulates what _ensure_task_infra does in the daemon loop so that
    merge tests can run pre-merge scripts in the agent worktree.
    """
    from delegate.paths import task_worktree_dir
    wt_path = task_worktree_dir(hc_home, SAMPLE_TEAM, repo_name, task_id)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), branch],
        cwd=str(repo), capture_output=True, check=True,
    )
    return wt_path


# ---------------------------------------------------------------------------
# merge_task tests (with real git)
# ---------------------------------------------------------------------------

class TestMergeTask:
    def test_successful_merge(self, hc_home, tmp_path):
        """Full merge: rebase, skip-tests, ff-merge."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001", merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True
        assert "success" in result.message.lower()

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"

        # Feature should be on main (check via rev-parse to avoid checkout)
        log = subprocess.run(
            ["git", "log", "--oneline", "main"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert "Add feature.py" in log.stdout

    def test_zero_commit_merge(self, hc_home, tmp_path):
        """Zero-commit branch (e.g., spec-only task) should merge successfully as no-op."""
        repo = _setup_git_repo(tmp_path)

        # Create a branch but don't add any commits to it
        subprocess.run(["git", "checkout", "-b", "alice/T0001"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001", merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        # Get main tip before merge
        main_before = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"

        # Main should be unchanged (no-op merge)
        main_after = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert main_after == main_before

    def test_rebase_conflict(self, hc_home, tmp_path):
        """True content conflict → rebase fails, squash-reapply also fails → SQUASH_CONFLICT."""
        repo = _setup_git_repo(tmp_path)

        # Create feature branch that modifies file.txt
        _make_feature_branch(repo, "alice/T0001", filename="file.txt", content="feature version\n")

        # Now modify same file on main
        (repo / "file.txt").write_text("main version\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Diverge main"], cwd=str(repo), capture_output=True, check=True)

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001", merging=True)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)

        assert result.success is False
        # Rebase fails, then squash-reapply also fails on true content conflict
        assert result.reason == MergeFailureReason.SQUASH_CONFLICT
        assert "conflict" in result.message.lower()

    def test_missing_branch(self, hc_home):
        """Task with no branch should fail."""
        task = create_task(hc_home, SAMPLE_TEAM, title="No branch", assignee="manager")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_review")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_approval")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "merging")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])
        assert result.success is False
        assert "no branch" in result.message.lower() or "not found" in result.message.lower()

    def test_missing_repo(self, hc_home):
        """Task with no repo should fail."""
        task = create_task(hc_home, SAMPLE_TEAM, title="No repo", assignee="manager")
        update_task(hc_home, SAMPLE_TEAM, task["id"], branch="some/branch")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_review")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_approval")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "merging")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])
        assert result.success is False
        assert "no repo" in result.message.lower()

    def test_main_repo_untouched_when_user_on_other_branch(self, hc_home, tmp_path):
        """When the user is on a non-main branch, the working directory is untouched."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Switch user to a different branch so update-ref path is used
        subprocess.run(
            ["git", "checkout", "-b", "user/work"],
            cwd=str(repo), capture_output=True, check=True,
        )

        # Add a dirty file to the main repo
        (repo / "dirty_file.txt").write_text("user's uncommitted work\n")

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"

        # Main repo should still be on user/work
        post_head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert post_head == "user/work", "Merge worker changed the checked-out branch"

        # Dirty file should still be there
        assert (repo / "dirty_file.txt").exists(), "Merge worker disturbed main repo working directory"
        assert (repo / "dirty_file.txt").read_text() == "user's uncommitted work\n"

    def test_dirty_main_checkout_blocks_merge(self, hc_home, tmp_path):
        """When user has main checked out with uncommitted changes, merge fails."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # User is on main (default after setup) — add dirty file
        (repo / "dirty_file.txt").write_text("user's uncommitted work\n")

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)

        assert result.success is False
        assert result.reason == MergeFailureReason.DIRTY_MAIN
        assert "uncommitted" in result.message.lower()

        # Dirty file should be preserved
        assert (repo / "dirty_file.txt").exists()
        assert (repo / "dirty_file.txt").read_text() == "user's uncommitted work\n"

    def test_clean_main_checkout_updates_working_tree(self, hc_home, tmp_path):
        """When user has main checked out cleanly, merge --ff-only updates the working tree."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch, filename="new_feature.py", content="# feature\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # User is on main (default after setup) and repo is clean
        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"

        # Working tree should have the merged file (ff-only updates it)
        assert (repo / "new_feature.py").exists(), "Working tree not updated after ff-only merge"
        assert (repo / "new_feature.py").read_text() == "# feature\n"

        # User should still be on main
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert head == "main"

    def test_other_branch_checkout_uses_ref_only(self, hc_home, tmp_path):
        """When user is on a different branch, update-ref advances main
        without checking out main or running merge --ff-only."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch, filename="new_feature.py", content="# feature\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Switch user to a different branch
        subprocess.run(
            ["git", "checkout", "-b", "user/work"],
            cwd=str(repo), capture_output=True, check=True,
        )

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"

        # User should still be on user/work (merge worker never checked out main)
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert head == "user/work", f"Merge changed checked-out branch to {head}"

        # Main ref should point to the merged commit
        show = subprocess.run(
            ["git", "show", "main:new_feature.py"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert show.returncode == 0, "main ref should include the merged feature"

    def test_feature_branch_intact_on_test_failure(self, hc_home, tmp_path):
        """On pre-merge test failure, the feature branch must still exist and be valid.

        The merge worker runs premerge.sh inside the merge worktree (not the agent
        worktree), so the failing script must be committed to the feature branch.
        The feature branch must survive the failed merge attempt.
        """
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        # Commit a failing premerge.sh to the feature branch so the merge worktree
        # inherits it (merge worker runs premerge.sh in its own worktree, not the agent's).
        _make_feature_branch_with_failing_premerge(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Advance main to create a rebase scenario
        (repo / "extra.txt").write_text("extra\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "main moves ahead"], cwd=str(repo), capture_output=True, check=True)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False
        assert result.reason == MergeFailureReason.PRE_MERGE_FAILED

        # Feature branch must still exist (agent can fix and resubmit)
        branch_check = subprocess.run(
            ["git", "branch", "--list", branch], cwd=str(repo), capture_output=True, text=True,
        )
        assert branch in branch_check.stdout, f"Feature branch '{branch}' was deleted after test failure"

    def test_agent_worktree_survives_failure(self, hc_home, tmp_path):
        """On failure, the agent's worktree should remain intact.

        The merge worker runs premerge.sh in its own merge worktree, never in the
        agent's worktree.  On pre-merge failure, the agent worktree is untouched.
        The failing script must be committed to the feature branch so the merge
        worktree inherits it.
        """
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        # Commit a failing premerge.sh to the feature branch so the merge worktree
        # inherits it — the agent worktree is never touched during merge.
        _make_feature_branch_with_failing_premerge(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task first so we know the ID, then create its worktree
        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        wt_path = _create_agent_worktree_for_task(hc_home, repo, "myrepo", branch, task["id"])
        assert wt_path.exists()

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False
        assert wt_path.exists(), "Agent worktree was removed on merge failure — should be preserved"

    def test_agent_worktree_removed_on_success(self, hc_home, tmp_path):
        """On success, the agent's worktree should be cleaned up."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task first to get the ID, then create the worktree at the right path
        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        wt_path = _create_agent_worktree_for_task(hc_home, repo, "myrepo", branch, task["id"])
        assert wt_path.exists()

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        # Agent worktree should be cleaned up after success
        assert not wt_path.exists(), "Agent worktree should be removed after successful merge"

    def test_temp_worktree_cleaned_up_on_failure(self, hc_home, tmp_path):
        """Temp merge worktree should be removed even on failure."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        # Commit a failing premerge.sh to the feature branch so the merge worktree
        # inherits it (merge worker runs premerge.sh in its own worktree).
        _make_feature_branch_with_failing_premerge(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False

        # No merge worktrees should remain
        merge_wt_dir = _team_dir(hc_home, SAMPLE_TEAM) / "worktrees" / "_merge"
        if merge_wt_dir.exists():
            remaining = list(merge_wt_dir.rglob("*"))
            assert len(remaining) == 0, f"Stale merge worktree remains: {remaining}"

    def test_temp_worktree_cleaned_up_on_success(self, hc_home, tmp_path):
        """Temp merge worktree should be removed after a successful merge.

        This guards against the bug that caused T0074's merge worktree to
        be left behind: if _remove_temp_worktree fails silently, the _merge/
        directory accumulates stale worktrees over time.
        """
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)

        assert result.success is True

        # No merge worktrees should remain under _merge/
        merge_wt_dir = _team_dir(hc_home, SAMPLE_TEAM) / "worktrees" / "_merge"
        assert not merge_wt_dir.exists(), (
            f"_merge/ directory still exists after successful merge: {merge_wt_dir}"
        )

        # Verify git also has no stale worktree entries for _merge/ branches
        worktree_list = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert "_merge" not in worktree_list.stdout, (
            f"Stale _merge worktree still registered in git:\n{worktree_list.stdout}"
        )

    def test_rebase_onto_with_base_sha(self, hc_home, tmp_path):
        """When base_sha is set on the task, rebase uses --onto to replay
        only the agent's commits (after base_sha) onto current main."""
        repo = _setup_git_repo(tmp_path)

        # Record the initial commit SHA — this will be our base_sha
        base_sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        base_sha = base_sha_result.stdout.strip()

        # Create a feature branch with one commit
        branch = "alice/T0001-onto"
        _make_feature_branch(repo, branch, filename="onto_feature.py", content="# onto\n")

        # Advance main with a non-conflicting commit (simulates main moving forward)
        (repo / "mainfile.txt").write_text("main extra\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Advance main"],
            cwd=str(repo), capture_output=True, check=True,
        )

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved", base_sha=base_sha)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge with --onto failed: {result.message}"

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"

        # Check that the feature file is in main's history
        show = subprocess.run(
            ["git", "show", "main:onto_feature.py"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert show.returncode == 0, "Agent's commit didn't land on main"

    def test_rebase_fallback_without_base_sha(self, hc_home, tmp_path):
        """When base_sha is empty/None the merge falls back to plain rebase."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001-nobase"
        _make_feature_branch(repo, branch, filename="nobase.py", content="# no base\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        # Explicitly set base_sha to empty string (simulating a task without it)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved", base_sha="")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Fallback merge failed: {result.message}"

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"

    def test_rebase_onto_excludes_reverted_commits(self, hc_home, tmp_path):
        """--onto correctly excludes commits that were reverted from main.

        Scenario:
        - main: M0 → M1 → M2 (base_sha = M2)
        - agent branch: M2 → A1
        - main is then reset to M0 (M1, M2 are reverted)
        - rebase --onto main M2 branch replays only A1 onto M0
        """
        repo = _setup_git_repo(tmp_path)

        # M0 is the initial commit. Add M1 and M2.
        (repo / "m1.txt").write_text("m1\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "M1"], cwd=str(repo), capture_output=True, check=True)

        (repo / "m2.txt").write_text("m2\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "M2"], cwd=str(repo), capture_output=True, check=True)

        # Record base_sha (M2)
        base_sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        base_sha = base_sha_result.stdout.strip()

        # Create agent branch from M2
        branch = "alice/T0001-revert"
        subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo), capture_output=True, check=True)
        (repo / "agent_work.py").write_text("# agent work\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Agent commit A1"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)

        # Reset main back to M0 (removing M1 and M2)
        m0_result = subprocess.run(
            ["git", "rev-parse", "HEAD~2"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        m0_sha = m0_result.stdout.strip()
        subprocess.run(
            ["git", "reset", "--hard", m0_sha], cwd=str(repo),
            capture_output=True, check=True,
        )

        # Verify main no longer has m1.txt or m2.txt
        assert not (repo / "m1.txt").exists()
        assert not (repo / "m2.txt").exists()

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved", base_sha=base_sha)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Rebase --onto with reverted commits failed: {result.message}"

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"

        # Agent's work should be on main (check via git show, not file existence — main CWD may be stale)
        show = subprocess.run(
            ["git", "show", "main:agent_work.py"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert show.returncode == 0, "Agent's commit should be on main"

        # M1 and M2 files should NOT be on main (they were reverted)
        show_m1 = subprocess.run(
            ["git", "show", "main:m1.txt"],
            cwd=str(repo), capture_output=True, text=True,
        )
        show_m2 = subprocess.run(
            ["git", "show", "main:m2.txt"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert show_m1.returncode != 0, "m1.txt should not be on main (reverted commit)"
        assert show_m2.returncode != 0, "m2.txt should not be on main (reverted commit)"


# ---------------------------------------------------------------------------
# merge_once tests
# ---------------------------------------------------------------------------

class TestMergeBaseAndTip:
    """Tests for merge_base and merge_tip fields."""

    def test_empty_on_task_creation(self, hc_home):
        """merge_base and merge_tip should be empty dicts on new tasks."""
        task = create_task(hc_home, SAMPLE_TEAM, title="New task", assignee="manager")
        assert task["merge_base"] == {}
        assert task["merge_tip"] == {}

    def test_set_after_successful_merge(self, hc_home, tmp_path):
        """merge_base and merge_tip should be set after a successful merge."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Record main HEAD before merge (expected merge_base)
        pre_merge = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, check=True,
        )
        expected_base = pre_merge.stdout.strip()

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        # merge_base and merge_tip are now dicts keyed by repo
        assert updated["merge_base"]["myrepo"] == expected_base
        assert updated["merge_tip"]["myrepo"] != ""
        assert updated["merge_tip"]["myrepo"] != updated["merge_base"]["myrepo"]

        # merge_tip should be the current main ref
        post_merge = _run_git_in(repo, ["rev-parse", "main"])
        assert updated["merge_tip"]["myrepo"] == post_merge

    def test_merge_base_tip_give_correct_diff(self, hc_home, tmp_path):
        """git diff merge_base..merge_tip should show exactly the merged changes."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch, filename="new_feature.py", content="# feature code\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        mb = updated["merge_base"]["myrepo"]
        mt = updated["merge_tip"]["myrepo"]
        diff_result = subprocess.run(
            ["git", "diff", f"{mb}..{mt}"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        assert "new_feature.py" in diff_result.stdout
        assert "# feature code" in diff_result.stdout

    def test_not_set_on_failed_merge(self, hc_home, tmp_path):
        """merge_base and merge_tip should remain empty on failed merges."""
        repo = _setup_git_repo(tmp_path)

        # Create a conflicting scenario
        _make_feature_branch(repo, "alice/T0001", filename="file.txt", content="feature\n")
        (repo / "file.txt").write_text("main conflict\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Conflict on main"], cwd=str(repo), capture_output=True, check=True)

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001", merging=True)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)

        assert result.success is False
        # Rebase fails, squash-reapply also fails on true content conflict
        assert result.reason == MergeFailureReason.SQUASH_CONFLICT
        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["merge_base"] == {}
        assert updated["merge_tip"] == {}


def _run_git_in(repo: Path, args: list[str]) -> str:
    """Run git in repo and return stripped stdout."""
    r = subprocess.run(["git"] + args, cwd=str(repo), capture_output=True, text=True, check=True)
    return r.stdout.strip()


class TestMergeOnce:
    def test_empty_when_no_tasks(self, hc_home):
        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_skips_task_without_repo(self, hc_home):
        """Tasks without a repo field are skipped."""
        task = create_task(hc_home, SAMPLE_TEAM, title="No repo", assignee="manager")
        update_task(hc_home, SAMPLE_TEAM, task["id"], branch="some/branch")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_review")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_approval")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_skips_review_needed_unapproved(self, hc_home):
        """review-needed tasks without approval are skipped."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/fake", merge_policy="review-needed")
        _make_in_approval_task(hc_home, title="Unapproved")
        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_no_review_merge_processes(self, hc_home, tmp_path):
        """no-review tasks should be processed without approval."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert len(results) == 1
        assert results[0].success is True

    def test_review_needed_approved_processes(self, hc_home, tmp_path):
        """review-needed tasks with an approved review should be processed."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")

        from delegate.paths import repos_dir
        from delegate.review import get_current_review, set_verdict
        rd = repos_dir(hc_home, SAMPLE_TEAM)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "myrepo").symlink_to(repo)
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", str(repo), merge_policy="review-needed")

        task = _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001")
        # Approve via the reviews table (not the deprecated approval_status field)
        # change_status to in_approval already creates a review (attempt=1)
        review = get_current_review(hc_home, SAMPLE_TEAM, task["id"])
        set_verdict(hc_home, SAMPLE_TEAM, task["id"], review["id"], "approved")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert len(results) == 1
        assert results[0].success is True

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"

    def test_processes_newly_approved_tasks_in_merging(self, hc_home, tmp_path):
        """Tasks transitioned to 'merging' by approval endpoint should be picked up."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task in merging status with merge_attempts=0 (simulating approve endpoint)
        task = _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "merging")
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        # Verify starting state: merging with attempts=0
        task_before = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert task_before["status"] == "merging"
        assert task_before.get("merge_attempts", 0) == 0

        # merge_once should pick it up and process it
        results = merge_once(hc_home, SAMPLE_TEAM)
        assert len(results) == 1
        assert results[0].success is True

        # Task should be done
        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"


# ---------------------------------------------------------------------------
# get_merge_policy tests
# ---------------------------------------------------------------------------

class TestGetMergePolicy:
    def test_returns_review_needed_by_default(self, hc_home):
        assert get_merge_policy(hc_home, SAMPLE_TEAM, "nonexistent") == "review-needed"

    def test_reads_from_config(self, hc_home):
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo", merge_policy="no-review")
        add_repo(hc_home, SAMPLE_TEAM, "other", "/tmp/other", merge_policy="review-needed")

        assert get_merge_policy(hc_home, SAMPLE_TEAM, "myrepo") == "no-review"
        assert get_merge_policy(hc_home, SAMPLE_TEAM, "other") == "review-needed"
        assert get_merge_policy(hc_home, SAMPLE_TEAM, "missing") == "review-needed"

    def test_legacy_approval_fallback(self, hc_home):
        """Legacy 'approval' key in repos.yaml is read transparently."""
        from delegate.config import _read_repos, _write_repos
        data = _read_repos(hc_home, SAMPLE_TEAM)
        data["legacy_repo"] = {"source": "/tmp/legacy", "approval": "auto"}
        _write_repos(hc_home, SAMPLE_TEAM, data)

        assert get_merge_policy(hc_home, SAMPLE_TEAM, "legacy_repo") == "no-review"

    def test_deprecated_alias_still_works(self, hc_home):
        """The deprecated get_repo_approval function still works."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo", merge_policy="no-review")
        assert get_repo_approval(hc_home, SAMPLE_TEAM, "myrepo") == "auto"


# ---------------------------------------------------------------------------
# get_repo_test_cmd / update_repo_test_cmd tests
# ---------------------------------------------------------------------------

class TestRepoTestCmd:
    def test_returns_none_by_default(self, hc_home):
        """test_cmd should be None for repos that don't configure it."""
        assert get_repo_test_cmd(hc_home, SAMPLE_TEAM, "nonexistent") is None

    def test_returns_none_when_not_set(self, hc_home):
        """Repo registered without test_cmd should return None."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo")
        assert get_repo_test_cmd(hc_home, SAMPLE_TEAM, "myrepo") is None

    def test_add_repo_with_test_cmd(self, hc_home):
        """add_repo with test_cmd stores it correctly."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo", test_cmd="/usr/bin/python -m pytest -x")
        assert get_repo_test_cmd(hc_home, SAMPLE_TEAM, "myrepo") == "/usr/bin/python -m pytest -x"

    def test_update_repo_test_cmd(self, hc_home):
        """update_repo_test_cmd sets/changes the test command for an existing repo."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo")
        assert get_repo_test_cmd(hc_home, SAMPLE_TEAM, "myrepo") is None

        update_repo_test_cmd(hc_home, SAMPLE_TEAM, "myrepo", "/path/to/venv/bin/python -m pytest -x -q")
        assert get_repo_test_cmd(hc_home, SAMPLE_TEAM, "myrepo") == "/path/to/venv/bin/python -m pytest -x -q"

    def test_update_repo_test_cmd_missing_repo(self, hc_home):
        """update_repo_test_cmd raises KeyError for unknown repo."""
        with pytest.raises(KeyError, match="not found"):
            update_repo_test_cmd(hc_home, SAMPLE_TEAM, "no_such_repo", "pytest")


# ---------------------------------------------------------------------------
# _run_pre_merge tests — .delegate/setup.sh / .delegate/premerge.sh protocol
# ---------------------------------------------------------------------------

class TestRunPreMerge:
    def _setup_worktree(self, hc_home, tmp_path, branch="alice/T0001"):
        """Create a repo, feature branch, and a worktree at that branch.
        Returns (repo, wt_path)."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        wt_path = tmp_path / "merge_wt"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(repo), capture_output=True, check=True,
        )
        return repo, wt_path

    def test_sources_both_scripts_when_present(self, hc_home, tmp_path):
        """When both scripts exist, setup is sourced first then premerge is sourced."""
        repo, wt_path = self._setup_worktree(hc_home, tmp_path)
        # setup.sh exports a variable; premerge.sh echoes it to confirm sourcing worked
        (wt_path / ".delegate").mkdir()
        (wt_path / ".delegate" / "setup.sh").write_text("export DELEGATE_TEST_VAR=sourced\n")
        (wt_path / ".delegate" / "premerge.sh").write_text("echo $DELEGATE_TEST_VAR\n")

        ok, output = _run_pre_merge(str(wt_path))
        assert ok is True
        assert "sourced" in output

    def test_skips_gracefully_when_both_scripts_missing(self, hc_home, tmp_path):
        """When neither script exists, return success with a skip message."""
        repo, wt_path = self._setup_worktree(hc_home, tmp_path)

        ok, output = _run_pre_merge(str(wt_path))
        assert ok is True
        assert "not found" in output.lower() or "skipping" in output.lower()

    def test_skips_setup_gracefully_when_only_premerge_present(self, hc_home, tmp_path):
        """Missing setup.sh is a warn-and-skip, not a failure."""
        repo, wt_path = self._setup_worktree(hc_home, tmp_path)
        (wt_path / ".delegate").mkdir()
        (wt_path / ".delegate" / "premerge.sh").write_text("echo tests-passed\n")

        ok, output = _run_pre_merge(str(wt_path))
        assert ok is True
        assert "tests-passed" in output

    def test_fails_when_premerge_script_exits_nonzero(self, hc_home, tmp_path):
        """When .delegate/premerge.sh exits non-zero, return failure with output."""
        repo, wt_path = self._setup_worktree(hc_home, tmp_path)
        (wt_path / ".delegate").mkdir()
        (wt_path / ".delegate" / "premerge.sh").write_text("echo test-failure-output\nexit 1\n")

        ok, output = _run_pre_merge(str(wt_path))
        assert ok is False
        assert "test-failure-output" in output

    def test_merge_with_premerge_script_failure(self, hc_home, tmp_path):
        """merge_task fails with PRE_MERGE_FAILED when .delegate/premerge.sh exits non-zero.

        The merge worker runs premerge.sh from the merge worktree, which is created
        from the feature branch.  The failing script must be committed to the feature
        branch so the merge worktree inherits it.
        """
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        # Commit a failing premerge.sh to the feature branch so the merge worktree
        # picks it up (merge worker never reads from the agent worktree).
        _make_feature_branch_with_failing_premerge(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False
        assert result.reason == MergeFailureReason.PRE_MERGE_FAILED

    def test_merge_with_premerge_script_success(self, hc_home, tmp_path):
        """merge_task succeeds when .delegate/premerge.sh exits zero.

        The script must be committed to the feature branch so the merge worktree
        inherits it (merge worker runs premerge.sh in its own worktree).
        """
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        # Commit a passing premerge.sh to the feature branch.
        subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo), capture_output=True, check=True)
        (repo / "feature.py").write_text("# New\n")
        (repo / ".delegate").mkdir(exist_ok=True)
        (repo / ".delegate" / "premerge.sh").write_text("#!/usr/bin/env bash\necho all-checks-pass\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add feature with passing premerge"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])
        assert result.success is True

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"


# ---------------------------------------------------------------------------
# Shared-branch safety tests (T0053)
# ---------------------------------------------------------------------------

class TestSharedBranchCleanup:
    """When multiple tasks share a branch, cleanup should only happen once
    the last task on that branch is merged."""

    def test_other_unmerged_tasks_on_branch_helper(self, hc_home):
        """_other_unmerged_tasks_on_branch returns True when another task
        with the same branch is not yet merged."""
        t1 = create_task(hc_home, SAMPLE_TEAM, title="Task 1", assignee="manager")
        t2 = create_task(hc_home, SAMPLE_TEAM, title="Task 2", assignee="manager")
        update_task(hc_home, SAMPLE_TEAM, t1["id"], branch="shared/branch", repo="myrepo")
        update_task(hc_home, SAMPLE_TEAM, t2["id"], branch="shared/branch", repo="myrepo")
        change_status(hc_home, SAMPLE_TEAM, t1["id"], "in_progress")
        change_status(hc_home, SAMPLE_TEAM, t2["id"], "in_progress")

        # Both in_progress — each should see the other as unmerged
        assert _other_unmerged_tasks_on_branch(hc_home, SAMPLE_TEAM, "shared/branch", t1["id"]) is True
        assert _other_unmerged_tasks_on_branch(hc_home, SAMPLE_TEAM, "shared/branch", t2["id"]) is True

    def test_no_other_unmerged_when_all_merged(self, hc_home, tmp_path):
        """_other_unmerged_tasks_on_branch returns False when the only other
        task on the branch is already merged."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "shared/branch")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        t1 = create_task(hc_home, SAMPLE_TEAM, title="Task 1", assignee="manager")
        t2 = create_task(hc_home, SAMPLE_TEAM, title="Task 2", assignee="manager")
        update_task(hc_home, SAMPLE_TEAM, t1["id"], branch="shared/branch", repo="myrepo")
        update_task(hc_home, SAMPLE_TEAM, t2["id"], branch="shared/branch", repo="myrepo")

        # Advance t1 to merged
        change_status(hc_home, SAMPLE_TEAM, t1["id"], "in_progress")
        change_status(hc_home, SAMPLE_TEAM, t1["id"], "in_review")
        change_status(hc_home, SAMPLE_TEAM, t1["id"], "in_approval")
        change_status(hc_home, SAMPLE_TEAM, t1["id"], "merging")
        change_status(hc_home, SAMPLE_TEAM, t1["id"], "done")

        # t2 is in_progress — from t2's perspective, t1 is merged, so False
        change_status(hc_home, SAMPLE_TEAM, t2["id"], "in_progress")
        assert _other_unmerged_tasks_on_branch(hc_home, SAMPLE_TEAM, "shared/branch", t2["id"]) is False

    def test_no_other_when_different_branch(self, hc_home):
        """Tasks on different branches do not interfere."""
        t1 = create_task(hc_home, SAMPLE_TEAM, title="Task 1", assignee="manager")
        t2 = create_task(hc_home, SAMPLE_TEAM, title="Task 2", assignee="manager")
        update_task(hc_home, SAMPLE_TEAM, t1["id"], branch="branch-a", repo="myrepo")
        update_task(hc_home, SAMPLE_TEAM, t2["id"], branch="branch-b", repo="myrepo")
        change_status(hc_home, SAMPLE_TEAM, t1["id"], "in_progress")
        change_status(hc_home, SAMPLE_TEAM, t2["id"], "in_progress")

        assert _other_unmerged_tasks_on_branch(hc_home, SAMPLE_TEAM, "branch-a", t1["id"]) is False
        assert _other_unmerged_tasks_on_branch(hc_home, SAMPLE_TEAM, "branch-b", t2["id"]) is False

    def test_branch_kept_when_sibling_task_unmerged(self, hc_home, tmp_path):
        """Merging one task should NOT delete the branch when a sibling task
        on the same branch is still unmerged."""
        repo = _setup_git_repo(tmp_path)
        branch = "shared/T0001-T0002"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create two tasks sharing the same branch
        t1 = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        t2 = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, t1["id"], approval_status="approved")
        update_task(hc_home, SAMPLE_TEAM, t2["id"], approval_status="approved")

        # Merge the first task
        result = merge_task(hc_home, SAMPLE_TEAM, t1["id"], skip_tests=True)
        assert result.success is True
        assert get_task(hc_home, SAMPLE_TEAM, t1["id"])["status"] == "done"

        # The branch must still exist because t2 is not merged yet
        branch_check = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch in branch_check.stdout, (
            f"Branch '{branch}' was deleted prematurely — t2 still needs it"
        )

    def test_branch_deleted_when_last_task_merged(self, hc_home, tmp_path):
        """Branch should be deleted after the last task on it is merged."""
        repo = _setup_git_repo(tmp_path)
        branch = "shared/T0001-T0002"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        t1 = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        t2 = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, t1["id"], approval_status="approved")
        update_task(hc_home, SAMPLE_TEAM, t2["id"], approval_status="approved")

        # Merge t1 (branch kept because t2 still unmerged)
        r1 = merge_task(hc_home, SAMPLE_TEAM, t1["id"], skip_tests=True)
        assert r1.success is True

        # Merge t2 — now last task, branch should be cleaned up
        r2 = merge_task(hc_home, SAMPLE_TEAM, t2["id"], skip_tests=True)
        assert r2.success is True

        # Branch should now be deleted
        branch_check = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch not in branch_check.stdout, (
            f"Branch '{branch}' should have been deleted after last task merged"
        )

    def test_single_task_branch_deleted_normally(self, hc_home, tmp_path):
        """When only one task uses a branch, cleanup proceeds normally."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        # Branch should be deleted (only one task)
        branch_check = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert branch not in branch_check.stdout


# ---------------------------------------------------------------------------
# Master-branch tests — verify everything works for repos using "master"
# instead of "main" as the default branch.
# ---------------------------------------------------------------------------

def _setup_git_repo_master(tmp_path: Path) -> Path:
    """Set up a local git repo with **master** as the default branch."""
    repo = tmp_path / "source_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "README.md").write_text("# Test repo\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo), capture_output=True)
    return repo


def _make_feature_branch_master(repo: Path, branch: str, filename: str = "feature.py", content: str = "# New\n"):
    """Create a feature branch and return to master."""
    subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo), capture_output=True, check=True)
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", f"Add {filename}"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "checkout", "master"], cwd=str(repo), capture_output=True, check=True)


class TestMasterBranch:
    """Verify that repos using 'master' as the default branch work correctly."""

    def test_get_default_branch_detects_master(self, tmp_path):
        """get_default_branch should return 'master' for repos without a 'main' branch."""
        from delegate.repo import get_default_branch, _default_branch_cache
        _default_branch_cache.clear()

        repo = _setup_git_repo_master(tmp_path)
        assert get_default_branch(repo) == "master"

    def test_get_default_branch_prefers_main(self, tmp_path):
        """If both 'main' and 'master' exist, prefer 'main'."""
        from delegate.repo import get_default_branch, _default_branch_cache
        _default_branch_cache.clear()

        repo = _setup_git_repo(tmp_path)
        # Create a master branch too
        subprocess.run(["git", "branch", "master"], cwd=str(repo), capture_output=True, check=True)
        assert get_default_branch(repo) == "main"

    def test_get_default_branch_caches(self, tmp_path):
        """Results should be cached — second call shouldn't need git."""
        from delegate.repo import get_default_branch, _default_branch_cache
        _default_branch_cache.clear()

        repo = _setup_git_repo_master(tmp_path)
        result1 = get_default_branch(repo)
        assert result1 == "master"

        # Verify it's cached
        key = str(Path(repo).resolve())
        assert key in _default_branch_cache
        assert _default_branch_cache[key] == "master"

    def test_successful_merge_master(self, hc_home, tmp_path):
        """Full merge flow works with a master-based repo."""
        from delegate.repo import _default_branch_cache
        _default_branch_cache.clear()

        repo = _setup_git_repo_master(tmp_path)
        _make_feature_branch_master(repo, "alice/T0001")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001", merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True
        assert "success" in result.message.lower()

        updated = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert updated["status"] == "done"

        # Feature should be on master
        log = subprocess.run(
            ["git", "log", "--oneline", "master"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert "Add feature.py" in log.stdout

    def test_conflict_detection_master(self, hc_home, tmp_path):
        """Conflict detection works with master-based repos."""
        from delegate.repo import _default_branch_cache
        _default_branch_cache.clear()

        repo = _setup_git_repo_master(tmp_path)

        # Create feature branch modifying file.txt
        _make_feature_branch_master(repo, "alice/T0001", filename="file.txt", content="feature version\n")

        # Create conflicting commit on master
        (repo / "file.txt").write_text("master version\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Conflict on master"], cwd=str(repo), capture_output=True)

        _register_repo_with_symlink(hc_home, "myrepo", repo)
        task = _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001", merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is False

    def test_ff_merge_user_on_master(self, hc_home, tmp_path):
        """When user has master checked out cleanly, ff-only updates working tree."""
        from delegate.repo import _default_branch_cache
        _default_branch_cache.clear()

        repo = _setup_git_repo_master(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch_master(repo, branch, filename="new_feature.py", content="# feature\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Ensure user is on master (should already be)
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert head.stdout.strip() == "master"

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        # Working tree should have the new file
        assert (repo / "new_feature.py").exists()

    def test_ff_merge_user_on_other_branch(self, hc_home, tmp_path):
        """When user is on another branch, master ref is updated without touching working tree."""
        from delegate.repo import _default_branch_cache
        _default_branch_cache.clear()

        repo = _setup_git_repo_master(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch_master(repo, branch, filename="new_feature.py", content="# feature\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Switch user to a different branch
        subprocess.run(["git", "checkout", "-b", "user/work"], cwd=str(repo), capture_output=True, check=True)

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        # User should still be on their branch
        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert head.stdout.strip() == "user/work"

        # But master ref should include the feature commit
        log = subprocess.run(
            ["git", "log", "--oneline", "master"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert "Add new_feature.py" in log.stdout

    def test_worktree_creation_off_master(self, hc_home, tmp_path):
        """create_task_worktree should branch off master when that's the default."""
        from delegate.repo import create_task_worktree, _default_branch_cache
        _default_branch_cache.clear()

        repo = _setup_git_repo_master(tmp_path)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = create_task(hc_home, SAMPLE_TEAM, title="Master task", assignee="alice")

        wt_path = create_task_worktree(hc_home, SAMPLE_TEAM, "myrepo", task["id"])
        assert wt_path.exists()

        # Worktree should be based off master
        log = subprocess.run(
            ["git", "log", "--oneline", "-1", "master"],
            cwd=str(repo), capture_output=True, text=True,
        )
        master_sha = log.stdout.split()[0]

        wt_log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(wt_path), capture_output=True, text=True,
        )
        assert master_sha in wt_log.stdout


class TestSortMergeCandidates:
    """Tests for _sort_merge_candidates merge ordering."""

    def test_dependency_order(self, hc_home, tmp_path):
        """B depends on A → A sorted first."""
        repo = _setup_git_repo(tmp_path)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create two feature branches with different files
        _make_feature_branch(repo, "feature/a", filename="a.py", content="# A\n")
        _make_feature_branch(repo, "feature/b", filename="b.py", content="# B\n")

        # Get base SHA
        base_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Create tasks: B depends on A
        task_a = create_task(hc_home, SAMPLE_TEAM, title="Task A", assignee="alice")
        update_task(hc_home, SAMPLE_TEAM, task_a["id"], repo="myrepo",
                    branch="feature/a", base_sha={"myrepo": base_sha})

        task_b = create_task(hc_home, SAMPLE_TEAM, title="Task B", assignee="bob",
                             depends_on=[task_a["id"]])
        update_task(hc_home, SAMPLE_TEAM, task_b["id"], repo="myrepo",
                    branch="feature/b", base_sha={"myrepo": base_sha})

        # Fetch fresh task data
        tasks = [
            get_task(hc_home, SAMPLE_TEAM, task_a["id"]),
            get_task(hc_home, SAMPLE_TEAM, task_b["id"]),
        ]

        # Sort — even if B is listed first, A should come first
        result = _sort_merge_candidates(hc_home, SAMPLE_TEAM, [tasks[1], tasks[0]])
        assert result[0]["id"] == task_a["id"], "A should be sorted before B"
        assert result[1]["id"] == task_b["id"]

    def test_non_overlapping_first(self, hc_home, tmp_path):
        """Task with unique files sorted before tasks with shared files."""
        repo = _setup_git_repo(tmp_path)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Task X: modifies shared.py (overlaps with Y)
        _make_feature_branch(repo, "feature/x", filename="shared.py", content="# X\n")
        # Task Y: modifies shared.py AND shared2.py (overlaps with X)
        subprocess.run(["git", "checkout", "-b", "feature/y"], cwd=str(repo),
                        capture_output=True, check=True)
        (repo / "shared.py").write_text("# Y\n")
        (repo / "shared2.py").write_text("# Y2\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Y changes"], cwd=str(repo),
                        capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(repo),
                        capture_output=True, check=True)
        # Task Z: modifies unique.py (no overlap)
        _make_feature_branch(repo, "feature/z", filename="unique.py", content="# Z\n")

        base_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        task_x = create_task(hc_home, SAMPLE_TEAM, title="Task X", assignee="alice")
        update_task(hc_home, SAMPLE_TEAM, task_x["id"], repo="myrepo",
                    branch="feature/x", base_sha={"myrepo": base_sha})

        task_y = create_task(hc_home, SAMPLE_TEAM, title="Task Y", assignee="bob")
        update_task(hc_home, SAMPLE_TEAM, task_y["id"], repo="myrepo",
                    branch="feature/y", base_sha={"myrepo": base_sha})

        task_z = create_task(hc_home, SAMPLE_TEAM, title="Task Z", assignee="alice")
        update_task(hc_home, SAMPLE_TEAM, task_z["id"], repo="myrepo",
                    branch="feature/z", base_sha={"myrepo": base_sha})

        tasks = [
            get_task(hc_home, SAMPLE_TEAM, task_x["id"]),
            get_task(hc_home, SAMPLE_TEAM, task_y["id"]),
            get_task(hc_home, SAMPLE_TEAM, task_z["id"]),
        ]

        result = _sort_merge_candidates(hc_home, SAMPLE_TEAM, tasks)
        # Z has no overlap → should be first
        assert result[0]["id"] == task_z["id"], \
            f"Non-overlapping task Z should be first, got task {result[0]['id']}"

    def test_single_task_unchanged(self, hc_home, tmp_path):
        """Single task returned as-is."""
        repo = _setup_git_repo(tmp_path)
        _register_repo_with_symlink(hc_home, "myrepo", repo)
        _make_feature_branch(repo, "feature/solo", filename="solo.py", content="# Solo\n")

        base_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        task = create_task(hc_home, SAMPLE_TEAM, title="Solo", assignee="alice")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo",
                    branch="feature/solo", base_sha={"myrepo": base_sha})
        task_data = get_task(hc_home, SAMPLE_TEAM, task["id"])

        result = _sort_merge_candidates(hc_home, SAMPLE_TEAM, [task_data])
        assert len(result) == 1
        assert result[0]["id"] == task["id"]

    def test_empty_list(self, hc_home):
        """Empty input returns empty output."""
        result = _sort_merge_candidates(hc_home, SAMPLE_TEAM, [])
        assert result == []
