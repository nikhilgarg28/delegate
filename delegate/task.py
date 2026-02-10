"""SQLite-backed task management — global across all teams.

Tasks are stored in the ``tasks`` table of ``~/.delegate/db.sqlite``.

Each task has a **DRI** (Directly Responsible Individual) set on first
assignment — the DRI never changes and anchors the branch name
(``<dri>/T<NNNN>``).  The **assignee** field tracks who currently owns
the ball and is updated by the manager as the task moves through stages.

Usage:
    python -m delegate.task create <home> --title "Build API" [--priority high]
    python -m delegate.task list <home> [--status open] [--assignee alice]
    python -m delegate.task update <home> <task_id> [--title ...] [--description ...] [--priority ...]
    python -m delegate.task assign <home> <task_id> <assignee>
    python -m delegate.task status <home> <task_id> <status>
    python -m delegate.task show <home> <task_id>
"""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from delegate.db import get_connection, task_row_to_dict, _JSON_COLUMNS


VALID_STATUSES = ("open", "in_progress", "review", "done", "needs_merge", "merged", "rejected", "conflict")
VALID_PRIORITIES = ("low", "medium", "high", "critical")
VALID_APPROVAL_STATUSES = ("", "pending", "approved", "rejected")

# Allowed status transitions: from_status -> set of valid to_statuses
VALID_TRANSITIONS = {
    "open": {"in_progress"},
    "in_progress": {"review"},
    "review": {"done", "needs_merge", "in_progress"},
    "needs_merge": {"merged", "rejected", "conflict"},
    "rejected": {"in_progress"},
    "conflict": {"in_progress"},
    # done and merged are terminal states — no transitions out
    "done": set(),
    "merged": set(),
}

