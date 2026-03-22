"""Global SQLite database with file-based versioned migrations.

All teams share one database at ``<DELEGATE_HOME>/protected/db.sqlite``.
On first access the ``schema_meta`` table is created and pending migrations
are applied in order.  Each migration is idempotent (uses ``IF NOT EXISTS``).

Migrations live as numbered SQL files in ``delegate/migrations/V001.sql``,
``V002.sql``, etc.  ``ensure_schema()`` discovers them at import time,
creates an automatic backup before applying new ones, runs an integrity
check afterwards, and restores the backup on failure.

**Connection pooling** — ``get_connection()`` maintains a per-thread pool
(via ``threading.local()``) so that successive calls on the same thread
reuse an existing ``sqlite3.Connection`` instead of opening a fresh one
each time.  This significantly reduces connection overhead during
high-concurrency agent turns where ``asyncio.to_thread`` dispatches work
to a thread pool.  Call ``close_thread_connections()`` to release all
pooled connections for the current thread.

Usage::

    from delegate.db import get_connection, close_thread_connections

    # For individual operations (connection is pooled per-thread):
    conn = get_connection(hc_home, project)
    conn.execute(...)
    conn.commit()
    # No need to call conn.close() — the pool reuses the connection.

    # When the thread is about to exit, release pooled connections:
    close_thread_connections()
"""

import json
import logging
import re
import shutil
import sqlite3
import threading
import uuid as uuid_module
from pathlib import Path

from delegate.paths import global_db_path, protected_dir, resolve_team_uuid

logger = logging.getLogger(__name__)

# Per-process cache to avoid redundant schema checks
# Changed to use just hc_home since we now have a global DB
_schema_verified: dict[str, int] = {}
_schema_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Per-thread connection pool
# ---------------------------------------------------------------------------
# Reuse SQLite connections within the same thread to avoid the overhead of
# opening (and WAL-pragmaing) a fresh connection on every get_connection()
# call.  Each thread keeps at most one connection per database path.
# Callers that still call conn.close() will simply cause the next
# get_connection() to transparently open a replacement.

_thread_local = threading.local()

# ---------------------------------------------------------------------------
# Migration registry  (file-based)
# ---------------------------------------------------------------------------
# Migrations live in delegate/migrations/V{NNN}.sql.  They are loaded once
# at import time and cached in MIGRATIONS.  To add a new migration, create
# a new V{N+1}.sql file — NEVER reorder or modify existing files.

def _load_migrations() -> list[str]:
    """Load migration SQL from delegate/migrations/V{NNN}.sql files.

    Files are discovered by scanning the migrations package directory and
    sorted numerically by version number.  Returns a list of SQL strings
    where index 0 is V001, index 1 is V002, etc.
    """
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.is_dir():
        return []

    files: list[tuple[int, Path]] = []
    for p in migrations_dir.iterdir():
        m = re.match(r"^V(\d+)\.sql$", p.name)
        if m:
            files.append((int(m.group(1)), p))

    files.sort(key=lambda t: t[0])

    # Verify no gaps
    for idx, (version, _path) in enumerate(files, start=1):
        if version != idx:
            raise RuntimeError(
                f"Migration gap: expected V{idx:03d}.sql but found V{version:03d}.sql"
            )

    return [p.read_text() for _, p in files]


MIGRATIONS: list[str] = _load_migrations()

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


