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
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-sans/400.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-sans/500.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-sans/600.css">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body { font-family: 'Geist Sans', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0b; color: #ededed; font-size: 14px; line-height: 1.5; letter-spacing: -0.01em; display: flex; flex-direction: column; -webkit-font-smoothing: antialiased; }
  .header { background: #111113; padding: 14px 24px; border-bottom: 1px solid rgba(255,255,255,0.08); display: flex; align-items: center; gap: 16px; flex-shrink: 0; }
  .header h1 { font-size: 16px; font-weight: 600; letter-spacing: -0.02em; color: #fafafa; }
  .tabs { display: flex; gap: 2px; margin-left: 32px; }
  .tab { padding: 7px 14px; cursor: pointer; border-radius: 6px; background: transparent; border: none; color: #666; font-family: inherit; font-size: 13px; font-weight: 500; transition: color 0.15s, background 0.15s; }
  .tab:hover { color: #999; background: rgba(255,255,255,0.04); }
  .tab.active { background: rgba(255,255,255,0.08); color: #fafafa; }
  .content { max-width: 1000px; width: 100%; margin: 0 auto; padding: 24px; flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .panel { display: none; }
  .panel.active { display: flex; flex-direction: column; flex: 1; min-height: 0; }

  /* Tasks */
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 13px; }
  th { color: #555; font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
  td { color: #a1a1a1; }
  tr:hover td { background: rgba(255,255,255,0.02); }
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
  .dot-active { background: #22c55e; box-shadow: 0 0 6px rgba(34,197,94,0.4); }
  .dot-idle { background: #333; }

  /* Chat filters */
  .chat-filters { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; flex-shrink: 0; }
  .chat-filters select { padding: 6px 10px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.1); background: #111113; color: #ededed; font-family: inherit; font-size: 12px; outline: none; cursor: pointer; transition: border-color 0.15s; }
  .chat-filters select:focus { border-color: rgba(255,255,255,0.25); }
  .chat-filters label { display: flex; align-items: center; gap: 6px; color: #666; font-size: 12px; cursor: pointer; user-select: none; transition: color 0.15s; }
  .chat-filters label:hover { color: #999; }
  .chat-filters input[type="checkbox"] { appearance: none; width: 14px; height: 14px; border: 1px solid rgba(255,255,255,0.15); border-radius: 3px; background: transparent; cursor: pointer; position: relative; transition: background 0.15s, border-color 0.15s; }
  .chat-filters input[type="checkbox"]:checked { background: #fafafa; border-color: #fafafa; }
  .chat-filters input[type="checkbox"]:checked::after { content: ''; position: absolute; top: 1px; left: 4px; width: 4px; height: 8px; border: solid #0a0a0b; border-width: 0 1.5px 1.5px 0; transform: rotate(45deg); }
  .filter-label { color: #444; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.18); }
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
  <div id="tasks" class="panel"></div>
  <div id="agents" class="panel"></div>
</div>
<script>
function cap(s){return s.charAt(0).toUpperCase()+s.slice(1);}
function fmtStatus(s){return s.split('_').map(w=>cap(w)).join(' ');}
function fmtTime(iso){const d=new Date(iso);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
const _avatarColors=['#e11d48','#7c3aed','#2563eb','#0891b2','#059669','#d97706','#dc2626','#4f46e5'];
function avatarColor(name){let h=0;for(let i=0;i<name.length;i++)h=name.charCodeAt(i)+((h<<5)-h);return _avatarColors[Math.abs(h)%_avatarColors.length];}
function avatarInitial(name){return name.charAt(0).toUpperCase();}
function switchTab(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  event.target.classList.add('active');
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

async function loadTasks() {
  const res = await fetch('/tasks');
  const tasks = await res.json();
  const el = document.getElementById('tasks');
  if (!tasks.length) { el.innerHTML = '<p style="color:#888">No tasks yet.</p>'; return; }

  // Fetch stats for in_progress and done tasks only (per director request)
  const showStats = new Set(['in_progress', 'done']);
  const statsMap = {};
  await Promise.all(tasks.filter(t => showStats.has(t.status)).map(async t => {
    try {
      const r = await fetch('/tasks/' + t.id + '/stats');
      if (r.ok) statsMap[t.id] = await r.json();
    } catch(e) { /* stats unavailable, show dashes */ }
  }));

  el.innerHTML = '<table><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Assignee</th><th>Project</th><th>Priority</th><th>Time</th><th>Tokens (in/out)</th><th>Cost</th></tr></thead><tbody>'
    + tasks.map(t => {
      const s = statsMap[t.id];
      return `<tr>
      <td>T${String(t.id).padStart(4,'0')}</td>
      <td>${esc(t.title)}</td>
      <td><span class="badge badge-${t.status}">${fmtStatus(t.status)}</span></td>
      <td>${t.assignee ? cap(t.assignee) : '\u2014'}</td>
      <td>${t.project || '\u2014'}</td>
      <td>${cap(t.priority)}</td>
      <td>${s ? fmtElapsed(s.elapsed_seconds) : '\u2014'}</td>
      <td>${s ? fmtTokens(s.total_tokens_in, s.total_tokens_out) : '\u2014'}</td>
      <td>${s ? fmtCost(s.total_cost_usd) : '\u2014'}</td>
    </tr>`;
    }).join('') + '</tbody></table>';
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
    if (m.type === 'event') return `<div class="msg-event"><span class="msg-event-line"></span><span class="msg-event-text">${fmtTime(m.timestamp)} \u2002${esc(m.content)}</span><span class="msg-event-line"></span></div>`;
    const c = avatarColor(m.sender);
    return `<div class="msg"><div class="msg-avatar" style="background:${c}">${avatarInitial(m.sender)}</div><div class="msg-body"><div class="msg-header"><span class="msg-sender">${cap(m.sender)}</span><span class="msg-recipient">\u2192 ${cap(m.recipient)}</span><span class="msg-time">${fmtTime(m.timestamp)}</span></div><div class="msg-content">${esc(m.content)}</div></div></div>`;
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
