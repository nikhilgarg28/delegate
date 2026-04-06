"""Configuration management for Delegate.

Global config lives in ``~/.delegate/protected/config.yaml``.
Human members live in ``~/.delegate/protected/members/<name>.yaml``.
Per-team repo config lives in ``~/.delegate/protected/teams/<team>/repos.yaml``.
"""

from pathlib import Path

import yaml

from delegate.paths import config_path, members_dir, member_path, repos_config_path

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

    Falls back to the legacy ``config.yaml:boss`` field, then ``"boss"``.
    Never returns ``"human"`` — that was the old fallback and is treated as
    a placeholder by the frontend display layer.
    """
    members = get_human_members(hc_home)
    if members:
        return members[0]["name"]
    # Legacy fallback
    return get_boss(hc_home) or "boss"


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

        # Register human in member_ids translation table
        from delegate.db import get_connection
        from delegate.db_ids import register_member
        conn = get_connection(hc_home, "")
        try:
            # Use INSERT OR IGNORE to handle re-runs
            register_member(conn, "human", None, name)
            conn.commit()
        except Exception:
            # If registration fails (e.g., duplicate), ignore and continue
            pass
        finally:
            conn.close()
    else:
        data = yaml.safe_load(mp.read_text()) or {}
        data.setdefault("name", name)
        data.setdefault("kind", "human")
    return data


def rename_member(hc_home: Path, old_name: str, new_name: str) -> bool:
    """Rename a human member from *old_name* to *new_name*.

    Updates:
    - The member YAML file (``members/<old_name>.yaml`` → ``members/<new_name>.yaml``)
    - The ``member_ids`` row in the global DB
    - All team roster files that reference the old name

    Returns True if a rename was performed, False if ``old_name`` did not exist
    or ``new_name`` already exists.
    """
    if old_name == new_name:
        return False
    mp_old = member_path(hc_home, old_name)
    if not mp_old.exists():
        return False
    mp_new = member_path(hc_home, new_name)
    if mp_new.exists():
        # Target already exists — do not overwrite
        return False

    # Rename the YAML file
    old_data = yaml.safe_load(mp_old.read_text()) or {}
    new_data = {**old_data, "name": new_name}
    mp_new.write_text(yaml.dump(new_data, default_flow_style=False, sort_keys=False))
    mp_old.unlink()

    # Update member_ids in the global DB
    from delegate.db import get_connection
    conn = get_connection(hc_home, "")
    try:
        conn.execute(
            "UPDATE member_ids SET name = ? WHERE kind = 'human' AND team_uuid IS NULL AND name = ? AND deleted = 0",
            (new_name, old_name),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

    # Update all team roster files that reference the old name
    from delegate.paths import teams_dir as _teams_dir_fn, roster_path as _roster_path_fn
    teams_root = _teams_dir_fn(hc_home)
    if teams_root.is_dir():
        for team_dir_obj in teams_root.iterdir():
            if not team_dir_obj.is_dir():
                continue
            team_name = team_dir_obj.name
            rp = _roster_path_fn(hc_home, team_name)
            if rp.exists():
                text = rp.read_text()
                # Replace exact name match in roster lines (bold-wrapped: **name**)
                new_text = text.replace(f"**{old_name}**", f"**{new_name}**")
                if new_text != text:
                    rp.write_text(new_text)

    return True


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
# Per-team repo config (protected/teams/<team>/repos.yaml)
# ---------------------------------------------------------------------------

def _repos_config_path(hc_home: Path, team: str) -> Path:
    return repos_config_path(hc_home, team)


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
    merge_policy: str = "review-needed",
    test_cmd: str | None = None,
    remote_url: str | None = None,
    *,
    approval: str | None = None,
) -> None:
    """Register a repo for a team.

    Args:
        hc_home: Delegate home directory.
        team: Team name.
        name: Repo name.
        source: Local path or remote URL.
        merge_policy: Merge policy — 'no-review' or 'review-needed' (default).
        test_cmd: Optional shell command to run tests.
        remote_url: Git remote URL (e.g. GitHub) for satellite sync.
        approval: **Deprecated** — legacy alias for merge_policy.
                  'auto' maps to 'no-review', 'manual' maps to 'review-needed'.
    """
    # Legacy mapping
    if approval is not None:
        merge_policy = _legacy_approval_to_policy(approval)

    data = _read_repos(hc_home, team)
    existing = data.get(name, {})
    existing["source"] = source
    existing["merge_policy"] = merge_policy
    existing.pop("approval", None)  # remove legacy key
    if test_cmd is not None:
        existing["test_cmd"] = test_cmd
    if remote_url is not None:
        existing["remote_url"] = remote_url
    data[name] = existing
    _write_repos(hc_home, team, data)


def _legacy_approval_to_policy(approval: str) -> str:
    """Map legacy approval values to merge_policy values."""
    return {"auto": "no-review", "manual": "review-needed"}.get(approval, approval)


def _legacy_policy_to_approval(policy: str) -> str:
    """Map merge_policy values back to legacy approval values."""
    return {"no-review": "auto", "review-needed": "manual"}.get(policy, policy)


def update_merge_policy(hc_home: Path, team: str, name: str, policy: str) -> None:
    """Update the merge policy for an existing repo.

    Args:
        policy: 'no-review' or 'review-needed'.
    """
    data = _read_repos(hc_home, team)
    if name not in data:
        raise KeyError(f"Repo '{name}' not found in team '{team}' config")
    data[name]["merge_policy"] = policy
    data[name].pop("approval", None)  # remove legacy key on write
    _write_repos(hc_home, team, data)


def get_merge_policy(hc_home: Path, team: str, repo_name: str) -> str:
    """Return the merge policy for a repo ('no-review' or 'review-needed').

    Falls back to legacy ``approval`` key if ``merge_policy`` is not set.
    Defaults to 'review-needed' if not set or repo not found.
    """
    repos = get_repos(hc_home, team)
    meta = repos.get(repo_name, {})
    if "merge_policy" in meta:
        return meta["merge_policy"]
    # Legacy fallback
    legacy = meta.get("approval")
    if legacy:
        return _legacy_approval_to_policy(legacy)
    return "review-needed"


# Deprecated aliases — remove after one release cycle
def update_repo_approval(hc_home: Path, team: str, name: str, approval: str) -> None:
    """**Deprecated** — use ``update_merge_policy`` instead."""
    update_merge_policy(hc_home, team, name, _legacy_approval_to_policy(approval))


def get_repo_approval(hc_home: Path, team: str, repo_name: str) -> str:
    """**Deprecated** — use ``get_merge_policy`` instead."""
    return _legacy_policy_to_approval(get_merge_policy(hc_home, team, repo_name))


# ---------------------------------------------------------------------------
# Reviewer config (per-team, stored in repos.yaml under 'reviewer')
# ---------------------------------------------------------------------------

_REVIEWER_DEFAULTS = {
    "mode": "human",
    "threshold": 3.5,
    "model": "claude-sonnet-4-20250514",
    "auto_merge": True,
}


def get_reviewer_config(hc_home: Path, team: str) -> dict:
    """Return the reviewer config for a team.

    Returns dict with keys: mode ('human'|'ai'), threshold (float),
    model (str), auto_merge (bool).

    Missing keys are filled from defaults.  When mode is ``"ai"``,
    ``auto_merge`` is always forced to ``True`` (AI review implies
    auto-merge).

    Falls back to legacy ``auto_approver`` key if ``reviewer`` is not set.
    """
    data = _read_repos(hc_home, team)
    if "reviewer" in data:
        stored = data["reviewer"]
        cfg = {**_REVIEWER_DEFAULTS, **stored}
    elif "auto_approver" in data:
        # Legacy fallback
        legacy = data["auto_approver"]
        cfg = {**_REVIEWER_DEFAULTS}
        cfg["mode"] = "ai" if legacy.get("enabled") else "human"
        if "threshold" in legacy:
            cfg["threshold"] = legacy["threshold"]
        if "model" in legacy:
            cfg["model"] = legacy["model"]
    else:
        cfg = dict(_REVIEWER_DEFAULTS)
    # AI review mode implies auto-merge is on.
    if cfg["mode"] == "ai":
        cfg["auto_merge"] = True
    return cfg


def is_reviewer_ai(hc_home: Path, team: str) -> bool:
    """Return True if the reviewer is set to AI mode for this team."""
    return get_reviewer_config(hc_home, team)["mode"] == "ai"


def set_reviewer_mode(hc_home: Path, team: str, mode: str) -> None:
    """Set the reviewer mode ('human' or 'ai') for a team."""
    update_reviewer_config(hc_home, team, mode=mode)


def update_reviewer_config(hc_home: Path, team: str, **kwargs) -> dict:
    """Update reviewer config keys (mode, threshold, model, auto_merge).

    When *mode* is set to ``"ai"``, *auto_merge* is forced to ``True``.
    Removes legacy ``auto_approver`` key on write.
    Returns the updated config dict.
    """
    data = _read_repos(hc_home, team)
    # Start from current config (which handles legacy fallback)
    current = dict(get_reviewer_config(hc_home, team))
    for key in ("mode", "threshold", "model", "auto_merge"):
        if key in kwargs:
            current[key] = kwargs[key]
    # AI review implies auto-merge.
    if current.get("mode") == "ai":
        current["auto_merge"] = True
    data["reviewer"] = current
    data.pop("auto_approver", None)  # remove legacy key on write
    _write_repos(hc_home, team, data)
    return dict(current)


# Deprecated aliases — remove after one release cycle

_AUTO_APPROVER_DEFAULTS = {
    "enabled": False,
    "threshold": 3.5,
    "model": "claude-sonnet-4-20250514",
}


def get_auto_approver_config(hc_home: Path, team: str) -> dict:
    """**Deprecated** — use ``get_reviewer_config`` instead."""
    cfg = get_reviewer_config(hc_home, team)
    return {"enabled": cfg["mode"] == "ai", "threshold": cfg["threshold"], "model": cfg["model"]}


def is_auto_approver_enabled(hc_home: Path, team: str) -> bool:
    """**Deprecated** — use ``is_reviewer_ai`` instead."""
    return is_reviewer_ai(hc_home, team)


def set_auto_approver_enabled(hc_home: Path, team: str, enabled: bool) -> None:
    """**Deprecated** — use ``set_reviewer_mode`` instead."""
    set_reviewer_mode(hc_home, team, "ai" if enabled else "human")


def update_auto_approver_config(hc_home: Path, team: str, **kwargs) -> dict:
    """**Deprecated** — use ``update_reviewer_config`` instead."""
    reviewer_kwargs = {}
    if "enabled" in kwargs:
        reviewer_kwargs["mode"] = "ai" if kwargs["enabled"] else "human"
    if "threshold" in kwargs:
        reviewer_kwargs["threshold"] = kwargs["threshold"]
    if "model" in kwargs:
        reviewer_kwargs["model"] = kwargs["model"]
    cfg = update_reviewer_config(hc_home, team, **reviewer_kwargs)
    return {"enabled": cfg["mode"] == "ai", "threshold": cfg["threshold"], "model": cfg["model"]}


# ---------------------------------------------------------------------------
# Task-creation freeze (per-team, stored in repos.yaml under 'task_freeze')
# ---------------------------------------------------------------------------

_TASK_FREEZE_DEFAULTS = {"enabled": False}


def get_task_freeze_config(hc_home: Path, team: str) -> dict:
    """Return the task-freeze config for a team."""
    data = _read_repos(hc_home, team)
    stored = data.get("task_freeze", {})
    return {**_TASK_FREEZE_DEFAULTS, **stored}


def is_task_creation_frozen(hc_home: Path, team: str) -> bool:
    """Return True if task creation is frozen for this team."""
    return get_task_freeze_config(hc_home, team)["enabled"]


def update_task_freeze_config(hc_home: Path, team: str, **kwargs) -> dict:
    """Update task-freeze config keys (enabled).

    Returns the updated config dict.
    """
    data = _read_repos(hc_home, team)
    current = data.get("task_freeze", {})
    if "enabled" in kwargs:
        current["enabled"] = kwargs["enabled"]
    data["task_freeze"] = current
    _write_repos(hc_home, team, data)
    return {**_TASK_FREEZE_DEFAULTS, **current}


# ---------------------------------------------------------------------------
# Max-tasks limit (per-team, stored in repos.yaml under 'max_tasks')
# ---------------------------------------------------------------------------

_MAX_TASKS_DEFAULTS = {"enabled": False, "limit_in_progress": 5, "limit_queued": 10}


def get_max_tasks_config(hc_home: Path, team: str) -> dict:
    """Return the max-tasks config for a team.

    Handles backward compatibility: if the old single ``limit`` key is
    present, it is mapped to both ``limit_in_progress`` and
    ``limit_queued`` (and removed).
    """
    data = _read_repos(hc_home, team)
    stored = data.get("max_tasks", {})

    # Migrate legacy single "limit" field
    if "limit" in stored and "limit_in_progress" not in stored:
        old = stored.pop("limit")
        stored["limit_in_progress"] = old
        stored["limit_queued"] = old
        data["max_tasks"] = stored
        _write_repos(hc_home, team, data)

    return {**_MAX_TASKS_DEFAULTS, **stored}


def update_max_tasks_config(hc_home: Path, team: str, **kwargs) -> dict:
    """Update max-tasks config keys (enabled, limit_in_progress, limit_queued).

    Returns the updated config dict.
    """
    data = _read_repos(hc_home, team)
    current = data.get("max_tasks", {})
    for key in ("enabled", "limit_in_progress", "limit_queued"):
        if key in kwargs:
            current[key] = kwargs[key]
    # Drop legacy key if present
    current.pop("limit", None)
    data["max_tasks"] = current
    _write_repos(hc_home, team, data)
    return {**_MAX_TASKS_DEFAULTS, **current}


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


# --- Repo remote_url ---

# --- Repo main_prefer_files ---

def get_main_prefer_files(hc_home: Path, team: str, repo_name: str) -> list[str]:
    """Return the list of file patterns that should always use main's version."""
    repos = get_repos(hc_home, team)
    meta = repos.get(repo_name, {})
    return meta.get("main_prefer_files", [])


def update_main_prefer_files(hc_home: Path, team: str, name: str, files: list[str]) -> None:
    """Update the main-prefer file patterns for an existing repo."""
    data = _read_repos(hc_home, team)
    if name not in data:
        raise KeyError(f"Repo '{name}' not found in team '{team}' config")
    data[name]["main_prefer_files"] = files
    _write_repos(hc_home, team, data)


def get_repo_remote_url(hc_home: Path, team: str, repo_name: str) -> str | None:
    """Return the configured remote URL for a repo, or None if not set."""
    repos = get_repos(hc_home, team)
    meta = repos.get(repo_name, {})
    return meta.get("remote_url")


def update_repo_remote_url(hc_home: Path, team: str, name: str, remote_url: str) -> None:
    """Update the remote URL for an existing repo."""
    data = _read_repos(hc_home, team)
    if name not in data:
        raise KeyError(f"Repo '{name}' not found in team '{team}' config")
    data[name]["remote_url"] = remote_url
    _write_repos(hc_home, team, data)


