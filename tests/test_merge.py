"""Tests for boss/merge.py — merge worker logic.

Tests the new merge flow:
    1. rebase onto main
    2. run tests (skippable)
    3. fast-forward merge
    4. cleanup
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from boss.task import (
    create_task,
    change_status,
    update_task,
    get_task,
)
from boss.config import add_repo, get_repo_approval, set_boss
from boss.merge import merge_task, merge_once, MergeResult
from boss.bootstrap import bootstrap


SAMPLE_TEAM = "myteam"


@pytest.fixture
def hc_home(tmp_path):
    """Create a fully bootstrapped boss home directory."""
    hc = tmp_path / "hc_home"
    hc.mkdir()
    set_boss(hc, "nikhil")
    bootstrap(hc, SAMPLE_TEAM, manager="edison", agents=["alice", "bob"], qa="sarah")
    return hc


def _make_needs_merge_task(hc_home, title="Task", repo="myrepo", branch="feature/test"):
    """Helper: create a task and advance it to needs_merge status."""
    task = create_task(hc_home, title=title)
    update_task(hc_home, task["id"], repo=repo, branch=branch)
    change_status(hc_home, task["id"], "in_progress")
    change_status(hc_home, task["id"], "review")
    change_status(hc_home, task["id"], "needs_merge")
    return get_task(hc_home, task["id"])


def _setup_git_repo(tmp_path: Path) -> Path:
    """Set up a local git repo with a main branch and initial commit.

    Returns the repo path.
    """
    repo = tmp_path / "source_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
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
    """Register a repo by creating a symlink in hc_home/repos/."""
    repos_dir = hc_home / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    link = repos_dir / name
    link.symlink_to(source_repo)
    add_repo(hc_home, name, str(source_repo), approval="auto")


# ---------------------------------------------------------------------------
# merge_task tests (with real git)
# ---------------------------------------------------------------------------

class TestMergeTask:
    def test_successful_merge(self, hc_home, tmp_path):
        """Full merge: rebase, skip-tests, ff-merge."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001-feat")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001-feat")
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True
        assert "success" in result.message.lower()

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"
        assert (repo / "feature.py").exists()  # Feature is on main

    def test_rebase_conflict(self, hc_home, tmp_path):
        """Rebase conflict → status becomes 'conflict' and manager notified."""
        repo = _setup_git_repo(tmp_path)

        # Create feature branch that modifies file.txt
        _make_feature_branch(repo, "alice/T0001-conflict", filename="file.txt", content="feature version\n")

        # Now modify same file on main
        (repo / "file.txt").write_text("main version\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "Diverge main"], cwd=str(repo), capture_output=True, check=True)

        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001-conflict")
        update_task(hc_home, task["id"], approval_status="approved")

        with patch("boss.merge.notify_conflict") as mock_notify:
            result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)

        assert result.success is False
        assert "conflict" in result.message.lower() or "rebase" in result.message.lower()

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "conflict"
        mock_notify.assert_called_once()

    def test_missing_branch(self, hc_home):
        """Task with no branch should fail."""
        task = create_task(hc_home, title="No branch")
        update_task(hc_home, task["id"], repo="myrepo")
        change_status(hc_home, task["id"], "in_progress")
        change_status(hc_home, task["id"], "review")
        change_status(hc_home, task["id"], "needs_merge")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])
        assert result.success is False
        assert "no branch" in result.message.lower()

    def test_missing_repo(self, hc_home):
        """Task with no repo should fail."""
        task = create_task(hc_home, title="No repo")
        update_task(hc_home, task["id"], branch="some/branch")
        change_status(hc_home, task["id"], "in_progress")
        change_status(hc_home, task["id"], "review")
        change_status(hc_home, task["id"], "needs_merge")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"])
        assert result.success is False
        assert "no repo" in result.message.lower()

    def test_merge_removes_worktree_before_rebase(self, hc_home, tmp_path):
        """Merge should remove agent worktree before rebasing so git doesn't
        refuse to rebase a branch checked out in another worktree."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001-wt"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create a worktree for the agent (simulating what happens during task work)
        wt_dir = hc_home / "teams" / SAMPLE_TEAM / "agents" / "alice" / "worktrees"
        wt_dir.mkdir(parents=True, exist_ok=True)
        wt_path = wt_dir / "myrepo-T0001"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(repo), capture_output=True, check=True,
        )
        assert wt_path.exists()

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], assignee="alice", approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"
        assert not wt_path.exists(), "Worktree should have been removed"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"

    def test_merge_succeeds_with_unstaged_changes(self, hc_home, tmp_path):
        """Merge should stash unstaged changes before rebasing and restore after."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001-unstaged"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create an unstaged change in the repo working directory
        (repo / "untracked_file.js").write_text("// generated\n")

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"

        # The untracked file should still be present after merge
        assert (repo / "untracked_file.js").exists(), "Stashed file should be restored"

    def test_merge_succeeds_with_modified_tracked_file(self, hc_home, tmp_path):
        """Merge should stash modified tracked files before rebasing."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001-modified"
        _make_feature_branch(repo, branch)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Modify a tracked file without staging it
        (repo / "README.md").write_text("# Modified but not staged\n")

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"

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

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved", base_sha=base_sha)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Merge with --onto failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"
        assert (repo / "onto_feature.py").exists()  # Agent's commit landed

    def test_rebase_fallback_without_base_sha(self, hc_home, tmp_path):
        """When base_sha is empty/None the merge falls back to plain rebase."""
        repo = _setup_git_repo(tmp_path)
        branch = "alice/T0001-nobase"
        _make_feature_branch(repo, branch, filename="nobase.py", content="# no base\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        # Explicitly set base_sha to empty string (simulating a task without it)
        update_task(hc_home, task["id"], approval_status="approved", base_sha="")

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Fallback merge failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"
        assert (repo / "nobase.py").exists()

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

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch=branch)
        update_task(hc_home, task["id"], approval_status="approved", base_sha=base_sha)

        result = merge_task(hc_home, SAMPLE_TEAM, task["id"], skip_tests=True)
        assert result.success is True, f"Rebase --onto with reverted commits failed: {result.message}"

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"

        # Agent's work should be on main
        assert (repo / "agent_work.py").exists()
        # M1 and M2 files should NOT be on main (they were reverted)
        assert not (repo / "m1.txt").exists(), "m1.txt should not be on main (reverted commit)"
        assert not (repo / "m2.txt").exists(), "m2.txt should not be on main (reverted commit)"


# ---------------------------------------------------------------------------
# merge_once tests
# ---------------------------------------------------------------------------

class TestMergeOnce:
    def test_empty_when_no_tasks(self, hc_home):
        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_skips_task_without_repo(self, hc_home):
        """Tasks without a repo field are skipped."""
        task = create_task(hc_home, title="No repo")
        update_task(hc_home, task["id"], branch="some/branch")
        change_status(hc_home, task["id"], "in_progress")
        change_status(hc_home, task["id"], "review")
        change_status(hc_home, task["id"], "needs_merge")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_skips_manual_unapproved(self, hc_home):
        """Manual approval tasks without approval_status='approved' are skipped."""
        add_repo(hc_home, "myrepo", "/fake", approval="manual")
        _make_needs_merge_task(hc_home, title="Unapproved")
        results = merge_once(hc_home, SAMPLE_TEAM)
        assert results == []

    def test_auto_merge_processes(self, hc_home, tmp_path):
        """Auto approval tasks should be processed without boss approval."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001-auto")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001-auto")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert len(results) == 1
        assert results[0].success is True

    def test_manual_approved_processes(self, hc_home, tmp_path):
        """Manual tasks with approval_status='approved' should be processed."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "alice/T0001-manual")

        repos_dir = hc_home / "repos"
        repos_dir.mkdir(parents=True, exist_ok=True)
        (repos_dir / "myrepo").symlink_to(repo)
        add_repo(hc_home, "myrepo", str(repo), approval="manual")

        task = _make_needs_merge_task(hc_home, repo="myrepo", branch="alice/T0001-manual")
        update_task(hc_home, task["id"], approval_status="approved")

        results = merge_once(hc_home, SAMPLE_TEAM)
        assert len(results) == 1
        assert results[0].success is True

        updated = get_task(hc_home, task["id"])
        assert updated["status"] == "merged"


# ---------------------------------------------------------------------------
# get_repo_approval tests
# ---------------------------------------------------------------------------

class TestGetRepoApproval:
    def test_returns_manual_by_default(self, hc_home):
        assert get_repo_approval(hc_home, "nonexistent") == "manual"

    def test_reads_from_config(self, hc_home):
        add_repo(hc_home, "myrepo", "/tmp/repo", approval="auto")
        add_repo(hc_home, "other", "/tmp/other", approval="manual")

        assert get_repo_approval(hc_home, "myrepo") == "auto"
        assert get_repo_approval(hc_home, "other") == "manual"
        assert get_repo_approval(hc_home, "missing") == "manual"
