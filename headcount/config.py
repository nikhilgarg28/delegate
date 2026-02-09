"""Org-wide configuration stored in ``~/.headcount/config.yaml``.

Manages:
- director name (single human director across all teams)
- source_repo path (for self-update)
- registered repos
"""

from pathlib import Path

import yaml

from headcount.paths import config_path


def _read(hc_home: Path) -> dict:
    """Read config.yaml, returning empty dict if missing."""
    cp = config_path(hc_home)
    if cp.exists():
        return yaml.safe_load(cp.read_text()) or {}
    return {}


def _write(hc_home: Path, data: dict) -> None:
    """Write config.yaml (creates parent dirs if needed)."""
    cp = config_path(hc_home)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


# --- Director ---

def get_director(hc_home: Path) -> str | None:
    """Return the org-wide director name, or None if not set."""
    return _read(hc_home).get("director")


def set_director(hc_home: Path, name: str) -> None:
    """Set the org-wide director name."""
    data = _read(hc_home)
    data["director"] = name
    _write(hc_home, data)


# --- Source repo (for self-update) ---

def get_source_repo(hc_home: Path) -> Path | None:
    """Return path to headcount's own source repo, or None."""
    val = _read(hc_home).get("source_repo")
    return Path(val) if val else None


def set_source_repo(hc_home: Path, path: Path) -> None:
    """Set the headcount source repo path."""
    data = _read(hc_home)
    data["source_repo"] = str(path)
    _write(hc_home, data)


# --- Repos ---

def get_repos(hc_home: Path) -> dict:
    """Return the repos section of config (dict of name -> metadata)."""
    return _read(hc_home).get("repos", {})


def add_repo(hc_home: Path, name: str, source: str, approval: str = "manual") -> None:
    """Register a repo in config.

    Args:
        hc_home: Headcount home directory.
        name: Repo name.
        source: Local path or remote URL.
        approval: Merge approval mode — 'auto' or 'manual' (default: 'manual').
    """
    data = _read(hc_home)
    repos = data.setdefault("repos", {})
    existing = repos.get(name, {})
    existing["source"] = source
    existing["approval"] = approval
    repos[name] = existing
    _write(hc_home, data)


def update_repo_approval(hc_home: Path, name: str, approval: str) -> None:
    """Update only the approval setting for an existing repo.

    Args:
        hc_home: Headcount home directory.
        name: Repo name (must already exist in config).
        approval: 'auto' or 'manual'.
    """
    data = _read(hc_home)
    repos = data.get("repos", {})
    if name not in repos:
        raise KeyError(f"Repo '{name}' not found in config")
    repos[name]["approval"] = approval
    _write(hc_home, data)


def get_repo_approval(hc_home: Path, repo_name: str) -> str:
    """Return the approval mode for a repo ('auto' or 'manual').

    Defaults to 'manual' if not set or repo not found.
    """
    repos = get_repos(hc_home)
    meta = repos.get(repo_name, {})
    return meta.get("approval", "manual")
