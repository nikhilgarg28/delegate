"""FastAPI web application for the director UI.

Provides:
    GET  /            — HTML single-page app
    GET  /tasks       — list tasks (JSON)
    GET  /tasks/{id}/stats — task stats (elapsed, agent time, tokens)
    GET  /messages    — get chat/event log (JSON)
    POST /messages    — director sends a message to an agent
    GET  /agents      — list agents and their states (JSON)

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

from scripts.task import list_tasks as _list_tasks, get_task as _get_task, VALID_STATUSES
from scripts.chat import get_messages as _get_messages, get_task_stats as _get_task_stats
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
        max_concurrent = int(os.environ.get("STANDUP_MAX_CONCURRENT", "3"))
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
            **stats,
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
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #e0e0e0; }
  .header { background: #1a1d28; padding: 16px 24px; border-bottom: 1px solid #2a2d3a; display: flex; align-items: center; gap: 16px; }
  .header h1 { font-size: 20px; font-weight: 600; }
  .tabs { display: flex; gap: 4px; margin-left: 32px; }
  .tab { padding: 8px 16px; cursor: pointer; border-radius: 6px 6px 0 0; background: transparent; border: none; color: #888; font-size: 14px; }
  .tab.active { background: #252836; color: #fff; }
  .content { max-width: 960px; margin: 24px auto; padding: 0 24px; }
  .panel { display: none; }
  .panel.active { display: block; }

  /* Tasks */
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #2a2d3a; }
  th { color: #888; font-weight: 500; font-size: 13px; text-transform: uppercase; }
  .badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 500; }
  .badge-open { background: #1e3a2f; color: #4ade80; }
  .badge-in_progress { background: #2a2520; color: #fbbf24; }
  .badge-review { background: #1e2a3a; color: #60a5fa; }
  .badge-done { background: #1a1d28; color: #888; }

  /* Chat */
  .chat-log { height: 500px; overflow-y: auto; background: #1a1d28; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
  .msg { margin-bottom: 8px; line-height: 1.5; }
  .msg .sender { font-weight: 600; color: #60a5fa; }
  .msg .event { color: #888; font-style: italic; }
  .msg .time { color: #555; font-size: 12px; margin-right: 8px; }
  .chat-input { display: flex; gap: 8px; }
  .chat-input input { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid #2a2d3a; background: #1a1d28; color: #e0e0e0; font-size: 14px; }
  .chat-input select { padding: 10px; border-radius: 8px; border: 1px solid #2a2d3a; background: #1a1d28; color: #e0e0e0; }
  .chat-input button { padding: 10px 20px; border-radius: 8px; border: none; background: #3b82f6; color: white; font-weight: 500; cursor: pointer; }
  .chat-input button:hover { background: #2563eb; }

  /* Agents */
  .agent-card { background: #1a1d28; border-radius: 8px; padding: 16px; margin-bottom: 8px; display: flex; align-items: center; gap: 16px; }
  .agent-name { font-weight: 600; min-width: 120px; }
  .agent-status { font-size: 13px; color: #888; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot-active { background: #4ade80; }
  .dot-idle { background: #555; }
</style>
</head>
<body>
<div class="header">
  <h1>Standup</h1>
  <div class="tabs">
    <button class="tab active" onclick="switchTab('chat')">Chat</button>
    <button class="tab" onclick="switchTab('tasks')">Tasks</button>
    <button class="tab" onclick="switchTab('agents')">Agents</button>
  </div>
</div>
<div class="content">
  <div id="chat" class="panel active">
    <div class="chat-log" id="chatLog"></div>
    <div class="chat-input">
      <select id="recipient"></select>
      <input type="text" id="msgInput" placeholder="Send a message..." onkeydown="if(event.key==='Enter')sendMsg()">
      <button onclick="sendMsg()">Send</button>
    </div>
  </div>
  <div id="tasks" class="panel"></div>
  <div id="agents" class="panel"></div>
</div>
<script>
function cap(s){return s.charAt(0).toUpperCase()+s.slice(1);}
function fmtStatus(s){return s.split('_').map(w=>cap(w)).join(' ');}
function fmtTime(iso){const d=new Date(iso);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function switchTab(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'tasks') loadTasks();
  if (name === 'chat') loadChat();
  if (name === 'agents') loadAgents();
}

async function loadTasks() {
  const res = await fetch('/tasks');
  const tasks = await res.json();
  const el = document.getElementById('tasks');
  if (!tasks.length) { el.innerHTML = '<p style="color:#888">No tasks yet.</p>'; return; }
  el.innerHTML = '<table><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Assignee</th><th>Project</th><th>Priority</th></tr></thead><tbody>'
    + tasks.map(t => `<tr>
      <td>task-${String(t.id).padStart(4,'0')}</td>
      <td>${esc(t.title)}</td>
      <td><span class="badge badge-${t.status}">${fmtStatus(t.status)}</span></td>
      <td>${t.assignee ? cap(t.assignee) : '\u2014'}</td>
      <td>${t.project || '\u2014'}</td>
      <td>${cap(t.priority)}</td>
    </tr>`).join('') + '</tbody></table>';
}

async function loadChat() {
  const res = await fetch('/messages');
  const msgs = await res.json();
  const log = document.getElementById('chatLog');
  log.innerHTML = msgs.map(m => {
    if (m.type === 'event') return `<div class="msg"><span class="time">${fmtTime(m.timestamp)}</span><span class="event">${esc(m.content)}</span></div>`;
    return `<div class="msg"><span class="time">${fmtTime(m.timestamp)}</span><span class="sender">${cap(m.sender)} \u2192 ${cap(m.recipient)}:</span> ${esc(m.content)}</div>`;
  }).join('');
  log.scrollTop = log.scrollHeight;

  // Populate recipient dropdown (default to the manager)
  const agentsRes = await fetch('/agents');
  const agents = await agentsRes.json();
  const sel = document.getElementById('recipient');
  const prev = sel.value;
  sel.innerHTML = agents.map(a => {
    const label = a.role === 'manager' ? `${cap(a.name)} (manager)` : cap(a.name);
    return `<option value="${a.name}">${label}</option>`;
  }).join('');
  // Restore previous selection or default to manager
  const mgr = agents.find(a => a.role === 'manager');
  sel.value = prev || (mgr ? mgr.name : agents[0]?.name || '');
}

async function loadAgents() {
  const res = await fetch('/agents');
  const agents = await res.json();
  const el = document.getElementById('agents');
  el.innerHTML = agents.map(a => `<div class="agent-card">
    <span class="dot ${a.pid ? 'dot-active' : 'dot-idle'}"></span>
    <span class="agent-name">${cap(a.name)}</span>
    <span class="agent-status">${a.pid ? 'Running (PID ' + a.pid + ')' : 'Idle'} \u00b7 ${a.unread_inbox} unread</span>
  </div>`).join('');
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

// Auto-refresh: poll chat + tasks every 2 seconds
setInterval(() => {
  const active = document.querySelector('.panel.active');
  if (active && active.id === 'chat') loadChat();
  if (active && active.id === 'tasks') loadTasks();
  if (active && active.id === 'agents') loadAgents();
}, 2000);

// Initial load
loadChat();
</script>
</body>
</html>
"""
