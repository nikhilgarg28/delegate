"""FastAPI web application for the Delegate UI.

Provides:
    GET  /            — HTML single-page app
    GET  /teams       — list teams (JSON)
    GET  /teams/{team}/tasks         — list tasks (JSON)
    GET  /teams/{team}/tasks/{id}/stats — task stats
    GET  /teams/{team}/tasks/{id}/diff  — task diff
    POST /teams/{team}/tasks/{id}/approve — approve task for merge
    POST /teams/{team}/tasks/{id}/reject  — reject task
    GET  /teams/{team}/messages      — chat/event log (JSON)
    POST /teams/{team}/messages      — user sends a message
    GET  /teams/{team}/agents        — list agents
    GET  /teams/{team}/agents/{name}/stats  — agent stats
    GET  /teams/{team}/agents/{name}/inbox  — agent inbox messages
    GET  /teams/{team}/agents/{name}/outbox — agent outbox messages
    GET  /teams/{team}/agents/{name}/logs   — agent worklog sessions

    Legacy convenience (aggregate across all teams, /api prefix):
    GET  /api/tasks       — list tasks across all teams
    GET  /api/messages    — messages across all teams
    POST /api/messages    — send message (includes team in body)

When started via the daemon, the daemon loop (message routing +
agent turn dispatch + merge processing) runs as an asyncio background
task inside the FastAPI lifespan, so uvicorn restarts everything together.
All agents are "always online" — the daemon dispatches turns directly
as asyncio tasks when agents have unread messages.
"""

import asyncio
import base64
import contextlib
import functools
import json
import logging
import mimetypes
import os
import shutil
import signal as signal_mod
import subprocess
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from delegate.paths import (
    home as _default_home,
    agents_dir as _agents_dir,
    agent_dir as _agent_dir,
    shared_dir as _shared_dir,
    team_dir as _team_dir,
    teams_dir as _teams_dir,
    resolve_team_uuid as _resolve_team,
    list_team_names as _list_team_names,
    get_bootstrap_id as _get_bootstrap_id,
)
from delegate.config import get_default_human
from delegate.names import pick_names
from delegate.task import list_tasks as _list_tasks, get_task as _get_task, get_task_diff as _get_task_diff, get_task_merge_preview as _get_merge_preview, get_task_commit_diffs as _get_commit_diffs, update_task as _update_task, change_status as _change_status, VALID_STATUSES, format_task_id
from delegate.chat import get_messages as _get_messages, get_task_stats as _get_task_stats, get_agent_stats as _get_agent_stats, get_team_agent_stats as _get_team_agent_stats, log_event as _log_event
from delegate.mailbox import send as _send, read_inbox as _read_inbox, read_outbox as _read_outbox, count_unread as _count_unread
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dedicated thread-pool for DB / file-IO operations.
#
# The default asyncio executor is shared by *everything* — using a dedicated
# pool prevents DB-heavy agent turns from starving HTTP handlers and other
# async work.  16 threads is a good balance: SQLite WAL allows concurrent
# readers and the busy_timeout handles writer contention.
# ---------------------------------------------------------------------------
_db_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="delegate-db")


