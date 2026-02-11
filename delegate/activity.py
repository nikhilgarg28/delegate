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
    tool: str
    detail: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Ring buffer (per-agent, in-memory)
# ---------------------------------------------------------------------------

RING_SIZE = 1024

# agent_name -> deque of ActivityEntry
_rings: dict[str, deque[ActivityEntry]] = {}


def _get_ring(agent: str) -> deque[ActivityEntry]:
    if agent not in _rings:
        _rings[agent] = deque(maxlen=RING_SIZE)
    return _rings[agent]


def get_recent(agent: str, n: int = 50) -> list[dict[str, str]]:
    """Return the last *n* activity entries for an agent (newest last)."""
    ring = _get_ring(agent)
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

# Each subscriber is an asyncio.Queue that receives ActivityEntry dicts
_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    """Register a new SSE client. Returns a queue to await on."""
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.add(q)
    logger.debug("SSE subscriber added (total=%d)", len(_subscribers))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Remove an SSE client queue."""
    _subscribers.discard(q)
    logger.debug("SSE subscriber removed (total=%d)", len(_subscribers))


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

def broadcast(agent: str, tool: str, detail: str) -> None:
    """Push an activity entry to the ring buffer and notify all SSE clients.

    This is called from the turn execution loop in ``runtime.py`` for
    every tool invocation observed in the SDK response stream.

    Safe to call from any coroutine — the queue puts are non-blocking
    (entries are silently dropped for slow subscribers).
    """
    entry = ActivityEntry(agent=agent, tool=tool, detail=detail)
    _get_ring(agent).append(entry)

    payload = entry.to_dict()
    dead: list[asyncio.Queue] = []
    for q in _subscribers:
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
        _subscribers.discard(q)
