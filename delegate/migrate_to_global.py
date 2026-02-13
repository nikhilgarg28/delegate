"""Migrate from per-team SQLite databases to a single global database.

This script:
1. Backs up each per-team DB to db.sqlite.bak
2. Reads all data from existing per-team databases
3. Merges into a single global DB at ~/.delegate/db.sqlite
4. Assigns new global task IDs (sequential across all teams)
5. Updates all task_id references in messages, sessions, reviews, etc.
6. Updates depends_on arrays with new global IDs

Usage:
    python -m delegate.migrate_to_global [--home ~/.delegate]
"""

import argparse
import json
import logging
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from delegate.db import ensure_schema, MIGRATIONS
from delegate.paths import global_db_path, db_path, team_dir, get_team_id

logger = logging.getLogger(__name__)


def backup_team_db(team_db_path: Path) -> None:
    """Backup a per-team database to .bak file."""
    if not team_db_path.exists():
        return
    backup_path = team_db_path.with_suffix(".sqlite.bak")
    shutil.copy2(team_db_path, backup_path)
    logger.info(f"Backed up {team_db_path} -> {backup_path}")


def read_team_data(hc_home: Path, team: str) -> dict:
    """Read all data from a per-team database.

    Returns dict with keys: tasks, messages, sessions, reviews, review_comments, task_comments
    Each value is a list of row dicts.
    """
    team_db = db_path(hc_home, team)
    if not team_db.exists():
        logger.warning(f"Team DB not found: {team_db}")
        return {
            "tasks": [],
            "messages": [],
            "sessions": [],
            "reviews": [],
            "review_comments": [],
            "task_comments": [],
        }

    conn = sqlite3.connect(str(team_db))
    conn.row_factory = sqlite3.Row

    data = {}

    # Read tasks
    cursor = conn.execute("SELECT * FROM tasks ORDER BY created_at")
    data["tasks"] = [dict(row) for row in cursor.fetchall()]

    # Read messages
    cursor = conn.execute("SELECT * FROM messages ORDER BY timestamp")
    data["messages"] = [dict(row) for row in cursor.fetchall()]

    # Read sessions
    cursor = conn.execute("SELECT * FROM sessions ORDER BY started_at")
    data["sessions"] = [dict(row) for row in cursor.fetchall()]

    # Read reviews
    cursor = conn.execute("SELECT * FROM reviews ORDER BY created_at")
    data["reviews"] = [dict(row) for row in cursor.fetchall()]

    # Read review_comments
    cursor = conn.execute("SELECT * FROM review_comments ORDER BY created_at")
    data["review_comments"] = [dict(row) for row in cursor.fetchall()]

    # Read task_comments
    cursor = conn.execute("SELECT * FROM task_comments ORDER BY created_at")
    data["task_comments"] = [dict(row) for row in cursor.fetchall()]

    conn.close()

    logger.info(f"Read {len(data['tasks'])} tasks, {len(data['messages'])} messages from team '{team}'")
    return data


def build_task_id_mapping(all_team_data: dict[str, dict]) -> tuple[dict, list]:
    """Build mapping from (team, old_id) -> new_global_id.

    Returns:
        - Mapping dict: {(team, old_id): new_global_id}
        - List of all tasks sorted by created_at with team and new IDs
    """
    # Collect all tasks from all teams with their team name
    all_tasks = []
    for team, data in all_team_data.items():
        for task in data["tasks"]:
            all_tasks.append((team, task))

    # Sort by created_at to maintain chronological order
    all_tasks.sort(key=lambda x: x[1]["created_at"])

    # Assign new sequential global IDs
    mapping = {}
    new_tasks = []
    for new_id, (team, task) in enumerate(all_tasks, start=1):
        old_id = task["id"]
        mapping[(team, old_id)] = new_id
        task["id"] = new_id
        task["team"] = team
        new_tasks.append(task)

    return mapping, new_tasks


def update_task_references(data: dict, team: str, id_mapping: dict) -> None:
    """Update all task_id references in data using the id_mapping.

    Modifies data dict in place.
    """
    # Update messages
    for msg in data["messages"]:
        if msg.get("task_id"):
            old_id = msg["task_id"]
            msg["task_id"] = id_mapping.get((team, old_id), old_id)
        msg["team"] = team

    # Update sessions
    for session in data["sessions"]:
        if session.get("task_id"):
            old_id = session["task_id"]
            session["task_id"] = id_mapping.get((team, old_id), old_id)
        session["team"] = team

    # Update reviews
    for review in data["reviews"]:
        old_id = review["task_id"]
        review["task_id"] = id_mapping.get((team, old_id), old_id)
        review["team"] = team

    # Update review_comments
    for comment in data["review_comments"]:
        old_id = comment["task_id"]
        comment["task_id"] = id_mapping.get((team, old_id), old_id)
        comment["team"] = team

    # Update task_comments
    for comment in data["task_comments"]:
        old_id = comment["task_id"]
        comment["task_id"] = id_mapping.get((team, old_id), old_id)
        comment["team"] = team


