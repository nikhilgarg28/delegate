"""Cleanup utilities for reclaiming disk space and pruning stale data.

Handles:
    - Old DB records (sessions, messages, reviews older than a threshold)
    - Stale git worktrees for completed/cancelled tasks
    - Package caches (.pkg-cache directories)
    - Virtual environments in worktree directories
    - Old agent log files (keeps only the most recent)
    - Rotated daemon log files
    - DB vacuum after deletions
"""

import logging
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from delegate.paths import (
    global_db_path,
    list_team_names,
    protected_dir,
    resolve_team_uuid,
    team_dir,
)
from delegate.task import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_LOG_KEEP = 1  # keep only the most recent log file per agent

_TERMINAL_STATUS_PLACEHOLDERS = ",".join("?" for _ in TERMINAL_STATUSES)
_TERMINAL_STATUS_PARAMS = list(TERMINAL_STATUSES)


@dataclass
class CleanupResult:
    """Summary of a cleanup operation."""

    bytes_freed: int = 0
    sessions_deleted: int = 0
    messages_deleted: int = 0
    reviews_deleted: int = 0
    worktrees_removed: int = 0
    venvs_removed: int = 0
    pkg_caches_cleared: int = 0
    log_files_removed: int = 0
    daemon_logs_removed: int = 0
    db_size_before: int = 0
    db_size_after: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bytes_freed": self.bytes_freed,
            "mb_freed": round(self.bytes_freed / (1024 * 1024), 1),
            "sessions_deleted": self.sessions_deleted,
            "messages_deleted": self.messages_deleted,
            "reviews_deleted": self.reviews_deleted,
            "worktrees_removed": self.worktrees_removed,
            "venvs_removed": self.venvs_removed,
            "pkg_caches_cleared": self.pkg_caches_cleared,
            "log_files_removed": self.log_files_removed,
            "daemon_logs_removed": self.daemon_logs_removed,
            "db_size_before": self.db_size_before,
            "db_size_after": self.db_size_after,
            "errors": self.errors,
        }


