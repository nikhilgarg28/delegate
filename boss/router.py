"""Daemon message router — polls agent outboxes and delivers to recipient inboxes.

This module contains the routing logic as callable functions (one poll cycle)
so it can be tested without running the full daemon event loop.

The actual event loop is in boss/daemon.py.
"""

import logging
from pathlib import Path

from boss.paths import agents_dir as _agents_dir
from boss.mailbox import (
    Message,
    read_outbox,
    deliver,
    mark_outbox_routed,
)
from boss.chat import log_message, log_event


logger = logging.getLogger(__name__)


def _list_agents(hc_home: Path, team: str) -> list[str]:
    """List all agent names from the team's agents directory."""
    ad = _agents_dir(hc_home, team)
    if not ad.is_dir():
        return []
    return [d.name for d in sorted(ad.iterdir()) if d.is_dir()]


class BossQueue:
    """In-memory queue for messages addressed to the boss."""

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


def route_once(
    hc_home: Path,
    team: str,
    boss_queue: BossQueue | None = None,
    boss_name: str | None = None,
) -> int:
    """Run one routing cycle: scan all outboxes, deliver pending messages.

    Scans two sources:
    1. Every team agent's outbox (agent-to-agent and agent-to-boss).
    2. The org-wide boss's outbox (boss-to-agent for this team).

    The BossQueue is an optional notification channel so the web UI
    can push real-time updates when a message arrives for the boss.

    Since agent names are globally unique, a boss message is only
    routed when its recipient belongs to the current team.

    Returns the number of messages routed in this cycle.
    """
    agents = _list_agents(hc_home, team)
    agent_set = set(agents)
    routed = 0

    # Resolve boss name if not provided
    if boss_name is None:
        from boss.config import get_boss
        boss_name = get_boss(hc_home)

    # --- Boss outbox: route messages to agents in this team ---
    if boss_name:
        try:
            boss_pending = read_outbox(hc_home, team, boss_name, pending_only=True)
        except ValueError:
            boss_pending = []

        for msg in boss_pending:
            # Only route if the recipient is in this team
            if msg.recipient not in agent_set:
                continue
            try:
                deliver(hc_home, team, msg)
                log_message(hc_home, msg.sender, msg.recipient, msg.body)
            except ValueError as e:
                logger.error(
                    "Failed to deliver boss message to %s: %s",
                    msg.recipient, e,
                )
                log_event(
                    hc_home,
                    f"Message delivery failed: {msg.sender.capitalize()} \u2192 {msg.recipient.capitalize()}",
                )

            if msg.filename:
                mark_outbox_routed(hc_home, team, boss_name, msg.filename)
            routed += 1

    # --- Team agent outboxes ---
    for agent in agents:
        pending = read_outbox(hc_home, team, agent, pending_only=True)

        for msg in pending:
            try:
                deliver(hc_home, team, msg)
                log_message(hc_home, msg.sender, msg.recipient, msg.body)
            except ValueError as e:
                logger.error(
                    "Failed to deliver message from %s to %s: %s",
                    msg.sender, msg.recipient, e,
                )
                log_event(
                    hc_home,
                    f"Message delivery failed: {msg.sender.capitalize()} \u2192 {msg.recipient.capitalize()}",
                )

            # Notify the web UI when a message arrives for the boss
            if boss_name and msg.recipient == boss_name and boss_queue is not None:
                boss_queue.put(msg)

            # Mark as routed in sender's outbox
            if msg.filename:
                mark_outbox_routed(hc_home, team, agent, msg.filename)
            routed += 1

    return routed
