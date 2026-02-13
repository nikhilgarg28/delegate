"""Delegate CLI entry point using Click.

Commands:
    delegate doctor                                  — verify runtime dependencies
    delegate start [--port N]                        — start delegate (web UI + agents)
    delegate stop                                    — stop running delegate
    delegate status                                  — check if delegate is running
    delegate team add <name> --manager M --agents a:role,b --repo /path  — create a new team
    delegate team list                               — list existing teams
    delegate team remove <name>                      — remove a team and all its data
    delegate agent add <team> <name>                 — add an agent to a team
    delegate config set boss <name>                  — set org-wide boss name
    delegate config set source-repo <path>           — set delegate source repo path
    delegate repo add <team> <path_or_url> [--name]  — register a repository for a team
    delegate repo list <team>                        — list repos for a team
    delegate self-update                             — update delegate from source repo
"""

import subprocess
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

# Load .env from CWD (or parent dirs) so ANTHROPIC_API_KEY etc. are available.
load_dotenv()

from delegate.paths import home as _home, teams_dir as _teams_dir, team_dir as _team_dir


def _get_home(ctx: click.Context) -> Path:
    """Resolve delegate home from context or default."""
    return _home(ctx.obj.get("home_override") if ctx.obj else None)


@click.group()
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

@main.command()
@click.option("--port", type=int, default=3548, help="Port for the web UI (default: 3548).")
@click.option("--interval", type=float, default=1.0, help="Poll interval in seconds.")
@click.option("--max-concurrent", type=int, default=32, help="Max concurrent agents.")
@click.option("--token-budget", type=int, default=None, help="Default token budget per agent session.")
@click.option("--foreground", is_flag=True, help="Run in foreground instead of background.")
@click.pass_context
def start(
    ctx: click.Context,
    port: int,
    interval: float,
    max_concurrent: int,
    token_budget: int | None,
    foreground: bool,
) -> None:
    """Start delegate (web UI + agent orchestration)."""
    import webbrowser
    import time
    from delegate.daemon import start_daemon, is_running
    from delegate.doctor import run_doctor, print_doctor_report
    from delegate.fmt import success, get_auth_display, get_version

    hc_home = _get_home(ctx)

    # Run doctor check first — suppress output if all checks pass
    checks = run_doctor()
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
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return

    success(f"Starting delegate on port {port}...")

    if foreground:
        # foreground blocks forever; open browser from a background thread
        # after a short delay to let the server bind
        import threading

        def _open_browser() -> None:
            time.sleep(2)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_open_browser, daemon=True).start()

        start_daemon(
            hc_home,
            port=port,
            interval=interval,
            max_concurrent=max_concurrent,
            token_budget=token_budget,
            foreground=True,
        )
    else:
        result_pid = start_daemon(
            hc_home,
            port=port,
            interval=interval,
            max_concurrent=max_concurrent,
            token_budget=token_budget,
            foreground=False,
        )
        if result_pid:
            success(f"Delegate started (PID {result_pid})")
        else:
            success("Delegate started")

        success(f"UI: {url}")

        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            pass


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


@team.command("add")
@click.argument("name")
@click.option("--manager", required=True, help="Name of the manager agent.")
@click.option(
    "--agents", required=True,
    help="Comma-separated list of agents as name[:role].  "
         "Examples: 'alex:devops,nikhil:designer,john,mark:backend'.  "
         "Agents without a role default to 'engineer'.",
)
@click.option(
    "--repo", "repos", required=True, multiple=True,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Local repo path(s) for the team.  Repeat for multiple repos: --repo /path/a --repo /path/b",
)
@click.option("--interactive", is_flag=True, help="Prompt for bios and charter overrides.")
@click.pass_context
def team_create(
    ctx: click.Context,
    name: str,
    manager: str,
    agents: str,
    repos: tuple[str, ...],
    interactive: bool,
) -> None:
    """Create a new team."""
    from delegate.bootstrap import bootstrap
    from delegate.repo import register_repo
    from delegate.fmt import success, warn

    hc_home = _get_home(ctx)

    # Parse "name:role" pairs — role defaults to "engineer"
    parsed_agents: list[tuple[str, str]] = []
    for token in agents.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            agent_name, role = token.split(":", 1)
            parsed_agents.append((agent_name.strip(), role.strip()))
        else:
            parsed_agents.append((token, "engineer"))

    bootstrap(
        hc_home,
        team_name=name,
        manager=manager,
        agents=parsed_agents,
        interactive=interactive,
    )

    success(f"Created team '{name}'")

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
    labels = [f"{manager} (manager)"]
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
    if not td.is_dir():
        click.echo(f"Team '{name}' does not exist.")
        raise SystemExit(1)

    if not yes:
        click.confirm(
            f"Remove team '{name}' and all its data? This cannot be undone.",
            abort=True,
        )

    shutil.rmtree(td)
    success(f"Removed team '{name}'")


