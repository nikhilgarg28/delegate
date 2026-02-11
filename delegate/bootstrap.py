"""Bootstrap a new delegate team.

Creates the team directory structure under ``~/.delegate/teams/<team_name>/``.
The boss is NOT created as an agent — they are configured org-wide in config.yaml.

Usage:
    python -m delegate.bootstrap <home> <team_name> --manager edison --agents alice,bob [--qa sarah]
"""

import argparse
import subprocess
import uuid
from pathlib import Path

import yaml

from delegate.db import ensure_schema
from delegate.paths import (
    team_dir as _team_dir,
    team_id_path as _team_id_path,
    teams_dir as _teams_dir,
    agents_dir as _agents_dir,
    repos_dir as _repos_dir,
    roster_path as _roster_path,
    boss_person_dir as _boss_person_dir,
    base_charter_dir,
)
from delegate.config import get_boss


def _detect_boss_name() -> str:
    """Auto-detect boss name from ``git config user.name``.

    Returns the first name lowercased (e.g. "Nikhil Gupta" → "nikhil").
    Falls back to ``"boss"`` if git config is not set or fails.
    """
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, timeout=5,
        )
        full_name = result.stdout.strip()
        if full_name:
            first = full_name.split()[0].lower()
            # Sanitize: only keep alphanumeric chars
            first = "".join(c for c in first if c.isalnum())
            return first if first else "boss"
    except Exception:
        pass
    return "boss"


AGENT_SUBDIRS = [
    "journals",
    "notes",
    "feedback",
    "logs",
    "workspace",
    "worktrees",
]


def _default_seniority(role: str) -> str:
    """Manager defaults to senior; all other roles default to junior."""
    return "senior" if role == "manager" else "junior"


def _default_state(role: str, seniority: str | None = None) -> dict:
    if seniority is None:
        seniority = _default_seniority(role)
    return {"role": role, "seniority": seniority, "pid": None, "token_budget": None}


def make_roster(members: list[tuple[str, str]], boss: str | None = None) -> str:
    """Generate roster.md content from a list of (name, role) pairs."""
    lines = ["# Team Roster\n"]
    if boss:
        lines.append(f"- **{boss}** (boss)")
    for name, role in members:
        lines.append(f"- **{name}** ({role})")
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
    """Interactively prompt for additional charter material (team override)."""
    print("\n--- Team charter overrides ---")
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


def _get_all_agent_names(hc_home: Path, exclude_team: str | None = None) -> set[str]:
    """Get all agent names across all existing teams.

    Args:
        hc_home: Delegate home directory.
        exclude_team: Team to exclude (e.g. the team being created/updated).

    Returns:
        Set of agent name strings.
    """
    names: set[str] = set()
    td = _teams_dir(hc_home)
    if not td.is_dir():
        return names
    for team_d in td.iterdir():
        if not team_d.is_dir():
            continue
        if exclude_team and team_d.name == exclude_team:
            continue
        agents_d = team_d / "agents"
        if agents_d.is_dir():
            names.update(d.name for d in agents_d.iterdir() if d.is_dir())
    return names


def bootstrap(
    hc_home: Path,
    team_name: str,
    manager: str = "manager",
    agents: list[tuple[str, str]] | list[str] | None = None,
    interactive: bool = False,
) -> None:
    """Create the team directory structure under ``hc_home/teams/<team_name>/``.

    The boss is NOT an agent — they are configured org-wide in config.yaml.
    The boss's mailbox lives at ``hc_home/boss/`` (outside any team).
    Base charter files are NOT copied — they are read from the installed package.

    Agent names must be globally unique across all teams.

    Args:
        hc_home: Delegate home directory (~/.delegate).
        team_name: Name for the new team.
        manager: Name of the manager agent.
        agents: List of ``(name, role)`` tuples **or** plain name strings
            (which default to role ``"engineer"``).
        interactive: If True, prompt for bios and charter overrides.

    Safe to call multiple times — does not overwrite existing files.
    """
    raw_agents = agents or []

    # Build the complete member list as (name, role) pairs
    members: list[tuple[str, str]] = [
        (manager, "manager"),
    ]
    for a in raw_agents:
        if isinstance(a, str):
            members.append((a, "engineer"))
        else:
            members.append((a[0], a[1]))

    # Check for duplicate names within this team
    names = [n for n, _ in members]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate names in team: {names}")

    # Ensure agent names don't conflict with the boss
    boss_name = get_boss(hc_home)
    if boss_name and boss_name in names:
        raise ValueError(
            f"Agent name '{boss_name}' conflicts with the org-wide boss name"
        )

    # Enforce globally unique names across all teams
    existing = _get_all_agent_names(hc_home, exclude_team=team_name)
    overlap = set(names) & existing
    if overlap:
        raise ValueError(
            f"Agent names already used in other teams: {overlap}"
        )

    # Ensure top-level directories exist
    hc_home.mkdir(parents=True, exist_ok=True)

    # Team directory
    td = _team_dir(hc_home, team_name)
    td.mkdir(parents=True, exist_ok=True)

    # Generate a unique team instance ID (6-char hex) if not already present.
    # This ID is embedded in branch names to avoid collisions when a team
    # is deleted and recreated with the same name.
    tid_path = _team_id_path(hc_home, team_name)
    if not tid_path.exists():
        tid_path.write_text(uuid.uuid4().hex[:6] + "\n")

    # Per-team repos directory
    _repos_dir(hc_home, team_name).mkdir(parents=True, exist_ok=True)

    # Interactive: charter override
    if interactive:
        extra = _prompt_extra_charter()
        if extra:
            override_path = td / "override.md"
            if not override_path.exists():
                override_path.write_text(f"# Team Charter Overrides\n\n{extra}\n")

    # Roster — include boss name from config if set
    boss_name = get_boss(hc_home)
    rp = _roster_path(hc_home, team_name)
    if not rp.exists():
        rp.write_text(make_roster(members, boss=boss_name))

    # Scripts dir (for user-defined team scripts)
    (td / "scripts").mkdir(exist_ok=True)

    # --- Agent bossies ---
    agents_root = _agents_dir(hc_home, team_name)
    agents_root.mkdir(parents=True, exist_ok=True)

    for name, role in members:
        member_dir = agents_root / name
        member_dir.mkdir(exist_ok=True)

        # All subdirs (journals/logs/workspace/worktrees/etc.)
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

    # --- Per-team SQLite DB ---
    ensure_schema(hc_home, team_name)

    # --- Boss mailbox (org-wide, outside any team) ---
    # Ensure a boss name is configured.
    # Auto-detect from git config user.name (first name, lowercased), fall back to "boss".
    from delegate.config import set_boss
    boss_name = get_boss(hc_home)
    if not boss_name:
        boss_name = _detect_boss_name()
        set_boss(hc_home, boss_name)

    dd = _boss_person_dir(hc_home)
    dd.mkdir(parents=True, exist_ok=True)


