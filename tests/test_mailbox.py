"""Tests for delegate/mailbox.py — SQLite-backed message system."""

import pytest

from delegate.mailbox import (
    Message,
    send,
    read_inbox,
    read_outbox,
    mark_seen,
    mark_seen_batch,
    mark_processed,
    mark_processed_batch,
    deliver,
    has_unread,
    count_unread,
)

TEAM = "testteam"


class TestMessageSerialization:
    def test_round_trip(self):
        msg = Message(
            sender="alice",
            recipient="bob",
            time="2026-02-08T12:00:00.000000Z",
            body="Hello Bob!",
        )
        text = msg.serialize()
        parsed = Message.deserialize(text, filename="test.msg")
        assert parsed.sender == "alice"
        assert parsed.recipient == "bob"
        assert parsed.time == "2026-02-08T12:00:00.000000Z"
        assert parsed.body == "Hello Bob!"
        assert parsed.filename == "test.msg"

    def test_multiline_body(self):
        body = "Line 1\nLine 2\nLine 3"
        msg = Message(
            sender="alice",
            recipient="bob",
            time="2026-02-08T12:00:00.000000Z",
            body=body,
        )
        parsed = Message.deserialize(msg.serialize())
        assert parsed.body == body

    def test_special_characters_in_body(self):
        body = 'He said "hello, world!" — and then: done.'
        msg = Message(
            sender="alice",
            recipient="bob",
            time="2026-02-08T12:00:00.000000Z",
            body=body,
        )
        parsed = Message.deserialize(msg.serialize())
        assert parsed.body == body


