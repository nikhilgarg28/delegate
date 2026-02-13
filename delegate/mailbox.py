"""SQLite-backed mailbox system for agent communication.

Each message is a row in the team's ``messages`` table (type='chat') with lifecycle columns:
    timestamp    — when the sender wrote the message
    delivered_at — when it was made available to the recipient
    seen_at      — when the agent's control loop picked it up (turn start)
    processed_at — when the agent finished the turn (message is "done")

``send()`` inserts with ``delivered_at = NOW`` (immediate delivery).
Messages are both stored in and retrieved from the unified messages table.

Usage:
    python -m delegate.mailbox send <home> <team> <sender> <recipient> <message>
    python -m delegate.mailbox inbox <home> <team> <agent> [--all]
    python -m delegate.mailbox outbox <home> <team> <agent> [--all]
"""

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from delegate.db import get_connection

logger = logging.getLogger(__name__)


@dataclass
class Message:
    sender: str
    recipient: str
    time: str
    body: str
    id: int | None = None
    delivered_at: str | None = None
    seen_at: str | None = None
    processed_at: str | None = None
    task_id: int | None = None

    def serialize(self) -> str:
        """Serialize message to the legacy file format (used in tests/logs)."""
        return f"sender: {self.sender}\nrecipient: {self.recipient}\ntime: {self.time}\n---\n{self.body}"

    @classmethod
    def deserialize(cls, text: str) -> "Message":
        """Parse a message from the legacy file format."""
        header, _, body = text.partition("\n---\n")
        fields = {}
        for line in header.strip().splitlines():
            key, _, value = line.partition(": ")
            fields[key.strip()] = value.strip()
        return cls(
            sender=fields["sender"],
            recipient=fields["recipient"],
            time=fields["time"],
            body=body,
        )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row_to_message(row) -> Message:
    """Convert a messages DB row to a Message dataclass."""
    return Message(
        sender=row["sender"],
        recipient=row["recipient"],
        time=row["timestamp"],
        body=row["content"],
        id=row["id"],
        delivered_at=row["delivered_at"] if "delivered_at" in row.keys() else None,
        seen_at=row["seen_at"] if "seen_at" in row.keys() else None,
        processed_at=row["processed_at"] if "processed_at" in row.keys() else None,
        task_id=row["task_id"] if "task_id" in row.keys() else None,
    )


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def send(
    hc_home: Path,
    team: str,
    sender: str,
    recipient: str,
    message: str,
    *,
    task_id: int | None = None,
) -> int:
    """Send a message by inserting into the team's messages table.

    Messages are delivered immediately (``delivered_at`` set on insert).
    Single write to the unified messages table.

    Returns the message id.
    """
    # Soft validation: warn when non-human messages lack task_id
    # System user messages are always valid (automated events).
    from delegate.config import SYSTEM_USER
    if task_id is None and sender != SYSTEM_USER and recipient != SYSTEM_USER:
        try:
            from delegate.config import get_default_human
            human = get_default_human(hc_home)
        except Exception:
            human = "human"
        if sender != human and recipient != human:
            logger.warning(
                "Message from %s to %s has no task_id — consider using --task",
                sender, recipient,
            )

    now = _now()
    conn = get_connection(hc_home, team)
    try:
        cursor = conn.execute(
            """\
            INSERT INTO messages (sender, recipient, content, type, task_id, delivered_at, team)
            VALUES (?, ?, ?, 'chat', ?, ?, ?)""",
            (sender, recipient, message, task_id, now, team),
        )
        conn.commit()
        msg_id = cursor.lastrowid
    finally:
        conn.close()

    return msg_id


def read_inbox(
    hc_home: Path, team: str, agent: str, unread_only: bool = True,
) -> list[Message]:
    """Read messages from an agent's inbox (messages addressed to them).

    If *unread_only* is True, returns only unprocessed messages.
    Messages must be delivered (``delivered_at IS NOT NULL``) to be visible.
    Filters by team to ensure cross-team isolation.
    """
    conn = get_connection(hc_home, team)
    try:
        if unread_only:
            rows = conn.execute(
                "SELECT * FROM messages WHERE type = 'chat' AND team = ? AND recipient = ? AND delivered_at IS NOT NULL AND processed_at IS NULL ORDER BY id ASC",
                (team, agent),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE type = 'chat' AND team = ? AND recipient = ? AND delivered_at IS NOT NULL ORDER BY id ASC",
                (team, agent),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_message(r) for r in rows]


