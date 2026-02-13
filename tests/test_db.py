"""Tests for delegate/db.py — schema versioning, migrations, and JSON serialization."""

import json
import sqlite3
from pathlib import Path

import pytest

from delegate.db import (
    ensure_schema,
    get_connection,
    task_row_to_dict,
    MIGRATIONS,
    _current_version,
)
from delegate.paths import db_path, global_db_path
from tests.conftest import SAMPLE_TEAM_NAME as TEAM


class TestSchemaInitialization:
    """Test initial schema creation and table structure."""

    def test_ensure_schema_creates_db_file(self, tmp_team):
        """ensure_schema should create the SQLite database file."""
        path = global_db_path(tmp_team)
        assert path.exists()
        assert path.is_file()

    def test_ensure_schema_creates_schema_meta(self, tmp_team):
        """ensure_schema should create the schema_meta table."""
        conn = sqlite3.connect(str(global_db_path(tmp_team)))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_ensure_schema_creates_all_tables(self, tmp_team):
        """ensure_schema should create all tables from migrations."""
        conn = sqlite3.connect(str(global_db_path(tmp_team)))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        # Expected tables from all migrations (mailbox dropped in V9, teams added in V11)
        expected = {
            "schema_meta",
            "messages",
            "sessions",
            "tasks",
            "reviews",
            "review_comments",
            "task_comments",
            "teams",
        }
        assert expected.issubset(tables)

    def test_ensure_schema_applies_all_migrations(self, tmp_team):
        """ensure_schema should apply all migrations in order."""
        conn = sqlite3.connect(str(global_db_path(tmp_team)))
        current = _current_version(conn)
        conn.close()
        assert current == len(MIGRATIONS)

    def test_ensure_schema_records_migration_metadata(self, tmp_team):
        """schema_meta should record version and timestamp for each migration."""
        conn = sqlite3.connect(str(global_db_path(tmp_team)))
        cursor = conn.execute("SELECT version, applied_at FROM schema_meta ORDER BY version")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == len(MIGRATIONS)
        for i, (version, applied_at) in enumerate(rows, start=1):
            assert version == i
            assert applied_at  # timestamp should be set
            assert applied_at.startswith("20")  # ISO format year check


