# Task Management

## Scoping

Tasks should be scoped to roughly half a day of work. If a task feels bigger than that, break it down into smaller pieces before starting. Smaller tasks are easier to review, easier to unblock, and easier to reason about.

## Focus

One task at a time. Focus on finishing what you started before picking up something new. Partial progress on three tasks is worth less than one completed task.

## Task Commands

```
# Create a task
python -m scripts.task create <root> --title "..." [--description "..."] [--project "..."] [--priority high]

# List tasks
python -m scripts.task list <root> [--status open] [--assignee <name>]

# View a task
python -m scripts.task show <root> <task_id>

# Assign a task
python -m scripts.task assign <root> <task_id> <assignee>

# Update task status
python -m scripts.task status <root> <task_id> <new_status>
```

Valid statuses: `open`, `in_progress`, `review`, `done`.

## Workflow

1. Manager creates a task and assigns it to an agent.
2. Agent sets status to `in_progress` when they start working.
3. Agent works on the task in their workspace.
4. When done, agent sets status to `review` and messages the manager.
5. After review, manager (or QA) sets status to `done`.

## Blockers

When you're blocked, message the manager immediately with a clear description of what's blocking you. Don't spend more than 15 minutes stuck before raising it.

## Dependencies

Dependencies between tasks must be explicit. If your task depends on another, say so when raising it. If you discover a new dependency while working, message the manager.

## Completion

When a task is done, write a summary describing what you built, any decisions you made, and anything the next person should know.
