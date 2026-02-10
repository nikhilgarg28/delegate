"""Tests for delegate/router.py and SQLite-backed message delivery.

With immediate delivery in ``send()``, the router's only remaining job
is to push incoming boss messages to the in-memory BossQueue.  Most
delivery tests now exercise ``send()`` / ``read_inbox()`` directly.
"""

import pytest

from delegate.mailbox import send, read_inbox, read_outbox, Message, deliver
from delegate.chat import get_messages
from delegate.router import route_once, BossQueue

TEAM = "testteam"


class TestImmediateDelivery:
    """send() delivers messages immediately — no router cycle needed."""

    def test_send_delivers_to_inbox(self, tmp_team):
        """A sent message appears in the recipient's inbox right away."""
        send(tmp_team, TEAM, "alice", "bob", "Hello Bob!")
        inbox = read_inbox(tmp_team, TEAM, "bob", unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == "alice"
        assert inbox[0].body == "Hello Bob!"

    def test_send_preserves_content(self, tmp_team):
        """Delivered message content matches exactly."""
        original = "Line 1\nLine 2\n🌍 Special chars: \"quotes\", commas"
        send(tmp_team, TEAM, "alice", "bob", original)
        inbox = read_inbox(tmp_team, TEAM, "bob")
        assert inbox[0].body == original

    def test_send_marks_delivered(self, tmp_team):
        """Sent messages have delivered_at set (no pending outbox)."""
        send(tmp_team, TEAM, "alice", "bob", "Hello")
        # With immediate delivery, nothing is "pending" in the outbox
        assert len(read_outbox(tmp_team, TEAM, "alice", pending_only=True)) == 0
        assert len(read_outbox(tmp_team, TEAM, "alice", pending_only=False)) == 1

    def test_send_logs_to_sqlite(self, tmp_team):
        """Every sent message is also logged in the SQLite messages table."""
        send(tmp_team, TEAM, "alice", "bob", "Logged message")
        messages = get_messages(tmp_team, TEAM, msg_type="chat")
        assert len(messages) == 1
        assert messages[0]["sender"] == "alice"
        assert messages[0]["recipient"] == "bob"
        assert messages[0]["content"] == "Logged message"

    def test_multiple_senders(self, tmp_team):
        """Messages from multiple agents all get delivered immediately."""
        send(tmp_team, TEAM, "alice", "bob", "From Alice")
        send(tmp_team, TEAM, "manager", "bob", "From Manager")
        inbox = read_inbox(tmp_team, TEAM, "bob")
        assert len(inbox) == 2
        senders = {m.sender for m in inbox}
        assert senders == {"alice", "manager"}

    def test_bidirectional_conversation(self, tmp_team):
        """Simulate a back-and-forth between two agents."""
        send(tmp_team, TEAM, "alice", "bob", "Hey Bob")
        send(tmp_team, TEAM, "bob", "alice", "Hey Alice")

        alice_inbox = read_inbox(tmp_team, TEAM, "alice")
        bob_inbox = read_inbox(tmp_team, TEAM, "bob")
        assert len(alice_inbox) == 1
        assert alice_inbox[0].sender == "bob"
        assert len(bob_inbox) == 1
        assert bob_inbox[0].sender == "alice"

        # Both should be in SQLite
        all_msgs = get_messages(tmp_team, TEAM, msg_type="chat")
        assert len(all_msgs) == 2


class TestRouteOnce:
    """route_once() pushes unread boss messages to BossQueue."""

    def test_route_notifies_boss_queue(self, tmp_team):
        """Messages to the boss are pushed to BossQueue."""
        from delegate.config import get_boss
        boss_name = get_boss(tmp_team) or "nikhil"
        dq = BossQueue()
        send(tmp_team, TEAM, "manager", boss_name, "Question for boss")
        notified = route_once(tmp_team, TEAM, boss_queue=dq, boss_name=boss_name)

        assert notified == 1
        assert len(dq.peek()) == 1
        assert dq.peek()[0].body == "Question for boss"

    def test_route_without_boss_queue_returns_zero(self, tmp_team):
        """Without a BossQueue, route_once does nothing."""
        send(tmp_team, TEAM, "alice", "bob", "Hello")
        routed = route_once(tmp_team, TEAM)
        assert routed == 0

    def test_route_empty_inbox(self, tmp_team):
        """Route with no pending messages returns 0."""
        from delegate.config import get_boss
        boss_name = get_boss(tmp_team) or "nikhil"
        dq = BossQueue()
        routed = route_once(tmp_team, TEAM, boss_queue=dq, boss_name=boss_name)
        assert routed == 0

    def test_boss_queue_deduplicates(self, tmp_team):
        """Running route_once twice doesn't push duplicates to BossQueue."""
        from delegate.config import get_boss
        boss_name = get_boss(tmp_team) or "nikhil"
        dq = BossQueue()
        send(tmp_team, TEAM, "manager", boss_name, "Hi boss")
        route_once(tmp_team, TEAM, boss_queue=dq, boss_name=boss_name)
        route_once(tmp_team, TEAM, boss_queue=dq, boss_name=boss_name)
        assert len(dq.peek()) == 1


class TestBossQueue:
    def test_put_and_get(self):
        dq = BossQueue()
        msg = Message(sender="mgr", recipient="boss", time="t", body="Hi", id=1)
        dq.put(msg)
        msgs = dq.get_all()
        assert len(msgs) == 1
        assert msgs[0].body == "Hi"
        # get_all clears the queue
        assert len(dq.get_all()) == 0

    def test_peek_does_not_consume(self):
        dq = BossQueue()
        msg = Message(sender="mgr", recipient="boss", time="t", body="Hi", id=1)
        dq.put(msg)
        assert len(dq.peek()) == 1
        assert len(dq.peek()) == 1  # still there

    def test_dedup_by_id(self):
        dq = BossQueue()
        msg = Message(sender="mgr", recipient="boss", time="t", body="Hi", id=42)
        dq.put(msg)
        dq.put(msg)  # same id
        assert len(dq.peek()) == 1
