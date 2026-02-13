"""Standard software development workflow.

This is the default workflow shipped with Delegate.  It replicates the
original hardcoded task lifecycle:

    todo → in_progress → in_review → in_approval → merging → done

With branches for rejection (→ in_progress), merge failure
(→ merge_failed → in_progress), and cancellation from any stage.

Usage:
    Register for a team automatically on ``team add`` or manually::

        delegate workflow add myteam delegate/workflows/standard.py
"""

from delegate.workflow import Stage, workflow


# ── Stages ────────────────────────────────────────────────────

class Todo(Stage):
    """Task has been created but work has not started."""

    label = "To Do"
    _transitions = {"in_progress", "cancelled"}


class InProgress(Stage):
    """An engineer is actively working on the task."""

    label = "In Progress"
    _transitions = {"in_review", "cancelled"}

    def assign(self, ctx):
        # If the task already has an assignee, keep them (e.g. rework
        # after rejection).  Otherwise, pick the least-loaded engineer.
        if ctx.task.get("assignee"):
            return ctx.task.assignee
        return ctx.pick(role="engineer")

    def enter(self, ctx):
        # Set up worktrees for all repos on the task (idempotent).
        repos = ctx.task.get("repo", [])
        if repos:
            ctx.setup_worktree()


class InReview(Stage):
    """A peer reviewer is reviewing the code changes."""

    label = "In Review"
    _transitions = {"in_approval", "in_progress", "cancelled"}

    def enter(self, ctx):
        # Gate: worktree must be clean and branch must have commits.
        ctx.require_clean_worktree()
        ctx.require_commits()

    def assign(self, ctx):
        # Assign to a different engineer than the DRI.
        dri = ctx.task.get("dri", "")
        return ctx.pick(role="engineer", exclude=dri)


class InApproval(Stage):
    """Waiting for human approval before merging."""

    label = "In Approval"
    _transitions = {"merging", "rejected", "cancelled"}

    def enter(self, ctx):
        # Create a review record for the human.
        ctx.create_review(reviewer=ctx.human)

    def assign(self, ctx):
        return ctx.human


class Merging(Stage):
    """Automated merge in progress (rebase, test, fast-forward)."""

    label = "Merging"
    auto = True
    _transitions = {"done", "merge_failed", "cancelled"}

    def action(self, ctx):
        result = ctx.merge()
        if result.success:
            ctx.log(f"T{ctx.task.id:04d} merged successfully")
            # Clean up worktrees
            ctx.teardown_worktree()
            return Done

        attempts = ctx.task.get("merge_attempts", 0) + 1
        ctx.task.update(merge_attempts=attempts)

        if result.retryable and attempts < 3:
            ctx.log(f"T{ctx.task.id:04d} merge retry {attempts}/3: {result.message}")
            return Merging  # retry

        # Non-retryable or exhausted retries → merge_failed
        ctx.notify(
            ctx.manager,
            f"MERGE_FAILED: T{ctx.task.id:04d}\n"
            f"Reason: {result.message}\n"
            f"Attempts: {attempts}",
        )
        return MergeFailed


class Done(Stage):
    """Task completed and merged."""

    label = "Done"
    terminal = True

    def enter(self, ctx):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ctx.task.update(completed_at=now)


class Rejected(Stage):
    """Task was rejected by the human during approval."""

    label = "Rejected"

    # _transitions set explicitly: can go back to in_progress or be cancelled
    _transitions = {"in_progress", "cancelled"}

    def enter(self, ctx):
        # Notify the manager about the rejection.
        reason = ctx.task.get("rejection_reason", "(no reason)")
        ctx.notify(
            ctx.manager,
            f"TASK_REJECTED: T{ctx.task.id:04d}\n"
            f"Reason: {reason}\n"
            f"Action: rework or reassign",
        )


class MergeFailed(Stage):
    """Automated merge failed (conflict, test failure, etc.)."""

    label = "Merge Failed"

    # Can retry merge or send back for rework
    _transitions = {"merging", "in_progress", "cancelled"}

    def assign(self, ctx):
        return ctx.manager


class Cancelled(Stage):
    """Task was cancelled — no further transitions."""

    label = "Cancelled"
    terminal = True

    def enter(self, ctx):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ctx.task.update(completed_at=now, assignee="")
        # Best-effort worktree cleanup
        try:
            ctx.teardown_worktree()
        except Exception:
            pass


class Error(Stage):
    """Task entered an error state due to an unrecoverable action failure.

    Assigned to the human for manual resolution.
    """

    label = "Error"

    # Can be retried from any preceding stage or cancelled
    _transitions = {"todo", "in_progress", "cancelled"}

    def assign(self, ctx):
        return ctx.human


# ── Workflow registration ─────────────────────────────────────

@workflow(name="standard", version=1)
def standard():
    return [
        Todo,
        InProgress,
        InReview,
        InApproval,
        Merging,
        Done,
        Rejected,
        MergeFailed,
        Cancelled,
        Error,
    ]
