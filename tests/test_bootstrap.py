"""Tests for scripts/bootstrap.py."""

import sqlite3

import yaml

from scripts.bootstrap import bootstrap, AGENT_SUBDIRS, get_member_by_role


def test_creates_directory_structure(tmp_team, all_members):
    """Bootstrap creates all expected directories for every member."""
    standup = tmp_team / ".standup"
    assert standup.is_dir()
    assert (standup / "scripts").is_dir()
    assert (standup / "tasks").is_dir()
    assert (standup / "team").is_dir()

    for name in all_members:
        member_dir = standup / "team" / name
        assert member_dir.is_dir(), f"Missing member dir: {name}"
        for subdir in AGENT_SUBDIRS:
            assert (member_dir / subdir).is_dir(), f"Missing {name}/{subdir}"


def test_creates_starter_files(tmp_team, all_members):
    """Bootstrap creates all expected files with content."""
    standup = tmp_team / ".standup"
    assert (standup / "charter").is_dir()
    assert (standup / "roster.md").is_file()
    assert (standup / "db.sqlite").is_file()

    for name in all_members:
        member_dir = standup / "team" / name
        assert (member_dir / "bio.md").is_file()
        assert (member_dir / "context.md").is_file()
        assert (member_dir / "state.yaml").is_file()


def test_state_yaml_has_role(team_path):
    """Each member's state.yaml includes the correct role."""
    state = yaml.safe_load((team_path / "manager" / "state.yaml").read_text())
    assert state["role"] == "manager"
    assert state["pid"] is None

    state = yaml.safe_load((team_path / "director" / "state.yaml").read_text())
    assert state["role"] == "director"

    state = yaml.safe_load((team_path / "alice" / "state.yaml").read_text())
    assert state["role"] == "worker"


def test_roster_contains_all_members(standup_path, all_members):
    """Roster file lists every team member."""
    content = (standup_path / "roster.md").read_text()
    for name in all_members:
        assert name in content


def test_roster_shows_roles(standup_path):
    """Roster shows role annotations for manager and director."""
    content = (standup_path / "roster.md").read_text()
    assert "(manager)" in content
    assert "(director)" in content


def test_charter_directory(standup_path):
    """Charter directory contains the expected files (copied from scripts/charter/)."""
    charter_dir = standup_path / "charter"
    assert charter_dir.is_dir()
    expected = {"constitution.md", "communication.md", "task-management.md", "code-review.md", "manager.md"}
    actual = {f.name for f in charter_dir.glob("*.md")}
    assert actual == expected
    # Each file should have content
    for f in charter_dir.glob("*.md"):
        assert len(f.read_text()) > 0


def test_maildir_subdirs_exist(team_path, all_members):
    """Each member has Maildir-style new/cur/tmp under inbox and outbox."""
    for name in all_members:
        for box in ["inbox", "outbox"]:
            for sub in ["new", "cur", "tmp"]:
                path = team_path / name / box / sub
                assert path.is_dir(), f"Missing {name}/{box}/{sub}"


def test_workspace_exists_per_member(team_path, all_members):
    """Each team member has a workspace directory."""
    for name in all_members:
        assert (team_path / name / "workspace").is_dir()


def test_db_schema_created(db_path):
    """SQLite database has the messages and sessions tables."""
    conn = sqlite3.connect(str(db_path))

    cursor = conn.execute("PRAGMA table_info(messages)")
    msg_columns = {row[1] for row in cursor.fetchall()}
    assert msg_columns == {"id", "timestamp", "sender", "recipient", "content", "type"}

    cursor = conn.execute("PRAGMA table_info(sessions)")
    sess_columns = {row[1] for row in cursor.fetchall()}
    assert sess_columns == {
        "id", "agent", "task_id", "started_at", "ended_at",
        "duration_seconds", "tokens_in", "tokens_out", "cost_usd",
    }

    conn.close()


def test_idempotent_rerun(tmp_path):
    """Running bootstrap twice doesn't corrupt existing files."""
    root = tmp_path / "team"
    bootstrap(root, manager="mgr", director="dir", agents=["a", "b"])

    # Overwrite one charter file to verify idempotency
    constitution = root / ".standup" / "charter" / "constitution.md"
    constitution.write_text("# Custom Constitution\n")

    bootstrap(root, manager="mgr", director="dir", agents=["a", "b"])

    assert constitution.read_text() == "# Custom Constitution\n"
    for name in ["mgr", "dir", "a", "b"]:
        assert (root / ".standup" / "team" / name / "state.yaml").is_file()


def test_bio_default_content(team_path, all_members):
    """Each member's bio.md has their name as a simple placeholder."""
    for name in all_members:
        content = (team_path / name / "bio.md").read_text()
        assert name in content
        assert content.strip() == f"# {name}"


def test_get_member_by_role(tmp_team):
    """get_member_by_role finds the correct member for each role."""
    assert get_member_by_role(tmp_team, "manager") == "manager"
    assert get_member_by_role(tmp_team, "director") == "director"
    assert get_member_by_role(tmp_team, "nonexistent") is None


def test_get_member_by_role_custom_names(tmp_path):
    """get_member_by_role works with custom names."""
    root = tmp_path / "team"
    bootstrap(root, manager="edison", director="nikhil", agents=["alice"])
    assert get_member_by_role(root, "manager") == "edison"
    assert get_member_by_role(root, "director") == "nikhil"


def test_duplicate_names_raises(tmp_path):
    """Bootstrap rejects duplicate member names."""
    import pytest
    root = tmp_path / "team"
    with pytest.raises(ValueError, match="Duplicate"):
        bootstrap(root, manager="alice", director="bob", agents=["alice"])


def test_interactive_bios(tmp_path, monkeypatch):
    """Interactive mode prompts for bios and writes them."""
    root = tmp_path / "team"

    # Order: additional charter prompt first, then bios for each member
    inputs = iter([
        "",                   # no additional charter
        "Great at planning",  # manager bio line 1
        "",                   # end manager bio
        "Human director",     # director bio line 1
        "",                   # end director bio
        "Python expert",      # alice bio line 1
        "",                   # end alice bio
    ])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    bootstrap(root, manager="mgr", director="dir", agents=["alice"], interactive=True)

    assert "Great at planning" in (root / ".standup" / "team" / "mgr" / "bio.md").read_text()
    assert "Human director" in (root / ".standup" / "team" / "dir" / "bio.md").read_text()
    assert "Python expert" in (root / ".standup" / "team" / "alice" / "bio.md").read_text()


def test_interactive_extra_charter(tmp_path, monkeypatch):
    """Interactive mode can add additional charter material."""
    root = tmp_path / "team"

    # Order: additional charter prompt first, then bios
    inputs = iter([
        "We use Rust for infrastructure",  # extra charter line 1
        "",                                 # end extra charter
        "",                                 # empty bio for manager
        "",                                 # empty bio for director
    ])
    monkeypatch.setattr("builtins.input", lambda: next(inputs))

    bootstrap(root, manager="mgr", director="dir", agents=[], interactive=True)

    extra = root / ".standup" / "charter" / "additional.md"
    assert extra.exists()
    assert "Rust for infrastructure" in extra.read_text()
