"""Bootstrap a new standup team directory structure.

Usage:
    python -m scripts.bootstrap <root_dir> --manager edison --director nikhil --agents alice,bob
    python -m scripts.bootstrap <root_dir> --manager edison --director nikhil --agents alice,bob --interactive
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

import yaml


CHARTER_DIR = Path(__file__).parent / "charter"

MAILDIR_SUBDIRS = [
    "inbox/new",
    "inbox/cur",
    "inbox/tmp",
    "outbox/new",
    "outbox/cur",
    "outbox/tmp",
]

AGENT_SUBDIRS = MAILDIR_SUBDIRS + [
    "journals",
    "notes",
    "feedback",
    "logs",
    "workspace",
]

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    content TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('chat', 'event'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    task_id INTEGER,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at TEXT,
    duration_seconds REAL DEFAULT 0.0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0
);
"""


def _default_state(role: str) -> dict:
    return {"role": role, "pid": None, "token_budget": None}


def make_roster(members: list[tuple[str, str]]) -> str:
    """Generate roster.md content from a list of (name, role) pairs."""
    lines = ["# Team Roster\n"]
    for name, role in members:
        if role in ("manager", "director"):
            lines.append(f"- **{name}** ({role})")
        else:
            lines.append(f"- **{name}**")
    lines.append("")
    return "\n".join(lines)


def _prompt_bio(name: str, role: str) -> str:
    """Interactively prompt for a team member's bio."""
    print(f"\n--- Bio for {name} (role: {role}) ---")
    print("Enter background, interests, strengths (empty line to finish):")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    body = "\n".join(lines).strip()
    if not body:
        return f"# {name}\n"
    return f"# {name}\n\n{body}\n"


def _prompt_extra_charter() -> str | None:
    """Interactively prompt for additional charter material."""
    print("\n--- Additional charter material ---")
    print("Enter any extra guidelines for your team (empty line to finish, or just press Enter to skip):")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    body = "\n".join(lines).strip()
    return body if body else None


def bootstrap(
    root: Path,
    manager: str = "manager",
    director: str = "director",
    agents: list[str] | None = None,
    interactive: bool = False,
) -> None:
    """Create the full team directory structure under ``root/.standup``.

    Args:
        root: Team root directory.
        manager: Name of the person/agent who will act as manager.
        director: Name of the human director.
        agents: Additional agent (worker) names.
        interactive: If True, prompt for bios and additional charter material.

    Safe to call multiple times — does not overwrite existing files.
    """
    agents = agents or []

    # Build the complete member list as (name, role) pairs
    members: list[tuple[str, str]] = [
        (manager, "manager"),
        (director, "director"),
    ]
    for a in agents:
        members.append((a, "worker"))

    # Check for duplicate names
    names = [n for n, _ in members]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate names in team: {names}")

    root.mkdir(parents=True, exist_ok=True)

    # --- .standup ---
    standup = root / ".standup"
    standup.mkdir(parents=True, exist_ok=True)

    # Charter directory — copy the whole source directory
    charter_dest = standup / "charter"
    if not charter_dest.exists() and CHARTER_DIR.is_dir():
        shutil.copytree(CHARTER_DIR, charter_dest)
    elif CHARTER_DIR.is_dir():
        # Idempotent: copy missing files only
        for tmpl in sorted(CHARTER_DIR.glob("*.md")):
            dest = charter_dest / tmpl.name
            if not dest.exists():
                dest.write_text(tmpl.read_text())

    # Interactive: additional charter material
    if interactive:
        extra = _prompt_extra_charter()
        if extra:
            extra_path = charter_dest / "additional.md"
            if not extra_path.exists():
                extra_path.write_text(f"# Additional Guidelines\n\n{extra}\n")

    # Roster
    roster = standup / "roster.md"
    if not roster.exists():
        roster.write_text(make_roster(members))

    # Scripts dir (for user-defined team scripts)
    (standup / "scripts").mkdir(exist_ok=True)

    # Tasks dir
    (standup / "tasks").mkdir(exist_ok=True)

    # --- Team member directories ---
    team_dir = standup / "team"
    team_dir.mkdir(exist_ok=True)

    for name, role in members:
        member_dir = team_dir / name
        member_dir.mkdir(exist_ok=True)

        # All subdirs (Maildir + journals/logs/workspace/etc.)
        for subdir in AGENT_SUBDIRS:
            (member_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Bio
        bio = member_dir / "bio.md"
        if not bio.exists():
            if interactive:
                bio.write_text(_prompt_bio(name, role))
            else:
                bio.write_text(f"# {name}\n")

        # Context
        context = member_dir / "context.md"
        if not context.exists():
            context.write_text("")

        # State — includes role
        state_file = member_dir / "state.yaml"
        if not state_file.exists():
            state_file.write_text(yaml.dump(_default_state(role), default_flow_style=False))

    # --- SQLite DB ---
    db_path = standup / "db.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(DB_SCHEMA)
    conn.close()


def get_member_by_role(root: Path, role: str) -> str | None:
    """Find the team member name with the given role.

    Returns the name (directory basename) or None if not found.
    """
    team_dir = root / ".standup" / "team"
    if not team_dir.is_dir():
        return None
    for d in sorted(team_dir.iterdir()):
        if not d.is_dir():
            continue
        state_file = d / "state.yaml"
        if state_file.exists():
            state = yaml.safe_load(state_file.read_text()) or {}
            if state.get("role") == role:
                return d.name
    return None


def main():
    parser = argparse.ArgumentParser(description="Bootstrap a standup team")
    parser.add_argument("root", type=Path, help="Root directory for the team")
    parser.add_argument(
        "--manager", required=True,
        help="Name of the manager agent (e.g. edison)",
    )
    parser.add_argument(
        "--director", required=True,
        help="Name of the human director (e.g. nikhil)",
    )
    parser.add_argument(
        "--agents", default="",
        help="Comma-separated list of worker agent names (e.g. alice,bob)",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Prompt for bios and additional charter material",
    )
    args = parser.parse_args()
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    all_names = [args.manager, args.director] + agents
    bootstrap(
        args.root,
        manager=args.manager,
        director=args.director,
        agents=agents,
        interactive=args.interactive,
    )
    print(f"Bootstrapped team at {args.root} with members: {', '.join(all_names)}")


if __name__ == "__main__":
    main()
