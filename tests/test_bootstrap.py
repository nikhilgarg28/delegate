"""Tests for delegate/bootstrap.py."""

import sqlite3

import pytest
import yaml

from delegate.bootstrap import bootstrap, add_agent, AGENT_SUBDIRS, get_member_by_role
from delegate.config import set_boss, get_boss
from delegate.paths import (
    team_dir, agents_dir, agent_dir, db_path,
    roster_path, boss_person_dir, base_charter_dir,
)

TEAM = "testteam"


@pytest.fixture
def hc(tmp_path):
    """Return an hc_home with the boss name configured."""
    hc_home = tmp_path / "hc"
    hc_home.mkdir()
    set_boss(hc_home, "nikhil")
    return hc_home


def test_creates_directory_structure(tmp_team):
    """Bootstrap creates all expected directories for every agent."""
    hc_home = tmp_team
    td = team_dir(hc_home, TEAM)
    assert td.is_dir()
    assert agents_dir(hc_home, TEAM).is_dir()

    for name in ["manager", "alice", "bob"]:
        ad = agent_dir(hc_home, TEAM, name)
        assert ad.is_dir(), f"Missing agent dir: {name}"
        for subdir in AGENT_SUBDIRS:
            assert (ad / subdir).is_dir(), f"Missing {name}/{subdir}"


def test_creates_starter_files(tmp_team):
    """Bootstrap creates all expected files with content."""
    hc_home = tmp_team
    assert roster_path(hc_home, TEAM).is_file()
    assert db_path(hc_home, TEAM).is_file()

    for name in ["manager", "alice", "bob"]:
        ad = agent_dir(hc_home, TEAM, name)
        assert (ad / "bio.md").is_file()
        assert (ad / "context.md").is_file()
        assert (ad / "state.yaml").is_file()


def test_state_yaml_has_role(tmp_team):
    """Each agent's state.yaml includes the correct role."""
    hc_home = tmp_team
    state = yaml.safe_load((agent_dir(hc_home, TEAM, "manager") / "state.yaml").read_text())
    assert state["role"] == "manager"
    assert state["pid"] is None

    state = yaml.safe_load((agent_dir(hc_home, TEAM, "alice") / "state.yaml").read_text())
    assert state["role"] == "engineer"


def test_boss_directory_created(tmp_team):
    """The boss's global directory is created outside any team."""
    hc_home = tmp_team
    bd = boss_person_dir(hc_home)
    assert bd.is_dir()


def test_roster_contains_all_members(tmp_team):
    """Roster file lists every team member."""
    content = roster_path(tmp_team, TEAM).read_text()
    for name in ["manager", "alice", "bob", "nikhil"]:
        assert name in content


def test_roster_shows_roles(tmp_team):
    """Roster shows role annotations for all members."""
    content = roster_path(tmp_team, TEAM).read_text()
    assert "(manager)" in content
    assert "(boss)" in content
    assert "(engineer)" in content


def test_charter_shipped_with_package():
    """Base charter files are shipped with the package."""
    cd = base_charter_dir()
    assert cd.is_dir()
    expected_top = {"values.md", "communication.md", "task-management.md", "code-review.md", "continuous-improvement.md"}
    actual = {f.name for f in cd.glob("*.md")}
    assert actual == expected_top
    for f in cd.glob("*.md"):
        assert len(f.read_text()) > 0

    # Role-specific charter files live in roles/
    roles_dir = cd / "roles"
    assert roles_dir.is_dir()
    required_roles = {"manager.md", "engineer.md", "designer.md", "qa.md"}
    actual_roles = {f.name for f in roles_dir.glob("*.md")}
    assert required_roles.issubset(actual_roles), f"Missing role files: {required_roles - actual_roles}"


def test_agent_subdirs_exist(tmp_team):
    """Each agent has journals/notes/workspace/worktrees subdirectories."""
    hc_home = tmp_team
    for name in ["manager", "alice", "bob"]:
        for subdir in ["journals", "notes", "workspace", "worktrees"]:
            path = agent_dir(hc_home, TEAM, name) / subdir
            assert path.is_dir(), f"Missing {name}/{subdir}"


