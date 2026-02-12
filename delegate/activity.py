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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = "agent_activity"
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
) -> None:
    """Push an activity entry to the ring buffer and notify all SSE clients.

    This is called from the turn execution loop in ``runtime.py`` for
    every tool invocation observed in the SDK response stream.

    Safe to call from any coroutine — the queue puts are non-blocking
    (entries are silently dropped for slow subscribers).
    """
    entry = ActivityEntry(agent=agent, team=team, tool=tool, detail=detail, task_id=task_id)
    _get_ring(team, agent).append(entry)
    _push_to_subscribers(entry.to_dict())


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


def broadcast_turn_event(
    event_type: str,
    agent: str,
    *,
    team: str = "",
    task_id: int | None = None,
    sender: str = "",
) -> None:
    """Broadcast a turn lifecycle event (turn_started or turn_ended) to all SSE clients.

    These are ephemeral signals (not stored in the ring buffer) that indicate when
    an agent begins or ends processing a batch of messages.

    Args:
        event_type: 'turn_started' or 'turn_ended'
        agent: The agent name
        team: The team this turn belongs to
        task_id: Optional task ID the turn is associated with
        sender: Optional sender name (relevant when task_id is None)
    """
    payload = {
        'type': event_type,
        'agent': agent,
        'team': team,
        'task_id': task_id,
        'sender': sender,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    _push_to_subscribers(payload)
