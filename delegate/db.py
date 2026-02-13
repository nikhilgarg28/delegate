"""Per-team SQLite database with versioned migrations.

Each team has its own database at ``~/.delegate/teams/<team>/db.sqlite``.
On first access the ``schema_meta`` table is created and pending migrations
are applied in order.  Each migration is idempotent (uses ``IF NOT EXISTS``).

Usage::

    from delegate.db import get_connection, ensure_schema

    # At daemon startup (or lazily on first query):
    ensure_schema(hc_home, team)

    # For individual operations:
    conn = get_connection(hc_home, team)
    ...
    conn.close()
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path

from delegate.paths import db_path, global_db_path

logger = logging.getLogger(__name__)

# Per-process cache to avoid redundant schema checks
# Changed to use just hc_home since we now have a global DB
_schema_verified: dict[str, int] = {}
_schema_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------
# Each entry is a SQL script.  Migrations are numbered starting at 1.
# To add a new migration, append a new string to this list.
# NEVER reorder or modify existing entries — only append.

MIGRATIONS: list[str] = [
    # --- V1: messages + sessions ---
    """\
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sender      TEXT    NOT NULL,
    recipient   TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    type        TEXT    NOT NULL CHECK(type IN ('chat', 'event'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    agent            TEXT    NOT NULL,
    task_id          INTEGER,
    started_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at         TEXT,
    duration_seconds REAL    DEFAULT 0.0,
    tokens_in        INTEGER DEFAULT 0,
    tokens_out       INTEGER DEFAULT 0,
    cost_usd         REAL    DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_messages_type
    ON messages(type);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp
    ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient
    ON messages(sender, recipient);
CREATE INDEX IF NOT EXISTS idx_sessions_agent
    ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_task_id
    ON sessions(task_id);
""",

    # --- V2: tasks table ---
    """\
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL,
    description      TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'todo',
    dri              TEXT    NOT NULL DEFAULT '',
    assignee         TEXT    NOT NULL DEFAULT '',
    project          TEXT    NOT NULL DEFAULT '',
    priority         TEXT    NOT NULL DEFAULT 'medium',
    repo             TEXT    NOT NULL DEFAULT '',
    tags             TEXT    NOT NULL DEFAULT '[]',
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    completed_at     TEXT    NOT NULL DEFAULT '',
    depends_on       TEXT    NOT NULL DEFAULT '[]',
    branch           TEXT    NOT NULL DEFAULT '',
    base_sha         TEXT    NOT NULL DEFAULT '',
    commits          TEXT    NOT NULL DEFAULT '[]',
    rejection_reason TEXT    NOT NULL DEFAULT '',
    approval_status  TEXT    NOT NULL DEFAULT '',
    merge_base       TEXT    NOT NULL DEFAULT '',
    merge_tip        TEXT    NOT NULL DEFAULT '',
    attachments      TEXT    NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee
    ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_dri
    ON tasks(dri);
CREATE INDEX IF NOT EXISTS idx_tasks_repo
    ON tasks(repo);
CREATE INDEX IF NOT EXISTS idx_tasks_branch
    ON tasks(branch);
CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON tasks(project);
""",

    # --- V3: mailbox table ---
    """\
CREATE TABLE IF NOT EXISTS mailbox (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sender         TEXT    NOT NULL,
    recipient      TEXT    NOT NULL,
    body           TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    delivered_at   TEXT,
    seen_at        TEXT,
    processed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_mailbox_recipient_unread
    ON mailbox(recipient, delivered_at)
    WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mailbox_sender
    ON mailbox(sender);
CREATE INDEX IF NOT EXISTS idx_mailbox_undelivered
    ON mailbox(id)
    WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mailbox_recipient_processed
    ON mailbox(recipient, processed_at)
    WHERE processed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mailbox_recipient_sender_processed
    ON mailbox(recipient, sender, processed_at)
    WHERE processed_at IS NOT NULL;
""",

    # --- V4: task_id on mailbox + messages ---
    """\
ALTER TABLE mailbox ADD COLUMN task_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_mailbox_task_id
    ON mailbox(task_id);
ALTER TABLE messages ADD COLUMN task_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_messages_task_id
    ON messages(task_id);
""",

    # --- V5: reviews + review_comments tables, review_attempt on tasks ---
    """\
ALTER TABLE tasks ADD COLUMN review_attempt INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    attempt    INTEGER NOT NULL,
    verdict    TEXT,
    summary    TEXT    NOT NULL DEFAULT '',
    reviewer   TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at TEXT,
    UNIQUE(task_id, attempt)
);

CREATE INDEX IF NOT EXISTS idx_reviews_task_id
    ON reviews(task_id);

CREATE TABLE IF NOT EXISTS review_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    attempt    INTEGER NOT NULL,
    file       TEXT    NOT NULL,
    line       INTEGER,
    body       TEXT    NOT NULL,
    author     TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_review_comments_task_attempt
    ON review_comments(task_id, attempt);
""",

    # --- V6: cache token columns on sessions ---
    """\
ALTER TABLE sessions ADD COLUMN cache_read_tokens INTEGER DEFAULT 0;
ALTER TABLE sessions ADD COLUMN cache_write_tokens INTEGER DEFAULT 0;
""",

    # --- V7: merge failure tracking ---
    """\
ALTER TABLE tasks ADD COLUMN status_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN merge_attempts INTEGER NOT NULL DEFAULT 0;
""",

    # --- V8: task_comments table ---
    """\
CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    author     TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_task_comments_task_id ON task_comments(task_id);
""",

    # --- V9: merge mailbox into messages ---
    """\
-- Add lifecycle columns to messages table
ALTER TABLE messages ADD COLUMN delivered_at TEXT;
ALTER TABLE messages ADD COLUMN seen_at TEXT;
ALTER TABLE messages ADD COLUMN processed_at TEXT;

-- Copy lifecycle data from mailbox to messages for existing chat messages
-- Match on sender, recipient, content (body in mailbox, content in messages)
UPDATE messages
SET delivered_at = (
    SELECT mb.delivered_at FROM mailbox mb
    WHERE mb.sender = messages.sender
      AND mb.recipient = messages.recipient
      AND mb.body = messages.content
      AND mb.task_id IS messages.task_id
    LIMIT 1
),
seen_at = (
    SELECT mb.seen_at FROM mailbox mb
    WHERE mb.sender = messages.sender
      AND mb.recipient = messages.recipient
      AND mb.body = messages.content
      AND mb.task_id IS messages.task_id
    LIMIT 1
),
processed_at = (
    SELECT mb.processed_at FROM mailbox mb
    WHERE mb.sender = messages.sender
      AND mb.recipient = messages.recipient
      AND mb.body = messages.content
      AND mb.task_id IS messages.task_id
    LIMIT 1
)
WHERE type = 'chat';

-- Insert any mailbox-only rows (the deliver() bug where messages were not logged to chat)
INSERT INTO messages (timestamp, sender, recipient, content, type, task_id, delivered_at, seen_at, processed_at)
SELECT mb.created_at, mb.sender, mb.recipient, mb.body, 'chat', mb.task_id, mb.delivered_at, mb.seen_at, mb.processed_at
FROM mailbox mb
WHERE NOT EXISTS (
    SELECT 1 FROM messages m
    WHERE m.sender = mb.sender
      AND m.recipient = mb.recipient
      AND m.content = mb.body
      AND m.task_id IS mb.task_id
);

-- Create indexes for efficient unread queries (replicate mailbox indexes)
CREATE INDEX IF NOT EXISTS idx_messages_recipient_unread
    ON messages(recipient, delivered_at)
    WHERE type = 'chat' AND processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_messages_sender
    ON messages(sender)
    WHERE type = 'chat';

CREATE INDEX IF NOT EXISTS idx_messages_undelivered
    ON messages(id)
    WHERE type = 'chat' AND delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_messages_recipient_processed
    ON messages(recipient, processed_at)
    WHERE type = 'chat' AND processed_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_recipient_sender_processed
    ON messages(recipient, sender, processed_at)
    WHERE type = 'chat' AND processed_at IS NOT NULL;

-- Drop the mailbox table
DROP TABLE IF EXISTS mailbox;
""",

    # --- V10: magic commands support ---
    """\
-- Add 'result' column to store command output as JSON
ALTER TABLE messages ADD COLUMN result TEXT;

-- Add 'command' to the allowed message types
-- SQLite doesn't support ALTER TABLE to modify CHECK constraints,
-- so we recreate the table with the updated constraint.
CREATE TABLE messages_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sender      TEXT    NOT NULL,
    recipient   TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    type        TEXT    NOT NULL CHECK(type IN ('chat', 'event', 'command')),
    task_id     INTEGER,
    delivered_at TEXT,
    seen_at     TEXT,
    processed_at TEXT,
    result      TEXT
);

-- Copy all data from old table to new table
INSERT INTO messages_new (id, timestamp, sender, recipient, content, type, task_id, delivered_at, seen_at, processed_at, result)
SELECT id, timestamp, sender, recipient, content, type, task_id, delivered_at, seen_at, processed_at, result
FROM messages;

-- Drop old table
DROP TABLE messages;

-- Rename new table to original name
ALTER TABLE messages_new RENAME TO messages;

-- Recreate all indexes
CREATE INDEX IF NOT EXISTS idx_messages_type
    ON messages(type);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp
    ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient
    ON messages(sender, recipient);
CREATE INDEX IF NOT EXISTS idx_messages_task_id
    ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_unread
    ON messages(recipient, delivered_at)
    WHERE type = 'chat' AND processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_sender
    ON messages(sender)
    WHERE type = 'chat';
CREATE INDEX IF NOT EXISTS idx_messages_undelivered
    ON messages(id)
    WHERE type = 'chat' AND delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_recipient_processed
    ON messages(recipient, processed_at)
    WHERE type = 'chat' AND processed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_recipient_sender_processed
    ON messages(recipient, sender, processed_at)
    WHERE type = 'chat' AND processed_at IS NOT NULL;
""",

    # --- V11: composite indexes for activity queries ---
    """\
-- Composite indexes to optimize task activity timeline queries
CREATE INDEX IF NOT EXISTS idx_messages_task_type
    ON messages(task_id, type);
CREATE INDEX IF NOT EXISTS idx_messages_task_ts
    ON messages(task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_task_comments_task_ts
    ON task_comments(task_id, created_at);
""",

    # --- V12: Multi-team support ---
    """\
-- Create teams metadata table
CREATE TABLE IF NOT EXISTS teams (
    name        TEXT PRIMARY KEY,
    team_id     TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Add team column to messages (requires table recreation since V10 just recreated it)
CREATE TABLE messages_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sender      TEXT    NOT NULL,
    recipient   TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    type        TEXT    NOT NULL CHECK(type IN ('chat', 'event', 'command')),
    task_id     INTEGER,
    delivered_at TEXT,
    seen_at     TEXT,
    processed_at TEXT,
    result      TEXT,
    team        TEXT    NOT NULL DEFAULT ''
);

INSERT INTO messages_new (id, timestamp, sender, recipient, content, type, task_id, delivered_at, seen_at, processed_at, result, team)
SELECT id, timestamp, sender, recipient, content, type, task_id, delivered_at, seen_at, processed_at, result, ''
FROM messages;

DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;

-- Recreate all messages indexes with team
CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(type);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient ON messages(sender, recipient);
CREATE INDEX IF NOT EXISTS idx_messages_task_id ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_unread
    ON messages(recipient, delivered_at)
    WHERE type = 'chat' AND processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_sender
    ON messages(sender)
    WHERE type = 'chat';
CREATE INDEX IF NOT EXISTS idx_messages_undelivered
    ON messages(id)
    WHERE type = 'chat' AND delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_recipient_processed
    ON messages(recipient, processed_at)
    WHERE type = 'chat' AND processed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_recipient_sender_processed
    ON messages(recipient, sender, processed_at)
    WHERE type = 'chat' AND processed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_task_type
    ON messages(task_id, type);
CREATE INDEX IF NOT EXISTS idx_messages_task_ts
    ON messages(task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_team_recipient ON messages(team, recipient);

-- Add team column to sessions
ALTER TABLE sessions ADD COLUMN team TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_sessions_team_agent ON sessions(team, agent);
CREATE INDEX IF NOT EXISTS idx_sessions_team_task_id ON sessions(team, task_id);

-- Add team column to tasks
ALTER TABLE tasks ADD COLUMN team TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_tasks_team_status ON tasks(team, status);
CREATE INDEX IF NOT EXISTS idx_tasks_team_id ON tasks(team, id);

-- Add team column to reviews
ALTER TABLE reviews ADD COLUMN team TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_reviews_team_task_id ON reviews(team, task_id);

-- Add team column to review_comments
ALTER TABLE review_comments ADD COLUMN team TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_review_comments_team_task_attempt ON review_comments(team, task_id, attempt);

-- Add team column to task_comments
ALTER TABLE task_comments ADD COLUMN team TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_task_comments_team_task_id ON task_comments(team, task_id);
""",

    # --- V13: Workflow columns on tasks ---
    """\
ALTER TABLE tasks ADD COLUMN workflow TEXT NOT NULL DEFAULT 'default';
ALTER TABLE tasks ADD COLUMN workflow_version INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow);
""",

    # --- V14: Free-form metadata JSON on tasks + rename standard→default workflow ---
    """\
ALTER TABLE tasks ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';
UPDATE tasks SET workflow = 'default' WHERE workflow = 'standard';
""",
]

# Columns that store JSON arrays and need parse/serialize on read/write.
_JSON_LIST_COLUMNS = frozenset({"tags", "depends_on", "attachments", "repo"})

# Columns that store JSON dicts (keyed by repo name for multi-repo).
_JSON_DICT_COLUMNS = frozenset({"commits", "base_sha", "merge_base", "merge_tip", "metadata"})

# Union of both — kept for external callers.
_JSON_COLUMNS = _JSON_LIST_COLUMNS | _JSON_DICT_COLUMNS


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

def _current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0."""
    row = conn.execute(
        "SELECT MAX(version) FROM schema_meta"
    ).fetchone()
    return row[0] or 0


def ensure_schema(hc_home: Path, team: str = "") -> None:
    """Apply any pending migrations to the global database.

    Safe to call repeatedly — each migration runs at most once.
    Call this at daemon startup or lazily before first DB access.

    Each migration step is wrapped in an explicit transaction so that all
    statements (including DDL) plus the version bump are applied atomically.
    SQLite supports transactional DDL — if any statement fails the entire
    migration is rolled back and no version is recorded.

    Note: team parameter is kept for backward compatibility but is no longer used.
    """
    # Set up paths and version info
    path = global_db_path(hc_home)
    key = str(hc_home)
    current_version = len(MIGRATIONS)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Use isolation_level=None (autocommit) so Python's sqlite3 module
    # does not silently start or commit transactions behind our back.
    # We manage BEGIN / COMMIT / ROLLBACK explicitly.
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Bootstrap the meta table (always idempotent).
    conn.execute("BEGIN")
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
                       DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    conn.execute("COMMIT")

    current = _current_version(conn)

    # If already at current version, update cache and return
    if current == current_version:
        with _schema_lock:
            _schema_verified[key] = current_version
        conn.close()
        return

    pending = MIGRATIONS[current:]

    for i, sql in enumerate(pending, start=current + 1):
        logger.info("Applying migration V%d to global DB …", i)
        stmts = [s.strip() for s in sql.split(";") if s.strip()]
        try:
            # BEGIN IMMEDIATE acquires a write-lock up front, preventing
            # other writers from sneaking in between statements.
            conn.execute("BEGIN IMMEDIATE")
            for stmt in stmts:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta (version) VALUES (?)", (i,)
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        logger.info("Migration V%d applied", i)

    # Update cache to avoid redundant checks on subsequent calls
    with _schema_lock:
        _schema_verified[key] = current_version

    conn.close()


def get_connection(hc_home: Path, team: str = "") -> sqlite3.Connection:
    """Open a connection to the global DB with row_factory and ensure schema is current.

    Callers are responsible for closing the connection.

    Note: team parameter is kept for backward compatibility but is no longer used.
    """
    ensure_schema(hc_home, team)
    path = global_db_path(hc_home)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def task_row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a tasks table row to a plain dict, deserializing JSON columns.

    Enforces element types:
      repo        → list[str]   (repo names, multi-repo)
      depends_on  → list[int]   (task IDs)
      tags        → list[str]
      attachments → list[str]   (file paths)
      commits     → dict[str, list[str]]  (repo → commit SHAs)
      base_sha    → dict[str, str]        (repo → base SHA)
      merge_base  → dict[str, str]        (repo → merge base)
      merge_tip   → dict[str, str]        (repo → merge tip)
    """
    d = dict(row)

    # --- JSON list columns ---
    for col in _JSON_LIST_COLUMNS:
        raw = d.get(col, "[]")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                # Backward compat: if a plain string was stored (e.g. old repo field),
                # wrap it in a list.
                if isinstance(parsed, str):
                    d[col] = [parsed] if parsed else []
                elif isinstance(parsed, list):
                    d[col] = parsed
                else:
                    d[col] = []
            except (json.JSONDecodeError, TypeError):
                # Non-JSON plain string (legacy repo = "myrepo")
                if raw and raw != "[]":
                    d[col] = [raw]
                else:
                    d[col] = []

    # --- JSON dict columns (multi-repo keyed by repo name) ---
    for col in _JSON_DICT_COLUMNS:
        raw = d.get(col, "{}")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    d[col] = parsed
                elif isinstance(parsed, list):
                    # Backward compat: old commits were a flat list.
                    repos = d.get("repo", [])
                    first_repo = repos[0] if repos else "_default"
                    d[col] = {first_repo: parsed} if parsed else {}
                elif isinstance(parsed, str) and parsed:
                    # Backward compat: plain string SHA (legacy base_sha = "abc123")
                    repos = d.get("repo", [])
                    first_repo = repos[0] if repos else "_default"
                    d[col] = {first_repo: parsed}
                else:
                    d[col] = {}
            except (json.JSONDecodeError, TypeError):
                # Non-JSON plain string (legacy base_sha = "abc123")
                if raw and raw != "{}" and raw != "[]" and raw != "":
                    repos = d.get("repo", [])
                    first_repo = repos[0] if repos else "_default"
                    d[col] = {first_repo: raw}
                else:
                    d[col] = {}

    # Coerce element types
    if d.get("depends_on"):
        d["depends_on"] = [int(x) for x in d["depends_on"]]
    if d.get("tags"):
        d["tags"] = [str(x) for x in d["tags"]]
    if d.get("attachments"):
        d["attachments"] = [str(x) for x in d["attachments"]]
    if d.get("repo"):
        d["repo"] = [str(x) for x in d["repo"]]
    # commits values are lists of strings keyed by repo
    if d.get("commits"):
        d["commits"] = {str(k): [str(v) for v in vs] for k, vs in d["commits"].items()}
    return d
