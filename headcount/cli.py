"""Headcount CLI entry point using Click.

Commands:
    headcount doctor                        — verify runtime dependencies
    headcount daemon start [--port N]       — start background daemon
    headcount daemon stop                   — stop running daemon
    headcount team create <name> ...        — create a new team
    headcount team list                     — list existing teams
    headcount config set director <name>    — set org-wide director name
    headcount config set source-repo <path> — set headcount source repo path
    headcount repo add <path_or_url> [--name N]  — register a repository
    headcount repo list                     — list registered repos
    headcount self-update                   — update headcount from source repo
"""

import subprocess
import sys
from pathlib import Path

import click

from headcount.paths import home as _home, teams_dir as _teams_dir


def _get_home(ctx: click.Context) -> Path:
    """Resolve headcount home from context or default."""
    return _home(ctx.obj.get("home_override") if ctx.obj else None)


@click.group()
@click.option(
    "--home", "home_override", type=click.Path(path_type=Path), default=None,
    envvar="HEADCOUNT_HOME",
    help="Override headcount home directory (default: ~/.headcount).",
)
@click.pass_context
def main(ctx: click.Context, home_override: Path | None) -> None:
    """Headcount — agentic team management system."""
    ctx.ensure_object(dict)
    ctx.obj["home_override"] = home_override


# ──────────────────────────────────────────────────────────────
# headcount doctor
# ──────────────────────────────────────────────────────────────

@main.command()
def doctor() -> None:
    """Verify that all runtime dependencies are installed."""
    from headcount.doctor import run_doctor, print_doctor_report

    checks = run_doctor()
    ok = print_doctor_report(checks)
    if not ok:
        raise SystemExit(1)


# ──────────────────────────────────────────────────────────────
# headcount daemon start / stop
# ──────────────────────────────────────────────────────────────

@main.group()
def daemon() -> None:
    """Manage the headcount daemon (web UI + agent orchestration)."""
    pass


@daemon.command("start")
@click.option("--port", type=int, default=8000, help="Port for the web UI (default: 8000).")
@click.option("--interval", type=float, default=1.0, help="Poll interval in seconds.")
@click.option("--max-concurrent", type=int, default=32, help="Max concurrent agents.")
@click.option("--token-budget", type=int, default=None, help="Default token budget per agent session.")
@click.option("--foreground", is_flag=True, help="Run in foreground instead of background.")
@click.pass_context
def daemon_start(
    ctx: click.Context,
    port: int,
    interval: float,
    max_concurrent: int,
    token_budget: int | None,
    foreground: bool,
) -> None:
    """Start the headcount daemon."""
    from headcount.daemon import start_daemon, is_running

    hc_home = _get_home(ctx)
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


@daemon.command("stop")
@click.pass_context
def daemon_stop(ctx: click.Context) -> None:
    """Stop the running headcount daemon."""
    from headcount.daemon import stop_daemon

    hc_home = _get_home(ctx)
    stopped = stop_daemon(hc_home)
    if stopped:
        click.echo("Daemon stopped")
    else:
        click.echo("No running daemon found")


@daemon.command("status")
@click.pass_context
def daemon_status(ctx: click.Context) -> None:
    """Check if the daemon is running."""
    from headcount.daemon import is_running

    hc_home = _get_home(ctx)
    alive, pid = is_running(hc_home)
    if alive:
        click.echo(f"Daemon running (PID {pid})")
    else:
        click.echo("Daemon not running")


# ──────────────────────────────────────────────────────────────
# headcount team create / list
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
    from headcount.bootstrap import bootstrap

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
# headcount config set director / source-repo
# ──────────────────────────────────────────────────────────────

@main.group()
def config() -> None:
    """Manage org-wide configuration."""
    pass


@config.group("set")
def config_set() -> None:
    """Set a configuration value."""
    pass


@config_set.command("director")
@click.argument("name")
@click.pass_context
def config_set_director(ctx: click.Context, name: str) -> None:
    """Set the org-wide director name."""
    from headcount.config import set_director

    hc_home = _get_home(ctx)
    set_director(hc_home, name)
    click.echo(f"Director set to: {name}")


