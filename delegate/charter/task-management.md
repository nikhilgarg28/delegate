# Task Management

## Scoping & Focus

Tasks should be scoped to roughly half a day of work. If bigger, break it down first. One task at a time — finish what you started before picking up something new.

## Commands

```
python -m delegate.task create <home> --title "..." [--description "..."] [--repo <name>] [--priority high] [--depends-on 1,2]
python -m delegate.task list <home> [--status todo] [--assignee <name>]
python -m delegate.task show <home> <task_id>
python -m delegate.task assign <home> <task_id> <assignee>
python -m delegate.task status <home> <task_id> <new_status>
python -m delegate.task attach <home> <task_id> <file_path>
python -m delegate.task detach <home> <task_id> <file_path>
```

Statuses: `todo` → `in_progress` → `in_review` → `in_approval` → `merging` → `done`. Also: `rejected` (→ `in_progress`), `conflict` (→ `in_progress`).

Tasks are stored per-team in SQLite. Associate with one or more repos using `--repo` (repeatable for multi-repo tasks).

## DRI and Assignee

Each task has two ownership fields:

- **DRI** (Directly Responsible Individual) — set automatically on first assignment, never changes. The branch name is derived from the team (`delegate/<team_id>/<team>/T<NNNN>`).
- **Assignee** — who currently owns the ball. The manager updates this as the task moves through stages (e.g., author → reviewer → boss for approval).

The boss's "Action Queue" in the UI shows tasks where the boss is the current assignee.

## Workflow

1. Manager creates and assigns task. First assignment sets the DRI.
2. Agent sets `in_progress`. If task has repos, a git worktree is created in each repo with `base_sha` recorded per-repo.
3. Agent completes → sets `in_review`. Manager reassigns to the reviewer.
4. Reviewer reviews diff (base_sha → branch tip), runs tests, checks quality.
5. Reviewer approves → `in_approval`. Manager reassigns to boss. Reviewer rejects → back to `in_progress`, manager reassigns to DRI with feedback.
6. Boss approves (manual repos) or auto-merge (auto repos). Task transitions to `merging`.
7. Merge worker rebases onto main in each repo, runs pre-merge script, fast-forward merges.
8. Task becomes `done` (successful merge) or `conflict` (rebase/test failure).

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

## Completion

Write a summary: what you built, decisions made, anything the next person should know.
