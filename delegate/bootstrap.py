"""Bootstrap a new delegate team.

Creates the team directory structure under ``~/.delegate/teams/<team_name>/``.
Human members are stored in ``~/.delegate/members/`` (outside any team).

Usage:
    python -m delegate.bootstrap <home> <team_name> --manager edison --agents alice,bob [--qa sarah]
"""

import argparse
import re
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
    members_dir as _members_dir,
    base_charter_dir,
    protected_dir,
    ensure_protected,
    ensure_protected_team,
    register_team_path,
    resolve_team_uuid,
    resolve_team_name,
)
from delegate.config import get_boss, get_default_human, add_member, get_human_members, rename_member

# Project name must start with a lowercase letter or digit, followed by any mix
# of lowercase letters, digits, hyphens, and underscores.

_PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PROJECT_NAME_ERROR = (
    "Project name must be lowercase letters, digits, hyphens, and underscores only"
    " (e.g. my-project-2026)"
)


def validate_project_name(name: str) -> None:
    """Raise ValueError if *name* is not a valid project/team slug.

    Valid names match ``^[a-z0-9][a-z0-9_-]*$``: start with a lowercase letter
    or digit, then any mix of lowercase letters, digits, hyphens, underscores.
    Raises ValueError with a human-readable message on failure.
    """
    if not _PROJECT_NAME_RE.match(name):
        raise ValueError(_PROJECT_NAME_ERROR)


def get_all_agent_names(hc_home: Path) -> dict[str, list[str]]:
    """Return a mapping of agent_name -> list[team_name] for all agents across all teams.

    Since agent names can now exist on multiple teams, each name maps to a list of teams.
    """
    teams_dir_path = _teams_dir(hc_home)
    result: dict[str, list[str]] = {}
    if not teams_dir_path.is_dir():
        return result
    for team_dir_obj in sorted(teams_dir_path.iterdir()):
        if not team_dir_obj.is_dir():
            continue
        agents_dir_obj = team_dir_obj / "agents"
        if not agents_dir_obj.is_dir():
            continue
        for agent_dir_obj in sorted(agents_dir_obj.iterdir()):
            if not agent_dir_obj.is_dir():
                continue
            agent_name = agent_dir_obj.name
            if agent_name not in result:
                result[agent_name] = []
            result[agent_name].append(
                resolve_team_name(hc_home, team_dir_obj.name)
            )
    return result


def get_all_member_names(hc_home: Path) -> set[str]:
    """Return all human member names."""
    return {m["name"] for m in get_human_members(hc_home)}


def _detect_human_name() -> str:
    """Auto-detect human member name.

    Priority order:
    1. ``git config user.name`` — first word, lowercased.
    2. Unix login name (``os.getlogin()`` or ``pwd.getpwuid``).
    3. ``"boss"`` — final fallback (never ``"human"``).

    Returns the first name lowercased (e.g. "Nikhil Gupta" → "nikhil").
    """
    import os
    import pwd

    # 1. git config user.name
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
            if first:
                return first
    except Exception:
        pass

    # 2. Unix login name
    try:
        login = os.getlogin()
        if login:
            sanitized = "".join(c for c in login.lower() if c.isalnum())
            if sanitized:
                return sanitized
    except Exception:
        pass
    try:
        pw_name = pwd.getpwuid(os.getuid()).pw_name
        if pw_name:
            sanitized = "".join(c for c in pw_name.lower() if c.isalnum())
            if sanitized:
                return sanitized
    except Exception:
        pass

    # 3. Final fallback — never "human"
    return "boss"


# Backward-compat alias (will be removed in a future release)
_detect_boss_name = _detect_human_name


AGENT_SUBDIRS = [
    "journals",
    "notes",
    "feedback",
    "logs",
    "workspace",
]


def _default_model(role: str) -> str:
    """All roles default to sonnet."""
    return "sonnet"


def _default_state(role: str, model: str | None = None) -> dict:
    if model is None:
        model = _default_model(role)
    return {"role": role, "model": model, "pid": None, "token_budget": None, "host": None}


def make_roster(
    members: list[tuple[str, str]],
    humans: list[str] | None = None,
    boss: str | None = None,  # deprecated, kept for backward compat
) -> str:
    """Generate roster.md content from a list of (name, role) pairs.

    Args:
        members: AI agent (name, role) pairs.
        humans: List of human member names.
        boss: Deprecated — use ``humans`` instead.
    """
    lines = ["# Team Roster\n"]
    # Human members
    if humans:
        for h in humans:
            lines.append(f"- **{h}** (human)")
    elif boss:
        lines.append(f"- **{boss}** (human)")
    # AI agents
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


