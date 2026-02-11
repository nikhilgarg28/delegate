"""Per-team SQLite-based chat, event log, and session tracking.

Central log of all routed messages, system events, and agent sessions.
Each team has its own database at ``~/.delegate/teams/<team>/db.sqlite``.

Schema is managed by ``delegate.db`` — this module only reads/writes data.
"""

import argparse
from pathlib import Path

from delegate.db import get_connection


def log_message(
    hc_home: Path,
    team: str,
    sender: str,
    recipient: str,
    content: str,
    *,
    task_id: int | None = None,
) -> int:
    """Log a chat message. Returns the message ID."""
    conn = get_connection(hc_home, team)
    cursor = conn.execute(
        "INSERT INTO messages (sender, recipient, content, type, task_id) VALUES (?, ?, ?, 'chat', ?)",
        (sender, recipient, content, task_id),
    )
    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()
    return msg_id


def log_event(hc_home: Path, team: str, description: str, *, task_id: int | None = None) -> int:
    """Log a system event. Returns the event ID."""
    conn = get_connection(hc_home, team)
    cursor = conn.execute(
        "INSERT INTO messages (sender, recipient, content, type, task_id) VALUES ('system', 'system', ?, 'event', ?)",
        (description, task_id),
    )
    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()
    return msg_id


def get_messages(
    hc_home: Path,
    team: str,
    since: str | None = None,
    between: tuple[str, str] | None = None,
    msg_type: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Query messages with optional filters."""
    conn = get_connection(hc_home, team)
    query = "SELECT id, timestamp, sender, recipient, content, type, task_id FROM messages WHERE 1=1"
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


def start_session(hc_home: Path, team: str, agent: str, task_id: int | None = None) -> int:
    """Start a new agent session. Returns session ID."""
    conn = get_connection(hc_home, team)
    cursor = conn.execute(
        "INSERT INTO sessions (agent, task_id) VALUES (?, ?)",
        (agent, task_id),
    )
    conn.commit()
    session_id = cursor.lastrowid
    conn.close()
    return session_id


def end_session(
    hc_home: Path,
    team: str,
    session_id: int,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """End an agent session, recording duration and token usage."""
    conn = get_connection(hc_home, team)
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


def close_orphaned_sessions(hc_home: Path, team: str, agent: str) -> int:
    """Close any sessions for *agent* that have ended_at IS NULL.

    Called by the daemon when a stale session is detected — the agent
    turn errored without closing the session, leaving the DB
    session open.  We stamp ``ended_at`` so it doesn't look "active" forever.

    Returns the number of sessions closed.
    """
    conn = get_connection(hc_home, team)
    cursor = conn.execute(
        """UPDATE sessions SET
            ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            duration_seconds = (julianday('now') - julianday(started_at)) * 86400
        WHERE agent = ? AND ended_at IS NULL""",
        (agent,),
    )
    closed = cursor.rowcount
    conn.commit()
    conn.close()
    return closed


def update_session_task(hc_home: Path, team: str, session_id: int, task_id: int) -> None:
    """Update the task_id on a running session."""
    conn = get_connection(hc_home, team)
    conn.execute(
        "UPDATE sessions SET task_id = ? WHERE id = ? AND task_id IS NULL",
        (task_id, session_id),
    )
    conn.commit()
    conn.close()


def update_session_tokens(
    hc_home: Path,
    team: str,
    session_id: int,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Persist running token/cost totals mid-session.

    Called after each agent turn so the dashboard reflects live usage
    even if the agent crashes before end_session().
    """
    conn = get_connection(hc_home, team)
    conn.execute(
        """UPDATE sessions SET
            tokens_in = ?,
            tokens_out = ?,
            cost_usd = ?
        WHERE id = ?""",
        (tokens_in, tokens_out, cost_usd, session_id),
    )
    conn.commit()
    conn.close()


def get_task_stats(hc_home: Path, team: str, task_id: int) -> dict:
    """Get aggregated stats for a task from the sessions table."""
    conn = get_connection(hc_home, team)
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


def get_agent_stats(hc_home: Path, team: str, agent: str) -> dict:
    """Get aggregated stats for an agent from sessions and tasks."""
    from delegate.task import list_tasks

    conn = get_connection(hc_home, team)
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

    all_tasks = list_tasks(hc_home, team, assignee=agent)
    tasks_done = sum(1 for t in all_tasks if t.get("status") == "done")
    tasks_in_review = sum(1 for t in all_tasks if t.get("status") == "in_review")
    avg_task_seconds = stats["agent_time_seconds"] / tasks_done if tasks_done > 0 else 0.0

    stats["tasks_done"] = tasks_done
    stats["tasks_in_review"] = tasks_in_review
    stats["tasks_total"] = len(all_tasks)
    stats["avg_task_seconds"] = avg_task_seconds

    return stats


def get_project_stats(hc_home: Path, team: str, project: str) -> dict:
    """Get aggregated stats for all tasks in a project."""
    from delegate.task import list_tasks

    tasks = list_tasks(hc_home, team, project=project)
    task_ids = [t["id"] for t in tasks]

    if not task_ids:
        return {
            "session_count": 0,
            "agent_time_seconds": 0.0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "total_cost_usd": 0.0,
        }

    conn = get_connection(hc_home, team)
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

    p_log = sub.add_parser("log", help="Log a chat message")
    p_log.add_argument("home", type=Path)
    p_log.add_argument("team")
    p_log.add_argument("--sender", required=True)
    p_log.add_argument("--to", required=True, dest="recipient")
    p_log.add_argument("--msg", required=True)

    p_event = sub.add_parser("event", help="Log a system event")
    p_event.add_argument("home", type=Path)
    p_event.add_argument("team")
    p_event.add_argument("--msg", required=True)

    p_hist = sub.add_parser("history", help="Query message history")
    p_hist.add_argument("home", type=Path)
    p_hist.add_argument("team")
    p_hist.add_argument("--since", help="ISO timestamp")
    p_hist.add_argument("--between", help="Two agent names, comma-separated")
    p_hist.add_argument("--type", choices=["chat", "event"], dest="msg_type")
    p_hist.add_argument("--limit", type=int)

    args = parser.parse_args()

    if args.command == "log":
        msg_id = log_message(args.home, args.team, args.sender, args.recipient, args.msg)
        print(f"Logged message #{msg_id}")
    elif args.command == "event":
        msg_id = log_event(args.home, args.team, args.msg)
        print(f"Logged event #{msg_id}")
    elif args.command == "history":
        between = None
        if args.between:
            parts = [p.strip() for p in args.between.split(",")]
            if len(parts) == 2:
                between = (parts[0], parts[1])
        messages = get_messages(args.home, args.team, since=args.since, between=between, msg_type=args.msg_type, limit=args.limit)
        for m in messages:
            prefix = "[EVENT]" if m["type"] == "event" else f"{m['sender']} -> {m['recipient']}"
            print(f"  [{m['timestamp']}] {prefix}: {m['content']}")
        if not messages:
            print("(no messages)")


if __name__ == "__main__":
    main()
