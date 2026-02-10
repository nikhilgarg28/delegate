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
3. QA creates a worktree from the repo, reviews only the diff between `base_sha` and branch tip.
4. QA runs the full test suite, linting (ruff), and type checking (pyright/mypy) — zero violations allowed.
5. QA verifies test coverage has not decreased.
6. If approved: task moves to `needs_merge`. Boss gives final approval (or auto-merge for auto-approval repos).
7. Merge worker rebases onto main, runs tests, then fast-forward merges. On conflict or test failure, task goes to `conflict` and manager is notified.

## Review Standards

The reviewer is the last line of defense before code reaches main. Your approval means "I have read every line, I understand the intent, I have verified it works, and I am confident this code is correct, tested, and maintainable."

**Do not approve code with any known issues.** Every concern is blocking. If you see something that could be a problem — even if you're not sure — raise it and require an answer before approving.

**Actually test the code thoroughly.** Check out the branch, run the full test suite, and manually verify behavior. Test the happy path, the error paths, the edge cases, and the boundary conditions. If you can't verify it works, you can't approve it.

**Verify test coverage.** Every new function must have corresponding tests. Every bug fix must include a regression test. If tests are missing, the review is automatically rejected.

If the author and reviewer disagree, escalate to the project DRI. The DRI makes the final call after hearing both sides.

## Review Focus

Reviewers evaluate five dimensions:

1. **Correctness** — does it do exactly what it claims? Have you verified this by running it?
2. **Readability** — can any team member understand this without the author explaining it?
3. **Test coverage** — is every code path tested? Are edge cases covered?
4. **Consistency** — does this match documented specs, conventions, and existing patterns?
5. **Robustness** — does it handle errors gracefully? Are inputs validated? Are resources cleaned up?

## Turnaround

Keep review turnaround under 30 minutes. Thorough reviews take time — budget for it. If you can't review within 30 minutes, let the author know.

## Feedback Style

When you have concerns, be specific and constructive. Explain what the problem is, why it matters, and suggest an alternative. Every piece of feedback should include a concrete suggestion for improvement.
