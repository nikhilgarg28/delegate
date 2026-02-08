"""SQLite-based chat, event log, and session tracking.

Central log of all routed messages, system events, and agent sessions.

Usage:
    python scripts/chat.py log <root> --sender alice --to bob --msg "Done with task"
    python scripts/chat.py event <root> --msg "Agent alice spawned"
    python scripts/chat.py history <root> [--since TIMESTAMP] [--between alice,bob] [--limit N]
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    content TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('chat', 'event'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    task_id INTEGER,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at TEXT,
    duration_seconds REAL DEFAULT 0.0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0
);
"""


def _db_path(root: Path) -> Path:
    return root / ".standup" / "db.sqlite"


def _connect(root: Path) -> sqlite3.Connection:
    """Open a connection and ensure schema exists."""
    path = _db_path(root)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    return conn


def log_message(root: Path, sender: str, recipient: str, content: str) -> int:
    """Log a chat message. Returns the message ID."""
    conn = _connect(root)
    cursor = conn.execute(
        "INSERT INTO messages (sender, recipient, content, type) VALUES (?, ?, ?, 'chat')",
        (sender, recipient, content),
    )
    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()
    return msg_id


def log_event(root: Path, description: str) -> int:
    """Log a system event. Returns the event ID."""
    conn = _connect(root)
    cursor = conn.execute(
        "INSERT INTO messages (sender, recipient, content, type) VALUES ('system', 'system', ?, 'event')",
        (description,),
    )
    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()
    return msg_id


