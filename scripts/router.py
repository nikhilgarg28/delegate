"""Daemon message router — polls agent outboxes and delivers to recipient inboxes.

This module contains the routing logic as callable functions (one poll cycle)
so it can be tested without running the full daemon event loop.

The actual event loop is in scripts/run.py.
"""

import logging
from pathlib import Path

from scripts.mailbox import (
    Message,
    read_outbox,
    deliver,
    mark_outbox_routed,
)
from scripts.chat import log_message, log_event


logger = logging.getLogger(__name__)


def _list_agents(root: Path) -> list[str]:
    """List all agent names from the team directory."""
    team_dir = root / ".standup" / "team"
    if not team_dir.is_dir():
        return []
    return [d.name for d in sorted(team_dir.iterdir()) if d.is_dir()]


class DirectorQueue:
    """In-memory queue for messages addressed to the director."""

    def __init__(self):
        self.messages: list[Message] = []

    def put(self, msg: Message) -> None:
        self.messages.append(msg)

    def get_all(self) -> list[Message]:
        msgs = list(self.messages)
        self.messages.clear()
        return msgs

    def peek(self) -> list[Message]:
        return list(self.messages)


def route_once(root: Path, director_queue: DirectorQueue | None = None) -> int:
    """Run one routing cycle: scan all outboxes, deliver pending messages.

    Every team member (including the director) is treated uniformly:
    messages go from sender's outbox → recipient's inbox.

    The DirectorQueue is an optional notification channel so the web UI
    can push real-time updates when a message arrives for the director.

    Returns the number of messages routed in this cycle.
    """
    agents = _list_agents(root)
    routed = 0

    for agent in agents:
        pending = read_outbox(root, agent, pending_only=True)

        for msg in pending:
            try:
                deliver(root, msg)
                log_message(root, msg.sender, msg.recipient, msg.body)
            except ValueError as e:
                logger.error(
                    "Failed to deliver message from %s to %s: %s",
                    msg.sender, msg.recipient, e,
                )
                log_event(
                    root,
                    f"Failed to deliver: {msg.sender} -> {msg.recipient}: {e}",
                )

            # Notify the web UI when a message arrives for the director
            if msg.recipient == "director" and director_queue is not None:
                director_queue.put(msg)

            # Mark as routed in sender's outbox
            if msg.filename:
                mark_outbox_routed(root, agent, msg.filename)
            routed += 1

    return routed