def bootstrap(
    hc_home: Path,
    team_name: str,
    manager: str = "delegate",
    agents: list[tuple[str, str]] | list[str] | None = None,
    interactive: bool = False,
    models: dict[str, str] | None = None,
) -> None:
    """Create the team directory structure under ``hc_home/teams/<team_name>/``.

    Human members are stored in ``hc_home/members/`` (outside any team).
    Base charter files are NOT copied — they are read from the installed package.

    Agent names can be reused across teams but cannot conflict with human member names.

    Args:
        hc_home: Delegate home directory (~/.delegate).
        team_name: Name for the new team.
        manager: Name of the manager/delegate agent (default: ``"delegate"``).
        agents: List of ``(name, role)`` tuples **or** plain name strings
            (which default to role ``"engineer"``).
            Can also be an integer-as-string to auto-generate names.
        interactive: If True, prompt for bios and charter overrides.
        models: Optional mapping of agent name -> model (``"opus"`` or ``"sonnet"``).
            Supports a special ``"*"`` key to set a default for all agents.
            Per-agent entries override the wildcard.

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

    # Legacy check for default human - will also be caught by global check below
    # Kept for backward compatibility and to provide consistent error messages
    human_name = get_default_human(hc_home)
    if human_name and human_name in names:
        raise ValueError(
            f"Name \"{human_name}\" conflicts with a human member. Names must be globally unique."
        )

    # Check that agent names don't conflict with human members
    # (cross-team agent name duplicates are now allowed)
    existing_humans = get_all_member_names(hc_home)
    for name, _ in members:
        # Check human member conflict
        if name in existing_humans:
            raise ValueError(f"Name \"{name}\" conflicts with a human member. Names must be globally unique.")

    # Ensure top-level directories exist
    hc_home.mkdir(parents=True, exist_ok=True)
    ensure_protected(hc_home)

    # Generate bootstrap_id if it doesn't exist
    bootstrap_id_path = protected_dir(hc_home) / "bootstrap_id"
    if not bootstrap_id_path.exists():
        bootstrap_id = uuid.uuid4().hex
        bootstrap_id_path.write_text(bootstrap_id + "\n")

    # Generate a unique team UUID if not already registered.
    # Must happen BEFORE creating directories so that team_dir() / protected_team_dir()
    # resolve to UUID-based paths from the start.
    existing_uuid = resolve_team_uuid(hc_home, team_name)
    if existing_uuid == team_name:
        # No UUID mapping yet — generate one
        team_uuid = uuid.uuid4().hex
        register_team_path(hc_home, team_name, team_uuid)
    else:
        team_uuid = existing_uuid

    # Team directory (working data) — now UUID-resolved
    td = _team_dir(hc_home, team_name)
    td.mkdir(parents=True, exist_ok=True)

    # Protected team directory (metadata)
    ensure_protected_team(hc_home, team_name)

    # Write short team ID for branch name compatibility
    tid_path = _team_id_path(hc_home, team_name)
    if not tid_path.exists():
        tid_path.parent.mkdir(parents=True, exist_ok=True)
        tid_path.write_text(team_uuid[:6] + "\n")

    # Register team in global projects table and project_ids translation table.
    # Always runs (INSERT OR IGNORE is idempotent) — even if the tid file
    # already exists the DB row may be missing (e.g. after a DB wipe).
    from delegate.db import get_connection
    from delegate.db_ids import register_team
    from delegate.task import _derive_prefix
    conn = get_connection(hc_home, "")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO projects (name, project_id) VALUES (?, ?)",
            (team_name, team_uuid),
        )
        register_team(conn, team_name, team_uuid=team_uuid)
        # Set the per-project prefix for task display IDs (e.g. "POLY")
        prefix = _derive_prefix(team_name)
        conn.execute(
            "UPDATE project_ids SET prefix = ? WHERE uuid = ? AND (prefix = '' OR prefix IS NULL)",
            (prefix, team_uuid),
        )
        conn.commit()
    finally:
        conn.close()

    # Per-team repos directory
    _repos_dir(hc_home, team_name).mkdir(parents=True, exist_ok=True)

    # Interactive: charter override
    if interactive:
        extra = _prompt_extra_charter()
        if extra:
            override_path = td / "override.md"
            if not override_path.exists():
                override_path.write_text(f"# Team Charter Overrides\n\n{extra}\n")

    # Roster — include all human members
    from delegate.config import get_human_members
    human_members = get_human_members(hc_home)
    human_names = [m["name"] for m in human_members]
    # Fallback: if no members dir yet, try legacy config
    if not human_names:
        legacy_name = get_boss(hc_home)
        if legacy_name:
            human_names = [legacy_name]
    rp = _roster_path(hc_home, team_name)
    if not rp.exists():
        rp.write_text(make_roster(members, humans=human_names))

    # Scripts dir (for user-defined team scripts)
    (td / "scripts").mkdir(exist_ok=True)

    # --- Agent directories ---
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

        # State — includes role and model
        state_file = member_dir / "state.yaml"
        if not state_file.exists():
            # Resolve model: per-agent override > wildcard > role default
            agent_model: str | None = None
            if models:
                agent_model = models.get(name) or models.get("*")
            state_file.write_text(yaml.dump(_default_state(role, agent_model), default_flow_style=False))

    # --- Per-team SQLite DB ---
    ensure_schema(hc_home, team_name)

    # --- Human member (org-wide, outside any team) ---
    # Ensure at least one human member exists.
    # Auto-detect from git config / Unix login, fall back to "boss".
    from delegate.config import get_human_members
    existing_members = get_human_members(hc_home)
    if not existing_members:
        # No member yet — detect the real name now
        human_name = _detect_human_name()
        add_member(hc_home, human_name)
    else:
        # Member already exists — leave it alone (idempotent)
        human_name = existing_members[0]["name"]

    # (Legacy boss dir creation removed — no backward compatibility needed.)


def add_agent(
    hc_home: Path,
    team_name: str,
    agent_name: str | None = None,
    role: str = "engineer",
    model: str | None = None,
    bio: str | None = None,
) -> None:
    """Add a new agent to an existing team.

    Creates the full agent directory structure (all AGENT_SUBDIRS),
    state.yaml, bio.md, and context.md.  Appends the agent to the
    team's roster.md.

    Args:
        hc_home: Delegate home directory (~/.delegate).
        team_name: Name of the existing team.
        agent_name: Name for the new agent. If None, a random name is generated.
        role: Agent role (default ``"engineer"``).
        model: ``"opus"`` or ``"sonnet"``.  Defaults based on role
            (manager → opus, others → sonnet).
        bio: Optional bio text.  If omitted a placeholder is written.

    Returns:
        The agent name (either the provided name or the auto-generated one).

    Raises:
        FileNotFoundError: If the team does not exist.
        ValueError: If the agent name already exists on this team
            or conflicts with a human member name.
    """
    from delegate.agent import ALLOWED_MODELS
    if model is None:
        model = _default_model(role)
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Invalid model '{model}'. Must be one of: {', '.join(ALLOWED_MODELS)}.")
    td = _team_dir(hc_home, team_name)
    if not td.is_dir():
        raise FileNotFoundError(f"Team '{team_name}' does not exist")

    # Auto-generate name if not provided
    if agent_name is None:
        from delegate.names import pick_names
        from delegate.config import get_human_members

        # Build exclusion set: existing agents + human members + manager
        exclude = {"delegate"}

        # Add all human member names
        human_members = get_human_members(hc_home)
        for member in human_members:
            exclude.add(member["name"])

        # Add all existing agent names in this team
        agents_root = _agents_dir(hc_home, team_name)
        if agents_root.is_dir():
            for agent_dir in agents_root.iterdir():
                if agent_dir.is_dir():
                    exclude.add(agent_dir.name)

        agent_name = pick_names(1, exclude)[0]

    agents_root = _agents_dir(hc_home, team_name)
    member_dir = agents_root / agent_name

    # --- name validation ---
    # Check that agent name doesn't conflict with human members
    # (cross-team agent name duplicates are now allowed)
    existing_humans = get_all_member_names(hc_home)
    if agent_name in existing_humans:
        raise ValueError(f"Name \"{agent_name}\" conflicts with a human member. Names must be globally unique.")

    # Check if agent already exists on this team (filesystem check)
    if member_dir.exists():
        raise ValueError(f"Agent '{agent_name}' already exists on team '{team_name}'")

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
        yaml.dump(_default_state(role, model), default_flow_style=False)
    )

    # --- register agent in member_ids translation table ---
    from delegate.db import get_connection
    from delegate.db_ids import register_member, resolve_team
    conn = get_connection(hc_home, "")
    try:
        team_uuid = resolve_team(conn, team_name)
        register_member(conn, "agent", team_uuid, agent_name)
        conn.commit()
    finally:
        conn.close()

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

    return agent_name


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
