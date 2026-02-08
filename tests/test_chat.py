"""Tests for scripts/chat.py."""

import sqlite3
import threading
import time

from scripts.chat import (
    log_message,
    log_event,
    get_messages,
    start_session,
    end_session,
    update_session_task,
    get_task_stats,
    get_project_stats,
    _db_path,
)


class TestSchema:
    def test_schema_created(self, tmp_team):
        conn = sqlite3.connect(str(_db_path(tmp_team)))
        cursor = conn.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert columns == {"id", "timestamp", "sender", "recipient", "content", "type"}

    def test_sessions_table_exists(self, tmp_team):
        conn = sqlite3.connect(str(_db_path(tmp_team)))
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "agent" in columns
        assert "task_id" in columns
        assert "tokens_in" in columns
        assert "tokens_out" in columns


class TestLogMessage:
    def test_returns_id(self, tmp_team):
        msg_id = log_message(tmp_team, "alice", "bob", "Hello")
        assert isinstance(msg_id, int)
        assert msg_id >= 1

    def test_increments_id(self, tmp_team):
        id1 = log_message(tmp_team, "alice", "bob", "First")
        id2 = log_message(tmp_team, "alice", "bob", "Second")
        assert id2 == id1 + 1

    def test_persists_fields(self, tmp_team):
        log_message(tmp_team, "alice", "bob", "Hello Bob!")
        messages = get_messages(tmp_team)
        assert len(messages) == 1
        m = messages[0]
        assert m["sender"] == "alice"
        assert m["recipient"] == "bob"
        assert m["content"] == "Hello Bob!"
        assert m["type"] == "chat"
        assert m["timestamp"]  # not empty


class TestLogEvent:
    def test_event_type(self, tmp_team):
        log_event(tmp_team, "Agent alice spawned")
        messages = get_messages(tmp_team)
        assert len(messages) == 1
        assert messages[0]["type"] == "event"
        assert messages[0]["sender"] == "system"
        assert messages[0]["recipient"] == "system"
        assert messages[0]["content"] == "Agent alice spawned"


