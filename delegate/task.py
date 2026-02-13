"""Per-team SQLite-backed task management.

Tasks are stored in the ``tasks`` table of each team's
``~/.delegate/teams/<team>/db.sqlite``.  Task IDs start from 1 per team.

Each task has a **DRI** (Directly Responsible Individual) set on first
assignment — the DRI never changes and anchors the branch name
(``delegate/<team>/T<NNNN>``).  The **assignee** field tracks who currently
owns the ball and is updated by the manager as the task moves through stages.

Usage:
    python -m delegate.task create <home> <team> --title "Build API" --assignee alice [--priority high]
    python -m delegate.task list <home> <team> [--status todo] [--assignee alice]
    python -m delegate.task update <home> <team> <task_id> [--title ...] [--description ...] [--priority ...]
    python -m delegate.task assign <home> <team> <task_id> <assignee>
    python -m delegate.task status <home> <team> <task_id> <status>
    python -m delegate.task show <home> <team> <task_id>
"""

import argparse
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from delegate.db import get_connection, task_row_to_dict, _JSON_COLUMNS

_log = logging.getLogger(__name__)


def _broadcast_update(task_id: int, team: str, changes: dict) -> None:
    """Best-effort SSE broadcast of a task mutation."""
    try:
        from delegate.activity import broadcast_task_update
        broadcast_task_update(task_id, team, changes)
    except Exception:
        pass


VALID_STATUSES = ("todo", "in_progress", "in_review", "in_approval", "merging", "done", "rejected", "merge_failed", "cancelled")
VALID_PRIORITIES = ("low", "medium", "high", "critical")
VALID_APPROVAL_STATUSES = ("", "pending", "approved", "rejected")

# Allowed status transitions: from_status -> set of valid to_statuses
VALID_TRANSITIONS = {
    "todo": {"in_progress", "cancelled"},
    "in_progress": {"in_review", "cancelled"},
    "in_review": {"in_approval", "in_progress", "cancelled"},
    "in_approval": {"merging", "rejected", "cancelled"},
    "merging": {"done", "merge_failed", "cancelled"},
    "rejected": {"in_progress", "cancelled"},
    "merge_failed": {"merging", "in_progress", "cancelled"},
    # Terminal states — no transitions out
    "done": set(),
    "cancelled": set(),
}

# All columns in the tasks table (used for field validation on update).
_TASK_FIELDS = frozenset({
    "id", "title", "description", "status", "dri", "assignee",
    "project", "priority", "repo", "tags", "created_at", "updated_at",
    "completed_at", "depends_on", "branch", "base_sha",
    "rejection_reason", "approval_status", "merge_base", "merge_tip",
    "attachments", "review_attempt", "status_detail", "merge_attempts",
    "workflow", "workflow_version", "metadata",
})