def _backfill_uuid_tables(conn: sqlite3.Connection, hc_home: Path) -> None:
    """Backfill project_ids and member_ids tables from existing data.

    This function is idempotent and safe to call multiple times.
    It populates:
    1. project_ids from the projects table
    2. member_ids from filesystem (agents) and members/*.yaml (humans)
    3. *_uuid columns in all data tables

    Args:
        conn: Database connection (should be in autocommit mode)
        hc_home: Delegate home directory
    """
    # Check if project_ids table exists (V15+V18 applied).
    # Also accept the legacy name team_ids (V15 only, before V18 ran).
    ids_table = None
    for candidate in ("project_ids", "team_ids"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (candidate,),
        ).fetchone()
        if row:
            ids_table = candidate
            break
    if ids_table is None:
        # V15 not yet applied, skip backfill
        return

    # Determine which projects/teams table name to use (V18 may not have run yet)
    projects_table = None
    for candidate in ("projects", "teams"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (candidate,),
        ).fetchone()
        if row:
            projects_table = candidate
            break
    if projects_table is None:
        return

    # -------------------------------------------------------------------------
    # Part 1: Backfill project_ids (or team_ids) from projects (or teams) table
    # -------------------------------------------------------------------------
    proj_id_col = "project_id" if projects_table == "projects" else "team_id"
    projects_rows = conn.execute(
        f"SELECT name, {proj_id_col} FROM {projects_table}"
    ).fetchall()
    for team_name, team_id in projects_rows:
        # INSERT OR IGNORE to handle re-runs
        conn.execute(
f"INSERT OR IGNORE INTO {ids_table} (uuid, name) VALUES (?, ?)",
            (team_id, team_name)
        )

    # -------------------------------------------------------------------------
    # Part 2: Backfill member_ids from filesystem
    # -------------------------------------------------------------------------
    from delegate.paths import teams_dir as _teams_dir
    projects_dir = _teams_dir(hc_home)
    if projects_dir.is_dir():
        for team_dir in projects_dir.iterdir():
            if not team_dir.is_dir():
                continue
            # Directory names are UUIDs (not human-readable team names)
            dir_name = team_dir.name

            # Try to match by UUID first (new layout), then by name (legacy)
            team_row = conn.execute(
                f"SELECT uuid FROM {ids_table} WHERE uuid = ? AND deleted = 0",
                (dir_name,)
            ).fetchone()
            if not team_row:
                # Legacy fallback: directory might still be named by team name
                team_row = conn.execute(
                    f"SELECT uuid FROM {ids_table} WHERE name = ? AND deleted = 0",
                    (dir_name,)
                ).fetchone()
            if not team_row:
                continue
            team_uuid = team_row[0]

            # Scan agents
            agents_dir = team_dir / "agents"
            if agents_dir.is_dir():
                for agent_dir in agents_dir.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    agent_name = agent_dir.name
                    conn.execute(
                        "INSERT OR IGNORE INTO member_ids (uuid, kind, team_uuid, name) VALUES (?, ?, ?, ?)",
                        (uuid_module.uuid4().hex, "agent", team_uuid, agent_name)
                    )

    # Scan humans (now in protected/members/)
    from delegate.paths import members_dir as _members_dir
    members_dir = _members_dir(hc_home)
    if members_dir.is_dir():
        for member_file in members_dir.glob("*.yaml"):
            human_name = member_file.stem
            conn.execute(
                "INSERT OR IGNORE INTO member_ids (uuid, kind, team_uuid, name) VALUES (?, ?, ?, ?)",
                (uuid_module.uuid4().hex, "human", None, human_name)
            )

    # -------------------------------------------------------------------------
    # Part 3: Backfill *_uuid columns in data tables (only if V16 applied)
    # -------------------------------------------------------------------------
    # Check if messages has project_uuid (V18) or team_uuid (V16 before V18) column
    cursor = conn.execute("PRAGMA table_info(messages)")
    columns = {row[1] for row in cursor.fetchall()}
    uuid_col = "project_uuid" if "project_uuid" in columns else (
        "team_uuid" if "team_uuid" in columns else None
    )
    if uuid_col is None:
        # V16 not yet applied, skip UUID column backfill
        return

    # Determine project/team column name used in data tables (project or team)
    proj_col = "project" if "project" in columns else "team"

    # Messages table
    conn.execute(f"""
        UPDATE messages
        SET {uuid_col} = COALESCE(
            (SELECT uuid FROM {ids_table} WHERE name = messages.{proj_col} AND deleted = 0),
            ''
        )
        WHERE {uuid_col} = ''
    """)

    # For sender_uuid and recipient_uuid, we need to try agent first then human
    # This is complex in SQL, so we'll do it row by row in Python for the backfill
    messages_to_update = conn.execute(
        f"SELECT id, {proj_col}, sender, recipient FROM messages WHERE sender_uuid = ''"
    ).fetchall()
    for msg_id, project, sender, recipient in messages_to_update:
        # Get team UUID
        team_uuid_row = conn.execute(
            f"SELECT uuid FROM {ids_table} WHERE name = ? AND deleted = 0", (project,)
        ).fetchone()
        if not team_uuid_row:
            continue
        team_uuid = team_uuid_row[0]

        # Resolve sender (try agent first, then human)
        sender_uuid = None
        row = conn.execute(
            "SELECT uuid FROM member_ids WHERE kind = 'agent' AND team_uuid = ? AND name = ? AND deleted = 0",
            (team_uuid, sender)
        ).fetchone()
        if row:
            sender_uuid = row[0]
        else:
            row = conn.execute(
                "SELECT uuid FROM member_ids WHERE kind = 'human' AND team_uuid IS NULL AND name = ? AND deleted = 0",
                (sender,)
            ).fetchone()
            if row:
                sender_uuid = row[0]

        # Resolve recipient
        recipient_uuid = None
        row = conn.execute(
            "SELECT uuid FROM member_ids WHERE kind = 'agent' AND team_uuid = ? AND name = ? AND deleted = 0",
            (team_uuid, recipient)
        ).fetchone()
        if row:
            recipient_uuid = row[0]
        else:
            row = conn.execute(
                "SELECT uuid FROM member_ids WHERE kind = 'human' AND team_uuid IS NULL AND name = ? AND deleted = 0",
                (recipient,)
            ).fetchone()
            if row:
                recipient_uuid = row[0]

        # Update message
        if sender_uuid and recipient_uuid:
            conn.execute(
                "UPDATE messages SET sender_uuid = ?, recipient_uuid = ? WHERE id = ?",
                (sender_uuid, recipient_uuid, msg_id)
            )

    # Sessions table
    conn.execute(f"""
        UPDATE sessions
        SET {uuid_col} = COALESCE(
            (SELECT uuid FROM {ids_table} WHERE name = sessions.{proj_col} AND deleted = 0),
            ''
        ),
        agent_uuid = COALESCE(
            (SELECT m.uuid FROM member_ids m
             JOIN {ids_table} t ON m.team_uuid = t.uuid
             WHERE m.kind = 'agent' AND t.name = sessions.{proj_col} AND m.name = sessions.agent AND m.deleted = 0),
            ''
        )
        WHERE {uuid_col} = ''
    """)

    # Tasks table
    # Note: tasks.team column is NOT renamed (collision with existing tasks.project label
    # column from V002). Use 'team' column unconditionally for tasks.
    tasks_team_col = "team"
    tasks_to_update = conn.execute(
        f"SELECT id, {tasks_team_col}, dri, assignee FROM tasks WHERE {uuid_col} = ''"
    ).fetchall()
    for task_id, project, dri, assignee in tasks_to_update:
        team_uuid_row = conn.execute(
            f"SELECT uuid FROM {ids_table} WHERE name = ? AND deleted = 0", (project,)
        ).fetchone()
        if not team_uuid_row:
            continue
        team_uuid = team_uuid_row[0]

        # Resolve DRI (flexible)
        dri_uuid = ''
        if dri:
            row = conn.execute(
                "SELECT uuid FROM member_ids WHERE kind = 'agent' AND team_uuid = ? AND name = ? AND deleted = 0",
                (team_uuid, dri)
            ).fetchone()
            if row:
                dri_uuid = row[0]
            else:
                row = conn.execute(
                    "SELECT uuid FROM member_ids WHERE kind = 'human' AND team_uuid IS NULL AND name = ? AND deleted = 0",
                    (dri,)
                ).fetchone()
                if row:
                    dri_uuid = row[0]

        # Resolve assignee (flexible)
        assignee_uuid = ''
        if assignee:
            row = conn.execute(
                "SELECT uuid FROM member_ids WHERE kind = 'agent' AND team_uuid = ? AND name = ? AND deleted = 0",
                (team_uuid, assignee)
            ).fetchone()
            if row:
                assignee_uuid = row[0]
            else:
                row = conn.execute(
                    "SELECT uuid FROM member_ids WHERE kind = 'human' AND team_uuid IS NULL AND name = ? AND deleted = 0",
                    (assignee,)
                ).fetchone()
                if row:
                    assignee_uuid = row[0]

        conn.execute(
            f"UPDATE tasks SET {uuid_col} = ?, dri_uuid = ?, assignee_uuid = ? WHERE id = ?",
            (team_uuid, dri_uuid, assignee_uuid, task_id)
        )

    # Task comments table
    # Note: tasks.team is used here (not proj_col) because tasks.team is the team name column.
    # tasks.team was NOT renamed to tasks.project (collision with the existing tasks.project
    # label column from V002). proj_col would resolve to "project" post-V018 — wrong column.
    conn.execute(f"""
        UPDATE task_comments
        SET {uuid_col} = COALESCE(
            (SELECT t.uuid FROM tasks tk
             JOIN {ids_table} t ON t.name = tk.team
             WHERE tk.id = task_comments.task_id AND t.deleted = 0),
            ''
        )
        WHERE {uuid_col} = ''
    """)

    # For author_uuid, need flexible resolution
    comments_to_update = conn.execute(
        f"SELECT task_comments.id, tasks.team, task_comments.author FROM task_comments "
        "JOIN tasks ON task_comments.task_id = tasks.id "
        "WHERE task_comments.author_uuid = ''"
    ).fetchall()
    for comment_id, project, author in comments_to_update:
        team_uuid_row = conn.execute(
            f"SELECT uuid FROM {ids_table} WHERE name = ? AND deleted = 0", (project,)
        ).fetchone()
        if not team_uuid_row:
            continue
        team_uuid = team_uuid_row[0]

        author_uuid = ''
        row = conn.execute(
            "SELECT uuid FROM member_ids WHERE kind = 'agent' AND team_uuid = ? AND name = ? AND deleted = 0",
            (team_uuid, author)
        ).fetchone()
        if row:
            author_uuid = row[0]
        else:
            row = conn.execute(
                "SELECT uuid FROM member_ids WHERE kind = 'human' AND team_uuid IS NULL AND name = ? AND deleted = 0",
                (author,)
            ).fetchone()
            if row:
                author_uuid = row[0]

        if author_uuid:
            conn.execute(
                "UPDATE task_comments SET author_uuid = ? WHERE id = ?",
                (author_uuid, comment_id)
            )

    # Reviews table
    # Use tasks.team (not proj_col) — tasks.team is the team name; tasks.project is the label.
    reviews_to_update = conn.execute(
        f"SELECT reviews.id, tasks.team, reviews.reviewer FROM reviews "
        f"JOIN tasks ON reviews.task_id = tasks.id "
        f"WHERE reviews.{uuid_col} = ''"
    ).fetchall()
    for review_id, project, reviewer in reviews_to_update:
        team_uuid_row = conn.execute(
            f"SELECT uuid FROM {ids_table} WHERE name = ? AND deleted = 0", (project,)
        ).fetchone()
        if not team_uuid_row:
            continue
        team_uuid = team_uuid_row[0]

        reviewer_uuid = ''
        if reviewer:
            row = conn.execute(
                "SELECT uuid FROM member_ids WHERE kind = 'agent' AND team_uuid = ? AND name = ? AND deleted = 0",
                (team_uuid, reviewer)
            ).fetchone()
            if row:
                reviewer_uuid = row[0]
            else:
                row = conn.execute(
                    "SELECT uuid FROM member_ids WHERE kind = 'human' AND team_uuid IS NULL AND name = ? AND deleted = 0",
                    (reviewer,)
                ).fetchone()
                if row:
                    reviewer_uuid = row[0]

        conn.execute(
            f"UPDATE reviews SET {uuid_col} = ?, reviewer_uuid = ? WHERE id = ?",
            (team_uuid, reviewer_uuid, review_id)
        )

    # Review comments table
    # Use tasks.team (not proj_col) — tasks.team is the team name; tasks.project is the label.
    review_comments_to_update = conn.execute(
        f"SELECT review_comments.id, tasks.team, review_comments.author FROM review_comments "
        f"JOIN tasks ON review_comments.task_id = tasks.id "
        f"WHERE review_comments.{uuid_col} = ''"
    ).fetchall()
    for rc_id, project, author in review_comments_to_update:
        team_uuid_row = conn.execute(
            f"SELECT uuid FROM {ids_table} WHERE name = ? AND deleted = 0", (project,)
        ).fetchone()
        if not team_uuid_row:
            continue
        team_uuid = team_uuid_row[0]

        author_uuid = ''
        row = conn.execute(
            "SELECT uuid FROM member_ids WHERE kind = 'agent' AND team_uuid = ? AND name = ? AND deleted = 0",
            (team_uuid, author)
        ).fetchone()
        if row:
            author_uuid = row[0]
        else:
            row = conn.execute(
                "SELECT uuid FROM member_ids WHERE kind = 'human' AND team_uuid IS NULL AND name = ? AND deleted = 0",
                (author,)
            ).fetchone()
            if row:
                author_uuid = row[0]

        if author_uuid:
            conn.execute(
                "UPDATE review_comments SET author_uuid = ? WHERE id = ?",
                (author_uuid, rc_id)
            )


