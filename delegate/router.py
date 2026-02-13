"""Daemon message router — lightweight routing cycle for the daemon loop.

With the SQLite-backed mailbox, ``send()`` delivers messages immediately.
The router's remaining job is to notify the in-memory HumanQueue so the
web UI can push real-time updates when a message arrives for a human member.

The actual event loop is in delegate/daemon.py.
"""

import logging
from pathlib import Path

from delegate.mailbox import Message, read_inbox

logger = logging.getLogger(__name__)


class HumanQueue:
    """In-memory queue for messages addressed to a human member."""

    def __init__(self):
        self.messages: list[Message] = []
        self._seen_ids: set[int] = set()

    def put(self, msg: Message) -> None:
        if msg.id is not None and msg.id not in self._seen_ids:
            self.messages.append(msg)
            self._seen_ids.add(msg.id)

    def get_all(self) -> list[Message]:
        msgs = list(self.messages)
        self.messages.clear()
        return msgs

    def peek(self) -> list[Message]:
        return list(self.messages)


# Backward-compat alias
BossQueue = HumanQueue


def route_once(
    hc_home: Path,
    team: str,
    boss_queue: HumanQueue | None = None,  # deprecated — use human_queue
    boss_name: str | None = None,  # deprecated — use human_name
    *,
    human_queue: HumanQueue | None = None,
    human_name: str | None = None,
) -> int:
    """Run one routing cycle.

    With immediate delivery in ``send()``, the only remaining work is to
    check for new unread messages addressed to the human and push them to
    the HumanQueue for web UI notifications.

    Returns the number of new human messages found in this cycle.
    """
    queue = human_queue or boss_queue
    name = human_name or boss_name

    if name is None:
        from delegate.config import get_default_human
        name = get_default_human(hc_home)

    if not name or queue is None:
        return 0

    # Check for unread messages addressed to the human
    unread = read_inbox(hc_home, team, name, unread_only=True)
    notified = 0
    for msg in unread:
        queue.put(msg)
        notified += 1

    if notified > 0:
        logger.debug(
            "Human notification cycle | team=%s | new_messages=%d",
            team, notified,
        )

    return notified