def read_outbox(
    hc_home: Path, team: str, agent: str, pending_only: bool = True,
) -> list[Message]:
    """Read messages from an agent's outbox (messages sent by them).

    If *pending_only* is True, returns only undelivered messages
    (``delivered_at IS NULL``).
    Filters by team to ensure cross-team isolation.
    """
    conn = get_connection(hc_home, team)
    try:
        if pending_only:
            rows = conn.execute(
                "SELECT * FROM messages WHERE type = 'chat' AND team = ? AND sender = ? AND delivered_at IS NULL ORDER BY id ASC",
                (team, agent),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE type = 'chat' AND team = ? AND sender = ? ORDER BY id ASC",
                (team, agent),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_message(r) for r in rows]


def mark_seen(hc_home: Path, team: str, msg_id: int) -> None:
    """Mark a message as seen (agent control loop picked it up at turn start)."""
    conn = get_connection(hc_home, team)
    try:
        conn.execute(
            "UPDATE messages SET seen_at = ? WHERE id = ? AND type = 'chat' AND seen_at IS NULL",
            (_now(), msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_seen_batch(hc_home: Path, team: str, msg_ids: list[int]) -> None:
    """Mark multiple messages as seen in a single transaction."""
    if not msg_ids:
        return
    now = _now()
    conn = get_connection(hc_home, team)
    try:
        conn.executemany(
            "UPDATE messages SET seen_at = ? WHERE id = ? AND type = 'chat' AND seen_at IS NULL",
            [(now, mid) for mid in msg_ids],
        )
        conn.commit()
    finally:
        conn.close()


def mark_processed(hc_home: Path, team: str, msg_id: int) -> None:
    """Mark a message as processed (agent finished the turn)."""
    conn = get_connection(hc_home, team)
    try:
        conn.execute(
            "UPDATE messages SET processed_at = ? WHERE id = ? AND type = 'chat' AND processed_at IS NULL",
            (_now(), msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_processed_batch(hc_home: Path, team: str, msg_ids: list[int]) -> None:
    """Mark multiple messages as processed in a single transaction."""
    if not msg_ids:
        return
    now = _now()
    conn = get_connection(hc_home, team)
    try:
        conn.executemany(
            "UPDATE messages SET processed_at = ? WHERE id = ? AND type = 'chat' AND processed_at IS NULL",
            [(now, mid) for mid in msg_ids],
        )
        conn.commit()
    finally:
        conn.close()


def mark_outbox_routed(
    hc_home: Path, team: str, agent: str, msg_id: int,
) -> None:
    """Mark a message as delivered/routed (set delivered_at).

    With immediate delivery in ``send()``, this is typically a no-op.
    Kept for backward compatibility.
    """
    conn = get_connection(hc_home, team)
    try:
        conn.execute(
            "UPDATE messages SET delivered_at = ? WHERE id = ? AND type = 'chat' AND delivered_at IS NULL",
            (_now(), msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def deliver(hc_home: Path, team: str, message: Message) -> int:
    """Deliver a message directly to the recipient's inbox.

    Equivalent to ``send()`` but takes a pre-built Message object.
    Uses ``message.task_id`` if set.
    Used by notification helpers that construct Messages themselves.

    Returns the message id.
    """
    now = _now()
    conn = get_connection(hc_home, team)
    try:
        cursor = conn.execute(
            """\
            INSERT INTO messages (sender, recipient, content, type, task_id, delivered_at, team)
            VALUES (?, ?, ?, 'chat', ?, ?, ?)""",
            (message.sender, message.recipient, message.body, message.task_id, now, team),
        )
        conn.commit()
        msg_id = cursor.lastrowid
    finally:
        conn.close()
    return msg_id


def recent_processed(
    hc_home: Path,
    team: str,
    agent: str,
    from_sender: str | None = None,
    limit: int = 10,
) -> list[Message]:
    """Return recently processed messages for context building.

    If *from_sender* is specified, only return messages from that sender.
    Otherwise return messages from any sender. Results are ordered newest-first.
    """
    conn = get_connection(hc_home, team)
    try:
        if from_sender:
            rows = conn.execute(
                "SELECT * FROM messages WHERE type = 'chat' AND recipient = ? AND sender = ? "
                "AND processed_at IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (agent, from_sender, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE type = 'chat' AND recipient = ? "
                "AND processed_at IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
    finally:
        conn.close()
    # Return in chronological order (oldest first)
    return [_row_to_message(r) for r in reversed(rows)]


def recent_conversation(
    hc_home: Path,
    team: str,
    agent: str,
    peer: str | None = None,
    limit: int = 10,
) -> list[Message]:
    """Return recent bidirectional messages (sent AND received) for context.

    When *peer* is specified, returns messages between *agent* and *peer*
    (in either direction).  Otherwise returns all recent messages involving
    *agent*.

    Only processed incoming messages are included (unprocessed ones haven't
    been acted on yet).  Outgoing messages are always included.

    Results are in chronological order (oldest first).
    """
    conn = get_connection(hc_home, team)
    try:
        if peer:
            rows = conn.execute(
                "SELECT * FROM messages WHERE type = 'chat' AND "
                "((recipient = ? AND sender = ? AND processed_at IS NOT NULL) "
                " OR (sender = ? AND recipient = ?)) "
                "ORDER BY id DESC LIMIT ?",
                (agent, peer, agent, peer, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE type = 'chat' AND "
                "((recipient = ? AND processed_at IS NOT NULL) "
                "OR sender = ?) "
                "ORDER BY id DESC LIMIT ?",
                (agent, agent, limit),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_message(r) for r in reversed(rows)]


def has_unread(hc_home: Path, team: str, agent: str) -> bool:
    """Check if an agent has any unread delivered messages.

    Fast path for the orchestrator — avoids fetching full message content.
    """
    conn = get_connection(hc_home, team)
    try:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE type = 'chat' AND recipient = ? AND delivered_at IS NOT NULL AND processed_at IS NULL LIMIT 1",
            (agent,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def agents_with_unread(hc_home: Path, team: str) -> list[str]:
    """Return all recipient names that have at least one unread message.

    Single query — used by the daemon to find every agent needing a turn.
    """
    conn = get_connection(hc_home, team)
    try:
        rows = conn.execute(
            "SELECT DISTINCT recipient FROM messages "
            "WHERE type = 'chat' AND delivered_at IS NOT NULL AND processed_at IS NULL",
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def count_unread(hc_home: Path, team: str, agent: str) -> int:
    """Count unread delivered messages for an agent."""
    conn = get_connection(hc_home, team)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE type = 'chat' AND recipient = ? AND delivered_at IS NOT NULL AND processed_at IS NULL",
            (agent,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Mailbox management")
    sub = parser.add_subparsers(dest="command", required=True)

    # send
    p_send = sub.add_parser("send", help="Send a message")
    p_send.add_argument("home", type=Path)
    p_send.add_argument("team")
    p_send.add_argument("sender", help="Sending agent name")
    p_send.add_argument("recipient", help="Recipient agent name")
    p_send.add_argument("message", help="Message body")
    p_send.add_argument("--task", type=int, default=None, help="Task ID to associate with this message")

    # inbox
    p_inbox = sub.add_parser("inbox", help="Read inbox")
    p_inbox.add_argument("home", type=Path)
    p_inbox.add_argument("team")
    p_inbox.add_argument("agent", help="Agent name")
    p_inbox.add_argument("--all", action="store_true", help="Include read messages")

    # outbox
    p_outbox = sub.add_parser("outbox", help="Read outbox")
    p_outbox.add_argument("home", type=Path)
    p_outbox.add_argument("team")
    p_outbox.add_argument("agent", help="Agent name")
    p_outbox.add_argument("--all", action="store_true", help="Include all messages")

    args = parser.parse_args()

    if args.command == "send":
        msg_id = send(args.home, args.team, args.sender, args.recipient, args.message, task_id=args.task)
        print(f"Message sent: {msg_id}")

    elif args.command == "inbox":
        messages = read_inbox(args.home, args.team, args.agent, unread_only=not args.all)
        for msg in messages:
            print(f"[{msg.time}] {msg.sender}: {msg.body}")
        if not messages:
            print("(no messages)")

    elif args.command == "outbox":
        messages = read_outbox(args.home, args.team, args.agent, pending_only=not args.all)
        for msg in messages:
            print(f"[{msg.time}] -> {msg.recipient}: {msg.body}")
        if not messages:
            print("(no messages)")


if __name__ == "__main__":
    main()
