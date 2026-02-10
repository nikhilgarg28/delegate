"""File-based task management — global across all teams.

Tasks are stored as individual YAML files under ``~/.delegate/tasks/``.

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
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from delegate.paths import tasks_dir as _resolve_tasks_dir


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


def _tasks_dir(hc_home: Path) -> Path:
    d = _resolve_tasks_dir(hc_home)
    if not d.is_dir():
        raise FileNotFoundError(f"Tasks directory not found: {d}")
    return d


def _next_id(td: Path) -> int:
    """Determine the next task ID by scanning existing files."""
    max_id = 0
    for f in td.glob("T*.yaml"):
        try:
            num = int(f.stem[1:])
            max_id = max(max_id, num)
        except (IndexError, ValueError):
            continue
    return max_id + 1


def format_task_id(task_id: int) -> str:
    """Format a task ID as ``T`` followed by zero-padded digits.

    Always uses at least 4 digits, but automatically widens
    for IDs ≥ 10000 (e.g. ``T10000``).
    """
    return f"T{task_id:04d}"


def _task_path(td: Path, task_id: int) -> Path:
    return td / f"{format_task_id(task_id)}.yaml"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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

    td = _tasks_dir(hc_home)
    task_id = _next_id(td)
    now = _now()

    task = {
        "id": task_id,
        "title": title,
        "description": description,
        "status": "open",
        "dri": "",
        "assignee": "",
        "project": project,
        "priority": priority,
        "repo": repo,
        "tags": list(tags) if tags else [],
        "created_at": now,
        "updated_at": now,
        "completed_at": "",
        "depends_on": depends_on or [],
        "branch": "",
        "base_sha": "",
        "commits": [],
        "rejection_reason": "",
        "approval_status": "",
        "merge_base": "",
        "merge_tip": "",
    }

    path = _task_path(td, task_id)
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))

    from delegate.chat import log_event
    log_event(hc_home, f"{format_task_id(task_id)} created \u2014 {title}")

    return task


def get_task(hc_home: Path, task_id: int) -> dict:
    """Load a single task by ID."""
    td = _tasks_dir(hc_home)
    path = _task_path(td, task_id)
    if not path.exists():
        raise FileNotFoundError(f"Task {task_id} not found at {path}")
    task = yaml.safe_load(path.read_text())
    task.setdefault("dri", task.get("assignee", ""))  # backfill: old tasks without dri
    task.setdefault("repo", "")
    task.setdefault("branch", "")
    task.setdefault("commits", [])
    task.setdefault("base_sha", "")
    task.setdefault("rejection_reason", "")
    task.setdefault("approval_status", "")
    task.setdefault("tags", [])
    task.setdefault("merge_base", "")
    task.setdefault("merge_tip", "")
    return task


def update_task(hc_home: Path, task_id: int, **updates) -> dict:
    """Update fields on an existing task. Returns the updated task."""
    task = get_task(hc_home, task_id)

    for key, value in updates.items():
        if key not in task:
            raise ValueError(f"Unknown task field: '{key}'")
        task[key] = value

    task["updated_at"] = _now()
    td = _tasks_dir(hc_home)
    path = _task_path(td, task_id)
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))
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
    # if they're still empty.  This catches cases where the worktree was
    # created before the metadata-saving logic ran (e.g. agent restart).
    if status in ("review", "needs_merge"):
        _backfill_branch_metadata(hc_home, old_task, updates)

    task = update_task(hc_home, task_id, **updates)

    new_status = status.replace("_", " ").title()
    from delegate.chat import log_event
    log_event(hc_home, f"{format_task_id(task_id)} {old_status} \u2192 {new_status}")

    return task


def set_task_branch(hc_home: Path, task_id: int, branch_name: str) -> dict:
    """Set the branch name on a task."""
    task = get_task(hc_home, task_id)
    task["branch"] = branch_name
    task["updated_at"] = _now()
    td = _tasks_dir(hc_home)
    path = _task_path(td, task_id)
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))
    return task


def add_task_commit(hc_home: Path, task_id: int, commit_sha: str) -> dict:
    """Append a commit SHA to the task's commits list."""
    task = get_task(hc_home, task_id)
    if commit_sha not in task["commits"]:
        task["commits"].append(commit_sha)
    task["updated_at"] = _now()
    td = _tasks_dir(hc_home)
    path = _task_path(td, task_id)
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))
    return task


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

    *tag* filters to tasks whose ``tags`` list contains the given value.
    """
    td = _tasks_dir(hc_home)
    tasks = []

    for f in sorted(td.glob("T*.yaml")):
        task = yaml.safe_load(f.read_text())
        if status and task.get("status") != status:
            continue
        if assignee and task.get("assignee") != assignee:
            continue
        if project and task.get("project") != project:
            continue
        if tag and tag not in task.get("tags", []):
            continue
        tasks.append(task)

    return tasks


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
        print(yaml.dump(task, default_flow_style=False, sort_keys=False))


if __name__ == "__main__":
    main()
