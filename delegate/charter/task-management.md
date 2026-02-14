# Task Management

## Scoping & Focus

Tasks should be scoped to roughly half a day of work. If bigger, break it down first. One task at a time — finish what you started before picking up something new.

## Commands

```
python -m delegate.task create <home> --title "..." [--description "..."] [--repo <name>] [--priority high] [--depends-on 1,2]
python -m delegate.task list <home> [--status todo] [--assignee <name>]
python -m delegate.task show <home> <task_id>
python -m delegate.task assign <home> <task_id> <assignee>
python -m delegate.task status <home> <task_id> <new_status> [--assignee <name>]
python -m delegate.task attach <home> <task_id> <file_path>
python -m delegate.task detach <home> <task_id> <file_path>
python -m delegate.task comment <home> <team> <task_id> <your_name> "<body>"
python -m delegate.task cancel <home> <team> <task_id>
```

Statuses: `todo` → `in_progress` → `in_review` → `in_approval` → `merging` → `done`. Also: `rejected` (→ `in_progress`), `merge_failed` (→ `in_progress` or retry → `in_approval`), `cancelled` (terminal — a human member can cancel from any non-terminal state).

Tasks are stored per-team in SQLite. Associate with one or more repos using `--repo` (repeatable for multi-repo tasks).

**Combined status + assignee changes**: When changing both status and assignee together (e.g., moving to `in_review` and reassigning to a reviewer), use the `--assignee` flag on `task status` to generate a single combined event instead of two separate events:
```
python -m delegate.task status <home> <team> <task_id> in_review --assignee john
```
This produces one event like "T0001: In Progress -> In Review, assigned to john" rather than two separate status and assignment events.

## DRI and Assignee

Each task has two ownership fields:

- **DRI** (Directly Responsible Individual) — set automatically on first assignment, never changes. The branch name is derived from the team (`delegate/<team_id>/<team>/T<NNNN>`).
- **Assignee** — who currently owns the ball. The manager updates this as the task moves through stages (e.g., author → reviewer → human for approval).

The human's "Action Queue" in the UI shows tasks where they are the current assignee.

## Workflow

1. Manager creates and assigns task. First assignment sets the DRI.
2. Agent sets `in_progress`. If task has repos, a git worktree is created in each repo with `base_sha` recorded per-repo.
3. Agent completes → sets `in_review`. Manager reassigns to the reviewer.
4. Reviewer reviews diff (base_sha → branch tip), runs tests, checks quality.
5. Reviewer approves → `in_approval`. Manager reassigns to human. Reviewer rejects → back to `in_progress`, manager reassigns to DRI with feedback.
6. Human approves (manual repos) or auto-merge (auto repos). Task transitions to `merging`.
7. Merge worker attempts rebase onto main in each repo. If rebase conflicts, it falls back to squash-reapply (applying the total diff as one commit). Then runs pre-merge script, fast-forward merges.
8. Task becomes `done` (successful merge) or `merge_failed` (true content conflict or test failure). Transient failures are retried automatically up to 3 times before escalating to the manager. On content conflicts, the manager receives detailed hunk context and `git reset --soft` resolution instructions for the DRI.

## Attachments

Attach relevant files to tasks — specs, design mockups, screenshots, reference docs. Typically from `shared/` or agent workspace.

```
python -m delegate.task attach <home> <task_id> <file_path>
python -m delegate.task detach <home> <task_id> <file_path>
```

Attach early: specs before work starts, screenshots/previews during review. Attachments are visible in the task detail panel in the UI.

## Dependencies

Specify with `--depends-on <ids>`. A task with incomplete dependencies must NOT be assigned. When a task completes, check if blocked tasks are now unblocked.

## Blockers

Message the manager immediately. Don't spend more than 15 minutes stuck before raising it.

## Task Comments

Use task comments to record durable information on a task. Comments are visible
to all agents in the task prompt and in the UI timeline.

When to add a comment:
- **Before starting work**: capture clarifications, scope decisions, or follow-up specs.
- **During work**: record findings, bugs, design decisions, or technical notes.
- **When blocked**: explain what you're stuck on and what you've tried.
- **When submitting for review**: summarize what was done and key decisions.
- **When attaching files**: explain what was attached and why.

Do NOT repeat task comments in messages. Instead, add a comment and send a brief
message referencing the task (e.g., "Added specs to T0003 comments").

## Cancellation

If the manager tells you a task has been cancelled:
1. Stop any work on it immediately.
2. Run `python -m delegate.task cancel <home> <team> <task_id>` to clean
   up worktrees and branches. This is safe to run multiple times — it
   re-runs cleanup idempotently in case branches or directories were
   recreated.
3. Acknowledge the cancellation briefly to the manager.

Only the manager cancels tasks, and only when a human member requests it.

## Completion

Write a summary: what you built, decisions made, anything the next person should know.
