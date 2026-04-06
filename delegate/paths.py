"""Centralized path computations for Delegate.

All state lives under a single home directory (``~/.delegate`` by default).
The ``DELEGATE_HOME`` environment variable overrides the default for testing.

Layout::

    ~/.delegate/
      protected/                  # Infrastructure (outside agent sandbox)
        daemon.pid
        delegate.log
        db.sqlite
        config.yaml
        network.yaml
        members/
        projects/<team>/
          repos.yaml
          roster.md
          team_id
      projects/                   # Working data (inside agent sandbox)
        <team>/
          override.md
          agents/<agent>/
          shared/
          worktrees/
"""

import json
import os
import threading
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


# =========================================================================
# Protected directory — infrastructure, outside agent sandbox
# =========================================================================

def protected_dir(hc_home: Path) -> Path:
    """Root of protected infrastructure: ``<home>/protected/``."""
    return hc_home / "protected"


def protected_team_dir(hc_home: Path, team: str) -> Path:
    """Per-team metadata inside protected: ``protected/projects/<team_uuid>/``.

    Resolves *team* (name) to UUID via the team map.
    Falls back to the name if no mapping exists.
    """
    return protected_dir(hc_home) / "projects" / resolve_team_uuid(hc_home, team)


# --- Global infrastructure ---

def global_db_path(hc_home: Path) -> Path:
    """Global SQLite database: ``protected/db.sqlite``."""
    return protected_dir(hc_home) / "db.sqlite"


def daemon_pid_path(hc_home: Path) -> Path:
    """Daemon PID file: ``protected/daemon.pid``."""
    return protected_dir(hc_home) / "daemon.pid"


def daemon_lock_path(hc_home: Path) -> Path:
    """Daemon lock file for ``fcntl.flock()``: ``protected/daemon.lock``."""
    return protected_dir(hc_home) / "daemon.lock"


def config_path(hc_home: Path) -> Path:
    """Global config: ``protected/config.yaml``."""
    return protected_dir(hc_home) / "config.yaml"


def network_config_path(hc_home: Path) -> Path:
    """Network allowlist: ``protected/network.yaml``."""
    return protected_dir(hc_home) / "network.yaml"


def get_bootstrap_id(hc_home: Path) -> str | None:
    """Return the bootstrap ID for this installation, or None if not set.

    The bootstrap_id is a UUID generated on first bootstrap, stored in
    ``protected/bootstrap_id``. Used by the frontend to namespace localStorage keys.
    """
    bootstrap_id_path = protected_dir(hc_home) / "bootstrap_id"
    if not bootstrap_id_path.exists():
        return None
    return bootstrap_id_path.read_text().strip()


# --- Members (org-wide, outside any team) ---

def members_dir(hc_home: Path) -> Path:
    """Directory containing human member YAML files: ``protected/members/``."""
    return protected_dir(hc_home) / "members"


def member_path(hc_home: Path, name: str) -> Path:
    """Path to a specific member's YAML file."""
    return members_dir(hc_home) / f"{name}.yaml"


# --- Per-team metadata (inside protected/) ---

def roster_path(hc_home: Path, team: str) -> Path:
    """Team roster: ``protected/projects/<team>/roster.md``."""
    return protected_team_dir(hc_home, team) / "roster.md"


def team_id_path(hc_home: Path, team: str) -> Path:
    """Path to the file storing the team's unique instance ID."""
    return protected_team_dir(hc_home, team) / "team_id"


def repos_config_path(hc_home: Path, team: str) -> Path:
    """Per-team repos config: ``protected/projects/<team>/repos.yaml``."""
    return protected_team_dir(hc_home, team) / "repos.yaml"


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


# =========================================================================
# Team name ↔ UUID resolution (file-based, no DB dependency)
# =========================================================================

_team_map_lock = threading.Lock()
_team_map_cache: dict[str, dict[str, str]] = {}  # hc_home_str -> {name: uuid}


def _team_map_path(hc_home: Path) -> Path:
    """Path to the team name → UUID mapping file."""
    return protected_dir(hc_home) / "project_map.json"


def _load_team_map(hc_home: Path) -> dict[str, str]:
    """Load the team name → UUID mapping (cached)."""
    key = str(hc_home)
    with _team_map_lock:
        if key in _team_map_cache:
            return _team_map_cache[key]
    mp = _team_map_path(hc_home)
    if mp.exists():
        data = json.loads(mp.read_text())
    else:
        data = {}
    with _team_map_lock:
        _team_map_cache[key] = data
    return data


def _reload_team_map(hc_home: Path) -> dict[str, str]:
    """Force-reload team map from disk, bypassing cache."""
    mp = _team_map_path(hc_home)
    data = json.loads(mp.read_text()) if mp.exists() else {}
    with _team_map_lock:
        _team_map_cache[str(hc_home)] = data
    return data


def _save_team_map(hc_home: Path, data: dict[str, str]) -> None:
    """Persist the team name → UUID mapping."""
    mp = _team_map_path(hc_home)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(data, indent=2))
    with _team_map_lock:
        _team_map_cache[str(hc_home)] = dict(data)


