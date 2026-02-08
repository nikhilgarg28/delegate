# Code Review

## Workspace Isolation

Each agent works in their own clone of the project repository, located in their workspace directory. This ensures agents never interfere with each other's uncommitted changes.

When starting a task, the agent should:

1. Ensure they have a clone of the project repo in their workspace. If not, clone it from the origin path provided in the task description.
2. Fetch the latest from origin: `git fetch origin`
3. Check out `main` and pull: `git checkout main && git pull origin main`
4. Create a feature branch from main.

When done, push the branch to origin so QA can review it.

## Branches

All work happens on feature branches. Branch naming convention:

```
<agent>/<project>/<task-number>-<short-name>
```

For example: `alice/backend/001-api-spec` or `bob/frontend/003-login-ui`.

No direct pushes to main.

## Merge Flow

Agents don't merge their own branches. To merge:

1. Agent sets the task status to `review`.
2. Agent sends a review request to QA: `REVIEW_REQUEST: repo=<path> branch=<branch>`
3. QA checks out the branch in their own clone, runs tests, verifies quality.
4. QA merges to main and pushes, then sends `MERGED` acknowledgment, or sends `REJECTED` with reason.

## Review Standards

The reviewer holds the line on code quality. Your approval means "I am confident this is correct, readable, tested, and consistent with our specs." If you wouldn't be comfortable maintaining this code yourself, don't approve it.

**Do not approve code with known bugs.** If you find a bug — even a minor one — it must be fixed before merge. Noting a bug as "non-blocking" and approving anyway is not acceptable. Every known issue is blocking until it's resolved. The purpose of review is to catch problems *before* they reach main, not to document them for later.

**Actually test the code.** Don't just read the diff. Check out the branch, run it, and verify the behavior matches the task requirements. Click the buttons, trigger the edge cases, check the error paths. If you can't verify it works, you can't approve it.

If the author and reviewer genuinely disagree and can't resolve it between themselves, escalate to the project DRI. The DRI makes the final call.

## Review Focus

Reviewers focus on four things:

1. **Correctness** — does it do what it claims? Did you actually run it and verify?
2. **Readability** — can I understand this without the author explaining it?
3. **Test coverage** — are the important business-logic paths tested?
4. **Consistency** — does this match documented specs and conventions?

## Turnaround

Keep review turnaround under 30 minutes when possible. Quick feedback loops keep the team moving. If you can't review within 30 minutes, let the author know so they can context-switch rather than wait.

## Feedback Style

When you have concerns, raise them as specific questions or suggestions, not vague reactions. "What happens if this input is empty?" is useful feedback. "I don't like this approach" is not — say why, and suggest an alternative.
