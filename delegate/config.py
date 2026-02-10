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


# --- Repo pipeline ---

def get_repo_pipeline(hc_home: Path, team: str, repo_name: str) -> list[dict] | None:
    """Return the configured pipeline for a repo, or None if not set.

    If the repo has a ``pipeline`` field, returns it directly.
    If no ``pipeline`` is set but a legacy ``test_cmd`` exists,
    returns a single-step pipeline wrapping it for backward compatibility.

    Returns None when neither pipeline nor test_cmd is configured.
    """
    repos = get_repos(hc_home, team)
    meta = repos.get(repo_name, {})

    pipeline = meta.get("pipeline")
    if pipeline is not None:
        return pipeline

    # Backward compat: wrap legacy test_cmd as a single-step pipeline
    test_cmd = meta.get("test_cmd")
    if test_cmd:
        return [{"name": "test", "run": test_cmd}]

    return None


def set_repo_pipeline(hc_home: Path, team: str, name: str, pipeline: list[dict]) -> None:
    """Set the full pipeline for an existing repo."""
    data = _read_repos(hc_home, team)
    if name not in data:
        raise KeyError(f"Repo '{name}' not found in team '{team}' config")
    data[name]["pipeline"] = pipeline
    _write_repos(hc_home, team, data)


def add_pipeline_step(hc_home: Path, team: str, repo_name: str, step_name: str, run: str) -> None:
    """Append a named step to a repo's pipeline.

    If the repo doesn't have a pipeline yet, creates one.  If it has a
    legacy ``test_cmd`` but no pipeline, migrates the test_cmd into the
    pipeline first.

    Raises:
        KeyError: If the repo doesn't exist.
        ValueError: If a step with the same name already exists.
    """
    data = _read_repos(hc_home, team)
    if repo_name not in data:
        raise KeyError(f"Repo '{repo_name}' not found in team '{team}' config")

    meta = data[repo_name]
    pipeline = meta.get("pipeline")

    if pipeline is None:
        # Migrate legacy test_cmd if present
        test_cmd = meta.get("test_cmd")
        if test_cmd:
            pipeline = [{"name": "test", "run": test_cmd}]
        else:
            pipeline = []

    # Check for duplicate step name
    for step in pipeline:
        if step["name"] == step_name:
            raise ValueError(f"Step '{step_name}' already exists in pipeline")

    pipeline.append({"name": step_name, "run": run})
    meta["pipeline"] = pipeline
    _write_repos(hc_home, team, data)


def remove_pipeline_step(hc_home: Path, team: str, repo_name: str, step_name: str) -> None:
    """Remove a named step from a repo's pipeline.

    Raises:
        KeyError: If the repo doesn't exist or the step is not found.
    """
    data = _read_repos(hc_home, team)
    if repo_name not in data:
        raise KeyError(f"Repo '{repo_name}' not found in team '{team}' config")

    meta = data[repo_name]
    pipeline = meta.get("pipeline")

    if pipeline is None:
        raise KeyError(f"No pipeline configured for repo '{repo_name}'")

    new_pipeline = [s for s in pipeline if s["name"] != step_name]
    if len(new_pipeline) == len(pipeline):
        raise KeyError(f"Step '{step_name}' not found in pipeline")

    meta["pipeline"] = new_pipeline
    _write_repos(hc_home, team, data)