def add_agent(
    hc_home: Path,
    team_name: str,
    agent_name: str,
    role: str = "engineer",
    seniority: str | None = None,
    bio: str | None = None,
) -> None:
    """Add a new agent to an existing team.

    Creates the full agent directory structure (all AGENT_SUBDIRS),
    state.yaml, bio.md, and context.md.  Appends the agent to the
    team's roster.md.

    Args:
        hc_home: Delegate home directory (~/.delegate).
        team_name: Name of the existing team.
        agent_name: Name for the new agent.
        role: Agent role (default ``"engineer"``).
        seniority: ``"junior"`` or ``"senior"``.  Defaults based on role
            (manager → senior, others → junior).
        bio: Optional bio text.  If omitted a placeholder is written.

    Raises:
        FileNotFoundError: If the team does not exist.
        ValueError: If the agent name already exists on this team,
            collides with another team's agent, or matches the boss name.
    """
    if seniority is None:
        seniority = _default_seniority(role)
    if seniority not in ("junior", "senior"):
        raise ValueError(f"Invalid seniority '{seniority}'. Must be 'junior' or 'senior'.")
    td = _team_dir(hc_home, team_name)
    if not td.is_dir():
        raise FileNotFoundError(f"Team '{team_name}' does not exist")

    agents_root = _agents_dir(hc_home, team_name)
    member_dir = agents_root / agent_name

    # --- name validation ---
    if member_dir.exists():
        raise ValueError(f"Agent '{agent_name}' already exists on team '{team_name}'")

    boss_name = get_boss(hc_home)
    if boss_name and agent_name == boss_name:
        raise ValueError(
            f"Agent name '{agent_name}' conflicts with the org-wide boss name"
        )

    existing = _get_all_agent_names(hc_home, exclude_team=team_name)
    if agent_name in existing:
        raise ValueError(
            f"Agent name '{agent_name}' already used in another team"
        )

    # --- create directory structure ---
    member_dir.mkdir(parents=True, exist_ok=True)
    for subdir in AGENT_SUBDIRS:
        (member_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Bio
    bio_file = member_dir / "bio.md"
    if bio:
        bio_file.write_text(f"# {agent_name}\n\n{bio}\n")
    else:
        bio_file.write_text(f"# {agent_name}\n")

    # Context
    (member_dir / "context.md").write_text("")

    # State
    (member_dir / "state.yaml").write_text(
        yaml.dump(_default_state(role, seniority), default_flow_style=False)
    )

    # --- append to roster.md ---
    rp = _roster_path(hc_home, team_name)
    if rp.exists():
        roster_text = rp.read_text()
    else:
        roster_text = "# Team Roster\n"

    # Build the roster line — always show the role
    roster_line = f"- **{agent_name}** ({role})"

    # Ensure trailing newline before appending
    if not roster_text.endswith("\n"):
        roster_text += "\n"
    roster_text += roster_line + "\n"
    rp.write_text(roster_text)


def get_member_by_role(hc_home: Path, team: str, role: str) -> str | None:
    """Find the team member name with the given role.

    Returns the name (directory basename) or None if not found.
    """
    agents_root = _agents_dir(hc_home, team)
    if not agents_root.is_dir():
        return None
    for d in sorted(agents_root.iterdir()):
        if not d.is_dir():
            continue
        state_file = d / "state.yaml"
        if state_file.exists():
            state = yaml.safe_load(state_file.read_text()) or {}
            if state.get("role") == role:
                return d.name
    return None


def main():
    parser = argparse.ArgumentParser(description="Bootstrap a delegate team")
    parser.add_argument("home", type=Path, help="Delegate home directory (~/.delegate)")
    parser.add_argument("team_name", help="Name for the team")
    parser.add_argument(
        "--manager", required=True,
        help="Name of the manager agent (e.g. edison)",
    )
    parser.add_argument(
        "--agents", default="",
        help="Comma-separated list of worker agent names (e.g. alice,bob)",
    )
    parser.add_argument(
        "--qa", default=None,
        help="Name of the QA agent (e.g. sarah)",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Prompt for bios and team charter overrides",
    )
    args = parser.parse_args()
    worker_agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    bootstrap(
        args.home,
        team_name=args.team_name,
        manager=args.manager,
        agents=worker_agents,
        qa=args.qa,
        interactive=args.interactive,
    )
    all_names = [args.manager] + (["(qa) " + args.qa] if args.qa else []) + worker_agents
    print(f"Bootstrapped team '{args.team_name}' with members: {', '.join(all_names)}")


if __name__ == "__main__":
    main()