def format_task_id(task_id: int) -> str:
    """Format a task ID as ``T`` followed by zero-padded digits.

    Always uses at least 4 digits, but automatically widens
    for IDs >= 10000 (e.g. ``T10000``).
    """
    return f"T{task_id:04d}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_task(
    hc_home: Path,
    team: str,
    title: str,
    assignee: str,
    description: str = "",
    project: str = "",
    priority: str = "medium",
    depends_on: list[int] | None = None,
    repo: str | list[str] = "",
    tags: list[str] | None = None,
    workflow_name: str = "default",
    workflow_version: int | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create a new task. Returns the task dict with assigned ID.

    *assignee* is required and will be set as both the assignee and DRI.

    *repo* can be a single repo name string or a list of repo names for
    multi-repo tasks.  Stored as a JSON array internally.

    *tags* is an optional free-form list of string labels (e.g.
    ``["bugfix", "frontend"]``).

    *workflow_name* specifies which workflow this task follows (default: "default").
    *workflow_version* stamps a specific version; if None, uses the latest
    registered version for the team (or 1 as fallback).

    *metadata* is an optional free-form dict for user/workflow-specific data.
    The core Delegate system never reads this field — it is exclusively for
    workflows, integrations, and user-defined extensions.
    """
    if not assignee or not assignee.strip():
        raise ValueError("Assignee/DRI is required when creating a task")

    if priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority '{priority}'. Must be one of: {VALID_PRIORITIES}")

    # Normalize repo to a JSON list
    if isinstance(repo, str):
        repo_list = [repo] if repo else []
    else:
        repo_list = list(repo)

    # Resolve workflow version
    if workflow_version is None:
        from delegate.workflow import get_latest_version
        workflow_version = get_latest_version(hc_home, team, workflow_name) or 1

    now = _now()
    conn = get_connection(hc_home, team)
    try:
        cursor = conn.execute(
            """\
            INSERT INTO tasks (
                title, description, status, dri, assignee,
                project, priority, repo, tags,
                created_at, updated_at, completed_at,
                depends_on, branch, base_sha, commits,
                rejection_reason, approval_status, merge_base, merge_tip, team,
                workflow, workflow_version, metadata
            ) VALUES (
                ?, ?, 'todo', ?, ?,
                ?, ?, ?, ?,
                ?, ?, '',
                ?, '', '{}', '{}',
                '', '', '{}', '{}', ?,
                ?, ?, ?
            )""",
            (
                title, description, assignee, assignee,
                project, priority,
                json.dumps(repo_list),
                json.dumps([str(t) for t in tags] if tags else []),
                now, now,
                json.dumps([int(d) for d in depends_on] if depends_on else []),
                team,
                workflow_name, workflow_version,
                json.dumps(metadata or {}),
            ),
        )
        conn.commit()
        task_id = cursor.lastrowid

        # Read back the full row to return
        row = conn.execute("SELECT * FROM tasks WHERE team = ? AND id = ?", (team, task_id)).fetchone()
        task = task_row_to_dict(row)
    finally:
        conn.close()

    from delegate.chat import log_event
    log_event(hc_home, team, f"{format_task_id(task_id)} created \u2014 {title}", task_id=task_id)

    # Eagerly create worktrees for each repo (with branch)
    if repo_list:
        import logging
        _task_log = logging.getLogger(__name__)
        from delegate.paths import get_team_id
        tid = get_team_id(hc_home, team)
        branch_name = f"delegate/{tid}/{team}/{format_task_id(task_id)}"
        for repo_name in repo_list:
            try:
                from delegate.repo import create_task_worktree
                create_task_worktree(hc_home, team, repo_name, task_id, branch=branch_name)
            except Exception as exc:
                _task_log.warning("Could not create worktree for %s (%s): %s", format_task_id(task_id), repo_name, exc)
        # Record branch on the task
        task = update_task(hc_home, team, task_id, branch=branch_name)

    return task


def get_task(hc_home: Path, team: str, task_id: int) -> dict:
    """Load a single task by ID.

    Raises ``FileNotFoundError`` if the task does not exist (preserves
    the same exception type used by the previous YAML implementation).
    """
    conn = get_connection(hc_home, team)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE team = ? AND id = ?", (team, task_id)).fetchone()
    finally:
        conn.close()

    if row is None:
        raise FileNotFoundError(f"Task {task_id} not found in team {team}")

    return task_row_to_dict(row)


def update_task(hc_home: Path, team: str, task_id: int, **updates) -> dict:
    """Update fields on an existing task. Returns the updated task."""
    # Validate field names
    for key in updates:
        if key not in _TASK_FIELDS:
            raise ValueError(f"Unknown task field: '{key}'")

    # Verify task exists
    get_task(hc_home, team, task_id)

    updates["updated_at"] = _now()

    # Serialize JSON columns with type coercion
    set_parts = []
    params: list = []
    for key, value in updates.items():
        set_parts.append(f"{key} = ?")
        if key == "depends_on":
            params.append(json.dumps([int(x) for x in value] if value else []))
        elif key == "repo":
            # Accept str or list[str]
            if isinstance(value, str):
                params.append(json.dumps([value] if value else []))
            else:
                params.append(json.dumps([str(x) for x in value] if value else []))
        elif key == "tags":
            params.append(json.dumps([str(x) for x in value] if value else []))
        elif key == "attachments":
            params.append(json.dumps([str(x) for x in value] if value else []))
        elif key in ("commits", "base_sha", "merge_base", "merge_tip", "metadata"):
            # Dict columns — keyed by repo name or free-form (metadata)
            if isinstance(value, dict):
                params.append(json.dumps(value))
            else:
                params.append(json.dumps(value) if value else "{}")
        else:
            params.append(value)
    params.extend([team, task_id])

    conn = get_connection(hc_home, team)
    try:
        conn.execute(
            f"UPDATE tasks SET {', '.join(set_parts)} WHERE team = ? AND id = ?",
            params,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE team = ? AND id = ?", (team, task_id)).fetchone()
        task = task_row_to_dict(row)
    finally:
        conn.close()

    return task


def assign_task(hc_home: Path, team: str, task_id: int, assignee: str, suppress_log: bool = False) -> dict:
    """Assign a task to an agent.

    On the first assignment (when ``dri`` is empty), the assignee is also
    recorded as the DRI (Directly Responsible Individual). The DRI never
    changes and is used for branch naming.

    Args:
        hc_home: Home directory path
        team: Team name
        task_id: Task ID
        assignee: Agent name to assign to
        suppress_log: If True, skip logging the assignment event (default: False)
    """
    task = get_task(hc_home, team, task_id)
    updates: dict[str, str] = {"assignee": assignee}
    if not task.get("dri"):
        updates["dri"] = assignee
    task = update_task(hc_home, team, task_id, **updates)

    if not suppress_log:
        from delegate.chat import log_event
        log_event(hc_home, team, f"{format_task_id(task_id)} assigned to {assignee.capitalize()}", task_id=task_id)
        _broadcast_update(task_id, team, {"assignee": assignee})

    return task


def _backfill_branch_metadata(hc_home: Path, team: str, task: dict, updates: dict) -> None:
    """Try to fill in missing branch and base_sha on a task.

    Called as a safety net when a task enters ``in_review`` or ``in_approval``
    status.  If the task already has both fields populated, this is a no-op.

    For ``branch``, derives the name from the team and task ID.
    For ``base_sha``, computes ``git merge-base main <branch>`` per repo.
    """
    import logging
    _log = logging.getLogger(__name__)

    repos = task.get("repo", [])
    if not repos:
        return

    task_id = task["id"]

    # Backfill branch name
    if not task.get("branch") and "branch" not in updates:
        from delegate.paths import get_team_id
        tid = get_team_id(hc_home, team)
        branch = f"delegate/{tid}/{team}/{format_task_id(task_id)}"
        updates["branch"] = branch
        _log.warning(
            "Backfilling branch=%s on task %s during status change — "
            "this should have been set at task creation",
            branch, task_id,
        )

    # Backfill base_sha (per-repo dict)
    branch = updates.get("branch") or task.get("branch", "")
    existing_base_sha: dict = task.get("base_sha", {})
    if not existing_base_sha and "base_sha" not in updates and branch:
        base_sha_dict: dict[str, str] = {}
        for repo_name in repos:
            try:
                from delegate.paths import repo_path as _repo_path
                git_cwd = str(_repo_path(hc_home, team, repo_name))
                result = subprocess.run(
                    ["git", "merge-base", "main", branch],
                    cwd=git_cwd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    base_sha_dict[repo_name] = result.stdout.strip()
                    _log.info(
                        "Backfilling base_sha[%s]=%s on task %s",
                        repo_name, base_sha_dict[repo_name][:8], task_id,
                    )
            except Exception as exc:
                _log.warning("Could not backfill base_sha for task %s repo %s: %s", task_id, repo_name, exc)
        if base_sha_dict:
            updates["base_sha"] = base_sha_dict


def _validate_review_gate(hc_home: Path, team: str, task: dict) -> None:
    """Validate that the task is ready for review.

    Raises ``ValueError`` if:
    1. The worktree has uncommitted changes.
    2. The branch has no new commits after ``base_sha``.
    3. The worktree has a different branch checked out than expected.
    """
    import subprocess
    from delegate.paths import task_worktree_dir

    repos: list[str] = task.get("repo", [])
    if not repos:
        return  # No repos — nothing to validate

    task_id = task["id"]
    branch = task.get("branch", "")
    base_sha_dict: dict = task.get("base_sha", {})

    for repo_name in repos:
        wt_path = task_worktree_dir(hc_home, team, repo_name, task_id)
        if not wt_path.is_dir():
            continue  # Worktree might not exist (no-repo task)

        wt_str = str(wt_path)

        # Check 1: no uncommitted changes
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=wt_str,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                raise ValueError(
                    f"Cannot move {format_task_id(task_id)} to in_review: "
                    f"worktree for {repo_name} has uncommitted changes. "
                    f"Please commit or stash before submitting for review."
                )
        except subprocess.TimeoutExpired:
            pass  # Skip validation if git is slow

        # Check 2: at least one commit after base_sha
        base_sha = base_sha_dict.get(repo_name, "")
        if base_sha:
            try:
                result = subprocess.run(
                    ["git", "log", f"{base_sha}..HEAD", "--oneline"],
                    cwd=wt_str,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and not result.stdout.strip():
                    raise ValueError(
                        f"Cannot move {format_task_id(task_id)} to in_review: "
                        f"branch for {repo_name} has no commits beyond base. "
                        f"Please make at least one commit before submitting for review."
                    )
            except subprocess.TimeoutExpired:
                pass

        # Check 3: worktree has the correct branch checked out
        if branch:
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=wt_str,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    checked_out = result.stdout.strip()
                    if checked_out != branch:
                        raise ValueError(
                            f"Cannot move {format_task_id(task_id)} to in_review: "
                            f"worktree for {repo_name} has '{checked_out}' checked out, "
                            f"expected '{branch}'."
                        )
            except subprocess.TimeoutExpired:
                pass


def change_status(hc_home: Path, team: str, task_id: int, status: str, suppress_log: bool = False) -> dict:
    """Change task status with workflow-driven validation and hooks.

    If the task has a ``workflow`` field, loads the workflow definition
    and uses it for transition validation and hook execution.  Otherwise
    falls back to the legacy ``VALID_TRANSITIONS`` table.

    Hook execution order:
    1. ``exit()`` on the **old** stage (cleanup).
    2. ``enter()`` on the **new** stage (gates, setup).
       If ``enter()`` raises ``GateError``, the transition is aborted.
    3. ``assign()`` on the **new** stage (optional reassignment).

    Args:
        hc_home: Home directory path
        team: Team name
        task_id: Task ID
        status: New status to transition to
        suppress_log: If True, skip logging the status change event (default: False)
    """
    old_task = get_task(hc_home, team, task_id)
    current = old_task["status"]

    wf_name = old_task.get("workflow", "")
    wf_version = old_task.get("workflow_version", 0)

    # ── Validate transition ──
    if wf_name and wf_version:
        # Workflow-driven validation
        try:
            from delegate.workflow import load_workflow_cached
            wf = load_workflow_cached(hc_home, team, wf_name, wf_version)
            wf.validate_transition(current, status)
        except (FileNotFoundError, KeyError):
            # Workflow file missing — fall back to legacy validation
            _legacy_validate_transition(current, status)
    else:
        # Legacy validation
        _legacy_validate_transition(current, status)

    # ── Run workflow hooks ──
    wf_def = None
    if wf_name and wf_version:
        try:
            from delegate.workflow import load_workflow_cached
            wf_def = load_workflow_cached(hc_home, team, wf_name, wf_version)
        except (FileNotFoundError, KeyError):
            wf_def = None

    if wf_def:
        from delegate.workflows.core import Context
        ctx = Context(hc_home, team, old_task)

        # 1. Exit hook on old stage
        if current in wf_def.stage_map:
            try:
                old_stage = wf_def.stage_map[current]()
                old_stage.exit(ctx)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Exit hook failed for stage '%s' on %s: %s",
                    current, format_task_id(task_id), exc,
                )

        # 2. Enter hook on new stage (can raise GateError to block)
        if status in wf_def.stage_map:
            new_stage = wf_def.stage_map[status]()
            new_stage.enter(ctx)  # May raise GateError — intentionally not caught

    # ── Build status updates ──
    old_status = current.replace("_", " ").title()
    updates: dict = {"status": status}

    # Legacy fallback: handle completed_at for terminal states
    if not wf_def:
        if status in ("done", "cancelled"):
            updates["completed_at"] = _now()

        # Safety net: backfill branch/base_sha when entering in_review or in_approval
        if status in ("in_review", "in_approval"):
            _backfill_branch_metadata(hc_home, team, old_task, updates)

        # Review gate (legacy path — workflow uses enter() hook instead)
        if status == "in_review":
            _validate_review_gate(hc_home, team, old_task)

        # When entering in_approval, increment review_attempt and create a pending review
        if status == "in_approval":
            new_attempt = old_task.get("review_attempt", 0) + 1
            updates["review_attempt"] = new_attempt
            updates["approval_status"] = ""

    task = update_task(hc_home, team, task_id, **updates)

    # Legacy: create review row after task is updated
    if not wf_def and status == "in_approval":
        from delegate.review import create_review
        try:
            create_review(hc_home, team, task_id, task["review_attempt"])
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to create review row for %s attempt %d",
                format_task_id(task_id), task["review_attempt"],
            )

    # ── Workflow assign hook ──
    if wf_def and status in wf_def.stage_map:
        try:
            from delegate.workflows.core import Context
            ctx = Context(hc_home, team, task)
            new_stage = wf_def.stage_map[status]()
            new_assignee = new_stage.assign(ctx)
            if new_assignee:
                task = assign_task(hc_home, team, task_id, new_assignee, suppress_log=True)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Assign hook failed for stage '%s' on %s: %s",
                status, format_task_id(task_id), exc,
            )

    if not suppress_log:
        new_status = status.replace("_", " ").title()
        from delegate.chat import log_event
        log_event(hc_home, team, f"{format_task_id(task_id)} {old_status} \u2192 {new_status}", task_id=task_id)
        _broadcast_update(task_id, team, {"status": status})

    return task


def _legacy_validate_transition(current: str, status: str) -> None:
    """Validate a status transition using the hardcoded VALID_TRANSITIONS table."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {VALID_STATUSES}")
    allowed = VALID_TRANSITIONS.get(current, set())
    if allowed and status not in allowed:
        raise ValueError(
            f"Invalid transition: '{current}' \u2192 '{status}'. "
            f"Allowed transitions from '{current}': {sorted(allowed)}"
        )
    if not allowed and current in VALID_TRANSITIONS:
        raise ValueError(
            f"Cannot transition from terminal status '{current}'."
        )


