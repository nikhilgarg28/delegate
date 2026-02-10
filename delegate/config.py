"""Configuration management for Delegate.

Global config lives in ``~/.delegate/config.yaml`` (boss name, source_repo).
Per-team repo config lives in ``~/.delegate/teams/<team>/repos.yaml``.
"""

from pathlib import Path

import yaml

from delegate.paths import config_path, team_dir


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


# --- Boss ---

def get_boss(hc_home: Path) -> str | None:
    """Return the org-wide boss name, or None if not set."""
    return _read(hc_home).get("boss")


def set_boss(hc_home: Path, name: str) -> None:
    """Set the org-wide boss name."""
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
