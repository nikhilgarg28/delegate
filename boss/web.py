"""FastAPI web application for the boss UI.

Provides:
    GET  /            — HTML single-page app
    GET  /teams       — list teams (JSON)
    GET  /tasks       — list tasks (JSON, global)
    GET  /tasks/{id}/stats — task stats (elapsed, agent time, tokens)
    GET  /tasks/{id}/diff — task diff
    GET  /messages    — get chat/event log (JSON, global)
    POST /messages    — boss sends a message to a team's manager
    GET  /teams/{team}/agents       — list agents for a team
    GET  /teams/{team}/agents/{name}/stats  — agent stats
    GET  /teams/{team}/agents/{name}/inbox  — agent inbox messages
    GET  /teams/{team}/agents/{name}/outbox — agent outbox messages
    GET  /teams/{team}/agents/{name}/logs   — agent worklog sessions

When started via the daemon, the daemon loop (message routing +
agent orchestration) runs as an asyncio background task inside the
FastAPI lifespan, so uvicorn restarts everything together.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from boss.paths import (
    home as _default_home,
    agents_dir as _agents_dir,
    agent_dir as _agent_dir,
    teams_dir as _teams_dir,
)
from boss.config import get_boss
from boss.task import list_tasks as _list_tasks, get_task as _get_task, get_task_diff as _get_task_diff, update_task as _update_task, change_status as _change_status, VALID_STATUSES, format_task_id
from boss.chat import get_messages as _get_messages, get_task_stats as _get_task_stats, get_agent_stats as _get_agent_stats, log_event as _log_event
from boss.mailbox import send as _send, read_inbox as _read_inbox, read_outbox as _read_outbox
from boss.bootstrap import get_member_by_role

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


def _list_team_agents(hc_home: Path, team: str) -> list[dict]:
    """List AI agents for a team (excludes boss)."""
    ad = _agents_dir(hc_home, team)
    agents = []
    if not ad.is_dir():
        return agents
    for d in sorted(ad.iterdir()):
        state_file = d / "state.yaml"
        if not d.is_dir() or not state_file.exists():
            continue
        state = yaml.safe_load(state_file.read_text()) or {}
        if state.get("role") == "boss":
            continue
        inbox_new = d / "inbox" / "new"
        unread = len(list(inbox_new.iterdir())) if inbox_new.is_dir() else 0
        agents.append({
            "name": d.name,
            "role": state.get("role", "worker"),
            "pid": state.get("pid"),
            "unread_inbox": unread,
            "team": team,
        })
    return agents


# ---------------------------------------------------------------------------
# Daemon loop — runs as a background asyncio task inside the lifespan
# ---------------------------------------------------------------------------

async def _daemon_loop(
    hc_home: Path,
    interval: float,
    max_concurrent: int,
    default_token_budget: int | None,
) -> None:
    """Route messages, spawn agents, and process merges on a fixed interval (all teams)."""
    from boss.router import route_once
    from boss.orchestrator import orchestrate_once, spawn_agent_subprocess
    from boss.merge import merge_once

    logger.info("Daemon loop started — polling every %.1fs", interval)

    while True:
        try:
            teams = _list_teams(hc_home)
            boss_name = get_boss(hc_home)

            for team in teams:
                def _spawn(h: Path, t: str, a: str) -> None:
                    spawn_agent_subprocess(h, t, a, token_budget=default_token_budget)

                routed = route_once(hc_home, team, boss_name=boss_name)
                if routed > 0:
                    logger.info("Routed %d message(s) for team %s", routed, team)

                spawned = orchestrate_once(
                    hc_home, team,
                    max_concurrent=max_concurrent,
                    spawn_fn=_spawn,
                )
                if spawned:
                    logger.info("Spawned agents in %s: %s", team, ", ".join(spawned))

                # Process merge queue
                merge_results = merge_once(hc_home, team)
                for mr in merge_results:
                    if mr.success:
                        logger.info("Merged %s in %s: %s", mr.task_id, team, mr.message)
                    else:
                        logger.warning("Merge failed %s in %s: %s", mr.task_id, team, mr.message)
        except Exception:
            logger.exception("Error during daemon cycle")
        await asyncio.sleep(interval)


def _find_frontend_dir() -> Path | None:
    """Locate the ``frontend/`` source directory (only exists in dev checkouts)."""
    # Walk upward from boss/ looking for frontend/build.js
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
    )
    return proc


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start/stop the daemon loop and frontend watcher with the server.

    The esbuild watcher is started automatically whenever a ``frontend/``
    source directory is detected (i.e. running from a source checkout).
    In a pip-installed deployment there is no ``frontend/`` and the watcher
    is silently skipped — the pre-built assets in ``boss/static/`` are used.
    """
    hc_home = app.state.hc_home
    enable = os.environ.get("BOSS_DAEMON", "").lower() in ("1", "true", "yes")

    task = None
    esbuild_proc: subprocess.Popen | None = None

    if enable:
        interval = float(os.environ.get("BOSS_INTERVAL", "1.0"))
        max_concurrent = int(os.environ.get("BOSS_MAX_CONCURRENT", "256"))
        budget_str = os.environ.get("BOSS_TOKEN_BUDGET")
        token_budget = int(budget_str) if budget_str else None

        task = asyncio.create_task(
            _daemon_loop(hc_home, interval, max_concurrent, token_budget)
        )

    # Auto-start frontend watcher if source checkout detected
    frontend_dir = _find_frontend_dir()
    if frontend_dir:
        esbuild_proc = _start_esbuild_watch(frontend_dir)

    yield

    # Shut down esbuild watcher
    if esbuild_proc is not None:
        logger.info("Stopping esbuild watcher (PID %d)", esbuild_proc.pid)
        esbuild_proc.terminate()
        try:
            esbuild_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            esbuild_proc.kill()

    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("Daemon loop stopped")


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
            override=Path(os.environ["BOSS_HOME"]) if "BOSS_HOME" in os.environ else None
        )

    app = FastAPI(title="Boss Boss UI", lifespan=_lifespan)
    app.state.hc_home = hc_home

    # --- Team endpoints ---

    @app.get("/teams")
    def get_teams():
        return _list_teams(hc_home)

    # --- Task endpoints (global) ---

    @app.get("/tasks")
    def get_tasks(status: str | None = None, assignee: str | None = None):
        return _list_tasks(hc_home, status=status, assignee=assignee)

    @app.get("/tasks/{task_id}/stats")
    def get_task_stats(task_id: int):
        try:
            task = _get_task(hc_home, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        stats = _get_task_stats(hc_home, task_id)

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
            "commits": task.get("commits", []),
            **stats,
        }

    @app.get("/tasks/{task_id}/diff")
    def get_task_diff(task_id: int):
        try:
            task = _get_task(hc_home, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        diff_text = _get_task_diff(hc_home, task_id)
        return {
            "task_id": task_id,
            "branch": task.get("branch", ""),
            "commits": task.get("commits", []),
            "diff": diff_text,
            "merge_base": task.get("merge_base", ""),
            "merge_tip": task.get("merge_tip", ""),
        }

    # --- Task approval endpoints ---

    @app.post("/tasks/{task_id}/approve")
    def approve_task(task_id: int):
        """Approve a task for merge.

        Sets task.approval_status to 'approved'. For manual-approval repos,
        this signals the daemon merge worker to merge on its next cycle.
        Only tasks in 'needs_merge' status can be approved.
        """
        try:
            task = _get_task(hc_home, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        if task["status"] != "needs_merge":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve task in '{task['status']}' status. Task must be in 'needs_merge' status.",
            )

        updated = _update_task(hc_home, task_id, approval_status="approved")
        _log_event(hc_home, f"{format_task_id(task_id)} approved \u2713")
        return updated

    class RejectBody(BaseModel):
        reason: str

    @app.post("/tasks/{task_id}/reject")
    def reject_task(task_id: int, body: RejectBody):
        """Reject a task with a reason.

        Sets task status to 'rejected', stores the rejection reason,
        and sends a notification to the manager for triage.
        """
        try:
            task = _get_task(hc_home, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        try:
            updated = _change_status(hc_home, task_id, "rejected")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        updated = _update_task(hc_home, task_id,
                               rejection_reason=body.reason,
                               approval_status="rejected")

        # Send notification to manager via the notify module
        from boss.notify import notify_rejection
        notify_rejection(hc_home, _first_team(hc_home), task, reason=body.reason)

        _log_event(hc_home, f"{format_task_id(task_id)} rejected \u2014 {body.reason}")
        return updated

    # --- Message endpoints (global) ---

    @app.get("/messages")
    def get_messages(since: str | None = None, between: str | None = None, type: str | None = None, limit: int | None = None):
        between_tuple = None
        if between:
            parts = [p.strip() for p in between.split(",")]
            if len(parts) == 2:
                between_tuple = (parts[0], parts[1])
        return _get_messages(hc_home, since=since, between=between_tuple, msg_type=type, limit=limit)

    class SendMessage(BaseModel):
        team: str
        recipient: str
        content: str

    @app.post("/messages")
    def post_message(msg: SendMessage):
        """Boss sends a message — restricted to team managers only."""
        boss_name = get_boss(hc_home) or "boss"
        # Verify recipient is a manager
        manager_name = get_member_by_role(hc_home, msg.team, "manager")
        if manager_name != msg.recipient:
            raise HTTPException(
                status_code=403,
                detail=f"Boss can only send messages to the team manager ({manager_name})",
            )
        _send(hc_home, msg.team, boss_name, msg.recipient, msg.content)
        return {"status": "queued"}

    # --- Agent endpoints (team-scoped) ---

    @app.get("/agents")
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
        return _get_agent_stats(hc_home, name)

    @app.get("/teams/{team}/agents/{name}/inbox")
    def get_agent_inbox(team: str, name: str):
        """Return all messages in the agent's inbox with read/unread status."""
        ad = _agent_dir(hc_home, team, name)
        if not ad.is_dir():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found in team '{team}'")

        # Get unread filenames for comparison
        unread_msgs = _read_inbox(hc_home, team, name, unread_only=True)
        unread_filenames = {m.filename for m in unread_msgs}

        # Get all messages (read + unread)
        all_msgs = _read_inbox(hc_home, team, name, unread_only=False)

        result = [
            {
                "sender": m.sender,
                "time": m.time,
                "body": m.body,
                "read": m.filename not in unread_filenames,
            }
            for m in all_msgs
        ]
        result.sort(key=lambda x: x["time"], reverse=True)
        return result[:100]

    @app.get("/teams/{team}/agents/{name}/outbox")
    def get_agent_outbox(team: str, name: str):
        """Return all messages in the agent's outbox with routed/pending status."""
        ad = _agent_dir(hc_home, team, name)
        if not ad.is_dir():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found in team '{team}'")

        pending_msgs = _read_outbox(hc_home, team, name, pending_only=True)
        pending_filenames = {m.filename for m in pending_msgs}

        all_msgs = _read_outbox(hc_home, team, name, pending_only=False)

        result = [
            {
                "recipient": m.recipient,
                "time": m.time,
                "body": m.body,
                "routed": m.filename not in pending_filenames,
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

    # --- Static files ---
    _static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (_static_dir / "index.html").read_text()

    return app