class TestSend:
    def test_send_returns_id(self, tmp_team):
        msg_id = send(tmp_team, TEAM, "alice", "bob", "Hello")
        assert msg_id.isdigit()

    def test_send_delivers_immediately(self, tmp_team):
        """Messages are delivered on send — visible in recipient inbox."""
        send(tmp_team, TEAM, "alice", "bob", "Hello Bob!")
        inbox = read_inbox(tmp_team, TEAM, "bob", unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == "alice"
        assert inbox[0].body == "Hello Bob!"
        assert inbox[0].delivered_at is not None

    def test_send_multiple_messages(self, tmp_team):
        id1 = send(tmp_team, TEAM, "alice", "bob", "First")
        id2 = send(tmp_team, TEAM, "alice", "bob", "Second")
        assert id1 != id2
        inbox = read_inbox(tmp_team, TEAM, "bob")
        assert len(inbox) == 2

    def test_send_logs_to_chat(self, tmp_team):
        """send() also writes to the chat messages table."""
        from delegate.chat import get_messages
        send(tmp_team, TEAM, "alice", "bob", "Logged msg")
        msgs = get_messages(tmp_team, msg_type="chat")
        assert len(msgs) == 1
        assert msgs[0]["sender"] == "alice"


class TestReadInbox:
    def test_read_inbox_empty(self, tmp_team):
        messages = read_inbox(tmp_team, TEAM, "bob")
        assert messages == []

    def test_read_inbox_returns_delivered_messages(self, tmp_team):
        send(tmp_team, TEAM, "alice", "bob", "Hello!")
        messages = read_inbox(tmp_team, TEAM, "bob")
        assert len(messages) == 1
        assert messages[0].sender == "alice"
        assert messages[0].body == "Hello!"

    def test_read_inbox_unread_only(self, tmp_team):
        msg_id = send(tmp_team, TEAM, "alice", "bob", "Hello!")
        mark_processed(tmp_team, msg_id)

        # Unread only should return nothing (message is processed)
        assert read_inbox(tmp_team, TEAM, "bob", unread_only=True) == []
        # All should return the message
        all_msgs = read_inbox(tmp_team, TEAM, "bob", unread_only=False)
        assert len(all_msgs) == 1


class TestReadOutbox:
    def test_read_outbox_sent(self, tmp_team):
        """With immediate delivery, sent messages appear in outbox (not pending)."""
        send(tmp_team, TEAM, "alice", "bob", "Hello")
        # Already delivered, so pending_only returns nothing
        pending = read_outbox(tmp_team, TEAM, "alice", pending_only=True)
        assert len(pending) == 0
        # All shows the message
        all_msgs = read_outbox(tmp_team, TEAM, "alice", pending_only=False)
        assert len(all_msgs) == 1
        assert all_msgs[0].body == "Hello"


class TestMarkProcessed:
    def test_mark_processed_removes_from_unread(self, tmp_team):
        msg_id = send(tmp_team, TEAM, "alice", "bob", "Hello!")
        assert len(read_inbox(tmp_team, TEAM, "bob", unread_only=True)) == 1

        mark_processed(tmp_team, msg_id)
        assert len(read_inbox(tmp_team, TEAM, "bob", unread_only=True)) == 0
        # Message still exists when reading all
        all_msgs = read_inbox(tmp_team, TEAM, "bob", unread_only=False)
        assert len(all_msgs) == 1
        assert all_msgs[0].processed_at is not None


class TestSeenAndProcessed:
    """Test the seen_at / processed_at lifecycle columns."""

    def test_mark_seen(self, tmp_team):
        msg_id = send(tmp_team, TEAM, "alice", "bob", "Hello")
        mark_seen(tmp_team, msg_id)

        inbox = read_inbox(tmp_team, TEAM, "bob")
        assert inbox[0].seen_at is not None
        assert inbox[0].processed_at is None

    def test_mark_seen_batch(self, tmp_team):
        id1 = send(tmp_team, TEAM, "alice", "bob", "First")
        id2 = send(tmp_team, TEAM, "alice", "bob", "Second")
        mark_seen_batch(tmp_team, [id1, id2])

        inbox = read_inbox(tmp_team, TEAM, "bob")
        assert all(m.seen_at is not None for m in inbox)

    def test_mark_processed(self, tmp_team):
        msg_id = send(tmp_team, TEAM, "alice", "bob", "Hello")
        mark_seen(tmp_team, msg_id)
        mark_processed(tmp_team, msg_id)

        inbox = read_inbox(tmp_team, TEAM, "bob", unread_only=False)
        assert inbox[0].seen_at is not None
        assert inbox[0].processed_at is not None

    def test_mark_processed_batch(self, tmp_team):
        id1 = send(tmp_team, TEAM, "alice", "bob", "First")
        id2 = send(tmp_team, TEAM, "alice", "bob", "Second")
        mark_processed_batch(tmp_team, [id1, id2])

        inbox = read_inbox(tmp_team, TEAM, "bob", unread_only=False)
        assert all(m.processed_at is not None for m in inbox)

    def test_full_lifecycle(self, tmp_team):
        """Message goes through delivered → seen → processed."""
        msg_id = send(tmp_team, TEAM, "alice", "bob", "Hello")

        msg = read_inbox(tmp_team, TEAM, "bob")[0]
        assert msg.delivered_at is not None
        assert msg.seen_at is None
        assert msg.processed_at is None

        mark_seen(tmp_team, msg_id)
        msg = read_inbox(tmp_team, TEAM, "bob")[0]
        assert msg.seen_at is not None

        mark_processed(tmp_team, msg_id)
        # processed = done, no longer unread
        assert read_inbox(tmp_team, TEAM, "bob", unread_only=True) == []
        msg = read_inbox(tmp_team, TEAM, "bob", unread_only=False)[0]
        assert msg.processed_at is not None


class TestDeliver:
    def test_deliver_to_recipient_inbox(self, tmp_team):
        msg = Message(
            sender="alice",
            recipient="bob",
            time="2026-02-08T12:00:00.000000Z",
            body="Delivered!",
        )
        msg_id = deliver(tmp_team, TEAM, msg)
        assert msg_id.isdigit()

        inbox = read_inbox(tmp_team, TEAM, "bob")
        assert len(inbox) == 1
        assert inbox[0].body == "Delivered!"


class TestHasUnread:
    def test_no_unread(self, tmp_team):
        assert not has_unread(tmp_team, "bob")

    def test_has_unread(self, tmp_team):
        send(tmp_team, TEAM, "alice", "bob", "Hey")
        assert has_unread(tmp_team, "bob")

    def test_no_unread_after_processed(self, tmp_team):
        msg_id = send(tmp_team, TEAM, "alice", "bob", "Hey")
        mark_processed(tmp_team, msg_id)
        assert not has_unread(tmp_team, "bob")


class TestCountUnread:
    def test_count_zero(self, tmp_team):
        assert count_unread(tmp_team, "bob") == 0

    def test_count_matches(self, tmp_team):
        send(tmp_team, TEAM, "alice", "bob", "First")
        send(tmp_team, TEAM, "alice", "bob", "Second")
        assert count_unread(tmp_team, "bob") == 2


class TestMessageEscaping:
    def test_commas_in_body(self, tmp_team):
        send(tmp_team, TEAM, "alice", "bob", "one, two, three")
        msgs = read_inbox(tmp_team, TEAM, "bob")
        assert msgs[0].body == "one, two, three"

    def test_quotes_in_body(self, tmp_team):
        send(tmp_team, TEAM, "alice", "bob", 'She said "hi"')
        msgs = read_inbox(tmp_team, TEAM, "bob")
        assert msgs[0].body == 'She said "hi"'

    def test_newlines_in_body(self, tmp_team):
        send(tmp_team, TEAM, "alice", "bob", "line1\nline2\nline3")
        msgs = read_inbox(tmp_team, TEAM, "bob")
        assert msgs[0].body == "line1\nline2\nline3"

    def test_unicode_in_body(self, tmp_team):
        send(tmp_team, TEAM, "alice", "bob", "Hello 🌍 — über cool")
        msgs = read_inbox(tmp_team, TEAM, "bob")
        assert msgs[0].body == "Hello 🌍 — über cool"