def _backup_db(db_path: Path, version: int, hc_home: Path) -> Path | None:
    """Create a backup of the DB before applying migration *version*.

    Backup is stored under ``protected/db.sqlite.bak.V{version}``.
    Returns the backup path, or None if the source DB doesn't exist yet.
    """
    if not db_path.exists():
        return None

    backup_dir = protected_dir(hc_home)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"db.sqlite.bak.V{version}"
    shutil.copy2(str(db_path), str(backup_path))
    logger.info("DB backup created: %s", backup_path)
    return backup_path


def _verify_db_health(conn: sqlite3.Connection) -> None:
    """Run a quick integrity check on the database.

    Raises RuntimeError if the DB is corrupt.
    """
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result[0] != "ok":
        raise RuntimeError(f"DB integrity check failed: {result[0]}")

    # Verify expected core tables exist
    expected_tables = {"messages", "sessions", "tasks", "schema_meta"}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    actual_tables = {row[0] for row in rows}
    missing = expected_tables - actual_tables
    if missing:
        raise RuntimeError(f"DB health check: missing tables {missing}")


def _validate_hc_home(hc_home: Path) -> None:
    """Raise ValueError if hc_home looks like a team subdirectory rather than
    the real delegate home.

    The real hc_home (~/.delegate) never contains /projects/ in its path.
    A team directory (~/.delegate/projects/<id>/) does. Passing a team
    directory causes the global database to be silently created in the wrong
    location, so we reject it loudly instead.
    """
    parts = hc_home.resolve().parts
    if "projects" in parts or "teams" in parts:
        raise ValueError(
            f"hc_home looks like a team directory, not the delegate home: {hc_home}. "
            f"Pass ~/.delegate (or DELEGATE_HOME) as hc_home, not a team subdirectory."
        )


