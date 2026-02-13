"""Emergency migration: merge per-team DB data into global DB.

Situation: T0033 merged and changed code to use global DB at ~/.delegate/db.sqlite,
but data was never migrated from per-team DB at ~/.delegate/teams/self/db.sqlite.
Both DBs have been receiving writes since. This script merges them.

Strategy:
1. Back up both DBs
2. Read all data from per-team DB (authoritative: 42 tasks, ~1191 messages, etc.)
3. Read messages from global DB that don't exist in per-team DB (17 messages written post-merge)
4. Drop and recreate global DB with full V12 schema
5. Insert all data with team='self'
"""

import json
import shutil
import sqlite3
from pathlib import Path

HOME = Path.home() / ".delegate"
TEAM = "self"
PER_TEAM_DB = HOME / "teams" / TEAM / "db.sqlite"
GLOBAL_DB = HOME / "db.sqlite"


def backup(path: Path, suffix: str) -> None:
    if path.exists():
        dst = path.with_suffix(f".{suffix}.bak")
        shutil.copy2(path, dst)
        print(f"  Backed up {path} -> {dst}")


def read_all(db_path: Path) -> dict:
    """Read all rows from all tables."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    data = {}
    for table in ["tasks", "messages", "sessions", "reviews", "review_comments", "task_comments"]:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            data[table] = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            data[table] = []
    conn.close()
    return data


def find_unique_global_messages(per_team_data: dict, global_data: dict) -> list[dict]:
    """Find messages in global DB that don't exist in per-team DB.

    Match on sender + recipient + timestamp (content might differ slightly).
    """
    per_team_keys = set()
    for msg in per_team_data["messages"]:
        key = (msg["sender"], msg["recipient"], msg["timestamp"])
        per_team_keys.add(key)

    unique = []
    for msg in global_data["messages"]:
        key = (msg["sender"], msg["recipient"], msg["timestamp"])
        if key not in per_team_keys:
            unique.append(msg)

    return unique


def main():
    print("=== Emergency Data Migration ===\n")

    # Step 1: Backups
    print("Step 1: Backing up databases...")
    backup(PER_TEAM_DB, "pre-migration")
    backup(GLOBAL_DB, "pre-migration")

    # Step 2: Read data from both DBs
    print("\nStep 2: Reading data from both databases...")
    per_team = read_all(PER_TEAM_DB)
    global_data = read_all(GLOBAL_DB)

    print(f"  Per-team DB: {len(per_team['tasks'])} tasks, {len(per_team['messages'])} messages, "
          f"{len(per_team['sessions'])} sessions, {len(per_team['reviews'])} reviews, "
          f"{len(per_team['task_comments'])} task_comments")
    print(f"  Global DB:   {len(global_data['tasks'])} tasks, {len(global_data['messages'])} messages")

    # Step 3: Find unique global messages
    print("\nStep 3: Finding unique messages in global DB...")
    unique_global_msgs = find_unique_global_messages(per_team, global_data)
    print(f"  Found {len(unique_global_msgs)} unique messages in global DB")
    for msg in unique_global_msgs:
        print(f"    [{msg['timestamp']}] {msg['sender']} -> {msg['recipient']}: {msg['content'][:60]}...")

    # Step 4: Delete global DB and let ensure_schema recreate it with V12
    print("\nStep 4: Recreating global DB with V12 schema...")
    if GLOBAL_DB.exists():
        GLOBAL_DB.unlink()

    # Import and run ensure_schema to get clean V12 DB
    import sys
    sys.path.insert(0, str(Path.home() / "dev" / "delegate"))
    from delegate.db import ensure_schema
    ensure_schema(HOME)
    print("  Schema created (V12)")

    # Step 5: Insert all data
    print("\nStep 5: Inserting data into global DB...")
    conn = sqlite3.connect(str(GLOBAL_DB))
    conn.execute("BEGIN")

    try:
        # Read team_id
        team_id_file = HOME / "teams" / TEAM / "team_id"
        team_id = team_id_file.read_text().strip() if team_id_file.exists() else TEAM

        # Insert team metadata
        conn.execute(
            "INSERT OR IGNORE INTO teams (name, team_id) VALUES (?, ?)",
            (TEAM, team_id)
        )

        # Insert tasks (keep original IDs since single team)
        for task in per_team["tasks"]:
            cols = [
                "id", "title", "description", "status", "dri", "assignee",
                "project", "priority", "repo", "tags", "created_at", "updated_at",
                "completed_at", "depends_on", "branch", "base_sha", "commits",
                "rejection_reason", "approval_status", "merge_base", "merge_tip",
                "attachments", "review_attempt", "status_detail", "merge_attempts"
            ]
            vals = {c: task.get(c, "") for c in cols}
            vals["team"] = TEAM
            placeholders = ", ".join(f":{c}" for c in cols + ["team"])
            col_names = ", ".join(cols + ["team"])
            conn.execute(f"INSERT INTO tasks ({col_names}) VALUES ({placeholders})", vals)
        print(f"  Inserted {len(per_team['tasks'])} tasks")

        # Merge messages: all per-team + unique global
        all_messages = per_team["messages"].copy()
        for msg in unique_global_msgs:
            all_messages.append(msg)
        # Sort by timestamp for consistent ordering
        all_messages.sort(key=lambda m: m["timestamp"])

        for msg in all_messages:
            conn.execute(
                """INSERT INTO messages (timestamp, sender, recipient, content, type, task_id,
                   delivered_at, seen_at, processed_at, result, team)
                   VALUES (:timestamp, :sender, :recipient, :content, :type, :task_id,
                   :delivered_at, :seen_at, :processed_at, :result, :team)""",
                {
                    "timestamp": msg["timestamp"],
                    "sender": msg["sender"],
                    "recipient": msg["recipient"],
                    "content": msg["content"],
                    "type": msg["type"],
                    "task_id": msg.get("task_id"),
                    "delivered_at": msg.get("delivered_at"),
                    "seen_at": msg.get("seen_at"),
                    "processed_at": msg.get("processed_at"),
                    "result": msg.get("result"),
                    "team": TEAM,
                }
            )
        print(f"  Inserted {len(all_messages)} messages ({len(per_team['messages'])} from per-team + {len(unique_global_msgs)} unique from global)")

        # Insert sessions
        for session in per_team["sessions"]:
            conn.execute(
                """INSERT INTO sessions (agent, task_id, started_at, ended_at, duration_seconds,
                   tokens_in, tokens_out, cost_usd, cache_read_tokens, cache_write_tokens, team)
                   VALUES (:agent, :task_id, :started_at, :ended_at, :duration_seconds,
                   :tokens_in, :tokens_out, :cost_usd, :cache_read_tokens, :cache_write_tokens, :team)""",
                {
                    "agent": session["agent"],
                    "task_id": session.get("task_id"),
                    "started_at": session["started_at"],
                    "ended_at": session.get("ended_at"),
                    "duration_seconds": session.get("duration_seconds", 0.0),
                    "tokens_in": session.get("tokens_in", 0),
                    "tokens_out": session.get("tokens_out", 0),
                    "cost_usd": session.get("cost_usd", 0.0),
                    "cache_read_tokens": session.get("cache_read_tokens", 0),
                    "cache_write_tokens": session.get("cache_write_tokens", 0),
                    "team": TEAM,
                }
            )
        print(f"  Inserted {len(per_team['sessions'])} sessions")

        # Insert reviews
        for review in per_team["reviews"]:
            conn.execute(
                """INSERT INTO reviews (task_id, attempt, verdict, summary, reviewer, created_at, decided_at, team)
                   VALUES (:task_id, :attempt, :verdict, :summary, :reviewer, :created_at, :decided_at, :team)""",
                {
                    "task_id": review["task_id"],
                    "attempt": review["attempt"],
                    "verdict": review.get("verdict"),
                    "summary": review.get("summary", ""),
                    "reviewer": review.get("reviewer", ""),
                    "created_at": review["created_at"],
                    "decided_at": review.get("decided_at"),
                    "team": TEAM,
                }
            )
        print(f"  Inserted {len(per_team['reviews'])} reviews")

        # Insert review_comments
        for comment in per_team["review_comments"]:
            conn.execute(
                """INSERT INTO review_comments (task_id, attempt, file, line, body, author, created_at, team)
                   VALUES (:task_id, :attempt, :file, :line, :body, :author, :created_at, :team)""",
                {
                    "task_id": comment["task_id"],
                    "attempt": comment["attempt"],
                    "file": comment["file"],
                    "line": comment.get("line"),
                    "body": comment["body"],
                    "author": comment["author"],
                    "created_at": comment["created_at"],
                    "team": TEAM,
                }
            )
        print(f"  Inserted {len(per_team['review_comments'])} review_comments")

        # Insert task_comments
        for comment in per_team["task_comments"]:
            conn.execute(
                """INSERT INTO task_comments (task_id, author, body, created_at, team)
                   VALUES (:task_id, :author, :body, :created_at, :team)""",
                {
                    "task_id": comment["task_id"],
                    "author": comment["author"],
                    "body": comment["body"],
                    "created_at": comment["created_at"],
                    "team": TEAM,
                }
            )
        print(f"  Inserted {len(per_team['task_comments'])} task_comments")

        conn.execute("COMMIT")
        print("\n  COMMIT successful!")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"\n  ROLLBACK! Error: {e}")
        raise
    finally:
        conn.close()

    # Step 6: Verify
    print("\nStep 6: Verification...")
    conn = sqlite3.connect(str(GLOBAL_DB))
    for table in ["tasks", "messages", "sessions", "reviews", "review_comments", "task_comments", "teams"]:
        count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")

    # Check team column is set
    sample = conn.execute("SELECT team FROM tasks LIMIT 1").fetchone()
    print(f"  Sample task team column: '{sample[0]}'" if sample else "  No tasks!")

    # Check schema version
    version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
    print(f"  Schema version: V{version}")

    conn.close()

    print("\n" + "="*60)
    print("MIGRATION COMPLETE")
    print("="*60)


if __name__ == "__main__":
    main()