def test_workspace_exists_per_agent(tmp_team):
    """Each team agent has a workspace directory."""
    hc_home = tmp_team
    for name in ["manager", "alice", "bob"]:
        assert (agent_dir(hc_home, TEAM, name) / "workspace").is_dir()


def test_db_schema_created(tmp_team):
    """SQLite database has the messages and sessions tables."""
    conn = sqlite3.connect(str(db_path(tmp_team, TEAM)))

    cursor = conn.execute("PRAGMA table_info(messages)")
    msg_columns = {row[1] for row in cursor.fetchall()}
    # V9 added delivered_at, seen_at, processed_at for unified mailbox/messages table
    assert msg_columns == {"id", "timestamp", "sender", "recipient", "content", "type", "task_id", "delivered_at", "seen_at", "processed_at"}

    cursor = conn.execute("PRAGMA table_info(sessions)")
    sess_columns = {row[1] for row in cursor.fetchall()}
    assert sess_columns == {
        "id", "agent", "task_id", "started_at", "ended_at",
        "duration_seconds", "tokens_in", "tokens_out", "cost_usd",
        "cache_read_tokens", "cache_write_tokens",
    }

    conn.close()


def test_idempotent_rerun(hc):
    """Running bootstrap twice doesn't corrupt existing files."""
    bootstrap(hc, TEAM, manager="mgr", agents=["a", "b"])
    bootstrap(hc, TEAM, manager="mgr", agents=["a", "b"])

    for name in ["mgr", "a", "b"]:
        assert (agent_dir(hc, TEAM, name) / "state.yaml").is_file()


def test_bio_default_content(tmp_team):
    """Each agent's bio.md has their name as a simple placeholder."""
    hc_home = tmp_team
    for name in ["manager", "alice", "bob"]:
        content = (agent_dir(hc_home, TEAM, name) / "bio.md").read_text()
        assert name in content
        assert content.strip() == f"# {name}"


def test_get_member_by_role(tmp_team):
    """get_member_by_role finds the correct member for each role."""
    assert get_member_by_role(tmp_team, TEAM, "manager") == "manager"
    assert get_member_by_role(tmp_team, TEAM, "nonexistent") is None


def test_get_member_by_role_custom_names(hc):
    """get_member_by_role works with custom names."""
    bootstrap(hc, TEAM, manager="edison", agents=["alice"])
    assert get_member_by_role(hc, TEAM, "manager") == "edison"


def test_duplicate_names_raises(hc):
    """Bootstrap rejects duplicate member names."""
    with pytest.raises(ValueError, match="Duplicate"):
        bootstrap(hc, TEAM, manager="alice", agents=["alice"])


def test_interactive_bios(tmp_path, monkeypatch):
    """Interactive mode prompts for bios and writes them."""
    hc_home = tmp_path / "hc"
    hc_home.mkdir()
    set_boss(hc_home, "nikhil")

    # Order: additional charter prompt first, then bios for each member
    inputs = iter([
        "",                   # no additional charter
        "Great at planning",  # manager bio line 1
        "",                   # end manager bio
        "Python expert",      # alice bio line 1
        "",                   # end alice bio
    ])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    bootstrap(hc_home, TEAM, manager="mgr", agents=["alice"], interactive=True)

    assert "Great at planning" in (agent_dir(hc_home, TEAM, "mgr") / "bio.md").read_text()
    assert "Python expert" in (agent_dir(hc_home, TEAM, "alice") / "bio.md").read_text()


def test_interactive_extra_charter(tmp_path, monkeypatch):
    """Interactive mode can add additional charter material."""
    hc_home = tmp_path / "hc"
    hc_home.mkdir()
    set_boss(hc_home, "nikhil")

    # Order: additional charter prompt first, then bios
    inputs = iter([
        "We use Rust for infrastructure",  # extra charter line 1
        "",                                 # end extra charter
        "",                                 # empty bio for manager
    ])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    bootstrap(hc_home, TEAM, manager="mgr", agents=[], interactive=True)

    override = team_dir(hc_home, TEAM) / "override.md"
    assert override.exists()
    assert "Rust for infrastructure" in override.read_text()