# ──────────────────────────────────────────────────────────────
# delegate agent add
# ──────────────────────────────────────────────────────────────

@main.group()
def agent() -> None:
    """Manage agents on a team."""
    pass


@agent.command("add")
@click.argument("team")
@click.argument("name")
@click.option(
    "--role", default="engineer",
    help="Role for the new agent (default: engineer).",
)
@click.option(
    "--seniority", default=None, type=click.Choice(["junior", "senior"]),
    help="Seniority level: junior (Sonnet) or senior (Opus). Default: junior for most roles, senior for manager.",
)
@click.option(
    "--bio", default=None,
    help="Short bio/description of the agent's strengths and focus.",
)
@click.pass_context
def agent_add(ctx: click.Context, team: str, name: str, role: str, seniority: str, bio: str | None) -> None:
    """Add a new agent to an existing team.

    TEAM is the team name.  NAME is the new agent's name.
    """
    from delegate.bootstrap import add_agent
    from delegate.fmt import success

    hc_home = _get_home(ctx)
    try:
        add_agent(hc_home, team_name=team, agent_name=name, role=role, seniority=seniority, bio=bio)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))

    success(f"Added agent '{name}' to team '{team}' (role: {role}, seniority: {seniority})")


# ──────────────────────────────────────────────────────────────
# delegate config set boss / source-repo
# ──────────────────────────────────────────────────────────────

@main.group()
def config() -> None:
    """Manage org-wide configuration."""
    pass


@config.group("set")
def config_set() -> None:
    """Set a configuration value."""
    pass


@config_set.command("boss")
@click.argument("name")
@click.pass_context
def config_set_boss(ctx: click.Context, name: str) -> None:
    """Set the org-wide boss name."""
    from delegate.config import set_boss

    hc_home = _get_home(ctx)
    set_boss(hc_home, name)
    click.echo(f"Boss set to: {name}")


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
    from delegate.config import get_boss, get_source_repo

    hc_home = _get_home(ctx)
    boss = get_boss(hc_home) or "(not set)"
    source_repo = get_source_repo(hc_home) or "(not set)"

    click.echo(f"Boss:        {boss}")
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
    "--approval",
    type=click.Choice(["auto", "manual"], case_sensitive=False),
    default=None,
    help="Merge approval mode: 'auto' (merge when QA approves) or 'manual' (require human approval). Default: manual.",
)
@click.option(
    "--test-cmd",
    default=None,
    help="Shell command to run tests (e.g. '/path/to/.venv/bin/python -m pytest -x -q').",
)
@click.pass_context
def repo_add(ctx: click.Context, team_name: str, path_or_url: str, repo_name: str | None, approval: str | None, test_cmd: str | None) -> None:
    """Register a repository for a team.

    TEAM_NAME is the team this repo belongs to.
    PATH_OR_URL is a local path or remote URL.
    """
    from delegate.repo import register_repo

    hc_home = _get_home(ctx)
    name = register_repo(hc_home, team_name, path_or_url, name=repo_name, approval=approval, test_cmd=test_cmd)
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


# ── delegate repo pre-merge-script ──

@repo.command("pre-merge-script")
@click.argument("team_name")
@click.argument("repo_name")
@click.option("--set", "script_path", default=None, help="Path to the pre-merge script (relative to repo root or absolute). Pass empty string to clear.")
@click.pass_context
def repo_pre_merge_script(ctx: click.Context, team_name: str, repo_name: str, script_path: str | None) -> None:
    """Show or set the pre-merge script for a repo.

    Without --set, displays the current pre-merge script.
    With --set <path>, sets the pre-merge script.
    With --set '', clears the pre-merge script.
    """
    from delegate.config import get_pre_merge_script, set_pre_merge_script

    hc_home = _get_home(ctx)

    if script_path is None:
        # Show current script
        script = get_pre_merge_script(hc_home, team_name, repo_name)
        if script:
            click.echo(f"Pre-merge script for '{repo_name}' (team: {team_name}): {script}")
        else:
            click.echo(f"No pre-merge script configured for repo '{repo_name}'.")
        return

    try:
        set_pre_merge_script(hc_home, team_name, repo_name, script_path)
    except KeyError as exc:
        raise click.ClickException(str(exc))

    if script_path:
        click.echo(f"Set pre-merge script for '{repo_name}' (team: {team_name}): {script_path}")
    else:
        click.echo(f"Cleared pre-merge script for '{repo_name}' (team: {team_name})")


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


if __name__ == "__main__":
    main()
