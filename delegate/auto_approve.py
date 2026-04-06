"""Auto-approver — AI-powered task review and approval.

When enabled for a team, ``auto_approve_once()`` reviews one ``in_approval``
task per daemon cycle using the existing ``judge_diff()`` LLM evaluator.

If the average score meets the configured threshold the task is approved;
otherwise it is rejected and the manager is notified (same path as human
rejection).

Usage (called from the daemon loop in web.py):
    from delegate.auto_approve import auto_approve_once
    result = auto_approve_once(hc_home, team)
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path

from delegate.chat import log_event as _log_event
from delegate.config import get_reviewer_config
from delegate.review import get_current_review, set_verdict
from delegate.task import (
    get_task as _get_task,
    get_task_diff as _get_task_diff,
    list_tasks as _list_tasks,
    update_task as _update_task,
    change_status as _change_status,
    format_task_id,
)

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 100_000

# Track tasks already notified for sensitive file skips (avoids repeat messages)
_sensitive_notified: set[tuple[str, int]] = set()

# ---------------------------------------------------------------------------
# Sensitive file blocklist — diffs touching these require human review
# ---------------------------------------------------------------------------

SENSITIVE_PATTERNS = [
    # Agent / LLM instruction files (rewriting these = rewriting agent behavior)
    "CLAUDE.md",
    "claude.md",
    "AGENTS.md",
    "agents.md",
    ".claude/instructions.md",
    ".cursorrules",
    ".github/copilot-instructions.md",

    # Delegate internals
    "override.md",
    ".delegate/*",
    "setup.sh",
    "premerge.sh",

    # CI/CD pipelines (injecting build-time commands)
    ".github/workflows/*",
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    "Jenkinsfile",
    ".circleci/*",
    ".travis.yml",

    # Secrets and credentials
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "credentials*",
    "secrets*",
    "*secret*.json",
    "*secret*.yaml",
    "*secret*.yml",

    # Container / infra (privilege escalation surface)
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose*",
]

_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def extract_diff_files(diff_text: str) -> set[str]:
    """Extract file paths from a unified git diff."""
    files: set[str] = set()
    for m in _DIFF_FILE_RE.finditer(diff_text):
        files.add(m.group(1))
        files.add(m.group(2))
    return files


def check_sensitive_files(diff_text: str) -> list[str]:
    """Return list of sensitive file paths found in the diff.

    Matches each file path against ``SENSITIVE_PATTERNS`` using
    case-insensitive fnmatch against both the full path and the
    basename (so ``".env"`` matches ``"config/.env"``).
    """
    files = extract_diff_files(diff_text)
    matched: list[str] = []
    for fpath in sorted(files):
        basename = fpath.rsplit("/", 1)[-1]
        for pattern in SENSITIVE_PATTERNS:
            if (fnmatch.fnmatch(fpath.lower(), pattern.lower())
                    or fnmatch.fnmatch(basename.lower(), pattern.lower())):
                matched.append(fpath)
                break
    return matched


def auto_approve_once(hc_home: Path, team: str) -> dict | None:
    """Review one ``in_approval`` task via LLM judge.

    Returns a result dict on action, or ``None`` if nothing to do.
    """
    cfg = get_reviewer_config(hc_home, team)
    if cfg["mode"] != "ai":
        return None

    threshold = cfg["threshold"]

    # Collect candidates: in_approval tasks whose current review has no verdict
    candidates = []
    for task in _list_tasks(hc_home, team, status="in_approval"):
        review = get_current_review(hc_home, team, task["id"])
        if review and review.get("verdict") is not None:
            continue  # already reviewed (human pre-empted or already auto-reviewed)
        candidates.append(task)

    if not candidates:
        return None

    # Sort by merge priority (best candidate first)
    from delegate.merge import _sort_merge_candidates

    candidates = _sort_merge_candidates(hc_home, team, candidates)
    task = candidates[0]
    task_id = task["id"]

    # Get diff
    try:
        diff_dict = _get_task_diff(hc_home, team, task_id)
    except Exception as exc:
        logger.warning("auto_approve: failed to get diff for %s: %s", format_task_id(task_id), exc)
        return None

    # Combine multi-repo diffs
    parts = []
    for repo_name, diff_text in diff_dict.items():
        if len(diff_dict) > 1:
            parts.append(f"# Repo: {repo_name}\n{diff_text}")
        else:
            parts.append(diff_text)
    combined_diff = "\n\n".join(parts)

    # Sensitive file blocklist — require human review for these
    sensitive = check_sensitive_files(combined_diff)
    if sensitive:
        logger.info(
            "auto_approve: skipping %s — diff touches sensitive files: %s",
            format_task_id(task_id), ", ".join(sensitive),
        )

        notify_key = (team, task_id)
        if notify_key not in _sensitive_notified:
            _sensitive_notified.add(notify_key)

            from delegate.notify import notify_sensitive_skip
            notify_sensitive_skip(hc_home, team, task, sensitive)

            _log_event(hc_home, team,
                       f"{format_task_id(task_id)} auto-approve skipped — sensitive files require human review",
                       task_id=task_id)

        return {"task_id": task_id, "verdict": "skipped", "reason": "sensitive_files", "files": sensitive}

    # Truncate large diffs
    if len(combined_diff) > MAX_DIFF_CHARS:
        combined_diff = combined_diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated at 100K chars ...]"

    # Build task spec from title + description
    title = task.get("title", "")
    description = task.get("description", "")
    task_spec = f"{title}\n\n{description}".strip()

    # Call judge
    from delegate.eval import judge_diff

    try:
        scores = judge_diff(combined_diff, task_spec)
    except Exception as exc:
        logger.warning("auto_approve: judge_diff failed for %s: %s", format_task_id(task_id), exc)
        return None

    avg = scores.get("avg", 0)
    attempt = task.get("review_attempt", 0)
    reasoning = scores.get("reasoning", "")

    if avg >= threshold:
        # Approve
        if attempt > 0:
            set_verdict(hc_home, team, task_id, attempt, "approved",
                        summary=reasoning, reviewer="auto-approver")
        _update_task(hc_home, team, task_id, approval_status="approved")

        _log_event(hc_home, team,
                   f"{format_task_id(task_id)} auto-approved (avg {avg:.1f}/{threshold}) ✓",
                   task_id=task_id)

        logger.info("auto_approve: approved %s (avg %.2f >= %.2f)", format_task_id(task_id), avg, threshold)

        return {"task_id": task_id, "verdict": "approved", "scores": scores, "threshold": threshold}
    else:
        # Reject
        if attempt > 0:
            set_verdict(hc_home, team, task_id, attempt, "rejected",
                        summary=reasoning, reviewer="auto-approver")
        _update_task(hc_home, team, task_id,
                     rejection_reason=f"Auto-approver: avg {avg:.1f} < {threshold}",
                     approval_status="rejected")
        _change_status(hc_home, team, task_id, "rejected")

        # Notify manager (same path as human rejection)
        from delegate.notify import notify_rejection
        notify_rejection(hc_home, team, task, reason=f"Auto-approver rejected (avg {avg:.1f} < {threshold}): {reasoning}")

        _log_event(hc_home, team,
                   f"{format_task_id(task_id)} auto-rejected (avg {avg:.1f}/{threshold})",
                   task_id=task_id)

        logger.info("auto_approve: rejected %s (avg %.2f < %.2f)", format_task_id(task_id), avg, threshold)

        return {"task_id": task_id, "verdict": "rejected", "scores": scores, "threshold": threshold}