# ──────────────────────────────────────────────────────────────
# add_agent tests
# ──────────────────────────────────────────────────────────────

def test_add_agent_creates_directory_structure(tmp_team):
    """add_agent creates all expected directories for the new agent."""
    add_agent(tmp_team, TEAM, "charlie")
    ad = agent_dir(tmp_team, TEAM, "charlie")
    assert ad.is_dir()
    for subdir in AGENT_SUBDIRS:
        assert (ad / subdir).is_dir(), f"Missing charlie/{subdir}"


def test_add_agent_creates_starter_files(tmp_team):
    """add_agent creates bio.md, context.md, and state.yaml."""
    add_agent(tmp_team, TEAM, "charlie")
    ad = agent_dir(tmp_team, TEAM, "charlie")
    assert (ad / "bio.md").is_file()
    assert (ad / "context.md").is_file()
    assert (ad / "state.yaml").is_file()


def test_add_agent_default_role(tmp_team):
    """add_agent defaults to 'engineer' role in state.yaml."""
    add_agent(tmp_team, TEAM, "charlie")
    state = yaml.safe_load(
        (agent_dir(tmp_team, TEAM, "charlie") / "state.yaml").read_text()
    )
    assert state["role"] == "engineer"
    assert state["pid"] is None


def test_add_agent_custom_role(tmp_team):
    """add_agent stores a custom role in state.yaml."""
    add_agent(tmp_team, TEAM, "charlie", role="designer")
    state = yaml.safe_load(
        (agent_dir(tmp_team, TEAM, "charlie") / "state.yaml").read_text()
    )
    assert state["role"] == "designer"


def test_add_agent_bio_written(tmp_team):
    """add_agent writes the provided bio text into bio.md."""
    add_agent(tmp_team, TEAM, "charlie", bio="Expert in testing")
    content = (agent_dir(tmp_team, TEAM, "charlie") / "bio.md").read_text()
    assert "# charlie" in content
    assert "Expert in testing" in content


def test_add_agent_bio_placeholder(tmp_team):
    """add_agent writes a placeholder bio when no bio is given."""
    add_agent(tmp_team, TEAM, "charlie")
    content = (agent_dir(tmp_team, TEAM, "charlie") / "bio.md").read_text()
    assert content.strip() == "# charlie"


def test_add_agent_appends_to_roster(tmp_team):
    """add_agent appends the new agent to roster.md."""
    add_agent(tmp_team, TEAM, "charlie")
    content = roster_path(tmp_team, TEAM).read_text()
    assert "charlie" in content
    # Original members still present
    for name in ["manager", "alice", "bob"]:
        assert name in content


def test_add_agent_roster_shows_special_role(tmp_team):
    """add_agent annotates designer/qa roles in roster.md."""
    add_agent(tmp_team, TEAM, "charlie", role="designer")
    content = roster_path(tmp_team, TEAM).read_text()
    assert "(designer)" in content


def test_add_agent_rejects_duplicate_on_team(tmp_team):
    """add_agent errors if the agent already exists on the team."""
    with pytest.raises(ValueError, match="already exists"):
        add_agent(tmp_team, TEAM, "alice")


def test_add_agent_rejects_boss_name(tmp_team):
    """add_agent errors if the name conflicts with the boss name."""
    with pytest.raises(ValueError, match="boss name"):
        add_agent(tmp_team, TEAM, "nikhil")


def test_add_agent_allows_cross_team_same_name(hc):
    """add_agent allows the same name on a different team."""
    bootstrap(hc, "team1", manager="mgr1", agents=["alice"])
    bootstrap(hc, "team2", manager="mgr2", agents=["bob"])
    # "alice" is already on team1 — should be fine on team2
    add_agent(hc, "team2", "alice")
    assert (agents_dir(hc, "team2") / "alice").is_dir()


def test_add_agent_team_not_found(hc):
    """add_agent errors if the team doesn't exist."""
    with pytest.raises(FileNotFoundError, match="does not exist"):
        add_agent(hc, "nonexistent", "charlie")