def ensure_schema(hc_home: Path, team: str = "") -> None:
    """Apply any pending migrations to the global database.

    Safe to call repeatedly — each migration runs at most once.
    Call this at daemon startup or lazily before first DB access.

    Each migration step is wrapped in an explicit transaction so that all
    statements (including DDL) plus the version bump are applied atomically.
    SQLite supports transactional DDL — if any statement fails the entire
    migration is rolled back and the pre-migration backup is restored.

    Before applying migrations, an automatic backup is created at
    ``protected/db.sqlite.bak.V{N}`` where N is the first migration
    being applied.

    After all migrations succeed, a quick integrity + table-existence
    check is performed.

    Note: team parameter is kept for backward compatibility but is no longer used.
    """
    _validate_hc_home(hc_home)
    key = str(hc_home)
    current_version = len(MIGRATIONS)

    # Fast path: skip if schema already verified for this hc_home
    with _schema_lock:
        if _schema_verified.get(key) == current_version:
            return

    # Set up paths and version info
    path = global_db_path(hc_home)
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
    first_pending_version = current + 1

    # --- Backup before applying migrations ---
    backup_path = _backup_db(path, first_pending_version, hc_home)

    try:
        for i, sql in enumerate(pending, start=first_pending_version):
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

        # --- Health verification after all migrations ---
        _verify_db_health(conn)

    except Exception:
        conn.close()
        # Restore backup if it exists
        if backup_path and backup_path.exists():
            logger.error(
                "Migration failed — restoring DB from backup %s", backup_path
            )
            shutil.copy2(str(backup_path), str(path))
        raise

    # Backfill UUID tables after migrations complete
    # This is idempotent and safe to run on every startup
    _backfill_uuid_tables(conn, hc_home)

    # Update cache to avoid redundant checks on subsequent calls
    with _schema_lock:
        _schema_verified[key] = current_version

    conn.close()


