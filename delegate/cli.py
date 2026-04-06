"""Delegate CLI entry point using Click.

Commands:
    delegate doctor                                  — verify runtime dependencies
    delegate start [--port N] [--env-file .env]       — start delegate (web UI + agents)
    delegate stop                                    — stop running delegate
    delegate status                                  — check if delegate is running
    delegate team add <name> --agents a:role,b --repo /path  — create a new team
    delegate team list                               — list existing teams
    delegate team remove <name>                      — remove a team and all its data
    delegate agent add <team> <name>                 — add an agent to a team
    delegate config set human <name>                 — set the human member name
    delegate config set boss <name>                  — (deprecated) alias for 'config set human'
    delegate config set source-repo <path>           — set delegate source repo path
    delegate repo add <team> <path_or_url> [--name]  — register a repository for a team
    delegate repo list <team>                        — list repos for a team
    delegate workflow add <team> <path>              — register a workflow for a team
    delegate workflow list <team>                    — list workflows for a team
    delegate workflow show <team> <name>             — show workflow details/graph
    delegate workflow update-actions <team> <name> <path> — update workflow actions
    delegate workflow init <team>                    — register built-in default workflow
    delegate self-update                             — update delegate from source repo
    delegate cleanup [--team X] [--max-age N] [--dry-run] — reclaim disk space (caches, logs, old data)
    delegate nuke                                    — destroy all delegate state (requires confirmation)
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

import click

from delegate.fmt import get_version
from delegate.paths import home as _home, teams_dir as _teams_dir, team_dir as _team_dir


def _get_home(ctx: click.Context) -> Path:
    """Resolve delegate home from context or default."""
    return _home(ctx.obj.get("home_override") if ctx.obj else None)


@click.group()
@click.version_option(version=get_version(), prog_name="delegate")
@click.option(
    "--home", "home_override", type=click.Path(path_type=Path), default=None,
    envvar="DELEGATE_HOME",
    help="Override delegate home directory (default: ~/.delegate).",
)
@click.pass_context
def main(ctx: click.Context, home_override: Path | None) -> None:
    """Delegate — agentic team management system."""
    ctx.ensure_object(dict)
    ctx.obj["home_override"] = home_override


# ──────────────────────────────────────────────────────────────
# delegate doctor
# ──────────────────────────────────────────────────────────────

@main.command()
def doctor() -> None:
    """Verify that all runtime dependencies are installed."""
    from delegate.doctor import run_doctor, print_doctor_report

    checks = run_doctor()
    ok = print_doctor_report(checks)
    if not ok:
        raise SystemExit(1)


# ──────────────────────────────────────────────────────────────
# delegate start / stop / status
# ──────────────────────────────────────────────────────────────

DEFAULT_PORT = 3548


def _open_ui(url: str, port: int) -> None:
    """Try to open the PWA (macOS); fall back to browser."""
    import webbrowser

    if platform.system() == "Darwin":
        app_name = "Delegate" if port == DEFAULT_PORT else f"Delegate :{port}"
        try:
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return  # PWA opened successfully
        except (subprocess.TimeoutExpired, OSError):
            pass
    # Fallback: open in browser
    try:
        webbrowser.open(url)
    except Exception:
        pass


@main.command()
@click.option("--port", type=int, default=3548, help="Port for the web UI (default: 3548).")
@click.option("--interval", type=float, default=1.0, help="Poll interval in seconds.")
@click.option("--max-concurrent", type=int, default=32, help="Max concurrent agents.")
@click.option("--token-budget", type=int, default=None, help="Default token budget per agent session.")
@click.option("--foreground", is_flag=True, help="Run in foreground instead of background.")
@click.option(
    "--env-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to .env file to load (e.g. for ANTHROPIC_API_KEY).",
)
@click.option("--dev", is_flag=True, help="Enable dev mode (esbuild watcher for live frontend rebuilds).")
@click.option("--skip-auth-check", is_flag=True, help="Skip Claude CLI/API key checks (useful for enterprise auth setups).")
@click.option("--host", type=str, default="127.0.0.1", help="Bind address (default: 127.0.0.1). Use 0.0.0.0 for network access.")
@click.pass_context
def start(
    ctx: click.Context,
    port: int,
    interval: float,
    max_concurrent: int,
    token_budget: int | None,
    foreground: bool,
    env_file: Path | None,
    dev: bool,
    skip_auth_check: bool,
    host: str,
) -> None:
    """Start delegate (web UI + agent orchestration)."""
    import time
    from delegate.daemon import start_daemon, is_running
    from delegate.doctor import run_doctor, print_doctor_report
    from delegate.fmt import success, get_auth_display, get_version

    # Load env file if provided — makes vars available to this process
    # and all child processes (daemon, agents).
    if env_file:
        from dotenv import load_dotenv
        load_dotenv(env_file)
        success(f"Loaded env file: {env_file}")

    hc_home = _get_home(ctx)

    # Migrate legacy boss config → members/ (one-time)
    from delegate.config import migrate_boss_to_member, migrate_standard_to_default_workflow
    migrated = migrate_boss_to_member(hc_home)
    if migrated:
        success(f"Migrated legacy boss '{migrated}' to members/")

    # Migrate standard → default workflow (one-time)
    wf_migrated = migrate_standard_to_default_workflow(hc_home)
    if wf_migrated:
        success(f"Migrated {wf_migrated} team(s) from 'standard' to 'default' workflow")

    # Run doctor check first — suppress output if all checks pass
    checks = run_doctor(skip_auth=skip_auth_check)
    all_ok = all(c.passed for c in checks)
    if not all_ok:
        print_doctor_report(checks)
        raise SystemExit(1)

    # Show version and auth method
    click.echo(f"Delegate v{get_version()}")
    click.echo()
    auth_display = get_auth_display()
    success(f"Auth: {auth_display}")

    url = f"http://localhost:{port}"

    alive, pid = is_running(hc_home)
    if alive:
        success(f"Delegate already running (PID {pid})")
        success(f"UI: {url}")
        # Server is up — open browser immediately regardless of --foreground
        _open_ui(url, port)
        return

    success(f"Starting delegate on port {port}...")

    if foreground:
        # foreground blocks forever; open browser from a background thread
        # after a short delay to let the server bind
        import threading

        def _open_browser() -> None:
            time.sleep(2)
            _open_ui(url, port)

        threading.Thread(target=_open_browser, daemon=True).start()

        start_daemon(
            hc_home,
            port=port,
            interval=interval,
            max_concurrent=max_concurrent,
            token_budget=token_budget,
            foreground=True,
            dev=dev,
            host=host,
        )
    else:
        result_pid = start_daemon(
            hc_home,
            port=port,
            interval=interval,
            max_concurrent=max_concurrent,
            token_budget=token_budget,
            foreground=False,
            dev=dev,
            host=host,
        )
        if result_pid:
            success(f"Delegate started (PID {result_pid})")
        else:
            success("Delegate started")

        success(f"UI: {url}")

        time.sleep(1.5)
        _open_ui(url, port)


@main.command()
@click.pass_context
def stop(ctx: click.Context) -> None:
    """Stop the running delegate."""
    from delegate.daemon import stop_daemon, is_running
    from delegate.fmt import success, warn, info

    hc_home = _get_home(ctx)
    alive, _ = is_running(hc_home)
    if not alive:
        warn("Delegate is not running")
        return

    info("Stopping delegate...")
    stopped = stop_daemon(hc_home)
    if stopped:
        success("Delegate stopped")


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Check if delegate is running."""
    from delegate.daemon import is_running
    from delegate.fmt import success, info

    hc_home = _get_home(ctx)
    alive, pid = is_running(hc_home)
    if alive:
        success(f"Delegate running (PID {pid})")
    else:
        info("Delegate not running")