def transition_task(hc_home: Path, team: str, task_id: int, new_status: str, new_assignee: str) -> dict:
    """Change task status and assignee together with a single combined log message.

    This function combines status change and assignment into one operation, emitting
    a single activity feed message like: 'T0026: In Review → In Approval, assigned to Nikhil'

    Args:
        hc_home: Home directory path
        team: Team name
        task_id: Task ID
        new_status: New status to transition to
        new_assignee: Agent name to assign to

    Returns:
        The updated task dict
    """
    # Get current task to capture old status for the combined message
    old_task = get_task(hc_home, team, task_id)
    old_status = old_task["status"].replace("_", " ").title()

    # Perform both operations without logging
    task = change_status(hc_home, team, task_id, new_status, suppress_log=True)
    task = assign_task(hc_home, team, task_id, new_assignee, suppress_log=True)

    # Emit a single combined log message
    new_status_title = new_status.replace("_", " ").title()
    from delegate.chat import log_event
    log_event(
        hc_home,
        team,
        f"{format_task_id(task_id)}: {old_status} \u2192 {new_status_title}, assigned to {new_assignee.capitalize()}",
        task_id=task_id,
    )
    _broadcast_update(task_id, team, {"status": new_status, "assignee": new_assignee})

    return task


def cancel_task(hc_home: Path, team: str, task_id: int) -> dict:
    """Cancel a task and clean up its worktrees and branches.

    Sets status to ``cancelled``, clears the assignee, and removes
    associated Git worktrees and feature branches (best-effort).

    If an agent is mid-turn on the task, the cleanup is still safe:
    the runtime will notice the task is cancelled when it finishes and
    won't dispatch further turns.  Git worktree removal only deletes
    the working directory — in-flight git processes may see errors but
    won't corrupt anything.
    """
    task = get_task(hc_home, team, task_id)

    if task["status"] == "done":
        raise ValueError(
            f"Task {format_task_id(task_id)} is already 'done' and cannot be cancelled."
        )

    if task["status"] == "cancelled":
        # Idempotent: re-run cleanup only (agent may have recreated branches)
        _cleanup_cancelled_task(hc_home, team, task)
        return task

    # Transition to cancelled and clear assignee
    updated = change_status(hc_home, team, task_id, "cancelled", suppress_log=True)
    updated = assign_task(hc_home, team, task_id, "", suppress_log=True)

    from delegate.chat import log_event
    log_event(
        hc_home, team,
        f"{format_task_id(task_id)} cancelled",
        task_id=task_id,
    )

    # Best-effort cleanup of worktrees and branches
    _cleanup_cancelled_task(hc_home, team, task)

    return updated


