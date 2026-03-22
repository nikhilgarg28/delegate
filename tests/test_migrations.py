"""Tests for the file-based migration system in delegate.db."""

import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from delegate.db import (
    MIGRATIONS,
    _backup_db,
    _load_migrations,
    _verify_db_health,
    ensure_schema,
    get_connection,
    global_db_path,
)
from delegate.paths import protected_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_hc(tmp_path):
    """Provide a temporary hc_home with protected/ dir."""
    hc = tmp_path / "hc"
    hc.mkdir()
    (hc / "protected").mkdir()
    yield hc
    # Clean up pooled connections so stale references don't leak across tests.
    from delegate.db import close_thread_connections
    close_thread_connections()


# ---------------------------------------------------------------------------
# Migration discovery
# ---------------------------------------------------------------------------

class TestMigrationDiscovery:
    def test_loads_all_migration_files(self):
        """_load_migrations discovers all V*.sql files in order."""
        migrations = _load_migrations()
        assert len(migrations) >= 16, f"Expected >=16 migrations, got {len(migrations)}"

    def test_migrations_are_non_empty(self):
        """Each migration SQL is non-empty."""
        for i, sql in enumerate(MIGRATIONS, start=1):
            assert sql.strip(), f"Migration V{i:03d} is empty"

    def test_migration_files_match_list(self):
        """MIGRATIONS list matches the loaded file contents."""
        fresh = _load_migrations()
        assert len(fresh) == len(MIGRATIONS)
        for i, (a, b) in enumerate(zip(fresh, MIGRATIONS), start=1):
            assert a == b, f"V{i:03d} content mismatch"

    def test_no_gaps_in_numbering(self):
        """Migration files are numbered consecutively without gaps."""
        migrations_dir = Path(__file__).parent.parent / "delegate" / "migrations"
        files = sorted(
            p for p in migrations_dir.iterdir()
            if re.match(r"^V\d+\.sql$", p.name)
        )
        for idx, f in enumerate(files, start=1):
            expected = f"V{idx:03d}.sql"
            assert f.name == expected, f"Expected {expected}, got {f.name}"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

class TestBackup:
    def test_backup_creates_file(self, tmp_hc):
        """_backup_db copies the DB to protected/."""
        db = global_db_path(tmp_hc)
        db.parent.mkdir(parents=True, exist_ok=True)
        # Create a minimal DB
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()

        backup = _backup_db(db, 5, tmp_hc)
        assert backup is not None
        assert backup.exists()
        assert "bak.V5" in backup.name

        # Verify backup is a valid DB with same data
        conn = sqlite3.connect(str(backup))
        row = conn.execute("SELECT id FROM t").fetchone()
        assert row[0] == 42
        conn.close()

    def test_backup_returns_none_for_nonexistent_db(self, tmp_hc):
        """_backup_db returns None if the DB file doesn't exist yet."""
        db = global_db_path(tmp_hc)
        result = _backup_db(db, 1, tmp_hc)
        assert result is None


# ---------------------------------------------------------------------------
# Health verification
# ---------------------------------------------------------------------------

class TestHealthVerification:
    def test_healthy_db_passes(self, tmp_hc):
        """_verify_db_health passes for a fully migrated DB."""
        ensure_schema(tmp_hc)
        conn = get_connection(tmp_hc)
        _verify_db_health(conn)  # Should not raise
        conn.close()

    def test_corrupt_db_fails(self, tmp_hc):
        """_verify_db_health raises on missing core tables."""
        db = global_db_path(tmp_hc)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), isolation_level=None)
        conn.execute("CREATE TABLE schema_meta (version INTEGER PRIMARY KEY)")
        # Missing 'messages', 'sessions', 'tasks'
        with pytest.raises(RuntimeError, match="missing tables"):
            _verify_db_health(conn)
        conn.close()


# ---------------------------------------------------------------------------
# Full migration flow
# ---------------------------------------------------------------------------

class TestEnsureSchema:
    def test_fresh_db_applies_all_migrations(self, tmp_hc):
        """ensure_schema on empty DB applies all migrations."""
        ensure_schema(tmp_hc)
        conn = get_connection(tmp_hc)

        # Verify version
        row = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()
        assert row[0] == len(MIGRATIONS)

        # Verify core tables
        tables = {
            r[0] for r in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "messages" in tables
        assert "sessions" in tables
        assert "tasks" in tables
        assert "reviews" in tables
        assert "task_comments" in tables
        assert "project_ids" in tables
        assert "member_ids" in tables
        conn.close()

    def test_idempotent(self, tmp_hc):
        """Calling ensure_schema twice doesn't error."""
        ensure_schema(tmp_hc)
        ensure_schema(tmp_hc)  # Should be a no-op

    def test_backup_created_on_migration(self, tmp_hc):
        """A backup file is created when migrations are applied to an existing DB."""
        # First create the DB with some migrations
        db = global_db_path(tmp_hc)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        conn.execute("""\
            CREATE TABLE IF NOT EXISTS schema_meta (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """)
        conn.execute("COMMIT")

        # Apply first migration manually
        stmts = [s.strip() for s in MIGRATIONS[0].split(";") if s.strip()]
        conn.execute("BEGIN IMMEDIATE")
        for stmt in stmts:
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_meta (version) VALUES (1)")
        conn.execute("COMMIT")
        conn.close()

        # Now run ensure_schema — it should backup before applying V2+
        from delegate.db import _schema_verified, _schema_lock
        with _schema_lock:
            _schema_verified.pop(str(tmp_hc), None)

        ensure_schema(tmp_hc)

        # Check backup exists
        pdir = protected_dir(tmp_hc)
        backups = list(pdir.glob("db.sqlite.bak.*"))
        assert len(backups) >= 1, "Expected at least one backup file"

    def test_rollback_on_bad_migration(self, tmp_hc):
        """If a migration fails, the DB is restored from backup."""
        # Apply all migrations first
        ensure_schema(tmp_hc)
        db = global_db_path(tmp_hc)

        # Insert some data
        conn = get_connection(tmp_hc)
        conn.execute(
            "INSERT INTO messages (sender, recipient, content, type, project, project_uuid) "
            "VALUES ('a', 'b', 'hello', 'chat', 'test', 'uuid1')"
        )
        conn.commit()
        conn.close()

        # Make a backup copy for comparison
        original_size = db.stat().st_size

        # Now simulate a bad migration by temporarily adding a bad one
        import delegate.db as db_mod
        original_migrations = db_mod.MIGRATIONS[:]
        db_mod.MIGRATIONS.append("INVALID SQL THAT WILL FAIL;")

        # Clear schema cache
        with db_mod._schema_lock:
            db_mod._schema_verified.pop(str(tmp_hc), None)

        try:
            with pytest.raises(Exception):
                ensure_schema(tmp_hc)

            # Verify backup was restored — DB should still be usable
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT content FROM messages WHERE sender='a'"
            ).fetchone()
            assert row is not None
            assert row["content"] == "hello"
            conn.close()
        finally:
            db_mod.MIGRATIONS[:] = original_migrations
            with db_mod._schema_lock:
                db_mod._schema_verified.pop(str(tmp_hc), None)
