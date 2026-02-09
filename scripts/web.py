"""FastAPI web application for the director UI.

Provides:
    GET  /            — HTML single-page app
    GET  /tasks       — list tasks (JSON)
    GET  /tasks/{id}/stats — task stats (elapsed, agent time, tokens)
    POST /tasks/{id}/approve — approve a task for merge
    POST /tasks/{id}/reject  — reject a task with reason
    GET  /messages    — get chat/event log (JSON)
    POST /messages    — director sends a message to an agent
    GET  /agents      — list agents and their states (JSON)
    GET  /agents/{name}/stats — agent stats (tasks, tokens, cost)
    GET  /agents/{name}/inbox — agent inbox messages (read/unread)
    GET  /agents/{name}/outbox — agent outbox messages (routed/pending)
    GET  /agents/{name}/logs — agent worklog sessions

When started via `scripts.run`, the daemon loop (message routing +
agent orchestration) runs as an asyncio background task inside the
FastAPI lifespan, so uvicorn --reload restarts everything together.
"""

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from scripts.task import list_tasks as _list_tasks, get_task as _get_task, get_task_diff as _get_task_diff, update_task as _update_task, change_status as _change_status, VALID_STATUSES
from scripts.chat import get_messages as _get_messages, get_task_stats as _get_task_stats, get_agent_stats as _get_agent_stats
from scripts.mailbox import send as _send, read_inbox as _read_inbox, read_outbox as _read_outbox
from scripts.bootstrap import get_member_by_role

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Daemon loop — runs as a background asyncio task inside the lifespan
# ---------------------------------------------------------------------------