def _cleanup_cancelled_task(hc_home: Path, team: str, task: dict) -> None:
    """Remove worktrees and feature branch for a cancelled task (best-effort)."""
    _log = logging.getLogger(__name__)

    branch: str = task.get("branch", "")
    repos: list[str] = task.get("repo", [])

    if not repos or not branch:
        return

    for repo_name in repos:
        try:
            from delegate.repo import get_repo_path, remove_task_worktree
            repo_dir = get_repo_path(hc_home, team, repo_name)
            real_repo = str(repo_dir.resolve())

            # Remove agent worktree
            try:
                remove_task_worktree(hc_home, team, repo_name, task["id"])
            except Exception as exc:
                _log.warning(
                    "Could not remove worktree for %s (%s): %s",
                    format_task_id(task["id"]), repo_name, exc,
                )

            # Delete the feature branch (best-effort; -D to force)
            if branch:
                import subprocess
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    cwd=real_repo,
                    capture_output=True,
                    check=False,
                )
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=real_repo,
                    capture_output=True,
                    check=False,
                )
                _log.info(
                    "Cleaned up branch %s for cancelled %s in %s",
                    branch, format_task_id(task["id"]), repo_name,
                )
        except Exception as exc:
            _log.warning(
                "Cleanup error for cancelled %s (%s): %s",
                format_task_id(task["id"]), repo_name, exc,
            )


