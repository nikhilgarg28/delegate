"""Manager notifications for task rejections and merge conflicts.

When a task is rejected by the human boss or hits a merge conflict,
these functions send a structured notification to the engineering manager's
inbox so they can triage and take action.

Notification types:
    REJECTION  — human rejected a task via POST /tasks/{id}/reject
    CONFLICT   — daemon merge worker detected a merge conflict

Usage:
    from delegate.notify import notify_rejection, notify_conflict
    notify_rejection(hc_home, team, task, reason="Code quality issues")
    notify_conflict(hc_home, team, task, conflict_details="...")
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from delegate.bootstrap import get_member_by_role
from delegate.config import get_boss
from delegate.mailbox import Message, deliver
from delegate.task import format_task_id

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get_manager_name(hc_home: Path, team: str) -> str:
    """Look up the manager agent by role."""
    name = get_member_by_role(hc_home, team, "manager")
    return name or "manager"


def _get_sender_name(hc_home: Path) -> str:
    """Return a valid sender for system notifications.

    Uses the boss name since they are the human who triggers
    rejections, and 'system' has no agent directory which would
    cause downstream routing failures.
    """
    return get_boss(hc_home) or "boss"


def notify_rejection(
    hc_home: Path,
    team: str,
    task: dict,
    reason: str = "",
) -> str | None:
    """Send a rejection notification to the manager.

    Called when a task is rejected via POST /tasks/{id}/reject.
    Includes any inline review comments from the current attempt.

    Args:
        hc_home: Delegate home directory.
        team: Team name.
        task: The task dict (must include id, title, assignee, status).
        reason: Human-provided rejection reason.

    Returns:
        The delivered message filename, or None if delivery failed.
    """
    manager = _get_manager_name(hc_home, team)
    sender = _get_sender_name(hc_home)
    task_id = task["id"]
    title = task.get("title", "(untitled)")
    assignee = task.get("assignee", "(unassigned)")
    attempt = task.get("review_attempt", 0)

    # Gather inline comments for this attempt
    comment_lines = ""
    if attempt > 0:
        try:
            from delegate.review import get_comments
            comments = get_comments(hc_home, team, task_id, attempt)
            if comments:
                parts = []
                for c in comments:
                    loc = f"{c['file']}:{c['line']}" if c.get("line") else c["file"]
                    parts.append(f"  {loc} — {c['body']}")
                comment_lines = (
                    f"\nInline comments ({len(comments)}):\n"
                    + "\n".join(parts)
                    + "\n"
                )
        except Exception as e:
            logger.warning(
                "Failed to fetch review comments for %s attempt %s: %s",
                format_task_id(task_id), attempt, e
            )

    body = (
        f"TASK_REJECTED: {format_task_id(task_id)}\n"
        f"\n"
        f"Task: {format_task_id(task_id)} — {title}\n"
        f"Assignee: {assignee}\n"
        f"Reason: {reason or '(no reason provided)'}\n"
        f"{comment_lines}"
        f"\n"
        f"Suggested actions:\n"
        f"  - Rework: reset to in_progress, same assignee fixes the issues\n"
        f"  - Reassign: assign to a different team member\n"
        f"  - Discard: close the task if no longer needed"
    )

    msg = Message(
        sender=sender,
        recipient=manager,
        time=_now_iso(),
        body=body,
    )

    try:
        filename = deliver(hc_home, team, msg)
        logger.info(
            "Rejection notification sent for %s to %s", task_id, manager
        )
        return filename
    except ValueError as e:
        logger.warning(
            "Invalid data when sending rejection notification for %s: %s", task_id, e
        )
        return None
    except FileNotFoundError as e:
        logger.warning(
            "Mailbox directory not found when sending rejection notification for %s: %s",
            task_id, e
        )
        return None


def notify_conflict(
    hc_home: Path,
    team: str,
    task: dict,
    conflict_details: str = "",
) -> str | None:
    """Send a merge conflict notification to the manager.

    Called when the daemon merge worker detects a merge failure and sets
    the task status to 'merge_failed'.

    Args:
        hc_home: Delegate home directory.
        team: Team name.
        task: The task dict (must include id, title, branch, assignee).
        conflict_details: Details about the conflict (files, error output, etc.).

    Returns:
        The delivered message filename, or None if delivery failed.
    """
    manager = _get_manager_name(hc_home, team)
    sender = _get_sender_name(hc_home)
    task_id = task["id"]
    title = task.get("title", "(untitled)")
    branch = task.get("branch", "(no branch)")
    assignee = task.get("assignee", "(unassigned)")

    body = (
        f"MERGE_CONFLICT: {format_task_id(task_id)}\n"
        f"\n"
        f"Task: {format_task_id(task_id)} — {title}\n"
        f"Branch: {branch}\n"
        f"Assignee: {assignee}\n"
        f"Conflict details: {conflict_details or '(no details available)'}\n"
        f"\n"
        f"Suggested action:\n"
        f"  - Assign back to {assignee or 'the original author'} to rebase "
        f"and resolve conflicts, then re-submit for review"
    )

    msg = Message(
        sender=sender,
        recipient=manager,
        time=_now_iso(),
        body=body,
    )

    try:
        filename = deliver(hc_home, team, msg)
        logger.info(
            "Conflict notification sent for %s to %s", task_id, manager
        )
        return filename
    except ValueError as e:
        logger.warning(
            "Invalid data when sending conflict notification for %s: %s", task_id, e
        )
        return None
    except FileNotFoundError as e:
        logger.warning(
            "Mailbox directory not found when sending conflict notification for %s: %s",
            task_id, e
        )
        return None
