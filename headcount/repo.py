"""Repository management — registration, full clones, and git worktrees.

Registered repos are stored as full clones in ``~/.headcount/repos/<name>/``.
Each agent gets isolated worktrees within their own agent directory for tasks.

Usage:
    headcount repo add <path_or_url> [--name NAME]
    headcount repo list
"""

import logging
import re
import subprocess
from pathlib import Path

from headcount.paths import repos_dir as _repos_dir, repo_path as _repo_path, agent_worktrees_dir
from headcount.config import add_repo as _config_add_repo, get_repos as _config_get_repos, update_repo_approval as _config_update_approval

logger = logging.getLogger(__name__)


def _derive_name(source: str) -> str:
    """Derive a repo name from a path or URL.

    Examples:
        /Users/me/projects/myapp       -> myapp
        https://github.com/org/myapp   -> myapp
        git@github.com:org/myapp.git   -> myapp
    """
    # Strip trailing .git
    source = source.rstrip("/")
    if source.endswith(".git"):
        source = source[:-4]

    # Get last path component
    name = source.rsplit("/", 1)[-1]
    # Also handle : in git@ URLs
    name = name.rsplit(":", 1)[-1]
    # Clean up
    name = re.sub(r"[^\w\-.]", "_", name)
    return name or "repo"


def register_repo(
    hc_home: Path,
    source: str,
    name: str | None = None,
    approval: str | None = None,
) -> str:
    """Register a repository by creating a full clone in ~/.headcount/repos/.

    Args:
        hc_home: Headcount home directory.
        source: Local path or remote URL to clone from.
        name: Name for the repo (default: derived from source).
        approval: Merge approval mode — 'auto' or 'manual'.
                  Defaults to 'manual' for new repos.
                  If None on re-registration, leaves existing setting unchanged.

    Returns:
        The name used for the repo.

    Raises:
        subprocess.CalledProcessError: If git clone fails.
    """
    name = name or _derive_name(source)
    dest = _repo_path(hc_home, name)

    if dest.exists():
        # Already registered — just fetch latest
        logger.info("Repo '%s' already exists at %s, fetching...", name, dest)
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=str(dest),
            capture_output=True,
            check=True,
        )
        # Update approval setting if explicitly provided
        if approval is not None:
            _config_update_approval(hc_home, name, approval)
            logger.info("Updated approval for '%s' to '%s'", name, approval)
    else:
        # Full clone
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Resolve local paths
        clone_source = source
        if not source.startswith(("http://", "https://", "git@", "ssh://")):
            clone_source = str(Path(source).resolve())

        logger.info("Cloning %s -> %s", clone_source, dest)
        subprocess.run(
            ["git", "clone", clone_source, str(dest)],
            capture_output=True,
            check=True,
        )

        # Register in config (new repo — default approval to 'manual')
        _config_add_repo(hc_home, name, source, approval=approval or "manual")

    logger.info("Registered repo '%s' from %s", name, source)
    return name


def list_repos(hc_home: Path) -> dict:
    """List registered repos from config.

    Returns:
        Dict of name -> metadata (source, etc.).
    """
    return _config_get_repos(hc_home)


def get_repo_clone_path(hc_home: Path, repo_name: str) -> Path:
    """Get the path to a repo's full clone."""
    return _repo_path(hc_home, repo_name)


def create_agent_worktree(
    hc_home: Path,
    team: str,
    repo_name: str,
    agent: str,
    task_id: int,
    branch: str | None = None,
) -> Path:
    """Create a git worktree for an agent working on a task.

    The worktree is created inside the agent's own directory:
        ~/.headcount/teams/<team>/agents/<agent>/worktrees/<repo_name>-T<task_id>/

    Args:
        hc_home: Headcount home directory.
        team: Team name.
        repo_name: Name of the registered repo.
        agent: Agent name.
        task_id: Task ID number.
        branch: Branch name (default: <agent>/T<task_id>).

    Returns:
        Path to the created worktree directory.

    Raises:
        FileNotFoundError: If the repo clone doesn't exist.
        subprocess.CalledProcessError: If git worktree add fails.
    """
    clone_path = get_repo_clone_path(hc_home, repo_name)
    if not clone_path.is_dir():
        raise FileNotFoundError(f"Repo clone not found at {clone_path}")

    # Default branch name
    if branch is None:
        branch = f"{agent}/T{task_id:04d}"

    # Worktree destination
    wt_dir = agent_worktrees_dir(hc_home, team, agent)
    wt_dir.mkdir(parents=True, exist_ok=True)
    wt_name = f"{repo_name}-T{task_id:04d}"
    wt_path = wt_dir / wt_name

    if wt_path.exists():
        logger.info("Worktree already exists at %s", wt_path)
        return wt_path

    # Fetch latest before creating worktree
    subprocess.run(
        ["git", "fetch", "--all"],
        cwd=str(clone_path),
        capture_output=True,
        check=False,  # Don't fail if fetch fails (offline)
    )

    # Create worktree with a new branch
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), "-b", branch],
        cwd=str(clone_path),
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
        hc_home: Headcount home directory.
        team: Team name.
        repo_name: Name of the registered repo.
        agent: Agent name.
        task_id: Task ID number.
    """
    clone_path = get_repo_clone_path(hc_home, repo_name)
    wt_dir = agent_worktrees_dir(hc_home, team, agent)
    wt_name = f"{repo_name}-T{task_id:04d}"
    wt_path = wt_dir / wt_name

    if not wt_path.exists():
        logger.info("Worktree already removed: %s", wt_path)
        return

    # Remove worktree via git
    if clone_path.is_dir():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=str(clone_path),
            capture_output=True,
            check=False,
        )
    else:
        # Clone gone — just remove directory
        import shutil
        shutil.rmtree(wt_path, ignore_errors=True)

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
    wt_name = f"{repo_name}-T{task_id:04d}"
    return wt_dir / wt_name


def push_branch(
    hc_home: Path,
    repo_name: str,
    branch: str,
    remote: str = "origin",
) -> bool:
    """Push a branch to the remote.

    Uses the full clone as the working directory.

    Returns:
        True if push succeeded, False otherwise.
    """
    clone_path = get_repo_clone_path(hc_home, repo_name)
    if not clone_path.is_dir():
        logger.error("Repo clone not found: %s", clone_path)
        return False

    result = subprocess.run(
        ["git", "push", remote, branch],
        cwd=str(clone_path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Push failed: %s", result.stderr)
        return False

    logger.info("Pushed branch '%s' to %s", branch, remote)
    return True
