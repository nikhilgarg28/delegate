"""Configuration management for Delegate.

Global config lives in ``~/.delegate/config.yaml`` (source_repo, etc.).
Human members live in ``~/.delegate/members/<name>.yaml``.
Per-team repo config lives in ``~/.delegate/teams/<team>/repos.yaml``.
"""

from pathlib import Path

import yaml

from delegate.paths import config_path, team_dir, members_dir, member_path

# ---------------------------------------------------------------------------
# Well-known identities
# ---------------------------------------------------------------------------

SYSTEM_USER = "system"
"""The system user identity — used for automated actions, merge outcomes,
status transitions, CI/CD integrations, and other non-human/non-agent events.

Not a real member; hardcoded as recognised everywhere (routing, display, etc.).
Messages from ``system`` are informational events, never routed to an inbox.
"""

# ---------------------------------------------------------------------------
# Global config (config.yaml)
# ---------------------------------------------------------------------------

def _read(hc_home: Path) -> dict:
    """Read global config.yaml, returning empty dict if missing."""
    cp = config_path(hc_home)
    if cp.exists():
        return yaml.safe_load(cp.read_text()) or {}
    return {}


def _write(hc_home: Path, data: dict) -> None:
    """Write global config.yaml (creates parent dirs if needed)."""
    cp = config_path(hc_home)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


# ---------------------------------------------------------------------------
# Members (human identities — replaces the old boss model)
# ---------------------------------------------------------------------------

def migrate_boss_to_member(hc_home: Path) -> str | None:
    """One-time migration: if ``config.yaml`` has a ``boss`` key but no
    ``members/`` directory, create the member file automatically.

    Returns the migrated name, or None if no migration was needed.
    """
    md = members_dir(hc_home)
    if md.is_dir() and any(md.iterdir()):
        return None  # members already exist
    legacy_name = _read(hc_home).get("boss")
    if not legacy_name:
        return None  # nothing to migrate
    add_member(hc_home, legacy_name)
    return legacy_name


def migrate_standard_to_default_workflow(hc_home: Path) -> int:
    """One-time migration: rename on-disk ``workflows/standard/`` dirs to
    ``workflows/default/`` for each team.

    The DB-level rename (``workflow = 'standard'`` → ``'default'``) is handled
    by migration V14 in ``db.py``.  This function handles the filesystem side.

    Returns the number of teams migrated.
    """
    teams_root = hc_home / "teams"
    if not teams_root.is_dir():
        return 0

    migrated = 0
    for team_dir in teams_root.iterdir():
        if not team_dir.is_dir():
            continue
        old = team_dir / "workflows" / "standard"
        new = team_dir / "workflows" / "default"
        if old.is_dir() and not new.exists():
            old.rename(new)
            migrated += 1
    return migrated


def get_human_members(hc_home: Path) -> list[dict]:
    """Return all human members as a list of dicts.

    Each dict has at least ``name`` and ``kind`` (always ``"human"``).
    """
    md = members_dir(hc_home)
    if not md.is_dir():
        return []
    members = []
    for f in sorted(md.iterdir()):
        if f.suffix != ".yaml":
            continue
        data = yaml.safe_load(f.read_text()) or {}
        data.setdefault("name", f.stem)
        data.setdefault("kind", "human")
        members.append(data)
    return members


def get_default_human(hc_home: Path) -> str:
    """Return the name of the default (first) human member.

    Falls back to the legacy ``config.yaml:boss`` field, then ``"human"``.
    """
    members = get_human_members(hc_home)
    if members:
        return members[0]["name"]
    # Legacy fallback
    return get_boss(hc_home) or "human"


def add_member(hc_home: Path, name: str, **extra) -> dict:
    """Create a human member YAML file.

    Returns the member dict.  Safe to call multiple times — does not
    overwrite existing files.
    """
    md = members_dir(hc_home)
    md.mkdir(parents=True, exist_ok=True)
    mp = member_path(hc_home, name)
    data = {"name": name, "kind": "human"}
    data.update(extra)
    if not mp.exists():
        mp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    else:
        data = yaml.safe_load(mp.read_text()) or {}
        data.setdefault("name", name)
        data.setdefault("kind", "human")
    return data


def remove_member(hc_home: Path, name: str) -> bool:
    """Remove a human member YAML file.  Returns True if removed."""
    mp = member_path(hc_home, name)
    if mp.exists():
        mp.unlink()
        return True
    return False


# --- Legacy boss helpers (backward compat — delegates to member API) ---

def get_boss(hc_home: Path) -> str | None:
    """Return the primary human name, or None if not set.

    .. deprecated:: Use ``get_default_human`` instead.

    Checks the members directory first, then falls back to
    the legacy ``config.yaml:boss`` field.
    """
    members = get_human_members(hc_home)
    if members:
        return members[0]["name"]
    return _read(hc_home).get("boss")


def set_boss(hc_home: Path, name: str) -> None:
    """Create a human member (and write legacy config.yaml key).

    .. deprecated:: Use ``add_member`` instead.
    """
    # Create member file
    add_member(hc_home, name)
    # Legacy config.yaml — kept so older code/tools can still read it
    data = _read(hc_home)
    data["boss"] = name
    _write(hc_home, data)


