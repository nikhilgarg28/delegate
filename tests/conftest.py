"""Shared test fixtures for standup tests."""

from pathlib import Path

import pytest

from scripts.bootstrap import bootstrap


SAMPLE_MANAGER = "manager"
SAMPLE_DIRECTOR = "director"
SAMPLE_WORKERS = ["alice", "bob"]


@pytest.fixture
def sample_agents():
    """Return a standard list of all agent (non-director) names for testing."""
    return [SAMPLE_MANAGER] + list(SAMPLE_WORKERS)


@pytest.fixture
def all_members():
    """Return all member names including director."""
    return [SAMPLE_MANAGER, SAMPLE_DIRECTOR] + list(SAMPLE_WORKERS)


@pytest.fixture
def tmp_team(tmp_path):
    """Create a fully bootstrapped team directory tree in a temp folder.

    Returns the root path. Every test gets an isolated, disposable team.
    Uses the real bootstrap() function.
    """
    root = tmp_path / "team"
    bootstrap(root, manager=SAMPLE_MANAGER, director=SAMPLE_DIRECTOR, agents=SAMPLE_WORKERS)
    return root


@pytest.fixture
def standup_path(tmp_team):
    """Return the .standup path within a tmp_team."""
    return tmp_team / ".standup"


@pytest.fixture
def team_path(standup_path):
    """Return the team directory path within .standup."""
    return standup_path / "team"


@pytest.fixture
def db_path(standup_path):
    """Return the SQLite database path within .standup."""
    return standup_path / "db.sqlite"