def update_depends_on(tasks: list[dict], id_mapping: dict) -> None:
    """Update depends_on arrays in tasks with new global IDs.

    Modifies tasks list in place.
    """
    for task in tasks:
        team = task["team"]
        depends_on_str = task.get("depends_on", "[]")

        # Parse JSON
        try:
            depends_on = json.loads(depends_on_str) if isinstance(depends_on_str, str) else depends_on_str
        except (json.JSONDecodeError, TypeError):
            depends_on = []

        if not isinstance(depends_on, list):
            depends_on = []

        # Map old IDs to new IDs (all within same team)
        new_depends_on = []
        for old_id in depends_on:
            if isinstance(old_id, int):
                new_id = id_mapping.get((team, old_id), old_id)
                new_depends_on.append(new_id)

        # Serialize back to JSON
        task["depends_on"] = json.dumps(new_depends_on)


def insert_tasks(conn: sqlite3.Connection, tasks: list[dict]) -> None:
    """Insert tasks into global DB."""
    for task in tasks:
        conn.execute("""
            INSERT INTO tasks (
                id, title, description, status, dri, assignee, project, priority,
                repo, tags, created_at, updated_at, completed_at, depends_on,
                branch, base_sha, commits, rejection_reason, approval_status,
                merge_base, merge_tip, attachments, review_attempt, status_detail,
                merge_attempts, team
            ) VALUES (
                :id, :title, :description, :status, :dri, :assignee, :project, :priority,
                :repo, :tags, :created_at, :updated_at, :completed_at, :depends_on,
                :branch, :base_sha, :commits, :rejection_reason, :approval_status,
                :merge_base, :merge_tip, :attachments, :review_attempt, :status_detail,
                :merge_attempts, :team
            )
        """, task)


def insert_messages(conn: sqlite3.Connection, messages: list[dict]) -> None:
    """Insert messages into global DB."""
    for msg in messages:
        # Handle messages from V9 and earlier that don't have lifecycle columns
        conn.execute("""
            INSERT INTO messages (
                timestamp, sender, recipient, content, type, task_id,
                delivered_at, seen_at, processed_at, result, team
            ) VALUES (
                :timestamp, :sender, :recipient, :content, :type, :task_id,
                :delivered_at, :seen_at, :processed_at, :result, :team
            )
        """, {
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
            "team": msg["team"],
        })


def insert_sessions(conn: sqlite3.Connection, sessions: list[dict]) -> None:
    """Insert sessions into global DB."""
    for session in sessions:
        conn.execute("""
            INSERT INTO sessions (
                agent, task_id, started_at, ended_at, duration_seconds,
                tokens_in, tokens_out, cost_usd, cache_read_tokens, cache_write_tokens, team
            ) VALUES (
                :agent, :task_id, :started_at, :ended_at, :duration_seconds,
                :tokens_in, :tokens_out, :cost_usd, :cache_read_tokens, :cache_write_tokens, :team
            )
        """, {
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
            "team": session["team"],
        })


def insert_reviews(conn: sqlite3.Connection, reviews: list[dict]) -> None:
    """Insert reviews into global DB."""
    for review in reviews:
        conn.execute("""
            INSERT INTO reviews (
                task_id, attempt, verdict, summary, reviewer, created_at, decided_at, team
            ) VALUES (
                :task_id, :attempt, :verdict, :summary, :reviewer, :created_at, :decided_at, :team
            )
        """, {
            "task_id": review["task_id"],
            "attempt": review["attempt"],
            "verdict": review.get("verdict"),
            "summary": review.get("summary", ""),
            "reviewer": review.get("reviewer", ""),
            "created_at": review["created_at"],
            "decided_at": review.get("decided_at"),
            "team": review["team"],
        })


def insert_review_comments(conn: sqlite3.Connection, comments: list[dict]) -> None:
    """Insert review_comments into global DB."""
    for comment in comments:
        conn.execute("""
            INSERT INTO review_comments (
                task_id, attempt, file, line, body, author, created_at, team
            ) VALUES (
                :task_id, :attempt, :file, :line, :body, :author, :created_at, :team
            )
        """, {
            "task_id": comment["task_id"],
            "attempt": comment["attempt"],
            "file": comment["file"],
            "line": comment.get("line"),
            "body": comment["body"],
            "author": comment["author"],
            "created_at": comment["created_at"],
            "team": comment["team"],
        })


def insert_task_comments(conn: sqlite3.Connection, comments: list[dict]) -> None:
    """Insert task_comments into global DB."""
    for comment in comments:
        conn.execute("""
            INSERT INTO task_comments (
                task_id, author, body, created_at, team
            ) VALUES (
                :task_id, :author, :body, :created_at, :team
            )
        """, {
            "task_id": comment["task_id"],
            "author": comment["author"],
            "body": comment["body"],
            "created_at": comment["created_at"],
            "team": comment["team"],
        })