@dataclass
class CleanupPreview:
    """Preview of what cleanup would do (dry-run)."""

    total_bytes_reclaimable: int = 0
    stale_sessions: int = 0
    stale_messages: int = 0
    stale_reviews: int = 0
    stale_worktrees: list[str] = field(default_factory=list)
    venv_dirs: list[str] = field(default_factory=list)
    pkg_cache_bytes: int = 0
    stale_log_files: int = 0
    daemon_log_files: int = 0
    db_size: int = 0

    def to_dict(self) -> dict:
        return {
            "total_bytes_reclaimable": self.total_bytes_reclaimable,
            "mb_reclaimable": round(self.total_bytes_reclaimable / (1024 * 1024), 1),
            "stale_sessions": self.stale_sessions,
            "stale_messages": self.stale_messages,
            "stale_reviews": self.stale_reviews,
            "stale_worktrees": self.stale_worktrees,
            "venv_dirs": self.venv_dirs,
            "pkg_cache_bytes": self.pkg_cache_bytes,
            "pkg_cache_mb": round(self.pkg_cache_bytes / (1024 * 1024), 1),
            "stale_log_files": self.stale_log_files,
            "daemon_log_files": self.daemon_log_files,
            "db_size": self.db_size,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dir_size(path: Path) -> int:
    """Return total size of a directory in bytes.

    Uses ``du -s`` for speed; falls back to Python walk on failure.
    """
    if not path.exists():
        return 0
    try:
        result = subprocess.run(
            ["du", "-s", "--bytes", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, IndexError):
        pass
    # Fallback: Python walk (slow for large dirs)
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _dir_size(Path(entry.path))
    except OSError:
        pass
    return total


def _resolve_teams(hc_home: Path, team_name: str | None) -> list[str]:
    """Return team names to process, respecting optional team_name filter."""
    return [team_name] if team_name else list_team_names(hc_home)


def _scoped_query(
    conn: sqlite3.Connection,
    base_sql: str,
    time_col: str,
    max_age_days: int,
    project_uuid: str | None,
) -> sqlite3.Cursor:
    """Execute a query scoped by age and optionally by project UUID."""
    where = f"julianday('now') - julianday({time_col}) > ?"
    params: list = [max_age_days]
    if project_uuid:
        where += " AND project_uuid = ?"
        params.append(project_uuid)
    return conn.execute(f"{base_sql} WHERE {where}", params)


def _get_completed_task_ids(
    conn: sqlite3.Connection, project_uuid: str | None, max_age_days: int
) -> set[int]:
    """Return IDs of terminal tasks older than max_age_days."""
    base = f"""SELECT id FROM tasks
               WHERE status IN ({_TERMINAL_STATUS_PLACEHOLDERS})
               AND julianday('now') - julianday(completed_at) > ?"""
    params: list = _TERMINAL_STATUS_PARAMS + [max_age_days]
    if project_uuid:
        base += " AND project_uuid = ?"
        params.append(project_uuid)
    rows = conn.execute(base, params).fetchall()
    return {r[0] for r in rows}


def _stale_worktrees_for_team(
    hc_home: Path, team_name: str, conn: sqlite3.Connection, max_age_days: int
) -> list[Path]:
    """Find worktree directories for completed/cancelled tasks."""
    uuid = resolve_team_uuid(hc_home, team_name)
    td = team_dir(hc_home, team_name)
    wt_root = td / "worktrees"
    if not wt_root.exists():
        return []

    done_ids = _get_completed_task_ids(conn, uuid, max_age_days)
    if not done_ids:
        return []

    # Batch query for display_ids (avoids N+1)
    placeholders = ",".join("?" for _ in done_ids)
    rows = conn.execute(
        f"SELECT id, display_id FROM tasks WHERE id IN ({placeholders})",
        list(done_ids),
    ).fetchall()
    done_display_ids: set[str] = set()
    for task_id, display_id in rows:
        if display_id:
            done_display_ids.add(display_id)
        done_display_ids.add(f"T{task_id:04d}")

    stale: list[Path] = []
    for repo_dir in wt_root.iterdir():
        if not repo_dir.is_dir():
            continue
        for wt_dir in repo_dir.iterdir():
            if not wt_dir.is_dir() or wt_dir.name.startswith("."):
                continue
            if wt_dir.name in done_display_ids:
                stale.append(wt_dir)
    return stale


def _find_venvs(hc_home: Path, team_name: str | None = None) -> list[Path]:
    """Find .venv directories inside worktree roots."""
    venvs: list[Path] = []
    for tn in _resolve_teams(hc_home, team_name):
        wt_root = team_dir(hc_home, tn) / "worktrees"
        if not wt_root.exists():
            continue
        for repo_dir in wt_root.iterdir():
            if not repo_dir.is_dir():
                continue
            venv_dir = repo_dir / ".venv"
            if venv_dir.is_dir():
                venvs.append(venv_dir)
    return venvs


def _find_pkg_caches(hc_home: Path, team_name: str | None = None) -> list[Path]:
    """Find .pkg-cache directories in team project directories."""
    caches: list[Path] = []
    for tn in _resolve_teams(hc_home, team_name):
        cache_dir = team_dir(hc_home, tn) / ".pkg-cache"
        if cache_dir.is_dir():
            caches.append(cache_dir)
    return caches


def _find_stale_agent_logs(
    hc_home: Path, team_name: str | None = None, keep: int = DEFAULT_LOG_KEEP
) -> list[Path]:
    """Find old agent log files (keeping only the N most recent per agent)."""
    stale: list[Path] = []
    for tn in _resolve_teams(hc_home, team_name):
        agents_root = team_dir(hc_home, tn) / "agents"
        if not agents_root.is_dir():
            continue
        for agent_entry in agents_root.iterdir():
            if not agent_entry.is_dir():
                continue
            log_dir = agent_entry / "logs"
            if not log_dir.is_dir():
                continue
            # os.scandir caches stat info — faster than iterdir + stat
            entries = []
            try:
                for e in os.scandir(log_dir):
                    if e.is_file():
                        entries.append((e.path, e.stat().st_mtime))
            except OSError:
                continue
            entries.sort(key=lambda x: x[1], reverse=True)
            for path_str, _ in entries[keep:]:
                stale.append(Path(path_str))
    return stale


def _find_stale_daemon_logs(hc_home: Path, keep: int = 1) -> list[Path]:
    """Find rotated daemon log files (delegate.log.N where N > keep).

    Retention is independent of logging_setup.py's backupCount — cleanup
    is intentionally more aggressive to reclaim disk space.
    """
    pdir = protected_dir(hc_home)
    stale: list[Path] = []
    for i in range(keep + 1, 20):
        log_path = pdir / f"delegate.log.{i}"
        if log_path.exists():
            stale.append(log_path)
    return stale


def _prune_git_worktrees(repo_path: Path) -> None:
    """Run ``git worktree prune`` to clean up orphaned worktree metadata."""
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_path),
            capture_output=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


# ---------------------------------------------------------------------------
# Preview (dry-run)
# ---------------------------------------------------------------------------

def preview_cleanup(
    hc_home: Path,
    team_name: str | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> CleanupPreview:
    """Compute what cleanup would do without making changes."""
    from delegate.db import get_connection

    preview = CleanupPreview()
    conn = get_connection(hc_home)
    project_uuid = resolve_team_uuid(hc_home, team_name) if team_name else None

    try:
        row = _scoped_query(
            conn, "SELECT count(*) FROM sessions", "started_at", max_age_days, project_uuid
        ).fetchone()
        preview.stale_sessions = row[0]

        row = _scoped_query(
            conn, "SELECT count(*) FROM messages", "timestamp", max_age_days, project_uuid
        ).fetchone()
        preview.stale_messages = row[0]

        done_ids = _get_completed_task_ids(conn, project_uuid, max_age_days)
        if done_ids:
            placeholders = ",".join("?" for _ in done_ids)
            row = conn.execute(
                f"SELECT count(*) FROM reviews WHERE task_id IN ({placeholders})",
                list(done_ids),
            ).fetchone()
            preview.stale_reviews = row[0]
    finally:
        conn.close()

    # Filesystem scanning (no DB connection held)
    teams = _resolve_teams(hc_home, team_name)
    conn = get_connection(hc_home)
    try:
        for tn in teams:
            for wt in _stale_worktrees_for_team(hc_home, tn, conn, max_age_days):
                preview.stale_worktrees.append(str(wt))
                preview.total_bytes_reclaimable += _dir_size(wt)
    finally:
        conn.close()

    for v in _find_venvs(hc_home, team_name):
        sz = _dir_size(v)
        preview.venv_dirs.append(str(v))
        preview.total_bytes_reclaimable += sz

    for c in _find_pkg_caches(hc_home, team_name):
        sz = _dir_size(c)
        preview.pkg_cache_bytes += sz
        preview.total_bytes_reclaimable += sz

    stale_logs = _find_stale_agent_logs(hc_home, team_name)
    preview.stale_log_files = len(stale_logs)
    for lf in stale_logs:
        try:
            preview.total_bytes_reclaimable += lf.stat().st_size
        except OSError:
            pass

    stale_daemon = _find_stale_daemon_logs(hc_home)
    preview.daemon_log_files = len(stale_daemon)
    for lf in stale_daemon:
        try:
            preview.total_bytes_reclaimable += lf.stat().st_size
        except OSError:
            pass

    db_path = global_db_path(hc_home)
    if db_path.exists():
        preview.db_size = db_path.stat().st_size

    return preview


# ---------------------------------------------------------------------------
# Execute cleanup
# ---------------------------------------------------------------------------

def run_cleanup(
    hc_home: Path,
    team_name: str | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    prune_db: bool = True,
    prune_worktrees: bool = True,
    prune_venvs: bool = True,
    prune_caches: bool = True,
    prune_logs: bool = True,
) -> CleanupResult:
    """Execute cleanup operations and return a summary."""
    from delegate.db import get_connection

    result = CleanupResult()
    project_uuid = resolve_team_uuid(hc_home, team_name) if team_name else None

    # --- 1. Prune DB records (short-lived connection) ---
    if prune_db:
        db_path = global_db_path(hc_home)
        if db_path.exists():
            result.db_size_before = db_path.stat().st_size

        conn = get_connection(hc_home)
        try:
            result.sessions_deleted = _scoped_query(
                conn, "DELETE FROM sessions", "started_at", max_age_days, project_uuid
            ).rowcount

            result.messages_deleted = _scoped_query(
                conn, "DELETE FROM messages", "timestamp", max_age_days, project_uuid
            ).rowcount

            done_ids = _get_completed_task_ids(conn, project_uuid, max_age_days)
            if done_ids:
                placeholders = ",".join("?" for _ in done_ids)
                result.reviews_deleted = conn.execute(
                    f"DELETE FROM reviews WHERE task_id IN ({placeholders})",
                    list(done_ids),
                ).rowcount

            conn.commit()
        finally:
            conn.close()

        # VACUUM requires its own connection (exclusive lock)
        conn = get_connection(hc_home)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

        if db_path.exists():
            result.db_size_after = db_path.stat().st_size
            result.bytes_freed += result.db_size_before - result.db_size_after

    # --- 2. Remove stale worktrees (no DB connection held during I/O) ---
    if prune_worktrees:
        conn = get_connection(hc_home)
        stale_wts: list[Path] = []
        repo_roots: set[Path] = set()
        try:
            for tn in _resolve_teams(hc_home, team_name):
                stale_wts.extend(_stale_worktrees_for_team(hc_home, tn, conn, max_age_days))
        finally:
            conn.close()

        for wt_path in stale_wts:
            sz = _dir_size(wt_path)
            try:
                subprocess.run(
                    ["git", "worktree", "remove", str(wt_path), "--force"],
                    capture_output=True, timeout=30,
                )
                if not wt_path.exists():
                    result.bytes_freed += sz
                    result.worktrees_removed += 1
                    repo_roots.add(wt_path.parent)
                    continue
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            # Fallback: direct removal
            try:
                shutil.rmtree(wt_path)
                result.bytes_freed += sz
                result.worktrees_removed += 1
                repo_roots.add(wt_path.parent)
            except OSError as e:
                result.errors.append(f"Failed to remove worktree {wt_path}: {e}")

        # Prune orphaned git worktree metadata
        for repo_root in repo_roots:
            _prune_git_worktrees(repo_root)

    # --- 3. Remove orphaned .venv directories ---
    if prune_venvs:
        for venv_path in _find_venvs(hc_home, team_name):
            sz = _dir_size(venv_path)
            try:
                shutil.rmtree(venv_path)
                result.bytes_freed += sz
                result.venvs_removed += 1
            except OSError as e:
                result.errors.append(f"Failed to remove venv {venv_path}: {e}")

    # --- 4. Clear package caches ---
    if prune_caches:
        for cache_path in _find_pkg_caches(hc_home, team_name):
            sz = _dir_size(cache_path)
            try:
                for child in cache_path.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                result.bytes_freed += sz
                result.pkg_caches_cleared += 1
            except OSError as e:
                result.errors.append(f"Failed to clear cache {cache_path}: {e}")

    # --- 5. Prune old log files ---
    if prune_logs:
        for lf in _find_stale_agent_logs(hc_home, team_name):
            try:
                sz = lf.stat().st_size
                lf.unlink()
                result.bytes_freed += sz
                result.log_files_removed += 1
            except OSError as e:
                result.errors.append(f"Failed to remove log {lf}: {e}")

        # Daemon logs (global, not team-scoped)
        if not team_name:
            for lf in _find_stale_daemon_logs(hc_home):
                try:
                    sz = lf.stat().st_size
                    lf.unlink()
                    result.bytes_freed += sz
                    result.daemon_logs_removed += 1
                except OSError as e:
                    result.errors.append(f"Failed to remove daemon log {lf}: {e}")

    logger.info(
        "Cleanup complete: freed %d MB, %d sessions, %d messages, %d reviews, %d worktrees, %d venvs, %d caches",
        result.bytes_freed // (1024 * 1024),
        result.sessions_deleted,
        result.messages_deleted,
        result.reviews_deleted,
        result.worktrees_removed,
        result.venvs_removed,
        result.pkg_caches_cleared,
    )
    return result
