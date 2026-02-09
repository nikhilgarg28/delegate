"""Repository management — registration via symlinks and git worktrees.

Registered repos are stored as **symlinks** in ``~/.boss/repos/<name>/``
pointing to the real local repository root.  No clones are made.

Only local repos are supported (the ``.git/`` directory must exist on disk).
If the repo has its own remote, that's fine — boss doesn't care.

When a repo moves on disk, update the symlink with ``boss repo update``.

Usage:
    boss repo add <local_path> [--name NAME]
    boss repo list
    boss repo update <name> <new_path>
"""

import logging
import re
import subprocess
from pathlib import Path

from boss.task import format_task_id

from boss.paths import repos_dir as _repos_dir, repo_path as _repo_path, agent_worktrees_dir
from boss.config import add_repo as _config_add_repo, get_repos as _config_get_repos, update_repo_approval as _config_update_approval, update_repo_test_cmd as _config_update_test_cmd

logger = logging.getLogger(__name__)


def _derive_name(source: str) -> str:
    """Derive a repo name from a local path.

    Examples:
        /Users/me/projects/myapp -> myapp
        /Users/me/dev/standup    -> standup
    """
    source = source.rstrip("/")
    name = source.rsplit("/", 1)[-1]
    name = re.sub(r"[^\w\-.]", "_", name)
    return name or "repo"


def _resolve_repo_dir(hc_home: Path, name: str) -> Path:
    """Return the canonical repo path (symlink location) inside ~/.boss/repos/."""
    return _repo_path(hc_home, name)


def register_repo(
    hc_home: Path,
    source: str,
    name: str | None = None,
    approval: str | None = None,
    test_cmd: str | None = None,
) -> str:
    """Register a local repository by creating a symlink in ~/.boss/repos/.

    Args:
        hc_home: Boss home directory.
        source: Local path to the repository root (must contain .git/).
        name: Name for the repo (default: derived from source).
        approval: Merge approval mode — 'auto' or 'manual'.
                  Defaults to 'manual' for new repos.
                  If None on re-registration, leaves existing setting unchanged.
        test_cmd: Optional shell command to run tests.
                  If None on re-registration, leaves existing setting unchanged.

    Returns:
        The name used for the repo.

    Raises:
        FileNotFoundError: If the source path doesn't exist or has no .git/.
        ValueError: If the source is a remote URL (not supported).
    """
    # Reject remote URLs
    if source.startswith(("http://", "https://", "git@", "ssh://")):
        raise ValueError(
            f"Remote URLs are not supported. Only local paths with .git/ are allowed. Got: {source}"
        )

    source_path = Path(source).resolve()

    if not source_path.is_dir():
        raise FileNotFoundError(f"Repository path not found: {source_path}")

    git_dir = source_path / ".git"
    if not git_dir.exists():
        raise FileNotFoundError(
            f"No .git directory found at {source_path}. "
            "Only local git repositories are supported."
        )

    name = name or _derive_name(source)
    link_path = _resolve_repo_dir(hc_home, name)

    if link_path.is_symlink() or link_path.exists():
        # Already registered — update symlink target if different
        current_target = link_path.resolve()
        if current_target != source_path:
            logger.info(
                "Repo '%s' symlink target changed: %s -> %s",
                name, current_target, source_path,
            )
            link_path.unlink()
            link_path.symlink_to(source_path)
        else:
            logger.info("Repo '%s' already registered at %s", name, source_path)

        # Update approval setting if explicitly provided
        if approval is not None:
            _config_update_approval(hc_home, name, approval)
            logger.info("Updated approval for '%s' to '%s'", name, approval)

        # Update test_cmd setting if explicitly provided
        if test_cmd is not None:
            _config_update_test_cmd(hc_home, name, test_cmd)
            logger.info("Updated test_cmd for '%s'", name)
    else:
        # Create symlink
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(source_path)
        logger.info("Created symlink %s -> %s", link_path, source_path)

        # Register in config (new repo — default approval to 'manual')
        _config_add_repo(hc_home, name, str(source_path), approval=approval or "manual", test_cmd=test_cmd)

    logger.info("Registered repo '%s' from %s", name, source_path)
    return name


def update_repo_path(hc_home: Path, name: str, new_path: str) -> None:
    """Update the symlink for a registered repo to point to a new location.

    Use this when a repo moves on disk.

    Args:
        hc_home: Boss home directory.
        name: Repo name.
        new_path: New local path to the repository root.

    Raises:
        FileNotFoundError: If repo isn't registered or new path doesn't exist.
    """
    link_path = _resolve_repo_dir(hc_home, name)
    if not link_path.is_symlink() and not link_path.exists():
        raise FileNotFoundError(f"Repo '{name}' is not registered")

    new_source = Path(new_path).resolve()
    if not new_source.is_dir():
        raise FileNotFoundError(f"New path not found: {new_source}")
    if not (new_source / ".git").exists():
        raise FileNotFoundError(f"No .git directory at {new_source}")

    if link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(new_source)

    # Update config
    from boss.config import _read, _write
    data = _read(hc_home)
    repos = data.get("repos", {})
    if name in repos:
        repos[name]["source"] = str(new_source)
        _write(hc_home, data)

    logger.info("Updated repo '%s' symlink -> %s", name, new_source)


def list_repos(hc_home: Path) -> dict:
    """List registered repos from config.

    Returns:
        Dict of name -> metadata (source, approval, etc.).
    """
    return _config_get_repos(hc_home)


