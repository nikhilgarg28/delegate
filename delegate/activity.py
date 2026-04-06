"""In-memory agent activity ring buffer + SSE broadcast.

Provides:

- ``AgentActivityRing`` — fixed-size ring buffer per agent storing recent
  tool-usage entries (tool name, detail, timestamp).
- ``broadcast()`` — push an entry to the ring buffer and notify all SSE
  subscribers so the frontend can show live agent activity.
- ``subscribe()`` / ``unsubscribe()`` — manage per-client asyncio queues
  for the SSE endpoint.

The ring buffer and subscriber list are plain module-level state.  This is
safe because Delegate runs as a single process with a single event loop.

**Team scoping**: Every broadcast includes a ``team`` field.  SSE
subscribers are registered with a team filter so they only receive events
for the team they are watching.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Activity entry
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ActivityEntry:
    """Single tool-usage observation."""

    agent: str
    team: str
    tool: str
    detail: str
    task_id: int | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    diff: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = "agent_activity"
        # Omit diff key entirely if None to avoid bloating events for non-edit tools
        if d.get("diff") is None:
            del d["diff"]
        return d


# ---------------------------------------------------------------------------
# Ring buffer (per team+agent, in-memory)
# ---------------------------------------------------------------------------

RING_SIZE = 1024

# (team, agent) -> deque of ActivityEntry
_rings: dict[tuple[str, str], deque[ActivityEntry]] = {}


def _get_ring(team: str, agent: str) -> deque[ActivityEntry]:
    key = (team, agent)
    if key not in _rings:
        _rings[key] = deque(maxlen=RING_SIZE)
    return _rings[key]


def get_recent(team: str, agent: str, n: int = 50) -> list[dict[str, str]]:
    """Return the last *n* activity entries for an agent on a team (newest last)."""
    ring = _get_ring(team, agent)
    items = list(ring)[-n:]
    return [e.to_dict() for e in items]


def get_all_recent(n: int = 100) -> list[dict[str, str]]:
    """Return the last *n* entries across ALL agents, sorted by time."""
    all_entries: list[ActivityEntry] = []
    for ring in _rings.values():
        all_entries.extend(ring)
    all_entries.sort(key=lambda e: e.timestamp)
    return [e.to_dict() for e in all_entries[-n:]]


# ---------------------------------------------------------------------------
# SSE subscriber management
# ---------------------------------------------------------------------------

# Maps subscriber queue → team filter (None = receive everything).
_subscribers: dict[asyncio.Queue, str | None] = {}


def subscribe(team: str | None = None) -> asyncio.Queue:
    """Register a new SSE client, optionally filtered to *team*.

    Returns a queue to await on.  Only events whose ``team`` field matches
    (or events with no team, like keepalives) will be forwarded.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers[q] = team
    logger.debug("SSE subscriber added (team=%s, total=%d)", team, len(_subscribers))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Remove an SSE client queue."""
    _subscribers.pop(q, None)
    logger.debug("SSE subscriber removed (total=%d)", len(_subscribers))


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

def _push_to_subscribers(payload: dict) -> None:
    """Push a payload dict to matching SSE subscriber queues (non-blocking).

    If the payload contains a ``team`` key, it is only sent to subscribers
    whose team filter matches (or to unfiltered subscribers).
    """
    payload_team = payload.get("team")
    dead: list[asyncio.Queue] = []
    for q, sub_team in _subscribers.items():
        # Skip if subscriber is team-filtered and payload has a different team
        if sub_team is not None and payload_team is not None and sub_team != payload_team:
            continue
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # Slow consumer — drop oldest and retry
            try:
                q.get_nowait()
                q.put_nowait(payload)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                dead.append(q)

    for q in dead:
        _subscribers.pop(q, None)


def broadcast(
    agent: str,
    team: str,
    tool: str,
    detail: str,
    *,
    task_id: int | None = None,
    diff: list[str] | None = None,
) -> None:
    """Push an activity entry to the ring buffer and notify all SSE clients.

    This is called from the turn execution loop in ``runtime.py`` for
    every tool invocation observed in the SDK response stream.

    ``diff`` is an optional list of up to 3 unified-diff lines (``+``,
    ``-``, or context) extracted from the first hunk of an Edit/Write
    operation.  It is included in the SSE payload so the frontend can
    render colored diff snippets in agent cards.

    Safe to call from any coroutine — the queue puts are non-blocking
    (entries are silently dropped for slow subscribers).
    """
    entry = ActivityEntry(agent=agent, team=team, tool=tool, detail=detail, task_id=task_id, diff=diff)
    _get_ring(team, agent).append(entry)
    _push_to_subscribers(entry.to_dict())


def broadcast_rate_limit(agent: str, team: str) -> None:
    """Broadcast a rate-limit notification to all SSE clients.

    Called when the SDK monkey-patch intercepts a ``rate_limit_event``
    message from the Claude Code CLI.  The frontend shows a transient
    toast so the user knows Claude is being throttled (the CLI retries
    automatically, so no action is needed).
    """
    payload = {
        "type": "rate_limit",
        "agent": agent,
        "team": team,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _push_to_subscribers(payload)


def broadcast_msg_status(
    team: str,
    msg_ids: list[int],
    field: str,
    timestamp: str,
) -> None:
    """Broadcast message status changes to SSE clients.

    Called from ``runtime.py`` after ``mark_seen_batch`` or
    ``mark_processed_batch``.  The payload carries just the message IDs
    and the updated field so the frontend can patch in-place without
    re-fetching.

    Args:
        team: Team the messages belong to.
        msg_ids: List of message IDs whose status changed.
        field: ``"seen_at"`` or ``"processed_at"``.
        timestamp: ISO timestamp that was written to the DB.
    """
    if not msg_ids:
        return
    _push_to_subscribers({
        "type": "msg_status",
        "team": team,
        "msg_ids": msg_ids,
        "field": field,
        "value": timestamp,
        "timestamp": timestamp,
    })


def broadcast_task_update(task_id: int, team: str, changes: dict[str, Any]) -> None:
    """Broadcast a task mutation to all SSE clients.

    ``changes`` should be a dict of the fields that changed, e.g.
    ``{"status": "merging", "assignee": "manager"}``.

    The SSE event has ``type: "task_update"`` so the frontend can
    distinguish it from agent activity events.
    """
    payload = {
        "type": "task_update",
        "task_id": task_id,
        "team": team,
        **changes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _push_to_subscribers(payload)


# Per-agent thinking buffer: accumulates text blocks across a turn so the
# frontend receives the full stream (not just the latest snippet).  Keyed
# by (team, agent).  Cleared automatically when a turn_ended event is
# broadcast.
_thinking_buffer: dict[tuple[str, str], str] = {}

# Tracks whether tool calls happened since the last thinking append for
# a given (team, agent).  When set, the next thinking append inserts a
# paragraph break (rendered as <hr> in markdown) to visually separate
# logical blocks.
_thinking_had_tools: dict[tuple[str, str], bool] = {}


def mark_thinking_tool_break(agent: str, team: str) -> None:
    """Signal that a tool call happened — next thinking append gets a break."""
    _thinking_had_tools[(team, agent)] = True


def broadcast_thinking(
    agent: str,
    team: str,
    text: str,
    *,
    task_id: int | None = None,
) -> None:
    """Broadcast accumulated thinking text to SSE clients (ephemeral, not stored).

    Called from the turn execution loop for every text block in the SDK
    response stream.  Each call appends to a per-agent buffer and sends
    the full accumulated text so the frontend can render a continuous
    scrolling stream.

    If tool calls happened since the last thinking append, a paragraph
    break (``---``) is inserted to visually separate logical blocks.
    """
    key = (team, agent)
    buf = _thinking_buffer.get(key, "")
    if buf and _thinking_had_tools.pop(key, False):
        buf += "\n\n---\n\n"
    buf += text
    _thinking_buffer[key] = buf
    payload = {
        "type": "agent_thinking",
        "agent": agent,
        "team": team,
        "text": buf,
        "task_id": task_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _push_to_subscribers(payload)


def clear_thinking_buffer(agent: str, team: str) -> None:
    """Clear the accumulated thinking buffer for an agent (call on turn end)."""
    _thinking_buffer.pop((team, agent), None)
    _thinking_had_tools.pop((team, agent), None)


def broadcast_teams_refresh() -> None:
    """Notify all SSE clients to re-fetch the teams/projects list.

    Called after a new project is created via the UI so all open tabs
    update their sidebar immediately.
    """
    payload = {
        "type": "teams_refresh",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _push_to_subscribers(payload)


# ---------------------------------------------------------------------------
# Active turns registry — queryable snapshot of who is currently working
# ---------------------------------------------------------------------------

# (team, agent) -> { agent, team, task_id, sender, timestamp }
_active_turns: dict[tuple[str, str], dict[str, Any]] = {}


def get_active_turns(team: str | None = None) -> list[dict[str, Any]]:
    """Return a list of currently active turns.

    If *team* is given, only turns for that team are returned.
    Each entry mirrors the ``turn_started`` SSE payload.
    """
    if team is None:
        return list(_active_turns.values())
    return [t for t in _active_turns.values() if t.get("team") == team]


def broadcast_turn_event(
    event_type: str,
    agent: str,
    *,
    team: str = "",
    task_id: int | None = None,
    sender: str = "",
) -> None:
    """Broadcast a turn lifecycle event to all SSE clients.

    Also maintains the in-memory ``_active_turns`` registry so that
    ``get_active_turns()`` always reflects who is currently in a turn.
    This allows the ``/turns/active`` REST endpoint to return the current
    state for clients that missed SSE events (e.g. on reconnect).

    Args:
        event_type: 'turn_started', 'turn_ended', or 'turn_interrupted'
        agent: The agent name
        team: The team this turn belongs to
        task_id: Optional task ID the turn is associated with
        sender: Optional sender name (relevant when task_id is None)
    """
    key = (team, agent)
    ts = datetime.now(timezone.utc).isoformat()

    if event_type == "turn_started":
        _active_turns[key] = {
            "type": "turn_started",
            "agent": agent,
            "team": team,
            "task_id": task_id,
            "sender": sender,
            "timestamp": ts,
        }
    elif event_type in ("turn_ended", "turn_interrupted"):
        _active_turns.pop(key, None)

    payload = {
        'type': event_type,
        'agent': agent,
        'team': team,
        'task_id': task_id,
        'sender': sender,
        'timestamp': ts,
    }
    _push_to_subscribers(payload)
