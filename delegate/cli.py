"""Delegate CLI entry point using Click.

Commands:
    delegate doctor                                  — verify runtime dependencies
    delegate start [--port N]                        — start daemon (web UI + agents)
    delegate stop                                    — stop running daemon
    delegate status                                  — check if daemon is running
    delegate team create <name> ...                  — create a new team
    delegate team list                               — list existing teams
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

from delegate.paths import home as _home, teams_dir as _teams_dir


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
@click.option("--port", type=int, default=8000, help="Port for the web UI (default: 8000).")
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
    """Start the delegate daemon (web UI + agent orchestration)."""
    import webbrowser
    import time
    from delegate.daemon import start_daemon, is_running
    from delegate.doctor import run_doctor, print_doctor_report

    hc_home = _get_home(ctx)

    # Run doctor check first — suppress output if all checks pass
    checks = run_doctor()
    all_ok = all(c.passed for c in checks)
    if not all_ok:
        print_doctor_report(checks)
        raise SystemExit(1)

    alive, pid = is_running(hc_home)
    if alive:
        click.echo(f"Daemon already running (PID {pid})")
        return

    click.echo(f"Starting daemon on port {port}...")
    result_pid = start_daemon(
        hc_home,
        port=port,
        interval=interval,
        max_concurrent=max_concurrent,
        token_budget=token_budget,
        foreground=foreground,
    )
    if result_pid:
        click.echo(f"Daemon started (PID {result_pid})")
    elif not foreground:
        click.echo("Daemon started")

    # Open browser — give background daemon a moment to bind the port
    if not foreground:
        time.sleep(1.5)
    url = f"http://localhost:{port}"
    try:
        webbrowser.open(url)
    except Exception:
        click.echo(f"Open {url} in your browser")


@main.command()
@click.pass_context
def stop(ctx: click.Context) -> None:
    """Stop the running delegate daemon."""
    from delegate.daemon import stop_daemon

    hc_home = _get_home(ctx)
    stopped = stop_daemon(hc_home)
    if stopped:
        click.echo("Daemon stopped")
    else:
        click.echo("No running daemon found")


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Check if the daemon is running."""
    from delegate.daemon import is_running

    hc_home = _get_home(ctx)
    alive, pid = is_running(hc_home)
    if alive:
        click.echo(f"Daemon running (PID {pid})")
    else:
        click.echo("Daemon not running")


# ──────────────────────────────────────────────────────────────
# delegate team create / list
# ──────────────────────────────────────────────────────────────

@main.group()
def team() -> None:
    """Manage teams."""
    pass


@team.command("create")
@click.argument("name")
@click.option("--manager", required=True, help="Name of the manager agent.")
@click.option("--agents", default="", help="Comma-separated list of worker agent names.")
@click.option("--qa", default=None, help="Name of the QA agent.")
@click.option("--interactive", is_flag=True, help="Prompt for bios and charter overrides.")
@click.pass_context
def team_create(
    ctx: click.Context,
    name: str,
    manager: str,
    agents: str,
    qa: str | None,
    interactive: bool,
) -> None:
    """Create a new team."""
    from delegate.bootstrap import bootstrap

    hc_home = _get_home(ctx)
    worker_agents = [a.strip() for a in agents.split(",") if a.strip()]

    bootstrap(
        hc_home,
        team_name=name,
        manager=manager,
        agents=worker_agents,
        qa=qa,
        interactive=interactive,
    )

    all_names = [manager] + ([f"(qa) {qa}"] if qa else []) + worker_agents
    click.echo(f"Created team '{name}' with members: {', '.join(all_names)}")


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
        click.echo(f"  - {t}")


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
    "--role", default="worker",
    help="Role for the new agent (default: worker).",
)
@click.option(
    "--seniority", default="senior", type=click.Choice(["junior", "senior"]),
    help="Seniority level: junior (Sonnet) or senior (Opus). Default: senior.",
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

    hc_home = _get_home(ctx)
    try:
        add_agent(hc_home, team_name=team, agent_name=name, role=role, seniority=seniority, bio=bio)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))

    click.echo(f"Added agent '{name}' to team '{team}' (role: {role}, seniority: {seniority})")


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