def get_messages(
    root: Path,
    since: str | None = None,
    between: tuple[str, str] | None = None,
    msg_type: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Query messages with optional filters.

    Args:
        since: ISO timestamp — only return messages after this time.
        between: (agent_a, agent_b) — only messages between these two agents.
        msg_type: 'chat' or 'event' — filter by type.
        limit: Max number of messages to return.
    """
    conn = _connect(root)
    query = "SELECT id, timestamp, sender, recipient, content, type FROM messages WHERE 1=1"
    params: list = []

    if since:
        query += " AND timestamp > ?"
        params.append(since)

    if between:
        a, b = between
        query += " AND ((sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?))"
        params.extend([a, b, b, a])

    if msg_type:
        query += " AND type = ?"
        params.append(msg_type)

    query += " ORDER BY id ASC"

    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# --- Session tracking ---


def start_session(root: Path, agent: str, task_id: int | None = None) -> int:
    """Start a new agent session. Returns session ID."""
    conn = _connect(root)
    cursor = conn.execute(
        "INSERT INTO sessions (agent, task_id) VALUES (?, ?)",
        (agent, task_id),
    )
    conn.commit()
    session_id = cursor.lastrowid
    conn.close()
    return session_id


def end_session(
    root: Path,
    session_id: int,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """End an agent session, recording duration and token usage."""
    conn = _connect(root)
    conn.execute(
        """UPDATE sessions SET
            ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            duration_seconds = (julianday('now') - julianday(started_at)) * 86400,
            tokens_in = ?,
            tokens_out = ?,
            cost_usd = ?
        WHERE id = ?""",
        (tokens_in, tokens_out, cost_usd, session_id),
    )
    conn.commit()
    conn.close()


def update_session_task(root: Path, session_id: int, task_id: int) -> None:
    """Update the task_id on a running session (e.g. when agent starts a task)."""
    conn = _connect(root)
    conn.execute(
        "UPDATE sessions SET task_id = ? WHERE id = ? AND task_id IS NULL",
        (task_id, session_id),
    )
    conn.commit()
    conn.close()


def get_task_stats(root: Path, task_id: int) -> dict:
    """Get aggregated stats for a task from the sessions table."""
    conn = _connect(root)
    row = conn.execute(
        """SELECT
            COUNT(*) as session_count,
            COALESCE(SUM(duration_seconds), 0.0) as agent_time_seconds,
            COALESCE(SUM(tokens_in), 0) as total_tokens_in,
            COALESCE(SUM(tokens_out), 0) as total_tokens_out,
            COALESCE(SUM(cost_usd), 0.0) as total_cost_usd
        FROM sessions WHERE task_id = ?""",
        (task_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def get_agent_stats(root: Path, agent: str) -> dict:
    """Get aggregated stats for an agent from sessions and tasks."""
    from scripts.task import list_tasks

    conn = _connect(root)

    # Session aggregates for this agent
    row = conn.execute(
        """SELECT
            COUNT(*) as session_count,
            COALESCE(SUM(duration_seconds), 0.0) as agent_time_seconds,
            COALESCE(SUM(tokens_in), 0) as total_tokens_in,
            COALESCE(SUM(tokens_out), 0) as total_tokens_out,
            COALESCE(SUM(cost_usd), 0.0) as total_cost_usd
        FROM sessions WHERE agent = ?""",
        (agent,),
    ).fetchone()
    conn.close()

    stats = dict(row) if row else {
        "session_count": 0,
        "agent_time_seconds": 0.0,
        "total_tokens_in": 0,
        "total_tokens_out": 0,
        "total_cost_usd": 0.0,
    }

    # Count tasks done and reviews done from tasks table
    all_tasks = list_tasks(root, assignee=agent)
    tasks_done = sum(1 for t in all_tasks if t.get("status") == "done")
    tasks_in_review = sum(1 for t in all_tasks if t.get("status") == "review")

    # Average task time: total agent time / tasks done (if any)
    avg_task_seconds = stats["agent_time_seconds"] / tasks_done if tasks_done > 0 else 0.0

    stats["tasks_done"] = tasks_done
    stats["tasks_in_review"] = tasks_in_review
    stats["tasks_total"] = len(all_tasks)
    stats["avg_task_seconds"] = avg_task_seconds

    return stats


def get_project_stats(root: Path, project: str) -> dict:
    """Get aggregated stats for all tasks in a project."""
    from scripts.task import list_tasks

    tasks = list_tasks(root, project=project)
    task_ids = [t["id"] for t in tasks]

    if not task_ids:
        return {
            "session_count": 0,
            "agent_time_seconds": 0.0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "total_cost_usd": 0.0,
        }

    conn = _connect(root)
    placeholders = ",".join("?" * len(task_ids))
    row = conn.execute(
        f"""SELECT
            COUNT(*) as session_count,
            COALESCE(SUM(duration_seconds), 0.0) as agent_time_seconds,
            COALESCE(SUM(tokens_in), 0) as total_tokens_in,
            COALESCE(SUM(tokens_out), 0) as total_tokens_out,
            COALESCE(SUM(cost_usd), 0.0) as total_cost_usd
        FROM sessions WHERE task_id IN ({placeholders})""",
        task_ids,
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def main():
    parser = argparse.ArgumentParser(description="Chat and event log")
    sub = parser.add_subparsers(dest="command", required=True)

    # log
    p_log = sub.add_parser("log", help="Log a chat message")
    p_log.add_argument("root", type=Path)
    p_log.add_argument("--sender", required=True)
    p_log.add_argument("--to", required=True, dest="recipient")
    p_log.add_argument("--msg", required=True)

    # event
    p_event = sub.add_parser("event", help="Log a system event")
    p_event.add_argument("root", type=Path)
    p_event.add_argument("--msg", required=True)

    # history
    p_hist = sub.add_parser("history", help="Query message history")
    p_hist.add_argument("root", type=Path)
    p_hist.add_argument("--since", help="ISO timestamp")
    p_hist.add_argument("--between", help="Two agent names, comma-separated")
    p_hist.add_argument("--type", choices=["chat", "event"], dest="msg_type")
    p_hist.add_argument("--limit", type=int)

    args = parser.parse_args()

    if args.command == "log":
        msg_id = log_message(args.root, args.sender, args.recipient, args.msg)
        print(f"Logged message #{msg_id}")

    elif args.command == "event":
        msg_id = log_event(args.root, args.msg)
        print(f"Logged event #{msg_id}")

    elif args.command == "history":
        between = None
        if args.between:
            parts = [p.strip() for p in args.between.split(",")]
            if len(parts) == 2:
                between = (parts[0], parts[1])

        messages = get_messages(
            args.root,
            since=args.since,
            between=between,
            msg_type=args.msg_type,
            limit=args.limit,
        )
        for m in messages:
            prefix = "[EVENT]" if m["type"] == "event" else f"{m['sender']} -> {m['recipient']}"
            print(f"  [{m['timestamp']}] {prefix}: {m['content']}")
        if not messages:
            print("(no messages)")


if __name__ == "__main__":
    main()