def register_team_path(hc_home: Path, team_name: str, team_uuid: str) -> None:
    """Register a team name → UUID mapping for directory paths.

    Called at bootstrap time.  The mapping is stored in
    ``protected/team_map.json`` and cached in-process.
    """
    data = _load_team_map(hc_home)
    data[team_name] = team_uuid
    _save_team_map(hc_home, data)


def unregister_team_path(hc_home: Path, team_name: str) -> None:
    """Remove a team name → UUID mapping."""
    data = _load_team_map(hc_home)
    data.pop(team_name, None)
    _save_team_map(hc_home, data)


def resolve_team_uuid(hc_home: Path, team_name: str) -> str:
    """Resolve a team name to its UUID for directory/DB usage.

    Returns the UUID if a mapping exists, otherwise returns the
    team name unchanged (fallback for tests and pre-UUID data).
    """
    data = _load_team_map(hc_home)
    if team_name in data:
        return data[team_name]
    # Cache miss — reload from disk in case an external process updated the file
    data = _reload_team_map(hc_home)
    return data.get(team_name, team_name)


def resolve_team_name(hc_home: Path, team_uuid: str) -> str:
    """Resolve a UUID back to a team name.

    Falls back to the UUID itself if no mapping is found.
    """
    data = _load_team_map(hc_home)
    for name, uid in data.items():
        if uid == team_uuid:
            return name
    return team_uuid


def list_team_names(hc_home: Path) -> list[str]:
    """Return all registered team names."""
    return list(_load_team_map(hc_home).keys())


def invalidate_team_map_cache(hc_home: Path | None = None) -> None:
    """Clear the team map cache (for tests)."""
    with _team_map_lock:
        if hc_home is not None:
            _team_map_cache.pop(str(hc_home), None)
        else:
            _team_map_cache.clear()


# =========================================================================
# Working data — teams directory (inside agent sandbox)
# =========================================================================

def teams_dir(hc_home: Path) -> Path:
    return hc_home / "projects"


def team_dir(hc_home: Path, team: str) -> Path:
    """Team working directory: ``projects/<team_uuid>/``.

    Resolves *team* (a human-readable name) to its UUID for the
    directory path.  Falls back to the name itself if no UUID
    mapping exists (convenience for tests).
    """
    return teams_dir(hc_home) / resolve_team_uuid(hc_home, team)


# --- Per-team paths (working data) ---

def repos_dir(hc_home: Path, team: str) -> Path:
    """Per-team repo symlinks directory: ``teams/<team>/repos/``."""
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


# Mapping from artifact category (used in tool schemas) to subdirectory name.
# The canonical source is delegate.adapters.DEFAULT_ARTIFACT_CATEGORIES.
from delegate.adapters import DEFAULT_ARTIFACT_CATEGORIES as ARTIFACT_CATEGORIES


def task_artifacts_dir(hc_home: Path, team: str, task_id: int) -> Path:
    """Per-task artifacts directory: ``teams/{team}/artifacts/T{id}/``.

    Unlike worktrees, this directory persists after task completion.
    Used for model checkpoints, training logs, evaluation reports,
    and other large binary outputs that don't belong in git.
    """
    from delegate.task import format_task_id
    return team_dir(hc_home, team) / "artifacts" / format_task_id(task_id)


def shared_dir(hc_home: Path, team: str) -> Path:
    """Team-level shared knowledge base directory."""
    return team_dir(hc_home, team) / "shared"


def charter_dir(hc_home: Path, team: str) -> Path:
    """Team-level charter directory (for override.md)."""
    return team_dir(hc_home, team)


# --- Deprecated per-team db path (use global_db_path instead) ---

def db_path(hc_home: Path, team: str) -> Path:
    """Per-team SQLite database (deprecated — use global_db_path)."""
    return team_dir(hc_home, team) / "db.sqlite"


# --- Boss (deprecated — use members_dir) ---

def boss_person_dir(hc_home: Path) -> Path:
    """Boss's global directory (outside any team).

    .. deprecated:: Use ``members_dir`` instead.
    """
    return hc_home / "boss"


# --- Package-shipped charter (read-only, from installed package) ---

def base_charter_dir() -> Path:
    """Return the path to the base charter files shipped with the package."""
    return Path(__file__).parent / "charter"


# =========================================================================
# Bootstrap helpers — ensure directory structure exists
# =========================================================================

def ensure_protected(hc_home: Path) -> None:
    """Create the protected/ directory structure if it doesn't exist."""
    protected = protected_dir(hc_home)
    protected.mkdir(parents=True, exist_ok=True)
    (protected / "projects").mkdir(exist_ok=True)
    members_dir(hc_home).mkdir(exist_ok=True)


def ensure_protected_team(hc_home: Path, team: str) -> None:
    """Create the protected/teams/<team>/ directory."""
    protected_team_dir(hc_home, team).mkdir(parents=True, exist_ok=True)