# All columns in the tasks table (used for field validation on update).
_TASK_FIELDS = frozenset({
    "id", "title", "description", "status", "dri", "assignee",
    "project", "priority", "repo", "tags", "created_at", "updated_at",
    "completed_at", "depends_on", "branch", "base_sha", "commits",
    "rejection_reason", "approval_status", "merge_base", "merge_tip",
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
    title: str,
    description: str = "",
    project: str = "",
    priority: str = "medium",
    depends_on: list[int] | None = None,
    repo: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Create a new task. Returns the task dict with assigned ID.

    *tags* is an optional free-form list of string labels (e.g.
    ``["bugfix", "frontend"]``).
    """
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority '{priority}'. Must be one of: {VALID_PRIORITIES}")

    now = _now()
    conn = get_connection(hc_home)
    try:
        cursor = conn.execute(
            """\
            INSERT INTO tasks (
                title, description, status, dri, assignee,
                project, priority, repo, tags,
                created_at, updated_at, completed_at,
                depends_on, branch, base_sha, commits,
                rejection_reason, approval_status, merge_base, merge_tip
            ) VALUES (
                ?, ?, 'open', '', '',
                ?, ?, ?, ?,
                ?, ?, '',
                ?, '', '', '[]',
                '', '', '', ''
            )""",
            (
                title, description,
                project, priority, repo,
                json.dumps(list(tags) if tags else []),
                now, now,
                json.dumps(depends_on or []),
            ),
        )
        conn.commit()
        task_id = cursor.lastrowid

        # Read back the full row to return
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task = task_row_to_dict(row)
    finally:
        conn.close()

    from delegate.chat import log_event
    log_event(hc_home, f"{format_task_id(task_id)} created \u2014 {title}")

    return task


def get_task(hc_home: Path, task_id: int) -> dict:
    """Load a single task by ID.

    Raises ``FileNotFoundError`` if the task does not exist (preserves
    the same exception type used by the previous YAML implementation).
    """
    conn = get_connection(hc_home)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()

    if row is None:
        raise FileNotFoundError(f"Task {task_id} not found")

    return task_row_to_dict(row)


def update_task(hc_home: Path, task_id: int, **updates) -> dict:
    """Update fields on an existing task. Returns the updated task."""
    # Validate field names
    for key in updates:
        if key not in _TASK_FIELDS:
            raise ValueError(f"Unknown task field: '{key}'")

    # Verify task exists
    get_task(hc_home, task_id)

    updates["updated_at"] = _now()

    # Serialize JSON columns
    set_parts = []
    params: list = []
    for key, value in updates.items():
        set_parts.append(f"{key} = ?")
        if key in _JSON_COLUMNS:
            params.append(json.dumps(value))
        else:
            params.append(value)
    params.append(task_id)

    conn = get_connection(hc_home)
    try:
        conn.execute(
            f"UPDATE tasks SET {', '.join(set_parts)} WHERE id = ?",
            params,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task = task_row_to_dict(row)
    finally:
        conn.close()

    return task


def assign_task(hc_home: Path, task_id: int, assignee: str) -> dict:
    """Assign a task to an agent.

    On the first assignment (when ``dri`` is empty), the assignee is also
    recorded as the DRI (Directly Responsible Individual). The DRI never
    changes and is used for branch naming.
    """
    task = get_task(hc_home, task_id)
    updates: dict[str, str] = {"assignee": assignee}
    if not task.get("dri"):
        updates["dri"] = assignee
    task = update_task(hc_home, task_id, **updates)

    from delegate.chat import log_event
    log_event(hc_home, f"{format_task_id(task_id)} assigned to {assignee.capitalize()}")

    return task


def _backfill_branch_metadata(hc_home: Path, task: dict, updates: dict) -> None:
    """Try to fill in missing branch and base_sha on a task.

    Called as a safety net when a task enters ``review`` or ``needs_merge``
    status.  If the task already has both fields populated, this is a no-op.

    For ``branch``, derives the name from the DRI and task ID.
    For ``base_sha``, computes ``git merge-base main <branch>`` in the repo.
    """
    import logging
    _log = logging.getLogger(__name__)

    repo_name = task.get("repo", "")
    if not repo_name:
        return

    dri = task.get("dri", "")
    task_id = task["id"]

    # Backfill branch name
    if not task.get("branch") and "branch" not in updates:
        if dri:
            branch = f"{dri}/{format_task_id(task_id)}"
            updates["branch"] = branch
            _log.info("Backfilling branch=%s on task %s during status change", branch, task_id)

    # Backfill base_sha
    branch = updates.get("branch") or task.get("branch", "")
    if not task.get("base_sha") and "base_sha" not in updates and branch:
        try:
            from delegate.paths import repo_path as _repo_path
            git_cwd = str(_repo_path(hc_home, repo_name))
            result = subprocess.run(
                ["git", "merge-base", "main", branch],
                cwd=git_cwd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                updates["base_sha"] = result.stdout.strip()
                _log.info(
                    "Backfilling base_sha=%s on task %s during status change",
                    updates["base_sha"][:8], task_id,
                )
        except Exception as exc:
            _log.warning("Could not backfill base_sha for task %s: %s", task_id, exc)


def change_status(hc_home: Path, task_id: int, status: str) -> dict:
    """Change task status. Sets completed_at when moving to 'done' or 'merged'.

    Validates status transitions according to VALID_TRANSITIONS.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {VALID_STATUSES}")
    old_task = get_task(hc_home, task_id)
    current = old_task["status"]

    # Validate transition
    allowed = VALID_TRANSITIONS.get(current, set())
    if allowed and status not in allowed:
        raise ValueError(
            f"Invalid transition: '{current}' \u2192 '{status}'. "
            f"Allowed transitions from '{current}': {sorted(allowed)}"
        )
    if not allowed and current in VALID_TRANSITIONS:
        # Terminal state — no transitions allowed out
        raise ValueError(
            f"Cannot transition from terminal status '{current}'."
        )

    old_status = current.replace("_", " ").title()
    updates: dict = {"status": status}
    if status in ("done", "merged"):
        updates["completed_at"] = _now()

    # Safety net: backfill branch/base_sha when entering review or needs_merge
    # if they're still empty.
    if status in ("review", "needs_merge"):
        _backfill_branch_metadata(hc_home, old_task, updates)

    task = update_task(hc_home, task_id, **updates)

    new_status = status.replace("_", " ").title()
    from delegate.chat import log_event
    log_event(hc_home, f"{format_task_id(task_id)} {old_status} \u2192 {new_status}")

    return task


def set_task_branch(hc_home: Path, task_id: int, branch_name: str) -> dict:
    """Set the branch name on a task."""
    return update_task(hc_home, task_id, branch=branch_name)


def add_task_commit(hc_home: Path, task_id: int, commit_sha: str) -> dict:
    """Append a commit SHA to the task's commits list."""
    task = get_task(hc_home, task_id)
    commits = list(task.get("commits", []))
    if commit_sha not in commits:
        commits.append(commit_sha)
    return update_task(hc_home, task_id, commits=commits)


def get_task_diff(hc_home: Path, task_id: int) -> str:
    """Return the git diff for the task's branch.

    If the task has a ``repo`` field, diffs are run against the repo
    (via symlink in ``~/.delegate/repos/<repo>/``).  Otherwise falls
    back to ``hc_home`` as the git working directory.

    If ``base_sha`` is set on the task, uses ``base_sha...branch`` (three-dot
    merge-base diff) for a precise diff showing only the agent's changes.
    Otherwise falls back to ``main...branch``.
    """
    task = get_task(hc_home, task_id)
    branch = task.get("branch", "")
    if not branch:
        return "(no branch set)"

    # Determine git cwd — prefer the repo (symlink) if set
    repo_name = task.get("repo", "")
    if repo_name:
        from delegate.paths import repo_path as _repo_path
        git_cwd = str(_repo_path(hc_home, repo_name))
    else:
        git_cwd = str(hc_home)

    # Prefer merge_base..merge_tip for merged tasks (exact diff that landed)
    merge_base = task.get("merge_base", "")
    merge_tip = task.get("merge_tip", "")
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
    base_sha = task.get("base_sha", "")
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


def list_tasks(
    hc_home: Path,
    status: str | None = None,
    assignee: str | None = None,
    project: str | None = None,
    tag: str | None = None,
) -> list[dict]:
    """List tasks with optional filters.

    *tag* filters to tasks whose ``tags`` JSON array contains the given value.
    """
    conn = get_connection(hc_home)
    try:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list = []

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
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--description", default="")
    p_create.add_argument("--project", default="")
    p_create.add_argument("--priority", default="medium", choices=VALID_PRIORITIES)
    p_create.add_argument("--repo", default="", help="Registered repo name for this task")
    p_create.add_argument("--tags", nargs="*", default=[], help="Free-form labels for the task")

    # list
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("home", type=Path)
    p_list.add_argument("--status", choices=VALID_STATUSES)
    p_list.add_argument("--assignee")
    p_list.add_argument("--project")
    p_list.add_argument("--tag", help="Filter by tag")

    # update
    p_update = sub.add_parser("update", help="Update a task")
    p_update.add_argument("home", type=Path)
    p_update.add_argument("task_id", type=int)
    p_update.add_argument("--title")
    p_update.add_argument("--description")
    p_update.add_argument("--priority", choices=VALID_PRIORITIES)

    # assign
    p_assign = sub.add_parser("assign", help="Assign a task")
    p_assign.add_argument("home", type=Path)
    p_assign.add_argument("task_id", type=int)
    p_assign.add_argument("assignee")

    # status
    p_status = sub.add_parser("status", help="Change task status")
    p_status.add_argument("home", type=Path)
    p_status.add_argument("task_id", type=int)
    p_status.add_argument("new_status", choices=VALID_STATUSES)

    # show
    p_show = sub.add_parser("show", help="Show a task")
    p_show.add_argument("home", type=Path)
    p_show.add_argument("task_id", type=int)

    args = parser.parse_args()

    if args.command == "create":
        task = create_task(
            args.home,
            title=args.title,
            description=args.description,
            project=args.project,
            priority=args.priority,
            repo=args.repo,
            tags=args.tags or None,
        )
        print(f"Created {format_task_id(task['id'])}: {task['title']}")

    elif args.command == "list":
        tasks = list_tasks(
            args.home,
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
        task = update_task(args.home, args.task_id, **updates)
        print(f"Updated {format_task_id(task['id'])}")

    elif args.command == "assign":
        task = assign_task(args.home, args.task_id, args.assignee)
        print(f"Assigned {format_task_id(task['id'])} to {args.assignee}")

    elif args.command == "status":
        task = change_status(args.home, args.task_id, args.new_status)
        print(f"{format_task_id(task['id'])} -> {args.new_status}")

    elif args.command == "show":
        task = get_task(args.home, args.task_id)
        import yaml
        print(yaml.dump(task, default_flow_style=False, sort_keys=False))


if __name__ == "__main__":
    main()
