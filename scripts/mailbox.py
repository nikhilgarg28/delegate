"""Maildir-based mailbox system for agent communication.

Each agent has an inbox/ and outbox/ with Maildir-style subdirectories:
    new/  — unprocessed messages
    cur/  — processed messages
    tmp/  — in-flight writes (atomicity)

Message file format:
    sender: <name>
    recipient: <name>
    time: <ISO 8601>
    ---
    <message body>

Usage:
    python scripts/mailbox.py send <root> <sender> <recipient> <message>
    python scripts/mailbox.py inbox <root> <agent> [--unread]
    python scripts/mailbox.py outbox <root> <agent> [--pending]
"""

import argparse
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Message:
    sender: str
    recipient: str
    time: str
    body: str
    filename: str | None = None

    def serialize(self) -> str:
        """Serialize message to the file format."""
        return f"sender: {self.sender}\nrecipient: {self.recipient}\ntime: {self.time}\n---\n{self.body}"

    @classmethod
    def deserialize(cls, text: str, filename: str | None = None) -> "Message":
        """Parse a message file's text content."""
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


def _agent_dir(root: Path, agent: str) -> Path:
    """Return the team directory for an agent."""
    d = root / ".standup" / "team" / agent
    if not d.is_dir():
        raise ValueError(f"Agent '{agent}' not found at {d}")
    return d


def _unique_filename() -> str:
    """Generate a unique filename for a message (Maildir convention)."""
    timestamp = int(time.time() * 1_000_000)
    unique = uuid.uuid4().hex[:8]
    pid = os.getpid()
    return f"{timestamp}.{pid}.{unique}"


def _write_atomic(directory: Path, content: str) -> str:
    """Write content to a file atomically using Maildir tmp->new pattern.

    Returns the filename of the written message.
    """
    filename = _unique_filename()
    tmp_path = directory.parent / "tmp" / filename
    new_path = directory / filename

    # Write to tmp first
    tmp_path.write_text(content)
    # Atomic rename to new
    tmp_path.rename(new_path)

    return filename


def send(root: Path, sender: str, recipient: str, message: str) -> str:
    """Send a message by writing it to the sender's outbox/new/.

    The daemon router will pick it up, deliver it to the recipient's inbox,
    and log it to the chat database.

    Returns the filename of the written message.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    msg = Message(sender=sender, recipient=recipient, time=now, body=message)

    agent_dir = _agent_dir(root, sender)
    outbox_new = agent_dir / "outbox" / "new"
    return _write_atomic(outbox_new, msg.serialize())


def read_inbox(root: Path, agent: str, unread_only: bool = True) -> list[Message]:
    """Read messages from an agent's inbox.

    If unread_only=True, only reads from new/.
    If unread_only=False, reads from both new/ and cur/.
    """
    agent_dir = _agent_dir(root, agent)
    messages = []

    dirs = [agent_dir / "inbox" / "new"]
    if not unread_only:
        dirs.append(agent_dir / "inbox" / "cur")

    for d in dirs:
        for f in sorted(d.iterdir()):
            if f.is_file():
                msg = Message.deserialize(f.read_text(), filename=f.name)
                messages.append(msg)

    return messages


def read_outbox(root: Path, agent: str, pending_only: bool = True) -> list[Message]:
    """Read messages from an agent's outbox.

    If pending_only=True, only reads from new/ (not yet routed).
    If pending_only=False, reads from both new/ and cur/.
    """
    agent_dir = _agent_dir(root, agent)
    messages = []

    dirs = [agent_dir / "outbox" / "new"]
    if not pending_only:
        dirs.append(agent_dir / "outbox" / "cur")

    for d in dirs:
        for f in sorted(d.iterdir()):
            if f.is_file():
                msg = Message.deserialize(f.read_text(), filename=f.name)
                messages.append(msg)

    return messages


def mark_inbox_read(root: Path, agent: str, filename: str) -> None:
    """Move a message from inbox/new/ to inbox/cur/."""
    agent_dir = _agent_dir(root, agent)
    src = agent_dir / "inbox" / "new" / filename
    dst = agent_dir / "inbox" / "cur" / filename
    if not src.exists():
        raise FileNotFoundError(f"Inbox message not found: {src}")
    src.rename(dst)


def mark_outbox_routed(root: Path, agent: str, filename: str) -> None:
    """Move a message from outbox/new/ to outbox/cur/."""
    agent_dir = _agent_dir(root, agent)
    src = agent_dir / "outbox" / "new" / filename
    dst = agent_dir / "outbox" / "cur" / filename
    if not src.exists():
        raise FileNotFoundError(f"Outbox message not found: {src}")
    src.rename(dst)


def deliver(root: Path, message: Message) -> str:
    """Deliver a message to the recipient's inbox/new/.

    Returns the filename of the delivered message.
    """
    agent_dir = _agent_dir(root, message.recipient)
    inbox_new = agent_dir / "inbox" / "new"
    return _write_atomic(inbox_new, message.serialize())


def main():
    parser = argparse.ArgumentParser(description="Mailbox management")
    sub = parser.add_subparsers(dest="command", required=True)

    # send
    p_send = sub.add_parser("send", help="Send a message")
    p_send.add_argument("root", type=Path)
    p_send.add_argument("sender", help="Sending agent name")
    p_send.add_argument("recipient", help="Recipient agent name")
    p_send.add_argument("message", help="Message body")

    # inbox
    p_inbox = sub.add_parser("inbox", help="Read inbox")
    p_inbox.add_argument("root", type=Path)
    p_inbox.add_argument("agent", help="Agent name")
    p_inbox.add_argument("--all", action="store_true", help="Include read messages")

    # outbox
    p_outbox = sub.add_parser("outbox", help="Read outbox")
    p_outbox.add_argument("root", type=Path)
    p_outbox.add_argument("agent", help="Agent name")
    p_outbox.add_argument("--all", action="store_true", help="Include routed messages")

    args = parser.parse_args()

    if args.command == "send":
        fname = send(args.root, args.sender, args.recipient, args.message)
        print(f"Message sent: {fname}")

    elif args.command == "inbox":
        messages = read_inbox(args.root, args.agent, unread_only=not args.all)
        for msg in messages:
            print(f"[{msg.time}] {msg.sender}: {msg.body}")
        if not messages:
            print("(no messages)")

    elif args.command == "outbox":
        messages = read_outbox(args.root, args.agent, pending_only=not args.all)
        for msg in messages:
            print(f"[{msg.time}] -> {msg.recipient}: {msg.body}")
        if not messages:
            print("(no messages)")


if __name__ == "__main__":
    main()