def set_task_branch(hc_home: Path, team: str, task_id: int, branch_name: str) -> dict:
    """Set the branch name on a task."""
    return update_task(hc_home, team, task_id, branch=branch_name)



def attach_file(hc_home: Path, team: str, task_id: int, file_path: str) -> dict:
    """Attach a file path to the task. Idempotent — duplicates are ignored."""
    task = get_task(hc_home, team, task_id)
    attachments = list(task.get("attachments", []))
    if file_path not in attachments:
        attachments.append(file_path)
    return update_task(hc_home, team, task_id, attachments=attachments)


def detach_file(hc_home: Path, team: str, task_id: int, file_path: str) -> dict:
    """Remove a file path from the task's attachments."""
    task = get_task(hc_home, team, task_id)
    attachments = [a for a in task.get("attachments", []) if a != file_path]
    return update_task(hc_home, team, task_id, attachments=attachments)


# ---------------------------------------------------------------------------
# Task comments
# ---------------------------------------------------------------------------

def add_comment(hc_home: Path, team: str, task_id: int, author: str, body: str) -> int:
    """Add a comment to a task. Returns the comment ID.

    Also logs a system event for the activity timeline.
    """
    get_task(hc_home, team, task_id)  # Verify task exists

    conn = get_connection(hc_home, team)
    try:
        cursor = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, team) VALUES (?, ?, ?, ?)",
            (task_id, author, body, team),
        )
        conn.commit()
        comment_id = cursor.lastrowid
    finally:
        conn.close()

    from delegate.chat import log_event
    log_event(
        hc_home, team,
        f"{author.capitalize()} commented on {format_task_id(task_id)}",
        task_id=task_id,
    )

    return comment_id