async def _daemon_loop(
    root: Path,
    interval: float,
    max_concurrent: int,
    default_token_budget: int | None,
) -> None:
    """Route messages and spawn agents on a fixed interval."""
    from scripts.router import route_once
    from scripts.orchestrator import orchestrate_once, spawn_agent_subprocess

    def _spawn(r: Path, a: str) -> None:
        spawn_agent_subprocess(r, a, token_budget=default_token_budget)

    logger.info("Daemon loop started — polling every %.1fs", interval)

    while True:
        try:
            routed = route_once(root)
            if routed > 0:
                logger.info("Routed %d message(s)", routed)

            spawned = orchestrate_once(root, max_concurrent=max_concurrent, spawn_fn=_spawn)
            if spawned:
                logger.info("Spawned agents: %s", ", ".join(spawned))
        except Exception:
            logger.exception("Error during daemon cycle")
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start/stop the daemon loop when the server starts/stops."""
    root = app.state.root
    enable = os.environ.get("STANDUP_DAEMON", "").lower() in ("1", "true", "yes")

    task = None
    if enable:
        interval = float(os.environ.get("STANDUP_INTERVAL", "1.0"))
        max_concurrent = int(os.environ.get("STANDUP_MAX_CONCURRENT", "32"))
        budget_str = os.environ.get("STANDUP_TOKEN_BUDGET")
        token_budget = int(budget_str) if budget_str else None

        task = asyncio.create_task(
            _daemon_loop(root, interval, max_concurrent, token_budget)
        )
    yield
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("Daemon loop stopped")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(root: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI app for a given team root.

    When *root* is ``None`` (e.g. when called by uvicorn as a factory),
    configuration is read from environment variables set by ``scripts.run``.
    """
    if root is None:
        root = Path(os.environ["STANDUP_ROOT"])

    app = FastAPI(title="Standup Director UI", lifespan=_lifespan)
    app.state.root = root

    # --- API Endpoints ---

    @app.get("/tasks")
    def get_tasks(status: str | None = None, assignee: str | None = None, project: str | None = None):
        return _list_tasks(root, status=status, assignee=assignee, project=project)

    @app.get("/tasks/{task_id}/stats")
    def get_task_stats(task_id: int):
        from fastapi import HTTPException
        try:
            task = _get_task(root, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        stats = _get_task_stats(root, task_id)

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
        from fastapi import HTTPException
        try:
            task = _get_task(root, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        diff_text = _get_task_diff(root, task_id)
        return {
            "task_id": task_id,
            "branch": task.get("branch", ""),
            "commits": task.get("commits", []),
            "diff": diff_text,
        }

    @app.post("/tasks/{task_id}/approve")
    def approve_task(task_id: int):
        """Approve a task for merge.

        Sets task.approval_status to 'approved'. For manual-approval repos,
        this signals the daemon to merge on its next cycle.
        Only tasks in 'needs_merge' status can be approved.
        """
        from fastapi import HTTPException
        try:
            task = _get_task(root, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        if task["status"] != "needs_merge":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve task in '{task['status']}' status. Task must be in 'needs_merge' status.",
            )

        updated = _update_task(root, task_id, approval_status="approved")

        from scripts.chat import log_event
        log_event(root, f"T{task_id:04d} approved for merge")

        return updated

    class RejectBody(BaseModel):
        reason: str

    @app.post("/tasks/{task_id}/reject")
    def reject_task(task_id: int, body: RejectBody):
        """Reject a task with a reason.

        Sets task status to 'rejected', stores the rejection reason,
        and sends a notification to the EM (manager) for triage.
        """
        from fastapi import HTTPException
        try:
            task = _get_task(root, task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        # Transition status to rejected
        try:
            updated = _change_status(root, task_id, "rejected")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Store rejection reason and approval status
        updated = _update_task(root, task_id,
                               rejection_reason=body.reason,
                               approval_status="rejected")

        # Notify the EM (manager) so they can triage
        director_name = get_member_by_role(root, "director") or "director"
        manager_name = get_member_by_role(root, "manager") or "manager"
        title = task.get("title", f"T{task_id:04d}")
        assignee = task.get("assignee", "unknown")
        _send(
            root,
            director_name,
            manager_name,
            f"Task T{task_id:04d} ({title}) by {assignee} was rejected. Reason: {body.reason}",
        )

        from scripts.chat import log_event
        log_event(root, f"T{task_id:04d} rejected: {body.reason}")

        return updated

    @app.get("/messages")
    def get_messages(since: str | None = None, between: str | None = None, type: str | None = None, limit: int | None = None):
        between_tuple = None
        if between:
            parts = [p.strip() for p in between.split(",")]
            if len(parts) == 2:
                between_tuple = (parts[0], parts[1])
        return _get_messages(root, since=since, between=between_tuple, msg_type=type, limit=limit)

    class SendMessage(BaseModel):
        recipient: str
        content: str

    @app.post("/messages")
    def post_message(msg: SendMessage):
        # Write to director's outbox — the router will deliver + log
        director_name = get_member_by_role(root, "director") or "director"
        _send(root, director_name, msg.recipient, msg.content)
        return {"status": "queued"}

    @app.get("/agents")
    def get_agents():
        """List AI agents (excludes director, who is a human)."""
        team_dir = root / ".standup" / "team"
        agents = []
        if team_dir.is_dir():
            for d in sorted(team_dir.iterdir()):
                state_file = d / "state.yaml"
                if not d.is_dir() or not state_file.exists():
                    continue
                state = yaml.safe_load(state_file.read_text()) or {}
                if state.get("role") == "director":
                    continue
                inbox_new = d / "inbox" / "new"
                unread = len(list(inbox_new.iterdir())) if inbox_new.is_dir() else 0
                agents.append({
                    "name": d.name,
                    "role": state.get("role", "worker"),
                    "pid": state.get("pid"),
                    "unread_inbox": unread,
                })
        return agents

    @app.get("/agents/{name}/stats")
    def get_agent_stats(name: str):
        """Get aggregated stats for a specific agent."""
        return _get_agent_stats(root, name)

    @app.get("/agents/{name}/inbox")
    def get_agent_inbox(name: str):
        """Return all messages in the agent's inbox with read/unread status."""
        from fastapi import HTTPException
        agent_dir = root / ".standup" / "team" / name
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        # Get unread filenames for comparison
        unread_msgs = _read_inbox(root, name, unread_only=True)
        unread_filenames = {m.filename for m in unread_msgs}

        # Get all messages (read + unread)
        all_msgs = _read_inbox(root, name, unread_only=False)

        result = [
            {
                "sender": m.sender,
                "time": m.time,
                "body": m.body,
                "read": m.filename not in unread_filenames,
            }
            for m in all_msgs
        ]
        # Sort newest first, limit to 100
        result.sort(key=lambda x: x["time"], reverse=True)
        return result[:100]

    @app.get("/agents/{name}/outbox")
    def get_agent_outbox(name: str):
        """Return all messages in the agent's outbox with routed/pending status."""
        from fastapi import HTTPException
        agent_dir = root / ".standup" / "team" / name
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        # Get pending filenames for comparison
        pending_msgs = _read_outbox(root, name, pending_only=True)
        pending_filenames = {m.filename for m in pending_msgs}

        # Get all messages (routed + pending)
        all_msgs = _read_outbox(root, name, pending_only=False)

        result = [
            {
                "recipient": m.recipient,
                "time": m.time,
                "body": m.body,
                "routed": m.filename not in pending_filenames,
            }
            for m in all_msgs
        ]
        # Sort newest first, limit to 100
        result.sort(key=lambda x: x["time"], reverse=True)
        return result[:100]

    @app.get("/agents/{name}/logs")
    def get_agent_logs(name: str):
        """Return the agent's worklog entries."""
        from fastapi import HTTPException
        agent_dir = root / ".standup" / "team" / name
        if not agent_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        logs_dir = agent_dir / "logs"
        sessions = []
        if logs_dir.is_dir():
            # Collect worklog files and sort numerically
            worklog_files = [f for f in logs_dir.iterdir() if f.name.endswith(".worklog.md")]
            worklog_files.sort(key=lambda f: int(f.name.split(".")[0]) if f.name.split(".")[0].isdigit() else 0)

            for f in worklog_files:
                content = f.read_text()
                # Truncate to last 50KB if very large
                if len(content) > 50 * 1024:
                    content = content[-(50 * 1024):]
                sessions.append({
                    "filename": f.name,
                    "content": content,
                })

        # Return in reverse order (latest session first)
        sessions.reverse()
        return {"sessions": sessions}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML_PAGE

    return app


# --- Inline HTML/JS frontend ---

HTML_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Standup — Director Dashboard</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-sans/400.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-sans/500.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-sans/600.css">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  /* Theme variables — light mode defaults */
  :root {
    --bg-body: #ffffff;
    --bg-surface: #f8f8f8;
    --bg-sidebar: #f3f3f5;
    --bg-hover: rgba(0,0,0,0.03);
    --bg-active: rgba(0,0,0,0.06);
    --bg-input: #ffffff;
    --text-primary: #1a1a1a;
    --text-secondary: #6b6b6b;
    --text-muted: #999999;
    --text-faint: #bbbbbb;
    --text-heading: #111111;
    --border-default: rgba(0,0,0,0.08);
    --border-subtle: rgba(0,0,0,0.04);
    --border-input: rgba(0,0,0,0.15);
    --border-focus: rgba(0,0,0,0.3);
    --accent-blue: #60a5fa;
    --accent-green: #22c55e;
    --accent-green-glow: rgba(34,197,94,0.4);
    --btn-bg: #1a1a1a;
    --btn-text: #ffffff;
    --btn-hover: #333333;
    --scrollbar-thumb: rgba(0,0,0,0.12);
    --scrollbar-hover: rgba(0,0,0,0.2);
    --dot-offline: #cccccc;
    --diff-add-bg: rgba(34,197,94,0.08);
    --diff-add-text: #16a34a;
    --diff-del-bg: rgba(248,113,113,0.08);
    --diff-del-text: #dc2626;
    --diff-ctx-text: #888888;
    --diff-hunk-bg: rgba(96,165,250,0.06);
    --backdrop-bg: rgba(0,0,0,0.3);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg-body: #0a0a0b;
      --bg-surface: #111113;
      --bg-sidebar: #0d0d0f;
      --bg-hover: rgba(255,255,255,0.02);
      --bg-active: rgba(255,255,255,0.08);
      --bg-input: #111113;
      --text-primary: #ededed;
      --text-secondary: #a1a1a1;
      --text-muted: #555555;
      --text-faint: #444444;
      --text-heading: #fafafa;
      --border-default: rgba(255,255,255,0.08);
      --border-subtle: rgba(255,255,255,0.04);
      --border-input: rgba(255,255,255,0.1);
      --border-focus: rgba(255,255,255,0.25);
      --accent-blue: #60a5fa;
      --accent-green: #22c55e;
      --accent-green-glow: rgba(34,197,94,0.4);
      --btn-bg: #fafafa;
      --btn-text: #0a0a0b;
      --btn-hover: #d4d4d4;
      --scrollbar-thumb: rgba(255,255,255,0.1);
      --scrollbar-hover: rgba(255,255,255,0.18);
      --dot-offline: #333333;
      --diff-add-bg: rgba(34,197,94,0.08);
      --diff-add-text: #6ee7b7;
      --diff-del-bg: rgba(248,113,113,0.08);
      --diff-del-text: #fca5a5;
      --diff-ctx-text: #666666;
      --diff-hunk-bg: rgba(96,165,250,0.06);
      --backdrop-bg: rgba(0,0,0,0.5);
    }
  }
  body { font-family: 'Geist Sans', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-body); color: var(--text-primary); font-size: 14px; line-height: 1.5; letter-spacing: -0.01em; display: flex; flex-direction: row; -webkit-font-smoothing: antialiased; }
  .main { flex: 1; display: flex; flex-direction: column; min-height: 0; height: 100vh; }
  .header { background: var(--bg-surface); padding: 14px 24px; border-bottom: 1px solid var(--border-default); display: flex; align-items: center; gap: 16px; flex-shrink: 0; }

  /* Sidebar */
  .sidebar { width: 280px; min-width: 280px; height: 100vh; position: sticky; top: 0; background: var(--bg-sidebar); border-right: 1px solid var(--border-subtle); display: flex; flex-direction: column; overflow: hidden; }
  .sidebar-widget { padding: 16px; border-bottom: 1px solid var(--border-subtle); }
  .sidebar-widget:last-child { border-bottom: none; flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .sidebar-widget-header { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 8px; }
  .sidebar-stat-row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary); margin-bottom: 4px; }
  .sidebar-stat-row .stat-value { color: var(--text-primary); font-weight: 500; font-variant-numeric: tabular-nums; }
  .sidebar-agent-list { display: flex; flex-direction: column; gap: 0; max-height: calc(28px * 6); overflow-y: auto; }
  .sidebar-agent-row { display: flex; align-items: center; gap: 8px; height: 28px; min-height: 28px; font-size: 12px; padding: 0 2px; }
  .sidebar-agent-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .sidebar-agent-dot.dot-working { background: #22c55e; box-shadow: 0 0 6px rgba(34,197,94,0.4); }
  .sidebar-agent-dot.dot-queued { background: #60a5fa; }
  .sidebar-agent-dot.dot-offline { background: var(--dot-offline); }
  .sidebar-agent-name { color: var(--text-primary); font-weight: 500; white-space: nowrap; }
  .sidebar-agent-activity { color: var(--text-muted); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sidebar-agent-cost { color: var(--text-faint); font-size: 11px; font-variant-numeric: tabular-nums; flex-shrink: 0; }
  .sidebar-task-list { display: flex; flex-direction: column; gap: 0; flex: 1; min-height: 0; overflow-y: auto; }
  .sidebar-task-row { display: flex; align-items: center; gap: 8px; height: 28px; min-height: 28px; font-size: 12px; padding: 0 2px; }
  .sidebar-task-id { color: var(--text-muted); font-variant-numeric: tabular-nums; flex-shrink: 0; min-width: 42px; }
  .sidebar-task-title { color: var(--text-secondary); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sidebar-task-badge { flex-shrink: 0; }
  .sidebar-task-badge .badge { font-size: 10px; padding: 2px 6px; }
  .sidebar-task-assignee { color: var(--text-faint); font-size: 11px; flex-shrink: 0; }
  .sidebar-see-all { color: var(--accent-blue); font-size: 11px; cursor: pointer; text-decoration: none; margin-top: 6px; display: inline-block; }
  .sidebar-see-all:hover { text-decoration: underline; }
  @media (max-width: 900px) { .sidebar { display: none; } }
  .header h1 { font-size: 16px; font-weight: 600; letter-spacing: -0.02em; color: var(--text-heading); }
  .tabs { display: flex; gap: 2px; margin-left: 32px; }
  .tab { padding: 7px 14px; cursor: pointer; border-radius: 6px; background: transparent; border: none; color: var(--text-muted); font-family: inherit; font-size: 13px; font-weight: 500; transition: color 0.15s, background 0.15s; }
  .tab:hover { color: var(--text-secondary); background: var(--border-subtle); }
  .tab.active { background: var(--bg-active); color: var(--text-heading); }
  .content { max-width: 1000px; width: 100%; margin: 0 auto; padding: 24px; flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .panel { display: none; }
  .panel.active { display: flex; flex-direction: column; flex: 1; min-height: 0; }

  /* Tasks */
  .task-list { display: flex; flex-direction: column; gap: 2px; }
  .task-row { cursor: pointer; border-radius: 8px; border: 1px solid transparent; transition: border-color 0.15s, background 0.15s; }
  .task-row:hover { background: var(--bg-hover); }
  .task-row.expanded { border-color: var(--border-default); background: var(--bg-hover); }
  .task-summary { display: grid; grid-template-columns: 60px 1fr auto auto auto; gap: 12px; align-items: center; padding: 10px 14px; font-size: 13px; }
  .task-summary > span { white-space: nowrap; }
  .task-id { color: var(--text-muted); font-variant-numeric: tabular-nums; font-size: 12px; }
  .task-title { color: var(--text-primary); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .task-assignee { color: var(--text-secondary); font-size: 12px; min-width: 70px; text-align: right; }
  .task-priority { color: var(--text-secondary); font-size: 12px; min-width: 60px; text-align: right; }
  .task-detail { max-height: 0; overflow: hidden; transition: max-height 0.25s ease-out, padding 0.25s ease-out; padding: 0 14px; }
  .task-row.expanded .task-detail { max-height: 400px; padding: 0 14px 14px; transition: max-height 0.3s ease-in, padding 0.2s ease-in; }
  .task-detail-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 10px; }
  .task-detail-item { background: var(--bg-hover); border-radius: 8px; padding: 10px 14px; }
  .task-detail-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 4px; }
  .task-detail-value { font-size: 13px; color: var(--text-primary); font-variant-numeric: tabular-nums; }
  .task-desc { color: var(--text-secondary); font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; padding: 10px 14px; background: var(--bg-hover); border-radius: 8px; }
  .task-dates { display: flex; gap: 24px; margin-top: 10px; font-size: 11px; color: var(--text-muted); align-items: center; }
  .diff-btn { margin-left: auto; padding: 4px 10px; border-radius: 5px; border: 1px solid rgba(96,165,250,0.3); background: rgba(96,165,250,0.08); color: #60a5fa; font-family: inherit; font-size: 11px; cursor: pointer; transition: background 0.15s, border-color 0.15s; }
  .diff-btn:hover { background: rgba(96,165,250,0.16); border-color: rgba(96,165,250,0.5); }
  .task-vcs-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .task-branch { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; background: var(--bg-active); color: var(--text-secondary); padding: 3px 10px; border-radius: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 320px; }
  .task-commit { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 11px; background: rgba(96,165,250,0.1); color: #60a5fa; padding: 2px 8px; border-radius: 4px; cursor: pointer; border: none; transition: background 0.15s; }
  .task-commit:hover { background: rgba(96,165,250,0.2); }
  .btn-diff { padding: 4px 12px; border-radius: 5px; border: 1px solid rgba(96,165,250,0.3); background: rgba(96,165,250,0.08); color: #60a5fa; font-family: inherit; font-size: 11px; cursor: pointer; transition: background 0.15s, border-color 0.15s; margin-left: auto; }
  .btn-diff:hover { background: rgba(96,165,250,0.16); border-color: rgba(96,165,250,0.5); }
  .badge { padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 500; letter-spacing: 0.01em; }
  .badge-open { background: rgba(52,211,153,0.12); color: #34d399; }
  .badge-in_progress { background: rgba(251,191,36,0.12); color: #fbbf24; }
  .badge-review { background: rgba(96,165,250,0.12); color: #60a5fa; }
  .badge-done { background: var(--bg-active); color: var(--text-muted); }

  /* Chat */
  .chat-log { flex: 1; min-height: 0; overflow-y: auto; background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 12px; margin-bottom: 12px; }
  .msg { display: flex; gap: 12px; padding: 10px 12px; border-radius: 8px; margin-bottom: 2px; transition: background 0.15s; }
  .msg:hover { background: var(--bg-hover); }
  .msg-avatar { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; color: #fff; flex-shrink: 0; margin-top: 2px; }
  .msg-body { flex: 1; min-width: 0; }
  .msg-header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 3px; }
  .msg-sender { font-weight: 600; color: var(--text-primary); font-size: 13px; }
  .msg-recipient { color: var(--text-muted); font-size: 12px; font-weight: 400; }
  .msg-time { color: var(--text-faint); font-size: 11px; font-variant-numeric: tabular-nums; margin-left: auto; flex-shrink: 0; }
  .msg-content { color: var(--text-secondary); font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
  .msg-event { display: flex; align-items: center; justify-content: center; gap: 10px; padding: 6px 12px; margin: 4px 0; }
  .msg-event-line { flex: 1; height: 1px; background: var(--border-subtle); }
  .msg-event-text { color: var(--text-faint); font-size: 11px; white-space: nowrap; }
  .msg-event-time { color: var(--text-faint); font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; flex-shrink: 0; }
  .chat-input { display: flex; gap: 8px; flex-shrink: 0; align-items: flex-end; }
  .chat-input textarea { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--text-primary); font-family: inherit; font-size: 13px; outline: none; transition: border-color 0.15s; resize: none; min-height: 3.6em; max-height: 12em; overflow-y: auto; line-height: 1.4; }
  .chat-input textarea:focus { border-color: var(--border-focus); }
  .chat-input textarea::placeholder { color: var(--text-faint); }
  .chat-input select { padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--text-primary); font-family: inherit; font-size: 13px; outline: none; cursor: pointer; }
  .chat-input button { padding: 10px 20px; border-radius: 8px; border: none; background: var(--btn-bg); color: var(--btn-text); font-family: inherit; font-size: 13px; font-weight: 500; cursor: pointer; transition: background 0.15s; }
  .chat-input button:hover { background: var(--btn-hover); }

  /* Agents */
  .agent-card { background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 16px 20px; margin-bottom: 8px; display: flex; align-items: center; gap: 16px; transition: border-color 0.15s; }
  .agent-card:hover { border-color: var(--border-default); }
  .agent-name { font-weight: 600; min-width: 120px; color: var(--text-primary); font-size: 13px; }
  .agent-status { font-size: 12px; color: var(--text-muted); }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; margin-right: 8px; }
  .agent-card { cursor: pointer; }
  .dot-active { background: #22c55e; box-shadow: 0 0 6px rgba(34,197,94,0.4); }
  .dot-idle { background: var(--dot-offline); }
  .agent-stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .agent-stat { background: var(--bg-hover); border-radius: 8px; padding: 10px 14px; }
  .agent-stat-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 4px; }
  .agent-stat-value { font-size: 14px; font-weight: 600; color: var(--text-primary); font-variant-numeric: tabular-nums; }

  /* Filters (shared) */
  .chat-filters, .task-filters { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; flex-shrink: 0; }
  .chat-filters select, .task-filters select { padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--text-primary); font-family: inherit; font-size: 12px; outline: none; cursor: pointer; transition: border-color 0.15s; }
  .chat-filters select:focus, .task-filters select:focus { border-color: var(--border-focus); }
  .chat-filters label, .task-filters label { display: flex; align-items: center; gap: 6px; color: var(--text-muted); font-size: 12px; cursor: pointer; user-select: none; transition: color 0.15s; }
  .chat-filters label:hover, .task-filters label:hover { color: var(--text-secondary); }
  .chat-filters input[type="checkbox"], .task-filters input[type="checkbox"] { appearance: none; width: 14px; height: 14px; border: 1px solid var(--border-input); border-radius: 3px; background: transparent; cursor: pointer; position: relative; transition: background 0.15s, border-color 0.15s; }
  .chat-filters input[type="checkbox"]:checked, .task-filters input[type="checkbox"]:checked { background: var(--btn-bg); border-color: var(--btn-bg); }
  .chat-filters input[type="checkbox"]:checked::after, .task-filters input[type="checkbox"]:checked::after { content: ''; position: absolute; top: 1px; left: 4px; width: 4px; height: 8px; border: solid var(--btn-text); border-width: 0 1.5px 1.5px 0; transform: rotate(45deg); }
  .filter-label { color: var(--text-faint); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }

  /* Diff Panel */
  .diff-panel { position: fixed; top: 0; right: 0; width: 55vw; height: 100vh; background: var(--bg-sidebar); border-left: 1px solid var(--border-default); z-index: 200; display: flex; flex-direction: column; transform: translateX(100%); transition: transform 0.25s ease; }
  .diff-panel.open { transform: translateX(0); }
  .diff-backdrop { position: fixed; inset: 0; background: var(--backdrop-bg); z-index: 199; opacity: 0; pointer-events: none; transition: opacity 0.25s ease; }
  .diff-backdrop.open { opacity: 1; pointer-events: auto; }
  .diff-panel-header { padding: 16px 20px 12px; border-bottom: 1px solid var(--border-subtle); flex-shrink: 0; }
  .diff-panel-title { font-size: 15px; font-weight: 600; color: var(--text-heading); margin-bottom: 4px; }
  .diff-panel-branch { font-size: 12px; color: #60a5fa; font-family: 'SF Mono', 'Fira Code', monospace; margin-bottom: 8px; }
  .diff-panel-commits { display: flex; flex-wrap: wrap; gap: 6px; }
  .diff-panel-commit { font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; background: var(--bg-active); color: var(--text-secondary); padding: 2px 8px; border-radius: 4px; }
  .diff-panel-close { position: absolute; top: 14px; right: 16px; background: none; border: none; color: var(--text-muted); font-size: 22px; cursor: pointer; line-height: 1; padding: 4px; }
  .diff-panel-close:hover { color: var(--text-primary); }
  .diff-panel-tabs { display: flex; gap: 2px; padding: 8px 20px; border-bottom: 1px solid var(--border-subtle); flex-shrink: 0; }
  .diff-tab { padding: 6px 12px; cursor: pointer; border-radius: 6px; background: transparent; border: none; color: var(--text-muted); font-family: inherit; font-size: 12px; font-weight: 500; transition: color 0.15s, background 0.15s; }
  .diff-tab:hover { color: var(--text-secondary); background: var(--border-subtle); }
  .diff-tab.active { background: var(--bg-active); color: var(--text-heading); }
  .diff-panel-body { flex: 1; overflow-y: auto; padding: 16px 20px; }
  .diff-file-section { margin-bottom: 20px; }
  .diff-file-header { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: var(--bg-hover); border-radius: 6px; margin-bottom: 4px; }
  .diff-file-name { font-size: 12px; font-family: 'SF Mono', 'Fira Code', monospace; color: var(--text-primary); font-weight: 500; }
  .diff-file-stats { margin-left: auto; display: flex; gap: 6px; font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; }
  .diff-file-add { color: #34d399; }
  .diff-file-del { color: #f87171; }
  .diff-hunk-header { font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; color: var(--text-muted); padding: 4px 12px; background: var(--diff-hunk-bg); margin: 2px 0; border-radius: 3px; }
  .diff-line { display: flex; font-size: 12px; font-family: 'SF Mono', 'Fira Code', monospace; line-height: 1.6; }
  .diff-line-gutter { width: 48px; min-width: 48px; text-align: right; padding-right: 8px; color: var(--text-faint); user-select: none; flex-shrink: 0; }
  .diff-line-content { flex: 1; padding: 0 8px; white-space: pre-wrap; word-break: break-all; }
  .diff-line.add { background: var(--diff-add-bg); }
  .diff-line.add .diff-line-content { color: var(--diff-add-text); }
  .diff-line.del { background: var(--diff-del-bg); }
  .diff-line.del .diff-line-content { color: var(--diff-del-text); }
  .diff-line.ctx .diff-line-content { color: var(--diff-ctx-text); }
  .diff-file-list { display: flex; flex-direction: column; gap: 2px; }
  .diff-file-list-item { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 6px; cursor: pointer; transition: background 0.15s; }
  .diff-file-list-item:hover { background: var(--border-subtle); }
  .diff-file-list-name { font-size: 13px; font-family: 'SF Mono', 'Fira Code', monospace; color: var(--text-primary); }
  .diff-empty { color: var(--text-muted); font-size: 13px; padding: 24px; text-align: center; }
  @media (max-width: 900px) { .diff-panel { width: 100vw; } }

  /* Agent Panel */
  .agent-msg { padding: 10px 12px; border-bottom: 1px solid var(--border-subtle); }
  .agent-msg.unread { border-left: 2px solid #60a5fa; }
  .agent-msg.pending { border-left: 2px solid #fbbf24; }
  .agent-msg-header { display: flex; justify-content: space-between; margin-bottom: 4px; }
  .agent-msg-sender { font-weight: 500; font-size: 12px; color: var(--text-primary); }
  .agent-msg-time { font-size: 11px; color: var(--text-faint); }
  .agent-msg-body { font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
  .agent-msg-body.collapsed { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; cursor: pointer; }
  .agent-log-session { margin-bottom: 12px; }
  .agent-log-header { cursor: pointer; padding: 8px 12px; background: var(--bg-hover); border-radius: 6px; font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 8px; }
  .agent-log-header:hover { background: var(--bg-active); }
  .agent-log-arrow { font-size: 10px; color: var(--text-muted); transition: transform 0.15s; }
  .agent-log-arrow.expanded { transform: rotate(90deg); }
  .agent-log-content { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 11px; line-height: 1.5; color: var(--text-secondary); padding: 8px 12px; white-space: pre-wrap; max-height: 400px; overflow-y: auto; display: none; }
  .agent-log-content.expanded { display: block; }

  /* Mute toggle */
  .mute-toggle { background: transparent; border: none; color: var(--text-muted); cursor: pointer; font-size: 14px; padding: 4px 8px; transition: color 0.15s; margin-left: auto; display: flex; align-items: center; justify-content: center; line-height: 1; }
  .mute-toggle:hover { color: var(--text-secondary); }

  /* Mic button */
  .chat-input .mic-btn { padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--text-secondary); font-size: 16px; cursor: pointer; transition: background 0.15s, color 0.15s, border-color 0.15s; display: flex; align-items: center; justify-content: center; line-height: 1; }
  .chat-input .mic-btn:hover { border-color: var(--border-focus); color: var(--text-primary); }
  .chat-input .mic-btn.recording { background: rgba(239,68,68,0.15); border-color: rgba(239,68,68,0.4); color: #ef4444; animation: mic-pulse 1.5s ease-in-out infinite; }
  @keyframes mic-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(239,68,68,0.3); } 50% { box-shadow: 0 0 0 6px rgba(239,68,68,0); } }
  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-hover); }
</style>
</head>
<body>
<div class="sidebar" id="sidebar">
  <div class="sidebar-widget" id="sidebarStatus">
    <div class="sidebar-widget-header">Team Status</div>
    <div id="sidebarStatusContent"><span style="color:var(--text-faint);font-size:12px">Loading...</span></div>
  </div>
  <div class="sidebar-widget" id="sidebarAgents">
    <div class="sidebar-widget-header">Agents</div>
    <div class="sidebar-agent-list" id="sidebarAgentList"></div>
    <a class="sidebar-see-all" onclick="switchTab('agents')">See All &rarr;</a>
  </div>
  <div class="sidebar-widget" id="sidebarTasks">
    <div class="sidebar-widget-header">Recent Tasks</div>
    <div class="sidebar-task-list" id="sidebarTaskList"></div>
    <a class="sidebar-see-all" onclick="switchTab('tasks')">See All &rarr;</a>
  </div>
</div>
<div class="main">
<div class="header">
  <h1>Standup</h1>
  <div class="tabs">
    <button class="tab active" data-tab="chat" onclick="switchTab('chat')">Mission Control</button>
    <button class="tab" data-tab="tasks" onclick="switchTab('tasks')">Tasks</button>
    <button class="tab" data-tab="agents" onclick="switchTab('agents')">Agents</button>
  </div>
  <button class="mute-toggle" id="muteToggle" onclick="toggleMute()" title="Toggle notification sounds"></button>
</div>
<div class="content">
  <div id="chat" class="panel active">
    <div class="chat-filters">
      <span class="filter-label">From</span>
      <select id="chatFilterFrom" onchange="loadChat()">
        <option value="">Anyone</option>
      </select>
      <span class="filter-label">To</span>
      <select id="chatFilterTo" onchange="loadChat()">
        <option value="">Anyone</option>
      </select>
      <label><input type="checkbox" id="chatBetween" onchange="loadChat()"> Between</label>
      <label><input type="checkbox" id="chatShowEvents" checked onchange="loadChat()"> System events</label>
    </div>
    <div class="chat-log" id="chatLog"></div>
    <div class="chat-input">
      <select id="recipient"></select>
      <textarea id="msgInput" placeholder="Send a message..." rows="3" onkeydown="handleChatKeydown(event)" oninput="autoResizeTextarea(this)"></textarea>
      <button class="mic-btn" id="micBtn" onclick="toggleMic()" title="Voice input" aria-label="Voice input" style="display:none"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5.5" y="1" width="5" height="9" rx="2.5"/><path d="M3 7.5a5 5 0 0 0 10 0"/><line x1="8" y1="12.5" x2="8" y2="15"/><line x1="5.5" y1="15" x2="10.5" y2="15"/></svg></button>
      <button onclick="sendMsg()">Send</button>
    </div>
  </div>
  <div id="tasks" class="panel">
    <div class="task-filters">
      <span class="filter-label">Status</span>
      <select id="taskFilterStatus" onchange="loadTasks()">
        <option value="">All</option>
        <option value="open">Open</option>
        <option value="in_progress">In Progress</option>
        <option value="review">Review</option>
        <option value="done">Done</option>
      </select>
      <span class="filter-label">Priority</span>
      <select id="taskFilterPriority" onchange="loadTasks()">
        <option value="">All</option>
        <option value="low">Low</option>
        <option value="medium">Medium</option>
        <option value="high">High</option>
        <option value="critical">Critical</option>
      </select>
      <span class="filter-label">Assignee</span>
      <select id="taskFilterAssignee" onchange="loadTasks()">
        <option value="">All</option>
      </select>
    </div>
    <div id="taskTable"></div>
  </div>
  <div id="agents" class="panel"></div>
</div>
</div>
<div id="diffPanel" class="diff-panel">
  <div class="diff-panel-header">
    <div class="diff-panel-title" id="diffPanelTitle"></div>
    <div class="diff-panel-branch" id="diffPanelBranch"></div>
    <div class="diff-panel-commits" id="diffPanelCommits"></div>
    <button class="diff-panel-close" onclick="closePanel()">&times;</button>
  </div>
  <div class="diff-panel-tabs">
    <button class="diff-tab active" data-dtab="files" onclick="switchDiffTab('files')">Files Changed</button>
    <button class="diff-tab" data-dtab="diff" onclick="switchDiffTab('diff')">Full Diff</button>
  </div>
  <div class="diff-panel-body" id="diffPanelBody"></div>
</div>
<div id="diffBackdrop" class="diff-backdrop" onclick="closePanel()"></div>
<script>
// --- Mute toggle ---
let _isMuted = localStorage.getItem('standup-muted') === 'true';
function toggleMute() {
  _isMuted = !_isMuted;
  localStorage.setItem('standup-muted', _isMuted ? 'true' : 'false');
  _updateMuteBtn();
}
function _updateMuteBtn() {
  const btn = document.getElementById('muteToggle');
  if (!btn) return;
  if (_isMuted) {
    btn.innerHTML = "<svg width='16' height='16' viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><polygon points='2,6 2,10 5,10 9,13 9,3 5,6'/><line x1='12' y1='5' x2='15' y2='11'/><line x1='15' y1='5' x2='12' y2='11'/></svg>";
    btn.title = 'Unmute notifications';
  } else {
    btn.innerHTML = "<svg width='16' height='16' viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><polygon points='2,6 2,10 5,10 9,13 9,3 5,6'/><path d='M11.5 5.5a3.5 3.5 0 0 1 0 5'/></svg>";
    btn.title = 'Mute notifications';
  }
}

// --- Notification sounds ---
let _audioCtx = null;
let _lastMsgTimestamp = '';
let _prevTaskStatuses = {};
let _msgSendCooldown = false;

function _getAudioCtx() {
  if (!_audioCtx) { try { _audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch(e) { return null; } }
  return _audioCtx;
}

function playMsgSound() {
  if (_isMuted) return;
  const ctx = _getAudioCtx(); if (!ctx) return;
  const now = ctx.currentTime;
  const g = ctx.createGain(); g.connect(ctx.destination); g.gain.setValueAtTime(0.15, now);
  g.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
  const o1 = ctx.createOscillator(); o1.type = 'sine'; o1.frequency.value = 800; o1.connect(g); o1.start(now); o1.stop(now + 0.08);
  const o2 = ctx.createOscillator(); o2.type = 'sine'; o2.frequency.value = 1000; o2.connect(g); o2.start(now + 0.1); o2.stop(now + 0.18);
}

function playTaskSound() {
  if (_isMuted) return;
  const ctx = _getAudioCtx(); if (!ctx) return;
  const now = ctx.currentTime;
  [523.25, 659.25, 783.99].forEach((freq, i) => {
    const t = now + i * 0.15;
    const g = ctx.createGain(); g.connect(ctx.destination); g.gain.setValueAtTime(0.12, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.15);
    const o = ctx.createOscillator(); o.type = 'sine'; o.frequency.value = freq; o.connect(g); o.start(t); o.stop(t + 0.15);
  });
}

function cap(s){return s.charAt(0).toUpperCase()+s.slice(1);}
function fmtStatus(s){return s.split('_').map(w=>cap(w)).join(' ');}
function fmtTime(iso){const d=new Date(iso);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});}
function fmtTimestamp(iso){
  if(!iso) return '\u2014';
  const d=new Date(iso), now=new Date(), diff=now-d, sec=Math.floor(diff/1000), min=Math.floor(sec/60), hr=Math.floor(min/60);
  if(sec<60) return 'Just now';
  if(min<60) return min+' min ago';
  const time=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false});
  if(hr<24) return time;
  const mon=d.toLocaleDateString([],{month:'short',day:'numeric'});
  return mon+', '+time;
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
const _avatarColors=['#e11d48','#7c3aed','#2563eb','#0891b2','#059669','#d97706','#dc2626','#4f46e5'];
function avatarColor(name){let h=0;for(let i=0;i<name.length;i++)h=name.charCodeAt(i)+((h<<5)-h);return _avatarColors[Math.abs(h)%_avatarColors.length];}
function avatarInitial(name){return name.charAt(0).toUpperCase();}
function switchTab(name, pushHash) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  document.querySelector('.tab[data-tab="' + name + '"]').classList.add('active');
  if (pushHash !== false) window.location.hash = name;
  if (name === 'tasks') loadTasks();
  if (name === 'chat') loadChat();
  if (name === 'agents') loadAgents();
}

function fmtElapsed(sec) {
  if (sec == null) return '\u2014';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? m + 'm ' + s + 's' : s + 's';
}
function fmtTokens(tin, tout) {
  if (tin == null && tout == null) return '\u2014';
  return Number(tin || 0).toLocaleString() + ' / ' + Number(tout || 0).toLocaleString();
}
function fmtCost(usd) {
  if (usd == null) return '\u2014';
  return '$' + Number(usd).toFixed(2);
}

let _expandedTasks = new Set();
let _taskStatsCache = {};

function _taskRowHtml(t) {
  const expanded = _expandedTasks.has(t.id);
  const s = _taskStatsCache[t.id];
  const tid = 'T' + String(t.id).padStart(4,'0');
  return `<div class="task-row${expanded ? ' expanded' : ''}" data-id="${t.id}" onclick="toggleTask(${t.id})">
    <div class="task-summary">
      <span class="task-id">${tid}</span>
      <span class="task-title">${esc(t.title)}</span>
      <span><span class="badge badge-${t.status}">${fmtStatus(t.status)}</span></span>
      <span class="task-assignee">${t.assignee ? cap(t.assignee) : '\u2014'}</span>
      <span class="task-priority">${cap(t.priority)}</span>
    </div>
    <div class="task-detail" onclick="event.stopPropagation()">
      <div class="task-detail-grid">
        <div class="task-detail-item"><div class="task-detail-label">Project</div><div class="task-detail-value">${t.project || '\u2014'}</div></div>
        <div class="task-detail-item"><div class="task-detail-label">Reviewer</div><div class="task-detail-value">${t.reviewer ? cap(t.reviewer) : '\u2014'}</div></div>
        <div class="task-detail-item"><div class="task-detail-label">Time</div><div class="task-detail-value">${s ? fmtElapsed(s.elapsed_seconds) : '\u2014'}</div></div>
        <div class="task-detail-item"><div class="task-detail-label">Tokens (in/out)</div><div class="task-detail-value">${s ? fmtTokens(s.total_tokens_in, s.total_tokens_out) : '\u2014'}</div></div>
        <div class="task-detail-item"><div class="task-detail-label">Cost</div><div class="task-detail-value">${s ? fmtCost(s.total_cost_usd) : '\u2014'}</div></div>
        <div class="task-detail-item"><div class="task-detail-label">Status</div><div class="task-detail-value">${fmtStatus(t.status)}</div></div>
        <div class="task-detail-item"><div class="task-detail-label">Assignee</div><div class="task-detail-value">${t.assignee ? cap(t.assignee) : '\u2014'}</div></div>
        <div class="task-detail-item"><div class="task-detail-label">Priority</div><div class="task-detail-value">${cap(t.priority)}</div></div>
      </div>
      ${s && s.branch ? '<div class="task-vcs-row" onclick="event.stopPropagation()"><span class="task-branch" title="' + esc(s.branch) + '">' + esc(s.branch) + '</span>' + (s.commits && s.commits.length ? s.commits.map(c => '<button class="task-commit" onclick="event.stopPropagation();openDiffPanel(' + t.id + ')" title="' + esc(String(c)) + '">' + esc(String(c).substring(0,7)) + '</button>').join('') : '') + '<button class="btn-diff" onclick="event.stopPropagation();openDiffPanel(' + t.id + ')">View Changes</button></div>' : ''}
      ${t.description ? '<div class="task-desc">' + esc(t.description) + '</div>' : ''}
      <div class="task-dates">
        <span>Created: <span class="ts" data-ts="${t.created_at || ''}">${fmtTimestamp(t.created_at)}</span></span>
        <span>Completed: <span class="ts" data-ts="${t.completed_at || ''}">${fmtTimestamp(t.completed_at)}</span></span>
      </div>
    </div>
  </div>`;
}

function _updateTaskRowInPlace(row, t) {
  // Sync expanded class so CSS max-height transition fires
  row.classList.toggle('expanded', _expandedTasks.has(t.id));
  const s = _taskStatsCache[t.id];
  // Update summary fields
  const summary = row.querySelector('.task-summary');
  summary.querySelector('.task-title').textContent = t.title;
  const badgeSpan = summary.querySelector('.badge');
  badgeSpan.className = 'badge badge-' + t.status;
  badgeSpan.textContent = fmtStatus(t.status);
  summary.querySelector('.task-assignee').textContent = t.assignee ? cap(t.assignee) : '\u2014';
  summary.querySelector('.task-priority').textContent = cap(t.priority);
  // Update detail grid values
  const vals = row.querySelectorAll('.task-detail-value');
  if (vals.length >= 8) {
    vals[0].textContent = t.project || '\u2014';
    vals[1].textContent = t.reviewer ? cap(t.reviewer) : '\u2014';
    vals[2].textContent = s ? fmtElapsed(s.elapsed_seconds) : '\u2014';
    vals[3].textContent = s ? fmtTokens(s.total_tokens_in, s.total_tokens_out) : '\u2014';
    vals[4].textContent = s ? fmtCost(s.total_cost_usd) : '\u2014';
    vals[5].textContent = fmtStatus(t.status);
    vals[6].textContent = t.assignee ? cap(t.assignee) : '\u2014';
    vals[7].textContent = cap(t.priority);
  }
  // Update description
  const descEl = row.querySelector('.task-desc');
  if (t.description && descEl) {
    descEl.textContent = t.description;
  } else if (t.description && !descEl) {
    const detail = row.querySelector('.task-detail-grid');
    const div = document.createElement('div');
    div.className = 'task-desc';
    div.textContent = t.description;
    detail.after(div);
  } else if (!t.description && descEl) {
    descEl.remove();
  }
  // Update VCS row (branch, commits, View Changes)
  const existingVcs = row.querySelector('.task-vcs-row');
  if (s && s.branch) {
    if (existingVcs) {
      // Update branch pill
      const branchEl = existingVcs.querySelector('.task-branch');
      if (branchEl) { branchEl.textContent = s.branch; branchEl.title = s.branch; }
      // Update commit pills — rebuild them
      const oldCommits = existingVcs.querySelectorAll('.task-commit');
      oldCommits.forEach(el => el.remove());
      const btnDiff = existingVcs.querySelector('.btn-diff');
      if (s.commits && s.commits.length) {
        s.commits.forEach(c => {
          const btn = document.createElement('button');
          btn.className = 'task-commit';
          btn.textContent = String(c).substring(0, 7);
          btn.title = String(c);
          btn.onclick = function(ev) { ev.stopPropagation(); openDiffPanel(t.id); };
          existingVcs.insertBefore(btn, btnDiff);
        });
      }
    } else {
      // Create VCS row
      const vcsDiv = document.createElement('div');
      vcsDiv.className = 'task-vcs-row';
      vcsDiv.onclick = function(ev) { ev.stopPropagation(); };
      const branchSpan = document.createElement('span');
      branchSpan.className = 'task-branch';
      branchSpan.textContent = s.branch;
      branchSpan.title = s.branch;
      vcsDiv.appendChild(branchSpan);
      if (s.commits && s.commits.length) {
        s.commits.forEach(c => {
          const btn = document.createElement('button');
          btn.className = 'task-commit';
          btn.textContent = String(c).substring(0, 7);
          btn.title = String(c);
          btn.onclick = function(ev) { ev.stopPropagation(); openDiffPanel(t.id); };
          vcsDiv.appendChild(btn);
        });
      }
      const diffBtn = document.createElement('button');
      diffBtn.className = 'btn-diff';
      diffBtn.textContent = 'View Changes';
      diffBtn.onclick = function(ev) { ev.stopPropagation(); openDiffPanel(t.id); };
      vcsDiv.appendChild(diffBtn);
      // Insert after detail grid, before desc or dates
      const grid = row.querySelector('.task-detail-grid');
      grid.after(vcsDiv);
    }
  } else if (existingVcs) {
    existingVcs.remove();
  }
  // Update dates (with data-ts for live refresh)
  const tsSpans = row.querySelectorAll('.task-dates .ts');
  if (tsSpans.length >= 2) {
    tsSpans[0].dataset.ts = t.created_at || '';
    tsSpans[0].textContent = fmtTimestamp(t.created_at);
    tsSpans[1].dataset.ts = t.completed_at || '';
    tsSpans[1].textContent = fmtTimestamp(t.completed_at);
  }
}

async function loadTasks() {
  const res = await fetch('/tasks');
  const allTasks = await res.json();
  const el = document.getElementById('taskTable');

  // Detect task status transitions to done/review for notification sound
  let taskSoundNeeded = false;
  for (const t of allTasks) {
    const prev = _prevTaskStatuses[t.id];
    if (prev && prev !== t.status && (t.status === 'done' || t.status === 'review')) {
      taskSoundNeeded = true;
    }
    _prevTaskStatuses[t.id] = t.status;
  }
  if (taskSoundNeeded) playTaskSound();

  if (!allTasks.length) { el.innerHTML = '<p style="color:var(--text-secondary)">No tasks yet.</p>'; return; }

  // Populate assignee dropdown from task data (always rebuild, preserve selection)
  const assignees = new Set();
  for (const t of allTasks) { if (t.assignee) assignees.add(t.assignee); }
  const assigneeSel = document.getElementById('taskFilterAssignee');
  const prevAssignee = assigneeSel.value;
  assigneeSel.innerHTML = '<option value="">All</option>'
    + [...assignees].sort().map(n => `<option value="${n}">${cap(n)}</option>`).join('');
  assigneeSel.value = prevAssignee;

  // Client-side filtering
  const filterStatus = document.getElementById('taskFilterStatus').value;
  const filterPriority = document.getElementById('taskFilterPriority').value;
  const filterAssignee = document.getElementById('taskFilterAssignee').value;
  let tasks = allTasks;
  if (filterStatus) tasks = tasks.filter(t => t.status === filterStatus);
  if (filterPriority) tasks = tasks.filter(t => t.priority === filterPriority);
  if (filterAssignee) tasks = tasks.filter(t => t.assignee === filterAssignee);

  // Reverse chronological order (newest first)
  tasks.sort((a, b) => b.id - a.id);

  if (!tasks.length) { el.innerHTML = '<p style="color:var(--text-secondary)">No tasks match filters.</p>'; return; }

  // Fetch stats for expanded tasks
  await Promise.all(tasks.filter(t => _expandedTasks.has(t.id)).map(async t => {
    try {
      const r = await fetch('/tasks/' + t.id + '/stats');
      if (r.ok) _taskStatsCache[t.id] = await r.json();
    } catch(e) {}
  }));

  // Check if we can update in-place (same task IDs in same order)
  const listEl = el.querySelector('.task-list');
  const existingIds = [];
  if (listEl) listEl.querySelectorAll('.task-row').forEach(r => existingIds.push(Number(r.dataset.id)));
  const newIds = tasks.map(t => t.id);
  const sameList = listEl && existingIds.length === newIds.length && existingIds.every((id, i) => id === newIds[i]);

  if (sameList) {
    // In-place update — no DOM rebuild, no transition restart
    for (const t of tasks) {
      const row = listEl.querySelector('.task-row[data-id="' + t.id + '"]');
      if (row) _updateTaskRowInPlace(row, t);
    }
  } else {
    // Full rebuild — first load or tasks changed
    el.innerHTML = '<div class="task-list">' + tasks.map(t => _taskRowHtml(t)).join('') + '</div>';
  }
}
function toggleTask(id) {
  if (_expandedTasks.has(id)) {
    _expandedTasks.delete(id);
  } else {
    _expandedTasks.add(id);
  }
  loadTasks();
}

async function loadChat() {
  // Build URL with query params based on filter state
  const showEvents = document.getElementById('chatShowEvents').checked;
  const filterFrom = document.getElementById('chatFilterFrom').value;
  const filterTo = document.getElementById('chatFilterTo').value;
  const params = new URLSearchParams();
  if (!showEvents) params.set('type', 'chat');
  const res = await fetch('/messages' + (params.toString() ? '?' + params : ''));
  let msgs = await res.json();

  // Populate filter dropdowns from unique senders/recipients in message data
  // (includes director and all participants, unlike /agents which skips director)
  const senders = new Set();
  const recipients = new Set();
  for (const m of msgs) {
    if (m.type === 'chat') { senders.add(m.sender); recipients.add(m.recipient); }
  }
  const fromSel = document.getElementById('chatFilterFrom');
  const toSel = document.getElementById('chatFilterTo');
  const prevFrom = fromSel.value;
  const prevTo = toSel.value;
  if (fromSel.options.length <= 1 || toSel.options.length <= 1) {
    const sortedSenders = [...senders].sort();
    const sortedRecipients = [...recipients].sort();
    fromSel.innerHTML = '<option value="">Anyone</option>'
      + sortedSenders.map(n => `<option value="${n}">${cap(n)}</option>`).join('');
    toSel.innerHTML = '<option value="">Anyone</option>'
      + sortedRecipients.map(n => `<option value="${n}">${cap(n)}</option>`).join('');
  }
  fromSel.value = prevFrom;
  toSel.value = prevTo;

  // Client-side filter: match sender and/or recipient
  const between = document.getElementById('chatBetween').checked;
  if (filterFrom || filterTo) {
    msgs = msgs.filter(m => {
      if (m.type === 'event') return true;
      if (between && filterFrom && filterTo) {
        return (m.sender === filterFrom && m.recipient === filterTo)
            || (m.sender === filterTo && m.recipient === filterFrom);
      }
      if (filterFrom && m.sender !== filterFrom) return false;
      if (filterTo && m.recipient !== filterTo) return false;
      return true;
    });
  }

  // Detect new messages for notification sound
  const chatMsgs = msgs.filter(m => m.type === 'chat');
  if (chatMsgs.length > 0) {
    const newestTs = chatMsgs[chatMsgs.length - 1].timestamp || '';
    if (_lastMsgTimestamp && newestTs > _lastMsgTimestamp && !_msgSendCooldown) {
      playMsgSound();
    }
    _lastMsgTimestamp = newestTs;
  }

  const log = document.getElementById('chatLog');
  const wasNearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  log.innerHTML = msgs.map(m => {
    if (m.type === 'event') return `<div class="msg-event"><span class="msg-event-line"></span><span class="msg-event-text">${esc(m.content)}</span><span class="msg-event-line"></span><span class="msg-event-time ts" data-ts="${m.timestamp}">${fmtTimestamp(m.timestamp)}</span></div>`;
    const c = avatarColor(m.sender);
    return `<div class="msg"><div class="msg-avatar" style="background:${c}">${avatarInitial(m.sender)}</div><div class="msg-body"><div class="msg-header"><span class="msg-sender" style="cursor:pointer" onclick="openAgentPanel('${m.sender}')">${cap(m.sender)}</span><span class="msg-recipient">\u2192 ${cap(m.recipient)}</span><span class="msg-time ts" data-ts="${m.timestamp}">${fmtTimestamp(m.timestamp)}</span></div><div class="msg-content">${esc(m.content)}</div></div></div>`;
  }).join('');
  if (wasNearBottom) log.scrollTop = log.scrollHeight;

  // Populate recipient dropdown for sending (from /agents)
  const agentsRes = await fetch('/agents');
  const agents = await agentsRes.json();
  const sel = document.getElementById('recipient');
  const prev = sel.value;
  sel.innerHTML = agents.map(a => {
    const label = a.role === 'manager' ? `${cap(a.name)} (manager)` : cap(a.name);
    return `<option value="${a.name}">${label}</option>`;
  }).join('');
  const mgr = agents.find(a => a.role === 'manager');
  sel.value = prev || (mgr ? mgr.name : agents[0]?.name || '');
}

async function loadAgents() {
  const res = await fetch('/agents');
  const agents = await res.json();
  const el = document.getElementById('agents');
  const agentNames = new Set(agents.map(a => a.name));
  const existingCards = new Set();
  el.querySelectorAll('.agent-card').forEach(c => existingCards.add(c.dataset.name));

  // Determine if we need a full rebuild (agents added/removed) or just update in-place
  const sameSet = agentNames.size === existingCards.size && [...agentNames].every(n => existingCards.has(n));

  if (sameSet) {
    // Update existing cards in-place — no flicker
    for (const a of agents) {
      const card = el.querySelector(`.agent-card[data-name="${a.name}"]`);
      if (!card) continue;
      const dot = card.querySelector('.dot');
      dot.className = 'dot ' + (a.pid ? 'dot-active' : 'dot-idle');
      card.querySelector('.agent-status').textContent =
        (a.pid ? 'Running (PID ' + a.pid + ')' : 'Idle') + ' \u00b7 ' + a.unread_inbox + ' unread';
    }
  } else {
    // Full rebuild — agents changed
    el.innerHTML = agents.map(a => `<div class="agent-card" data-name="${a.name}" onclick="openAgentPanel('${a.name}')">
      <span class="dot ${a.pid ? 'dot-active' : 'dot-idle'}"></span>
      <span class="agent-name">${cap(a.name)}</span>
      <span class="agent-status">${a.pid ? 'Running (PID ' + a.pid + ')' : 'Idle'} \u00b7 ${a.unread_inbox} unread</span>
    </div>`).join('');
  }
}

// --- Sidebar ---
async function loadSidebar() {
  try {
    const [tasksRes, agentsRes] = await Promise.all([fetch('/tasks'), fetch('/agents')]);
    const tasks = await tasksRes.json();
    const agents = await agentsRes.json();

    // Fetch stats for all agents in parallel
    const statsMap = {};
    await Promise.all(agents.map(async a => {
      try {
        const r = await fetch('/agents/' + a.name + '/stats');
        if (r.ok) statsMap[a.name] = await r.json();
      } catch(e) {}
    }));

    // 1. Team Status Widget
    const now = new Date();
    const oneDayAgo = new Date(now - 24*60*60*1000);
    const doneToday = tasks.filter(t => t.completed_at && new Date(t.completed_at) > oneDayAgo && (t.status === 'done')).length;
    const openCount = tasks.filter(t => t.status === 'open' || t.status === 'in_progress' || t.status === 'review').length;
    let totalCost = 0;
    for (const name in statsMap) { totalCost += (statsMap[name].total_cost_usd || 0); }
    document.getElementById('sidebarStatusContent').innerHTML =
      '<div class="sidebar-stat-row"><span class="stat-value">' + doneToday + ' done</span> &middot; <span class="stat-value">' + openCount + ' open</span></div>' +
      '<div class="sidebar-stat-row">$' + totalCost.toFixed(2) + ' total spent</div>';

    // 2. Agent Roster Widget
    const inProgressTasks = tasks.filter(t => t.status === 'in_progress');
    let agentHtml = '';
    for (const a of agents) {
      let dotClass = 'dot-offline';
      let activity = 'Idle';
      if (a.pid) {
        dotClass = 'dot-working';
        const agentTask = inProgressTasks.find(t => t.assignee === a.name);
        activity = agentTask ? ('T' + String(agentTask.id).padStart(4,'0') + ' ' + agentTask.title) : 'Working...';
      } else if (a.unread_inbox > 0) {
        dotClass = 'dot-queued';
      }
      const cost = statsMap[a.name] ? '$' + Number(statsMap[a.name].total_cost_usd || 0).toFixed(2) : '';
      agentHtml += '<div class="sidebar-agent-row" style="cursor:pointer" onclick="openAgentPanel(\\'' + a.name + '\\')">' +
        '<span class="sidebar-agent-dot ' + dotClass + '"></span>' +
        '<span class="sidebar-agent-name">' + cap(a.name) + '</span>' +
        '<span class="sidebar-agent-activity">' + esc(activity) + '</span>' +
        '<span class="sidebar-agent-cost">' + cost + '</span>' +
        '</div>';
    }
    document.getElementById('sidebarAgentList').innerHTML = agentHtml;

    // 3. Recent Tasks Widget
    const sorted = [...tasks].sort((a, b) => {
      const da = a.updated_at || a.created_at || '';
      const db = b.updated_at || b.created_at || '';
      return db.localeCompare(da);
    }).slice(0, 7);
    let taskHtml = '';
    for (const t of sorted) {
      const tid = 'T' + String(t.id).padStart(4,'0');
      taskHtml += '<div class="sidebar-task-row">' +
        '<span class="sidebar-task-id">' + tid + '</span>' +
        '<span class="sidebar-task-title">' + esc(t.title) + '</span>' +
        '<span class="sidebar-task-badge"><span class="badge badge-' + t.status + '">' + fmtStatus(t.status) + '</span></span>' +
        '<span class="sidebar-task-assignee">' + (t.assignee ? cap(t.assignee) : '') + '</span>' +
        '</div>';
    }
    document.getElementById('sidebarTaskList').innerHTML = taskHtml;
  } catch(e) { console.error('Sidebar load error:', e); }
}

// --- Panel (shared state for diff + agent modes) ---
let _panelMode = null; // 'diff' | 'agent'
let _panelAgent = null;
let _agentTabData = {};
let _agentCurrentTab = 'inbox';

// --- Diff Panel ---
let _diffData = null;
let _diffCurrentTab = 'files';

function parseDiff(diffText) {
  if (!diffText) return [];
  const files = [];
  let currentFile = null;
  const lines = diffText.split('\\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('diff --git')) {
      const match = line.match(/b\\/(.+)$/);
      currentFile = { name: match ? match[1] : 'unknown', hunks: [], additions: 0, deletions: 0 };
      files.push(currentFile);
      // Skip --- and +++ lines
      while (i + 1 < lines.length && (lines[i+1].startsWith('---') || lines[i+1].startsWith('+++') || lines[i+1].startsWith('index ') || lines[i+1].startsWith('new file') || lines[i+1].startsWith('deleted file'))) i++;
    } else if (line.startsWith('@@') && currentFile) {
      currentFile.hunks.push({ header: line, lines: [] });
    } else if (currentFile && currentFile.hunks.length > 0) {
      const hunk = currentFile.hunks[currentFile.hunks.length - 1];
      if (line.startsWith('+')) {
        hunk.lines.push({ type: 'add', content: line.substring(1) });
        currentFile.additions++;
      } else if (line.startsWith('-')) {
        hunk.lines.push({ type: 'del', content: line.substring(1) });
        currentFile.deletions++;
      } else {
        hunk.lines.push({ type: 'ctx', content: line.startsWith(' ') ? line.substring(1) : line });
      }
    }
  }
  return files;
}

function renderDiffFull(files) {
  if (!files.length) return '<div class="diff-empty">No changes</div>';
  let html = '';
  for (const f of files) {
    html += '<div class="diff-file-section" id="diff-file-' + encodeURIComponent(f.name) + '">';
    html += '<div class="diff-file-header"><span class="diff-file-name">' + esc(f.name) + '</span>';
    html += '<span class="diff-file-stats"><span class="diff-file-add">+' + f.additions + '</span><span class="diff-file-del">-' + f.deletions + '</span></span></div>';
    for (const h of f.hunks) {
      html += '<div class="diff-hunk-header">' + esc(h.header) + '</div>';
      let lineNum = 1;
      const m = h.header.match(/\\+(\\d+)/);
      if (m) lineNum = parseInt(m[1]);
      for (const l of h.lines) {
        const gutter = l.type === 'del' ? '' : lineNum;
        html += '<div class="diff-line ' + l.type + '"><span class="diff-line-gutter">' + gutter + '</span><span class="diff-line-content">' + esc(l.content) + '</span></div>';
        if (l.type !== 'del') lineNum++;
      }
    }
    html += '</div>';
  }
  return html;
}

function renderDiffFiles(files) {
  if (!files.length) return '<div class="diff-empty">No files changed</div>';
  let html = '<div class="diff-file-list">';
  for (const f of files) {
    html += '<div class="diff-file-list-item" onclick="scrollToDiffFile(\\'' + encodeURIComponent(f.name) + '\\')">';
    html += '<span class="diff-file-list-name">' + esc(f.name) + '</span>';
    html += '<span class="diff-file-stats"><span class="diff-file-add">+' + f.additions + '</span><span class="diff-file-del">-' + f.deletions + '</span></span>';
    html += '</div>';
  }
  html += '</div>';
  return html;
}

function scrollToDiffFile(encodedName) {
  switchDiffTab('diff');
  setTimeout(() => {
    const el = document.getElementById('diff-file-' + encodedName);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 50);
}

function switchDiffTab(tab) {
  _diffCurrentTab = tab;
  document.querySelectorAll('.diff-tab').forEach(t => t.classList.toggle('active', t.dataset.dtab === tab));
  const body = document.getElementById('diffPanelBody');
  if (!_diffData) return;
  body.innerHTML = tab === 'files' ? renderDiffFiles(_diffData) : renderDiffFull(_diffData);
}

async function openDiffPanel(taskId) {
  _panelMode = 'diff';
  _panelAgent = null;
  _agentTabData = {};
  const panel = document.getElementById('diffPanel');
  const backdrop = document.getElementById('diffBackdrop');
  document.getElementById('diffPanelTitle').textContent = 'T' + String(taskId).padStart(4, '0');
  document.getElementById('diffPanelBranch').textContent = 'Loading...';
  document.getElementById('diffPanelCommits').innerHTML = '';
  document.getElementById('diffPanelCommits').style.display = '';
  // Restore diff tabs
  const tabsEl = panel.querySelector('.diff-panel-tabs');
  tabsEl.innerHTML = '<button class="diff-tab active" data-dtab="files" onclick="switchDiffTab(\\'files\\')">Files Changed</button><button class="diff-tab" data-dtab="diff" onclick="switchDiffTab(\\'diff\\')">Full Diff</button>';
  document.getElementById('diffPanelBody').innerHTML = '<div class="diff-empty">Loading diff...</div>';
  panel.classList.add('open');
  backdrop.classList.add('open');
  try {
    const res = await fetch('/tasks/' + taskId + '/diff');
    const data = await res.json();
    document.getElementById('diffPanelBranch').textContent = data.branch || 'no branch';
    const commitsHtml = (data.commits || []).map(c => '<span class="diff-panel-commit">' + esc(String(c).substring(0, 7)) + '</span>').join('');
    document.getElementById('diffPanelCommits').innerHTML = commitsHtml;
    _diffData = parseDiff(data.diff || '');
    _diffCurrentTab = 'files';
    document.querySelectorAll('.diff-tab').forEach(t => t.classList.toggle('active', t.dataset.dtab === 'files'));
    document.getElementById('diffPanelBody').innerHTML = renderDiffFiles(_diffData);
  } catch(e) {
    document.getElementById('diffPanelBody').innerHTML = '<div class="diff-empty">Failed to load diff</div>';
  }
}

function closePanel() {
  document.getElementById('diffPanel').classList.remove('open');
  document.getElementById('diffBackdrop').classList.remove('open');
  _diffData = null;
  _panelMode = null;
  _panelAgent = null;
  _agentTabData = {};
}
function closeDiffPanel() { closePanel(); }

// --- Agent Panel ---
function renderAgentInbox(msgs) {
  if (!msgs || !msgs.length) return '<div class="diff-empty">No messages</div>';
  return msgs.map(m => {
    const cls = m.read ? '' : ' unread';
    return '<div class="agent-msg' + cls + '"><div class="agent-msg-header"><span class="agent-msg-sender">' + esc(cap(m.sender)) + '</span><span class="agent-msg-time">' + fmtTimestamp(m.time) + '</span></div><div class="agent-msg-body collapsed" onclick="this.classList.toggle(\\'collapsed\\')">' + esc(m.body) + '</div></div>';
  }).join('');
}

function renderAgentOutbox(msgs) {
  if (!msgs || !msgs.length) return '<div class="diff-empty">No messages</div>';
  return msgs.map(m => {
    const cls = m.routed ? '' : ' pending';
    return '<div class="agent-msg' + cls + '"><div class="agent-msg-header"><span class="agent-msg-sender">\\u2192 ' + esc(cap(m.recipient)) + '</span><span class="agent-msg-time">' + fmtTimestamp(m.time) + '</span></div><div class="agent-msg-body collapsed" onclick="this.classList.toggle(\\'collapsed\\')">' + esc(m.body) + '</div></div>';
  }).join('');
}

function renderAgentLogs(data) {
  const sessions = data && data.sessions ? data.sessions : [];
  if (!sessions.length) return '<div class="diff-empty">No worklogs</div>';
  return sessions.map((s, i) => {
    const expanded = i === 0;
    return '<div class="agent-log-session"><div class="agent-log-header" onclick="toggleLogSession(this)"><span class="agent-log-arrow' + (expanded ? ' expanded' : '') + '">\\u25B6</span>' + esc(s.filename) + '</div><div class="agent-log-content' + (expanded ? ' expanded' : '') + '">' + esc(s.content) + '</div></div>';
  }).join('');
}

function toggleLogSession(header) {
  const arrow = header.querySelector('.agent-log-arrow');
  const content = header.nextElementSibling;
  arrow.classList.toggle('expanded');
  content.classList.toggle('expanded');
}

function renderAgentStatsPanel(s) {
  if (!s) return '<div class="diff-empty">Stats unavailable</div>';
  return '<div class="agent-stats-grid">' +
    '<div class="agent-stat"><div class="agent-stat-label">Tasks done</div><div class="agent-stat-value">' + s.tasks_done + '</div></div>' +
    '<div class="agent-stat"><div class="agent-stat-label">In review</div><div class="agent-stat-value">' + s.tasks_in_review + '</div></div>' +
    '<div class="agent-stat"><div class="agent-stat-label">Total tasks</div><div class="agent-stat-value">' + s.tasks_total + '</div></div>' +
    '<div class="agent-stat"><div class="agent-stat-label">Sessions</div><div class="agent-stat-value">' + s.session_count + '</div></div>' +
    '<div class="agent-stat"><div class="agent-stat-label">Tokens (in/out)</div><div class="agent-stat-value">' + fmtTokens(s.total_tokens_in, s.total_tokens_out) + '</div></div>' +
    '<div class="agent-stat"><div class="agent-stat-label">Total cost</div><div class="agent-stat-value">' + fmtCost(s.total_cost_usd) + '</div></div>' +
    '<div class="agent-stat"><div class="agent-stat-label">Agent time</div><div class="agent-stat-value">' + fmtElapsed(s.agent_time_seconds) + '</div></div>' +
    '<div class="agent-stat"><div class="agent-stat-label">Avg task time</div><div class="agent-stat-value">' + fmtElapsed(s.avg_task_seconds) + '</div></div>' +
  '</div>';
}

async function switchAgentTab(tab) {
  _agentCurrentTab = tab;
  const panel = document.getElementById('diffPanel');
  panel.querySelectorAll('.diff-tab').forEach(t => t.classList.toggle('active', t.dataset.dtab === tab));
  const body = document.getElementById('diffPanelBody');
  const name = _panelAgent;
  if (!name) return;

  // Use cached data if available
  if (_agentTabData[tab]) {
    _renderAgentTab(tab, _agentTabData[tab]);
    return;
  }

  body.innerHTML = '<div class="diff-empty">Loading...</div>';
  try {
    let url = '/agents/' + name + '/' + tab;
    const res = await fetch(url);
    const data = await res.json();
    _agentTabData[tab] = data;
    _renderAgentTab(tab, data);
  } catch(e) {
    body.innerHTML = '<div class="diff-empty">Failed to load ' + tab + '</div>';
  }
}

function _renderAgentTab(tab, data) {
  const body = document.getElementById('diffPanelBody');
  if (tab === 'inbox') body.innerHTML = renderAgentInbox(data);
  else if (tab === 'outbox') body.innerHTML = renderAgentOutbox(data);
  else if (tab === 'logs') body.innerHTML = renderAgentLogs(data);
  else if (tab === 'stats') body.innerHTML = renderAgentStatsPanel(data);
}

async function openAgentPanel(agentName) {
  _panelMode = 'agent';
  _panelAgent = agentName;
  _agentTabData = {};
  _agentCurrentTab = 'inbox';
  _diffData = null;

  const panel = document.getElementById('diffPanel');
  const backdrop = document.getElementById('diffBackdrop');

  document.getElementById('diffPanelTitle').textContent = cap(agentName);
  document.getElementById('diffPanelBranch').textContent = '';
  document.getElementById('diffPanelCommits').innerHTML = '';
  document.getElementById('diffPanelCommits').style.display = 'none';

  // Fetch role for subtitle
  try {
    const r = await fetch('/agents');
    const agents = await r.json();
    const agent = agents.find(a => a.name === agentName);
    if (agent) document.getElementById('diffPanelBranch').textContent = cap(agent.role);
  } catch(e) {}

  // Replace tabs with agent tabs
  const tabsEl = panel.querySelector('.diff-panel-tabs');
  tabsEl.innerHTML = '<button class="diff-tab active" data-dtab="inbox" onclick="switchAgentTab(\\'inbox\\')">Inbox</button>' +
    '<button class="diff-tab" data-dtab="outbox" onclick="switchAgentTab(\\'outbox\\')">Outbox</button>' +
    '<button class="diff-tab" data-dtab="logs" onclick="switchAgentTab(\\'logs\\')">Logs</button>' +
    '<button class="diff-tab" data-dtab="stats" onclick="switchAgentTab(\\'stats\\')">Stats</button>';

  document.getElementById('diffPanelBody').innerHTML = '<div class="diff-empty">Loading...</div>';
  panel.classList.add('open');
  backdrop.classList.add('open');

  // Load inbox tab by default
  switchAgentTab('inbox');
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closePanel();
});

function autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = el.scrollHeight + 'px';
}
function resetTextareaHeight(el) {
  el.style.height = '';
}
function handleChatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
}
async function sendMsg() {
  if (_micActive && _recognition) { _recognition.stop(); _micActive = false; const mb = document.getElementById('micBtn'); if (mb) { mb.classList.remove('recording'); mb.title = 'Voice input'; } }
  const input = document.getElementById('msgInput');
  const recipient = document.getElementById('recipient').value;
  if (!input.value.trim()) return;
  _msgSendCooldown = true;
  setTimeout(function() { _msgSendCooldown = false; }, 4000);
  await fetch('/messages', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({recipient, content: input.value})
  });
  input.value = '';
  resetTextareaHeight(input);
}

// Lightweight refresh of relative timestamps (no data fetch)
function refreshTimestamps() {
  document.querySelectorAll('.ts[data-ts]').forEach(el => {
    const iso = el.dataset.ts;
    el.textContent = fmtTimestamp(iso);
  });
}
setInterval(refreshTimestamps, 30000);

// Auto-refresh: poll chat + tasks every 2 seconds, sidebar always
setInterval(() => {
  loadSidebar();
  const active = document.querySelector('.panel.active');
  if (active && active.id === 'chat') loadChat();
  if (active && active.id === 'tasks') loadTasks();
  if (active && active.id === 'agents') loadAgents();
}, 2000);

// Read URL hash on page load to activate the correct tab
function initFromHash() {
  const hash = window.location.hash.replace('#', '');
  const valid = ['chat', 'tasks', 'agents'];
  switchTab(valid.includes(hash) ? hash : 'chat', false);
}
window.addEventListener('hashchange', () => {
  const hash = window.location.hash.replace('#', '');
  const valid = ['chat', 'tasks', 'agents'];
  if (valid.includes(hash)) switchTab(hash, false);
});
// --- Voice-to-text mic ---
let _recognition = null;
let _micActive = false;
let _micStopping = false;
let _micBaseText = '';
let _micFinalText = '';

(function initMic() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;
  const micBtn = document.getElementById('micBtn');
  micBtn.style.display = 'flex';
  _recognition = new SpeechRecognition();
  _recognition.continuous = true;
  _recognition.interimResults = true;
  _recognition.lang = navigator.language || 'en-US';
  _recognition.onresult = function(e) {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) {
        _micFinalText += e.results[i][0].transcript;
      } else {
        interim += e.results[i][0].transcript;
      }
    }
    const _el = document.getElementById('msgInput'); _el.value = _micBaseText + _micFinalText + interim; autoResizeTextarea(_el);
  };
  _recognition.onend = function() {
    _micActive = false; _micStopping = false;
    micBtn.classList.remove('recording'); micBtn.title = 'Voice input';
  };
  _recognition.onerror = function(e) {
    if (e.error !== 'aborted' && e.error !== 'no-speech') console.warn('Speech recognition error:', e.error);
    _micActive = false; _micStopping = false;
    micBtn.classList.remove('recording'); micBtn.title = 'Voice input';
  };
})();

function toggleMic() {
  if (!_recognition || _micStopping) return;
  const micBtn = document.getElementById('micBtn');
  if (_micActive) {
    _micStopping = true; _recognition.stop();
    micBtn.classList.remove('recording'); micBtn.title = 'Voice input';
  } else {
    const input = document.getElementById('msgInput');
    _micBaseText = input.value ? input.value + ' ' : '';
    _micFinalText = '';
    try { _recognition.start(); } catch(e) { return; }
    _micActive = true;
    micBtn.classList.add('recording'); micBtn.title = 'Stop recording';
  }
}

_updateMuteBtn();
initFromHash();
loadSidebar();
</script>
</body>
</html>
"""
