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
    add_repo, get_repo_approval, get_repo_test_cmd, update_repo_test_cmd, set_boss,
    get_pre_merge_script, set_pre_merge_script,
)
from delegate.merge import merge_task, merge_once, _run_pre_merge, _other_unmerged_tasks_on_branch, MergeResult, MergeFailureReason
from delegate.bootstrap import bootstrap


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


def _register_repo_with_symlink(hc_home: Path, name: str, source_repo: Path):
    """Register a repo by creating a symlink in hc_home/teams/<team>/repos/."""
    from delegate.paths import repos_dir
    rd = repos_dir(hc_home, SAMPLE_TEAM)
    rd.mkdir(parents=True, exist_ok=True)
    link = rd / name
    if not link.exists():
        link.symlink_to(source_repo)
    add_repo(hc_home, SAMPLE_TEAM, name, str(source_repo), approval="auto")


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

    def test_rebase_conflict(self, hc_home, tmp_path):
        """Rebase conflict → merge_task returns REBASE_CONFLICT reason."""
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
        assert result.reason == MergeFailureReason.REBASE_CONFLICT
        assert "conflict" in result.message.lower() or "rebase" in result.message.lower()

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

    def test_feature_branch_untouched_on_failure(self, hc_home, tmp_path):
        """On merge failure, the feature branch should remain at its original tip."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Advance main to create a rebase scenario
        (repo / "extra.txt").write_text("extra\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "main moves ahead"], cwd=str(repo), capture_output=True, check=True)

        # Record branch tip BEFORE merge attempt
        pre_tip = subprocess.run(
            ["git", "rev-parse", branch], cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()

        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "false")  # Tests will fail

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False
        assert result.reason == MergeFailureReason.PRE_MERGE_FAILED

        # Feature branch must be at its ORIGINAL tip (never touched)
        post_tip = subprocess.run(
            ["git", "rev-parse", branch], cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()
        assert post_tip == pre_tip, f"Feature branch was modified: {post_tip} != {pre_tip}"

    def test_agent_worktree_survives_failure(self, hc_home, tmp_path):
        """On failure, the agent's worktree should remain intact."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create an agent worktree (simulating normal task work)
        wt_dir = hc_home / "teams" / SAMPLE_TEAM / "worktrees" / "myrepo"
        wt_dir.mkdir(parents=True, exist_ok=True)
        wt_path = wt_dir / "T0001"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(repo), capture_output=True, check=True,
        )
        assert wt_path.exists()

        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "false")  # Force failure

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False
        assert wt_path.exists(), "Agent worktree was removed on merge failure — should be preserved"

    def test_agent_worktree_removed_on_success(self, hc_home, tmp_path):
        """On success, the agent's worktree should be cleaned up."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create an agent worktree
        wt_dir = hc_home / "teams" / SAMPLE_TEAM / "worktrees" / "myrepo"
        wt_dir.mkdir(parents=True, exist_ok=True)
        wt_path = wt_dir / "T0001"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(repo), capture_output=True, check=True,
        )
        assert wt_path.exists()

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True

        # Agent worktree should be cleaned up after success
        assert not wt_path.exists(), "Agent worktree should be removed after successful merge"

    def test_temp_worktree_cleaned_up_on_failure(self, hc_home, tmp_path):
        """Temp merge worktree should be removed even on failure."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "false")  # Force failure

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False

        # No merge worktrees should remain
        merge_wt_dir = hc_home / "teams" / SAMPLE_TEAM / "worktrees" / "_merge"
        if merge_wt_dir.exists():
            remaining = list(merge_wt_dir.rglob("*"))
            assert len(remaining) == 0, f"Stale merge worktree remains: {remaining}"

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
        assert result.reason == MergeFailureReason.REBASE_CONFLICT
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

    def test_skips_manual_unapproved(self, hc_home):
        """Manual approval tasks without approval_status='approved' are skipped."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/fake", approval="manual")
        _make_in_approval_task(hc_home, title="Unapproved")
        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_auto_merge_processes(self, hc_home, tmp_path):
        """Auto approval tasks should be processed without boss approval."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        _make_in_approval_task(hc_home, repo="myrepo", branch="alice/T0001")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert len(results) == 1
        assert results[0].success is True

    def test_manual_approved_processes(self, hc_home, tmp_path):
        """Manual tasks with an approved review should be processed."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001")

        from delegate.paths import repos_dir
        from delegate.review import get_current_review, set_verdict
        rd = repos_dir(hc_home, SAMPLE_TEAM)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "myrepo").symlink_to(repo)
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", str(repo), approval="manual")

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


# ---------------------------------------------------------------------------
# get_repo_approval tests
# ---------------------------------------------------------------------------

class TestGetRepoApproval:
    def test_returns_manual_by_default(self, hc_home):
        assert get_repo_approval(hc_home, SAMPLE_TEAM, "nonexistent") == "manual"

    def test_reads_from_config(self, hc_home):
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo", approval="auto")
        add_repo(hc_home, SAMPLE_TEAM, "other", "/tmp/other", approval="manual")

        assert get_repo_approval(hc_home, SAMPLE_TEAM, "myrepo") == "auto"
        assert get_repo_approval(hc_home, SAMPLE_TEAM, "other") == "manual"
        assert get_repo_approval(hc_home, SAMPLE_TEAM, "missing") == "manual"


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
# Pre-merge script config tests
# ---------------------------------------------------------------------------

class TestPreMergeScriptConfig:
    def test_returns_none_by_default(self, hc_home):
        """Repo without pre-merge script or test_cmd returns None."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo")
        assert get_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo") is None

    def test_returns_none_for_missing_repo(self, hc_home):
        assert get_pre_merge_script(hc_home, SAMPLE_TEAM, "nonexistent") is None

    def test_backward_compat_test_cmd(self, hc_home):
        """Legacy test_cmd should be returned as pre-merge script."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo", test_cmd="pytest -x")
        script = get_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo")
        assert script == "pytest -x"

    def test_set_pre_merge_script(self, hc_home):
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo")
        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "./scripts/pre-merge.sh")
        assert get_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo") == "./scripts/pre-merge.sh"

    def test_set_pre_merge_script_missing_repo(self, hc_home):
        with pytest.raises(KeyError, match="not found"):
            set_pre_merge_script(hc_home, SAMPLE_TEAM, "no_such_repo", "echo test")

    def test_clear_pre_merge_script(self, hc_home):
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo")
        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "./test.sh")
        assert get_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo") == "./test.sh"
        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "")
        assert get_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo") is None

    def test_set_cleans_up_legacy_fields(self, hc_home):
        """Setting pre-merge script should remove legacy pipeline and test_cmd."""
        add_repo(hc_home, SAMPLE_TEAM, "myrepo", "/tmp/repo", test_cmd="pytest -x")
        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "./ci.sh")
        assert get_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo") == "./ci.sh"


# ---------------------------------------------------------------------------
# _run_pre_merge tests (runs inside a worktree, no branch arg needed)
# ---------------------------------------------------------------------------

class TestRunPreMerge:
    def _setup_worktree(self, hc_home, tmp_path, branch="alice/T0001"):
        """Create a repo, feature branch, and a worktree at that branch.
        Returns (repo, wt_path)."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create a worktree to simulate the merge worktree
        wt_path = tmp_path / "merge_wt"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(repo), capture_output=True, check=True,
        )
        return repo, wt_path

    def test_script_passes(self, hc_home, tmp_path):
        """Pre-merge script that succeeds should return ok."""
        repo, wt_path = self._setup_worktree(hc_home, tmp_path)
        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "echo all-checks-pass")

        ok, output = _run_pre_merge(str(wt_path), hc_home=hc_home, team=SAMPLE_TEAM, repo_name="myrepo")
        assert ok is True
        assert "all-checks-pass" in output

    def test_script_fails(self, hc_home, tmp_path):
        """Pre-merge script that fails should return not ok."""
        repo, wt_path = self._setup_worktree(hc_home, tmp_path)
        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "false")

        ok, output = _run_pre_merge(str(wt_path), hc_home=hc_home, team=SAMPLE_TEAM, repo_name="myrepo")
        assert ok is False

    def test_backward_compat_test_cmd(self, hc_home, tmp_path):
        """Legacy test_cmd should work as pre-merge script."""
        repo, wt_path = self._setup_worktree(hc_home, tmp_path)
        update_repo_test_cmd(hc_home, SAMPLE_TEAM, "myrepo", "echo legacy-test-passed")

        ok, output = _run_pre_merge(str(wt_path), hc_home=hc_home, team=SAMPLE_TEAM, repo_name="myrepo")
        assert ok is True
        assert "legacy-test-passed" in output

    def test_falls_back_to_autodetect(self, hc_home, tmp_path):
        """When no script and no test_cmd, falls back to auto-detection."""
        repo, wt_path = self._setup_worktree(hc_home, tmp_path)

        # No script, no test_cmd, no pyproject.toml → skip tests
        ok, output = _run_pre_merge(str(wt_path), hc_home=hc_home, team=SAMPLE_TEAM, repo_name="myrepo")
        assert ok is True
        assert "no test runner" in output.lower()

    def test_merge_with_script_failure(self, hc_home, tmp_path):
        """merge_task should fail when pre-merge script fails."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "false")

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])

        assert result.success is False
        assert result.reason == MergeFailureReason.PRE_MERGE_FAILED
        assert "pre-merge" in result.message.lower() or "failed" in result.message.lower()

    def test_merge_with_script_success(self, hc_home, tmp_path):
        """merge_task should succeed when pre-merge script passes."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        set_pre_merge_script(hc_home, SAMPLE_TEAM, "myrepo", "echo all-checks-pass")

        task = _make_in_approval_task(hc_home, repo="myrepo", branch=branch, merging=True)
        update_task(hc_home, SAMPLE_TEAM, task["id"], approval_status="approved")

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
