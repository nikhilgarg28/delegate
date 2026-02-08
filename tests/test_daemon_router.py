"""Tests for scripts/router.py — the daemon's message routing logic."""

import pytest

from scripts.mailbox import send, read_inbox, read_outbox, Message, deliver
from scripts.chat import get_messages
from scripts.router import route_once, DirectorQueue


class TestRouteOnce:
    def test_route_single_message(self, tmp_team):
        """A message in alice's outbox is delivered to bob's inbox."""
        send(tmp_team, "alice", "bob", "Hello Bob!")
        routed = route_once(tmp_team)
        assert routed == 1

        inbox = read_inbox(tmp_team, "bob", unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == "alice"
        assert inbox[0].body == "Hello Bob!"

    def test_route_preserves_content(self, tmp_team):
        """Routed message content matches exactly."""
        original = "Line 1\nLine 2\n🌍 Special chars: \"quotes\", commas"
        send(tmp_team, "alice", "bob", original)
        route_once(tmp_team)

        inbox = read_inbox(tmp_team, "bob")
        assert inbox[0].body == original

    def test_route_advances_outbox(self, tmp_team):
        """After routing, the message moves from outbox/new to outbox/cur."""
        send(tmp_team, "alice", "bob", "Hello")
        assert len(read_outbox(tmp_team, "alice", pending_only=True)) == 1

        route_once(tmp_team)

        assert len(read_outbox(tmp_team, "alice", pending_only=True)) == 0
        assert len(read_outbox(tmp_team, "alice", pending_only=False)) == 1

    def test_route_skips_already_routed(self, tmp_team):
        """Running route twice with no new messages doesn't create duplicates."""
        send(tmp_team, "alice", "bob", "Hello")
        route_once(tmp_team)
        route_once(tmp_team)  # second cycle, nothing new

        inbox = read_inbox(tmp_team, "bob", unread_only=True)
        assert len(inbox) == 1

    def test_route_multiple_senders(self, tmp_team):
        """Messages from multiple agents in the same cycle all get delivered."""
        send(tmp_team, "alice", "bob", "From Alice")
        send(tmp_team, "manager", "bob", "From Manager")
        routed = route_once(tmp_team)
        assert routed == 2

        inbox = read_inbox(tmp_team, "bob")
        assert len(inbox) == 2
        senders = {m.sender for m in inbox}
        assert senders == {"alice", "manager"}

    def test_route_logs_to_sqlite(self, tmp_team):
        """Every routed message is also logged in the SQLite messages table."""
        send(tmp_team, "alice", "bob", "Logged message")
        route_once(tmp_team)

        messages = get_messages(tmp_team, msg_type="chat")
        assert len(messages) == 1
        assert messages[0]["sender"] == "alice"
        assert messages[0]["recipient"] == "bob"
        assert messages[0]["content"] == "Logged message"

    def test_route_to_director(self, tmp_team):
        """Messages to 'director' are delivered to director's inbox AND queued."""
        dq = DirectorQueue()
        send(tmp_team, "manager", "director", "Question for director")
        routed = route_once(tmp_team, director_queue=dq)

        assert routed == 1

        # Delivered to director's inbox
        inbox = read_inbox(tmp_team, "director", unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].body == "Question for director"

        # Also pushed to DirectorQueue for web UI
        assert len(dq.peek()) == 1
        assert dq.peek()[0].body == "Question for director"

        # Logged to SQLite
        messages = get_messages(tmp_team, msg_type="chat")
        assert len(messages) == 1

    def test_route_from_director(self, tmp_team):
        """Director's outbox is scanned like any other agent."""
        send(tmp_team, "director", "manager", "Start the project")
        routed = route_once(tmp_team)
        assert routed == 1

        inbox = read_inbox(tmp_team, "manager", unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == "director"
        assert inbox[0].body == "Start the project"

        # Logged to SQLite
        messages = get_messages(tmp_team, msg_type="chat")
        assert len(messages) == 1

    def test_route_unknown_recipient_logs_error(self, tmp_team):
        """Sending to a non-existent agent doesn't crash, logs an event."""
        send(tmp_team, "alice", "nonexistent", "Hello?")
        routed = route_once(tmp_team)
        assert routed == 1

        # Should have an error event logged
        events = get_messages(tmp_team, msg_type="event")
        assert len(events) >= 1
        assert "Failed to deliver" in events[0]["content"]

    def test_route_empty_outboxes(self, tmp_team):
        """Route with no pending messages returns 0."""
        routed = route_once(tmp_team)
        assert routed == 0

    def test_bidirectional_conversation(self, tmp_team):
        """Simulate a back-and-forth between two agents."""
        send(tmp_team, "alice", "bob", "Hey Bob")
        route_once(tmp_team)

        send(tmp_team, "bob", "alice", "Hey Alice")
        route_once(tmp_team)

        alice_inbox = read_inbox(tmp_team, "alice")
        bob_inbox = read_inbox(tmp_team, "bob")
        assert len(alice_inbox) == 1
        assert alice_inbox[0].sender == "bob"
        assert len(bob_inbox) == 1
        assert bob_inbox[0].sender == "alice"

        # Both should be in SQLite
        all_msgs = get_messages(tmp_team, msg_type="chat")
        assert len(all_msgs) == 2


class TestDirectorQueue:
    def test_put_and_get(self):
        dq = DirectorQueue()
        msg = Message(sender="mgr", recipient="director", time="t", body="Hi")
        dq.put(msg)
        msgs = dq.get_all()
        assert len(msgs) == 1
        assert msgs[0].body == "Hi"
        # get_all clears the queue
        assert len(dq.get_all()) == 0

    def test_peek_does_not_consume(self):
        dq = DirectorQueue()
        msg = Message(sender="mgr", recipient="director", time="t", body="Hi")
        dq.put(msg)
        assert len(dq.peek()) == 1
        assert len(dq.peek()) == 1  # still there
