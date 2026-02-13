"""Per-team SQLite-based chat, event log, and session tracking.

Central log of all routed messages, system events, and agent sessions.
Each team has its own database at ``~/.delegate/teams/<team>/db.sqlite``.

Schema is managed by ``delegate.db`` — this module only reads/writes data.
"""

import argparse
from pathlib import Path

from delegate.config import SYSTEM_USER
from delegate.db import get_connection


def log_event(hc_home: Path, team: str, description: str, *, task_id: int | None = None) -> int:
    """Log a system event. Returns the event ID."""
    conn = get_connection(hc_home, team)
    cursor = conn.execute(
        "INSERT INTO messages (sender, recipient, content, type, task_id, team) VALUES (?, ?, ?, 'event', ?, ?)",
        (SYSTEM_USER, SYSTEM_USER, description, task_id, team),
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
    before_id: int | None = None,
) -> list[dict]:
    """Query messages with optional filters.

    Returns messages with lifecycle fields (delivered_at, seen_at, processed_at).
    For type='event' rows, lifecycle fields will be NULL.

    When limit is used without before_id, returns the LAST N messages (most recent).
    When before_id is provided, returns messages with id < before_id (for pagination).
    """
    conn = get_connection(hc_home, team)
    query = """
        SELECT
            id, timestamp, sender, recipient, content, type, task_id,
            delivered_at, seen_at, processed_at, result
        FROM messages
        WHERE team = ?
    """
    params: list = [team]

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

    if before_id:
        query += " AND id < ?"
        params.append(before_id)

    # If limit is used without before_id, we want the LAST N messages
    # So we ORDER BY id DESC, LIMIT, then reverse the result
    if limit and not before_id:
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        # Reverse to return oldest-first
        return [dict(row) for row in reversed(rows)]
    else:
        # Normal pagination or no limit
        query += " ORDER BY id ASC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(row) for row in rows]


def get_task_activity(
    hc_home: Path,
    team: str,
    task_id: int,
    limit: int | None = None,
) -> list[dict]:
    """Return all messages (events + chat) associated with a task.

    Combines system events (task created, assigned, status changes) and
    inter-agent messages that reference the task.  Results are ordered
    chronologically, oldest first.
    """
    conn = get_connection(hc_home, team)
    query = """
        SELECT id, timestamp, sender, recipient, content, type, task_id
        FROM messages
        WHERE task_id = ?
        ORDER BY id ASC
    """
    params: list = [task_id]
    if limit:
        query = query.rstrip() + " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_task_timeline(
    hc_home: Path,
    team: str,
    task_id: int,
    limit: int | None = None,
) -> list[dict]:
    """Return interleaved activity events and task comments for a task.

    Merges system events from ``messages`` and ``task_comments`` into a
    single chronological list, ordered oldest first.  Chat messages
    between agents are excluded — only status/assignee changes (events)
    and task comments are returned.

    Comment rows are returned with ``type: "comment"`` and use the
    ``author`` as ``sender``.  This makes the shape uniform with event
    rows so the UI can render them in a single timeline.
    """
    conn = get_connection(hc_home, team)

    # --- UNION ALL query combines events and comments with ordering at DB level ---
    query = """
        SELECT id, timestamp, sender, recipient, content, type, task_id
        FROM messages
        WHERE task_id = ? AND type = 'event' AND team = ?

        UNION ALL

        SELECT id, created_at AS timestamp, author AS sender,
               '' AS recipient, body AS content, 'comment' AS type,
               task_id
        FROM task_comments
        WHERE task_id = ? AND team = ?

        ORDER BY timestamp ASC, id ASC
    """
    params = [task_id, team, task_id, team]
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
        "INSERT INTO sessions (agent, task_id, team) VALUES (?, ?, ?)",
        (agent, task_id, team),
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
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    """End an agent session, recording duration and token usage."""
    conn = get_connection(hc_home, team)
    conn.execute(
        """UPDATE sessions SET
            ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            duration_seconds = (julianday('now') - julianday(started_at)) * 86400,
            tokens_in = ?,
            tokens_out = ?,
            cost_usd = ?,
            cache_read_tokens = ?,
            cache_write_tokens = ?
        WHERE id = ?""",
        (tokens_in, tokens_out, cost_usd, cache_read_tokens, cache_write_tokens, session_id),
    )
    conn.commit()
    conn.close()


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
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
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
            cost_usd = ?,
            cache_read_tokens = ?,
            cache_write_tokens = ?
        WHERE id = ?""",
        (tokens_in, tokens_out, cost_usd, cache_read_tokens, cache_write_tokens, session_id),
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
            COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
            COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
            COALESCE(SUM(cache_write_tokens), 0) as total_cache_write
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
            COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
            COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
            COALESCE(SUM(cache_write_tokens), 0) as total_cache_write
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
        "total_cache_read": 0,
        "total_cache_write": 0,
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
            "total_cache_read": 0,
            "total_cache_write": 0,
        }

    conn = get_connection(hc_home, team)
    placeholders = ",".join("?" * len(task_ids))
    row = conn.execute(
        f"""SELECT
            COUNT(*) as session_count,
            COALESCE(SUM(duration_seconds), 0.0) as agent_time_seconds,
            COALESCE(SUM(tokens_in), 0) as total_tokens_in,
            COALESCE(SUM(tokens_out), 0) as total_tokens_out,
            COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
            COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
            COALESCE(SUM(cache_write_tokens), 0) as total_cache_write
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
