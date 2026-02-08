"""File-based task and project management.

Tasks are stored as individual YAML files under .standup/tasks/.

Usage:
    python scripts/task.py create <root> --title "Build API" [--project myproject] [--priority high]
    python scripts/task.py list <root> [--status open] [--assignee alice] [--project myproject]
    python scripts/task.py update <root> <task_id> [--title ...] [--description ...] [--priority ...]
    python scripts/task.py assign <root> <task_id> <assignee>
    python scripts/task.py status <root> <task_id> <status>
    python scripts/task.py show <root> <task_id>
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml


VALID_STATUSES = ("open", "in_progress", "review", "done")
VALID_PRIORITIES = ("low", "medium", "high", "critical")


def _tasks_dir(root: Path) -> Path:
    d = root / ".standup" / "tasks"
    if not d.is_dir():
        raise FileNotFoundError(f"Tasks directory not found: {d}")
    return d


def _next_id(tasks_dir: Path) -> int:
    """Determine the next task ID by scanning existing files."""
    max_id = 0
    for f in tasks_dir.glob("T*.yaml"):
        try:
            num = int(f.stem[1:])
            max_id = max(max_id, num)
        except (IndexError, ValueError):
            continue
    return max_id + 1


def _task_path(tasks_dir: Path, task_id: int) -> Path:
    return tasks_dir / f"T{task_id:04d}.yaml"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def create_task(
    root: Path,
    title: str,
    description: str = "",
    project: str = "",
    priority: str = "medium",
    depends_on: list[int] | None = None,
) -> dict:
    """Create a new task. Returns the task dict with assigned ID."""
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority '{priority}'. Must be one of: {VALID_PRIORITIES}")

    tasks_dir = _tasks_dir(root)
    task_id = _next_id(tasks_dir)
    now = _now()

    task = {
        "id": task_id,
        "title": title,
        "description": description,
        "status": "open",
        "assignee": "",
        "project": project,
        "priority": priority,
        "created_at": now,
        "updated_at": now,
        "completed_at": "",
        "depends_on": depends_on or [],
    }

    path = _task_path(tasks_dir, task_id)
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))

    from scripts.chat import log_event
    log_event(root, f"Task created: T{task_id:04d} '{title}' (project={project or 'none'}, priority={priority.capitalize()})")

    return task


def get_task(root: Path, task_id: int) -> dict:
    """Load a single task by ID."""
    tasks_dir = _tasks_dir(root)
    path = _task_path(tasks_dir, task_id)
    if not path.exists():
        raise FileNotFoundError(f"Task {task_id} not found at {path}")
    return yaml.safe_load(path.read_text())


def update_task(root: Path, task_id: int, **updates) -> dict:
    """Update fields on an existing task. Returns the updated task."""
    task = get_task(root, task_id)

    for key, value in updates.items():
        if key not in task:
            raise ValueError(f"Unknown task field: '{key}'")
        task[key] = value

    task["updated_at"] = _now()
    tasks_dir = _tasks_dir(root)
    path = _task_path(tasks_dir, task_id)
    path.write_text(yaml.dump(task, default_flow_style=False, sort_keys=False))
    return task


def assign_task(root: Path, task_id: int, assignee: str) -> dict:
    """Assign a task to an agent."""
    task = update_task(root, task_id, assignee=assignee)

    from scripts.chat import log_event
    log_event(root, f"Task T{task_id:04d} assigned to {assignee.capitalize()}")

    return task


def change_status(root: Path, task_id: int, status: str) -> dict:
    """Change task status. Sets completed_at when moving to 'done'."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {VALID_STATUSES}")
    updates: dict = {"status": status}
    if status == "done":
        updates["completed_at"] = _now()
    task = update_task(root, task_id, **updates)

    from scripts.chat import log_event
    log_event(root, f"Task T{task_id:04d} status → {status.replace('_', ' ').title()}")

    return task


def list_tasks(
    root: Path,
    status: str | None = None,
    assignee: str | None = None,
    project: str | None = None,
) -> list[dict]:
    """List tasks with optional filters."""
    tasks_dir = _tasks_dir(root)
    tasks = []

    for f in sorted(tasks_dir.glob("T*.yaml")):
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
    p_create.add_argument("root", type=Path)
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--description", default="")
    p_create.add_argument("--project", default="")
    p_create.add_argument("--priority", default="medium", choices=VALID_PRIORITIES)

    # list
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("root", type=Path)
    p_list.add_argument("--status", choices=VALID_STATUSES)
    p_list.add_argument("--assignee")
    p_list.add_argument("--project")

    # update
    p_update = sub.add_parser("update", help="Update a task")
    p_update.add_argument("root", type=Path)
    p_update.add_argument("task_id", type=int)
    p_update.add_argument("--title")
    p_update.add_argument("--description")
    p_update.add_argument("--priority", choices=VALID_PRIORITIES)

    # assign
    p_assign = sub.add_parser("assign", help="Assign a task")
    p_assign.add_argument("root", type=Path)
    p_assign.add_argument("task_id", type=int)
    p_assign.add_argument("assignee")

    # status
    p_status = sub.add_parser("status", help="Change task status")
    p_status.add_argument("root", type=Path)
    p_status.add_argument("task_id", type=int)
    p_status.add_argument("new_status", choices=VALID_STATUSES)

    # show
    p_show = sub.add_parser("show", help="Show a task")
    p_show.add_argument("root", type=Path)
    p_show.add_argument("task_id", type=int)

    args = parser.parse_args()

    if args.command == "create":
        task = create_task(
            args.root,
            title=args.title,
            description=args.description,
            project=args.project,
            priority=args.priority,
        )
        print(f"Created T{task['id']:04d}: {task['title']}")

    elif args.command == "list":
        tasks = list_tasks(
            args.root,
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
        task = update_task(args.root, args.task_id, **updates)
        print(f"Updated T{task['id']:04d}")

    elif args.command == "assign":
        task = assign_task(args.root, args.task_id, args.assignee)
        print(f"Assigned T{task['id']:04d} to {args.assignee}")

    elif args.command == "status":
        task = change_status(args.root, args.task_id, args.new_status)
        print(f"T{task['id']:04d} -> {args.new_status}")

    elif args.command == "show":
        task = get_task(args.root, args.task_id)
        print(yaml.dump(task, default_flow_style=False, sort_keys=False))


if __name__ == "__main__":
    main()
