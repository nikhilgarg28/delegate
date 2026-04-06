"""Tests for rebase_to_main MCP tool.

Verifies that:
- Tool uses diff-apply to rebase onto main without phantom deletions
- Tool updates task base_sha to new main HEAD
- Tool checks for dirty/staged changes before rebasing
- Tool returns detailed status per repo
- Tool reports had_conflicts for real conflicts
"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from delegate.task import create_task, change_status, update_task, get_task
from delegate.repo import get_task_worktree_path
from delegate.config import add_repo, set_boss
from delegate.bootstrap import bootstrap
from delegate.mcp_tools import build_agent_tools
from delegate.paths import repos_dir

SAMPLE_TEAM = "myteam"


@pytest.fixture
def hc_home(tmp_path):
    """Create a fully bootstrapped delegate home directory."""
    hc = tmp_path / "hc_home"
    hc.mkdir()
    set_boss(hc, "nikhil")
    bootstrap(hc, SAMPLE_TEAM, manager="delegate", agents=[("tyson", "engineer")])
    return hc


def _setup_git_repo(tmp_path: Path) -> Path:
    """Set up a local git repo with a main branch and initial commit."""
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
    """Register a repo by creating a symlink in repos/."""
    rd = repos_dir(hc_home, SAMPLE_TEAM)
    rd.mkdir(parents=True, exist_ok=True)
    link = rd / name
    if not link.exists():
        link.symlink_to(source_repo)
    add_repo(hc_home, SAMPLE_TEAM, name, str(source_repo), approval="auto")


def _advance_main(repo: Path, filename: str = "main.py", content: str = "# Main change\n"):
    """Add a commit to main branch."""
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", f"Add {filename}"], cwd=str(repo), capture_output=True, check=True)


def _call_async_tool(tool, args):
    """Helper to call an async tool function synchronously."""
    return asyncio.run(tool.handler(args))


class TestRebaseToMain:
    """Tests for rebase_to_main MCP tool."""

    def test_rebase_updates_base_sha(self, hc_home, tmp_path):
        """rebase_to_main updates task base_sha to new main HEAD."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "feature/test")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task and worktree
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo", branch="feature/test")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        # Create worktree (branch already exists from _make_feature_branch)
        worktree_path = get_task_worktree_path(hc_home, SAMPLE_TEAM, "myrepo", task["id"])
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "feature/test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        # Get old base_sha
        task_before = get_task(hc_home, SAMPLE_TEAM, task["id"])
        old_base_sha = task_before.get("base_sha", {}).get("myrepo")

        # Advance main
        _advance_main(repo)

        # Get new main SHA
        new_main_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Verify result
        assert not result.get("isError"), f"Tool failed: {result}"
        result_text = result["content"][0]["text"]
        result_data = json.loads(result_text)
        assert result_data["repos"]["myrepo"]["new_base_sha"] == new_main_sha
        assert result_data["repos"]["myrepo"]["status"] == "applied_clean"

        # Verify task base_sha was updated
        task_after = get_task(hc_home, SAMPLE_TEAM, task["id"])
        assert task_after["base_sha"]["myrepo"] == new_main_sha
        assert task_after["base_sha"]["myrepo"] != old_base_sha

    def test_rebase_fails_on_dirty_worktree(self, hc_home, tmp_path):
        """rebase_to_main fails if worktree has uncommitted changes to tracked files."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "feature/test")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task and worktree
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo", branch="feature/test")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        worktree_path = get_task_worktree_path(hc_home, SAMPLE_TEAM, "myrepo", task["id"])
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "feature/test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        # Make worktree dirty by modifying a tracked file
        (worktree_path / "feature.py").write_text("# Modified\n")

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Verify it failed
        assert result.get("isError"), "Tool should fail on dirty worktree"
        result_text = result["content"][0]["text"]
        assert "dirty" in result_text.lower()

    def test_rebase_fails_on_staged_changes(self, hc_home, tmp_path):
        """rebase_to_main fails if worktree has staged changes."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "feature/test")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task and worktree
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo", branch="feature/test")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        worktree_path = get_task_worktree_path(hc_home, SAMPLE_TEAM, "myrepo", task["id"])
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "feature/test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        # Stage changes
        (worktree_path / "staged.py").write_text("# Staged\n")
        subprocess.run(["git", "add", "staged.py"], cwd=str(worktree_path), capture_output=True, check=True)

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Verify it failed (staged untracked file shows as dirty in diff --cached)
        assert result.get("isError"), "Tool should fail on staged changes"
        result_text = result["content"][0]["text"]
        # Error message mentions "staged" or "dirty" depending on file state
        assert ("staged" in result_text.lower() or "dirty" in result_text.lower())

    def test_rebase_fails_on_missing_worktree(self, hc_home, tmp_path):
        """rebase_to_main fails if worktree doesn't exist."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "feature/test")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task WITHOUT creating worktree
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo", branch="feature/test")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Verify it failed
        assert result.get("isError"), "Tool should fail on missing worktree"
        result_text = result["content"][0]["text"]
        assert "not found" in result_text.lower()

    def test_rebase_fails_on_task_without_branch(self, hc_home, tmp_path):
        """rebase_to_main fails if task has no branch."""
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Verify it failed
        assert result.get("isError"), "Tool should fail on task without branch"
        result_text = result["content"][0]["text"]
        assert "no branch" in result_text.lower()

    def test_rebase_changes_are_staged(self, hc_home, tmp_path):
        """After rebase_to_main, changes are staged and ready to commit."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "feature/test")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task and worktree
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo", branch="feature/test")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        worktree_path = get_task_worktree_path(hc_home, SAMPLE_TEAM, "myrepo", task["id"])
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "feature/test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        # Advance main
        _advance_main(repo)

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Verify success
        assert not result.get("isError"), f"Tool failed: {result}"

        # Check that changes are staged
        staged_result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        staged_files = staged_result.stdout.strip().split("\n")
        assert "feature.py" in staged_files, "Feature file should be staged"

        # Check that working tree is clean
        diff_result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert not diff_result.stdout.strip(), "Working tree should be clean"

    def test_rebase_no_phantom_deletions(self, hc_home, tmp_path):
        """After rebase, files added to main by other tasks are NOT staged as deletions."""
        repo = _setup_git_repo(tmp_path)
        _make_feature_branch(repo, "feature/test")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task and worktree
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo", branch="feature/test")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        worktree_path = get_task_worktree_path(hc_home, SAMPLE_TEAM, "myrepo", task["id"])
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "feature/test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        # Advance main with files from "another task"
        _advance_main(repo, "other_task_file.py", "# From other task\n")
        _advance_main(repo, "another_file.py", "# Another merged file\n")

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Verify success
        assert not result.get("isError"), f"Tool failed: {result}"
        result_data = json.loads(result["content"][0]["text"])
        assert result_data["repos"]["myrepo"]["status"] == "applied_clean"
        assert result_data["had_conflicts"] is False

        # Verify other tasks' files exist in working tree (not deleted)
        assert (worktree_path / "other_task_file.py").exists(), \
            "File from other task should exist in working tree"
        assert (worktree_path / "another_file.py").exists(), \
            "File from other task should exist in working tree"

        # Verify other tasks' files are NOT staged as deletions
        staged_result = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        for line in staged_result.stdout.strip().splitlines():
            if line:
                status = line.split("\t")[0]
                filename = line.split("\t")[1] if "\t" in line else ""
                assert status != "D" or filename not in ("other_task_file.py", "another_file.py"), \
                    f"Phantom deletion detected: {line}"

    def test_rebase_with_real_conflict(self, hc_home, tmp_path):
        """When feature and main modify the same file, rebase reports conflicts."""
        repo = _setup_git_repo(tmp_path)
        # Create feature that modifies README.md
        _make_feature_branch(repo, "feature/test", filename="README.md", content="# Feature version\n")
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task and worktree
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo", branch="feature/test")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        worktree_path = get_task_worktree_path(hc_home, SAMPLE_TEAM, "myrepo", task["id"])
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "feature/test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        # Advance main with a conflicting change to the same file
        _advance_main(repo, "README.md", "# Main version — conflicting change\n")

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Tool should succeed but report conflicts
        assert not result.get("isError"), f"Tool failed unexpectedly: {result}"
        result_data = json.loads(result["content"][0]["text"])
        assert result_data["repos"]["myrepo"]["had_conflicts"] is True
        assert result_data["repos"]["myrepo"]["status"] == "applied_conflicts"
        assert result_data["had_conflicts"] is True

        # Verify conflict markers exist in the working tree
        readme_content = (worktree_path / "README.md").read_text()
        assert "<<<<<<<" in readme_content, "Should have conflict markers"

    def test_rebase_empty_diff(self, hc_home, tmp_path):
        """When feature branch has no changes from base, rebase produces a clean state."""
        repo = _setup_git_repo(tmp_path)
        # Create a feature branch with NO actual changes (just branch off main)
        subprocess.run(["git", "checkout", "-b", "feature/test"], cwd=str(repo),
                        capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(repo),
                        capture_output=True, check=True)
        _register_repo_with_symlink(hc_home, "myrepo", repo)

        # Create task and worktree
        task = create_task(hc_home, SAMPLE_TEAM, title="Test", assignee="tyson")
        update_task(hc_home, SAMPLE_TEAM, task["id"], repo="myrepo", branch="feature/test")
        change_status(hc_home, SAMPLE_TEAM, task["id"], "in_progress")

        worktree_path = get_task_worktree_path(hc_home, SAMPLE_TEAM, "myrepo", task["id"])
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "feature/test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        # Advance main so there's something new
        _advance_main(repo)

        # Call rebase_to_main tool
        tools = build_agent_tools(hc_home, SAMPLE_TEAM, "tyson")
        rebase_tool = next(t for t in tools if t.name == "rebase_to_main")
        result = _call_async_tool(rebase_tool, {"task_id": task["id"]})

        # Verify clean result with no changes staged
        assert not result.get("isError"), f"Tool failed: {result}"
        result_data = json.loads(result["content"][0]["text"])
        assert result_data["repos"]["myrepo"]["status"] == "applied_clean"
        assert result_data["had_conflicts"] is False

        # No staged changes (empty diff means nothing to apply)
        staged_result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert not staged_result.stdout.strip(), "No changes should be staged for empty diff"