async def _run_in_db_pool(fn, *args, **kwargs):
    """Run *fn(*args, **kwargs)* in the dedicated DB/IO thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_db_pool, functools.partial(fn, *args, **kwargs))


# ---------------------------------------------------------------------------
# Process pool for CPU / subprocess-heavy merge work.
#
# merge_once() is fully synchronous, takes only picklable args (Path, str),
# and returns picklable results (list[MergeResult]).  Running it in a
# ProcessPoolExecutor moves git rebase, test execution, and merge operations
# off the main process — bypassing the GIL and freeing the event loop core.
#
# The pool is created lazily during the lifespan (not at import time) to
# avoid spawning workers when the daemon is not enabled (e.g. tests).
# ---------------------------------------------------------------------------
_merge_pool: ProcessPoolExecutor | None = None


def _init_merge_worker():
    """Configure logging in merge worker processes.

    forkserver children get a fresh interpreter with default (WARNING-only)
    logging.  This sets up INFO-level output so merge diagnostics are visible.
    """
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(name)s %(levelname)s %(message)s",
    )


def _create_merge_pool() -> ProcessPoolExecutor:
    """Create a new merge ProcessPoolExecutor."""
    return ProcessPoolExecutor(
        max_workers=min(4, os.cpu_count() or 4),
        mp_context=multiprocessing.get_context("forkserver"),
        initializer=_init_merge_worker,
    )


async def _run_in_merge_pool(fn, *args, **kwargs):
    """Run *fn* in the process pool for CPU/subprocess-heavy merge work.

    Falls back to ``_run_in_db_pool`` if the merge pool is not initialised
    (e.g. daemon not enabled, tests running without lifespan).
    """
    global _merge_pool
    pool = _merge_pool
    if pool is None:
        return await _run_in_db_pool(fn, *args, **kwargs)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(pool, functools.partial(fn, *args, **kwargs))
    except BrokenProcessPool:
        logger.error("Merge process pool crashed — recreating")
        _merge_pool = _create_merge_pool()
        raise  # Let the caller's except handle the current failure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_teams(hc_home: Path) -> list[str]:
    """List all team names from the DB projects table (authoritative source).

    Falls back to project_map.json if the DB table is empty or inaccessible,
    then to a filesystem scan as a last resort.
    """
    # Primary: DB projects table
    try:
        from delegate.db import get_connection
        conn = get_connection(hc_home)
        try:
            rows = conn.execute("SELECT name FROM projects ORDER BY name").fetchall()
            names = [r["name"] for r in rows]
            if names:
                return names
        finally:
            conn.close()
    except Exception:
        pass
    # Fallback: team_map.json
    names = _list_team_names(hc_home)
    if names:
        return sorted(names)
    # Last resort: filesystem scan of team dirs
    td = _teams_dir(hc_home)
    if td.is_dir():
        from delegate.paths import resolve_team_name as _rtn
        return sorted({_rtn(hc_home, d.name) for d in td.iterdir() if d.is_dir()})
    return []


def _first_team(hc_home: Path) -> str:
    """Return the first team name (for single-team operations)."""
    teams = _list_teams(hc_home)
    return teams[0] if teams else "default"


def _bootstrap_human_member(hc_home: Path) -> None:
    """One-time bootstrap: create the human member entry if none exists yet.

    Runs at daemon startup.  If the members directory is empty (fresh install),
    detects the human name from git config / Unix login and creates the member
    file immediately — before any team or project is bootstrapped.

    This avoids storing the placeholder name "human" (the old fallback) and
    ensures the real name is available from the first API call.

    Idempotent — no-ops if at least one member already exists.
    """
    from delegate.paths import members_dir
    from delegate.bootstrap import _detect_human_name
    from delegate.config import add_member
    md = members_dir(hc_home)
    if md.is_dir() and any(md.iterdir()):
        return  # Member already exists — nothing to do
    name = _detect_human_name()
    add_member(hc_home, name)
    logger.info("Bootstrapped human member: '%s'", name)


def _reconcile_team_map(hc_home: Path) -> None:
    """Ensure project_map.json and the DB projects table are in sync.

    Runs once at daemon startup.  Handles three failure modes:

    1. **project_map.json missing** — rebuilt from DB projects table.
    2. **DB projects table empty** — populated from project_map.json.
    3. **Both have entries** — merged (union); each source may have
       entries the other lacks.

    This makes the system self-healing after partial data loss (e.g. a
    user deletes protected/ but not projects/, or vice-versa).
    """
    from delegate.paths import (
        register_team_path,
        list_team_names,
    )
    from delegate.db import get_connection

    # Read both sources
    map_data: dict[str, str] = {}
    for name in _list_team_names(hc_home):
        uid = _resolve_team(hc_home, name)
        if uid != name:  # only include real mappings
            map_data[name] = uid

    db_data: dict[str, str] = {}
    try:
        conn = get_connection(hc_home)
        try:
            for row in conn.execute("SELECT name, project_id FROM projects").fetchall():
                db_data[row["name"]] = row["project_id"]
        finally:
            conn.close()
    except Exception:
        pass

    # Reconcile: DB → project_map.json (only if directory exists)
    for name, uid in db_data.items():
        if name not in map_data:
            team_root = _team_dir(hc_home, name)
            if team_root.is_dir():
                logger.info("Reconcile: adding team '%s' to project_map.json from DB", name)
                register_team_path(hc_home, name, uid)
            else:
                logger.info("Reconcile: skipping team '%s' — directory missing", name)

    # Reconcile: project_map.json → DB (only if directory exists)
    for name, uid in map_data.items():
        if name not in db_data:
            team_root = _team_dir(hc_home, name)
            if not team_root.is_dir():
                logger.info("Reconcile: skipping team '%s' — directory missing", name)
                continue
            logger.info("Reconcile: adding team '%s' to DB from project_map.json", name)
            try:
                conn = get_connection(hc_home)
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO projects (name, project_id) VALUES (?, ?)",
                        (name, uid),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                logger.warning("Could not reconcile team '%s' into DB", name, exc_info=True)


def _agent_last_active_at(agent_dir: Path) -> str | None:
    """Return ISO timestamp of the agent's most recent activity.

    Checks worklog files in the agent's logs/ directory and uses the
    most recent mtime.  Falls back to the state.yaml mtime if no
    worklogs exist.  Returns None if nothing is found.
    """
    latest_mtime: float | None = None

    logs_dir = agent_dir / "logs"
    if logs_dir.is_dir():
        for f in logs_dir.iterdir():
            if f.suffix == ".md":
                try:
                    mt = f.stat().st_mtime
                    if latest_mtime is None or mt > latest_mtime:
                        latest_mtime = mt
                except OSError:
                    continue

    # Fall back to state.yaml mtime
    if latest_mtime is None:
        state_file = agent_dir / "state.yaml"
        if state_file.exists():
            try:
                latest_mtime = state_file.stat().st_mtime
            except OSError:
                pass

    if latest_mtime is not None:
        return datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()
    return None


def _agent_current_task(hc_home: Path, team: str, agent_name: str, ip_tasks: list[dict] | None = None) -> dict | None:
    """Return {id, title} of the agent's in_progress task, or None.

    When *ip_tasks* is provided it is used directly (avoids re-scanning
    the tasks directory for every agent).
    """
    if ip_tasks is None:
        ip_tasks = _list_tasks(hc_home, team, status="in_progress", assignee=agent_name)
    for t in ip_tasks:
        if t.get("assignee") == agent_name:
            return {"id": t["id"], "title": t["title"]}
    return None


def _is_local_agent(hc_home: Path, team: str, agent: str) -> bool:
    """Check if an agent runs locally (no host assigned, or host is None).

    Agents with a non-None ``host`` field in state.yaml are satellite agents
    and should be skipped by the coordinator's daemon loop.
    """
    ad = _agent_dir(hc_home, team, agent)
    state_file = ad / "state.yaml"
    if not state_file.exists():
        return True  # unknown agent — treat as local
    state = yaml.safe_load(state_file.read_text()) or {}
    return state.get("host") is None


def _list_team_agents(hc_home: Path, team: str) -> list[dict]:
    """List AI agents for a team (excludes human members)."""
    ad = _agents_dir(hc_home, team)
    agents = []
    if not ad.is_dir():
        return agents

    # Build set of human member names for fast lookup
    from delegate.config import get_human_members
    human_names = {m["name"] for m in get_human_members(hc_home)}

    # Pre-load all in_progress tasks once (lightweight — avoids per-agent scans)
    try:
        ip_tasks = _list_tasks(hc_home, team, status="in_progress")
    except FileNotFoundError:
        ip_tasks = []

    for d in sorted(ad.iterdir()):
        state_file = d / "state.yaml"
        if not d.is_dir() or not state_file.exists():
            continue
        # Skip human members
        if d.name in human_names:
            continue
        state = yaml.safe_load(state_file.read_text()) or {}
        # Also skip legacy "boss" role agents
        if state.get("role") == "boss":
            continue
        unread = _count_unread(hc_home, team, d.name)
        agents.append({
            "name": d.name,
            "role": state.get("role", "engineer"),
            "model": state.get("model", "sonnet"),
            "pid": True,  # All agents are always online — daemon dispatches turns
            "unread_inbox": unread,
            "team": team,
            "host": state.get("host"),  # None = local, str = satellite name
            "last_active_at": _agent_last_active_at(d),
            "current_task": _agent_current_task(hc_home, team, d.name, ip_tasks),
        })
    return agents


# ---------------------------------------------------------------------------
# Startup greeting — dynamic, time-aware message from manager
# ---------------------------------------------------------------------------

def _build_first_run_greeting(
    hc_home: Path,
    team: str,
    manager: str,
    human: str,
    agent_count: int,
    has_repos: bool,
) -> str:
    """Build the welcome message shown on first ever session.

    Introduces Delegate and guides the user to their first action.
    """
    lines: list[str] = []

    lines.append(
        f"Hi {human.capitalize()}! I'm your delegate — I manage a team of "
        f"{agent_count} engineer{'s' if agent_count != 1 else ''} "
        f"ready to build software for you."
    )

    lines.append("")  # blank line

    if has_repos:
        lines.append(
            "Tell me what you'd like built and I'll plan the work, "
            "assign it to the team, manage code reviews, and merge it in."
        )
    else:
        lines.append(
            "To get started, I need a repo to work in. "
            "Just tell me the path — for example: "
            '"Please add the repo at /path/to/my-project"'
        )

    lines.append("")

    # Tips
    tips = [
        "Send me a task in plain English and I'll handle the rest",
        "Use `/shell <cmd>` to run any shell command right here",
        "Press `?` to see all keyboard shortcuts",
    ]
    for tip in tips:
        lines.append(f"• {tip}")

    return "\n".join(lines)


def _build_greeting(
    hc_home: Path,
    team: str,
    manager: str,
    human: str,
    now_utc: "datetime",
    last_seen: "datetime | None" = None,
) -> str:
    """Build a context-aware startup greeting from the manager.

    Takes into account:
    - Time of day (in the user's local timezone via the system clock)
    - Active in-progress tasks (brief status summary)
    - Activity since last_seen (if provided and recent)
    """
    from delegate.task import list_tasks
    from delegate.mailbox import read_inbox

    # Time-of-day awareness (use local time, not UTC)
    local_hour = datetime.now().hour
    if local_hour < 5:
        time_greeting = "Burning the midnight oil"
    elif local_hour < 12:
        time_greeting = "Good morning"
    elif local_hour < 17:
        time_greeting = "Good afternoon"
    elif local_hour < 21:
        time_greeting = "Good evening"
    else:
        time_greeting = "Working late"

    # Gather task context
    try:
        active = list_tasks(hc_home, team, status="in_progress")
        review = list_tasks(hc_home, team, status="in_review")
        approval = list_tasks(hc_home, team, status="in_approval")
        failed = list_tasks(hc_home, team, status="merge_failed")
    except Exception:
        active = review = approval = failed = []

    # Build "while you were away" context if last_seen is recent enough
    away_parts: list[str] = []
    if last_seen and (now_utc - last_seen) < timedelta(hours=24):
        try:
            # Tasks completed since last_seen
            all_tasks = list_tasks(hc_home, team, status="done")
            completed_since = [
                t for t in all_tasks
                if t.get("completed_at") and
                   datetime.fromisoformat(t["completed_at"].replace("Z", "+00:00")) > last_seen
            ]
            if completed_since:
                away_parts.append(f"{len(completed_since)} task{'s' if len(completed_since) != 1 else ''} completed")

            # Messages to human since last_seen
            messages = read_inbox(hc_home, team, human, unread_only=False)
            new_messages = [
                m for m in messages
                if datetime.fromisoformat(m.time.replace("Z", "+00:00")) > last_seen
            ]
            if new_messages:
                away_parts.append(f"{len(new_messages)} new message{'s' if len(new_messages) != 1 else ''}")
        except Exception:
            pass

    # Build status line
    status_parts: list[str] = []
    if active:
        status_parts.append(f"{len(active)} task{'s' if len(active) != 1 else ''} in progress")
    if review:
        status_parts.append(f"{len(review)} awaiting review")
    if approval:
        status_parts.append(f"{len(approval)} ready for approval")
    if failed:
        status_parts.append(f"{len(failed)} with merge issues")

    # Assemble
    lines = [f"{time_greeting} — I'm your delegate, managing this team."]

    if away_parts:
        lines.append("While you were away: " + ", ".join(away_parts) + ".")

    if status_parts:
        lines.append("Current board: " + ", ".join(status_parts) + ".")
    else:
        lines.append("The board is clear — ready for new work.")

    lines.append("Send me tasks, questions, or anything you need the team on.")

    return " ".join(lines)


# ---------------------------------------------------------------------------
# Auto-stage processing (workflow engine)
# ---------------------------------------------------------------------------

def _notify_manager_sync(hc_home: Path, team: str, body: str) -> None:
    """Send a system notification to the team's manager (synchronous helper)."""
    from delegate.bootstrap import get_member_by_role
    from delegate.mailbox import send as send_message

    try:
        manager = get_member_by_role(hc_home, team, "manager")
        if manager:
            send_message(hc_home, team, "system", manager, body)
    except Exception:
        logger.debug("Could not notify manager for team %s", team, exc_info=True)


def _process_auto_stages(hc_home: Path, team: str) -> None:
    """Find tasks in auto stages and run their action() hooks.

    An auto stage (e.g. ``Merging``) has ``auto = True``.  When a task
    sits in such a stage, the runtime calls ``action(ctx)`` which must
    return the next Stage class.  The task is then transitioned.

    This replaces the hardcoded ``merge_once()`` for workflow-managed tasks.
    """
    from delegate.task import list_tasks, change_status, format_task_id, get_task
    from delegate.workflow import load_workflow_cached, ActionError
    from delegate.workflows.core import Context
    from delegate.chat import log_event

    try:
        all_tasks = list_tasks(hc_home, team)
    except Exception:
        return

    for task in all_tasks:
        wf_name = task.get("workflow", "")
        wf_version = task.get("workflow_version", 0)
        if not wf_name or not wf_version:
            continue

        try:
            wf = load_workflow_cached(hc_home, team, wf_name, wf_version)
        except (FileNotFoundError, KeyError, ValueError):
            continue

        current = task.get("status", "")
        if current not in wf.stage_map:
            continue

        stage_cls = wf.stage_map[current]
        if not stage_cls.auto:
            continue

        # This task is in an auto stage — run its action
        task_id = task["id"]
        try:
            # Re-fetch to get latest state
            fresh_task = get_task(hc_home, team, task_id)
            ctx = Context(hc_home, team, fresh_task)
            stage = stage_cls()
            next_stage_cls = stage.action(ctx)

            if next_stage_cls is not None and hasattr(next_stage_cls, '_key') and next_stage_cls._key:
                # Transition to the next stage
                change_status(hc_home, team, task_id, next_stage_cls._key)
                logger.info(
                    "Auto-stage %s → %s for %s",
                    current, next_stage_cls._key, format_task_id(task_id),
                )
                # Notify manager when a task reaches a terminal state
                if wf.is_terminal(next_stage_cls._key):
                    _notify_manager_sync(
                        hc_home, team,
                        f"Task {format_task_id(task_id)} completed (status: {next_stage_cls._key}).",
                    )
        except ActionError as exc:
            # Unrecoverable error → transition to 'error' state
            logger.error(
                "Auto-stage action failed for %s in %s: %s",
                format_task_id(task_id), current, exc,
            )
            if "error" in wf.stage_map:
                try:
                    change_status(hc_home, team, task_id, "error")
                except Exception:
                    logger.exception("Failed to transition %s to error state", format_task_id(task_id))
            else:
                log_event(
                    hc_home, team,
                    f"{format_task_id(task_id)} auto-action failed: {exc}",
                    task_id=task_id,
                )
        except Exception as exc:
            logger.exception(
                "Unexpected error in auto-stage for %s (%s): %s",
                format_task_id(task_id), current, exc,
            )


# ---------------------------------------------------------------------------
# Daemon loop — runs as a background asyncio task inside the lifespan
# ---------------------------------------------------------------------------

# Module-level tracking of active agent asyncio tasks for shutdown
_active_agent_tasks: dict[tuple[str, str], asyncio.Task] = {}
_active_merge_tasks: set[asyncio.Task] = set()
_shutdown_flag: bool = False
_exchange = None  # TelephoneExchange; set during lifespan
_daemon_wakeup: asyncio.Event | None = None  # Set during lifespan


def _wake_daemon():
    """Thread-safe daemon wakeup — safe to call from sync HTTP handlers.

    Uses ``call_soon_threadsafe`` because FastAPI sync endpoints run in
    uvicorn's thread pool, not on the event loop thread.
    """
    evt = _daemon_wakeup
    if evt is None:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(evt.set)
    except RuntimeError:
        pass  # No running loop (tests, shutdown)


def _ensure_task_infra(
    hc_home: Path,
    team: str,
    infra_ready: set[tuple[str, int]],
) -> None:
    """Ensure worktrees exist for active tasks with resolved dependencies.

    Called from the daemon loop (in a thread) **before** dispatching
    agent turns.  This guarantees that an agent never receives a turn
    for a task whose worktree hasn't been created yet.

    **Dependency gating**: Worktrees are only created for tasks whose
    ``depends_on`` dependencies are ALL resolved (done/cancelled).
    This prevents agents from starting work before prerequisite tasks
    are complete.

    Worktree creation runs in the daemon process — which is **not**
    sandboxed — so it can write to the real repo's ``.git/`` directory.

    The *infra_ready* set acts as an in-memory cache to avoid redundant
    filesystem checks on every poll cycle.  It is cleared when a task
    transitions to ``done`` or ``cancelled``.
    """
    from delegate.repo import (
        create_task_worktree,
        get_task_worktree_path,
        BranchExistsError,
        ensure_default_branch_checked_out,
        get_repo_path,
        list_repos,
    )
    from delegate.task import _all_deps_resolved
    from delegate.env import write_env_scripts

    # Self-heal: verify all registered repos have their default branch
    # checked out in the main working copy.  This prevents cascading
    # failures if a delegate branch was accidentally left checked out.
    for repo_name in list_repos(hc_home, team):
        try:
            repo_dir = get_repo_path(hc_home, team, repo_name).resolve()
            if repo_dir.is_dir():
                ensure_default_branch_checked_out(repo_dir)
        except Exception:
            pass  # best-effort; don't block infra setup

    # Active statuses that need worktrees
    active_statuses = ("todo", "in_progress")

    for status in active_statuses:
        try:
            tasks = _list_tasks(hc_home, team, status=status)
        except Exception:
            continue

        for task in tasks:
            task_id = task["id"]
            key = (team, task_id)
            if key in infra_ready:
                continue  # already known-good

            repos: list[str] = task.get("repo", [])
            branch: str = task.get("branch", "")
            if not repos or not branch:
                # No repos or no branch — nothing to set up
                infra_ready.add(key)
                continue

            # --- Dependency gating: skip tasks with unresolved deps ---
            if not _all_deps_resolved(hc_home, team, task):
                logger.debug(
                    "Skipping worktree for %s/%s — dependencies not resolved",
                    team, format_task_id(task_id),
                )
                continue

            # Check if ALL worktrees exist
            all_exist = True
            for repo_name in repos:
                wt = get_task_worktree_path(hc_home, team, repo_name, task_id)
                if not wt.is_dir():
                    all_exist = False
                    break

            if all_exist:
                infra_ready.add(key)
                continue

            # Create missing worktrees
            try:
                for repo_name in repos:
                    wt = get_task_worktree_path(hc_home, team, repo_name, task_id)
                    if not wt.is_dir():
                        create_task_worktree(
                            hc_home, team, repo_name, task_id, branch=branch,
                        )
                        logger.info(
                            "Daemon created worktree for %s/%s (%s)",
                            team, format_task_id(task_id), repo_name,
                        )
                        # Generate .delegate/setup.sh & premerge.sh if missing
                        try:
                            if write_env_scripts(wt):
                                logger.info(
                                    "Generated env scripts for %s/%s (%s)",
                                    team, format_task_id(task_id), repo_name,
                                )
                        except Exception:
                            logger.warning(
                                "Failed to generate env scripts for %s/%s",
                                team, format_task_id(task_id),
                                exc_info=True,
                            )
                infra_ready.add(key)
            except BranchExistsError as exc:
                # Branch exists but worktree doesn't — mark task as blocked
                # so it stops retrying and surfaces to the manager.
                logger.warning(
                    "Branch conflict for %s/%s: %s",
                    team, format_task_id(task_id), exc,
                )
                try:
                    from delegate.task import update_task
                    update_task(
                        hc_home, team, task_id,
                        status_detail=f"Blocked: {exc}",
                    )
                    from delegate.chat import log_event
                    log_event(
                        hc_home, team,
                        f"{format_task_id(task_id)} blocked — branch already exists, worktree cannot be created",
                        task_id=task_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to mark task %s/%s as blocked after branch conflict",
                        team, format_task_id(task_id),
                    )
                infra_ready.add(key)  # stop retrying
            except Exception:
                logger.exception(
                    "Failed to create worktree infra for %s/%s",
                    team, format_task_id(task_id),
                )


def _dispatch_review_request(
    hc_home: Path,
    team: str,
    reviewer_name: str,
    recently_dispatched: set[tuple[str, int]],
) -> None:
    """Send a review request to the reviewer agent for the top-priority in_approval task.

    Only one review at a time — skips if the top candidate was already dispatched.
    """
    from delegate.config import get_reviewer_config
    from delegate.task import list_tasks as _list_tasks, format_task_id
    from delegate.review import get_current_review
    from delegate.mailbox import send as send_message, read_inbox

    cfg = get_reviewer_config(hc_home, team)
    if cfg["mode"] != "ai":
        return

    # Collect in_approval tasks without a verdict
    candidates = []
    for task in _list_tasks(hc_home, team, status="in_approval"):
        review = get_current_review(hc_home, team, task["id"])
        if review and review.get("verdict") is not None:
            continue
        candidates.append(task)

    if not candidates:
        return

    # Skip if the reviewer has ANY unclaimed messages — one review at a time.
    # This prevents flooding the reviewer with duplicate requests.
    # Check seen_at IS NULL (not just processed_at IS NULL) so stale
    # claimed messages from previous sessions don't block new dispatches.
    from delegate.mailbox import has_unclaimed_messages
    if has_unclaimed_messages(hc_home, team, reviewer_name):
        return

    # Sort by merge priority
    from delegate.merge import _sort_merge_candidates
    candidates = _sort_merge_candidates(hc_home, team, candidates)

    # Pick only the top candidate
    task = candidates[0]
    task_id = task["id"]

    # Idempotency: skip if already dispatched recently
    key = (team, task_id)
    if key in recently_dispatched:
        return

    title = task.get("title", f"T{task_id:04d}")
    send_message(
        hc_home, team, "system", reviewer_name,
        f"Please review {format_task_id(task_id)}: {title}",
        task_id=task_id,
    )
    recently_dispatched.add(key)
    logger.info("Dispatched review request for %s to %s", format_task_id(task_id), reviewer_name)


async def _daemon_loop(
    hc_home: Path,
    interval: float,
    max_concurrent: int,
    default_token_budget: int | None,
    exchange: "TelephoneExchange | None" = None,
) -> None:
    """Route messages, dispatch agent turns, and process merges (all teams).

    All agents are "always online".  Instead of spawning subprocesses,
    the daemon dispatches ``run_turn()`` as asyncio tasks when an agent
    has unread mail.  A semaphore enforces *max_concurrent* across all
    teams.

    Before dispatching turns for each team, ``_ensure_task_infra()``
    creates any missing worktrees for active tasks — this runs in the
    unsandboxed daemon process so ``git worktree add`` can write to
    the real repo's ``.git/`` directory.

    When *exchange* is provided, persistent ``Telephone`` subprocesses
    are reused across turns (the normal production path).  When ``None``,
    each turn falls back to a one-shot ``sdk_query()`` call.
    """
    import time as _time

    from delegate.runtime import run_turn, list_ai_agents
    from delegate.merge import merge_once
    from delegate.bootstrap import get_member_by_role
    from delegate.mailbox import send as send_message, agents_with_unread, agents_with_unread_prioritized
    from delegate.config import get_human_members
    from delegate.task import format_task_id, list_tasks as _list_tasks_fn, IN_PROGRESS_STATUSES, QUEUED_STATUSES
    from delegate.config import SYSTEM_USER
    from delegate.activity import broadcast_turn_event

    _ACTIVE_STATUSES = QUEUED_STATUSES | IN_PROGRESS_STATUSES  # precomputed union

    logger.info("Daemon loop started — polling every %.1fs", interval)

    human_names = [m["name"] for m in get_human_members(hc_home)]

    sem = asyncio.Semaphore(max_concurrent)
    merge_sems: dict[str, asyncio.Semaphore] = {}  # per-team merge semaphores
    in_flight: set[tuple[str, str]] = set()  # (team, agent) pairs currently running
    in_flight_lock = asyncio.Lock()  # guards check-then-add on in_flight

    # Stall detector state
    last_stall_check: float = 0.0
    recently_nudged: set[tuple[str, str, int]] = set()  # (team, agent, task_id)
    last_nudge_clear: float = 0.0

    # Reviewer dispatch state — prevents duplicate review requests
    recently_dispatched: set[tuple[str, int]] = set()  # (team, task_id)
    last_dispatch_clear: float = 0.0

    # In-memory cache: (team, task_id) pairs whose worktrees are confirmed.
    # Cleared when tasks transition to done/cancelled.
    infra_ready: set[tuple[str, int]] = set()

    def _notify_manager(team: str, body: str) -> None:
        """Send a system notification to the team's manager (if any)."""
        try:
            manager = get_member_by_role(hc_home, team, "manager")
            if manager:
                send_message(hc_home, team, "system", manager, body)
        except Exception:
            logger.debug("Could not notify manager for team %s", team, exc_info=True)

    # --- Delayed startup notification ---
    async def _delayed_startup_notification() -> None:
        """Send startup notification after 60s delay, only if there are active tasks."""
        await asyncio.sleep(60)
        try:
            teams = _list_teams(hc_home)
            for team in teams:
                from delegate.task import list_tasks as _list_tasks_all
                try:
                    all_tasks = _list_tasks_all(hc_home, team)
                    active = [t for t in all_tasks if t.get("status") not in ("done", "cancelled")]
                    # Skip notification if no active tasks
                    if not active:
                        continue
                    summary = (
                        f"Daemon started. Team '{team}' has {len(all_tasks)} total tasks "
                        f"({len(active)} active).\n"
                        f"Check status of all tasks and agents -- assign/reassign as needed and send messages to wake up relevant agents."
                    )
                    _notify_manager(team, summary)
                except Exception:
                    # Only notify if there might be active tasks (can't determine due to error)
                    _notify_manager(
                        team,
                        f"Daemon started for team '{team}'. Check status of all tasks and agents -- assign/reassign as needed and send messages to wake up relevant agents."
                    )
        except Exception:
            logger.debug("Startup notification failed", exc_info=True)

    # Start the delayed notification task in the background
    asyncio.create_task(_delayed_startup_notification())

    async def _dispatch_turn(team: str, agent: str) -> None:
        """Dispatch and run one turn, then remove from in_flight."""
        async with sem:
            try:
                result = await run_turn(hc_home, team, agent, exchange=exchange)
                if result.error:
                    logger.warning(
                        "Turn error | agent=%s | team=%s | error=%s",
                        agent, team, result.error,
                    )
                else:
                    total = result.tokens_in + result.tokens_out
                    logger.info(
                        "Turn complete | agent=%s | team=%s | tokens=%d | cost=$%.4f",
                        agent, team, total, result.cost_usd,
                    )
            except asyncio.CancelledError:
                logger.info("Turn cancelled | agent=%s | team=%s", agent, team)
                broadcast_turn_event('turn_interrupted', agent, team=team)
                raise
            except Exception:
                logger.exception("Uncaught error in turn | agent=%s | team=%s", agent, team)
            finally:
                in_flight.discard((team, agent))

    # --- Greeting logic ---
    # Greeting is now handled by the frontend on page load / return-from-away.
    # The daemon doesn't send greetings on startup since it can't know if anyone
    # is looking at the screen. Frontend uses localStorage to track last-greeted
    # timestamp and only triggers greeting after meaningful absence (30+ min).

    # --- Per-team processing (runs concurrently across teams) ---
    async def _process_team(team: str) -> None:
        """Handle infra, dispatch, and auto-stages for one team.

        Called concurrently for every team via asyncio.gather() so that
        slow worktree creation in one team doesn't block dispatch in others.
        """
        if _shutdown_flag:
            return

        # --- Ensure worktree infrastructure for active tasks ---
        try:
            await _run_in_db_pool(
                _ensure_task_infra, hc_home, team, infra_ready,
            )
        except Exception:
            logger.exception("Error ensuring task infra for team %s", team)

        # Find agents with unread messages and dispatch turns.
        # Both DB queries run in the dedicated thread pool so the
        # event loop stays free for HTTP / SSE traffic.
        ai_agents = set(await _run_in_db_pool(list_ai_agents, hc_home, team))
        unread_prioritized = await _run_in_db_pool(
            agents_with_unread_prioritized, hc_home, team, human_names,
        )
        needing_turn = [
            a for a in unread_prioritized
            if a in ai_agents and _is_local_agent(hc_home, team, a)
        ]
        for agent in needing_turn:
            if _shutdown_flag:
                break

            key = (team, agent)
            async with in_flight_lock:
                if key in in_flight:
                    continue
                in_flight.add(key)
            agent_task = asyncio.create_task(_dispatch_turn(team, agent))
            _active_agent_tasks[key] = agent_task
            agent_task.add_done_callback(lambda t, k=key: _active_agent_tasks.pop(k, None))

        # Clear recently_dispatched every 5 minutes
        nonlocal last_dispatch_clear
        now_d = _time.monotonic()
        if now_d - last_dispatch_clear > 300:
            recently_dispatched.clear()
            last_dispatch_clear = now_d

        # Process auto stages — per-team semaphore so different teams
        # can merge/approve in parallel (previously a single global sem
        # serialized all teams).
        if not _shutdown_flag:
            team_merge_sem = merge_sems.setdefault(team, asyncio.Semaphore(1))

            async def _run_auto_stages(t: str) -> None:
                async with team_merge_sem:
                    reviewer_name = await _run_in_db_pool(
                        get_member_by_role, hc_home, t, "reviewer",
                    )
                    if reviewer_name:
                        try:
                            _dispatch_review_request(
                                hc_home, t, reviewer_name, recently_dispatched,
                            )
                        except Exception:
                            logger.debug("_dispatch_review_request error for %s", t, exc_info=True)
                    else:
                        from delegate.auto_approve import auto_approve_once
                        try:
                            await _run_in_db_pool(auto_approve_once, hc_home, t)
                        except Exception:
                            logger.debug("auto_approve_once error for %s", t, exc_info=True)

                    results = await _run_in_merge_pool(merge_once, hc_home, t)
                    for mr in results:
                        if mr.success:
                            logger.info("Merged %s in %s: %s", mr.task_id, t, mr.message)
                            infra_ready.discard((t, mr.task_id))
                            _notify_manager(
                                t,
                                f"Task {format_task_id(mr.task_id)} has been merged successfully. Check status of tasks and agents -- make any necessary assignment decisions.",
                            )
                        else:
                            logger.warning("Merge failed %s in %s: %s", mr.task_id, t, mr.message)
                            _notify_manager(
                                t,
                                f"Task {format_task_id(mr.task_id)} merge failed: {mr.message}",
                            )

                    await _run_in_db_pool(_process_auto_stages, hc_home, t)

            merge_task = asyncio.create_task(_run_auto_stages(team))
            _active_merge_tasks.add(merge_task)
            merge_task.add_done_callback(_active_merge_tasks.discard)

    # --- Main loop ---
    while True:
        try:
            # Check shutdown flag at the top of each iteration
            global _shutdown_flag
            if _shutdown_flag:
                logger.info("Shutdown flag set — exiting daemon loop")
                break

            teams = await _run_in_db_pool(_list_teams, hc_home)
            human_name = get_default_human(hc_home)

            # Process all teams concurrently — each team's infra setup,
            # agent dispatch, and auto-stages run in parallel.
            await asyncio.gather(
                *[_process_team(t) for t in teams],
                return_exceptions=True,
            )

            # --- Stall detector: nudge worker agents with assigned tasks but no activity ---
            now = _time.monotonic()
            if now - last_stall_check > 600:
                last_stall_check = now
                if now - last_nudge_clear > 3000:
                    recently_nudged.clear()
                    last_nudge_clear = now
                for team in teams:
                    try:
                        manager, all_tasks = await asyncio.gather(
                            _run_in_db_pool(get_member_by_role, hc_home, team, "manager"),
                            _run_in_db_pool(_list_tasks_fn, hc_home, team),
                        )
                        active_assigned = [
                            t for t in all_tasks
                            if t.get("assignee")
                            and t.get("assignee") != manager
                            and t.get("status") in _ACTIVE_STATUSES
                        ]
                        if not active_assigned:
                            continue
                        unread_agents_raw, ai_agents_raw = await asyncio.gather(
                            _run_in_db_pool(agents_with_unread, hc_home, team),
                            _run_in_db_pool(list_ai_agents, hc_home, team),
                        )
                        unread_agents = set(unread_agents_raw)
                        ai_agent_set = set(ai_agents_raw)
                        for t in active_assigned:
                            assignee = t["assignee"]
                            tid = t["id"]
                            key = (team, assignee, tid)
                            if assignee not in ai_agent_set:
                                continue
                            if assignee in unread_agents:
                                continue
                            async with in_flight_lock:
                                if (team, assignee) in in_flight:
                                    continue
                            if key in recently_nudged:
                                continue
                            title = t.get("title", f"T{tid:04d}")
                            status = t.get("status", "unknown")
                            nudge_body = (
                                f"You are assigned to T{tid:04d} ({title}) which is {status}. "
                                f"Please check the task and continue working on it, or report any blockers."
                            )
                            await _run_in_db_pool(
                                send_message,
                                hc_home, team, SYSTEM_USER, assignee,
                                nudge_body, task_id=tid,
                            )
                            recently_nudged.add(key)
                            logger.info("Stall detector nudged %s for T%04d in %s", assignee, tid, team)
                    except Exception:
                        logger.exception("Stall detector error for team %s", team)
        except asyncio.CancelledError:
            logger.info("Daemon loop cancelled")
            raise
        except Exception:
            logger.exception("Error during daemon cycle")

        # Wait for either a wakeup signal (new message sent) or the poll
        # interval to elapse.  clear() before wait() ensures signals
        # arriving between wakeup and the next wait are not lost.
        if _daemon_wakeup is not None:
            _daemon_wakeup.clear()
            try:
                await asyncio.wait_for(_daemon_wakeup.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)


def _find_frontend_dir() -> Path | None:
    """Locate the ``frontend/`` source directory (only exists in dev checkouts)."""
    # Walk upward from delegate/ looking for frontend/build.js
    candidate = Path(__file__).resolve().parent.parent / "frontend"
    if (candidate / "build.js").is_file():
        return candidate
    return None


def _start_esbuild_watch(frontend_dir: Path) -> subprocess.Popen | None:
    """Spawn ``node build.js --watch`` and return the process handle.

    Returns None (with a log warning) if node/npm are missing.
    """
    node = shutil.which("node")
    if node is None:
        logger.warning("Frontend watcher: 'node' not found on PATH — skipping")
        return None

    # Ensure node_modules are installed
    if not (frontend_dir / "node_modules").is_dir():
        npm = shutil.which("npm")
        if npm is None:
            logger.warning("Frontend watcher: 'npm' not found on PATH — skipping")
            return None
        logger.info("Installing frontend dependencies …")
        subprocess.run([npm, "install"], cwd=str(frontend_dir), check=True)

    build_js = str(frontend_dir / "build.js")
    logger.info("Starting esbuild watcher: node %s --watch", build_js)
    proc = subprocess.Popen(
        [node, build_js, "--watch"],
        cwd=str(frontend_dir),
        # Stay in parent's process group so child dies when parent is killed.
        # (start_new_session=True caused orphaned esbuild on CI.)
    )
    return proc


async def _reap_orphaned_subprocesses(
    exchange: "TelephoneExchange",
    interval: float = 120.0,
) -> None:
    """Periodically kill child Claude processes not tracked by the exchange.

    Safety net for the subprocess leak described in
    ``docs/bug-daemon-subprocess-leak.md``.  Every *interval* seconds,
    enumerates child processes of this daemon, identifies those that look
    like Claude SDK subprocesses (``claude`` in the binary name), and
    kills any whose PID is not held by a tracked Telephone.
    """
    import signal as _signal
    from delegate.daemon import find_claude_pids

    await asyncio.sleep(60)  # let the first batch of telephones spin up

    while True:
        try:
            # Collect PIDs the exchange knows about.
            tracked_pids: set[int] = set()
            for tel in exchange._telephones.values():
                for client in (tel._client, tel._stale_client):
                    if client is None:
                        continue
                    transport = getattr(client, "_transport", None) or getattr(
                        getattr(client, "_query", None), "transport", None
                    )
                    proc = getattr(transport, "_process", None) if transport else None
                    pid = getattr(proc, "pid", None)
                    if pid:
                        tracked_pids.add(pid)

            orphan_pids = find_claude_pids(
                ppid_filter=os.getpid(),
                exclude_pids=tracked_pids,
            )
            for pid in orphan_pids:
                try:
                    logger.warning("Reaper: killing orphaned Claude subprocess PID %d", pid)
                    os.kill(pid, _signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            if orphan_pids:
                logger.info("Reaper: killed %d orphaned subprocess(es)", len(orphan_pids))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("Reaper error", exc_info=True)

        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start/stop the daemon loop and frontend watcher with the server.

    The esbuild watcher is started automatically whenever a ``frontend/``
    source directory is detected (i.e. running from a source checkout).
    In a pip-installed deployment there is no ``frontend/`` and the watcher
    is silently skipped — the pre-built assets in ``delegate/static/`` are used.
    """
    from delegate.runtime import TelephoneExchange

    hc_home = app.state.hc_home
    enable = os.environ.get("DELEGATE_DAEMON", "").lower() in ("1", "true", "yes")

    # Reset shutdown flag (for server restart/reload scenarios)
    global _shutdown_flag
    _shutdown_flag = False

    task = None
    reaper_task = None
    esbuild_proc: subprocess.Popen | None = None
    exchange: TelephoneExchange | None = None
    daemon_lock_fd: int | None = None

    if enable:
        # Acquire the daemon singleton lock.  In foreground mode the lock
        # is acquired by daemon.py directly; for background mode (spawned
        # via subprocess) we acquire it here — inside the child process.
        from delegate.daemon import _acquire_lock, _release_lock
        try:
            daemon_lock_fd = _acquire_lock(hc_home)
        except RuntimeError:
            logger.error("Another daemon is already running — refusing to start")
            raise
        interval = float(os.environ.get("DELEGATE_INTERVAL", "1.0"))
        max_concurrent = int(os.environ.get("DELEGATE_MAX_CONCURRENT", "256"))
        budget_str = os.environ.get("DELEGATE_TOKEN_BUDGET")
        token_budget = int(budget_str) if budget_str else None

        # Kill orphaned Claude processes from any previous daemon before
        # spawning new ones.  After restart, old orphans are reparented to
        # PID 1 and invisible to the periodic reaper.
        from delegate.daemon import sweep_orphaned_claude_processes
        sweep_orphaned_claude_processes()

        exchange = TelephoneExchange()
        global _exchange
        _exchange = exchange

        # Process pool for merge operations — runs git rebase, tests, and
        # merges in separate OS processes, bypassing the GIL.
        global _merge_pool
        _merge_pool = _create_merge_pool()

        # Event for instant daemon wakeup when messages arrive.
        global _daemon_wakeup
        _daemon_wakeup = asyncio.Event()

        # Reconcile project_map.json with the DB projects table.
        # If either source is incomplete (e.g. after a partial nuke),
        # this ensures both are in sync so resolve_team_uuid() and
        # _list_teams() work correctly.
        _reconcile_team_map(hc_home)

        task = asyncio.create_task(
            _daemon_loop(hc_home, interval, max_concurrent, token_budget, exchange=exchange)
        )

        # Safety-net: periodically reap orphaned Claude subprocesses that
        # survived disconnect (see docs/bug-daemon-subprocess-leak.md).
        reaper_task = asyncio.create_task(
            _reap_orphaned_subprocesses(exchange)
        )

    # Always do a one-shot frontend build if frontend/ exists and node is available
    frontend_dir = _find_frontend_dir()
    if frontend_dir:
        node = shutil.which("node")
        if node:
            # Ensure node_modules are installed
            if not (frontend_dir / "node_modules").is_dir():
                npm = shutil.which("npm")
                if npm:
                    logger.info("Installing frontend dependencies...")
                    subprocess.run([npm, "install"], cwd=str(frontend_dir), check=True)

            build_js = str(frontend_dir / "build.js")
            logger.info("Building frontend assets...")
            try:
                subprocess.run(
                    [node, build_js],
                    cwd=str(frontend_dir),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                logger.info("Frontend build complete")
            except subprocess.CalledProcessError as e:
                logger.warning("Frontend build failed: %s", e.stderr or e.stdout or str(e))

    # Auto-start frontend watcher only in dev mode (delegate start --dev)
    dev_mode = os.environ.get("DELEGATE_DEV", "").lower() in ("1", "true", "yes")
    if dev_mode:
        if frontend_dir is None:
            frontend_dir = _find_frontend_dir()
        if frontend_dir:
            esbuild_proc = _start_esbuild_watch(frontend_dir)

    yield

    # Shut down esbuild watcher
    if esbuild_proc is not None:
        logger.info("Stopping esbuild watcher (PID %d)", esbuild_proc.pid)
        try:
            esbuild_proc.terminate()
        except OSError:
            pass
        try:
            esbuild_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                esbuild_proc.kill()
            except OSError:
                pass

    # Cancel the reaper task
    if enable and reaper_task is not None:
        reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task

    if task is not None:
        # Set shutdown flag before cancelling the daemon loop
        _shutdown_flag = True
        logger.info("Setting shutdown flag and cancelling daemon loop")

        # Cancel the daemon loop
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("Daemon loop stopped")
        _daemon_wakeup = None

        # Cancel all in-flight merge tasks
        if _active_merge_tasks:
            logger.info("Cancelling %d merge task(s)...", len(_active_merge_tasks))
            # Snapshot the set before iteration to avoid mutation during iteration
            merge_tasks_snapshot = list(_active_merge_tasks)
            for merge_task in merge_tasks_snapshot:
                merge_task.cancel()

            try:
                await asyncio.wait_for(
                    asyncio.gather(*merge_tasks_snapshot, return_exceptions=True),
                    timeout=5.0
                )
                logger.info("All merge tasks cancelled")
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout waiting for merge tasks — %d task(s) still running",
                    len([t for t in _active_merge_tasks if not t.done()])
                )
            _active_merge_tasks.clear()

        # Cancel all in-flight agent tasks with timeout
        if _active_agent_tasks:
            logger.info("Waiting for %d agent session(s) to finish...", len(_active_agent_tasks))
            # Snapshot the values before iteration to avoid mutation during iteration
            agent_tasks_snapshot = list(_active_agent_tasks.values())
            for agent_task in agent_tasks_snapshot:
                agent_task.cancel()

            # Wait for tasks to finish with 10 second timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*agent_tasks_snapshot, return_exceptions=True),
                    timeout=10.0
                )
                logger.info("All agent sessions finished")
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout waiting for agent sessions — %d task(s) still running",
                    len([t for t in agent_tasks_snapshot if not t.done()])
                )
            _active_agent_tasks.clear()

        # Close all persistent Telephone subprocesses
        if exchange is not None:
            logger.info("Closing all Telephone conversations...")
            try:
                await asyncio.wait_for(exchange.close_all(), timeout=10.0)
                logger.info("All Telephone conversations closed")
            except asyncio.TimeoutError:
                logger.warning("Timeout closing Telephone conversations")
            except Exception:
                logger.exception("Error closing Telephone conversations")
            _exchange = None

        # Shut down the merge process pool — running merges complete,
        # queued merges are cancelled (they retry on next daemon start).
        if _merge_pool is not None:
            logger.info("Shutting down merge process pool...")
            _merge_pool.shutdown(wait=True, cancel_futures=True)
            _merge_pool = None

    # Clean up PID file (background daemon may exit without going through
    # stop_daemon — e.g. port conflict, crash, OS signal).
    if enable:
        from delegate.paths import daemon_pid_path
        pid_path = daemon_pid_path(hc_home)
        pid_path.unlink(missing_ok=True)
        logger.info("Cleaned up daemon PID file")

    # Release daemon singleton lock
    if daemon_lock_fd is not None:
        from delegate.daemon import _release_lock
        _release_lock(daemon_lock_fd)
        logger.info("Released daemon singleton lock")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(hc_home: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI app.

    When *hc_home* is ``None`` (e.g. when called by uvicorn as a factory),
    configuration is read from environment variables.
    """
    if hc_home is None:
        hc_home = _default_home(
            override=Path(os.environ["DELEGATE_HOME"]) if "DELEGATE_HOME" in os.environ else None
        )

    # Unified logging (file + console) — safe to call multiple times
    from delegate.logging_setup import configure_logging
    configure_logging(hc_home, console=True)

    # Apply any pending database migrations on startup (per team).
    from delegate.db import ensure_schema
    for team_name in _list_teams(hc_home):
        ensure_schema(hc_home, team_name)

    # One-time bootstrap: create the human member entry if no members exist yet.
    # On fresh installs this ensures the real name is stored from the first startup.
    _bootstrap_human_member(hc_home)

    app = FastAPI(title="Delegate UI", lifespan=_lifespan)
    app.state.hc_home = hc_home

    # CORS — restrict to localhost origins by default.
    from fastapi.middleware.cors import CORSMiddleware
    port = int(os.environ.get("DELEGATE_PORT", "3548"))
    cors_origins = [
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Config endpoint ---

    @app.get("/config")
    def get_config():
        """Return app configuration (human member, etc.) for the frontend."""
        human = get_default_human(hc_home)
        return {
            "boss_name": human,  # backward compat
            "human_name": human,
            "hc_home": str(hc_home),
            "bootstrap_id": _get_bootstrap_id(hc_home),
        }

    # --- Bootstrap endpoint (all initial data in one call) ---

    def _get_teams_list():
        """Shared teams-list logic for /teams and /bootstrap.

        Uses cheap directory counts for agent/human counts instead of
        loading full agent data (YAML, unread, last_active) per agent.
        """
        from delegate.db import get_connection
        from delegate.config import get_human_members

        conn = get_connection(hc_home)
        try:
            teams_rows = conn.execute(
                "SELECT name, project_id, created_at FROM projects ORDER BY created_at ASC"
            ).fetchall()
            human_names = {m["name"] for m in get_human_members(hc_home)}

            # Batch task counts in one query (tasks.team column not renamed — see V018)
            task_counts: dict[str, int] = {}
            for r in conn.execute(
                "SELECT team, COUNT(*) as cnt FROM tasks GROUP BY team"
            ).fetchall():
                task_counts[r["team"]] = r["cnt"]

            result = []
            for row in teams_rows:
                team_name = row["name"]
                # Skip teams whose directory no longer exists (orphaned DB rows)
                team_root = _team_dir(hc_home, team_name)
                if not team_root.is_dir():
                    continue
                # Cheap dir scan: count agent vs human dirs (no YAML/DB per agent)
                agent_count = 0
                human_count = 0
                team_agents_dir = _agents_dir(hc_home, team_name)
                if team_agents_dir.is_dir():
                    for d in team_agents_dir.iterdir():
                        if d.is_dir():
                            if d.name in human_names:
                                human_count += 1
                            else:
                                agent_count += 1
                # task_counts is keyed by the project column value which is now a UUID
                team_uuid = row["project_id"]
                result.append({
                    "name": team_name,
                    "team_id": team_uuid,
                    "created_at": row["created_at"],
                    "agent_count": agent_count,
                    "task_count": task_counts.get(team_uuid, task_counts.get(team_name, 0)),
                    "human_count": human_count,
                })
            return result
        finally:
            conn.close()

    @app.get("/bootstrap")
    def bootstrap(team: str | None = None):
        """Return config + teams + first team's data in one request.

        Eliminates the waterfall of sequential fetches on initial page load.
        The frontend calls this once instead of /config → /teams → /tasks + /agents + /messages + /stats.
        """
        human = get_default_human(hc_home)
        team_list = _get_teams_list()

        # Determine which team to load initial data for
        team_names = [t["name"] for t in team_list]
        initial_team = None
        if team and team in team_names:
            initial_team = team
        elif team_names:
            initial_team = team_names[0]

        result = {
            "config": {
                "boss_name": human,
                "human_name": human,
                "hc_home": str(hc_home),
                "bootstrap_id": _get_bootstrap_id(hc_home),
            },
            "teams": team_list,
            "initial_team": initial_team,
        }

        if initial_team:
            agents_data = _list_team_agents(hc_home, initial_team)

            # Batch agent stats: single GROUP BY query + single list_tasks
            # instead of N separate connections
            agent_names = [a["name"] for a in agents_data]
            agent_stats = _get_team_agent_stats(hc_home, initial_team, agent_names)

            tasks_data = _list_tasks(hc_home, initial_team)
            messages_data = _get_messages(hc_home, initial_team, limit=100)

            from delegate.config import get_reviewer_config, get_auto_approver_config, get_task_freeze_config, get_max_tasks_config
            reviewer_cfg = get_reviewer_config(hc_home, initial_team)
            result["initial_data"] = {
                "tasks": tasks_data,
                "agents": agents_data,
                "agent_stats": agent_stats,
                "messages": messages_data,
                "reviewer": reviewer_cfg,
                "auto_approver": get_auto_approver_config(hc_home, initial_team),  # compat
                "task_freeze": get_task_freeze_config(hc_home, initial_team),
                "max_tasks": get_max_tasks_config(hc_home, initial_team),
            }

        return result

    # --- Team endpoints ---

    @app.get("/teams")
    def get_teams():
        """List all teams with metadata from the global DB.

        Returns: List of team objects with name, team_id, created_at, agent_count, task_count
        """
        return _get_teams_list()

    # --- Reviewer endpoints ---

    @app.get("/teams/{team}/reviewer")
    def get_reviewer(team: str):
        """Return the reviewer config for a team."""
        from delegate.config import get_reviewer_config
        return get_reviewer_config(hc_home, team)

    @app.post("/teams/{team}/reviewer")
    def post_reviewer(team: str, body: dict):
        """Update reviewer config (mode, threshold, model, auto_merge)."""
        from delegate.config import update_reviewer_config
        kwargs = {}
        if "mode" in body:
            kwargs["mode"] = str(body["mode"])
        if "threshold" in body:
            kwargs["threshold"] = float(body["threshold"])
        if "model" in body:
            kwargs["model"] = str(body["model"])
        if "auto_merge" in body:
            kwargs["auto_merge"] = bool(body["auto_merge"])
        return update_reviewer_config(hc_home, team, **kwargs)

    # --- Auto-approver endpoints (deprecated — kept for backwards compat) ---

    @app.get("/teams/{team}/auto-approver")
    def get_auto_approver(team: str):
        """**Deprecated** — use /teams/{team}/reviewer instead."""
        from delegate.config import get_auto_approver_config
        return get_auto_approver_config(hc_home, team)

    @app.post("/teams/{team}/auto-approver")
    def post_auto_approver(team: str, body: dict):
        """**Deprecated** — use /teams/{team}/reviewer instead."""
        from delegate.config import update_auto_approver_config
        kwargs = {}
        if "enabled" in body:
            kwargs["enabled"] = bool(body["enabled"])
        if "threshold" in body:
            kwargs["threshold"] = float(body["threshold"])
        if "model" in body:
            kwargs["model"] = str(body["model"])
        return update_auto_approver_config(hc_home, team, **kwargs)

    # --- Task-freeze endpoints ---

    @app.get("/teams/{team}/task-freeze")
    def get_task_freeze(team: str):
        """Return the task-freeze config for a team."""
        from delegate.config import get_task_freeze_config
        return get_task_freeze_config(hc_home, team)

    @app.post("/teams/{team}/task-freeze")
    def post_task_freeze(team: str, body: dict):
        """Update task-freeze config (enabled)."""
        from delegate.config import update_task_freeze_config
        kwargs = {}
        if "enabled" in body:
            kwargs["enabled"] = bool(body["enabled"])
        return update_task_freeze_config(hc_home, team, **kwargs)

    # --- Max-tasks limit endpoints ---

    @app.get("/teams/{team}/max-tasks")
    def get_max_tasks(team: str):
        """Return the max-tasks config for a team."""
        from delegate.config import get_max_tasks_config
        return get_max_tasks_config(hc_home, team)

    @app.post("/teams/{team}/max-tasks")
    def post_max_tasks(team: str, body: dict):
        """Update max-tasks config (enabled, limit_in_progress, limit_queued)."""
        from delegate.config import update_max_tasks_config
        kwargs = {}
        if "enabled" in body:
            kwargs["enabled"] = bool(body["enabled"])
        if "limit_in_progress" in body:
            kwargs["limit_in_progress"] = int(body["limit_in_progress"])
        if "limit_queued" in body:
            kwargs["limit_queued"] = int(body["limit_queued"])
        return update_max_tasks_config(hc_home, team, **kwargs)

    # --- Workflow endpoints (team-scoped) ---

    @app.get("/teams/{team}/workflows")
    def get_team_workflows(team: str):
        """List all registered workflows for a team."""
        from delegate.workflow import list_workflows as _list_wf
        return _list_wf(hc_home, team)

    @app.get("/teams/{team}/workflows/{name}")
    def get_team_workflow(team: str, name: str, version: int | None = None):
        """Get a specific workflow definition."""
        from delegate.workflow import load_workflow, get_latest_version

        if version is None:
            version = get_latest_version(hc_home, team, name)
            if version is None:
                raise HTTPException(404, f"Workflow '{name}' not found for team '{team}'")

        try:
            wf = load_workflow(hc_home, team, name, version)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, str(exc))

        return {
            "name": wf.name,
            "version": wf.version,
            "stages": [
                {
                    "key": cls._key,
                    "label": cls.label,
                    "terminal": cls.terminal,
                    "auto": cls.auto,
                }
                for cls in wf.stages
            ],
            "transitions": {k: sorted(v) for k, v in wf.transitions.items()},
            "initial": wf.initial_stage,
            "terminals": sorted(wf.terminal_stages),
        }

    # --- Task endpoints (team-scoped) ---

    @app.get("/teams/{team}/tasks")
    def get_team_tasks(team: str, status: str | None = None, assignee: str | None = None):
        return _list_tasks(hc_home, team, status=status, assignee=assignee)

    @app.get("/teams/{team}/tasks/merge-order")
    def get_merge_order(team: str):
        from delegate.merge import _sort_merge_candidates
        all_approval = _list_tasks(hc_home, team, status="in_approval")
        sorted_tasks = _sort_merge_candidates(hc_home, team, all_approval)
        return {"order": [t["id"] for t in sorted_tasks]}

    # --- Message endpoints (team-scoped) ---

    @app.get("/teams/{team}/messages")
    def get_team_messages(team: str, since: str | None = None, between: str | None = None, type: str | None = None, limit: int | None = None, before_id: int | None = None):
        between_tuple = None
        if between:
            parts = [p.strip() for p in between.split(",")]
            if len(parts) == 2:
                between_tuple = (parts[0], parts[1])
        return _get_messages(hc_home, team, since=since, between=between_tuple, msg_type=type, limit=limit, before_id=before_id)

    class SendMessage(BaseModel):
        team: str | None = None
        recipient: str
        content: str

    @app.post("/teams/{team}/messages")
    def post_team_message(team: str, msg: SendMessage):
        """Human sends a message to any agent in the team."""
        human_name = get_default_human(hc_home)
        team_agents = _list_team_agents(hc_home, team)
        agent_names = {a["name"] for a in team_agents}
        if msg.recipient not in agent_names:
            raise HTTPException(
                status_code=403,
                detail=f"Recipient '{msg.recipient}' is not an agent in team '{team}'",
            )
        _send(hc_home, team, human_name, msg.recipient, msg.content)
        _wake_daemon()
        return {"status": "queued"}

    @app.post("/teams/{team}/greet")
    def greet_team(team: str, last_seen: str | None = None):
        """Send a welcome greeting from the team's manager to the human.
        Called by the frontend after meaningful absence (30+ min).

        On the very first session (zero messages in the team DB), sends a
        special first-run welcome that introduces Delegate.

        Args:
            last_seen: ISO timestamp of when user was last active (optional)
        """
        from delegate.bootstrap import get_member_by_role
        from delegate.mailbox import read_inbox
        from delegate.repo import list_repos

        human_name = get_default_human(hc_home)
        manager_name = get_member_by_role(hc_home, team, "manager")

        if not manager_name:
            raise HTTPException(
                status_code=404,
                detail=f"No manager found for team '{team}'",
            )

        now_utc = datetime.now(timezone.utc)

        # ── First-run detection (legacy fallback) ──
        # For projects created via the UI, the first-run welcome is sent
        # during POST /projects.  This path is a fallback for CLI-bootstrapped
        # teams or old installs where no welcome was sent at creation time.
        try:
            all_messages = _get_messages(hc_home, team, limit=1)
            is_first_run = len(all_messages) == 0
        except Exception:
            is_first_run = False

        if is_first_run:
            # Count AI agents (excluding manager)
            ai_agents = [
                a for a in _list_team_agents(hc_home, team)
                if a.get("role") != "manager"
            ]
            has_repos = bool(list_repos(hc_home, team))

            greeting = _build_first_run_greeting(
                hc_home, team, manager_name, human_name,
                agent_count=len(ai_agents),
                has_repos=has_repos,
            )
            _send(hc_home, team, manager_name, human_name, greeting)
            logger.info(
                "First-run welcome sent by %s to %s | team=%s | agents=%d | repos=%s",
                manager_name, human_name, team,
                len(ai_agents), has_repos,
            )
            return {"status": "sent"}

        # ── Regular greeting ──
        # Check if manager sent a message to human in the last 15 minutes
        # If so, skip the greeting to avoid noise
        try:
            recent_messages = read_inbox(hc_home, team, human_name, unread_only=False)
            cutoff = now_utc - timedelta(minutes=15)
            recent_manager_msg = any(
                m.sender == manager_name and
                datetime.fromisoformat(m.time.replace("Z", "+00:00")) > cutoff
                for m in recent_messages
            )
            if recent_manager_msg:
                logger.info(
                    "Skipping greeting — manager %s sent message to %s in last 15 min | team=%s",
                    manager_name, human_name, team,
                )
                return {"status": "skipped"}
        except Exception:
            pass  # If we can't check, proceed with greeting

        # Parse last_seen if provided
        last_seen_dt = None
        if last_seen:
            try:
                last_seen_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            except Exception:
                pass

        greeting = _build_greeting(hc_home, team, manager_name, human_name, now_utc, last_seen_dt)
        _send(
            hc_home, team,
            manager_name,
            human_name,
            greeting,
        )
        logger.info(
            "Manager %s sent greeting to %s | team=%s | last_seen=%s",
            manager_name, human_name, team, last_seen or "none",
        )
        return {"status": "sent"}

    # --- File upload endpoints ---

    @app.post("/teams/{team}/uploads")
    async def upload_files(team: str, files: list[UploadFile]):
        """Upload one or more files to team uploads directory.

        Args:
            team: Team name
            files: List of uploaded files (multipart/form-data)

        Returns:
            JSON with list of uploaded file metadata

        Raises:
            400: Invalid file type or file too large
            413: Payload too large
        """
        from delegate.uploads import (
            validate_file,
            validate_file_size,
            store_upload,
            MAX_TOTAL_SIZE,
        )

        # Check total size
        total_size = 0
        file_data = []

        for file in files:
            content = await file.read()
            size = len(content)
            total_size += size

            if total_size > MAX_TOTAL_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"Total upload size exceeds limit: {MAX_TOTAL_SIZE} bytes",
                )

            file_data.append((file.filename or "unnamed", content, size))

        # Validate and store each file
        uploaded = []
        uploads_dir = _team_dir(hc_home, team) / "uploads"
        now = datetime.now(timezone.utc)
        year = now.strftime("%Y")
        month = now.strftime("%m")

        for filename, content, size in file_data:
            # Validate size
            size_valid, size_error = validate_file_size(size)
            if not size_valid:
                raise HTTPException(status_code=400, detail=size_error)

            # Validate file type
            is_valid, mime_type, error_msg = validate_file(content, filename)
            if not is_valid:
                raise HTTPException(status_code=400, detail=error_msg)

            # Store file
            try:
                final_filename, final_path = store_upload(
                    content, filename, uploads_dir, year, month
                )
            except IOError as e:
                raise HTTPException(status_code=500, detail=str(e))

            # Build response -- use absolute path so the frontend can pass it
            # directly to the file viewer without any path manipulation.
            stored_path = str(final_path)
            url = str(final_path)

            uploaded.append({
                "original_name": filename,
                "stored_path": stored_path,
                "url": url,
                "size_bytes": size,
                "mime_type": mime_type,
            })

        return {"uploaded": uploaded}

    @app.get("/teams/{team}/uploads/{year}/{month}/{filename}")
    def serve_file(team: str, year: str, month: str, filename: str):
        """Serve an uploaded file with appropriate headers.

        Args:
            team: Team name
            year: Year subdirectory (e.g., "2026")
            month: Month subdirectory (e.g., "02")
            filename: Filename to serve

        Returns:
            File content with appropriate headers

        Raises:
            403: Path traversal attempt
            404: File not found
        """
        from delegate.uploads import safe_path

        uploads_dir = _team_dir(hc_home, team) / "uploads"
        user_path = f"{year}/{month}/{filename}"

        # Validate path (prevent traversal)
        file_path = safe_path(uploads_dir, user_path)
        if file_path is None:
            raise HTTPException(status_code=403, detail="Invalid path")

        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        # Detect MIME type
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            mime_type = "application/octet-stream"

        # Determine Content-Disposition
        # SVG: force download (XSS prevention)
        # Images/PDF: inline (show in browser)
        # Others: attachment (download)
        if filename.lower().endswith(".svg"):
            content_disposition = f'attachment; filename="{filename}"'
        elif mime_type.startswith("image/") or mime_type == "application/pdf":
            content_disposition = "inline"
        else:
            content_disposition = f'attachment; filename="{filename}"'

        # Read file content
        with file_path.open("rb") as f:
            content = f.read()

        # Build response with security headers
        headers = {
            "Content-Type": mime_type,
            "Content-Disposition": content_disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=86400",
        }

        # SVG: add CSP header
        if filename.lower().endswith(".svg"):
            headers["Content-Security-Policy"] = "default-src 'none'"

        return Response(content=content, headers=headers, media_type=mime_type)

    @app.get("/teams/{team}/cost-summary")
    def get_cost_summary(team: str):
        """Return cost analytics: today, this week, and top tasks by cost."""
        from delegate.db import get_connection
        t = _resolve_team(hc_home, team)
        conn = get_connection(hc_home, team)
        # Use local timezone for day/week boundaries so "today" and "this week"
        # reflect the user's local calendar day, not UTC.
        now_local = datetime.now().astimezone()

        # Today: midnight in local time, converted to UTC for comparison
        midnight_today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_today_utc = midnight_today_local.astimezone(timezone.utc)

        # This week: Monday 00:00 local time, converted to UTC
        days_since_monday = now_local.weekday()
        monday_this_week_local = (now_local - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        monday_this_week_utc = monday_this_week_local.astimezone(timezone.utc)

        # Query today
        today_rows = conn.execute("""
            SELECT
                COALESCE(SUM(cost_usd), 0) as total_cost,
                COUNT(DISTINCT task_id) as task_count
            FROM sessions
            WHERE started_at >= ? AND project_uuid = ?
        """, (midnight_today_utc.isoformat(), t)).fetchone()

        today_cost = today_rows[0] or 0.0
        today_task_count = today_rows[1] or 0
        today_avg = today_cost / today_task_count if today_task_count > 0 else 0.0

        # Query this week
        week_rows = conn.execute("""
            SELECT
                COALESCE(SUM(cost_usd), 0) as total_cost,
                COUNT(DISTINCT task_id) as task_count
            FROM sessions
            WHERE started_at >= ? AND project_uuid = ?
        """, (monday_this_week_utc.isoformat(), t)).fetchone()

        week_cost = week_rows[0] or 0.0
        week_task_count = week_rows[1] or 0
        week_avg = week_cost / week_task_count if week_task_count > 0 else 0.0

        # Top 3 tasks by total cost (all time)
        top_tasks_rows = conn.execute("""
            SELECT
                s.task_id,
                t.title,
                SUM(s.cost_usd) as total_cost
            FROM sessions s
            LEFT JOIN tasks t ON s.task_id = t.id
            WHERE s.task_id IS NOT NULL AND s.project_uuid = ?
            GROUP BY s.task_id
            ORDER BY total_cost DESC
            LIMIT 3
        """, (t,)).fetchall()

        top_tasks = [
            {
                "task_id": row[0],
                "title": row[1] or f"Task {row[0]}",
                "cost_usd": row[2] or 0.0,
            }
            for row in top_tasks_rows
        ]

        return {
            "today": {
                "total_cost_usd": round(today_cost, 2),
                "task_count": today_task_count,
                "avg_cost_per_task": round(today_avg, 2),
            },
            "this_week": {
                "total_cost_usd": round(week_cost, 2),
                "task_count": week_task_count,
                "avg_cost_per_task": round(week_avg, 2),
            },
            "top_tasks": top_tasks,
        }

    # --- Magic commands endpoints ---

    class AddAgentRequest(BaseModel):
        name: str | None = None
        role: str | None = None
        model: str | None = None
        bio: str | None = None

    @app.post("/teams/{team}/agents/add")
    def add_agent_endpoint(team: str, req: AddAgentRequest):
        from delegate.bootstrap import add_agent
        kwargs = {"hc_home": hc_home, "team_name": team, "agent_name": req.name}
        if req.role is not None:
            kwargs["role"] = req.role
        if req.model is not None:
            kwargs["model"] = req.model
        if req.bio is not None:
            kwargs["bio"] = req.bio
        try:
            agent_name = add_agent(**kwargs)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        resolved_model = req.model or "sonnet"
        return {"message": f"Added agent '{agent_name}' to team '{team}' (role: {req.role or 'engineer'}, model: {resolved_model})"}

    # --- Project (team) creation from UI ---

    def _rollback_project(home: Path, team_name: str) -> None:
        """Best-effort cleanup of a partially created project.

        Removes the team directory, DB rows, project_map entry, and
        protected metadata that ``bootstrap()`` may have created.
        Called when project creation fails after bootstrap to avoid
        leaving orphaned state.
        """
        import shutil
        from delegate.db import get_connection as _gc
        from delegate.paths import (
            team_dir as _td,
            protected_team_dir as _ptd,
            unregister_team_path,
        )

        # Remove team working directory
        td = _td(home, team_name)
        if td.is_dir():
            shutil.rmtree(td, ignore_errors=True)

        # Remove protected metadata
        try:
            ptd = _ptd(home, team_name)
            if ptd.is_dir():
                shutil.rmtree(ptd, ignore_errors=True)
        except Exception:
            pass

        # Remove from project_map.json
        try:
            unregister_team_path(home, team_name)
        except Exception:
            pass

        # Remove DB rows (look up UUID before deleting the row)
        try:
            conn = _gc(home)
            try:
                row = conn.execute(
                    "SELECT project_id FROM projects WHERE name = ?", (team_name,)
                ).fetchone()
                team_uuid = row["project_id"] if row else None
                conn.execute("DELETE FROM projects WHERE name = ?", (team_name,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            team_uuid = None

        # Soft-delete db_ids entries
        if team_uuid:
            try:
                from delegate.db_ids import soft_delete_team
                conn = _gc(home)
                try:
                    soft_delete_team(conn, team_uuid)
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass

        logger.info("Rolled back partially created project '%s'", team_name)

    class CreateProjectRequest(BaseModel):
        name: str
        repo_path: str
        agent_count: int = 2
        model: str = "sonnet"

    @app.post("/projects")
    def create_project(req: CreateProjectRequest):
        """Create a new project (team) from the UI.

        Bootstraps the team, registers the repo, and installs the default
        workflow.  Broadcasts a ``teams_refresh`` SSE event so all open
        tabs update their sidebar immediately.

        All validation is performed upfront before any side effects.  If
        a later step fails, partial state is rolled back (best-effort).
        """
        from delegate.bootstrap import bootstrap, validate_project_name
        from delegate.repo import register_repo
        from delegate.activity import broadcast_teams_refresh

        name = req.name.strip()
        repo_path = str(Path(req.repo_path).expanduser())
        resolved_repo = Path(repo_path).resolve()

        # ── Phase 1: Validate everything BEFORE creating anything ──

        # 1a. Name format
        try:
            validate_project_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # 1b. Repo path exists
        if not resolved_repo.is_dir():
            raise HTTPException(status_code=400, detail=f"Repository path does not exist: {req.repo_path}")

        # 1c. Repo is a git repository
        if not (resolved_repo / ".git").exists():
            raise HTTPException(
                status_code=400,
                detail=f"Not a git repository (no .git directory): {req.repo_path}",
            )

        # 1d. No duplicate project name
        from delegate.db import get_connection
        conn = get_connection(hc_home)
        try:
            existing = conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone()
        finally:
            conn.close()
        if existing:
            raise HTTPException(status_code=409, detail=f"Project '{name}' already exists")

        # ── Phase 2: Create (with rollback on failure) ──

        # Generate agent names using friendly name pool
        exclude = {"delegate"}
        human = get_default_human(hc_home)
        if human:
            exclude.add(human)
        generated = pick_names(req.agent_count, exclude=exclude)
        agent_list = [(n, "engineer") for n in generated]

        # Bootstrap
        models_dict = {"*": req.model} if req.model in ("opus", "sonnet") else None
        try:
            bootstrap(hc_home, team_name=name, agents=agent_list, models=models_dict)
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # From here on, if anything fails we must clean up the bootstrapped team.
        try:
            # Register repo
            register_repo(hc_home, name, repo_path)

            # Register default workflow (non-critical)
            try:
                from delegate.workflow import register_workflow, get_latest_version
                builtin = Path(__file__).parent / "workflows" / "default.py"
                if builtin.is_file() and get_latest_version(hc_home, name, "default") is None:
                    register_workflow(hc_home, name, builtin)
            except Exception:
                logger.warning("Could not register default workflow for project '%s'", name, exc_info=True)

            # Send first-run welcome greeting (non-critical)
            try:
                from delegate.bootstrap import get_member_by_role
                from delegate.repo import list_repos as _list_repos_fn

                manager_name = get_member_by_role(hc_home, name, "manager")
                human_name = get_default_human(hc_home)
                if manager_name and human_name:
                    ai_agents = [
                        a for a in _list_team_agents(hc_home, name)
                        if a.get("role") != "manager"
                    ]
                    greeting = _build_first_run_greeting(
                        hc_home, name, manager_name, human_name,
                        agent_count=len(ai_agents),
                        has_repos=bool(_list_repos_fn(hc_home, name)),
                    )
                    _send(hc_home, name, manager_name, human_name, greeting)
                    logger.info(
                        "First-run welcome sent during project creation | team=%s",
                        name,
                    )
            except Exception:
                logger.warning(
                    "Could not send first-run greeting for project '%s'",
                    name, exc_info=True,
                )
        except Exception as exc:
            # Rollback: remove the partially created project
            logger.warning(
                "Project creation failed after bootstrap — rolling back '%s': %s",
                name, exc,
            )
            _rollback_project(hc_home, name)
            detail = str(exc)
            if isinstance(exc, (FileNotFoundError, ValueError)):
                raise HTTPException(status_code=400, detail=f"Failed to register repo: {detail}")
            raise HTTPException(status_code=500, detail=f"Project creation failed: {detail}")

        # Notify all SSE clients to refresh their team list
        broadcast_teams_refresh()

        return {"name": name, "status": "created"}

    @app.delete("/projects/{name}")
    def delete_project(name: str):
        """Delete a project (team) and all its data.

        Removes the team directory (agents, worktrees, DB, repos config) and
        cleans up the global projects table.  Broadcasts a ``teams_refresh``
        SSE event so all open tabs update their sidebar immediately.
        """
        import shutil
        from delegate.db import get_connection
        from delegate.activity import broadcast_teams_refresh

        td = _team_dir(hc_home, name)
        if not td.is_dir():
            raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

        # Resolve team UUID before we delete the directory
        team_uuid: str | None = None
        try:
            conn = get_connection(hc_home)
            row = conn.execute(
                "SELECT project_id FROM projects WHERE name = ?", (name,)
            ).fetchone()
            if row:
                team_uuid = row["project_id"]
            conn.close()
        except Exception:
            pass

        # Remove the team directory (agents, worktrees, db, repos config)
        shutil.rmtree(td)

        # Remove from global projects table
        try:
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
                from delegate.db_ids import soft_delete_team
                ids_conn = get_connection(hc_home)
                try:
                    soft_delete_team(ids_conn, team_uuid)
                    ids_conn.commit()
                finally:
                    ids_conn.close()
            except Exception:
                pass  # Best-effort

        # Notify all SSE clients to refresh their team list
        broadcast_teams_refresh()

        return {"ok": True}

    # --- Cleanup endpoints ---

    @app.get("/teams/{team}/cleanup/preview")
    def cleanup_preview(team: str, max_age: int = 14):
        """Preview what cleanup would do for a team (dry-run)."""
        from delegate.cleanup import preview_cleanup
        preview = preview_cleanup(hc_home, team_name=team, max_age_days=max_age)
        return preview.to_dict()

    @app.post("/teams/{team}/cleanup")
    def cleanup_team(team: str, max_age: int = 14):
        """Execute cleanup for a team: prune old data, caches, logs, worktrees."""
        from delegate.cleanup import run_cleanup
        result = run_cleanup(hc_home, team_name=team, max_age_days=max_age)
        return result.to_dict()

    @app.get("/cleanup/preview")
    def cleanup_preview_all(max_age: int = 14):
        """Preview what cleanup would do across all teams (dry-run)."""
        from delegate.cleanup import preview_cleanup
        preview = preview_cleanup(hc_home, max_age_days=max_age)
        return preview.to_dict()

    @app.post("/cleanup")
    def cleanup_all(max_age: int = 14):
        """Execute cleanup across all teams."""
        from delegate.cleanup import run_cleanup
        result = run_cleanup(hc_home, max_age_days=max_age)
        return result.to_dict()

    @app.get("/teams/{team}/default-cwd")
    def get_default_cwd(team: str):
        """Return the default working directory for shell commands in a team.

        Resolution order:
        1. First repo root (resolved symlink)
        2. User's home directory
        """
        from delegate.paths import repos_dir

        repos_path = repos_dir(hc_home, team)
        if repos_path.exists():
            repo_links = sorted(repos_path.iterdir())
            if repo_links:
                first_repo = repo_links[0]
                cwd = str(first_repo.resolve()) if first_repo.is_symlink() else str(first_repo)
                return {"cwd": cwd}
        return {"cwd": str(Path.home())}

    class ShellExecRequest(BaseModel):
        command: str
        cwd: str | None = None
        timeout: int = 30

    @app.post("/teams/{team}/exec/shell")
    def exec_shell(team: str, req: ShellExecRequest):
        """Execute a shell command for the human (magic commands feature).

        Resolves CWD in priority order:
        1. Explicit req.cwd if provided
        2. First repo root for the team
        3. User's home directory
        """
        import time
        from delegate.paths import repos_dir

        # Resolve CWD
        resolved_cwd: str
        if req.cwd:
            resolved_cwd = req.cwd
        else:
            # Try to get first repo root
            repos_path = repos_dir(hc_home, team)
            if repos_path.exists():
                repo_links = sorted(repos_path.iterdir())
                if repo_links:
                    # Follow the symlink to get the real repo path
                    first_repo = repo_links[0]
                    if first_repo.is_symlink():
                        resolved_cwd = str(first_repo.resolve())
                    else:
                        resolved_cwd = str(first_repo)
                else:
                    # No repos, use home directory
                    resolved_cwd = str(Path.home())
            else:
                # No repos dir, use home directory
                resolved_cwd = str(Path.home())

        # Expand ~ and ~user paths
        resolved_cwd = str(Path(resolved_cwd).expanduser())

        # Validate CWD exists
        cwd_path = Path(resolved_cwd)
        if not cwd_path.exists() or not cwd_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"Directory not found: {resolved_cwd}"
            )

        # Execute command
        start_time = time.time()
        try:
            result = subprocess.run(
                req.command,
                shell=True,
                cwd=resolved_cwd,
                capture_output=True,
                text=True,
                timeout=req.timeout,
            )
            duration_ms = int((time.time() - start_time) * 1000)

            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
                "cwd": resolved_cwd,
                "duration_ms": duration_ms,
            }
        except subprocess.TimeoutExpired as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return {
                "stdout": e.stdout.decode() if e.stdout else "",
                "stderr": e.stderr.decode() if e.stderr else "",
                "exit_code": -1,
                "cwd": resolved_cwd,
                "duration_ms": duration_ms,
                "error": f"Command timed out after {req.timeout}s",
            }
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Command execution failed: {str(e)}"
            )

    class CommandMessage(BaseModel):
        command: str
        result: dict

    @app.post("/teams/{team}/commands")
    def save_command(team: str, msg: CommandMessage):
        """Persist a command and its result as a message in the DB.

        Commands are stored with type='command' and both sender and recipient
        set to the human name. The result is stored as JSON.
        """
        from delegate.db import get_connection

        human_name = get_default_human(hc_home)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        t = _resolve_team(hc_home, team)

        conn = get_connection(hc_home, team)
        cursor = conn.execute(
            "INSERT INTO messages (sender, recipient, content, type, result, delivered_at, project, project_uuid) VALUES (?, ?, ?, 'command', ?, ?, ?, ?)",
            (human_name, human_name, msg.command, json.dumps(msg.result), now, team, t)
        )
        conn.commit()
        msg_id = cursor.lastrowid
        conn.close()

        return {"id": msg_id}

    # --- Legacy global endpoints (aggregate across all teams) ---
    # Prefixed with /api/ to avoid colliding with SPA routes (/tasks, /agents).

    # In-memory PyPI version cache: {"version": str, "fetched_at": float}
    _pypi_cache: dict = {}
    _PYPI_CACHE_TTL = 3600  # seconds

    @app.get("/api/version")
    def get_version():
        """Return the current installed version and latest available on PyPI.

        Response:
            {
              "current": "0.2.6",
              "latest": "0.3.0",    # null on fetch failure
              "update_available": true
            }

        The PyPI response is cached in-memory for 1 hour.
        On any fetch failure the endpoint still returns 200 with
        ``latest: null`` and ``update_available: false``.
        """
        import time
        import urllib.request
        import json as _json
        from importlib.metadata import version as _pkg_version
        from packaging.version import Version

        try:
            current = _pkg_version("delegate-ai")
        except Exception:
            try:
                # Package not installed via pip — read version directly from pyproject.toml
                import tomllib
                _pyproject = Path(__file__).parent.parent / "pyproject.toml"
                with open(_pyproject, "rb") as _f:
                    current = tomllib.load(_f)["project"]["version"]
            except Exception:
                current = None

        # Serve from cache if fresh
        now = time.monotonic()
        cached = _pypi_cache.get("data")
        if cached and (now - _pypi_cache.get("fetched_at", 0)) < _PYPI_CACHE_TTL:
            latest = cached
        else:
            latest = None
            try:
                req = urllib.request.Request(
                    "https://pypi.org/pypi/delegate-ai/json",
                    headers={"User-Agent": "delegate-ai/version-check"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        data = _json.loads(resp.read())
                        latest = data["info"]["version"]
                        _pypi_cache["data"] = latest
                        _pypi_cache["fetched_at"] = now
            except Exception:
                pass  # network error, timeout, bad JSON — leave latest as None

        update_available = False
        if latest is not None and current is not None:
            try:
                update_available = Version(latest) > Version(current)
            except Exception:
                pass

        return {"current": current, "latest": latest, "update_available": update_available}

    @app.get("/api/tasks")
    def get_tasks(status: str | None = None, assignee: str | None = None, team: str | None = None):
        """List tasks across all teams or specific team.

        Query params:
            status: Filter by status
            assignee: Filter by assignee
            team: Filter by team name, or "all" for all teams (default: all)
        """
        all_tasks = []
        # Determine which teams to query
        if team and team != "all":
            teams = [team]
        else:
            teams = _list_teams(hc_home)

        for t in teams:
            try:
                tasks = _list_tasks(hc_home, t, status=status, assignee=assignee)
                for task in tasks:
                    task["team"] = t
                all_tasks.extend(tasks)
            except Exception:
                pass

        # Sort by updated_at desc
        all_tasks.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return all_tasks

    # --- Pydantic models for request bodies ---

    class TaskCommentBody(BaseModel):
        author: str
        body: str

    class ReviewCommentBody(BaseModel):
        file: str
        line: int | None = None
        body: str

    class ReviewCommentUpdateBody(BaseModel):
        body: str

    class ApproveBody(BaseModel):
        summary: str = ""

    class RejectBody(BaseModel):
        reason: str
        summary: str = ""

    # --- Global task endpoints ---

    @app.get("/api/tasks/{task_id}")
    def get_task_global(task_id: int):
        """Get a single task by ID — scans all teams."""
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                task["team"] = t
                return task
            except FileNotFoundError:
                continue
            except Exception:
                logger.exception("Unexpected error in get_task_global for task %d team %s", task_id, t)
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/tasks/{task_id}/stats")
    def get_task_stats_global(task_id: int):
        """Get task stats — scans all teams for the task (legacy compat)."""
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                stats = _get_task_stats(hc_home, t, task_id)
                created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
                completed_at = task.get("completed_at")
                ended = datetime.fromisoformat(completed_at.replace("Z", "+00:00")) if completed_at else datetime.now(timezone.utc)
                elapsed_seconds = (ended - created).total_seconds()
                return {"task_id": task_id, "elapsed_seconds": elapsed_seconds, "branch": task.get("branch", ""), **stats}
            except FileNotFoundError:
                continue
            except Exception:
                logger.exception("Unexpected error in get_task_stats_global for task %d team %s", task_id, t)
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/tasks/{task_id}/diff")
    def get_task_diff_global(task_id: int):
        """Get task diff — scans all teams (legacy compat)."""
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                diff_dict = _get_task_diff(hc_home, t, task_id)
                return {"task_id": task_id, "branch": task.get("branch", ""), "repo": task.get("repo", []), "diff": diff_dict, "merge_base": task.get("merge_base", {}), "merge_tip": task.get("merge_tip", {})}
            except FileNotFoundError:
                continue
            except Exception:
                logger.exception("Unexpected error in get_task_diff_global for task %d team %s", task_id, t)
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/tasks/{task_id}/activity")
    def get_task_activity_global(task_id: int, limit: int | None = None):
        """Get task activity — scans all teams (legacy compat)."""
        from delegate.chat import get_task_timeline

        for t in _list_teams(hc_home):
            try:
                _get_task(hc_home, t, task_id)
                return get_task_timeline(hc_home, t, task_id, limit=limit)
            except FileNotFoundError:
                continue
            except Exception:
                logger.exception("Unexpected error in get_task_activity_global for task %d team %s", task_id, t)
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.post("/api/tasks/{task_id}/approve")
    def approve_task_global(task_id: int, body: ApproveBody | None = None):
        """Approve task — scans all teams (legacy compat)."""
        from delegate.review import set_verdict
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                if task["status"] != "in_approval":
                    raise HTTPException(status_code=400, detail=f"Cannot approve task in '{task['status']}' status. Task must be in 'in_approval' status.")
                attempt = task.get("review_attempt", 0)
                human_name = get_default_human(hc_home)
                summary = body.summary if body else ""
                if attempt > 0:
                    set_verdict(hc_home, t, task_id, attempt, "approved", summary=summary, reviewer=human_name)
                updated = _update_task(hc_home, t, task_id, approval_status="approved")
                _log_event(hc_home, t, f"{format_task_id(task_id)} approved \u2713", task_id=task_id)
                return updated
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.post("/api/tasks/{task_id}/reject")
    def reject_task_global(task_id: int, body: RejectBody):
        """Reject task — scans all teams (legacy compat)."""
        from delegate.review import set_verdict
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                try:
                    _change_status(hc_home, t, task_id, "rejected")
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                attempt = task.get("review_attempt", 0)
                human_name = get_default_human(hc_home)
                summary = body.summary or body.reason
                if attempt > 0:
                    set_verdict(hc_home, t, task_id, attempt, "rejected", summary=summary, reviewer=human_name)
                updated = _update_task(hc_home, t, task_id, rejection_reason=body.reason, approval_status="rejected")
                from delegate.notify import notify_rejection
                notify_rejection(hc_home, t, task, reason=body.reason)
                _log_event(hc_home, t, f"{format_task_id(task_id)} rejected \u2014 {body.reason}", task_id=task_id)
                return updated
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/tasks/{task_id}/comments")
    def get_task_comments_global(task_id: int, limit: int = 50):
        """Get task comments — scans all teams (legacy compat)."""
        from delegate.task import get_comments as _get_comments
        for t in _list_teams(hc_home):
            try:
                _get_task(hc_home, t, task_id)
                return _get_comments(hc_home, t, task_id, limit=limit)
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.post("/api/tasks/{task_id}/comments")
    def post_task_comment_global(task_id: int, comment: TaskCommentBody):
        """Add a comment to a task — scans all teams (legacy compat)."""
        from delegate.task import add_comment as _add_comment
        for t in _list_teams(hc_home):
            try:
                _get_task(hc_home, t, task_id)
                cid = _add_comment(hc_home, t, task_id, comment.author, comment.body)
                # Notify manager if comment is from a human member
                try:
                    from delegate.config import get_human_members
                    human_names = {m["name"] for m in get_human_members(hc_home)}
                    if comment.author in human_names:
                        from delegate.notify import notify_human_comment
                        notify_human_comment(hc_home, t, task_id, comment.author, comment.body)
                except Exception:
                    logger.debug("Failed to send human comment notification", exc_info=True)
                return {"id": cid, "task_id": task_id, "author": comment.author, "body": comment.body}
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/tasks/{task_id}/merge-preview")
    def get_task_merge_preview_global(task_id: int):
        """Get merge preview — scans all teams (legacy compat)."""
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                preview = _get_merge_preview(hc_home, t, task_id)
                return {
                    "task_id": task_id,
                    "branch": task.get("branch", ""),
                    "diff": preview,
                }
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/tasks/{task_id}/commits")
    def get_task_commits_global(task_id: int):
        """Get task commits — scans all teams (legacy compat)."""
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                diffs = _get_commit_diffs(hc_home, t, task_id)
                return {"task_id": task_id, "branch": task.get("branch", ""), "commit_diffs": diffs}
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.post("/api/tasks/{task_id}/retry-merge")
    def retry_merge_global(task_id: int):
        """Retry a failed merge — scans all teams (legacy compat)."""
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                if task["status"] != "merge_failed":
                    raise HTTPException(
                        status_code=400,
                        detail=f"Task is in '{task['status']}', not 'merge_failed'",
                    )
                from delegate.task import transition_task
                _update_task(hc_home, t, task_id, merge_attempts=0, status_detail="")
                from delegate.bootstrap import get_member_by_role
                manager = get_member_by_role(hc_home, t, "manager") or "delegate"
                updated = transition_task(hc_home, t, task_id, "merging", manager)
                return updated
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task_global(task_id: int):
        """Cancel a task — scans all teams (legacy compat)."""
        for t in _list_teams(hc_home):
            try:
                _get_task(hc_home, t, task_id)
                from delegate.task import cancel_task
                updated = cancel_task(hc_home, t, task_id)
                return updated
            except (FileNotFoundError, ValueError):
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/tasks/{task_id}/reviews")
    def get_task_reviews_global(task_id: int):
        """Get all review attempts for a task — scans all teams (legacy compat)."""
        from delegate.review import get_reviews, get_comments
        for t in _list_teams(hc_home):
            try:
                _get_task(hc_home, t, task_id)
                reviews = get_reviews(hc_home, t, task_id)
                for r in reviews:
                    r["comments"] = get_comments(hc_home, t, task_id, r["attempt"])
                return reviews
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/tasks/{task_id}/reviews/current")
    def get_task_current_review_global(task_id: int):
        """Get current review attempt with comments — scans all teams (legacy compat)."""
        from delegate.review import get_current_review
        for t in _list_teams(hc_home):
            try:
                _get_task(hc_home, t, task_id)
                review = get_current_review(hc_home, t, task_id)
                if review is None:
                    return {"attempt": 0, "verdict": None, "summary": "", "comments": []}
                return review
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.post("/api/tasks/{task_id}/reviews/comments")
    def post_review_comment_global(task_id: int, comment: ReviewCommentBody):
        """Add an inline comment to the current review attempt — scans all teams (legacy compat)."""
        from delegate.review import add_comment
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
                attempt = task.get("review_attempt", 0)
                if attempt == 0:
                    raise HTTPException(status_code=400, detail="Task has no active review attempt.")
                human_name = get_default_human(hc_home)
                result = add_comment(
                    hc_home, t, task_id, attempt,
                    file=comment.file, body=comment.body, author=human_name,
                    line=comment.line,
                )
                return result
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.put("/api/tasks/{task_id}/reviews/comments/{comment_id}")
    def edit_review_comment_global(task_id: int, comment_id: int, payload: ReviewCommentUpdateBody):
        """Edit an existing review comment's body — scans all teams (legacy compat)."""
        from delegate.review import update_comment
        for t in _list_teams(hc_home):
            try:
                _get_task(hc_home, t, task_id)
                result = update_comment(hc_home, t, comment_id, payload.body)
                if result is None:
                    raise HTTPException(status_code=404, detail="Comment not found")
                return result
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.delete("/api/tasks/{task_id}/reviews/comments/{comment_id}")
    def remove_review_comment_global(task_id: int, comment_id: int):
        """Delete a review comment — scans all teams (legacy compat)."""
        from delegate.review import delete_comment
        for t in _list_teams(hc_home):
            try:
                _get_task(hc_home, t, task_id)
                deleted = delete_comment(hc_home, t, comment_id)
                if not deleted:
                    raise HTTPException(status_code=404, detail="Comment not found")
                return {"ok": True}
            except FileNotFoundError:
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # ---------------------------------------------------------------------------
    # Reviewer edit endpoints
    # ---------------------------------------------------------------------------

    def _get_branch_head_sha(repo_dir: str, branch: str) -> str:
        """Return the current HEAD sha for a branch in repo_dir."""
        result = subprocess.run(
            ["git", "rev-parse", branch],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Could not resolve branch {branch!r}: {result.stderr.strip()}")
        return result.stdout.strip()

    def _read_file_from_branch(repo_dir: str, branch: str, file_path: str) -> str | None:
        """Return file content at file_path on branch, or None if file doesn't exist."""
        result = subprocess.run(
            ["git", "show", f"{branch}:{file_path}"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    @app.get("/api/tasks/{task_id}/file")
    def get_task_file_global(task_id: int, path: str):
        """Return file content + HEAD sha from the task branch.

        Query params:
            path: File path relative to repo root (e.g. src/foo.py)

        Returns:
            { "content": "<full text>", "head_sha": "<branch HEAD sha>" }

        Errors:
            404 if task or file not found
        """
        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
            except FileNotFoundError:
                continue

            branch = task.get("branch", "")
            if not branch:
                raise HTTPException(status_code=404, detail=f"Task {task_id} has no branch")

            repos = task.get("repo", [])
            if not repos:
                raise HTTPException(status_code=404, detail=f"Task {task_id} has no associated repo")

            from delegate.repo import get_repo_path
            repo_name = repos[0]
            try:
                repo_dir = str(get_repo_path(hc_home, t, repo_name).resolve())
            except Exception:
                raise HTTPException(status_code=404, detail=f"Repo {repo_name!r} not found")

            try:
                head_sha = _get_branch_head_sha(repo_dir, branch)
            except RuntimeError as e:
                raise HTTPException(status_code=404, detail=str(e))

            content = _read_file_from_branch(repo_dir, branch, path)
            if content is None:
                raise HTTPException(status_code=404, detail=f"File {path!r} not found on branch {branch!r}")

            return {"content": content, "head_sha": head_sha}

        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    class ReviewerEdit(BaseModel):
        file: str
        content: str
        expected_sha: str

    class ReviewerEditsBody(BaseModel):
        edits: list[ReviewerEdit]

    @app.post("/api/tasks/{task_id}/reviewer-edits")
    def post_reviewer_edits_global(task_id: int, body: ReviewerEditsBody):
        """Commit reviewer edits to the task branch.

        Request body:
            { "edits": [ { "file": "...", "content": "...", "expected_sha": "..." } ] }

        Behavior:
            1. Check task is in in_review or in_approval (403 otherwise).
            2. Verify all expected_sha values match current HEAD (409 if stale).
            3. Create a temp worktree from the branch.
            4. Write each file, skipping byte-identical content.
            5. If any file changed: git add -A + git commit.
            6. Clean up temp worktree (try/finally).
            7. Return { "new_sha": "...", ["no_changes": true] }.

        Errors:
            403 if task not in in_review or in_approval
            404 if task/repo not found
            409 if expected_sha is stale
        """
        import uuid as _uuid

        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
            except FileNotFoundError:
                continue

            # Status gate
            status = task.get("status", "")
            if status not in ("in_review", "in_approval"):
                raise HTTPException(
                    status_code=403,
                    detail=f"Reviewer edits are only allowed for tasks in 'in_review' or 'in_approval' status (current: {status!r})",
                )

            branch = task.get("branch", "")
            if not branch:
                raise HTTPException(status_code=404, detail=f"Task {task_id} has no branch")

            repos = task.get("repo", [])
            if not repos:
                raise HTTPException(status_code=404, detail=f"Task {task_id} has no associated repo")

            from delegate.repo import get_repo_path
            repo_name = repos[0]
            try:
                repo_dir = str(get_repo_path(hc_home, t, repo_name).resolve())
            except Exception:
                raise HTTPException(status_code=404, detail=f"Repo {repo_name!r} not found")

            # Stale detection
            try:
                current_head = _get_branch_head_sha(repo_dir, branch)
            except RuntimeError as e:
                raise HTTPException(status_code=404, detail=str(e))

            for edit in body.edits:
                if edit.expected_sha != current_head:
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "stale", "current_sha": current_head},
                    )

            # Determine author name
            human_name = get_default_human(hc_home) or "reviewer"

            # Create temp worktree
            uid = _uuid.uuid4().hex[:12]
            parts = branch.rsplit("/", 1)
            if len(parts) == 2:
                temp_branch = f"{parts[0]}/_review/{uid}/{parts[1]}"
            else:
                temp_branch = f"_review/{uid}/{branch}"

            team_uuid_dir = _team_dir(hc_home, t)
            wt_path = team_uuid_dir / "worktrees" / "_review" / uid / format_task_id(task_id)
            wt_path.parent.mkdir(parents=True, exist_ok=True)

            result = subprocess.run(
                ["git", "worktree", "add", "-b", temp_branch, str(wt_path), branch],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create temp worktree: {result.stderr.strip()}",
                )

            try:
                any_changed = False
                for edit in body.edits:
                    dest = wt_path / edit.file
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    # Check if content is byte-identical to avoid no-op writes
                    existing = _read_file_from_branch(repo_dir, branch, edit.file)
                    if existing == edit.content:
                        continue  # Skip identical content

                    dest.write_text(edit.content, encoding="utf-8")
                    any_changed = True

                if not any_changed:
                    return {"new_sha": current_head, "no_changes": True}

                # Stage and commit
                add_result = subprocess.run(
                    ["git", "add", "-A"],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if add_result.returncode != 0:
                    raise HTTPException(
                        status_code=500,
                        detail=f"git add failed: {add_result.stderr.strip()}",
                    )

                commit_result = subprocess.run(
                    [
                        "git", "commit",
                        f"--author={human_name} <{human_name}@localhost>",
                        "-m", f"reviewer edits — T{task_id}",
                    ],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if commit_result.returncode != 0:
                    raise HTTPException(
                        status_code=500,
                        detail=f"git commit failed: {commit_result.stderr.strip()}",
                    )

                # Get new HEAD sha from the temp worktree
                sha_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if sha_result.returncode != 0:
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to read new HEAD sha after commit",
                    )
                new_sha = sha_result.stdout.strip()

                # Fast-forward the original branch to the new commit
                ff_result = subprocess.run(
                    ["git", "update-ref", f"refs/heads/{branch}", new_sha],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if ff_result.returncode != 0:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to advance branch: {ff_result.stderr.strip()}",
                    )

                return {"new_sha": new_sha}

            finally:
                # Best-effort cleanup of temp worktree and branch
                subprocess.run(
                    ["git", "worktree", "remove", str(wt_path), "--force"],
                    cwd=repo_dir,
                    capture_output=True,
                    timeout=30,
                )
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=repo_dir,
                    capture_output=True,
                    timeout=30,
                )
                subprocess.run(
                    ["git", "branch", "-D", temp_branch],
                    cwd=repo_dir,
                    capture_output=True,
                    timeout=30,
                )
                # Clean up empty parent dirs
                try:
                    parent = wt_path.parent
                    while parent.name != "_review" and parent != parent.parent:
                        if parent.exists() and not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent
                        else:
                            break
                    if parent.name == "_review" and parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    pass

        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # ---------------------------------------------------------------------------
    # File completion endpoints
    # ---------------------------------------------------------------------------

    def _list_completions(prefix: str, limit: int) -> list[dict]:
        """Return filesystem entries whose absolute paths begin with ``prefix``.

        If ``prefix`` ends with ``/``, all entries in that directory are listed
        (no name filter applied).  Otherwise, the parent directory of ``prefix``
        is listed and only entries whose names start with the final component of
        ``prefix`` are included.

        Sort order: directories first (alphabetical within group), then files
        (alphabetical within group).

        Security: symlinks are never followed outside the effective parent
        directory, and ``..`` components in the prefix are rejected upstream.
        """
        from pathlib import Path as _Path

        if prefix.endswith("/"):
            parent = _Path(prefix[:-1])  # directory to list
            partial_name = ""            # no filter
        else:
            prefix_path = _Path(prefix)
            parent = prefix_path.parent
            partial_name = prefix_path.name

        if not parent.exists():
            return []

        entries = []
        try:
            for entry in parent.iterdir():
                if partial_name and not entry.name.startswith(partial_name):
                    continue
                # Security: skip symlinks to prevent traversal outside prefix dir
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir()
                entries.append({
                    "path": str(entry),
                    "is_dir": is_dir,
                    "has_git": is_dir and (entry / ".git").exists(),
                })
        except PermissionError:
            return []

        # Sort: directories first, then alphabetical within each group
        entries.sort(key=lambda e: (0 if e["is_dir"] else 1, e["path"]))
        return entries[:limit]

    _COMPLETION_MAX_LIMIT = 50

    @app.get("/api/files/complete")
    def get_files_complete(path: str = "", limit: int = 20):
        """List filesystem entries whose absolute paths begin with ``path``.

        Query params:
            path:  Optional. Absolute path (starts with ``/``) or a ``~``-prefixed
                   path which is expanded to the user's home directory.
                   Defaults to the user's home directory when empty.
            limit: Optional. Default 20, max 50.

        Returns:
            { "entries": [{ "path": "/abs/path", "is_dir": true }, ...] }

        Errors:
            400 if path is non-empty, non-absolute (after ~ expansion), or contains ``..`` components.
        """
        if not path:
            path = str(Path.home()) + "/"
        elif path.startswith("~"):
            path = str(Path(path).expanduser())
        if not path.startswith("/"):
            raise HTTPException(status_code=400, detail="path must be an absolute path starting with /")
        if ".." in path.split("/"):
            raise HTTPException(status_code=400, detail="path traversal via .. is not allowed")
        limit = min(max(1, limit), _COMPLETION_MAX_LIMIT)
        entries = _list_completions(path, limit)
        return {"entries": entries}

    @app.get("/api/tasks/{task_id}/files/complete")
    def get_task_files_complete(task_id: int, q: str = "", limit: int = 20):
        """List worktree entries matching the partial relative path ``q``.

        Query params:
            q:     Partial path relative to worktree root (may be empty to list
                   top-level entries). Must not contain ``..`` components.
            limit: Optional. Default 20, max 50.

        Returns:
            { "entries": [{ "path": "src/main.py", "is_dir": false }, ...] }
            Paths are relative to the worktree root.

        Errors:
            400 if ``q`` contains ``..`` components.
            404 if task has no worktree (not in_progress / in_review / in_approval).
        """
        from delegate.repo import get_task_worktree_path

        if ".." in q.split("/"):
            raise HTTPException(status_code=400, detail="path traversal via .. is not allowed")
        limit = min(max(1, limit), _COMPLETION_MAX_LIMIT)

        _active_statuses = {"in_progress", "in_review", "in_approval"}

        for t in _list_teams(hc_home):
            try:
                task = _get_task(hc_home, t, task_id)
            except FileNotFoundError:
                continue

            status = task.get("status", "")
            if status not in _active_statuses:
                raise HTTPException(
                    status_code=404,
                    detail=f"Task {task_id} has no worktree (status: {status!r})",
                )

            repos = task.get("repo", [])
            if not repos:
                raise HTTPException(
                    status_code=404,
                    detail=f"Task {task_id} has no associated repo",
                )

            repo_name = repos[0]
            wt_root = get_task_worktree_path(hc_home, t, repo_name, task_id)
            if not wt_root.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"Worktree for task {task_id} does not exist on disk",
                )

            # Build the absolute prefix and list
            abs_prefix = str(wt_root / q) if q else str(wt_root) + "/"
            abs_entries = _list_completions(abs_prefix, limit)

            # Convert absolute paths to paths relative to worktree root
            wt_root_str = str(wt_root)
            rel_entries = []
            for e in abs_entries:
                abs_p = e["path"]
                if abs_p.startswith(wt_root_str + "/"):
                    rel_p = abs_p[len(wt_root_str) + 1:]
                else:
                    rel_p = abs_p
                rel_entries.append({"path": rel_p, "is_dir": e["is_dir"], "has_git": e["has_git"]})

            return {"entries": rel_entries}

        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/messages")
    def get_messages(since: str | None = None, between: str | None = None, type: str | None = None, limit: int | None = None, before_id: int | None = None, team: str | None = None):
        """Messages across all teams or specific team.

        Query params:
            since: ISO timestamp to filter messages after
            between: Comma-separated sender,recipient pair
            type: Message type filter
            limit: Maximum number of messages
            before_id: Return messages before this ID
            team: Filter by team name, or "all" for all teams (default: all)
        """
        between_tuple = None
        if between:
            parts = [p.strip() for p in between.split(",")]
            if len(parts) == 2:
                between_tuple = (parts[0], parts[1])

        # Determine which teams to query
        if team and team != "all":
            teams = [team]
        else:
            teams = _list_teams(hc_home)

        all_msgs = []
        for t in teams:
            try:
                msgs = _get_messages(hc_home, t, since=since, between=between_tuple, msg_type=type, limit=limit, before_id=before_id)
                for m in msgs:
                    m["team"] = t
                all_msgs.extend(msgs)
            except Exception:
                pass
        all_msgs.sort(key=lambda m: m.get("id", 0))
        if limit:
            all_msgs = all_msgs[:limit]
        return all_msgs

    @app.post("/api/messages")
    def post_message(msg: SendMessage):
        """Human sends a message (legacy — uses msg.team field)."""
        team = msg.team or _first_team(hc_home)
        human_name = get_default_human(hc_home)
        team_agents = _list_team_agents(hc_home, team)
        agent_names = {a["name"] for a in team_agents}
        if msg.recipient not in agent_names:
            raise HTTPException(
                status_code=403,
                detail=f"Recipient '{msg.recipient}' is not an agent in team '{team}'",
            )
        _send(hc_home, team, human_name, msg.recipient, msg.content)
        _wake_daemon()
        return {"status": "queued"}

    # --- Agent endpoints (team-scoped) ---

    @app.get("/api/agents")
    def get_all_agents(team: str | None = None):
        """List all agents across all teams or specific team.

        Query params:
            team: Filter by team name, or "all" for all teams (default: all)
        """
        # Determine which teams to query
        if team and team != "all":
            teams = [team]
        else:
            teams = _list_teams(hc_home)

        all_agents = []
        for t in teams:
            all_agents.extend(_list_team_agents(hc_home, t))
        return all_agents

    @app.get("/teams/{team}/agents")
    def get_agents(team: str):
        """List AI agents for a team (excludes human members)."""
        return _list_team_agents(hc_home, team)

    @app.get("/teams/{team}/agents/stats")
    def get_all_agent_stats(team: str):
        """Get aggregated stats for all agents in a team (single DB query)."""
        agents_data = _list_team_agents(hc_home, team)
        agent_names = [a["name"] for a in agents_data]
        return _get_team_agent_stats(hc_home, team, agent_names)

    @app.get("/teams/{team}/agents/{name}/stats")
    def get_agent_stats(team: str, name: str):
        """Get aggregated stats for a specific agent."""
        return _get_agent_stats(hc_home, team, name)

    @app.get("/teams/{team}/agents/{name}/inbox")
    def get_agent_inbox(team: str, name: str):
        """Return all messages in the agent's inbox with lifecycle status."""
        from delegate.config import SYSTEM_USER
        all_msgs = _read_inbox(hc_home, team, name, unread_only=False)
        result = [
            {
                "id": m.id,
                "sender": m.sender,
                "time": m.time,
                "body": m.body,
                "task_id": m.task_id,
                "delivered_at": m.delivered_at,
                "seen_at": m.seen_at,
                "processed_at": m.processed_at,
            }
            for m in all_msgs
            if m.sender != SYSTEM_USER
        ]
        result.sort(key=lambda x: x["time"], reverse=True)
        return result[:100]

    @app.get("/teams/{team}/agents/{name}/outbox")
    def get_agent_outbox(team: str, name: str):
        """Return all messages sent by the agent with delivery status."""
        all_msgs = _read_outbox(hc_home, team, name, pending_only=False)
        result = [
            {
                "id": m.id,
                "recipient": m.recipient,
                "time": m.time,
                "body": m.body,
                "task_id": m.task_id,
                "delivered_at": m.delivered_at,
                "seen_at": m.seen_at,
                "processed_at": m.processed_at,
            }
            for m in all_msgs
        ]
        result.sort(key=lambda x: x["time"], reverse=True)
        return result[:100]

    @app.get("/teams/{team}/agents/{name}/messages")
    def get_agent_messages(team: str, name: str):
        """Return unified inbox + outbox messages in chronological order."""
        inbox_msgs = _read_inbox(hc_home, team, name, unread_only=False)
        outbox_msgs = _read_outbox(hc_home, team, name, pending_only=False)

        # Convert inbox messages to unified format
        inbox_result = [
            {
                "id": m.id,
                "direction": "in",
                "counterparty": m.sender,
                "time": m.time,
                "body": m.body,
                "task_id": m.task_id,
                "delivered_at": m.delivered_at,
                "seen_at": m.seen_at,
                "processed_at": m.processed_at,
            }
            for m in inbox_msgs
        ]

        # Convert outbox messages to unified format
        outbox_result = [
            {
                "id": m.id,
                "direction": "out",
                "counterparty": m.recipient,
                "time": m.time,
                "body": m.body,
                "task_id": m.task_id,
                "delivered_at": m.delivered_at,
                "seen_at": m.seen_at,
                "processed_at": m.processed_at,
            }
            for m in outbox_msgs
        ]

        # Merge and sort by time (newest first)
        all_msgs = inbox_result + outbox_result
        all_msgs.sort(key=lambda x: x["time"], reverse=True)
        return all_msgs[:100]

    @app.get("/teams/{team}/agents/{name}/logs")
    def get_agent_logs(team: str, name: str):
        """Return the agent's worklog entries."""
        ad = _agent_dir(hc_home, team, name)
        if not ad.is_dir():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found in team '{team}'")

        logs_dir = ad / "logs"
        sessions = []
        if logs_dir.is_dir():
            worklog_files = [f for f in logs_dir.iterdir() if f.name.endswith(".worklog.md")]
            worklog_files.sort(key=lambda f: int(f.name.split(".")[0]) if f.name.split(".")[0].isdigit() else 0)

            for f in worklog_files:
                content = f.read_text()
                if len(content) > 50 * 1024:
                    content = content[-(50 * 1024):]
                sessions.append({
                    "filename": f.name,
                    "content": content,
                })

        sessions.reverse()
        return {"sessions": sessions}

    @app.get("/teams/{team}/agents/{name}/reflections")
    def get_agent_reflections(team: str, name: str):
        """Return the agent's reflections markdown."""
        ad = _agent_dir(hc_home, team, name)
        if not ad.is_dir():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        path = ad / "notes" / "reflections.md"
        content = path.read_text() if path.exists() else ""
        return {"content": content}

    @app.get("/teams/{team}/agents/{name}/journal")
    def get_agent_journal(team: str, name: str):
        """Return the agent's task journals (one file per task)."""
        ad = _agent_dir(hc_home, team, name)
        if not ad.is_dir():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        journals_dir = ad / "journals"
        entries: list[dict] = []
        if journals_dir.is_dir():
            for f in sorted(journals_dir.iterdir(), reverse=True):
                if f.suffix == ".md":
                    content = f.read_text()
                    if len(content) > 50 * 1024:
                        content = content[-(50 * 1024):]
                    entries.append({"filename": f.name, "content": content})
        return {"entries": entries}

    # --- Agent activity (ring buffer history + SSE stream) ---

    @app.get("/teams/{team}/agents/{name}/activity")
    def get_agent_activity(team: str, name: str, n: int = 100):
        """Return the most recent activity entries for an agent."""
        from delegate.activity import get_recent
        return get_recent(team, name, n=n)

    @app.post("/api/agents/restart-all")
    async def restart_all_agents(team: str | None = None):
        """Restart all agent Telephone sessions across all (or one) team.

        Cancels every in-flight agent turn, closes and removes every
        cached Telephone (killing the Claude subprocess), and clears the
        in-flight tracking set.  The daemon loop will re-dispatch agents
        with unread messages on the next cycle, creating fresh Telephone
        sessions.

        Use this after a usage-limit reset to unstick agents whose
        cached Telephones are wedged on API errors.
        """
        from delegate.activity import broadcast_turn_event

        teams = [team] if team else _list_teams(hc_home)
        cancelled = 0
        closed = 0

        # 1. Cancel all active agent asyncio tasks
        for key, task in list(_active_agent_tasks.items()):
            agent_team, agent_name = key
            if team and agent_team != team:
                continue
            if not task.done():
                task.cancel()
                cancelled += 1
                broadcast_turn_event(
                    "turn_interrupted", agent_name, team=agent_team,
                )

        # 2. Close and remove all cached Telephones (kills subprocesses)
        if _exchange is not None:
            for (t, a), tel in list(_exchange._telephones.items()):
                if team and t != team:
                    continue
                try:
                    await tel.close()
                except Exception:
                    logger.debug("Error closing telephone %s/%s", t, a, exc_info=True)
                _exchange._telephones.pop((t, a), None)
                closed += 1

        # 3. Wake the daemon so it re-dispatches immediately
        if _daemon_wakeup is not None:
            _daemon_wakeup.set()

        logger.info(
            "restart-all: cancelled %d turns, closed %d telephones (team=%s)",
            cancelled, closed, team or "all",
        )
        return {
            "status": "restarted",
            "turns_cancelled": cancelled,
            "telephones_closed": closed,
            "teams": teams,
        }

    @app.post("/api/agents/{agent}/interrupt")
    async def interrupt_agent(agent: str, team: str | None = None):
        """Interrupt an agent's current turn and reset its conversation.

        If the agent is currently running a turn, cancels the asyncio task
        and sends an interrupt signal to the Telephone subprocess. The agent
        will be available for new turns on the next daemon cycle.
        """
        from delegate.activity import broadcast_turn_event

        # Resolve team: use provided team, or scan all teams
        teams = [team] if team else _list_teams(hc_home)
        resolved_team = None
        for t in teams:
            if (t, agent) in _active_agent_tasks:
                resolved_team = t
                break

        # If not found in active tasks, check if agent exists in any team
        if resolved_team is None:
            for t in teams:
                ad = _agent_dir(hc_home, t, agent)
                if ad.is_dir():
                    resolved_team = t
                    break

        if resolved_team is None:
            raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")

        key = (resolved_team, agent)

        # Cancel the asyncio task if running
        task = _active_agent_tasks.get(key)
        was_running = task is not None and not task.done()
        if was_running:
            task.cancel()

        # Send interrupt signal to the Telephone
        if _exchange is not None:
            tel = _exchange.get(resolved_team, agent)
            if tel is not None:
                await tel.interrupt()

        if was_running:
            return {"status": "interrupted", "agent": agent, "team": resolved_team}
        return {"status": "not_running", "agent": agent, "team": resolved_team}

    @app.post("/api/agents/{agent}/nudge")
    async def nudge_agent(agent: str, team: str | None = None):
        """Send a system nudge message to an agent to check its assigned tasks.

        If the agent has active assigned tasks, the nudge lists them.
        The daemon loop will dispatch a turn on the next cycle.
        """
        from delegate.config import SYSTEM_USER
        from delegate.mailbox import send as send_message
        from delegate.task import list_tasks

        # Resolve team
        teams = [team] if team else _list_teams(hc_home)
        resolved_team = None
        for t in teams:
            ad = _agent_dir(hc_home, t, agent)
            if ad.is_dir():
                resolved_team = t
                break

        if resolved_team is None:
            raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")

        active_tasks = [
            t for t in list_tasks(hc_home, resolved_team, assignee=agent)
            if t.get("status") not in ("done", "cancelled")
        ]

        if active_tasks:
            task_lines = ", ".join(
                f"T{t['id']:04d} ({t.get('title', 'untitled')}, status: {t.get('status', '?')})"
                for t in active_tasks
            )
            body = f"Nudge: you have assigned tasks that need attention: {task_lines}. Please review and continue working."
        else:
            body = "Nudge: please check if there are any tasks or messages that need your attention."

        send_message(hc_home, resolved_team, SYSTEM_USER, agent, body)
        return {
            "status": "nudged",
            "agent": agent,
            "team": resolved_team,
            "active_tasks": len(active_tasks),
        }

    @app.get("/teams/{team}/activity/stream")
    async def activity_stream(team: str):
        """SSE endpoint streaming real-time agent activity events.

        The client opens an ``EventSource`` to this URL and receives
        ``data: {...}`` events for every tool invocation across all
        agents on this team.  Events from other teams are filtered out.
        """
        from delegate.activity import subscribe, unsubscribe

        queue = subscribe(team=team)

        async def _generate():
            try:
                # Send a ping immediately so the client knows the stream is alive
                yield f"data: {json.dumps({'type': 'connected'})}\n\n"
                while True:
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"data: {json.dumps(entry)}\n\n"
                    except asyncio.TimeoutError:
                        # Send keepalive comment to prevent proxy/browser timeout
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                unsubscribe(queue)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # --- Global SSE stream (all teams) ---

    @app.get("/stream")
    async def global_activity_stream():
        """SSE endpoint streaming real-time agent activity events across all teams.

        The client opens an ``EventSource`` to this URL and receives
        ``data: {...}`` events for every tool invocation across all teams.
        Each event includes a ``team`` field for client-side filtering.
        """
        from delegate.activity import subscribe, unsubscribe

        queue = subscribe(team=None)  # No team filter — receive all events

        async def _generate():
            try:
                # Send a ping immediately so the client knows the stream is alive
                yield f"data: {json.dumps({'type': 'connected'})}\n\n"
                while True:
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"data: {json.dumps(entry)}\n\n"
                    except asyncio.TimeoutError:
                        # Send keepalive comment to prevent proxy/browser timeout
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                unsubscribe(queue)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # --- Active turns snapshot (for SSE reconnect recovery) ---

    @app.get("/turns/active")
    def active_turns():
        """Return a snapshot of all currently active agent turns.

        The frontend calls this on SSE connect/reconnect to recover turn
        state that may have been missed while the connection was down.
        Each entry mirrors a ``turn_started`` SSE event payload.
        """
        from delegate.activity import get_active_turns
        return get_active_turns()

    # --- Shared files endpoints ---

    MAX_FILE_SIZE = 1_000_000  # 1 MB truncation limit

    @app.get("/teams/{team}/files")
    def list_shared_files(team: str, path: str | None = None):
        """List files in the team's shared/ directory or a subdirectory."""
        base = _shared_dir(hc_home, team)
        if not base.is_dir():
            return {"files": []}

        if path:
            target = (base / path).resolve()
            try:
                target.relative_to(base.resolve())
            except ValueError:
                raise HTTPException(
                    status_code=403, detail="Path traversal not allowed"
                )
        else:
            target = base

        if not target.is_dir():
            raise HTTPException(
                status_code=404, detail=f"Directory not found: {path}"
            )

        entries = []
        for item in target.iterdir():
            stat = item.stat()
            entries.append(
                {
                    "name": item.name,
                    "path": str(item.relative_to(base)),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "is_dir": item.is_dir(),
                }
            )

        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"files": entries}

    def _resolve_file_path(team: str, path: str) -> Path:
        """Resolve a file path from an API ``path`` parameter.

        Absolute and ``~``-prefixed paths are allowed only if they fall
        within the delegate home directory or registered repo directories.
        Other paths are resolved relative to ``hc_home``.

        Returns the resolved ``Path``, or raises 403/404.
        """
        if path.startswith("/"):
            target = Path(path).resolve()
        elif path.startswith("~"):
            target = Path(path).expanduser().resolve()
        else:
            target = (hc_home / path).resolve()

        # Build allowed roots: hc_home + all registered repo paths
        hc_resolved = hc_home.resolve()
        allowed_roots = [hc_resolved]
        try:
            from delegate.repo import list_repos, get_repo_path
            for repo_name in list_repos(hc_home, team):
                rp = get_repo_path(hc_home, team, repo_name)
                if rp.exists():
                    allowed_roots.append(rp.resolve())
        except Exception:
            pass

        # Also allow worktree directories (they may be outside hc_home)
        try:
            from delegate.paths import task_worktree_dir
            wt_base = task_worktree_dir(hc_home, team).resolve()
            allowed_roots.append(wt_base)
        except Exception:
            pass

        target_str = str(target)
        if not any(target_str.startswith(str(root)) for root in allowed_roots):
            raise HTTPException(
                status_code=403,
                detail="Access denied: path is outside the project directory",
            )

        if not target.exists():
            raise HTTPException(
                status_code=404, detail=f"Path not found: {path}"
            )
        return target

    @app.get("/teams/{team}/files/content")
    def read_file_content(team: str, path: str):
        """Read any file and return its content as JSON.

        Supports absolute paths and delegate-relative paths (resolved
        from ``hc_home``, e.g. ``teams/self/shared/spec.md``).

        For text files, returns content as string.
        For images and binary files, returns base64-encoded data with content_type.
        """
        target = _resolve_file_path(team, path)

        if target.is_dir():
            entries = []
            for item in sorted(target.iterdir(), key=lambda i: (not i.is_dir(), i.name.lower())):
                try:
                    stat = item.stat()
                    entries.append({
                        "name": item.name,
                        "path": str(item),
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "is_dir": item.is_dir(),
                    })
                except OSError:
                    continue
            return {
                "path": str(target),
                "name": target.name,
                "is_directory": True,
                "files": entries,
            }

        stat = target.stat()
        ext = target.suffix.lower()

        # Image extensions
        image_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".webp": "image/webp",
        }

        # Common binary extensions (non-image)
        binary_exts = {".pdf", ".zip", ".tar", ".gz", ".exe", ".bin", ".ico"}

        display_path = str(target)

        if ext in image_types:
            # Read as binary and encode as base64
            data = target.read_bytes()
            if len(data) > MAX_FILE_SIZE:
                data = data[:MAX_FILE_SIZE]
            return {
                "path": display_path,
                "name": target.name,
                "size": stat.st_size,
                "content": base64.b64encode(data).decode("utf-8"),
                "content_type": image_types[ext],
                "is_binary": True,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        elif ext in binary_exts:
            # Binary file - return metadata only
            return {
                "path": display_path,
                "name": target.name,
                "size": stat.st_size,
                "content": "",
                "content_type": "application/octet-stream",
                "is_binary": True,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        else:
            # Text file - read as text
            content = target.read_text(errors="replace")
            if len(content) > MAX_FILE_SIZE:
                content = content[:MAX_FILE_SIZE]
            return {
                "path": display_path,
                "name": target.name,
                "size": stat.st_size,
                "content": content,
                "content_type": "text/plain",
                "is_binary": False,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }

    @app.get("/teams/{team}/files/raw")
    def serve_raw_file(team: str, path: str):
        """Serve a raw file (absolute or delegate-relative path).

        Returns the file with its native content type so browsers can render it directly.
        Used for opening HTML attachments in new tabs.
        """
        target = _resolve_file_path(team, path)

        # Read file content
        file_bytes = target.read_bytes()

        # Determine content type
        ext = target.suffix.lower()
        if ext in (".html", ".htm"):
            media_type = "text/html"
        else:
            # Use mimetypes module as fallback
            guessed_type, _ = mimetypes.guess_type(target.name)
            media_type = guessed_type or "application/octet-stream"

        return Response(content=file_bytes, media_type=media_type)

    # ===================================================================
    # /internal/* — Satellite API endpoints (bearer-token protected)
    # ===================================================================

    from fastapi import Request, Depends, Cookie
    from fastapi.responses import RedirectResponse

    async def _require_satellite_token(request: Request) -> str:
        """Dependency: validate bearer token for /internal/* routes.

        Returns the satellite name on success, raises 401 on failure.

        TODO: Add per-team/agent authorization.  Currently any valid satellite
        token grants access to *all* teams and agents.  Each /internal/*
        endpoint should verify that the satellite actually owns the
        team/agent it is operating on (e.g. check agent state.yaml
        ``host`` field matches the satellite name).
        """
        from delegate.auth import validate_satellite_token

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        token = auth_header[7:]
        satellite_name = validate_satellite_token(hc_home, token)
        if satellite_name is None:
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        return satellite_name

    # --- Satellite polling ---

    @app.get("/internal/satellite/poll")
    def satellite_poll(satellite_id: str, _sat: str = Depends(_require_satellite_token)):
        """Return agents with unread messages whose host matches this satellite."""
        from delegate.mailbox import agents_with_unread
        from delegate.runtime import list_ai_agents

        result = []
        for team in _list_teams(hc_home):
            ai_agents = set(list_ai_agents(hc_home, team))
            for agent_name in agents_with_unread(hc_home, team):
                if agent_name not in ai_agents:
                    continue
                # Check if this agent is assigned to this satellite
                ad = _agent_dir(hc_home, team, agent_name)
                state_file = ad / "state.yaml"
                if not state_file.exists():
                    continue
                state = yaml.safe_load(state_file.read_text()) or {}
                if state.get("host") == satellite_id:
                    result.append({
                        "team": team,
                        "agent": agent_name,
                        "role": state.get("role", "engineer"),
                        "model": state.get("model", "sonnet"),
                    })
        return {"agents": result}

    # --- Mailbox proxies ---

    class MailboxSendBody(BaseModel):
        team: str
        sender: str
        recipient: str
        message: str
        task_id: int | None = None

    @app.post("/internal/mailbox/send")
    def internal_mailbox_send(body: MailboxSendBody, _sat: str = Depends(_require_satellite_token)):
        msg_id = _send(hc_home, body.team, body.sender, body.recipient, body.message, task_id=body.task_id)
        return {"id": msg_id}

    class MailboxClaimBody(BaseModel):
        team: str
        agent: str
        limit: int = 50

    @app.post("/internal/mailbox/claim")
    def internal_mailbox_claim(body: MailboxClaimBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.mailbox import claim_inbox_batch
        messages = claim_inbox_batch(hc_home, body.team, body.agent, limit=body.limit)
        return {"messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "recipient": m.recipient,
                "body": m.body,
                "task_id": m.task_id,
                "time": m.time,
                "delivered_at": m.delivered_at,
                "seen_at": m.seen_at,
                "processed_at": m.processed_at,
            }
            for m in messages
        ]}

    class MailboxMarkProcessedBody(BaseModel):
        team: str
        message_ids: list[int]

    @app.post("/internal/mailbox/mark-processed")
    def internal_mailbox_mark_processed(body: MailboxMarkProcessedBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.mailbox import mark_processed_batch
        mark_processed_batch(hc_home, body.team, body.message_ids)
        return {"ok": True}

    @app.get("/internal/mailbox/inbox")
    def internal_mailbox_inbox(team: str, agent: str, _sat: str = Depends(_require_satellite_token)):
        messages = _read_inbox(hc_home, team, agent, unread_only=True)
        return {"messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "body": m.body,
                "task_id": m.task_id,
                "time": m.time,
            }
            for m in messages
        ]}

    # --- Task proxies ---

    class TaskCreateBody(BaseModel):
        team: str
        title: str
        assignee: str
        description: str = ""
        priority: str = "medium"
        repo: str = ""
        depends_on: list[int] | None = None

    @app.post("/internal/task/create")
    def internal_task_create(body: TaskCreateBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.task import create_task
        task = create_task(
            hc_home, body.team,
            title=body.title,
            assignee=body.assignee,
            description=body.description,
            priority=body.priority,
            repo=body.repo,
            depends_on=body.depends_on,
        )
        return task

    @app.get("/internal/task/list")
    def internal_task_list(team: str, status: str | None = None, assignee: str | None = None, _sat: str = Depends(_require_satellite_token)):
        kwargs = {}
        if status:
            kwargs["status"] = status
        if assignee:
            kwargs["assignee"] = assignee
        return _list_tasks(hc_home, team, **kwargs)

    @app.get("/internal/task/show")
    def internal_task_show(team: str, task_id: int, _sat: str = Depends(_require_satellite_token)):
        return _get_task(hc_home, team, task_id)

    class TaskStatusBody(BaseModel):
        team: str
        task_id: int
        new_status: str

    @app.post("/internal/task/status")
    def internal_task_status(body: TaskStatusBody, _sat: str = Depends(_require_satellite_token)):
        _change_status(hc_home, body.team, body.task_id, body.new_status)
        return {"ok": True}

    class TaskAssignBody(BaseModel):
        team: str
        task_id: int
        assignee: str

    @app.post("/internal/task/assign")
    def internal_task_assign(body: TaskAssignBody, _sat: str = Depends(_require_satellite_token)):
        _update_task(hc_home, body.team, body.task_id, assignee=body.assignee)
        return {"ok": True}

    class TaskCommentInternalBody(BaseModel):
        team: str
        task_id: int
        author: str
        body: str

    @app.post("/internal/task/comment")
    def internal_task_comment(body: TaskCommentInternalBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.task import add_comment
        cid = add_comment(hc_home, body.team, body.task_id, body.author, body.body)
        return {"id": cid}

    class TaskCancelBody(BaseModel):
        team: str
        task_id: int

    @app.post("/internal/task/cancel")
    def internal_task_cancel(body: TaskCancelBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.task import cancel_task
        return cancel_task(hc_home, body.team, body.task_id)

    class TaskAttachBody(BaseModel):
        team: str
        task_id: int
        file_path: str

    @app.post("/internal/task/attach")
    def internal_task_attach(body: TaskAttachBody, _sat: str = Depends(_require_satellite_token)):
        task = _get_task(hc_home, body.team, body.task_id)
        attachments = list(task.get("attachments", []))
        if body.file_path not in attachments:
            attachments.append(body.file_path)
        _update_task(hc_home, body.team, body.task_id, attachments=attachments)
        return {"ok": True}

    class TaskDetachBody(BaseModel):
        team: str
        task_id: int
        file_path: str

    @app.post("/internal/task/detach")
    def internal_task_detach(body: TaskDetachBody, _sat: str = Depends(_require_satellite_token)):
        task = _get_task(hc_home, body.team, body.task_id)
        attachments = list(task.get("attachments", []))
        if body.file_path in attachments:
            attachments.remove(body.file_path)
        _update_task(hc_home, body.team, body.task_id, attachments=attachments)
        return {"ok": True}

    # --- Repo proxy ---

    @app.get("/internal/repo/list")
    def internal_repo_list(team: str, _sat: str = Depends(_require_satellite_token)):
        from delegate.config import get_repos
        repos = get_repos(hc_home, team)
        result = []
        for name, meta in repos.items():
            result.append({
                "name": name,
                "source": meta.get("source", ""),
                "remote_url": meta.get("remote_url", ""),
                "approval": meta.get("approval", "manual"),
            })
        return {"repos": result}

    # --- Session & activity proxies ---

    class SessionStartBody(BaseModel):
        team: str
        agent: str
        task_id: int | None = None

    @app.post("/internal/session/start")
    def internal_session_start(body: SessionStartBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.chat import start_session
        session_id = start_session(hc_home, body.team, body.agent, task_id=body.task_id)
        return {"session_id": session_id}

    class SessionEndBody(BaseModel):
        team: str
        session_id: int
        tokens_in: int = 0
        tokens_out: int = 0
        cost_usd: float = 0.0
        cache_read_tokens: int = 0
        cache_write_tokens: int = 0

    @app.post("/internal/session/end")
    def internal_session_end(body: SessionEndBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.chat import end_session
        end_session(
            hc_home, body.team, body.session_id,
            tokens_in=body.tokens_in,
            tokens_out=body.tokens_out,
            cost_usd=body.cost_usd,
            cache_read_tokens=body.cache_read_tokens,
            cache_write_tokens=body.cache_write_tokens,
        )
        return {"ok": True}

    class ActivityBroadcastBody(BaseModel):
        agent: str
        team: str
        tool: str
        detail: str
        task_id: int | None = None

    @app.post("/internal/activity/broadcast")
    def internal_activity_broadcast(body: ActivityBroadcastBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.activity import broadcast as _broadcast
        _broadcast(body.agent, body.team, body.tool, body.detail, task_id=body.task_id)
        return {"ok": True}

    class TurnEventBody(BaseModel):
        event_type: str
        agent: str
        team: str = ""
        task_id: int | None = None
        sender: str = ""

    @app.post("/internal/activity/turn-event")
    def internal_turn_event(body: TurnEventBody, _sat: str = Depends(_require_satellite_token)):
        from delegate.activity import broadcast_turn_event as _bte
        _bte(body.event_type, body.agent, team=body.team, task_id=body.task_id, sender=body.sender)
        return {"ok": True}

    # --- Agent config proxy ---

    @app.get("/internal/agent/config")
    def internal_agent_config(team: str, agent: str, _sat: str = Depends(_require_satellite_token)):
        """Return agent config needed by satellite to build prompts."""
        ad = _agent_dir(hc_home, team, agent)
        state_file = ad / "state.yaml"
        if not state_file.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")

        state = yaml.safe_load(state_file.read_text()) or {}

        # Read bio
        bio_file = ad / "bio.md"
        bio = bio_file.read_text() if bio_file.exists() else ""

        # Read context.md
        context_file = ad / "context.md"
        context = context_file.read_text() if context_file.exists() else ""

        # Read preamble from prompt.py
        from delegate.prompt import Prompt
        preamble = Prompt(hc_home, team, agent).build_preamble()

        return {
            "role": state.get("role", "engineer"),
            "model": state.get("model", "sonnet"),
            "token_budget": state.get("token_budget"),
            "bio": bio,
            "context": context,
            "preamble": preamble,
        }

    class WriteContextBody(BaseModel):
        team: str
        agent: str
        content: str

    @app.post("/internal/agent/write-context")
    def internal_agent_write_context(body: WriteContextBody, _sat: str = Depends(_require_satellite_token)):
        ad = _agent_dir(hc_home, body.team, body.agent)
        (ad / "context.md").write_text(body.content)
        return {"ok": True}

    class WriteWorklogBody(BaseModel):
        team: str
        agent: str
        session_id: int
        content: str

    @app.post("/internal/agent/write-worklog")
    def internal_agent_write_worklog(body: WriteWorklogBody, _sat: str = Depends(_require_satellite_token)):
        ad = _agent_dir(hc_home, body.team, body.agent)
        logs_dir = ad / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        worklog_path = logs_dir / f"{body.session_id}.worklog.md"
        worklog_path.write_text(body.content)
        return {"ok": True}

    # ===================================================================
    # Web UI passphrase authentication middleware
    # ===================================================================

    @app.middleware("http")
    async def _passphrase_auth_middleware(request: Request, call_next):
        """Require session cookie for web UI routes when passphrase is set.

        Exempted paths:
        - /internal/* (uses bearer token auth)
        - /login (login page and form submission)
        - /static/* (CSS/JS assets)
        - /manifest.json, /sw.js (PWA files)
        """
        from delegate.auth import is_passphrase_enabled, validate_session_cookie

        path = request.url.path
        # Skip auth for internal API, login, and static routes
        if (
            path.startswith("/internal/")
            or path == "/login"
            or path.startswith("/static/")
            or path == "/manifest.json"
            or path == "/sw.js"
        ):
            return await call_next(request)

        if not is_passphrase_enabled(hc_home):
            return await call_next(request)

        # Check session cookie
        cookie = request.cookies.get("delegate_session")
        if cookie and validate_session_cookie(hc_home, cookie):
            return await call_next(request)

        # Redirect to login for HTML requests, return 401 for API
        if path.startswith("/api/") or path.startswith("/teams/"):
            # Check if it's an API call (Accept: application/json)
            accept = request.headers.get("accept", "")
            if "application/json" in accept or "text/html" not in accept:
                raise HTTPException(status_code=401, detail="Authentication required")

        return RedirectResponse(url="/login", status_code=302)

    # --- Login page ---

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        from delegate.auth import is_passphrase_enabled
        if not is_passphrase_enabled(hc_home):
            return RedirectResponse(url="/", status_code=302)
        return """<!DOCTYPE html>
<html><head><title>Delegate Login</title>
<style>
body{background:#1e1e1e;color:#e0e0e0;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
form{background:#2d2d2d;padding:2rem;border-radius:8px;min-width:300px}
h2{margin-top:0;color:#f0f0f0}
input{width:100%;padding:0.5rem;margin:0.5rem 0;box-sizing:border-box;background:#3d3d3d;border:1px solid #555;color:#e0e0e0;border-radius:4px}
button{width:100%;padding:0.5rem;margin-top:0.5rem;background:#4a9eff;color:white;border:none;border-radius:4px;cursor:pointer}
button:hover{background:#3a8eef}
.error{color:#ff6b6b;font-size:0.9em}
</style></head>
<body><form method="post" action="/login">
<h2>Delegate</h2>
<input type="password" name="passphrase" placeholder="Passphrase" autofocus>
<button type="submit">Login</button>
</form></body></html>"""

    @app.post("/login")
    async def login_submit(request: Request):
        from delegate.auth import verify_passphrase, create_session_cookie
        form = await request.form()
        passphrase = form.get("passphrase", "")
        if verify_passphrase(hc_home, passphrase):
            cookie = create_session_cookie(hc_home)
            response = RedirectResponse(url="/", status_code=302)
            is_dev = os.environ.get("DELEGATE_DEV") == "1"
            response.set_cookie(
                key="delegate_session",
                value=cookie,
                httponly=True,
                samesite="lax",
                secure=not is_dev,
                max_age=7 * 24 * 3600,
            )
            return response
        return HTMLResponse("""<!DOCTYPE html>
<html><head><title>Delegate Login</title>
<style>
body{background:#1e1e1e;color:#e0e0e0;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
form{background:#2d2d2d;padding:2rem;border-radius:8px;min-width:300px}
h2{margin-top:0;color:#f0f0f0}
input{width:100%;padding:0.5rem;margin:0.5rem 0;box-sizing:border-box;background:#3d3d3d;border:1px solid #555;color:#e0e0e0;border-radius:4px}
button{width:100%;padding:0.5rem;margin-top:0.5rem;background:#4a9eff;color:white;border:none;border-radius:4px;cursor:pointer}
button:hover{background:#3a8eef}
.error{color:#ff6b6b;font-size:0.9em}
</style></head>
<body><form method="post" action="/login">
<h2>Delegate</h2>
<p class="error">Invalid passphrase</p>
<input type="password" name="passphrase" placeholder="Passphrase" autofocus>
<button type="submit">Login</button>
</form></body></html>""", status_code=401)

    # --- Static files ---
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    # Cache-bust token: derived from content hash so browsers always fetch
    # the latest bundle after a rebuild or package upgrade.
    import hashlib as _hashlib
    _cache_bust = ""
    _app_js = _static_dir / "app.js"
    if _app_js.is_file():
        _hash = _hashlib.md5(_app_js.read_bytes()).hexdigest()[:8]
        _cache_bust = f"?v={_hash}"

    def _serve_index():
        index_html = _static_dir / "index.html"
        if not index_html.is_file():
            return "Frontend not built. Run esbuild or npm run build."
        html = index_html.read_text()
        # Inject cache-bust query params for JS and CSS
        if _cache_bust:
            html = html.replace('"/static/app.js"', f'"/static/app.js{_cache_bust}"')
            html = html.replace('"/static/styles.css"', f'"/static/styles.css{_cache_bust}"')
        return html

    @app.get("/manifest.json")
    def manifest():
        port = int(os.environ.get("DELEGATE_PORT", "3548"))
        name = "Delegate" if port == 3548 else f"Delegate :{port}"
        return JSONResponse({
            "name": name,
            "short_name": name,
            "start_url": "/",
            "display": "standalone",
            "background_color": "#1e1e1e",
            "theme_color": "#1e1e1e",
            "id": "/",
            "icons": [
                {"src": "/static/pwa-icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/pwa-icon-512.png", "sizes": "512x512", "type": "image/png"},
            ]
        })

    @app.get("/sw.js")
    def service_worker():
        # Minimal service worker for PWA installability.
        # Delegate requires the daemon running, so no offline caching is needed.
        content = 'self.addEventListener("fetch", () => {});\n'
        return Response(content=content, media_type="application/javascript")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _serve_index()

    # Catch-all for SPA routing (must be last to not intercept API routes)
    @app.get("/{full_path:path}", response_class=HTMLResponse)
    def catch_all(full_path: str = ""):
        return _serve_index()

    return app