def _is_connection_usable(conn: sqlite3.Connection) -> bool:
    """Return True if *conn* is open and can execute a trivial query."""
    try:
        conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def get_connection(hc_home: Path, team: str = "") -> sqlite3.Connection:
    """Return a per-thread cached connection to the global DB.

    Connections are stored in ``threading.local()`` and reused across
    successive calls **on the same thread** for the same database path.
    This avoids the overhead of opening a fresh ``sqlite3.Connection``
    (plus WAL pragma) on every call — which matters during high-concurrency
    agent turns where ``asyncio.to_thread`` dispatches work to a pool.

    If a caller explicitly closes the returned connection, the next call
    will transparently open a replacement.

    Use :func:`close_thread_connections` to release all pooled connections
    for the current thread when the thread is about to exit or when you
    want to force fresh connections.

    Note: team parameter is kept for backward compatibility but is no longer used.
    """
    ensure_schema(hc_home, team)
    path = str(global_db_path(hc_home))

    pool: dict[str, sqlite3.Connection] | None = getattr(
        _thread_local, "connections", None
    )
    if pool is None:
        pool = {}
        _thread_local.connections = pool

    conn = pool.get(path)
    if conn is not None and _is_connection_usable(conn):
        return conn

    # Evict the stale/closed entry (if any) before inserting a fresh one.
    pool.pop(path, None)

    # Open a new connection and cache it for this thread + path.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    pool[path] = conn
    return conn


def close_thread_connections() -> None:
    """Close and discard all pooled connections for the **current** thread.

    Call this when a thread is about to exit or when you want to force
    subsequent ``get_connection()`` calls to open fresh connections.
    """
    pool: dict[str, sqlite3.Connection] | None = getattr(_thread_local, "connections", None)
    if pool is None:
        return
    for conn in pool.values():
        try:
            conn.close()
        except Exception:
            pass
    pool.clear()



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