def get_repo_path(hc_home: Path, repo_name: str) -> Path:
    """Get the canonical path to a repo (the symlink in ~/.boss/repos/).

    The symlink resolves to the real repo root on disk.
    """
    return _resolve_repo_dir(hc_home, repo_name)


# Keep old name as alias for compatibility
get_repo_clone_path = get_repo_path


def _get_main_head(repo_dir: Path) -> str:
    """Get the current HEAD SHA of the main branch in a repo."""
    result = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def create_agent_worktree(
    hc_home: Path,
    team: str,
    repo_name: str,
    agent: str,
    task_id: int,
    branch: str | None = None,
) -> Path:
    """Create a git worktree for an agent working on a task.

    The worktree is created directly against the real repo (via symlink)
    inside the agent's directory:
        ~/.boss/teams/<team>/agents/<agent>/worktrees/<repo_name>-T<task_id>/

    Before creating the branch, fetches the latest from origin (if available)
    and records the base SHA (current main HEAD) on the task.

    Args:
        hc_home: Boss home directory.
        team: Team name.
        repo_name: Name of the registered repo.
        agent: Agent name.
        task_id: Task ID number.
        branch: Branch name (default: <agent>/T<task_id>).

    Returns:
        Path to the created worktree directory.

    Raises:
        FileNotFoundError: If the repo isn't registered.
        subprocess.CalledProcessError: If git worktree add fails.
    """
    repo_dir = get_repo_path(hc_home, repo_name)
    real_repo = repo_dir.resolve()
    if not real_repo.is_dir():
        raise FileNotFoundError(f"Repo not found at {real_repo} (symlink: {repo_dir})")

    # Default branch name
    if branch is None:
        branch = f"{agent}/{format_task_id(task_id)}"

    # Worktree destination
    wt_dir = agent_worktrees_dir(hc_home, team, agent)
    wt_dir.mkdir(parents=True, exist_ok=True)
    wt_name = f"{repo_name}-{format_task_id(task_id)}"
    wt_path = wt_dir / wt_name

    if wt_path.exists():
        logger.info("Worktree already exists at %s", wt_path)
        return wt_path

    # Fetch latest before creating worktree (best effort)
    subprocess.run(
        ["git", "fetch", "--all"],
        cwd=str(real_repo),
        capture_output=True,
        check=False,  # Don't fail if fetch fails (offline, no remote)
    )

    # Record base SHA (current main HEAD) on the task
    try:
        base_sha = _get_main_head(real_repo)
        from boss.task import update_task
        update_task(hc_home, task_id, base_sha=base_sha)
        logger.info("Recorded base_sha=%s for %s", base_sha[:8], task_id)
    except Exception as exc:
        logger.warning("Could not record base_sha for %s: %s", task_id, exc)

    # Create worktree with a new branch off main
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), "-b", branch, "main"],
        cwd=str(real_repo),
        capture_output=True,
        check=True,
    )

    logger.info("Created worktree at %s (branch: %s)", wt_path, branch)
    return wt_path


def remove_agent_worktree(
    hc_home: Path,
    team: str,
    repo_name: str,
    agent: str,
    task_id: int,
) -> None:
    """Remove an agent's worktree for a task.

    Args:
        hc_home: Boss home directory.
        team: Team name.
        repo_name: Name of the registered repo.
        agent: Agent name.
        task_id: Task ID number.
    """
    repo_dir = get_repo_path(hc_home, repo_name)
    real_repo = repo_dir.resolve()
    wt_dir = agent_worktrees_dir(hc_home, team, agent)
    wt_name = f"{repo_name}-{format_task_id(task_id)}"
    wt_path = wt_dir / wt_name

    if not wt_path.exists():
        logger.info("Worktree already removed: %s", wt_path)
        return

    # Remove worktree via git
    if real_repo.is_dir():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=str(real_repo),
            capture_output=True,
            check=False,
        )
    else:
        # Repo gone — just remove directory
        import shutil
        shutil.rmtree(wt_path, ignore_errors=True)

    # Prune stale worktree entries
    if real_repo.is_dir():
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(real_repo),
            capture_output=True,
            check=False,
        )

    logger.info("Removed worktree at %s", wt_path)


def get_worktree_path(
    hc_home: Path,
    team: str,
    repo_name: str,
    agent: str,
    task_id: int,
) -> Path:
    """Get the path to an agent's worktree for a task.

    Returns the path even if the worktree doesn't exist yet.
    """
    wt_dir = agent_worktrees_dir(hc_home, team, agent)
    wt_name = f"{repo_name}-{format_task_id(task_id)}"
    return wt_dir / wt_name


def push_branch(
    hc_home: Path,
    repo_name: str,
    branch: str,
    remote: str = "origin",
) -> bool:
    """Push a branch to the remote.

    Uses the real repo (via symlink) as the working directory.

    Returns:
        True if push succeeded, False otherwise.
    """
    repo_dir = get_repo_path(hc_home, repo_name)
    real_repo = repo_dir.resolve()
    if not real_repo.is_dir():
        logger.error("Repo not found: %s", real_repo)
        return False

    result = subprocess.run(
        ["git", "push", remote, branch],
        cwd=str(real_repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Push failed: %s", result.stderr)
        return False

    logger.info("Pushed branch '%s' to %s", branch, remote)
    return True
