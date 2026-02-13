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
import json
import logging
import mimetypes
import os
import shutil
import signal as signal_mod
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from delegate.paths import (
    home as _default_home,
    agents_dir as _agents_dir,
    agent_dir as _agent_dir,
    shared_dir as _shared_dir,
    team_dir as _team_dir,
    teams_dir as _teams_dir,
)
from delegate.config import get_boss
from delegate.task import list_tasks as _list_tasks, get_task as _get_task, get_task_diff as _get_task_diff, get_task_merge_preview as _get_merge_preview, get_task_commit_diffs as _get_commit_diffs, update_task as _update_task, change_status as _change_status, VALID_STATUSES, format_task_id
from delegate.chat import get_messages as _get_messages, get_task_stats as _get_task_stats, get_agent_stats as _get_agent_stats, log_event as _log_event
from delegate.mailbox import send as _send, read_inbox as _read_inbox, read_outbox as _read_outbox, count_unread as _count_unread
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_teams(hc_home: Path) -> list[str]:
    """List all team names under hc_home/teams/."""
    td = _teams_dir(hc_home)
    if not td.is_dir():
        return []
    return sorted(d.name for d in td.iterdir() if d.is_dir())


def _first_team(hc_home: Path) -> str:
    """Return the first team name (for single-team operations)."""
    teams = _list_teams(hc_home)
    return teams[0] if teams else "default"


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


def _list_team_agents(hc_home: Path, team: str) -> list[dict]:
    """List AI agents for a team (excludes boss)."""
    ad = _agents_dir(hc_home, team)
    agents = []
    if not ad.is_dir():
        return agents

    # Pre-load all in_progress tasks once (lightweight — avoids per-agent scans)
    try:
        ip_tasks = _list_tasks(hc_home, team, status="in_progress")
    except FileNotFoundError:
        ip_tasks = []

    for d in sorted(ad.iterdir()):
        state_file = d / "state.yaml"
        if not d.is_dir() or not state_file.exists():
            continue
        state = yaml.safe_load(state_file.read_text()) or {}
        if state.get("role") == "boss":
            continue
        unread = _count_unread(hc_home, team, d.name)
        agents.append({
            "name": d.name,
            "role": state.get("role", "engineer"),
            "pid": True,  # All agents are always online — daemon dispatches turns
            "unread_inbox": unread,
            "team": team,
            "last_active_at": _agent_last_active_at(d),
            "current_task": _agent_current_task(hc_home, team, d.name, ip_tasks),
        })
    return agents


# ---------------------------------------------------------------------------
# Startup greeting — dynamic, time-aware message from manager
# ---------------------------------------------------------------------------

def _build_greeting(
    hc_home: Path,
    team: str,
    manager: str,
    boss: str,
    now_utc: "datetime",
) -> str:
    """Build a context-aware startup greeting from the manager.

    Takes into account:
    - Time of day (in the user's local timezone via the system clock)
    - Active in-progress tasks (brief status summary)
    """
    from datetime import datetime, timezone
    from delegate.task import list_tasks

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
    lines = [f"{time_greeting} — {manager.capitalize()} here, your team manager."]

    if status_parts:
        lines.append("Current board: " + ", ".join(status_parts) + ".")
    else:
        lines.append("The board is clear — ready for new work.")

    lines.append("Send me tasks, questions, or anything you need the team on.")

    return " ".join(lines)


# ---------------------------------------------------------------------------
# Daemon loop — runs as a background asyncio task inside the lifespan
# ---------------------------------------------------------------------------

# Module-level tracking of active agent asyncio tasks for shutdown
_active_agent_tasks: set[asyncio.Task] = set()
_active_merge_tasks: set[asyncio.Task] = set()
_shutdown_flag: bool = False