def get_comments(hc_home: Path, team: str, task_id: int, limit: int = 50) -> list[dict]:
    """Return comments for a task, oldest first.

    Returns ``[{id, task_id, author, body, created_at}, ...]``.
    """
    conn = get_connection(hc_home, team)
    try:
        rows = conn.execute(
            "SELECT id, task_id, author, body, created_at "
            "FROM task_comments WHERE task_id = ? ORDER BY id ASC LIMIT ?",
            (task_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_task_diff(hc_home: Path, team: str, task_id: int) -> dict[str, str]:
    """Return the git diff for the task's branch, keyed by repo name.

    For multi-repo tasks, returns ``{repo_name: diff_text, ...}``.
    For tasks with no repos, returns ``{"_default": diff_text}``.

    If ``base_sha`` is set per-repo, uses ``base_sha...branch`` (three-dot
    merge-base diff) for a precise diff showing only the agent's changes.
    Otherwise falls back to ``main...branch``.
    """
    task = get_task(hc_home, team, task_id)
    branch = task.get("branch", "")
    if not branch:
        return {"_default": "(no branch set)"}

    repos = task.get("repo", [])
    if not repos:
        # No repos — try diff from hc_home
        diff = _diff_for_one_repo(str(hc_home), branch, task, "_default")
        return {"_default": diff}

    from delegate.paths import repo_path as _repo_path
    diffs: dict[str, str] = {}
    for repo_name in repos:
        try:
            git_cwd = str(_repo_path(hc_home, team, repo_name))
        except FileNotFoundError:
            diffs[repo_name] = f"(repo '{repo_name}' not found)"
            continue
        diffs[repo_name] = _diff_for_one_repo(git_cwd, branch, task, repo_name)
    return diffs


def _diff_for_one_repo(git_cwd: str, branch: str, task: dict, repo_key: str) -> str:
    """Compute the diff for a single repo within a task."""
    # Prefer merge_base..merge_tip for merged tasks (exact diff that landed)
    merge_base_dict: dict = task.get("merge_base", {})
    merge_tip_dict: dict = task.get("merge_tip", {})
    merge_base = merge_base_dict.get(repo_key, "")
    merge_tip = merge_tip_dict.get(repo_key, "")

    if merge_base and merge_tip:
        try:
            result = subprocess.run(
                ["git", "diff", f"{merge_base}..{merge_tip}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=git_cwd,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # Fall back to base_sha...branch (pre-merge or older tasks)
    base_sha_dict: dict = task.get("base_sha", {})
    base_sha = base_sha_dict.get(repo_key, "")
    diff_base = base_sha if base_sha else "main"

    try:
        result = subprocess.run(
            ["git", "diff", f"{diff_base}...{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=git_cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "(no diff available)"


def get_task_merge_preview(hc_home: Path, team: str, task_id: int) -> dict[str, str]:
    """Return a diff of ``main...branch`` (current main merge-base) per repo.

    Unlike :func:`get_task_diff` which uses the *recorded* ``base_sha``
    (the main SHA at branch-creation time), this computes the diff
    relative to the *current* merge-base of ``main`` and ``branch``.
    The result shows what the merge into current ``main`` would look like.
    """
    task = get_task(hc_home, team, task_id)
    branch = task.get("branch", "")
    if not branch:
        return {"_default": "(no branch set)"}

    repos = task.get("repo", [])
    if not repos:
        diff = _merge_preview_for_one_repo(str(hc_home), branch)
        return {"_default": diff}

    from delegate.paths import repo_path as _repo_path

    diffs: dict[str, str] = {}
    for repo_name in repos:
        try:
            git_cwd = str(_repo_path(hc_home, team, repo_name))
        except FileNotFoundError:
            diffs[repo_name] = f"(repo '{repo_name}' not found)"
            continue
        diffs[repo_name] = _merge_preview_for_one_repo(git_cwd, branch)
    return diffs


def _merge_preview_for_one_repo(git_cwd: str, branch: str) -> str:
    """Compute ``git diff main...branch`` using the *current* main HEAD."""
    try:
        result = subprocess.run(
            ["git", "diff", "main...%s" % branch],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=git_cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # If the branch doesn't exist or diff fails, try a two-dot diff
    try:
        result = subprocess.run(
            ["git", "diff", "main..%s" % branch],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=git_cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "(no merge preview available)"


def get_task_commit_diffs(
    hc_home: Path, team: str, task_id: int,
) -> dict[str, list[dict]]:
    """Return per-commit diffs for a task, keyed by repo name.

    Returns ``{repo_name: [{"sha": str, "message": str, "diff": str}, ...]}``.
    Commits are always discovered dynamically via ``git log base_sha..branch``.
    """
    task = get_task(hc_home, team, task_id)
    repos: list[str] = task.get("repo", [])
    branch: str = task.get("branch", "")
    base_sha_dict: dict = task.get("base_sha", {})

    if not branch or not repos:
        return {}

    from delegate.paths import repo_path as _repo_path

    results: dict[str, list[dict]] = {}

    for repo_name in repos:
        try:
            git_cwd = str(_repo_path(hc_home, team, repo_name))
        except FileNotFoundError:
            results[repo_name] = [{"sha": "", "message": "", "diff": f"(repo '{repo_name}' not found)"}]
            continue

        # Discover commits from git log
        base_sha = base_sha_dict.get(repo_name, "")
        range_spec = f"{base_sha}..{branch}" if base_sha else f"main..{branch}"
        try:
            log_result = subprocess.run(
                ["git", "log", "--reverse", "--pretty=format:%H%n%s", range_spec],
                capture_output=True, text=True, timeout=30, cwd=git_cwd,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            results[repo_name] = [{"sha": "", "message": "", "diff": f"(failed to discover commits for '{repo_name}')"}]
            continue

        if log_result.returncode != 0 or not log_result.stdout.strip():
            continue  # No commits found

        lines = log_result.stdout.strip().split("\n")
        repo_results: list[dict] = []
        for i in range(0, len(lines), 2):
            sha = lines[i]
            msg = lines[i + 1] if i + 1 < len(lines) else ""
            diff = ""
            try:
                diff_result = subprocess.run(
                    ["git", "diff", f"{sha}~1..{sha}"],
                    capture_output=True, text=True, timeout=30, cwd=git_cwd,
                )
                if diff_result.returncode == 0:
                    diff = diff_result.stdout
                else:
                    show_result = subprocess.run(
                        ["git", "show", sha, "--format=", "--diff-merges=first-parent"],
                        capture_output=True, text=True, timeout=30, cwd=git_cwd,
                    )
                    if show_result.returncode == 0:
                        diff = show_result.stdout
            except (subprocess.TimeoutExpired, FileNotFoundError):
                diff = "(failed to compute diff)"
            repo_results.append({"sha": sha, "message": msg, "diff": diff or "(empty diff)"})
        if repo_results:
            results[repo_name] = repo_results

    return results


def list_tasks(
    hc_home: Path,
    team: str,
    status: str | None = None,
    assignee: str | None = None,
    project: str | None = None,
    tag: str | None = None,
) -> list[dict]:
    """List tasks with optional filters.

    *tag* filters to tasks whose ``tags`` JSON array contains the given value.
    """
    conn = get_connection(hc_home, team)
    try:
        query = "SELECT * FROM tasks WHERE team = ?"
        params: list = [team]

        if status:
            query += " AND status = ?"
            params.append(status)
        if assignee:
            query += " AND assignee = ?"
            params.append(assignee)
        if project:
            query += " AND project = ?"
            params.append(project)

        query += " ORDER BY id ASC"

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    tasks = [task_row_to_dict(row) for row in rows]

    # Tag filtering (done in Python since tags are stored as JSON)
    if tag:
        tasks = [t for t in tasks if tag in t.get("tags", [])]

    return tasks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Task management")
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create", help="Create a task")
    p_create.add_argument("home", type=Path)
    p_create.add_argument("team")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--assignee", required=True, help="Agent to assign the task to (sets DRI)")
    p_create.add_argument("--description", default="")
    p_create.add_argument("--project", default="")
    p_create.add_argument("--priority", default="medium", choices=VALID_PRIORITIES)
    p_create.add_argument("--repo", default="", help="Registered repo name for this task")
    p_create.add_argument("--tags", nargs="*", default=[], help="Free-form labels for the task")

    # list
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("home", type=Path)
    p_list.add_argument("team")
    p_list.add_argument("--status", choices=VALID_STATUSES)
    p_list.add_argument("--assignee")
    p_list.add_argument("--project")
    p_list.add_argument("--tag", help="Filter by tag")

    # update
    p_update = sub.add_parser("update", help="Update a task")
    p_update.add_argument("home", type=Path)
    p_update.add_argument("team")
    p_update.add_argument("task_id", type=int)
    p_update.add_argument("--title")
    p_update.add_argument("--description")
    p_update.add_argument("--priority", choices=VALID_PRIORITIES)

    # assign
    p_assign = sub.add_parser("assign", help="Assign a task")
    p_assign.add_argument("home", type=Path)
    p_assign.add_argument("team")
    p_assign.add_argument("task_id", type=int)
    p_assign.add_argument("assignee")

    # status
    p_status = sub.add_parser("status", help="Change task status")
    p_status.add_argument("home", type=Path)
    p_status.add_argument("team")
    p_status.add_argument("task_id", type=int)
    p_status.add_argument("new_status", choices=VALID_STATUSES)
    p_status.add_argument("--assignee", help="Also reassign the task (combined single event)")

    # show
    p_show = sub.add_parser("show", help="Show a task")
    p_show.add_argument("home", type=Path)
    p_show.add_argument("team")
    p_show.add_argument("task_id", type=int)

    # attach
    p_attach = sub.add_parser("attach", help="Attach a file to a task")
    p_attach.add_argument("home", type=Path)
    p_attach.add_argument("team")
    p_attach.add_argument("task_id", type=int)
    p_attach.add_argument("file", help="Path to the file to attach")

    # detach
    p_detach = sub.add_parser("detach", help="Detach a file from a task")
    p_detach.add_argument("home", type=Path)
    p_detach.add_argument("team")
    p_detach.add_argument("task_id", type=int)
    p_detach.add_argument("file", help="Path of the file to detach")

    # comment
    p_comment = sub.add_parser("comment", help="Add a comment to a task")
    p_comment.add_argument("home", type=Path)
    p_comment.add_argument("team")
    p_comment.add_argument("task_id", type=int)
    p_comment.add_argument("author", help="Name of the comment author")
    p_comment.add_argument("body", help="Comment body text")

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a task and clean up worktrees/branches")
    p_cancel.add_argument("home", type=Path)
    p_cancel.add_argument("team")
    p_cancel.add_argument("task_id", type=int)

    args = parser.parse_args()

    if args.command == "create":
        task = create_task(
            args.home,
            args.team,
            title=args.title,
            assignee=args.assignee,
            description=args.description,
            project=args.project,
            priority=args.priority,
            repo=args.repo,
            tags=args.tags or None,
        )
        print(f"Created {format_task_id(task['id'])}: {task['title']} (assigned to {args.assignee})")

    elif args.command == "list":
        tasks = list_tasks(
            args.home,
            args.team,
            status=args.status,
            assignee=args.assignee,
            project=args.project,
            tag=args.tag,
        )
        for t in tasks:
            assignee = f" [{t['assignee']}]" if t["assignee"] else ""
            print(f"  {format_task_id(t['id'])} ({t['status']}) {t['title']}{assignee}")
        if not tasks:
            print("(no tasks)")

    elif args.command == "update":
        updates = {}
        if args.title:
            updates["title"] = args.title
        if args.description:
            updates["description"] = args.description
        if args.priority:
            updates["priority"] = args.priority
        task = update_task(args.home, args.team, args.task_id, **updates)
        print(f"Updated {format_task_id(task['id'])}")

    elif args.command == "assign":
        task = assign_task(args.home, args.team, args.task_id, args.assignee)
        print(f"Assigned {format_task_id(task['id'])} to {args.assignee}")

    elif args.command == "status":
        if args.assignee:
            task = transition_task(args.home, args.team, args.task_id, args.new_status, args.assignee)
            print(f"{format_task_id(task['id'])} -> {args.new_status}, assigned to {args.assignee}")
        else:
            task = change_status(args.home, args.team, args.task_id, args.new_status)
            print(f"{format_task_id(task['id'])} -> {args.new_status}")

    elif args.command == "show":
        task = get_task(args.home, args.team, args.task_id)
        import yaml
        print(yaml.dump(task, default_flow_style=False, sort_keys=False))

    elif args.command == "attach":
        task = attach_file(args.home, args.team, args.task_id, args.file)
        print(f"Attached '{args.file}' to {format_task_id(task['id'])}")
        for f in task.get("attachments", []):
            print(f"  - {f}")

    elif args.command == "detach":
        task = detach_file(args.home, args.team, args.task_id, args.file)
        print(f"Detached '{args.file}' from {format_task_id(task['id'])}")

    elif args.command == "comment":
        cid = add_comment(args.home, args.team, args.task_id, args.author, args.body)
        print(f"Comment #{cid} added to {format_task_id(args.task_id)} by {args.author}")

    elif args.command == "cancel":
        task = cancel_task(args.home, args.team, args.task_id)
        print(f"{format_task_id(args.task_id)} cancelled")


if __name__ == "__main__":
    main()
