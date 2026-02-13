"""Tests for delegate/repo.py — repo registration via symlinks and worktrees."""

import subprocess
from pathlib import Path

import pytest

from delegate.bootstrap import bootstrap
from delegate.config import set_boss, add_repo
from delegate.repo import (
    register_repo,
    update_repo_path,
    list_repos,
    get_repo_path,
    create_agent_worktree,
    remove_agent_worktree,
    get_worktree_path,
)
from delegate.task import create_task, update_task, get_task


TEAM = "myteam"


@pytest.fixture
def hc_home(tmp_path):
    """Create a fully bootstrapped delegate home directory."""
    hc = tmp_path / "hc_home"
    hc.mkdir()
    set_boss(hc, "nikhil")
    bootstrap(hc, TEAM, manager="edison", agents=["alice", "bob", ("sarah", "qa")])
    return hc


@pytest.fixture
def local_repo(tmp_path):
    """Create a local git repo with a main branch."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    (repo / "README.md").write_text("# Project\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo), capture_output=True, check=True)
    return repo


class TestRegisterRepo:
    def test_creates_symlink(self, hc_home, local_repo):
        name = register_repo(hc_home, TEAM, str(local_repo))
        link = get_repo_path(hc_home, TEAM, name)
        assert link.is_symlink()
        assert link.resolve() == local_repo.resolve()

    def test_derives_name_from_path(self, hc_home, local_repo):
        name = register_repo(hc_home, TEAM, str(local_repo))
        assert name == local_repo.name

    def test_custom_name(self, hc_home, local_repo):
        name = register_repo(hc_home, TEAM, str(local_repo), name="custom")
        assert name == "custom"
        link = get_repo_path(hc_home, TEAM, "custom")
        assert link.is_symlink()

    def test_rejects_remote_url(self, hc_home):
        with pytest.raises(ValueError, match="Remote URLs are not supported"):
            register_repo(hc_home, TEAM, "https://github.com/org/repo.git")

    def test_rejects_missing_path(self, hc_home, tmp_path):
        with pytest.raises(FileNotFoundError):
            register_repo(hc_home, TEAM, str(tmp_path / "nonexistent"))

    def test_rejects_no_git_dir(self, hc_home, tmp_path):
        no_git = tmp_path / "not_a_repo"
        no_git.mkdir()
        with pytest.raises(FileNotFoundError, match="No .git"):
            register_repo(hc_home, TEAM, str(no_git))

    def test_registers_in_config(self, hc_home, local_repo):
        register_repo(hc_home, TEAM, str(local_repo))
        repos = list_repos(hc_home, TEAM)
        assert local_repo.name in repos

    def test_idempotent(self, hc_home, local_repo):
        name1 = register_repo(hc_home, TEAM, str(local_repo))
        name2 = register_repo(hc_home, TEAM, str(local_repo))
        assert name1 == name2

    def test_updates_symlink_on_move(self, hc_home, local_repo, tmp_path):
        register_repo(hc_home, TEAM, str(local_repo))
        new_loc = tmp_path / "moved_repo"
        local_repo.rename(new_loc)
        # Re-register with same name pointing to new location
        register_repo(hc_home, TEAM, str(new_loc), name=local_repo.name)
        link = get_repo_path(hc_home, TEAM, local_repo.name)
        assert link.resolve() == new_loc.resolve()


class TestUpdateRepoPath:
    def test_updates_symlink(self, hc_home, local_repo, tmp_path):
        register_repo(hc_home, TEAM, str(local_repo))
        new_loc = tmp_path / "moved"
        new_loc.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(new_loc), capture_output=True, check=True)

        update_repo_path(hc_home, TEAM, local_repo.name, str(new_loc))
        link = get_repo_path(hc_home, TEAM, local_repo.name)
        assert link.resolve() == new_loc.resolve()

    def test_raises_for_unknown_repo(self, hc_home, tmp_path):
        with pytest.raises(FileNotFoundError):
            update_repo_path(hc_home, TEAM, "nonexistent", str(tmp_path))


class TestWorktree:
    def test_create_and_get_worktree(self, hc_home, local_repo):
        register_repo(hc_home, TEAM, str(local_repo))
        repo_name = local_repo.name

        wt_path = create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=1, branch="alice/T0001",
        )
        assert wt_path.is_dir()
        assert (wt_path / "README.md").exists()

        expected = get_worktree_path(hc_home, TEAM, repo_name, "alice", 1)
        assert wt_path == expected

    def test_records_base_sha(self, hc_home, local_repo):
        register_repo(hc_home, TEAM, str(local_repo))
        repo_name = local_repo.name

        # Create a task to receive the base_sha
        task = create_task(hc_home, TEAM, title="Test task", assignee="manager")
        update_task(hc_home, TEAM, task["id"], repo=repo_name)

        create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=task["id"], branch="alice/T0001",
        )

        updated = get_task(hc_home, TEAM, task["id"])
        assert updated["base_sha"] != {}
        assert isinstance(updated["base_sha"], dict)
        sha = updated["base_sha"][repo_name]
        assert len(sha) == 40  # Full SHA

    def test_remove_worktree(self, hc_home, local_repo):
        register_repo(hc_home, TEAM, str(local_repo))
        repo_name = local_repo.name

        wt_path = create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=2, branch="alice/T0002",
        )
        assert wt_path.is_dir()

        remove_agent_worktree(hc_home, TEAM, repo_name, "alice", 2)
        assert not wt_path.exists()

    def test_remove_worktree_prunes_when_directory_missing(self, hc_home, local_repo):
        """Verify git worktree prune runs even when worktree directory is already gone."""
        register_repo(hc_home, TEAM, str(local_repo))
        repo_name = local_repo.name
        real_repo = get_repo_path(hc_home, TEAM, repo_name).resolve()

        # Create and then manually delete the worktree directory (not via git)
        wt_path = create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=5, branch="alice/T0005",
        )
        assert wt_path.is_dir()

        # Manually delete directory to simulate the bug scenario
        import shutil
        shutil.rmtree(wt_path)
        assert not wt_path.exists()

        # Verify git still sees the worktree in metadata
        result = subprocess.run(
            ["git", "worktree", "list"],
            cwd=str(real_repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "alice/T0005" in result.stdout

        # Call remove_agent_worktree — should prune even though dir is gone
        remove_agent_worktree(hc_home, TEAM, repo_name, "alice", 5)

        # Verify git metadata is cleaned up
        result = subprocess.run(
            ["git", "worktree", "list"],
            cwd=str(real_repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "alice/T0005" not in result.stdout

        # Clean up the branch so we can reuse the name
        subprocess.run(
            ["git", "branch", "-D", "alice/T0005"],
            cwd=str(real_repo),
            capture_output=True,
            check=False,
        )

        # Verify we can now create a new worktree with the same branch name
        wt_path2 = create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=6, branch="alice/T0005",
        )
        assert wt_path2.is_dir()

    def test_idempotent_create(self, hc_home, local_repo):
        register_repo(hc_home, TEAM, str(local_repo))
        repo_name = local_repo.name

        wt1 = create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=3, branch="alice/T0003",
        )
        wt2 = create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=3, branch="alice/T0003",
        )
        assert wt1 == wt2

    def test_backfills_base_sha_on_existing_worktree(self, hc_home, local_repo):
        """When worktree already exists but task has no base_sha, backfill it."""
        register_repo(hc_home, TEAM, str(local_repo))
        repo_name = local_repo.name

        # Create a task
        task = create_task(hc_home, TEAM, title="Backfill test", assignee="manager")
        update_task(hc_home, TEAM, task["id"], repo=repo_name)

        # First call creates the worktree and sets base_sha
        create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=task["id"], branch="alice/T0001",
        )
        t1 = get_task(hc_home, TEAM, task["id"])
        assert isinstance(t1["base_sha"], dict)
        assert repo_name in t1["base_sha"]

        # Clear base_sha to simulate the bug
        update_task(hc_home, TEAM, task["id"], base_sha={})
        t_cleared = get_task(hc_home, TEAM, task["id"])
        assert t_cleared["base_sha"] == {}

        # Second call should backfill base_sha even though worktree exists
        create_agent_worktree(
            hc_home, TEAM, repo_name, "alice", task_id=task["id"], branch="alice/T0001",
        )
        t2 = get_task(hc_home, TEAM, task["id"])
        assert isinstance(t2["base_sha"], dict)
        assert len(t2["base_sha"][repo_name]) == 40
