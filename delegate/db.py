"""Centralized SQLite database with versioned migrations.

All state lives in ``~/.delegate/db.sqlite``.  On first access, the
``schema_meta`` table is created and pending migrations are applied in
order.  Each migration is idempotent (uses ``IF NOT EXISTS``).

Usage::

    from delegate.db import get_connection, ensure_schema

    # At daemon startup (or lazily on first query):
    ensure_schema(hc_home)

    # For individual operations:
    conn = get_connection(hc_home)
    ...
    conn.close()
"""

import json
import logging
import sqlite3
from pathlib import Path

import yaml

from delegate.paths import db_path, tasks_dir as _yaml_tasks_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------
# Each entry is a SQL script.  Migrations are numbered starting at 1.
# To add a new migration, append a new string to this list.
# NEVER reorder or modify existing entries — only append.

MIGRATIONS: list[str] = [
    # --- V1: messages + sessions (previously inline in chat.py / bootstrap.py) ---
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
    status           TEXT    NOT NULL DEFAULT 'open',
    dri              TEXT    NOT NULL DEFAULT '',
    assignee         TEXT    NOT NULL DEFAULT '',
    project          TEXT    NOT NULL DEFAULT '',
    priority         TEXT    NOT NULL DEFAULT 'medium',
    repo             TEXT    NOT NULL DEFAULT '',
    tags             TEXT    NOT NULL DEFAULT '[]',  -- JSON array of strings
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    completed_at     TEXT    NOT NULL DEFAULT '',
    depends_on       TEXT    NOT NULL DEFAULT '[]',  -- JSON array of ints (task IDs)
    branch           TEXT    NOT NULL DEFAULT '',
    base_sha         TEXT    NOT NULL DEFAULT '',
    commits          TEXT    NOT NULL DEFAULT '[]',  -- JSON array of strings (SHAs)
    rejection_reason TEXT    NOT NULL DEFAULT '',
    approval_status  TEXT    NOT NULL DEFAULT '',
    merge_base       TEXT    NOT NULL DEFAULT '',
    merge_tip        TEXT    NOT NULL DEFAULT ''
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

    # --- V3: mailbox table (replaces Maildir filesystem) ---
    # Lifecycle: created → delivered → seen → processed → read
    #   delivered_at  — router/send marked it ready for recipient
    #   seen_at       — agent control loop picked it up at turn start
    #   processed_at  — agent finished the turn (message is "done")
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
""",
]

# Columns that store JSON arrays and need parse/serialize on read/write.
_JSON_COLUMNS = frozenset({"tags", "depends_on", "commits"})


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

def _current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0."""
    row = conn.execute(
        "SELECT MAX(version) FROM schema_meta"
    ).fetchone()
    return row[0] or 0


def ensure_schema(hc_home: Path) -> None:
    """Apply any pending migrations to the database.

    Safe to call repeatedly — each migration runs at most once.
    Call this at daemon startup or lazily before first DB access.
    """
    path = db_path(hc_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")

    # Bootstrap the meta table (always idempotent).
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
                       DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    conn.commit()

    current = _current_version(conn)
    pending = MIGRATIONS[current:]

    for i, sql in enumerate(pending, start=current + 1):
        logger.info("Applying migration V%d …", i)
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_meta (version) VALUES (?)", (i,)
        )
        conn.commit()
        logger.info("Migration V%d applied", i)

    # One-time: import any leftover YAML tasks into SQLite.
    if current < 2:
        _import_yaml_tasks(conn, hc_home)

    conn.close()


def get_connection(hc_home: Path) -> sqlite3.Connection:
    """Open a connection with row_factory and ensure schema is current.

    Callers are responsible for closing the connection.
    """
    ensure_schema(hc_home)
    path = db_path(hc_home)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# YAML → SQLite one-time data migration
# ---------------------------------------------------------------------------

def _import_yaml_tasks(conn: sqlite3.Connection, hc_home: Path) -> None:
    """Import existing YAML task files into the tasks table.

    Only runs when the tasks table is empty and YAML files exist.
    After importing, the YAML files are left in place (harmless).
    """
    td = _yaml_tasks_dir(hc_home)
    if not td.is_dir():
        return

    yaml_files = sorted(td.glob("T*.yaml"))
    if not yaml_files:
        return

    # Check if tasks table already has data
    row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    if row[0] > 0:
        return

    imported = 0
    for f in yaml_files:
        try:
            task = yaml.safe_load(f.read_text())
        except Exception:
            logger.warning("Skipping unreadable YAML task: %s", f)
            continue
        if not task or "id" not in task:
            continue

        conn.execute(
            """\
            INSERT INTO tasks (
                id, title, description, status, dri, assignee,
                project, priority, repo, tags, created_at, updated_at,
                completed_at, depends_on, branch, base_sha, commits,
                rejection_reason, approval_status, merge_base, merge_tip
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )""",
            (
                task["id"],
                task.get("title", ""),
                task.get("description", ""),
                task.get("status", "open"),
                task.get("dri", task.get("assignee", "")),
                task.get("assignee", ""),
                task.get("project", ""),
                task.get("priority", "medium"),
                task.get("repo", ""),
                json.dumps(task.get("tags", [])),
                task.get("created_at", ""),
                task.get("updated_at", ""),
                task.get("completed_at", ""),
                json.dumps(task.get("depends_on", [])),
                task.get("branch", ""),
                task.get("base_sha", ""),
                json.dumps(task.get("commits", [])),
                task.get("rejection_reason", ""),
                task.get("approval_status", ""),
                task.get("merge_base", ""),
                task.get("merge_tip", ""),
            ),
        )
        imported += 1

    conn.commit()
    if imported:
        logger.info("Imported %d YAML tasks into SQLite", imported)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def task_row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a tasks table row to a plain dict, deserializing JSON columns.

    Enforces element types:
      depends_on → list[int]   (task IDs)
      commits    → list[str]   (commit SHAs)
      tags       → list[str]
    """
    d = dict(row)
    for col in _JSON_COLUMNS:
        raw = d.get(col, "[]")
        if isinstance(raw, str):
            try:
                d[col] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[col] = []
    # Coerce element types
    if d.get("depends_on"):
        d["depends_on"] = [int(x) for x in d["depends_on"]]
    if d.get("commits"):
        d["commits"] = [str(x) for x in d["commits"]]
    if d.get("tags"):
        d["tags"] = [str(x) for x in d["tags"]]
    return d