async def _daemon_loop(
    hc_home: Path,
    interval: float,
    max_concurrent: int,
    default_token_budget: int | None,
) -> None:
    """Route messages, dispatch agent turns, and process merges (all teams).

    All agents are "always online".  Instead of spawning subprocesses,
    the daemon dispatches ``run_turn()`` as asyncio tasks when an agent
    has unread mail.  A semaphore enforces *max_concurrent* across all
    teams.
    """
    from delegate.runtime import run_turn, list_ai_agents
    from delegate.merge import merge_once
    from delegate.bootstrap import get_member_by_role
    from delegate.mailbox import send as send_message, agents_with_unread

    logger.info("Daemon loop started — polling every %.1fs", interval)

    sem = asyncio.Semaphore(max_concurrent)
    merge_sem = asyncio.Semaphore(1)
    in_flight: set[tuple[str, str]] = set()  # (team, agent) pairs currently running

    async def _dispatch_turn(team: str, agent: str) -> None:
        """Dispatch and run one turn, then remove from in_flight."""
        async with sem:
            try:
                result = await run_turn(hc_home, team, agent)
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
                raise
            except Exception:
                logger.exception("Uncaught error in turn | agent=%s | team=%s", agent, team)
            finally:
                in_flight.discard((team, agent))

    # --- One-time startup: greeting from managers ---
    # Only send if no message from manager→boss in the last 30 minutes.
    # Content-agnostic: the greeting text can change freely without
    # breaking dedup.  Handles frequent uvicorn reloads gracefully.
    try:
        from delegate.db import get_connection
        from delegate.task import list_tasks
        from datetime import datetime, timedelta, timezone

        teams = _list_teams(hc_home)
        boss_name = get_boss(hc_home) or "boss"
        now_utc = datetime.now(timezone.utc)
        cutoff = (now_utc - timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%fZ"
        )

        for team in teams:
            manager_name = get_member_by_role(hc_home, team, "manager")
            if not manager_name:
                continue

            # Check if manager sent *any* message to boss recently
            conn = get_connection(hc_home, team)
            row = conn.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE type = 'chat' AND sender = ? AND recipient = ?
                     AND timestamp > ?""",
                (manager_name, boss_name, cutoff),
            ).fetchone()
            recent_count = row[0] if row else 0
            conn.close()

            if recent_count > 0:
                logger.info(
                    "Skipping startup greeting (recent activity) | team=%s",
                    team,
                )
                continue

            greeting = _build_greeting(
                hc_home, team, manager_name, boss_name, now_utc,
            )
            send_message(
                hc_home, team,
                sender=manager_name,
                recipient=boss_name,
                message=greeting,
            )
            logger.info(
                "Manager %s sent startup greeting to %s | team=%s",
                manager_name, boss_name, team,
            )
    except Exception:
        logger.exception("Error during startup greeting")

    # --- Main loop ---
    while True:
        try:
            # Check shutdown flag at the top of each iteration
            global _shutdown_flag
            if _shutdown_flag:
                logger.info("Shutdown flag set — exiting daemon loop")
                break

            teams = _list_teams(hc_home)
            boss_name = get_boss(hc_home)

            for team in teams:
                # Check shutdown flag before dispatching new tasks
                if _shutdown_flag:
                    break

                # Find agents with unread messages and dispatch turns
                ai_agents = set(list_ai_agents(hc_home, team))
                needing_turn = [
                    a for a in agents_with_unread(hc_home, team)
                    if a in ai_agents
                ]
                for agent in needing_turn:
                    # Check shutdown flag before dispatching
                    if _shutdown_flag:
                        break

                    key = (team, agent)
                    if key not in in_flight:
                        in_flight.add(key)
                        agent_task = asyncio.create_task(_dispatch_turn(team, agent))
                        _active_agent_tasks.add(agent_task)
                        agent_task.add_done_callback(_active_agent_tasks.discard)

                # Process merge queue (serialized — one merge at a time)
                if not _shutdown_flag:
                    async def _run_merge(t: str) -> None:
                        async with merge_sem:
                            results = await asyncio.to_thread(merge_once, hc_home, t)
                            for mr in results:
                                if mr.success:
                                    logger.info("Merged %s in %s: %s", mr.task_id, t, mr.message)
                                else:
                                    logger.warning("Merge failed %s in %s: %s", mr.task_id, t, mr.message)

                    merge_task = asyncio.create_task(_run_merge(team))
                    _active_merge_tasks.add(merge_task)
                    merge_task.add_done_callback(_active_merge_tasks.discard)
        except asyncio.CancelledError:
            logger.info("Daemon loop cancelled")
            raise
        except Exception:
            logger.exception("Error during daemon cycle")
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
        # Put in its own process group so we can kill node + esbuild together
        start_new_session=True,
    )
    return proc


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start/stop the daemon loop and frontend watcher with the server.

    The esbuild watcher is started automatically whenever a ``frontend/``
    source directory is detected (i.e. running from a source checkout).
    In a pip-installed deployment there is no ``frontend/`` and the watcher
    is silently skipped — the pre-built assets in ``delegate/static/`` are used.
    """
    hc_home = app.state.hc_home
    enable = os.environ.get("DELEGATE_DAEMON", "").lower() in ("1", "true", "yes")

    # Reset shutdown flag (for server restart/reload scenarios)
    global _shutdown_flag
    _shutdown_flag = False

    task = None
    esbuild_proc: subprocess.Popen | None = None

    if enable:
        interval = float(os.environ.get("DELEGATE_INTERVAL", "1.0"))
        max_concurrent = int(os.environ.get("DELEGATE_MAX_CONCURRENT", "256"))
        budget_str = os.environ.get("DELEGATE_TOKEN_BUDGET")
        token_budget = int(budget_str) if budget_str else None

        task = asyncio.create_task(
            _daemon_loop(hc_home, interval, max_concurrent, token_budget)
        )

    # Auto-start frontend watcher if source checkout detected
    frontend_dir = _find_frontend_dir()
    if frontend_dir:
        esbuild_proc = _start_esbuild_watch(frontend_dir)

    yield

    # Shut down esbuild watcher (kill entire process group: node + esbuild)
    if esbuild_proc is not None:
        logger.info("Stopping esbuild watcher (PID %d)", esbuild_proc.pid)
        try:
            os.killpg(os.getpgid(esbuild_proc.pid), signal_mod.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            esbuild_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(esbuild_proc.pid), signal_mod.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    if task is not None:
        # Set shutdown flag before cancelling the daemon loop
        _shutdown_flag = True
        logger.info("Setting shutdown flag and cancelling daemon loop")

        # Cancel the daemon loop
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("Daemon loop stopped")

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
            # Snapshot the set before iteration to avoid mutation during iteration
            for agent_task in list(_active_agent_tasks):
                agent_task.cancel()

            # Wait for tasks to finish with 10 second timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*_active_agent_tasks, return_exceptions=True),
                    timeout=10.0
                )
                logger.info("All agent sessions finished")
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout waiting for agent sessions — %d task(s) still running",
                    len([t for t in _active_agent_tasks if not t.done()])
                )
            _active_agent_tasks.clear()


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

    app = FastAPI(title="Delegate UI", lifespan=_lifespan)
    app.state.hc_home = hc_home

    # --- Config endpoint ---

    @app.get("/config")
    def get_config():
        """Return app configuration (boss name, etc.) for the frontend."""
        return {
            "boss_name": get_boss(hc_home) or "boss",
            "hc_home": str(hc_home),
        }

    # --- Team endpoints ---

    @app.get("/teams")
    def get_teams():
        return _list_teams(hc_home)

    # --- Task endpoints (team-scoped) ---

    @app.get("/teams/{team}/tasks")
    def get_team_tasks(team: str, status: str | None = None, assignee: str | None = None):
        return _list_tasks(hc_home, team, status=status, assignee=assignee)

    @app.get("/teams/{team}/tasks/{task_id}/stats")
    def get_team_task_stats(team: str, task_id: int):
        try:
            task = _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        stats = _get_task_stats(hc_home, team, task_id)

        # Compute elapsed time
        created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        completed_at = task.get("completed_at")
        if completed_at:
            ended = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        else:
            ended = datetime.now(timezone.utc)
        elapsed_seconds = (ended - created).total_seconds()

        return {
            "task_id": task_id,
            "elapsed_seconds": elapsed_seconds,
            "branch": task.get("branch", ""),
            **stats,
        }

    @app.get("/teams/{team}/tasks/{task_id}/diff")
    def get_team_task_diff(team: str, task_id: int):
        try:
            task = _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        diff_dict = _get_task_diff(hc_home, team, task_id)
        return {
            "task_id": task_id,
            "branch": task.get("branch", ""),
            "repo": task.get("repo", []),
            "diff": diff_dict,
            "merge_base": task.get("merge_base", {}),
            "merge_tip": task.get("merge_tip", {}),
        }

    @app.get("/teams/{team}/tasks/{task_id}/merge-preview")
    def get_team_task_merge_preview(team: str, task_id: int):
        """Return a diff of branch vs current main (merge preview)."""
        try:
            task = _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        diff_dict = _get_merge_preview(hc_home, team, task_id)
        return {
            "task_id": task_id,
            "branch": task.get("branch", ""),
            "diff": diff_dict,
        }

    @app.get("/teams/{team}/tasks/{task_id}/commits")
    def get_team_task_commits(team: str, task_id: int):
        """Return per-commit diffs for a task, keyed by repo."""
        try:
            _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        # Frontend expects { commit_diffs: { repo: [...] } }
        return {"commit_diffs": _get_commit_diffs(hc_home, team, task_id)}

    @app.get("/teams/{team}/tasks/{task_id}/activity")
    def get_team_task_activity(team: str, task_id: int, limit: int | None = None):
        """Return interleaved activity (events + messages + comments) for a task."""
        from delegate.chat import get_task_timeline

        try:
            _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return get_task_timeline(hc_home, team, task_id, limit=limit)

    # --- Task comments endpoints (team-scoped) ---

    @app.get("/teams/{team}/tasks/{task_id}/comments")
    def get_team_task_comments(team: str, task_id: int, limit: int = 50):
        """Return comments for a task."""
        from delegate.task import get_comments as _get_comments
        try:
            _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return _get_comments(hc_home, team, task_id, limit=limit)

    class TaskCommentBody(BaseModel):
        author: str
        body: str

    @app.post("/teams/{team}/tasks/{task_id}/comments")
    def post_team_task_comment(team: str, task_id: int, comment: TaskCommentBody):
        """Add a comment to a task."""
        from delegate.task import add_comment as _add_comment
        try:
            _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        cid = _add_comment(hc_home, team, task_id, comment.author, comment.body)
        return {"id": cid, "task_id": task_id, "author": comment.author, "body": comment.body}

    # --- Review endpoints (team-scoped) ---

    @app.get("/teams/{team}/tasks/{task_id}/reviews")
    def get_task_reviews(team: str, task_id: int):
        """Return all review attempts for a task."""
        from delegate.review import get_reviews, get_comments
        try:
            _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        reviews = get_reviews(hc_home, team, task_id)
        # Attach comments to each review
        for r in reviews:
            r["comments"] = get_comments(hc_home, team, task_id, r["attempt"])
        return reviews

    @app.get("/teams/{team}/tasks/{task_id}/reviews/current")
    def get_task_current_review(team: str, task_id: int):
        """Return the current (latest) review attempt with comments."""
        from delegate.review import get_current_review
        try:
            _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        review = get_current_review(hc_home, team, task_id)
        if review is None:
            return {"attempt": 0, "verdict": None, "summary": "", "comments": []}
        return review

    class ReviewCommentBody(BaseModel):
        file: str
        line: int | None = None
        body: str

    @app.post("/teams/{team}/tasks/{task_id}/reviews/comments")
    def post_review_comment(team: str, task_id: int, comment: ReviewCommentBody):
        """Add an inline comment to the current review attempt."""
        from delegate.review import add_comment
        try:
            task = _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        attempt = task.get("review_attempt", 0)
        if attempt == 0:
            raise HTTPException(status_code=400, detail="Task has no active review attempt.")

        boss_name = get_boss(hc_home) or "boss"
        result = add_comment(
            hc_home, team, task_id, attempt,
            file=comment.file, body=comment.body, author=boss_name,
            line=comment.line,
        )
        return result

    class ReviewCommentUpdateBody(BaseModel):
        body: str

    @app.put("/teams/{team}/tasks/{task_id}/reviews/comments/{comment_id}")
    def edit_review_comment(team: str, task_id: int, comment_id: int, payload: ReviewCommentUpdateBody):
        """Edit an existing review comment's body."""
        from delegate.review import update_comment
        result = update_comment(hc_home, team, comment_id, payload.body)
        if result is None:
            raise HTTPException(status_code=404, detail="Comment not found")
        return result

    @app.delete("/teams/{team}/tasks/{task_id}/reviews/comments/{comment_id}")
    def remove_review_comment(team: str, task_id: int, comment_id: int):
        """Delete a review comment."""
        from delegate.review import delete_comment
        deleted = delete_comment(hc_home, team, comment_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Comment not found")
        return {"ok": True}

    # --- Task approval endpoints (team-scoped) ---

    class ApproveBody(BaseModel):
        summary: str = ""

    @app.post("/teams/{team}/tasks/{task_id}/approve")
    def approve_task(team: str, task_id: int, body: ApproveBody | None = None):
        """Approve a task for merge."""
        from delegate.review import set_verdict
        try:
            task = _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        if task["status"] != "in_approval":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve task in '{task['status']}' status. Task must be in 'in_approval' status.",
            )

        # Record verdict on the review
        attempt = task.get("review_attempt", 0)
        boss_name = get_boss(hc_home) or "boss"
        summary = body.summary if body else ""
        if attempt > 0:
            set_verdict(hc_home, team, task_id, attempt, "approved", summary=summary, reviewer=boss_name)

        updated = _update_task(hc_home, team, task_id, approval_status="approved")
        _log_event(hc_home, team, f"{format_task_id(task_id)} approved \u2713", task_id=task_id)
        return updated

    class RejectBody(BaseModel):
        reason: str
        summary: str = ""

    @app.post("/teams/{team}/tasks/{task_id}/reject")
    def reject_task(team: str, task_id: int, body: RejectBody):
        """Reject a task with a reason."""
        from delegate.review import set_verdict
        try:
            task = _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        try:
            updated = _change_status(hc_home, team, task_id, "rejected")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Record verdict on the review
        attempt = task.get("review_attempt", 0)
        boss_name = get_boss(hc_home) or "boss"
        # Use reason as summary if no separate summary provided
        summary = body.summary or body.reason
        if attempt > 0:
            set_verdict(hc_home, team, task_id, attempt, "rejected", summary=summary, reviewer=boss_name)

        updated = _update_task(hc_home, team, task_id,
                               rejection_reason=body.reason,
                               approval_status="rejected")

        # Send notification to manager via the notify module
        from delegate.notify import notify_rejection
        notify_rejection(hc_home, team, task, reason=body.reason)

        _log_event(hc_home, team, f"{format_task_id(task_id)} rejected \u2014 {body.reason}", task_id=task_id)
        return updated

    @app.post("/teams/{team}/tasks/{task_id}/retry-merge")
    def retry_merge(team: str, task_id: int):
        """Retry a failed merge.

        Resets ``merge_attempts`` to 0, clears ``status_detail``, and
        transitions the task to ``merging`` with the manager as assignee.
        The merge worker will pick it up on the next daemon cycle.
        """
        try:
            task = _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        if task["status"] != "merge_failed":
            raise HTTPException(
                status_code=400,
                detail=f"Task is in '{task['status']}', not 'merge_failed'",
            )

        from delegate.task import transition_task
        # Reset counters and detail
        _update_task(hc_home, team, task_id,
                     merge_attempts=0, status_detail="")
        # Transition to merging with manager as assignee
        from delegate.bootstrap import get_member_by_role
        manager = get_member_by_role(hc_home, team, "manager") or "manager"
        updated = transition_task(hc_home, team, task_id, "merging", manager)
        return updated

    @app.post("/teams/{team}/tasks/{task_id}/cancel")
    def cancel_task_endpoint(team: str, task_id: int):
        """Cancel a task.

        Sets status to ``cancelled``, clears the assignee, and
        performs best-effort cleanup of worktrees and branches.
        """
        try:
            _get_task(hc_home, team, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        try:
            from delegate.task import cancel_task
            updated = cancel_task(hc_home, team, task_id)
            return updated
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # --- Message endpoints (team-scoped) ---

    @app.get("/teams/{team}/messages")
    def get_team_messages(team: str, since: str | None = None, between: str | None = None, type: str | None = None, limit: int | None = None):
        between_tuple = None
        if between:
            parts = [p.strip() for p in between.split(",")]
            if len(parts) == 2:
                between_tuple = (parts[0], parts[1])
        return _get_messages(hc_home, team, since=since, between=between_tuple, msg_type=type, limit=limit)

    class SendMessage(BaseModel):
        team: str | None = None
        recipient: str
        content: str

    @app.post("/teams/{team}/messages")
    def post_team_message(team: str, msg: SendMessage):
        """Boss sends a message to any agent in the team."""
        boss_name = get_boss(hc_home) or "boss"
        team_agents = _list_team_agents(hc_home, team)
        agent_names = {a["name"] for a in team_agents}
        if msg.recipient not in agent_names:
            raise HTTPException(
                status_code=403,
                detail=f"Recipient '{msg.recipient}' is not an agent in team '{team}'",
            )
        _send(hc_home, team, boss_name, msg.recipient, msg.content)
        return {"status": "queued"}

    # --- Legacy global endpoints (aggregate across all teams) ---
    # Prefixed with /api/ to avoid colliding with SPA routes (/tasks, /agents).

    @app.get("/api/tasks")
    def get_tasks(status: str | None = None, assignee: str | None = None):
        """List tasks across all teams (for backward compat)."""
        all_tasks = []
        for t in _list_teams(hc_home):
            try:
                tasks = _list_tasks(hc_home, t, status=status, assignee=assignee)
                for task in tasks:
                    task["team"] = t
                all_tasks.extend(tasks)
            except Exception:
                pass
        return all_tasks

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
            except (FileNotFoundError, Exception):
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
            except (FileNotFoundError, Exception):
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
            except (FileNotFoundError, Exception):
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
                    raise HTTPException(status_code=400, detail=f"Cannot approve task in '{task['status']}' status.")
                attempt = task.get("review_attempt", 0)
                boss_name = get_boss(hc_home) or "boss"
                summary = body.summary if body else ""
                if attempt > 0:
                    set_verdict(hc_home, t, task_id, attempt, "approved", summary=summary, reviewer=boss_name)
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
                _change_status(hc_home, t, task_id, "rejected")
                attempt = task.get("review_attempt", 0)
                boss_name = get_boss(hc_home) or "boss"
                summary = body.summary or body.reason
                if attempt > 0:
                    set_verdict(hc_home, t, task_id, attempt, "rejected", summary=summary, reviewer=boss_name)
                updated = _update_task(hc_home, t, task_id, rejection_reason=body.reason, approval_status="rejected")
                from delegate.notify import notify_rejection
                notify_rejection(hc_home, t, task, reason=body.reason)
                _log_event(hc_home, t, f"{format_task_id(task_id)} rejected \u2014 {body.reason}", task_id=task_id)
                return updated
            except (FileNotFoundError, ValueError):
                continue
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    @app.get("/api/messages")
    def get_messages(since: str | None = None, between: str | None = None, type: str | None = None, limit: int | None = None):
        """Messages across all teams (legacy compat)."""
        between_tuple = None
        if between:
            parts = [p.strip() for p in between.split(",")]
            if len(parts) == 2:
                between_tuple = (parts[0], parts[1])
        all_msgs = []
        for t in _list_teams(hc_home):
            try:
                msgs = _get_messages(hc_home, t, since=since, between=between_tuple, msg_type=type, limit=limit)
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
        """Boss sends a message (legacy — uses msg.team field)."""
        team = msg.team or _first_team(hc_home)
        boss_name = get_boss(hc_home) or "boss"
        team_agents = _list_team_agents(hc_home, team)
        agent_names = {a["name"] for a in team_agents}
        if msg.recipient not in agent_names:
            raise HTTPException(
                status_code=403,
                detail=f"Recipient '{msg.recipient}' is not an agent in team '{team}'",
            )
        _send(hc_home, team, boss_name, msg.recipient, msg.content)
        return {"status": "queued"}

    # --- Agent endpoints (team-scoped) ---

    @app.get("/api/agents")
    def get_all_agents():
        """List all agents across all teams."""
        all_agents = []
        for team in _list_teams(hc_home):
            all_agents.extend(_list_team_agents(hc_home, team))
        return all_agents

    @app.get("/teams/{team}/agents")
    def get_agents(team: str):
        """List AI agents for a team (excludes boss)."""
        return _list_team_agents(hc_home, team)

    @app.get("/teams/{team}/agents/{name}/stats")
    def get_agent_stats(team: str, name: str):
        """Get aggregated stats for a specific agent."""
        return _get_agent_stats(hc_home, team, name)

    @app.get("/teams/{team}/agents/{name}/inbox")
    def get_agent_inbox(team: str, name: str):
        """Return all messages in the agent's inbox with lifecycle status."""
        all_msgs = _read_inbox(hc_home, team, name, unread_only=False)
        result = [
            {
                "id": m.id,
                "sender": m.sender,
                "time": m.time,
                "body": m.body,
                "delivered_at": m.delivered_at,
                "seen_at": m.seen_at,
                "processed_at": m.processed_at,
            }
            for m in all_msgs
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
                "delivered_at": m.delivered_at,
                "seen_at": m.seen_at,
                "processed_at": m.processed_at,
            }
            for m in all_msgs
        ]
        result.sort(key=lambda x: x["time"], reverse=True)
        return result[:100]

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

        Two path kinds are supported:

        * **Absolute** (starts with ``/``) — used directly.
        * **Delegate-relative** (anything else) — resolved from ``hc_home``
          (typically ``~/.delegate``).  E.g. ``teams/self/shared/spec.md``
          resolves to ``~/.delegate/teams/self/shared/spec.md``.

        Returns the resolved ``Path``, or raises 404.
        """
        if path.startswith("/"):
            target = Path(path).resolve()
        else:
            target = (hc_home / path).resolve()

        if not target.is_file():
            raise HTTPException(
                status_code=404, detail=f"File not found: {path}"
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

    # --- Static files ---
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        index_html = _static_dir / "index.html"
        if index_html.is_file():
            return index_html.read_text()
        return "Frontend not built. Run esbuild or npm run build."

    # Catch-all for SPA routing (must be last to not intercept API routes)
    @app.get("/{full_path:path}", response_class=HTMLResponse)
    def catch_all(full_path: str = ""):
        index_html = _static_dir / "index.html"
        if index_html.is_file():
            return index_html.read_text()
        return "Frontend not built. Run esbuild or npm run build."

    return app