# ──────────────────────────────────────────────────────────────
# delegate team add / list / remove
# ──────────────────────────────────────────────────────────────

@main.group()
def team() -> None:
    """Manage teams."""
    pass


def _update_roster_with_new_agents(
    hc_home: Path,
    team_name: str,
    new_parsed_agents: list[tuple[str, str]],
    existing_agent_names: set[str],
) -> None:
    """Rewrite the roster to include both existing and newly added agents."""
    import yaml
    from delegate.bootstrap import make_roster
    from delegate.config import get_human_members
    from delegate.paths import roster_path, agents_dir

    # Build merged member list starting with manager
    all_members: list[tuple[str, str]] = [("delegate", "manager")]

    # Add existing agents with their actual roles from state.yaml
    adir = agents_dir(hc_home, team_name)
    for agent_name in sorted(existing_agent_names):
        state_file = adir / agent_name / "state.yaml"
        role = "engineer"
        if state_file.exists():
            state = yaml.safe_load(state_file.read_text()) or {}
            role = state.get("role", "engineer")
        all_members.append((agent_name, role))

    # Add new agents from the current call
    for aname, arole in new_parsed_agents:
        if aname not in existing_agent_names:
            all_members.append((aname, arole))

    human_names = [m["name"] for m in get_human_members(hc_home)]
    rp = roster_path(hc_home, team_name)
    rp.write_text(make_roster(all_members, humans=human_names))


@team.command("add")
@click.argument("name")
@click.option(
    "--agents", required=True,
    help="Number of agents (e.g. '3') or comma-separated names as name[:role].  "
         "Examples: '3', 'alex:devops,nikhil:designer,john,mark:backend'.  "
         "Numeric values auto-generate names (agent-1, agent-2, ...).  "
         "Agents without a role default to 'engineer'.",
)
@click.option(
    "--repo", "repos", required=True, multiple=True,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Local repo path(s) for the team.  Repeat for multiple repos: --repo /path/a --repo /path/b",
)
@click.option("--interactive", is_flag=True, help="Prompt for bios and charter overrides.")
@click.option(
    "--model", default=None,
    help="Model assignment. Either a single model ('opus' or 'sonnet') to apply to all agents, "
         "or comma-separated name:model pairs (e.g. 'alice:opus,bob:sonnet'). "
         "Defaults to sonnet for all roles.",
)
@click.pass_context
def team_create(
    ctx: click.Context,
    name: str,
    agents: str,
    repos: tuple[str, ...],
    interactive: bool,
    model: str | None,
) -> None:
    """Create a new team."""
    from delegate.bootstrap import bootstrap, validate_project_name
    from delegate.repo import register_repo
    from delegate.runtime import list_ai_agents
    from delegate.fmt import success, warn

    hc_home = _get_home(ctx)

    # Validate team name before doing any work
    try:
        validate_project_name(name)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    # Parse agents: either a count or "name:role" pairs
    parsed_agents: list[tuple[str, str]] = []
    agents_stripped = agents.strip()
    if agents_stripped.isdigit():
        # Numeric: pick random names from pool
        count = int(agents_stripped)
        from delegate.names import pick_names
        from delegate.config import get_default_human
        from delegate.paths import teams_dir

        # Exclude human member name, manager name, and all existing agent names
        exclude = set()
        human_name = get_default_human(hc_home)
        if human_name:
            exclude.add(human_name)
        exclude.add("delegate")

        # Collect all existing agent names across all teams
        tdir = teams_dir(hc_home)
        if tdir.is_dir():
            for team_path in tdir.iterdir():
                if team_path.is_dir():
                    team_name = team_path.name
                    agent_names = list_ai_agents(hc_home, team_name)
                    exclude.update(agent_names)

        chosen = pick_names(count, exclude)
        for agent_name in chosen:
            parsed_agents.append((agent_name, "engineer"))
    else:
        # Parse "name:role" pairs — role defaults to "engineer"
        for token in agents_stripped.split(","):
            token = token.strip()
            if not token:
                continue
            if ":" in token:
                agent_name, role = token.split(":", 1)
                parsed_agents.append((agent_name.strip(), role.strip()))
            else:
                parsed_agents.append((token, "engineer"))

    # Parse --model option into a models dict for bootstrap
    # Formats: "opus" (all agents), or "alice:opus,bob:sonnet" (per-agent)
    models_dict: dict[str, str] | None = None
    if model is not None:
        valid_models = ("opus", "sonnet")
        model_stripped = model.strip()
        if model_stripped in valid_models:
            # Single model applies to all agents via wildcard key
            models_dict = {"*": model_stripped}
        elif ":" in model_stripped:
            # Per-agent name:model pairs
            models_dict = {}
            for token in model_stripped.split(","):
                token = token.strip()
                if not token:
                    continue
                if ":" not in token:
                    raise click.ClickException(
                        f"Invalid --model format '{token}'. Use 'opus', 'sonnet', or 'name:model' pairs."
                    )
                agent_name, agent_model = token.split(":", 1)
                agent_name = agent_name.strip()
                agent_model = agent_model.strip()
                if agent_model not in valid_models:
                    raise click.ClickException(
                        f"Invalid model '{agent_model}' for agent '{agent_name}'. Must be 'opus' or 'sonnet'."
                    )
                models_dict[agent_name] = agent_model
        else:
            raise click.ClickException(
                f"Invalid --model value '{model_stripped}'. Use 'opus', 'sonnet', or 'name:model' pairs."
            )

    # Detect whether the team already exists before bootstrap
    from delegate.paths import team_dir as _team_dir_resolved
    team_existed = _team_dir_resolved(hc_home, name).is_dir()
    existing_agents = set(list_ai_agents(hc_home, name)) if team_existed else set()

    bootstrap(
        hc_home,
        team_name=name,
        manager="delegate",
        agents=parsed_agents,
        interactive=interactive,
        models=models_dict,
    )

    # Accurate messaging: created vs updated
    new_agent_names = [n for n, _ in parsed_agents if n not in existing_agents]
    if team_existed:
        if new_agent_names:
            success(f"Updated team '{name}' — added agent(s): {', '.join(new_agent_names)}")
            # Update roster to include both old and new members
            _update_roster_with_new_agents(hc_home, name, parsed_agents, existing_agents)
        else:
            from delegate.fmt import info
            info(f"Team '{name}' already exists (no changes)")
    else:
        success(f"Created team '{name}'")

    # Register the built-in default workflow
    try:
        from delegate.workflow import register_workflow, get_latest_version
        builtin = Path(__file__).parent / "workflows" / "default.py"
        if builtin.is_file() and get_latest_version(hc_home, name, "default") is None:
            register_workflow(hc_home, name, builtin)
            success("Registered default workflow: default v1")
    except Exception as exc:
        from delegate.fmt import warn
        warn(f"Could not register default workflow: {exc}")

    # Register repos
    registered: list[str] = []
    for repo_path in repos:
        try:
            repo_name = register_repo(hc_home, name, repo_path)
            registered.append(repo_name)
            success(f"Registered repo: {repo_name}")
        except (FileNotFoundError, ValueError) as exc:
            warn(f"Could not register repo '{repo_path}': {exc}")

    # Show team members
    labels = ["delegate (manager)"]
    for aname, arole in parsed_agents:
        labels.append(f"{aname} ({arole})" if arole != "engineer" else aname)
    success(f"Members: {', '.join(labels)}")


