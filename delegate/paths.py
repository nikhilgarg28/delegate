"""Centralized path computations for Delegate.

All state lives under a single home directory (``~/.delegate`` by default).
The ``DELEGATE_HOME`` environment variable overrides the default for testing.

Per-team state (DB, repos, tasks, mailbox) lives under
``~/.delegate/teams/<team>/``.
"""

import os
from pathlib import Path

_DEFAULT_HOME = Path.home() / ".delegate"


def home(override: Path | None = None) -> Path:
    """Return the Delegate home directory.

    Resolution order:
    1. *override* argument (used in tests)
    2. ``DELEGATE_HOME`` environment variable
    3. ``~/.delegate``
    """
    if override is not None:
        return override
    env = os.environ.get("DELEGATE_HOME")
    if env:
        return Path(env)
    return _DEFAULT_HOME


# --- Boss (org-wide, outside any team) ---

def boss_person_dir(hc_home: Path) -> Path:
    """Boss's global directory (outside any team)."""
    return hc_home / "boss"


# --- Global paths ---

def config_path(hc_home: Path) -> Path:
    return hc_home / "config.yaml"


def daemon_pid_path(hc_home: Path) -> Path:
    return hc_home / "daemon.pid"


# --- Team paths ---

def teams_dir(hc_home: Path) -> Path:
    return hc_home / "teams"


def team_dir(hc_home: Path, team: str) -> Path:
    return teams_dir(hc_home) / team


def team_id_path(hc_home: Path, team: str) -> Path:
    """Path to the file storing the team's unique instance ID."""
    return team_dir(hc_home, team) / "team_id"


def get_team_id(hc_home: Path, team: str) -> str:
    """Read the 6-char hex team instance ID.

    Every team gets a random ID at bootstrap time.  This ID is embedded in
    branch names (``delegate/<team_id>/<team>/T<NNN>``) so that recreating
    a team with the same name doesn't collide with leftover branches.

    Falls back to the team name if the file doesn't exist (pre-migration
    teams).
    """
    p = team_id_path(hc_home, team)
    if p.exists():
        tid = p.read_text().strip()
        if tid:
            return tid
    return team


def global_db_path(hc_home: Path) -> Path:
    """Global SQLite database (multi-team)."""
    return hc_home / "db.sqlite"


def db_path(hc_home: Path, team: str) -> Path:
    """Per-team SQLite database (deprecated — use global_db_path)."""
    return team_dir(hc_home, team) / "db.sqlite"


def repos_dir(hc_home: Path, team: str) -> Path:
    """Per-team repo symlinks directory."""
    return team_dir(hc_home, team) / "repos"


def repo_path(hc_home: Path, team: str, name: str) -> Path:
    """Path to a specific repo symlink within a team."""
    return repos_dir(hc_home, team) / name


def agents_dir(hc_home: Path, team: str) -> Path:
    return team_dir(hc_home, team) / "agents"


def agent_dir(hc_home: Path, team: str, agent: str) -> Path:
    return agents_dir(hc_home, team) / agent


def agent_worktrees_dir(hc_home: Path, team: str, agent: str) -> Path:
    return agent_dir(hc_home, team, agent) / "worktrees"


def task_worktree_dir(hc_home: Path, team: str, repo_name: str, task_id: int) -> Path:
    """Per-task worktree directory: ``teams/{team}/worktrees/{repo}/T{id}/``."""
    from delegate.task import format_task_id
    return team_dir(hc_home, team) / "worktrees" / repo_name / format_task_id(task_id)


def shared_dir(hc_home: Path, team: str) -> Path:
    """Team-level shared knowledge base directory."""
    return team_dir(hc_home, team) / "shared"


def charter_dir(hc_home: Path, team: str) -> Path:
    """Team-level charter directory (for override.md)."""
    return team_dir(hc_home, team)


def roster_path(hc_home: Path, team: str) -> Path:
    return team_dir(hc_home, team) / "roster.md"


# --- Package-shipped charter (read-only, from installed package) ---

def base_charter_dir() -> Path:
    """Return the path to the base charter files shipped with the package."""
    return Path(__file__).parent / "charter"