def insert_teams(conn: sqlite3.Connection, teams_data: dict[str, str]) -> None:
    """Insert team metadata into global DB.

    teams_data: {team_name: team_id_hex}
    """
    for team_name, tid in teams_data.items():
        conn.execute("""
            INSERT INTO teams (name, team_id, created_at)
            VALUES (?, ?, ?)
        """, (team_name, tid, datetime.utcnow().isoformat() + "Z"))


def migrate(hc_home: Path) -> None:
    """Run the full migration from per-team DBs to global DB."""
    hc_home = hc_home.resolve()
    teams_dir = hc_home / "teams"

    if not teams_dir.exists():
        logger.error(f"Teams directory not found: {teams_dir}")
        return

    # Find all teams
    team_dirs = [d for d in teams_dir.iterdir() if d.is_dir() and (d / "db.sqlite").exists()]
    team_names = [d.name for d in team_dirs]

    if not team_names:
        logger.info("No teams found with databases. Nothing to migrate.")
        return

    logger.info(f"Found {len(team_names)} teams: {', '.join(team_names)}")

    # Backup all per-team DBs
    for team in team_names:
        backup_team_db(db_path(hc_home, team))

    # Read all team data
    all_team_data = {}
    teams_metadata = {}
    for team in team_names:
        all_team_data[team] = read_team_data(hc_home, team)
        tid = get_team_id(hc_home, team)
        teams_metadata[team] = tid
        logger.info(f"Team '{team}' has team_id '{tid}'")

    # Build task ID mapping
    id_mapping, merged_tasks = build_task_id_mapping(all_team_data)

    # Update task references in all data
    for team, data in all_team_data.items():
        update_task_references(data, team, id_mapping)

    # Update depends_on in tasks
    update_depends_on(merged_tasks, id_mapping)

    # Ensure global DB schema is ready
    global_db = global_db_path(hc_home)
    if global_db.exists():
        logger.warning(f"Global DB already exists: {global_db}")
        backup = global_db.with_suffix(".sqlite.pre-migration.bak")
        shutil.copy2(global_db, backup)
        logger.info(f"Backed up existing global DB to {backup}")
        # Delete and recreate
        global_db.unlink()

    # Create fresh global DB with V11 schema
    ensure_schema(hc_home)

    # Insert all data
    conn = sqlite3.connect(str(global_db))
    conn.execute("BEGIN")

    try:
        # Insert teams metadata first
        insert_teams(conn, teams_metadata)
        logger.info(f"Inserted {len(teams_metadata)} teams")

        # Insert tasks with new global IDs
        insert_tasks(conn, merged_tasks)
        logger.info(f"Inserted {len(merged_tasks)} tasks")

        # Insert all other data
        total_messages = sum(len(d["messages"]) for d in all_team_data.values())
        total_sessions = sum(len(d["sessions"]) for d in all_team_data.values())
        total_reviews = sum(len(d["reviews"]) for d in all_team_data.values())
        total_review_comments = sum(len(d["review_comments"]) for d in all_team_data.values())
        total_task_comments = sum(len(d["task_comments"]) for d in all_team_data.values())

        for team, data in all_team_data.items():
            insert_messages(conn, data["messages"])
            insert_sessions(conn, data["sessions"])
            insert_reviews(conn, data["reviews"])
            insert_review_comments(conn, data["review_comments"])
            insert_task_comments(conn, data["task_comments"])

        logger.info(f"Inserted {total_messages} messages")
        logger.info(f"Inserted {total_sessions} sessions")
        logger.info(f"Inserted {total_reviews} reviews")
        logger.info(f"Inserted {total_review_comments} review_comments")
        logger.info(f"Inserted {total_task_comments} task_comments")

        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        conn.close()

    # Print summary
    print("\n" + "="*60)
    print("MIGRATION COMPLETE")
    print("="*60)
    print(f"Teams migrated: {len(team_names)}")
    print(f"Global DB: {global_db}")
    print()
    print("Tasks per team:")
    for team in team_names:
        team_tasks = [t for t in merged_tasks if t["team"] == team]
        if team_tasks:
            old_ids = [id_mapping[(team, t["id"])] for t in all_team_data[team]["tasks"]]
            min_id = min(t["id"] for t in team_tasks)
            max_id = max(t["id"] for t in team_tasks)
            print(f"  {team}: {len(team_tasks)} tasks (new IDs: {min_id}-{max_id})")
        else:
            print(f"  {team}: 0 tasks")
    print()
    print("Original per-team DBs backed up with .bak extension")
    print("="*60)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Migrate per-team DBs to global DB")
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home() / ".delegate",
        help="Delegate home directory (default: ~/.delegate)"
    )
    args = parser.parse_args()

    migrate(args.home)


if __name__ == "__main__":
    main()