class TestGetMessages:
    def test_all_chronological(self, tmp_team):
        log_message(tmp_team, "alice", "bob", "First")
        log_message(tmp_team, "bob", "alice", "Second")
        log_event(tmp_team, "Something happened")
        messages = get_messages(tmp_team)
        assert len(messages) == 3
        assert messages[0]["content"] == "First"
        assert messages[1]["content"] == "Second"
        assert messages[2]["content"] == "Something happened"

    def test_filter_since(self, tmp_team):
        log_message(tmp_team, "alice", "bob", "Old message")
        # Get the timestamp of the first message
        all_msgs = get_messages(tmp_team)
        cutoff = all_msgs[0]["timestamp"]
        time.sleep(0.01)  # ensure distinct timestamp
        log_message(tmp_team, "alice", "bob", "New message")
        recent = get_messages(tmp_team, since=cutoff)
        assert len(recent) == 1
        assert recent[0]["content"] == "New message"

    def test_filter_between(self, tmp_team):
        log_message(tmp_team, "alice", "bob", "A to B")
        log_message(tmp_team, "bob", "alice", "B to A")
        log_message(tmp_team, "alice", "manager", "A to M")

        ab_msgs = get_messages(tmp_team, between=("alice", "bob"))
        assert len(ab_msgs) == 2
        assert all(
            {m["sender"], m["recipient"]} == {"alice", "bob"} for m in ab_msgs
        )

    def test_filter_type(self, tmp_team):
        log_message(tmp_team, "alice", "bob", "Chat msg")
        log_event(tmp_team, "Event msg")

        chats = get_messages(tmp_team, msg_type="chat")
        assert len(chats) == 1
        assert chats[0]["type"] == "chat"

        events = get_messages(tmp_team, msg_type="event")
        assert len(events) == 1
        assert events[0]["type"] == "event"

    def test_limit(self, tmp_team):
        for i in range(10):
            log_message(tmp_team, "alice", "bob", f"Message {i}")
        messages = get_messages(tmp_team, limit=3)
        assert len(messages) == 3

    def test_special_characters(self, tmp_team):
        content = 'He said "hello, world!" — über cool 🌍\nNew line here'
        log_message(tmp_team, "alice", "bob", content)
        messages = get_messages(tmp_team)
        assert messages[0]["content"] == content

    def test_concurrent_writes(self, tmp_team):
        """Multiple threads writing simultaneously should not lose data."""
        errors = []

        def writer(sender, count):
            try:
                for i in range(count):
                    log_message(tmp_team, sender, "bob", f"{sender}-{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(f"agent{n}", 20))
            for n in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent writes: {errors}"
        messages = get_messages(tmp_team)
        assert len(messages) == 100  # 5 agents * 20 messages


class TestSessions:
    def test_start_session_returns_id(self, tmp_team):
        session_id = start_session(tmp_team, "alice")
        assert isinstance(session_id, int)
        assert session_id >= 1

    def test_start_session_increments_id(self, tmp_team):
        id1 = start_session(tmp_team, "alice")
        id2 = start_session(tmp_team, "bob")
        assert id2 == id1 + 1

    def test_end_session_records_tokens(self, tmp_team):
        from scripts.task import create_task
        task = create_task(tmp_team, title="Test task")
        session_id = start_session(tmp_team, "alice", task_id=task["id"])
        end_session(tmp_team, session_id, tokens_in=100, tokens_out=200, cost_usd=0.05)

        stats = get_task_stats(tmp_team, task["id"])
        assert stats["session_count"] == 1
        assert stats["total_tokens_in"] == 100
        assert stats["total_tokens_out"] == 200
        assert stats["total_cost_usd"] == 0.05

    def test_end_session_records_duration(self, tmp_team):
        from scripts.task import create_task
        task = create_task(tmp_team, title="Test task")
        session_id = start_session(tmp_team, "alice", task_id=task["id"])
        time.sleep(0.1)
        end_session(tmp_team, session_id, tokens_in=50, tokens_out=50)

        stats = get_task_stats(tmp_team, task["id"])
        assert stats["agent_time_seconds"] > 0

    def test_multiple_sessions_aggregate(self, tmp_team):
        from scripts.task import create_task
        task = create_task(tmp_team, title="Test task")

        s1 = start_session(tmp_team, "alice", task_id=task["id"])
        end_session(tmp_team, s1, tokens_in=100, tokens_out=50)

        s2 = start_session(tmp_team, "bob", task_id=task["id"])
        end_session(tmp_team, s2, tokens_in=200, tokens_out=100)

        stats = get_task_stats(tmp_team, task["id"])
        assert stats["session_count"] == 2
        assert stats["total_tokens_in"] == 300
        assert stats["total_tokens_out"] == 150

    def test_session_without_task(self, tmp_team):
        session_id = start_session(tmp_team, "alice")
        end_session(tmp_team, session_id, tokens_in=50, tokens_out=25)
        # Should not crash — stats for nonexistent task returns zeros
        stats = get_task_stats(tmp_team, 9999)
        assert stats["session_count"] == 0

    def test_project_stats(self, tmp_team):
        from scripts.task import create_task
        t1 = create_task(tmp_team, title="A", project="myproject")
        t2 = create_task(tmp_team, title="B", project="myproject")

        s1 = start_session(tmp_team, "alice", task_id=t1["id"])
        end_session(tmp_team, s1, tokens_in=100, tokens_out=50)

        s2 = start_session(tmp_team, "bob", task_id=t2["id"])
        end_session(tmp_team, s2, tokens_in=200, tokens_out=100)

        stats = get_project_stats(tmp_team, "myproject")
        assert stats["session_count"] == 2
        assert stats["total_tokens_in"] == 300
        assert stats["total_tokens_out"] == 150

    def test_update_session_task(self, tmp_team):
        """update_session_task links a running session to a task retroactively."""
        from scripts.task import create_task
        task = create_task(tmp_team, title="Late-linked task")

        # Start session without a task
        session_id = start_session(tmp_team, "alice")
        # Link the task mid-session
        update_session_task(tmp_team, session_id, task["id"])
        end_session(tmp_team, session_id, tokens_in=100, tokens_out=200, cost_usd=0.05)

        stats = get_task_stats(tmp_team, task["id"])
        assert stats["session_count"] == 1
        assert stats["total_tokens_in"] == 100
        assert stats["total_tokens_out"] == 200
        assert stats["total_cost_usd"] == 0.05

    def test_update_session_task_no_overwrite(self, tmp_team):
        """update_session_task doesn't overwrite an existing task_id."""
        from scripts.task import create_task
        t1 = create_task(tmp_team, title="First task")
        t2 = create_task(tmp_team, title="Second task")

        session_id = start_session(tmp_team, "alice", task_id=t1["id"])
        # Try to overwrite — should be ignored (WHERE task_id IS NULL)
        update_session_task(tmp_team, session_id, t2["id"])
        end_session(tmp_team, session_id, tokens_in=50, tokens_out=50)

        stats1 = get_task_stats(tmp_team, t1["id"])
        stats2 = get_task_stats(tmp_team, t2["id"])
        assert stats1["session_count"] == 1  # stays with t1
        assert stats2["session_count"] == 0  # not linked to t2

    def test_project_stats_empty(self, tmp_team):
        stats = get_project_stats(tmp_team, "nonexistent")
        assert stats["session_count"] == 0
        assert stats["total_tokens_in"] == 0


class TestGetCurrentTaskId:
    """Tests for _get_current_task_id covering the open-task fallback."""

    def test_finds_in_progress_task(self, tmp_team):
        from scripts.agent import _get_current_task_id
        from scripts.task import create_task, change_status, assign_task
        task = create_task(tmp_team, title="In progress task")
        assign_task(tmp_team, task["id"], "alice")
        change_status(tmp_team, task["id"], "in_progress")
        assert _get_current_task_id(tmp_team, "alice") == task["id"]

    def test_finds_open_task(self, tmp_team):
        """An open task assigned to the agent should be found (session-start case)."""
        from scripts.agent import _get_current_task_id
        from scripts.task import create_task, assign_task
        task = create_task(tmp_team, title="Open task")
        assign_task(tmp_team, task["id"], "alice")
        assert _get_current_task_id(tmp_team, "alice") == task["id"]

    def test_prefers_in_progress_over_open(self, tmp_team):
        """If both an in_progress and open task exist, prefer in_progress."""
        from scripts.agent import _get_current_task_id
        from scripts.task import create_task, assign_task, change_status
        open_task = create_task(tmp_team, title="Open task")
        assign_task(tmp_team, open_task["id"], "alice")
        ip_task = create_task(tmp_team, title="IP task")
        assign_task(tmp_team, ip_task["id"], "alice")
        change_status(tmp_team, ip_task["id"], "in_progress")
        assert _get_current_task_id(tmp_team, "alice") == ip_task["id"]

    def test_returns_none_when_no_tasks(self, tmp_team):
        from scripts.agent import _get_current_task_id
        assert _get_current_task_id(tmp_team, "alice") is None

    def test_returns_none_when_multiple_open(self, tmp_team):
        """Ambiguous: multiple open tasks, no in_progress — returns None."""
        from scripts.agent import _get_current_task_id
        from scripts.task import create_task, assign_task
        t1 = create_task(tmp_team, title="Task 1")
        t2 = create_task(tmp_team, title="Task 2")
        assign_task(tmp_team, t1["id"], "alice")
        assign_task(tmp_team, t2["id"], "alice")
        assert _get_current_task_id(tmp_team, "alice") is None
