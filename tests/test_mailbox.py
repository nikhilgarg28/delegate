"""Tests for scripts/mailbox.py."""

import pytest

from scripts.mailbox import (
    Message,
    send,
    read_inbox,
    read_outbox,
    mark_inbox_read,
    mark_outbox_routed,
    deliver,
)


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
    def test_send_creates_outbox_file(self, tmp_team):
        fname = send(tmp_team, "alice", "bob", "Hello")
        outbox_new = tmp_team / ".standup" / "team" / "alice" / "outbox" / "new"
        assert (outbox_new / fname).is_file()

    def test_send_file_content(self, tmp_team):
        fname = send(tmp_team, "alice", "bob", "Hello Bob!")
        outbox_new = tmp_team / ".standup" / "team" / "alice" / "outbox" / "new"
        msg = Message.deserialize((outbox_new / fname).read_text())
        assert msg.sender == "alice"
        assert msg.recipient == "bob"
        assert msg.body == "Hello Bob!"

    def test_send_multiple_messages(self, tmp_team):
        f1 = send(tmp_team, "alice", "bob", "First")
        f2 = send(tmp_team, "alice", "bob", "Second")
        assert f1 != f2
        outbox_new = tmp_team / ".standup" / "team" / "alice" / "outbox" / "new"
        assert len(list(outbox_new.iterdir())) == 2

    def test_send_unknown_agent_raises(self, tmp_team):
        with pytest.raises(ValueError, match="not found"):
            send(tmp_team, "nonexistent", "bob", "Hello")

    def test_send_from_director_creates_outbox_file(self, tmp_team):
        """Director has a Maildir like everyone else — send() writes to outbox."""
        fname = send(tmp_team, "director", "manager", "Please start the project")
        outbox_new = tmp_team / ".standup" / "team" / "director" / "outbox" / "new"
        assert (outbox_new / fname).is_file()

        msg = Message.deserialize((outbox_new / fname).read_text())
        assert msg.sender == "director"
        assert msg.recipient == "manager"
        assert msg.body == "Please start the project"


class TestReadInbox:
    def test_read_inbox_empty(self, tmp_team):
        messages = read_inbox(tmp_team, "bob")
        assert messages == []

    def test_read_inbox_returns_delivered_messages(self, tmp_team):
        msg = Message(
            sender="alice",
            recipient="bob",
            time="2026-02-08T12:00:00.000000Z",
            body="Hello!",
        )
        deliver(tmp_team, msg)
        messages = read_inbox(tmp_team, "bob")
        assert len(messages) == 1
        assert messages[0].sender == "alice"
        assert messages[0].body == "Hello!"

    def test_read_inbox_unread_only(self, tmp_team):
        msg = Message(
            sender="alice",
            recipient="bob",
            time="2026-02-08T12:00:00.000000Z",
            body="Hello!",
        )
        fname = deliver(tmp_team, msg)
        mark_inbox_read(tmp_team, "bob", fname)

        # Unread only should return nothing
        assert read_inbox(tmp_team, "bob", unread_only=True) == []
        # All should return the message
        all_msgs = read_inbox(tmp_team, "bob", unread_only=False)
        assert len(all_msgs) == 1

    def test_read_inbox_unknown_agent(self, tmp_team):
        with pytest.raises(ValueError, match="not found"):
            read_inbox(tmp_team, "nonexistent")


class TestReadOutbox:
    def test_read_outbox_pending(self, tmp_team):
        send(tmp_team, "alice", "bob", "Hello")
        messages = read_outbox(tmp_team, "alice", pending_only=True)
        assert len(messages) == 1
        assert messages[0].body == "Hello"

    def test_read_outbox_after_routing(self, tmp_team):
        fname = send(tmp_team, "alice", "bob", "Hello")
        mark_outbox_routed(tmp_team, "alice", fname)

        # Pending only should return nothing
        assert read_outbox(tmp_team, "alice", pending_only=True) == []
        # All should return the message
        all_msgs = read_outbox(tmp_team, "alice", pending_only=False)
        assert len(all_msgs) == 1


class TestMarkRead:
    def test_mark_inbox_read_moves_file(self, tmp_team):
        msg = Message(
            sender="alice",
            recipient="bob",
            time="2026-02-08T12:00:00.000000Z",
            body="Hello!",
        )
        fname = deliver(tmp_team, msg)
        bob_dir = tmp_team / ".standup" / "team" / "bob"

        assert (bob_dir / "inbox" / "new" / fname).exists()
        mark_inbox_read(tmp_team, "bob", fname)
        assert not (bob_dir / "inbox" / "new" / fname).exists()
        assert (bob_dir / "inbox" / "cur" / fname).exists()

    def test_mark_inbox_read_nonexistent_raises(self, tmp_team):
        with pytest.raises(FileNotFoundError):
            mark_inbox_read(tmp_team, "bob", "nonexistent.msg")

    def test_mark_outbox_routed_moves_file(self, tmp_team):
        fname = send(tmp_team, "alice", "bob", "Hello")
        alice_dir = tmp_team / ".standup" / "team" / "alice"

        assert (alice_dir / "outbox" / "new" / fname).exists()
        mark_outbox_routed(tmp_team, "alice", fname)
        assert not (alice_dir / "outbox" / "new" / fname).exists()
        assert (alice_dir / "outbox" / "cur" / fname).exists()


class TestDeliver:
    def test_deliver_to_recipient_inbox(self, tmp_team):
        msg = Message(
            sender="alice",
            recipient="bob",
            time="2026-02-08T12:00:00.000000Z",
            body="Delivered!",
        )
        fname = deliver(tmp_team, msg)
        inbox_new = tmp_team / ".standup" / "team" / "bob" / "inbox" / "new"
        assert (inbox_new / fname).is_file()

        parsed = Message.deserialize((inbox_new / fname).read_text())
        assert parsed.body == "Delivered!"

    def test_deliver_unknown_recipient_raises(self, tmp_team):
        msg = Message(
            sender="alice",
            recipient="nobody",
            time="2026-02-08T12:00:00.000000Z",
            body="Hello?",
        )
        with pytest.raises(ValueError, match="not found"):
            deliver(tmp_team, msg)


class TestMessageEscaping:
    def test_commas_in_body(self, tmp_team):
        send(tmp_team, "alice", "bob", "one, two, three")
        msgs = read_outbox(tmp_team, "alice")
        assert msgs[0].body == "one, two, three"

    def test_quotes_in_body(self, tmp_team):
        send(tmp_team, "alice", "bob", 'She said "hi"')
        msgs = read_outbox(tmp_team, "alice")
        assert msgs[0].body == 'She said "hi"'

    def test_newlines_in_body(self, tmp_team):
        send(tmp_team, "alice", "bob", "line1\nline2\nline3")
        msgs = read_outbox(tmp_team, "alice")
        assert msgs[0].body == "line1\nline2\nline3"

    def test_unicode_in_body(self, tmp_team):
        send(tmp_team, "alice", "bob", "Hello 🌍 — über cool")
        msgs = read_outbox(tmp_team, "alice")
        assert msgs[0].body == "Hello 🌍 — über cool"