@team.command("list")
@click.pass_context
def team_list(ctx: click.Context) -> None:
    """List all teams."""
    hc_home = _get_home(ctx)
    td = _teams_dir(hc_home)
    if not td.is_dir():
        click.echo("No teams found.")
        return

    teams = sorted(d.name for d in td.iterdir() if d.is_dir())
    if not teams:
        click.echo("No teams found.")
        return

    click.echo("Teams:")
    for t in teams:
        click.echo(f"  - {click.style(t, bold=True)}")


@team.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def team_remove(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove a team and all its data.

    This deletes the team directory (agents, worktrees, DB, repos config)
    permanently.  It does NOT delete the actual git repositories — only the
    symlinks/config that Delegate created.
    """
    import shutil
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    td = _team_dir(hc_home, name)

    # Check if the team exists in ANY source (directory, DB, or project_map)
    has_dir = td.is_dir()
    team_uuid: str | None = None
    has_db_entry = False
    try:
        from delegate.db import get_connection
        conn = get_connection(hc_home)
        row = conn.execute(
            "SELECT project_id FROM projects WHERE name = ?", (name,)
        ).fetchone()
        if row:
            team_uuid = row["project_id"]
            has_db_entry = True
        conn.close()
    except Exception:
        pass

    has_map_entry = False
    try:
        from delegate.paths import list_team_names
        has_map_entry = name in list_team_names(hc_home)
    except Exception:
        pass

    if not has_dir and not has_db_entry and not has_map_entry:
        click.echo(f"Team '{name}' does not exist.")
        raise SystemExit(1)

    if not yes:
        click.confirm(
            f"Remove team '{name}' and all its data? This cannot be undone.",
            abort=True,
        )

    if has_dir:
        shutil.rmtree(td)

    # Remove from global teams database table
    try:
        from delegate.db import get_connection
        conn = get_connection(hc_home)
        try:
            conn.execute("DELETE FROM projects WHERE name = ?", (name,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Best-effort — directory is already gone

    # Soft-delete team UUID + member IDs in db_ids
    if team_uuid:
        try:
            from delegate.db import get_connection
            from delegate.db_ids import soft_delete_team
            ids_conn = get_connection(hc_home)
            try:
                soft_delete_team(ids_conn, team_uuid)
                ids_conn.commit()
            finally:
                ids_conn.close()
        except Exception:
            pass  # Best-effort

    # Remove from project_map.json
    try:
        from delegate.paths import unregister_team_path
        unregister_team_path(hc_home, name)
    except Exception:
        pass  # Best-effort

    # Notify all SSE clients to refresh their team list
    try:
        from delegate.activity import broadcast_teams_refresh
        broadcast_teams_refresh()
    except Exception:
        pass  # Best-effort — no-op if no SSE clients connected

    success(f"Removed team '{name}'")


@team.command("set-reviewer")
@click.argument("team_name")
@click.argument("mode", type=click.Choice(["human", "ai"], case_sensitive=False))
@click.option("--threshold", type=float, default=None, help="Score threshold for AI reviewer (default: 3.5).")
@click.option("--model", default=None, help="Model to use for AI reviewer.")
@click.pass_context
def team_set_reviewer(ctx: click.Context, team_name: str, mode: str, threshold: float | None, model: str | None) -> None:
    """Set the reviewer mode for a team.

    MODE is 'human' (require human approval) or 'ai' (AI reviews diffs automatically).
    """
    from delegate.config import update_reviewer_config

    hc_home = _get_home(ctx)
    kwargs: dict = {"mode": mode}
    if threshold is not None:
        kwargs["threshold"] = threshold
    if model is not None:
        kwargs["model"] = model
    cfg = update_reviewer_config(hc_home, team_name, **kwargs)
    click.echo(f"Set reviewer for team '{team_name}': mode={cfg['mode']}, threshold={cfg['threshold']}")


# ──────────────────────────────────────────────────────────────
# delegate agent add
# ──────────────────────────────────────────────────────────────

@main.group()
def agent() -> None:
    """Manage agents on a team."""
    pass


@agent.command("add")
@click.argument("team")
@click.argument("name", required=False, default=None)
@click.option(
    "--role", default="engineer",
    help="Role for the new agent (default: engineer).",
)
@click.option(
    "--model", default=None, type=click.Choice(["opus", "sonnet"]),
    help="Model: opus or sonnet. Default: sonnet for all roles.",
)
@click.option(
    "--bio", default=None,
    help="Short bio/description of the agent's strengths and focus.",
)
@click.pass_context
def agent_add(ctx: click.Context, team: str, name: str | None, role: str, model: str, bio: str | None) -> None:
    """Add a new agent to an existing team.

    TEAM is the team name.  NAME is the new agent's name (optional - auto-generated if omitted).
    """
    from delegate.bootstrap import add_agent
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    try:
        agent_name = add_agent(hc_home, team_name=team, agent_name=name, role=role, model=model, bio=bio)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))

    resolved_model = model or "sonnet"
    success(f"Added agent '{agent_name}' to team '{team}' (role: {role}, model: {resolved_model})")


# ──────────────────────────────────────────────────────────────
# delegate member add / list / remove
# ──────────────────────────────────────────────────────────────

@main.group()
def member() -> None:
    """Manage human members."""
    pass


@member.command("add")
@click.argument("name")
@click.pass_context
def member_add(ctx: click.Context, name: str) -> None:
    """Add a human member to Delegate.

    Creates a member YAML file in ~/.delegate/members/.
    The member is automatically added to all existing teams' rosters.
    """
    from delegate.config import add_member
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    add_member(hc_home, name)
    success(f"Added member '{name}'")

    # Auto-add to all existing teams' rosters
    td = _teams_dir(hc_home)
    if td.is_dir():
        from delegate.paths import roster_path as _roster_path
        for team_dir in sorted(td.iterdir()):
            if not team_dir.is_dir():
                continue
            rp = _roster_path(hc_home, team_dir.name)
            if rp.exists():
                roster_text = rp.read_text()
                roster_line = f"- **{name}** (member)"
                if roster_line not in roster_text:
                    if not roster_text.endswith("\n"):
                        roster_text += "\n"
                    roster_text += roster_line + "\n"
                    rp.write_text(roster_text)
                    success(f"  Added to team '{team_dir.name}'")


@member.command("list")
@click.pass_context
def member_list(ctx: click.Context) -> None:
    """List all human members."""
    from delegate.config import get_human_members

    hc_home = _get_home(ctx)
    members = get_human_members(hc_home)
    if not members:
        click.echo("No members found.")
        return

    click.echo("Members:")
    for m in members:
        click.echo(f"  - {click.style(m['name'], bold=True)} (kind: {m.get('kind', 'human')})")


@member.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def member_remove(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove a human member."""
    from delegate.config import remove_member
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    if not yes:
        click.confirm(f"Remove member '{name}'?", abort=True)

    if remove_member(hc_home, name):
        success(f"Removed member '{name}'")
    else:
        click.echo(f"Member '{name}' not found.")


# ──────────────────────────────────────────────────────────────
# delegate config set human / boss / source-repo
# ──────────────────────────────────────────────────────────────

@main.group()
def config() -> None:
    """Manage org-wide configuration."""
    pass


@config.group("set")
def config_set() -> None:
    """Set a configuration value."""
    pass


def _set_human_name(hc_home: Path, name: str) -> None:
    """Shared implementation for config set human/boss."""
    from delegate.config import add_member

    add_member(hc_home, name)
    click.echo(f"Human member set to: {name}")


@config_set.command("human")
@click.argument("name")
@click.pass_context
def config_set_human(ctx: click.Context, name: str) -> None:
    """Set the human member name."""
    _set_human_name(_get_home(ctx), name)


@config_set.command("boss")
@click.argument("name")
@click.pass_context
def config_set_boss(ctx: click.Context, name: str) -> None:
    """(Deprecated) Alias for 'config set human'."""
    _set_human_name(_get_home(ctx), name)


@config_set.command("source-repo")
@click.argument("path", type=click.Path(path_type=Path))
@click.pass_context
def config_set_source_repo(ctx: click.Context, path: Path) -> None:
    """Set the path to the delegate source repository (for self-update)."""
    from delegate.config import set_source_repo

    hc_home = _get_home(ctx)
    set_source_repo(hc_home, path.resolve())
    click.echo(f"Source repo set to: {path.resolve()}")


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Show the current configuration."""
    from delegate.config import get_default_human, get_source_repo, get_human_members

    hc_home = _get_home(ctx)
    human = get_default_human(hc_home)
    source_repo = get_source_repo(hc_home) or "(not set)"

    # Members
    members = get_human_members(hc_home)
    member_names = ", ".join(m["name"] for m in members) if members else "(none)"
    click.echo(f"Members:     {member_names}")
    click.echo(f"Default:     {human}")
    click.echo(f"Source repo: {source_repo}")

    # List teams and their repos
    td = _teams_dir(hc_home)
    if td.is_dir():
        teams = sorted(d.name for d in td.iterdir() if d.is_dir())
        if teams:
            from delegate.config import get_repos
            click.echo(f"Teams:       {len(teams)}")
            for t in teams:
                repos = get_repos(hc_home, t)
                click.echo(f"  {t}: {len(repos)} repo(s)")
                for rn, meta in repos.items():
                    click.echo(f"    - {rn}: {meta.get('source', '?')}")


# ──────────────────────────────────────────────────────────────
# delegate repo add / list
# ──────────────────────────────────────────────────────────────

@main.group()
def repo() -> None:
    """Manage registered repositories."""
    pass


@repo.command("add")
@click.argument("team_name")
@click.argument("path_or_url")
@click.option("--name", "repo_name", default=None, help="Name for the repo (default: derived from path/URL).")
@click.option(
    "--merge-policy",
    type=click.Choice(["no-review", "review-needed"], case_sensitive=False),
    default=None,
    help="Merge policy: 'no-review' (skip review) or 'review-needed' (require approval). Default: review-needed.",
)
@click.option(
    "--approval",
    type=click.Choice(["auto", "manual"], case_sensitive=False),
    default=None,
    hidden=True,
    help="Deprecated — use --merge-policy instead.",
)
@click.option(
    "--test-cmd",
    default=None,
    help="Shell command to run tests (e.g. '/path/to/.venv/bin/python -m pytest -x -q').",
)
@click.pass_context
def repo_add(ctx: click.Context, team_name: str, path_or_url: str, repo_name: str | None, merge_policy: str | None, approval: str | None, test_cmd: str | None) -> None:
    """Register a repository for a team.

    TEAM_NAME is the team this repo belongs to.
    PATH_OR_URL is a local path or remote URL.
    """
    from delegate.repo import register_repo

    hc_home = _get_home(ctx)
    name = register_repo(hc_home, team_name, path_or_url, name=repo_name, merge_policy=merge_policy, approval=approval, test_cmd=test_cmd)
    click.echo(f"Registered repo '{name}' for team '{team_name}'")


@repo.command("list")
@click.argument("team_name")
@click.pass_context
def repo_list(ctx: click.Context, team_name: str) -> None:
    """List registered repositories for a team."""
    from delegate.config import get_repos

    hc_home = _get_home(ctx)
    repos = get_repos(hc_home, team_name)
    if not repos:
        click.echo(f"No repositories registered for team '{team_name}'.")
        return

    click.echo(f"Repos for team '{team_name}':")
    for name, meta in repos.items():
        click.echo(f"  - {name}: {meta.get('source', '?')}")


@repo.command("set-merge-policy")
@click.argument("team_name")
@click.argument("repo_name")
@click.argument("policy", type=click.Choice(["no-review", "review-needed"], case_sensitive=False))
@click.pass_context
def repo_set_merge_policy(ctx: click.Context, team_name: str, repo_name: str, policy: str) -> None:
    """Set the merge policy for a repo.

    POLICY is 'no-review' (skip review, merge when tests pass)
    or 'review-needed' (require human/AI approval before merge).
    """
    from delegate.config import update_merge_policy

    hc_home = _get_home(ctx)
    update_merge_policy(hc_home, team_name, repo_name, policy)
    click.echo(f"Set merge policy for '{repo_name}' to '{policy}'")


@repo.command("set-approval", hidden=True)
@click.argument("team_name")
@click.argument("repo_name")
@click.argument("approval", type=click.Choice(["auto", "manual"], case_sensitive=False))
@click.pass_context
def repo_set_approval(ctx: click.Context, team_name: str, repo_name: str, approval: str) -> None:
    """Deprecated — use 'set-merge-policy' instead."""
    from delegate.config import update_merge_policy, _legacy_approval_to_policy

    hc_home = _get_home(ctx)
    policy = _legacy_approval_to_policy(approval)
    update_merge_policy(hc_home, team_name, repo_name, policy)
    click.echo(f"Set merge policy for '{repo_name}' to '{policy}'")


# ──────────────────────────────────────────────────────────────
# delegate repo prefer-main
# ──────────────────────────────────────────────────────────────

@repo.command("prefer-main")
@click.argument("team_name")
@click.argument("repo_name")
@click.argument("files", nargs=-1)
@click.option("--show", is_flag=True, help="Show current main-prefer file patterns.")
@click.option("--clear", is_flag=True, help="Clear all main-prefer file patterns.")
@click.pass_context
def repo_prefer_main(
    ctx: click.Context, team_name: str, repo_name: str,
    files: tuple[str, ...], show: bool, clear: bool,
) -> None:
    """Configure files that should always use main's version after rebase.

    Examples:

      delegate repo prefer-main myteam myrepo conftest.py tests/conftest.py

      delegate repo prefer-main myteam myrepo --show

      delegate repo prefer-main myteam myrepo --clear
    """
    from delegate.config import get_main_prefer_files, update_main_prefer_files

    hc_home = _get_home(ctx)

    if show:
        patterns = get_main_prefer_files(hc_home, team_name, repo_name)
        if patterns:
            click.echo(f"Main-prefer files for {repo_name}:")
            for p in patterns:
                click.echo(f"  - {p}")
        else:
            click.echo(f"No main-prefer files configured for {repo_name}.")
        return

    if clear:
        update_main_prefer_files(hc_home, team_name, repo_name, [])
        click.echo(f"Cleared main-prefer files for {repo_name}.")
        return

    if not files:
        click.echo("Error: provide file patterns, or use --show / --clear.", err=True)
        raise SystemExit(1)

    update_main_prefer_files(hc_home, team_name, repo_name, list(files))
    click.echo(f"Set main-prefer files for {repo_name}:")
    for f in files:
        click.echo(f"  - {f}")


# ──────────────────────────────────────────────────────────────
# delegate self-update
# ──────────────────────────────────────────────────────────────

@main.command("self-update")
@click.pass_context
def self_update(ctx: click.Context) -> None:
    """Update delegate from the source repository.

    Runs 'git pull' in the source repo and reinstalls the package.
    """
    from delegate.config import get_source_repo

    hc_home = _get_home(ctx)
    source_repo = get_source_repo(hc_home)
    if source_repo is None:
        click.echo("Error: No source repo configured.")
        click.echo("Set one with: delegate config set source-repo /path/to/delegate")
        raise SystemExit(1)

    if not source_repo.is_dir():
        click.echo(f"Error: Source repo not found at {source_repo}")
        raise SystemExit(1)

    # Step 1: git pull
    click.echo(f"Updating source repo at {source_repo}...")
    result = subprocess.run(
        ["git", "pull", "--rebase"],
        cwd=str(source_repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(f"Git pull failed:\n{result.stderr}")
        raise SystemExit(1)
    click.echo(result.stdout.strip())

    # Step 2: reinstall
    click.echo("Reinstalling delegate...")
    install_cmd = [sys.executable, "-m", "pip", "install", "-e", str(source_repo)]

    # Prefer uv if available
    import shutil
    if shutil.which("uv"):
        install_cmd = ["uv", "pip", "install", "-e", str(source_repo)]

    result = subprocess.run(install_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        click.echo(f"Install failed:\n{result.stderr}")
        raise SystemExit(1)

    click.echo("Delegate updated successfully. ✓")


# ──────────────────────────────────────────────────────────────
# delegate workflow add / list / show / update-actions
# ──────────────────────────────────────────────────────────────

@main.group("workflow")
def workflow_group() -> None:
    """Manage task workflows."""
    pass


@workflow_group.command("add")
@click.argument("team_name")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def workflow_add(ctx: click.Context, team_name: str, path: Path) -> None:
    """Register a workflow for a team.

    TEAM_NAME is the team this workflow belongs to.
    PATH is the Python workflow definition file.

    The file must use the @workflow decorator to define at least one
    workflow with a name and version.  The version must be higher than
    any existing version for that workflow name.

    Example:
        delegate workflow add myteam ./pipelines/my-workflow.py
    """
    from delegate.workflow import register_workflow
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    try:
        wf = register_workflow(hc_home, team_name, path)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))

    success(f"Registered workflow '{wf.name}' v{wf.version} for team '{team_name}'")
    click.echo(wf.format_graph())


@workflow_group.command("list")
@click.argument("team_name")
@click.pass_context
def workflow_list(ctx: click.Context, team_name: str) -> None:
    """List workflows registered for a team."""
    from delegate.workflow import list_workflows

    hc_home = _get_home(ctx)
    workflows = list_workflows(hc_home, team_name)

    if not workflows:
        click.echo(f"No workflows registered for team '{team_name}'.")
        return

    click.echo(f"Workflows for team '{team_name}':")
    for wf in workflows:
        versions_str = ", ".join(f"v{v}" for v in wf["all_versions"])
        stage_count = len(wf["stages"])
        click.echo(
            f"  {click.style(wf['name'], bold=True)} "
            f"(latest: v{wf['version']}, {stage_count} stages) "
            f"[{versions_str}]"
        )


@workflow_group.command("show")
@click.argument("team_name")
@click.argument("name")
@click.option("--version", "version", type=int, default=None, help="Show a specific version (default: latest).")
@click.pass_context
def workflow_show(ctx: click.Context, team_name: str, name: str, version: int | None) -> None:
    """Show the details and graph of a workflow."""
    from delegate.workflow import load_workflow, get_latest_version

    hc_home = _get_home(ctx)

    if version is None:
        version = get_latest_version(hc_home, team_name, name)
        if version is None:
            raise click.ClickException(f"No workflow '{name}' found for team '{team_name}'.")

    try:
        wf = load_workflow(hc_home, team_name, name, version)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))

    click.echo(wf.format_graph())
    click.echo()
    click.echo(f"Source: {wf.source_path}")


@workflow_group.command("update-actions")
@click.argument("team_name")
@click.argument("name")
@click.argument("actions_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.pass_context
def workflow_update_actions(ctx: click.Context, team_name: str, name: str, actions_path: Path) -> None:
    """Update actions for an existing workflow (no version bump).

    TEAM_NAME is the team.
    NAME is the workflow name.
    ACTIONS_PATH is the directory containing action scripts.

    This replaces the workflow's actions directory without changing
    the stage graph or requiring a version bump.
    """
    from delegate.workflow import update_actions
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    try:
        update_actions(hc_home, team_name, name, actions_path)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))

    success(f"Updated actions for workflow '{name}' (team '{team_name}')")


@workflow_group.command("init")
@click.argument("team_name")
@click.pass_context
def workflow_init(ctx: click.Context, team_name: str) -> None:
    """Register built-in workflows (default + research) for a team.

    This copies the built-in workflows shipped with Delegate into the
    team's workflows directory.  Safe to re-run.
    """
    from delegate.workflow import register_workflow, get_latest_version
    from delegate.fmt import success, info

    hc_home = _get_home(ctx)

    # Built-in workflows to register
    builtins = [
        ("default", "default.py"),
        ("research", "research.py"),
    ]

    for wf_name, filename in builtins:
        current = get_latest_version(hc_home, team_name, wf_name)
        if current is not None:
            info(f"Workflow '{wf_name}' v{current} already registered for team '{team_name}'")
            continue

        builtin = Path(__file__).parent / "workflows" / filename
        if not builtin.is_file():
            raise click.ClickException(f"Built-in {wf_name} workflow not found at {builtin}")

        try:
            wf = register_workflow(hc_home, team_name, builtin)
        except (FileNotFoundError, ValueError) as exc:
            raise click.ClickException(str(exc))

        success(f"Registered built-in workflow '{wf.name}' v{wf.version} for team '{team_name}'")


# ---------------------------------------------------------------------------
# delegate network …
# ---------------------------------------------------------------------------

@main.group()
def network() -> None:
    """Manage the global network allowlist."""
    pass


@network.command("show")
@click.pass_context
def network_show(ctx: click.Context) -> None:
    """Show the current network allowlist."""
    from delegate.network import get_allowed_domains, DEFAULT_DOMAINS

    hc_home = _get_home(ctx)
    domains = get_allowed_domains(hc_home)

    is_default = set(domains) == set(DEFAULT_DOMAINS)
    header = "Network allowlist (default):" if is_default else "Network allowlist:"
    click.echo(header)
    for d in sorted(domains):
        click.echo(f"  - {d}")


@network.command("allow")
@click.argument("domain")
@click.pass_context
def network_allow(ctx: click.Context, domain: str) -> None:
    """Add a domain to the allowlist.

    DOMAIN can be an exact domain (api.github.com), a wildcard
    (*.openai.com), or '*' to allow all.
    """
    from delegate.network import allow_domain
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    try:
        updated = allow_domain(hc_home, domain)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    success(f"Added '{domain}' to network allowlist")
    for d in sorted(updated):
        click.echo(f"  - {d}")


@network.command("disallow")
@click.argument("domain")
@click.pass_context
def network_disallow(ctx: click.Context, domain: str) -> None:
    """Remove a domain from the allowlist."""
    from delegate.network import disallow_domain
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    try:
        updated = disallow_domain(hc_home, domain)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    success(f"Removed '{domain}' from network allowlist")
    for d in sorted(updated):
        click.echo(f"  - {d}")


@network.command("reset")
@click.pass_context
def network_reset(ctx: click.Context) -> None:
    """Reset the allowlist to the default wildcard (allow all)."""
    from delegate.network import reset_config
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    reset_config(hc_home)
    success("Network allowlist reset to * (unrestricted)")


# ──────────────────────────────────────────────────────────────
# delegate satellite add / remove / list / start / stop / status
# ──────────────────────────────────────────────────────────────

@main.group()
def satellite() -> None:
    """Manage satellite workers for distributed execution."""
    pass


@satellite.command("add")
@click.argument("name")
@click.pass_context
def satellite_add(ctx: click.Context, name: str) -> None:
    """Register a new satellite and generate its bearer token.

    NAME is a unique identifier for the satellite (e.g. 'rana-ai-server').
    The token is displayed once and cannot be retrieved later.
    """
    from delegate.auth import add_satellite
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    token = add_satellite(hc_home, name)
    success(f"Satellite '{name}' registered")
    click.echo()
    click.echo("Bearer token (save this — it cannot be retrieved later):")
    click.echo(f"  {token}")
    click.echo()
    click.echo("On the satellite machine, start with:")
    click.echo(f"  delegate satellite start --coordinator http://<coordinator>:3548 --token {token} --id {name}")


@satellite.command("remove")
@click.argument("name")
@click.pass_context
def satellite_remove(ctx: click.Context, name: str) -> None:
    """Remove a registered satellite."""
    from delegate.auth import remove_satellite

    hc_home = _get_home(ctx)
    if remove_satellite(hc_home, name):
        click.echo(f"Satellite '{name}' removed")
    else:
        click.echo(f"Satellite '{name}' not found")


@satellite.command("list")
@click.pass_context
def satellite_list(ctx: click.Context) -> None:
    """List registered satellites."""
    from delegate.auth import list_satellites

    hc_home = _get_home(ctx)
    sats = list_satellites(hc_home)
    if not sats:
        click.echo("No satellites registered.")
        return

    click.echo("Satellites:")
    for s in sats:
        click.echo(f"  - {click.style(s['name'], bold=True)} (registered: {s['created_at']})")


@satellite.command("start")
@click.option("--coordinator", required=True, help="Coordinator URL (e.g. http://mac-mini:3548)")
@click.option("--token", required=True, help="Bearer token from 'delegate satellite add'")
@click.option("--id", "satellite_id", required=True, help="Satellite identifier (must match coordinator registration)")
@click.option("--poll-interval", default=2.0, help="Poll interval in seconds (default: 2.0)")
@click.option("--max-concurrent", default=8, help="Max concurrent agent turns (default: 8)")
@click.pass_context
def satellite_start(ctx: click.Context, coordinator: str, token: str, satellite_id: str, poll_interval: float, max_concurrent: int) -> None:
    """Start the satellite daemon (run on the remote machine).

    Polls the coordinator for work and executes agent turns locally.
    """
    import asyncio
    from delegate.satellite import SatelliteDaemon

    daemon = SatelliteDaemon(
        coordinator_url=coordinator.rstrip("/"),
        satellite_id=satellite_id,
        auth_token=token,
        poll_interval=poll_interval,
        max_concurrent=max_concurrent,
    )
    click.echo(f"Starting satellite '{satellite_id}' → {coordinator}")
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        click.echo("Satellite stopped")


@satellite.command("stop")
def satellite_stop() -> None:
    """Stop the satellite daemon (sends SIGTERM to the running process)."""
    click.echo("Use Ctrl+C or send SIGTERM to the satellite process to stop it.")


@satellite.command("status")
def satellite_status() -> None:
    """Check satellite daemon status."""
    click.echo("Satellite status check not yet implemented. Use process monitoring tools.")


# ──────────────────────────────────────────────────────────────
# delegate agent set-host
# ──────────────────────────────────────────────────────────────

@agent.command("set-host")
@click.argument("team")
@click.argument("name")
@click.argument("host", required=False, default=None)
@click.option("--local", is_flag=True, help="Set agent to run locally on the coordinator")
@click.pass_context
def agent_set_host(ctx: click.Context, team: str, name: str, host: str | None, local: bool) -> None:
    """Set which satellite an agent runs on.

    TEAM is the team name. NAME is the agent name.
    HOST is the satellite identifier (e.g. 'rana-ai-server').
    Use --local to move the agent back to the coordinator.
    """
    import yaml
    from delegate.paths import agent_dir as _ad

    hc_home = _get_home(ctx)
    ad = _ad(hc_home, team, name)
    state_file = ad / "state.yaml"
    if not state_file.exists():
        raise click.ClickException(f"Agent '{name}' not found in team '{team}'")

    state = yaml.safe_load(state_file.read_text()) or {}
    if local:
        state["host"] = None
        state_file.write_text(yaml.dump(state, default_flow_style=False, sort_keys=False))
        click.echo(f"Agent '{name}' set to run locally (coordinator)")
    elif host:
        state["host"] = host
        state_file.write_text(yaml.dump(state, default_flow_style=False, sort_keys=False))
        click.echo(f"Agent '{name}' set to run on satellite '{host}'")
    else:
        raise click.ClickException("Provide a satellite HOST or use --local")


# ──────────────────────────────────────────────────────────────
# delegate agent nudge
# ──────────────────────────────────────────────────────────────

@agent.command("nudge")
@click.argument("team")
@click.argument("name")
@click.option("--message", default=None, help="Custom nudge message to send.")
@click.pass_context
def agent_nudge(ctx: click.Context, team: str, name: str, message: str | None) -> None:
    """Nudge an agent to check its assigned tasks and start working.

    TEAM is the team name. NAME is the agent name.
    Sends a system message to the agent's mailbox listing its pending tasks.
    """
    from delegate.config import SYSTEM_USER
    from delegate.fmt import success, info
    from delegate.mailbox import send as send_message
    from delegate.task import list_tasks

    hc_home = _get_home(ctx)

    # Gather the agent's active tasks
    active_tasks = [
        t for t in list_tasks(hc_home, team, assignee=name)
        if t.get("status") not in ("done", "cancelled")
    ]

    if message:
        body = message
    elif active_tasks:
        task_lines = ", ".join(
            f"T{t['id']:04d} ({t.get('title', 'untitled')}, status: {t.get('status', '?')})"
            for t in active_tasks
        )
        body = f"Nudge: you have assigned tasks that need attention: {task_lines}. Please review and continue working."
    else:
        body = "Nudge: please check if there are any tasks or messages that need your attention."

    send_message(hc_home, team, SYSTEM_USER, name, body)
    success(f"Nudged agent '{name}' on team '{team}'")
    if active_tasks:
        info(f"  {len(active_tasks)} active task(s) mentioned in nudge")


@agent.command("restart-all")
@click.argument("team", required=False, default=None)
@click.option("--port", type=int, default=None, help="Delegate port (default: auto-detect from env).")
@click.pass_context
def agent_restart_all(ctx: click.Context, team: str | None, port: int | None) -> None:
    """Restart all agent sessions (kill subprocesses, clear caches).

    Useful after a usage-limit reset when agents are stuck on API errors.
    Cancels in-flight turns, closes all cached Telephone sessions, and
    wakes the daemon to re-dispatch agents with unread messages.

    TEAM is optional — if omitted, restarts agents across all teams.
    """
    import urllib.request
    import urllib.error
    from delegate.daemon import is_running
    from delegate.fmt import success, error, info

    hc_home = _get_home(ctx)
    alive, _ = is_running(hc_home)
    if not alive:
        error("Delegate is not running")
        raise SystemExit(1)

    p = port or int(os.environ.get("DELEGATE_PORT", DEFAULT_PORT))
    url = f"http://127.0.0.1:{p}/api/agents/restart-all"
    if team:
        url += f"?team={team}"

    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            import json
            data = json.loads(resp.read())
        success(
            f"Restarted: {data['turns_cancelled']} turn(s) cancelled, "
            f"{data['telephones_closed']} session(s) closed"
        )
        info(f"  Teams: {', '.join(data.get('teams', []))}")
        info("  Daemon will re-dispatch agents with unread messages on next cycle")
    except urllib.error.URLError as exc:
        error(f"Could not reach delegate at port {p}: {exc}")
        raise SystemExit(1)


# ──────────────────────────────────────────────────────────────
# delegate config set passphrase
# ──────────────────────────────────────────────────────────────

@config_set.command("passphrase")
@click.argument("value", required=False, default=None)
@click.option("--disable", is_flag=True, help="Disable passphrase authentication")
@click.pass_context
def config_set_passphrase(ctx: click.Context, value: str | None, disable: bool) -> None:
    """Set a passphrase for web UI authentication.

    When set, the web UI requires login. Use --disable to remove.
    """
    from delegate.auth import set_passphrase, disable_passphrase
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    if disable:
        disable_passphrase(hc_home)
        success("Web UI passphrase authentication disabled")
    elif value:
        set_passphrase(hc_home, value)
        success("Web UI passphrase set")
    else:
        raise click.ClickException("Provide a passphrase value or use --disable")


# ──────────────────────────────────────────────────────────────
# delegate cleanup
# ──────────────────────────────────────────────────────────────

@main.command()
@click.option("--team", default=None, help="Limit cleanup to a specific team.")
@click.option("--max-age", default=14, type=int, show_default=True,
              help="Days to keep — records older than this are pruned.")
@click.option("--dry-run", is_flag=True, help="Preview what would be cleaned without making changes.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.option("--no-db", is_flag=True, help="Skip database pruning.")
@click.option("--no-worktrees", is_flag=True, help="Skip worktree removal.")
@click.option("--no-venvs", is_flag=True, help="Skip .venv removal.")
@click.option("--no-caches", is_flag=True, help="Skip package cache cleanup.")
@click.option("--no-logs", is_flag=True, help="Skip log file pruning.")
@click.pass_context
def cleanup(
    ctx: click.Context,
    team: str | None,
    max_age: int,
    dry_run: bool,
    yes: bool,
    no_db: bool,
    no_worktrees: bool,
    no_venvs: bool,
    no_caches: bool,
    no_logs: bool,
) -> None:
    """Reclaim disk space by pruning stale data, caches, and logs.

    Removes old database records (sessions, messages, reviews), stale git
    worktrees for completed tasks, package caches, virtual environments
    in worktree directories, and rotated log files.

    Use --dry-run to preview what would be cleaned without making changes.
    Use --team to limit cleanup to a specific team.

    \b
    Examples:
        delegate cleanup --dry-run          # preview everything
        delegate cleanup --team myteam      # clean one team only
        delegate cleanup --max-age 7        # aggressive: prune after 7 days
        delegate cleanup --no-db            # skip DB pruning, clean only files
    """
    from delegate.cleanup import preview_cleanup, run_cleanup
    from delegate.fmt import success, info, warn, header

    hc_home = _get_home(ctx)

    if dry_run:
        header("Cleanup preview")
        if team:
            info(f"Team: {team}")
        info(f"Max age: {max_age} days")
        click.echo()

        preview = preview_cleanup(hc_home, team_name=team, max_age_days=max_age)

        info(f"Stale sessions:   {preview.stale_sessions}")
        info(f"Stale messages:   {preview.stale_messages}")
        info(f"Stale reviews:    {preview.stale_reviews}")
        info(f"Stale worktrees:  {len(preview.stale_worktrees)}")
        for wt in preview.stale_worktrees:
            click.echo(f"      {wt}")
        info(f"Venv directories: {len(preview.venv_dirs)}")
        for v in preview.venv_dirs:
            click.echo(f"      {v}")
        info(f"Package caches:   {preview.pkg_cache_bytes / (1024 * 1024):.1f} MB")
        info(f"Stale log files:  {preview.stale_log_files}")
        info(f"Daemon log files: {preview.daemon_log_files}")
        info(f"Database size:    {preview.db_size / (1024 * 1024):.1f} MB")
        click.echo()
        success(f"Estimated reclaimable: {preview.total_bytes_reclaimable / (1024 * 1024):.1f} MB")
        click.echo()
        info("Run without --dry-run to execute cleanup.")
        return

    # Confirmation
    if not yes:
        scope = f"team '{team}'" if team else "all teams"
        click.confirm(
            f"Clean up {scope}? (max age: {max_age} days, this cannot be undone)",
            abort=True,
        )

    result = run_cleanup(
        hc_home,
        team_name=team,
        max_age_days=max_age,
        prune_db=not no_db,
        prune_worktrees=not no_worktrees,
        prune_venvs=not no_venvs,
        prune_caches=not no_caches,
        prune_logs=not no_logs,
    )

    header("Cleanup complete")
    info(f"Freed: {result.bytes_freed / (1024 * 1024):.1f} MB")
    if result.sessions_deleted:
        info(f"Sessions pruned:    {result.sessions_deleted}")
    if result.messages_deleted:
        info(f"Messages pruned:    {result.messages_deleted}")
    if result.reviews_deleted:
        info(f"Reviews pruned:     {result.reviews_deleted}")
    if result.worktrees_removed:
        info(f"Worktrees removed:  {result.worktrees_removed}")
    if result.venvs_removed:
        info(f"Venvs removed:      {result.venvs_removed}")
    if result.pkg_caches_cleared:
        info(f"Caches cleared:     {result.pkg_caches_cleared}")
    if result.log_files_removed:
        info(f"Log files removed:  {result.log_files_removed}")
    if result.daemon_logs_removed:
        info(f"Daemon logs removed:{result.daemon_logs_removed}")
    if result.db_size_before and result.db_size_after:
        info(f"DB: {result.db_size_before / (1024 * 1024):.1f} MB → {result.db_size_after / (1024 * 1024):.1f} MB")

    if result.errors:
        click.echo()
        warn(f"{len(result.errors)} error(s) during cleanup:")
        for err in result.errors:
            click.echo(f"    {err}")

    success("Done.")


# ──────────────────────────────────────────────────────────────
# delegate nuke
# ──────────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def nuke(ctx: click.Context) -> None:
    """Destroy all Delegate state by deleting ~/.delegate.

    WARNING: This permanently deletes ALL Delegate data including teams,
    agents, tasks, history, database, and configuration. This cannot be undone.

    Requires interactive confirmation by typing "delete everything".
    """
    import shutil
    from delegate.fmt import warn, success

    hc_home = _get_home(ctx)

    # Show warning and prompt for confirmation
    click.echo()
    warn("This will permanently delete ALL Delegate data including:")
    click.echo("  - All teams and agent data")
    click.echo("  - All tasks and history")
    click.echo("  - All configuration")
    click.echo("  - Database")
    click.echo()
    click.echo(f"Directory: {hc_home}")
    click.echo()

    confirmation = click.prompt('Type "delete everything" to confirm', type=str, default="")

    if confirmation != "delete everything":
        click.echo("Aborted. Nothing was deleted.")
        return

    # Delete the entire delegate home directory
    click.echo(f"Nuking {hc_home}...")
    if hc_home.exists():
        shutil.rmtree(hc_home)
        success("Done. All Delegate data has been removed.")
    else:
        click.echo(f"Directory {hc_home} does not exist. Nothing to delete.")


if __name__ == "__main__":
    main()