# --- Source repo (for self-update) ---

def get_source_repo(hc_home: Path) -> Path | None:
    """Return path to delegate's own source repo, or None."""
    val = _read(hc_home).get("source_repo")
    return Path(val) if val else None


def set_source_repo(hc_home: Path, path: Path) -> None:
    """Set the delegate source repo path."""
    data = _read(hc_home)
    data["source_repo"] = str(path)
    _write(hc_home, data)


# ---------------------------------------------------------------------------
# Per-team repo config (teams/<team>/repos.yaml)
# ---------------------------------------------------------------------------

def _repos_config_path(hc_home: Path, team: str) -> Path:
    return team_dir(hc_home, team) / "repos.yaml"


def _read_repos(hc_home: Path, team: str) -> dict:
    """Read per-team repos.yaml, returning empty dict if missing."""
    rp = _repos_config_path(hc_home, team)
    if rp.exists():
        return yaml.safe_load(rp.read_text()) or {}
    return {}


def _write_repos(hc_home: Path, team: str, data: dict) -> None:
    """Write per-team repos.yaml."""
    rp = _repos_config_path(hc_home, team)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def get_repos(hc_home: Path, team: str) -> dict:
    """Return the repos dict (name -> metadata) for a team."""
    return _read_repos(hc_home, team)


def add_repo(
    hc_home: Path,
    team: str,
    name: str,
    source: str,
    approval: str = "manual",
    test_cmd: str | None = None,
) -> None:
    """Register a repo for a team.

    Args:
        hc_home: Delegate home directory.
        team: Team name.
        name: Repo name.
        source: Local path or remote URL.
        approval: Merge approval mode — 'auto' or 'manual' (default: 'manual').
        test_cmd: Optional shell command to run tests.
    """
    data = _read_repos(hc_home, team)
    existing = data.get(name, {})
    existing["source"] = source
    existing["approval"] = approval
    if test_cmd is not None:
        existing["test_cmd"] = test_cmd
    data[name] = existing
    _write_repos(hc_home, team, data)


def update_repo_approval(hc_home: Path, team: str, name: str, approval: str) -> None:
    """Update the approval setting for an existing repo."""
    data = _read_repos(hc_home, team)
    if name not in data:
        raise KeyError(f"Repo '{name}' not found in team '{team}' config")
    data[name]["approval"] = approval
    _write_repos(hc_home, team, data)


def get_repo_approval(hc_home: Path, team: str, repo_name: str) -> str:
    """Return the approval mode for a repo ('auto' or 'manual').

    Defaults to 'manual' if not set or repo not found.
    """
    repos = get_repos(hc_home, team)
    meta = repos.get(repo_name, {})
    return meta.get("approval", "manual")


# --- Repo test_cmd ---

def get_repo_test_cmd(hc_home: Path, team: str, repo_name: str) -> str | None:
    """Return the configured test command for a repo, or None if not set."""
    repos = get_repos(hc_home, team)
    meta = repos.get(repo_name, {})
    return meta.get("test_cmd")


def update_repo_test_cmd(hc_home: Path, team: str, name: str, test_cmd: str) -> None:
    """Update the test command for an existing repo."""
    data = _read_repos(hc_home, team)
    if name not in data:
        raise KeyError(f"Repo '{name}' not found in team '{team}' config")
    data[name]["test_cmd"] = test_cmd
    _write_repos(hc_home, team, data)


# --- Pre-merge script ---

def get_pre_merge_script(hc_home: Path, team: str, repo_name: str) -> str | None:
    """Return the configured pre-merge script path for a repo, or None.

    Also checks for legacy ``pipeline`` and ``test_cmd`` fields for
    backward compatibility — returns the first step's command if found.
    """
    repos = get_repos(hc_home, team)
    meta = repos.get(repo_name, {})

    script = meta.get("pre_merge_script")
    if script:
        return script

    # Backward compat: legacy pipeline → use first step
    pipeline = meta.get("pipeline")
    if pipeline and len(pipeline) > 0:
        return pipeline[0].get("run")

    # Backward compat: legacy test_cmd
    test_cmd = meta.get("test_cmd")
    if test_cmd:
        return test_cmd

    return None


def set_pre_merge_script(hc_home: Path, team: str, repo_name: str, script_path: str) -> None:
    """Set the pre-merge script for an existing repo.

    The *script_path* should be relative to the repo root or an absolute path.
    Pass an empty string to clear the pre-merge script.
    """
    data = _read_repos(hc_home, team)
    if repo_name not in data:
        raise KeyError(f"Repo '{repo_name}' not found in team '{team}' config")

    if script_path:
        data[repo_name]["pre_merge_script"] = script_path
    else:
        data[repo_name].pop("pre_merge_script", None)
    # Clean up legacy fields
    data[repo_name].pop("pipeline", None)
    data[repo_name].pop("test_cmd", None)
    _write_repos(hc_home, team, data)