class TestMigrationIdempotency:
    """Test that migrations can be run multiple times safely."""

    def test_ensure_schema_is_idempotent(self, tmp_team):
        """Running ensure_schema twice should not fail or duplicate data."""
        # First run already happened in tmp_team fixture
        version1 = _current_version(sqlite3.connect(str(global_db_path(tmp_team))))

        # Run again
        ensure_schema(tmp_team, TEAM)
        version2 = _current_version(sqlite3.connect(str(global_db_path(tmp_team))))

        assert version1 == version2 == len(MIGRATIONS)

    def test_multiple_ensure_schema_calls_do_not_duplicate_migrations(self, tmp_team):
        """Multiple ensure_schema calls should not re-apply migrations."""
        conn = sqlite3.connect(str(global_db_path(tmp_team)))
        count1 = conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0]
        conn.close()

        # Run ensure_schema multiple times
        ensure_schema(tmp_team, TEAM)
        ensure_schema(tmp_team, TEAM)
        ensure_schema(tmp_team, TEAM)

        conn = sqlite3.connect(str(global_db_path(tmp_team)))
        count2 = conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0]
        conn.close()

        assert count1 == count2 == len(MIGRATIONS)

    def test_partial_migration_resumes_correctly(self, tmp_team):
        """If some migrations are applied, ensure_schema applies only pending ones."""
        # Delete the existing global DB and create a fresh one with only first 2 migrations
        fresh_path = global_db_path(tmp_team)
        fresh_path.unlink(missing_ok=True)
        fresh_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(fresh_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """)
        conn.commit()

        # Apply first 2 migrations manually
        for i in range(2):
            conn.executescript(MIGRATIONS[i])
            conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (i + 1,))
            conn.commit()

        assert _current_version(conn) == 2
        conn.close()

        # Now run ensure_schema — should apply remaining migrations
        ensure_schema(tmp_team)
        conn = sqlite3.connect(str(fresh_path))
        final_version = _current_version(conn)
        conn.close()

        assert final_version == len(MIGRATIONS)


class TestConnectionManagement:
    """Test get_connection and connection pooling behavior."""

    def test_get_connection_returns_connection(self, tmp_team):
        """get_connection should return a valid SQLite connection."""
        conn = get_connection(tmp_team, TEAM)
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_get_connection_sets_row_factory(self, tmp_team):
        """get_connection should set row_factory to sqlite3.Row."""
        conn = get_connection(tmp_team, TEAM)
        assert conn.row_factory == sqlite3.Row
        conn.close()

    def test_get_connection_enables_wal_mode(self, tmp_team):
        """get_connection should enable WAL journaling mode."""
        conn = get_connection(tmp_team, TEAM)
        cursor = conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        conn.close()
        assert mode.lower() == "wal"

    def test_get_connection_ensures_schema(self, tmp_team):
        """get_connection should call ensure_schema before returning."""
        # Delete the DB to force re-creation
        path = global_db_path(tmp_team)
        path.unlink(missing_ok=True)

        conn = get_connection(tmp_team, TEAM)
        # Schema should be initialized
        cursor = conn.execute("SELECT COUNT(*) FROM schema_meta")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == len(MIGRATIONS)

    def test_multiple_connections_to_same_db(self, tmp_team):
        """Multiple connections to the same DB should work (WAL mode)."""
        conn1 = get_connection(tmp_team, TEAM)
        conn2 = get_connection(tmp_team, TEAM)

        # Insert via conn1
        conn1.execute("INSERT INTO messages (sender, recipient, content, type) VALUES (?, ?, ?, ?)",
                      ("alice", "bob", "test", "chat"))
        conn1.commit()

        # Read via conn2
        cursor = conn2.execute("SELECT content FROM messages WHERE sender='alice'")
        row = cursor.fetchone()

        conn1.close()
        conn2.close()

        assert row["content"] == "test"


class TestJSONColumnRoundtrips:
    """Test serialization/deserialization of JSON columns in task_row_to_dict."""

    def test_json_list_columns_parse_correctly(self, tmp_team):
        """JSON list columns (tags, depends_on, attachments, repo) should parse as lists."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, tags, depends_on, attachments, repo, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """, ("Test", '["tag1", "tag2"]', '[1, 2, 3]', '["file.txt"]', '["repo1", "repo2"]'))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["tags"] == ["tag1", "tag2"]
        assert task["depends_on"] == [1, 2, 3]
        assert task["attachments"] == ["file.txt"]
        assert task["repo"] == ["repo1", "repo2"]

    def test_json_dict_columns_parse_correctly(self, tmp_team):
        """JSON dict columns (commits, base_sha, merge_base, merge_tip) should parse as dicts."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, commits, base_sha, merge_base, merge_tip, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """, ("Test", '{"repo1": ["abc123"]}', '{"repo1": "def456"}', '{"repo1": "ghi789"}', '{"repo1": "jkl012"}'))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["commits"] == {"repo1": ["abc123"]}
        assert task["base_sha"] == {"repo1": "def456"}
        assert task["merge_base"] == {"repo1": "ghi789"}
        assert task["merge_tip"] == {"repo1": "jkl012"}

    def test_empty_json_arrays_default_to_empty_list(self, tmp_team):
        """Empty JSON arrays should parse as empty lists."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, tags, depends_on, attachments, repo, created_at, updated_at)
            VALUES (?, '[]', '[]', '[]', '[]', datetime('now'), datetime('now'))
        """, ("Test",))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["tags"] == []
        assert task["depends_on"] == []
        assert task["attachments"] == []
        assert task["repo"] == []

    def test_empty_json_dicts_default_to_empty_dict(self, tmp_team):
        """Empty JSON dicts should parse as empty dicts."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, commits, base_sha, merge_base, merge_tip, created_at, updated_at)
            VALUES (?, '{}', '{}', '{}', '{}', datetime('now'), datetime('now'))
        """, ("Test",))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["commits"] == {}
        assert task["base_sha"] == {}
        assert task["merge_base"] == {}
        assert task["merge_tip"] == {}

    def test_backward_compat_plain_string_repo(self, tmp_team):
        """Legacy repo field as plain string should be wrapped in a list."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, repo, created_at, updated_at)
            VALUES (?, 'myrepo', datetime('now'), datetime('now'))
        """, ("Test",))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["repo"] == ["myrepo"]

    def test_backward_compat_plain_string_base_sha(self, tmp_team):
        """Legacy base_sha as plain string should convert to dict."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, repo, base_sha, created_at, updated_at)
            VALUES (?, '["myrepo"]', 'abc123', datetime('now'), datetime('now'))
        """, ("Test",))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["base_sha"] == {"myrepo": "abc123"}

    def test_backward_compat_list_commits(self, tmp_team):
        """Legacy commits as list should convert to dict with first repo."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, repo, commits, created_at, updated_at)
            VALUES (?, '["myrepo"]', '["abc123", "def456"]', datetime('now'), datetime('now'))
        """, ("Test",))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["commits"] == {"myrepo": ["abc123", "def456"]}

    def test_depends_on_coerced_to_ints(self, tmp_team):
        """depends_on elements should be coerced to integers."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, depends_on, created_at, updated_at)
            VALUES (?, '[1, 2, 3]', datetime('now'), datetime('now'))
        """, ("Test",))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["depends_on"] == [1, 2, 3]
        assert all(isinstance(x, int) for x in task["depends_on"])

    def test_tags_coerced_to_strings(self, tmp_team):
        """tags elements should be coerced to strings."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, tags, created_at, updated_at)
            VALUES (?, '["tag1", "tag2"]', datetime('now'), datetime('now'))
        """, ("Test",))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        assert task["tags"] == ["tag1", "tag2"]
        assert all(isinstance(x, str) for x in task["tags"])

    def test_malformed_json_defaults_gracefully(self, tmp_team):
        """Malformed JSON should default to empty list/dict instead of crashing."""
        conn = get_connection(tmp_team, TEAM)
        conn.execute("""
            INSERT INTO tasks (title, tags, base_sha, created_at, updated_at)
            VALUES (?, 'not-json', 'plain-sha', datetime('now'), datetime('now'))
        """, ("Test",))
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Test'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        # Malformed JSON for list columns should wrap as single-element list
        assert task["tags"] == ["not-json"]
        # Malformed JSON for dict columns (like base_sha) should convert to dict with _default key
        assert task["base_sha"] == {"_default": "plain-sha"}


