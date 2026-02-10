"""SQLite-backed mailbox system for agent communication.

Each message is a row in the ``mailbox`` table with lifecycle columns:
    created_at   — when the sender wrote the message
    delivered_at — when it was made available to the recipient
    seen_at      — when the agent's control loop picked it up (turn start)
    processed_at — when the agent finished the turn (message is "done")

``send()`` inserts with ``delivered_at = NOW`` (immediate delivery).
The router is no longer required for delivery — it only handles
supplementary tasks like logging to the chat table.

Usage:
    python -m delegate.mailbox send <home> <team> <sender> <recipient> <message>
    python -m delegate.mailbox inbox <home> <team> <agent> [--all]
    python -m delegate.mailbox outbox <home> <team> <agent> [--all]
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from delegate.db import get_connection


@dataclass
class Message:
    sender: str
    recipient: str
    time: str
    body: str
    id: int | None = None
    # Legacy compat: some call sites still reference .filename
    filename: str | None = None
    delivered_at: str | None = None
    seen_at: str | None = None
    processed_at: str | None = None

    def serialize(self) -> str:
        """Serialize message to the legacy file format (used in tests/logs)."""
        return f"sender: {self.sender}\nrecipient: {self.recipient}\ntime: {self.time}\n---\n{self.body}"

    @classmethod
    def deserialize(cls, text: str, filename: str | None = None) -> "Message":
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
            filename=filename,
        )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row_to_message(row) -> Message:
    """Convert a mailbox DB row to a Message dataclass."""
    return Message(
        sender=row["sender"],
        recipient=row["recipient"],
        time=row["created_at"],
        body=row["body"],
        id=row["id"],
        filename=str(row["id"]),  # backwards compat for code using .filename
        delivered_at=row["delivered_at"],
        seen_at=row["seen_at"],
        processed_at=row["processed_at"],
    )


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def send(hc_home: Path, team: str, sender: str, recipient: str, message: str) -> str:
    """Send a message by inserting into the mailbox table.

    Messages are delivered immediately (``delivered_at`` set on insert).
    Also logs the message to the chat event stream.

    Returns the message id as a string (for backward compatibility with
    code that expects a filename).
    """
    now = _now()
    conn = get_connection(hc_home)
    try:
        cursor = conn.execute(
            """\
            INSERT INTO mailbox (sender, recipient, body, created_at, delivered_at)
            VALUES (?, ?, ?, ?, ?)""",
            (sender, recipient, message, now, now),
        )
        conn.commit()
        msg_id = cursor.lastrowid
    finally:
        conn.close()

    # Log to the chat event stream
    from delegate.chat import log_message
    log_message(hc_home, sender, recipient, message)

    return str(msg_id)


def read_inbox(
    hc_home: Path, team: str, agent: str, unread_only: bool = True,
) -> list[Message]:
    """Read messages from an agent's inbox (messages addressed to them).

    If *unread_only* is True, returns only unprocessed messages.
    Messages must be delivered (``delivered_at IS NOT NULL``) to be visible.
    """
    conn = get_connection(hc_home)
    try:
        if unread_only:
            rows = conn.execute(
                "SELECT * FROM mailbox WHERE recipient = ? AND delivered_at IS NOT NULL AND processed_at IS NULL ORDER BY id ASC",
                (agent,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mailbox WHERE recipient = ? AND delivered_at IS NOT NULL ORDER BY id ASC",
                (agent,),
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
    """
    conn = get_connection(hc_home)
    try:
        if pending_only:
            rows = conn.execute(
                "SELECT * FROM mailbox WHERE sender = ? AND delivered_at IS NULL ORDER BY id ASC",
                (agent,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mailbox WHERE sender = ? ORDER BY id ASC",
                (agent,),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_message(r) for r in rows]


def mark_seen(hc_home: Path, msg_identifier: str) -> None:
    """Mark a message as seen (agent control loop picked it up at turn start).

    *msg_identifier* is the message id as a string.
    """
    msg_id = int(msg_identifier)
    conn = get_connection(hc_home)
    try:
        conn.execute(
            "UPDATE mailbox SET seen_at = ? WHERE id = ? AND seen_at IS NULL",
            (_now(), msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_seen_batch(hc_home: Path, msg_ids: list[str]) -> None:
    """Mark multiple messages as seen in a single transaction."""
    if not msg_ids:
        return
    now = _now()
    conn = get_connection(hc_home)
    try:
        conn.executemany(
            "UPDATE mailbox SET seen_at = ? WHERE id = ? AND seen_at IS NULL",
            [(now, int(mid)) for mid in msg_ids],
        )
        conn.commit()
    finally:
        conn.close()


def mark_processed(hc_home: Path, msg_identifier: str) -> None:
    """Mark a message as processed (agent finished the turn).

    *msg_identifier* is the message id as a string.
    """
    msg_id = int(msg_identifier)
    conn = get_connection(hc_home)
    try:
        conn.execute(
            "UPDATE mailbox SET processed_at = ? WHERE id = ? AND processed_at IS NULL",
            (_now(), msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_processed_batch(hc_home: Path, msg_ids: list[str]) -> None:
    """Mark multiple messages as processed in a single transaction."""
    if not msg_ids:
        return
    now = _now()
    conn = get_connection(hc_home)
    try:
        conn.executemany(
            "UPDATE mailbox SET processed_at = ? WHERE id = ? AND processed_at IS NULL",
            [(now, int(mid)) for mid in msg_ids],
        )
        conn.commit()
    finally:
        conn.close()


def mark_outbox_routed(
    hc_home: Path, team: str, agent: str, msg_identifier: str,
) -> None:
    """Mark a message as delivered/routed (set delivered_at).

    With immediate delivery in ``send()``, this is typically a no-op.
    Kept for backward compatibility.
    """
    msg_id = int(msg_identifier)
    conn = get_connection(hc_home)
    try:
        conn.execute(
            "UPDATE mailbox SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
            (_now(), msg_id),
        )
        conn.commit()
    finally:
        conn.close()


def deliver(hc_home: Path, team: str, message: Message) -> str:
    """Deliver a message directly to the recipient's inbox.

    Equivalent to ``send()`` but takes a pre-built Message object.
    Used by notification helpers that construct Messages themselves.

    Returns the message id as a string.
    """
    now = _now()
    conn = get_connection(hc_home)
    try:
        cursor = conn.execute(
            """\
            INSERT INTO mailbox (sender, recipient, body, created_at, delivered_at)
            VALUES (?, ?, ?, ?, ?)""",
            (message.sender, message.recipient, message.body, message.time or now, now),
        )
        conn.commit()
        msg_id = cursor.lastrowid
    finally:
        conn.close()
    return str(msg_id)


def has_unread(hc_home: Path, agent: str) -> bool:
    """Check if an agent has any unread delivered messages.

    Fast path for the orchestrator — avoids fetching full message content.
    """
    conn = get_connection(hc_home)
    try:
        row = conn.execute(
            "SELECT 1 FROM mailbox WHERE recipient = ? AND delivered_at IS NOT NULL AND processed_at IS NULL LIMIT 1",
            (agent,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def count_unread(hc_home: Path, agent: str) -> int:
    """Count unread delivered messages for an agent."""
    conn = get_connection(hc_home)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM mailbox WHERE recipient = ? AND delivered_at IS NOT NULL AND processed_at IS NULL",
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
        msg_id = send(args.home, args.team, args.sender, args.recipient, args.message)
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