@config_set.command("source-repo")
@click.argument("path", type=click.Path(path_type=Path))
@click.pass_context
def config_set_source_repo(ctx: click.Context, path: Path) -> None:
    """Set the path to the headcount source repository (for self-update)."""
    from headcount.config import set_source_repo

    hc_home = _get_home(ctx)
    set_source_repo(hc_home, path.resolve())
    click.echo(f"Source repo set to: {path.resolve()}")


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Show the current configuration."""
    from headcount.config import get_director, get_source_repo, get_repos

    hc_home = _get_home(ctx)
    director = get_director(hc_home) or "(not set)"
    source_repo = get_source_repo(hc_home) or "(not set)"
    repos = get_repos(hc_home)

    click.echo(f"Director:    {director}")
    click.echo(f"Source repo: {source_repo}")
    click.echo(f"Repos:       {len(repos)} registered")
    if repos:
        for name, meta in repos.items():
            click.echo(f"  - {name}: {meta.get('source', '?')}")


# ──────────────────────────────────────────────────────────────
# headcount repo add / list
# ──────────────────────────────────────────────────────────────

@main.group()
def repo() -> None:
    """Manage registered repositories."""
    pass


@repo.command("add")
@click.argument("path_or_url")
@click.option("--name", "repo_name", default=None, help="Name for the repo (default: derived from path/URL).")
@click.option(
    "--approval",
    type=click.Choice(["auto", "manual"], case_sensitive=False),
    default=None,
    help="Merge approval mode: 'auto' (merge when QA approves) or 'manual' (require human approval). Default: manual.",
)
@click.pass_context
def repo_add(ctx: click.Context, path_or_url: str, repo_name: str | None, approval: str | None) -> None:
    """Register a repository (local path or remote URL)."""
    from headcount.repo import register_repo

    hc_home = _get_home(ctx)
    name = register_repo(hc_home, path_or_url, name=repo_name, approval=approval)
    click.echo(f"Registered repo '{name}'")


@repo.command("list")
@click.pass_context
def repo_list(ctx: click.Context) -> None:
    """List registered repositories."""
    from headcount.repo import list_repos

    hc_home = _get_home(ctx)
    repos = list_repos(hc_home)
    if not repos:
        click.echo("No repositories registered.")
        return

    click.echo("Registered repos:")
    for name, meta in repos.items():
        click.echo(f"  - {name}: {meta.get('source', '?')}")


# ──────────────────────────────────────────────────────────────
# headcount migrate
# ──────────────────────────────────────────────────────────────

@main.command()
@click.argument("old_root", type=click.Path(exists=True, path_type=Path))
@click.argument("team_name")
@click.pass_context
def migrate(ctx: click.Context, old_root: Path, team_name: str) -> None:
    """Migrate old .standup state to the new ~/.headcount structure.

    OLD_ROOT is the directory containing .standup/ (e.g. /path/to/myteam).
    TEAM_NAME is the name for the team in the new structure.
    """
    from headcount.migrate import migrate as run_migrate, print_migration_report

    hc_home = _get_home(ctx)
    report = run_migrate(old_root.resolve(), team_name, hc_home=hc_home)
    print_migration_report(report)


# ──────────────────────────────────────────────────────────────
# headcount self-update
# ──────────────────────────────────────────────────────────────

@main.command("self-update")
@click.pass_context
def self_update(ctx: click.Context) -> None:
    """Update headcount from the source repository.

    Runs 'git pull' in the source repo and reinstalls the package.
    """
    from headcount.config import get_source_repo

    hc_home = _get_home(ctx)
    source_repo = get_source_repo(hc_home)
    if source_repo is None:
        click.echo("Error: No source repo configured.")
        click.echo("Set one with: headcount config set source-repo /path/to/headcount")
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
    click.echo("Reinstalling headcount...")
    install_cmd = [sys.executable, "-m", "pip", "install", "-e", str(source_repo)]

    # Prefer uv if available
    import shutil
    if shutil.which("uv"):
        install_cmd = ["uv", "pip", "install", "-e", str(source_repo)]

    result = subprocess.run(install_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        click.echo(f"Install failed:\n{result.stderr}")
        raise SystemExit(1)

    click.echo("Headcount updated successfully. ✓")


if __name__ == "__main__":
    main()
