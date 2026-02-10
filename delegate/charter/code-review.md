# Code Review

## Workspace Isolation

Each agent works in their own git worktree, created automatically for tasks with a registered repo. Worktrees live in `~/.delegate/teams/<team>/agents/<agent>/worktrees/<repo>-T<NNNN>/`. Registered repos are symlinks in `~/.delegate/repos/` pointing to the real local repo. No clones — worktrees are created directly against the local repo.

## Branches

All work on feature branches: `<dri>/T<NNNN>` (e.g., `alice/T0012`). The branch name is derived from the DRI (Directly Responsible Individual), not the current assignee, so it stays stable even when the task is reassigned for review. No direct pushes to main.

## Merge Flow

1. Agent completes work → sets task to `review`. Manager reassigns to the reviewer.
2. Reviewer reviews diff (base_sha → branch tip), runs tests, checks quality.
3. Approved → `needs_merge`, manager reassigns to boss. Rejected → `in_progress`, manager reassigns to DRI with feedback.
4. Boss approves (manual) or auto-merge (auto repos).
5. Merge worker rebases onto main, runs tests. Conflicts → task becomes `conflict`, manager notified.
6. Clean rebase + tests pass → fast-forward merge → `merged`, worktree cleaned up.

## Review Standards

Your approval means "this is correct, readable, tested, and consistent." Don't approve code with known bugs — every known issue is blocking. Actually test the code: check out the branch, run it, verify behavior, trigger edge cases. Don't just read the diff.

## Review Focus

1. **Correctness** — does it work? Did you verify?
2. **Readability** — understandable without author explaining?
3. **Test coverage** — important paths tested?
4. **Consistency** — matches specs and conventions?

Review turnaround: under 30 minutes when possible. Raise concerns as specific questions with suggested alternatives.
