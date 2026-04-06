"""Research workflow — iterative experimentation lifecycle.

This workflow is designed for autonomous research tasks where an agent
runs iterative experiments (modify code → run → evaluate → keep/discard).

Unlike the default software development workflow, research tasks skip
the review/merge pipeline entirely.  The researcher works autonomously
in a worktree, and results are reviewed by a human when ready.

Lifecycle:

    todo → researching → reporting → done
                ↕
              paused

With cancellation possible from any non-terminal stage.
The researcher auto-continues while in ``researching``.  Moving the
task to ``paused`` triggers a graceful wrap-up (progress documentation)
before the researcher goes idle.  Resuming moves back to ``researching``.

Usage:
    Register for a team::

        delegate workflow add myteam delegate/workflows/research.py
"""

from datetime import datetime, timezone

from delegate.workflow import Stage, workflow

# Import git mixin so ctx gets git methods
import delegate.workflows.git  # noqa: F401


# ── Stages ────────────────────────────────────────────────────

class Todo(Stage):
    """Research task has been created but work has not started."""

    label = "To Do"
    _transitions = {"researching", "cancelled"}


class Researching(Stage):
    """Researcher is actively running experiments."""

    label = "Researching"
    _transitions = {"reporting", "paused", "cancelled"}

    def assign(self, ctx):
        # Assign to the DRI (original researcher) if set, otherwise pick one.
        dri = ctx.task.get("dri")
        if dri:
            return dri
        return ctx.pick(role="researcher")

    def enter(self, ctx):
        # Set up worktrees for all repos on the task (idempotent).
        repos = ctx.task.get("repo", [])
        if repos:
            ctx.setup_worktree()
        # Create persistent artifacts directory (survives worktree teardown).
        ctx.setup_artifacts()


class Paused(Stage):
    """Research paused — researcher will document progress and stop.

    A non-terminal stage that halts auto-continuation.  The researcher
    gets one final wrap-up turn to document results, then goes idle
    until the human resumes (→ researching) or closes out
    (→ done / cancelled).
    """

    label = "Paused"
    _transitions = {"researching", "done", "cancelled"}

    def assign(self, ctx):
        # Keep current assignee so the researcher gets the wrap-up turn.
        return ctx.task.get("assignee") or ctx.pick(role="researcher")


class Reporting(Stage):
    """Research complete — results ready for human review."""

    label = "Reporting"
    _transitions = {"done", "researching", "cancelled"}

    def enter(self, ctx):
        # Build notification with inline results summary if available.
        title = ctx.task.get("title", "(untitled)")
        body = f"Research results ready for T{ctx.task.id:04d}: {title}\n"

        results = ctx.get_metadata("results")
        if results and isinstance(results, dict):
            summary = results.get("summary", "")
            total = results.get("total_experiments", "?")
            kept = results.get("kept", "?")
            baseline = results.get("baseline", {})
            best = results.get("best", {})
            if baseline and best:
                metric = baseline.get("metric", "metric")
                body += (
                    f"\n{metric}: {baseline.get('value', '?')} → "
                    f"{best.get('value', '?')} "
                    f"({total} experiments, {kept} kept)\n"
                )
            if summary:
                body += f"\n{summary}\n"
        else:
            body += "Please review the experiment log and results.\n"

        ctx.notify(ctx.human, body)

    def assign(self, ctx):
        return ctx.human


class Done(Stage):
    """Research task completed and reviewed."""

    label = "Done"
    terminal = True

    def enter(self, ctx):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ctx.task.update(completed_at=now)
        # Best-effort worktree cleanup
        try:
            ctx.teardown_worktree()
        except Exception:
            pass


class Cancelled(Stage):
    """Research task was cancelled."""

    label = "Cancelled"
    terminal = True

    def enter(self, ctx):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ctx.task.update(completed_at=now, assignee="")
        try:
            ctx.teardown_worktree()
        except Exception:
            pass


# ── Workflow registration ─────────────────────────────────────

@workflow(name="research", version=1)
def research():
    return [
        Todo,
        Researching,
        Paused,
        Reporting,
        Done,
        Cancelled,
    ]
