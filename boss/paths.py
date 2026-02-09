"""Centralized path computations for boss.

All state lives under a single home directory (``~/.boss`` by default).
The ``BOSS_HOME`` environment variable overrides the default for testing.
"""

import os
from pathlib import Path

_DEFAULT_HOME = Path.home() / ".boss"


def home(override: Path | None = None) -> Path:
    """Return the boss home directory.

    Resolution order:
    1. *override* argument (used in tests)
    2. ``BOSS_HOME`` environment variable
    3. ``~/.boss``
    """
    if override is not None:
        return override
    env = os.environ.get("BOSS_HOME")
    if env:
        return Path(env)
    return _DEFAULT_HOME


# --- Boss (org-wide, outside any team) ---

def boss_person_dir(hc_home: Path) -> Path:
    """Boss's global mailbox directory (outside any team)."""
    return hc_home / "boss"


# --- Global paths ---

def config_path(hc_home: Path) -> Path:
    return hc_home / "config.yaml"


def db_path(hc_home: Path) -> Path:
    return hc_home / "db.sqlite"


def daemon_pid_path(hc_home: Path) -> Path:
    return hc_home / "daemon.pid"


def tasks_dir(hc_home: Path) -> Path:
    return hc_home / "tasks"


def repos_dir(hc_home: Path) -> Path:
    return hc_home / "repos"


def repo_path(hc_home: Path, name: str) -> Path:
    return repos_dir(hc_home) / name


# --- Team paths ---

def teams_dir(hc_home: Path) -> Path:
    return hc_home / "teams"


def team_dir(hc_home: Path, team: str) -> Path:
    return teams_dir(hc_home) / team


def agents_dir(hc_home: Path, team: str) -> Path:
    return team_dir(hc_home, team) / "agents"


def agent_dir(hc_home: Path, team: str, agent: str) -> Path:
    return agents_dir(hc_home, team) / agent


def agent_worktrees_dir(hc_home: Path, team: str, agent: str) -> Path:
    return agent_dir(hc_home, team, agent) / "worktrees"


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