class TestEdgeCases:
    """Edge cases and error conditions."""

    def test_fresh_db_has_no_tasks(self, tmp_team):
        """A fresh database should have zero tasks."""
        conn = get_connection(tmp_team, TEAM)
        cursor = conn.execute("SELECT COUNT(*) FROM tasks")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0

    def test_fresh_db_has_no_messages(self, tmp_team):
        """A fresh database should have zero messages."""
        conn = get_connection(tmp_team, TEAM)
        cursor = conn.execute("SELECT COUNT(*) FROM messages")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0

    def test_current_version_on_empty_schema_meta(self, tmp_team):
        """_current_version should return 0 if schema_meta is empty."""
        # Create a DB with empty schema_meta
        fresh_path = tmp_team / "teams" / "empty" / "db.sqlite"
        fresh_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(fresh_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """)
        conn.commit()

        version = _current_version(conn)
        conn.close()
        assert version == 0

    def test_ensure_schema_on_nonexistent_directory(self, tmp_team):
        """ensure_schema should create parent directories if they don't exist."""
        # Delete the global DB and recreate with ensure_schema
        global_path = global_db_path(tmp_team)
        global_path.unlink(missing_ok=True)

        ensure_schema(tmp_team)
        assert global_path.exists()

    def test_task_row_to_dict_with_null_fields(self, tmp_team):
        """task_row_to_dict should handle NULL fields gracefully."""
        conn = get_connection(tmp_team, TEAM)
        # Insert task with minimal required fields
        conn.execute("""
            INSERT INTO tasks (title, created_at, updated_at)
            VALUES ('Minimal', datetime('now'), datetime('now'))
        """)
        conn.commit()

        cursor = conn.execute("SELECT * FROM tasks WHERE title='Minimal'")
        row = cursor.fetchone()
        conn.close()

        task = task_row_to_dict(row)
        # Default values should be set
        assert task["tags"] == []
        assert task["depends_on"] == []
        assert task["attachments"] == []
        assert task["repo"] == []
        assert task["commits"] == {}
        assert task["base_sha"] == {}
