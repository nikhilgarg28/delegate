"""File-based task management — global across all teams.

Tasks are stored as individual YAML files under ``~/.headcount/tasks/``.

Usage:
    python -m headcount.task create <home> --title "Build API" [--priority high]
    python -m headcount.task list <home> [--status open] [--assignee alice]
    python -m headcount.task update <home> <task_id> [--title ...] [--description ...] [--priority ...]
    python -m headcount.task assign <home> <task_id> <assignee>
    python -m headcount.task reviewer <home> <task_id> <reviewer>
    python -m headcount.task status <home> <task_id> <status>
    python -m headcount.task show <home> <task_id>
"""

import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from headcount.paths import tasks_dir as _resolve_tasks_dir


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


def _task_path(td: Path, task_id: int) -> Path:
    return td / f"T{task_id:04d}.yaml"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def create_task(
    hc_home: Path,
    title: str,
    description: str = "",
    project: str = "",
    priority: str = "medium",
    reviewer: str = "",
    depends_on: list[int] | None = None,
    repo: str = "",
) -> dict:
    """Create a new task. Returns the task dict with assigned ID."""
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
        "assignee": "",
        "reviewer": reviewer,
        "project": project,
        "priority": priority,
        "repo": repo,
        "created_at": now,
        "updated_at": now,
        "completed_at": "",
        "depends_on": depends_on or [],
        "branch": "",
        "commits": [],
        "rejection_reason": "",
        "approval_status": "",
    }

    path = _task_path(td, task_id)
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))

    from headcount.chat import log_event
    log_event(hc_home, f"Created T{task_id:04d}: {title}")

    return task


def get_task(hc_home: Path, task_id: int) -> dict:
    """Load a single task by ID."""
    td = _tasks_dir(hc_home)
    path = _task_path(td, task_id)
    if not path.exists():
        raise FileNotFoundError(f"Task {task_id} not found at {path}")
    task = yaml.safe_load(path.read_text())
    task.setdefault("reviewer", "")
    task.setdefault("repo", "")
    task.setdefault("branch", "")
    task.setdefault("commits", [])
    task.setdefault("rejection_reason", "")
    task.setdefault("approval_status", "")
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
    """Assign a task to an agent."""
    task = update_task(hc_home, task_id, assignee=assignee)

    from headcount.chat import log_event
    log_event(hc_home, f"T{task_id:04d} assigned to {assignee.capitalize()}")

    return task


def set_reviewer(hc_home: Path, task_id: int, reviewer: str) -> dict:
    """Set the reviewer for a task."""
    task = update_task(hc_home, task_id, reviewer=reviewer)

    from headcount.chat import log_event
    log_event(hc_home, f"T{task_id:04d} reviewer set to {reviewer.capitalize()}")

    return task


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
    task = update_task(hc_home, task_id, **updates)

    new_status = status.replace("_", " ").title()
    from headcount.chat import log_event
    log_event(hc_home, f"Status of T{task_id:04d} changed from {old_status} \u2192 {new_status}")

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

    If the task has a ``repo`` field, diffs are run against the repo's full
    clone in ``~/.headcount/repos/<repo>/``.  Otherwise falls back to
    ``hc_home`` as the git working directory.

    Uses three-dot diff against main. Falls back to git log + git show
    if main doesn't exist.
    """
    task = get_task(hc_home, task_id)
    branch = task.get("branch", "")
    if not branch:
        return "(no branch set)"

    # Determine git cwd — prefer the repo clone if set
    repo_name = task.get("repo", "")
    if repo_name:
        from headcount.paths import repo_path as _repo_path
        git_cwd = str(_repo_path(hc_home, repo_name))
    else:
        git_cwd = str(hc_home)

    # Try three-dot diff against main
    try:
        result = subprocess.run(
            ["git", "diff", f"main...{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=git_cwd,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: use git log to find commits, then git show
    try:
        log_result = subprocess.run(
            ["git", "log", "--oneline", branch],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=git_cwd,
        )
        if log_result.returncode == 0 and log_result.stdout.strip():
            lines = log_result.stdout.strip().splitlines()
            if len(lines) >= 2:
                last_commit = lines[0].split()[0]
                first_commit = lines[-1].split()[0]
                show_result = subprocess.run(
                    ["git", "show", f"{first_commit}..{last_commit}"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=git_cwd,
                )
                if show_result.returncode == 0:
                    return show_result.stdout
            elif len(lines) == 1:
                sha = lines[0].split()[0]
                show_result = subprocess.run(
                    ["git", "show", sha],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=git_cwd,
                )
                if show_result.returncode == 0:
                    return show_result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "(no diff available)"


def list_tasks(
    hc_home: Path,
    status: str | None = None,
    assignee: str | None = None,
    project: str | None = None,
) -> list[dict]:
    """List tasks with optional filters."""
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
    p_create.add_argument("--reviewer", default="")
    p_create.add_argument("--repo", default="", help="Registered repo name for this task")

    # list
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("home", type=Path)
    p_list.add_argument("--status", choices=VALID_STATUSES)
    p_list.add_argument("--assignee")
    p_list.add_argument("--project")

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

    # reviewer
    p_reviewer = sub.add_parser("reviewer", help="Set task reviewer")
    p_reviewer.add_argument("home", type=Path)
    p_reviewer.add_argument("task_id", type=int)
    p_reviewer.add_argument("reviewer_name")

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
            reviewer=args.reviewer,
            repo=args.repo,
        )
        print(f"Created T{task['id']:04d}: {task['title']}")

    elif args.command == "list":
        tasks = list_tasks(
            args.home,
            status=args.status,
            assignee=args.assignee,
            project=args.project,
        )
        for t in tasks:
            assignee = f" [{t['assignee']}]" if t["assignee"] else ""
            print(f"  T{t['id']:04d} ({t['status']}) {t['title']}{assignee}")
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
        print(f"Updated T{task['id']:04d}")

    elif args.command == "assign":
        task = assign_task(args.home, args.task_id, args.assignee)
        print(f"Assigned T{task['id']:04d} to {args.assignee}")

    elif args.command == "reviewer":
        task = set_reviewer(args.home, args.task_id, args.reviewer_name)
        print(f"T{task['id']:04d} reviewer -> {args.reviewer_name}")

    elif args.command == "status":
        task = change_status(args.home, args.task_id, args.new_status)
        print(f"T{task['id']:04d} -> {args.new_status}")

    elif args.command == "show":
        task = get_task(args.home, args.task_id)
        print(yaml.dump(task, default_flow_style=False, sort_keys=False))


if __name__ == "__main__":
    main()
