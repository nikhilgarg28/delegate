"""Review management — per-attempt verdicts and inline comments.

Each time a task enters ``in_approval``, the ``review_attempt`` counter
on the task is incremented and a new pending row is inserted into the
``reviews`` table.  The human reviewer can then:

* Leave inline comments (file + optional line) via ``add_comment()``.
* Render a verdict (approve / reject) with an optional summary via
  ``set_verdict()``.

All data is keyed on ``(task_id, attempt)`` so previous review rounds
are preserved and can be shown behind a toggle in the UI.

Usage (Python):
    from delegate.review import (
        create_review, get_reviews, get_current_review,
        set_verdict, add_comment, get_comments,
    )
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from delegate.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Reviews CRUD
# ---------------------------------------------------------------------------

def create_review(hc_home: Path, team: str, task_id: int, attempt: int, reviewer: str = "") -> dict:
    """Insert a new pending review row for ``(task_id, attempt)``.

    Called automatically when a task transitions to ``in_approval``.
    Returns the created review as a dict.
    """
    conn = get_connection(hc_home, team)
    try:
        conn.execute(
            "INSERT INTO reviews (task_id, attempt, reviewer) VALUES (?, ?, ?)",
            (task_id, attempt, reviewer),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM reviews WHERE task_id = ? AND attempt = ?",
            (task_id, attempt),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_reviews(hc_home: Path, team: str, task_id: int) -> list[dict]:
    """Return all review attempts for a task, oldest first."""
    conn = get_connection(hc_home, team)
    try:
        rows = conn.execute(
            "SELECT * FROM reviews WHERE task_id = ? ORDER BY attempt ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_current_review(hc_home: Path, team: str, task_id: int) -> dict | None:
    """Return the latest review attempt for a task, or None."""
    conn = get_connection(hc_home, team)
    try:
        row = conn.execute(
            "SELECT * FROM reviews WHERE task_id = ? ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        review = dict(row)
        # Attach comments for this attempt
        review["comments"] = get_comments(hc_home, team, task_id, review["attempt"])
        return review
    finally:
        conn.close()


def set_verdict(
    hc_home: Path,
    team: str,
    task_id: int,
    attempt: int,
    verdict: str,
    summary: str = "",
    reviewer: str = "",
) -> dict:
    """Set the verdict on a review attempt.

    Args:
        verdict: ``'approved'`` or ``'rejected'``.
        summary: Optional overall comment from the reviewer.
        reviewer: Name of the reviewer.

    Returns the updated review row as a dict.
    """
    if verdict not in ("approved", "rejected"):
        raise ValueError(f"Invalid verdict '{verdict}'. Must be 'approved' or 'rejected'.")

    now = _now()
    conn = get_connection(hc_home, team)
    try:
        conn.execute(
            """\
            UPDATE reviews
               SET verdict = ?, summary = ?, reviewer = ?, decided_at = ?
             WHERE task_id = ? AND attempt = ?""",
            (verdict, summary, reviewer, now, task_id, attempt),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM reviews WHERE task_id = ? AND attempt = ?",
            (task_id, attempt),
        ).fetchone()
        if row is None:
            raise ValueError(f"No review found for task {task_id} attempt {attempt}")
        return dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Review comments CRUD
# ---------------------------------------------------------------------------

def add_comment(
    hc_home: Path,
    team: str,
    task_id: int,
    attempt: int,
    file: str,
    body: str,
    author: str,
    line: int | None = None,
) -> dict:
    """Add an inline comment to a review attempt.

    Args:
        file: Relative file path in the diff.
        line: Optional line number (None for general file-level comment).
        body: Comment text.
        author: Who wrote the comment (typically the human reviewer).

    Returns the created comment as a dict.
    """
    conn = get_connection(hc_home, team)
    try:
        cursor = conn.execute(
            """\
            INSERT INTO review_comments (task_id, attempt, file, line, body, author)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, attempt, file, line, body, author),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM review_comments WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_comments(
    hc_home: Path,
    team: str,
    task_id: int,
    attempt: int | None = None,
) -> list[dict]:
    """Return comments for a task, optionally filtered by attempt.

    If ``attempt`` is None, returns comments across all attempts (for
    the "previous reviews" toggle).  Results are ordered by creation time.
    """
    conn = get_connection(hc_home, team)
    try:
        if attempt is not None:
            rows = conn.execute(
                "SELECT * FROM review_comments WHERE task_id = ? AND attempt = ? ORDER BY id ASC",
                (task_id, attempt),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM review_comments WHERE task_id = ? ORDER BY attempt ASC, id ASC",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_comment(
    hc_home: Path,
    team: str,
    comment_id: int,
    body: str,
) -> dict | None:
    """Update the body of an existing review comment.

    Returns the updated comment as a dict, or None if not found.
    """
    conn = get_connection(hc_home, team)
    try:
        conn.execute(
            "UPDATE review_comments SET body = ? WHERE id = ?",
            (body, comment_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM review_comments WHERE id = ?",
            (comment_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_comment(
    hc_home: Path,
    team: str,
    comment_id: int,
) -> bool:
    """Delete a review comment by id.

    Returns True if a row was deleted, False if not found.
    """
    conn = get_connection(hc_home, team)
    try:
        cursor = conn.execute(
            "DELETE FROM review_comments WHERE id = ?",
            (comment_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
