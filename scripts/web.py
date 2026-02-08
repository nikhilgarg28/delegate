"""FastAPI web application for the director UI.

Provides:
    GET  /            — HTML single-page app
    GET  /tasks       — list tasks (JSON)
    GET  /tasks/{id}/stats — task stats (elapsed, agent time, tokens)
    GET  /messages    — get chat/event log (JSON)
    POST /messages    — director sends a message to an agent
    GET  /agents      — list agents and their states (JSON)
    GET  /agents/{name}/stats — agent stats (tasks, tokens, cost)

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

from scripts.task import list_tasks as _list_tasks, get_task as _get_task, get_task_diff as _get_task_diff, VALID_STATUSES
from scripts.chat import get_messages as _get_messages, get_task_stats as _get_task_stats, get_agent_stats as _get_agent_stats
from scripts.mailbox import send as _send
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
  body { font-family: 'Geist Sans', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0b; color: #ededed; font-size: 14px; line-height: 1.5; letter-spacing: -0.01em; display: flex; flex-direction: row; -webkit-font-smoothing: antialiased; }
  .main { flex: 1; display: flex; flex-direction: column; min-height: 0; height: 100vh; }
  .header { background: #111113; padding: 14px 24px; border-bottom: 1px solid rgba(255,255,255,0.08); display: flex; align-items: center; gap: 16px; flex-shrink: 0; }

  /* Sidebar */
  .sidebar { width: 280px; min-width: 280px; height: 100vh; position: sticky; top: 0; background: #0d0d0f; border-right: 1px solid rgba(255,255,255,0.06); display: flex; flex-direction: column; overflow: hidden; }
  .sidebar-widget { padding: 16px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .sidebar-widget:last-child { border-bottom: none; flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .sidebar-widget-header { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #555; margin-bottom: 8px; }
  .sidebar-stat-row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #a1a1a1; margin-bottom: 4px; }
  .sidebar-stat-row .stat-value { color: #ededed; font-weight: 500; font-variant-numeric: tabular-nums; }
  .sidebar-agent-list { display: flex; flex-direction: column; gap: 0; max-height: calc(28px * 6); overflow-y: auto; }
  .sidebar-agent-row { display: flex; align-items: center; gap: 8px; height: 28px; min-height: 28px; font-size: 12px; padding: 0 2px; }
  .sidebar-agent-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .sidebar-agent-dot.dot-working { background: #22c55e; box-shadow: 0 0 6px rgba(34,197,94,0.4); }
  .sidebar-agent-dot.dot-queued { background: #60a5fa; }
  .sidebar-agent-dot.dot-offline { background: #333; }
  .sidebar-agent-name { color: #ededed; font-weight: 500; white-space: nowrap; }
  .sidebar-agent-activity { color: #555; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sidebar-agent-cost { color: #444; font-size: 11px; font-variant-numeric: tabular-nums; flex-shrink: 0; }
  .sidebar-task-list { display: flex; flex-direction: column; gap: 0; flex: 1; min-height: 0; overflow-y: auto; }
  .sidebar-task-row { display: flex; align-items: center; gap: 8px; height: 28px; min-height: 28px; font-size: 12px; padding: 0 2px; }
  .sidebar-task-id { color: #555; font-variant-numeric: tabular-nums; flex-shrink: 0; min-width: 42px; }
  .sidebar-task-title { color: #a1a1a1; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sidebar-task-badge { flex-shrink: 0; }
  .sidebar-task-badge .badge { font-size: 10px; padding: 2px 6px; }
  .sidebar-task-assignee { color: #444; font-size: 11px; flex-shrink: 0; }
  .sidebar-see-all { color: #60a5fa; font-size: 11px; cursor: pointer; text-decoration: none; margin-top: 6px; display: inline-block; }
  .sidebar-see-all:hover { text-decoration: underline; }
  @media (max-width: 900px) { .sidebar { display: none; } }
  .header h1 { font-size: 16px; font-weight: 600; letter-spacing: -0.02em; color: #fafafa; }
  .tabs { display: flex; gap: 2px; margin-left: 32px; }
  .tab { padding: 7px 14px; cursor: pointer; border-radius: 6px; background: transparent; border: none; color: #666; font-family: inherit; font-size: 13px; font-weight: 500; transition: color 0.15s, background 0.15s; }
  .tab:hover { color: #999; background: rgba(255,255,255,0.04); }
  .tab.active { background: rgba(255,255,255,0.08); color: #fafafa; }
  .content { max-width: 1000px; width: 100%; margin: 0 auto; padding: 24px; flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .panel { display: none; }
  .panel.active { display: flex; flex-direction: column; flex: 1; min-height: 0; }

  /* Tasks */
  .task-list { display: flex; flex-direction: column; gap: 2px; }
  .task-row { cursor: pointer; border-radius: 8px; border: 1px solid transparent; transition: border-color 0.15s, background 0.15s; }
  .task-row:hover { background: rgba(255,255,255,0.02); }
  .task-row.expanded { border-color: rgba(255,255,255,0.08); background: rgba(255,255,255,0.02); }
  .task-summary { display: grid; grid-template-columns: 60px 1fr auto auto auto; gap: 12px; align-items: center; padding: 10px 14px; font-size: 13px; }
  .task-summary > span { white-space: nowrap; }
  .task-id { color: #555; font-variant-numeric: tabular-nums; font-size: 12px; }
  .task-title { color: #ededed; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .task-assignee { color: #a1a1a1; font-size: 12px; min-width: 70px; text-align: right; }
  .task-priority { color: #a1a1a1; font-size: 12px; min-width: 60px; text-align: right; }
  .task-detail { max-height: 0; overflow: hidden; transition: max-height 0.25s ease-out, padding 0.25s ease-out; padding: 0 14px; }
  .task-row.expanded .task-detail { max-height: 400px; padding: 0 14px 14px; transition: max-height 0.3s ease-in, padding 0.2s ease-in; }
  .task-detail-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 10px; }
  .task-detail-item { background: rgba(255,255,255,0.03); border-radius: 8px; padding: 10px 14px; }
  .task-detail-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #555; margin-bottom: 4px; }
  .task-detail-value { font-size: 13px; color: #ededed; font-variant-numeric: tabular-nums; }
  .task-desc { color: #a1a1a1; font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; padding: 10px 14px; background: rgba(255,255,255,0.02); border-radius: 8px; }
  .task-dates { display: flex; gap: 24px; margin-top: 10px; font-size: 11px; color: #555; }
  .badge { padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 500; letter-spacing: 0.01em; }
  .badge-open { background: rgba(52,211,153,0.12); color: #34d399; }
  .badge-in_progress { background: rgba(251,191,36,0.12); color: #fbbf24; }
  .badge-review { background: rgba(96,165,250,0.12); color: #60a5fa; }
  .badge-done { background: rgba(255,255,255,0.06); color: #555; }

  /* Chat */
  .chat-log { flex: 1; min-height: 0; overflow-y: auto; background: #111113; border: 1px solid rgba(255,255,255,0.06); border-radius: 10px; padding: 12px; margin-bottom: 12px; }
  .msg { display: flex; gap: 12px; padding: 10px 12px; border-radius: 8px; margin-bottom: 2px; transition: background 0.15s; }
  .msg:hover { background: rgba(255,255,255,0.02); }
  .msg-avatar { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; color: #fff; flex-shrink: 0; margin-top: 2px; }
  .msg-body { flex: 1; min-width: 0; }
  .msg-header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 3px; }
  .msg-sender { font-weight: 600; color: #ededed; font-size: 13px; }
  .msg-recipient { color: #555; font-size: 12px; font-weight: 400; }
  .msg-time { color: #444; font-size: 11px; font-variant-numeric: tabular-nums; margin-left: auto; flex-shrink: 0; }
  .msg-content { color: #a1a1a1; font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
  .msg-event { display: flex; align-items: center; justify-content: center; gap: 10px; padding: 6px 12px; margin: 4px 0; }
  .msg-event-line { flex: 1; height: 1px; background: rgba(255,255,255,0.06); }
  .msg-event-text { color: #444; font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .chat-input { display: flex; gap: 8px; flex-shrink: 0; }
  .chat-input input { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: #111113; color: #ededed; font-family: inherit; font-size: 13px; outline: none; transition: border-color 0.15s; }
  .chat-input input:focus { border-color: rgba(255,255,255,0.25); }
  .chat-input input::placeholder { color: #444; }
  .chat-input select { padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: #111113; color: #ededed; font-family: inherit; font-size: 13px; outline: none; cursor: pointer; }
  .chat-input button { padding: 10px 20px; border-radius: 8px; border: none; background: #fafafa; color: #0a0a0b; font-family: inherit; font-size: 13px; font-weight: 500; cursor: pointer; transition: background 0.15s; }
  .chat-input button:hover { background: #d4d4d4; }

  /* Agents */
  .agent-card { background: #111113; border: 1px solid rgba(255,255,255,0.06); border-radius: 10px; padding: 16px 20px; margin-bottom: 8px; display: flex; align-items: center; gap: 16px; transition: border-color 0.15s; }
  .agent-card:hover { border-color: rgba(255,255,255,0.12); }
  .agent-name { font-weight: 600; min-width: 120px; color: #ededed; font-size: 13px; }
  .agent-status { font-size: 12px; color: #555; }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; margin-right: 8px; }
  .agent-card { cursor: pointer; }
  .dot-active { background: #22c55e; box-shadow: 0 0 6px rgba(34,197,94,0.4); }
  .dot-idle { background: #333; }
  .agent-stats { display: none; width: 100%; padding: 14px 0 2px; }
  .agent-card.expanded .agent-stats { display: block; }
  .agent-card.expanded { flex-wrap: wrap; }
  .agent-stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .agent-stat { background: rgba(255,255,255,0.03); border-radius: 8px; padding: 10px 14px; }
  .agent-stat-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #555; margin-bottom: 4px; }
  .agent-stat-value { font-size: 14px; font-weight: 600; color: #ededed; font-variant-numeric: tabular-nums; }

  /* Filters (shared) */
  .chat-filters, .task-filters { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; flex-shrink: 0; }
  .chat-filters select, .task-filters select { padding: 6px 10px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.1); background: #111113; color: #ededed; font-family: inherit; font-size: 12px; outline: none; cursor: pointer; transition: border-color 0.15s; }
  .chat-filters select:focus, .task-filters select:focus { border-color: rgba(255,255,255,0.25); }
  .chat-filters label, .task-filters label { display: flex; align-items: center; gap: 6px; color: #666; font-size: 12px; cursor: pointer; user-select: none; transition: color 0.15s; }
  .chat-filters label:hover, .task-filters label:hover { color: #999; }
  .chat-filters input[type="checkbox"], .task-filters input[type="checkbox"] { appearance: none; width: 14px; height: 14px; border: 1px solid rgba(255,255,255,0.15); border-radius: 3px; background: transparent; cursor: pointer; position: relative; transition: background 0.15s, border-color 0.15s; }
  .chat-filters input[type="checkbox"]:checked, .task-filters input[type="checkbox"]:checked { background: #fafafa; border-color: #fafafa; }
  .chat-filters input[type="checkbox"]:checked::after, .task-filters input[type="checkbox"]:checked::after { content: ''; position: absolute; top: 1px; left: 4px; width: 4px; height: 8px; border: solid #0a0a0b; border-width: 0 1.5px 1.5px 0; transform: rotate(45deg); }
  .filter-label { color: #444; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.18); }
</style>
</head>
<body>
<div class="sidebar" id="sidebar">
  <div class="sidebar-widget" id="sidebarStatus">
    <div class="sidebar-widget-header">Team Status</div>
    <div id="sidebarStatusContent"><span style="color:#444;font-size:12px">Loading...</span></div>
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
      <input type="text" id="msgInput" placeholder="Send a message..." onkeydown="if(event.key==='Enter')sendMsg()">
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
<script>
function cap(s){return s.charAt(0).toUpperCase()+s.slice(1);}
function fmtStatus(s){return s.split('_').map(w=>cap(w)).join(' ');}
function fmtTime(iso){const d=new Date(iso);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});}
function fmtTimestamp(iso){
  if(!iso) return '\u2014';
  const d=new Date(iso), now=new Date(), diff=now-d, sec=Math.floor(diff/1000), min=Math.floor(sec/60), hr=Math.floor(min/60);
  if(sec<60) return 'just now';
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
  if (!allTasks.length) { el.innerHTML = '<p style="color:#888">No tasks yet.</p>'; return; }

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

  if (!tasks.length) { el.innerHTML = '<p style="color:#888">No tasks match filters.</p>'; return; }

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

  const log = document.getElementById('chatLog');
  const wasNearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  log.innerHTML = msgs.map(m => {
    if (m.type === 'event') return `<div class="msg-event"><span class="msg-event-line"></span><span class="msg-event-text"><span class="ts" data-ts="${m.timestamp}">${fmtTimestamp(m.timestamp)}</span> \u2002${esc(m.content)}</span><span class="msg-event-line"></span></div>`;
    const c = avatarColor(m.sender);
    return `<div class="msg"><div class="msg-avatar" style="background:${c}">${avatarInitial(m.sender)}</div><div class="msg-body"><div class="msg-header"><span class="msg-sender">${cap(m.sender)}</span><span class="msg-recipient">\u2192 ${cap(m.recipient)}</span><span class="msg-time ts" data-ts="${m.timestamp}">${fmtTimestamp(m.timestamp)}</span></div><div class="msg-content">${esc(m.content)}</div></div></div>`;
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

let _expandedAgents = new Set();
let _agentStatsCache = {};

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
    el.innerHTML = agents.map(a => `<div class="agent-card${_expandedAgents.has(a.name) ? ' expanded' : ''}" data-name="${a.name}" onclick="toggleAgent(this, '${a.name}')">
      <span class="dot ${a.pid ? 'dot-active' : 'dot-idle'}"></span>
      <span class="agent-name">${cap(a.name)}</span>
      <span class="agent-status">${a.pid ? 'Running (PID ' + a.pid + ')' : 'Idle'} \u00b7 ${a.unread_inbox} unread</span>
      <div class="agent-stats" id="agent-stats-${a.name}" onclick="event.stopPropagation()"></div>
    </div>`).join('');
    // Restore stats from cache immediately (fetch happens in common block below)
    for (const name of _expandedAgents) {
      if (agentNames.has(name) && _agentStatsCache[name]) {
        renderAgentStats(name, _agentStatsCache[name]);
      }
    }
  }
  // Refresh stats for expanded cards periodically (in-place path too)
  for (const name of _expandedAgents) {
    if (agentNames.has(name)) fetchAgentStats(name);
  }
}
async function toggleAgent(card, name) {
  card.classList.toggle('expanded');
  if (card.classList.contains('expanded')) {
    _expandedAgents.add(name);
    fetchAgentStats(name);
  } else {
    _expandedAgents.delete(name);
  }
}
function renderAgentStats(name, s) {
  const el = document.getElementById('agent-stats-' + name);
  if (!el) return;
  el.innerHTML = `<div class="agent-stats-grid">
    <div class="agent-stat"><div class="agent-stat-label">Tasks done</div><div class="agent-stat-value">${s.tasks_done}</div></div>
    <div class="agent-stat"><div class="agent-stat-label">In review</div><div class="agent-stat-value">${s.tasks_in_review}</div></div>
    <div class="agent-stat"><div class="agent-stat-label">Total tasks</div><div class="agent-stat-value">${s.tasks_total}</div></div>
    <div class="agent-stat"><div class="agent-stat-label">Sessions</div><div class="agent-stat-value">${s.session_count}</div></div>
    <div class="agent-stat"><div class="agent-stat-label">Tokens (in/out)</div><div class="agent-stat-value">${fmtTokens(s.total_tokens_in, s.total_tokens_out)}</div></div>
    <div class="agent-stat"><div class="agent-stat-label">Total cost</div><div class="agent-stat-value">${fmtCost(s.total_cost_usd)}</div></div>
    <div class="agent-stat"><div class="agent-stat-label">Agent time</div><div class="agent-stat-value">${fmtElapsed(s.agent_time_seconds)}</div></div>
    <div class="agent-stat"><div class="agent-stat-label">Avg task time</div><div class="agent-stat-value">${fmtElapsed(s.avg_task_seconds)}</div></div>
  </div>`;
}
async function fetchAgentStats(name) {
  const el = document.getElementById('agent-stats-' + name);
  if (!el) return;
  try {
    const r = await fetch('/agents/' + name + '/stats');
    const s = await r.json();
    _agentStatsCache[name] = s;
    renderAgentStats(name, s);
  } catch(e) { el.innerHTML = '<p style="color:#555;font-size:12px">Stats unavailable</p>'; }
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
      agentHtml += '<div class="sidebar-agent-row">' +
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

async function sendMsg() {
  const input = document.getElementById('msgInput');
  const recipient = document.getElementById('recipient').value;
  if (!input.value.trim()) return;
  await fetch('/messages', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({recipient, content: input.value})
  });
  input.value = '';
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
initFromHash();
loadSidebar();
</script>
</body>
</html>
"""
