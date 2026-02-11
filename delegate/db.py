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
from pathlib import Path

from delegate.paths import db_path

logger = logging.getLogger(__name__)

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
]

# Columns that store JSON arrays and need parse/serialize on read/write.
_JSON_LIST_COLUMNS = frozenset({"tags", "depends_on", "attachments", "repo"})

# Columns that store JSON dicts (keyed by repo name for multi-repo).
_JSON_DICT_COLUMNS = frozenset({"commits", "base_sha", "merge_base", "merge_tip"})

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


def ensure_schema(hc_home: Path, team: str) -> None:
    """Apply any pending migrations to the team's database.

    Safe to call repeatedly — each migration runs at most once.
    Call this at daemon startup or lazily before first DB access.

    Each migration step is wrapped in an explicit transaction so that all
    statements (including DDL) plus the version bump are applied atomically.
    SQLite supports transactional DDL — if any statement fails the entire
    migration is rolled back and no version is recorded.
    """
    path = db_path(hc_home, team)
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
    pending = MIGRATIONS[current:]

    for i, sql in enumerate(pending, start=current + 1):
        logger.info("Applying migration V%d to team DB …", i)
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

    conn.close()


def get_connection(hc_home: Path, team: str) -> sqlite3.Connection:
    """Open a connection to the team's DB with row_factory and ensure schema is current.

    Callers are responsible for closing the connection.
    """
    ensure_schema(hc_home, team)
    path = db_path(hc_home, team)
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
