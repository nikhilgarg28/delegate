# Code Review

## Branches

All work happens on feature branches in git worktrees. Branch naming convention:

```
<dri>/T<NNNN>
```

No direct pushes to main.

## Merge Flow

Agents don't merge their own branches. To merge:

1. Agent sets the task status to `review`.
2. Agent sends a review request to QA: `REVIEW_REQUEST: repo=<repo_name> branch=<branch>`
3. QA creates a worktree, runs a quick smoke test — does it start, does the happy path work?
4. If approved: task moves to `needs_merge`. Auto-merge or boss approval depending on repo settings.
5. Merge worker rebases onto main, runs tests, fast-forward merges. On conflict, manager is notified.

## Review Standards

Reviews should be fast — under 10 minutes for most changes. Focus on correctness, not style. If it works and isn't actively harmful, approve it.

Don't block a merge for:
- Style nits or formatting preferences
- Missing tests on non-critical code
- "I would have done it differently" opinions
- Minor naming choices

**Do block for:**
- Bugs that affect users
- Security issues
- Breaking changes to shared APIs without migration
- Data loss risks

## Review Focus

Reviewers focus on two things:

1. **Correctness** — does it do what it claims?
2. **Safety** — could this break something important or lose data?

## Turnaround

Keep review turnaround under 10 minutes. If you can't review immediately, let the author know so they can find someone else.
