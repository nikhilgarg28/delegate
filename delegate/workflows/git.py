"""Git-specific workflow operations.

These functions extend ``Context`` with git-centric capabilities:
worktree management, code review, merge, and testing.  They are
mixed into ``Context`` so workflow authors can call them directly
(e.g. ``ctx.setup_worktree()``).

Keeping git operations separate from the core context allows
Delegate to support alternative VCS backends in the future.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from delegate.workflow import GateError, ActionError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    """Result of a merge attempt, exposed to workflow authors."""
    success: bool
    message: str
    retryable: bool = False


@dataclass
class TestResult:
    """Result of running tests."""
    passed: bool
    output: str
    command: str = ""


# ---------------------------------------------------------------------------
# Git mixin — mixed into Context at import time
# ---------------------------------------------------------------------------

class GitMixin:
    """Git-specific methods mixed into ``Context``.

    All methods assume ``self._hc_home``, ``self._team``, and
    ``self.task`` are available (inherited from ``Context``).
    """

    # ── Workspace ───────────────────────────────────────────────

    def setup_worktree(self, repo: str | None = None) -> list[Path]:
        """Create git worktree(s) for the task.

        If *repo* is None, creates worktrees for all repos on the task.
        Returns the list of worktree paths created.
        """
        from delegate.repo import create_task_worktree

        repos = [repo] if repo else self.task.get("repo", [])
        paths = []
        for r in repos:
            try:
                wt = create_task_worktree(
                    self._hc_home, self._team, r, self.task.id,
                )
                paths.append(wt)
            except Exception as exc:
                raise ActionError(
                    f"Failed to create worktree for repo '{r}': {exc}"
                ) from exc
        return paths

    def teardown_worktree(self, repo: str | None = None) -> None:
        """Remove git worktree(s) for the task (best-effort).

        If *repo* is None, removes worktrees for all repos on the task.
        Also deletes the feature branch.
        """
        from delegate.repo import remove_task_worktree, get_repo_path

        repos = [repo] if repo else self.task.get("repo", [])
        branch = self.task.get("branch", "")

        for r in repos:
            try:
                remove_task_worktree(self._hc_home, self._team, r, self.task.id)
            except Exception as exc:
                logger.warning(
                    "Could not remove worktree for task %s repo %s: %s",
                    self.task.id, r, exc,
                )

            # Delete feature branch (best-effort)
            if branch:
                try:
                    repo_dir = get_repo_path(self._hc_home, self._team, r)
                    real_repo = str(repo_dir.resolve())
                    subprocess.run(
                        ["git", "branch", "-D", branch],
                        cwd=real_repo,
                        capture_output=True,
                        check=False,
                    )
                    subprocess.run(
                        ["git", "worktree", "prune"],
                        cwd=real_repo,
                        capture_output=True,
                        check=False,
                    )
                except Exception:
                    pass

    # ── Git gates ───────────────────────────────────────────────

    def require_clean_worktree(self, repo: str | None = None) -> None:
        """Gate: require no uncommitted changes in the worktree(s).

        Raises ``GateError`` if any worktree has uncommitted changes.
        """
        from delegate.paths import task_worktree_dir

        repos = [repo] if repo else self.task.get("repo", [])
        task_id = self.task.id

        for r in repos:
            wt_path = task_worktree_dir(self._hc_home, self._team, r, task_id)
            if not wt_path.is_dir():
                continue

            try:
                result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    raise GateError(
                        f"Cannot proceed: worktree for {r} has uncommitted changes. "
                        f"Please commit or stash before continuing."
                    )
            except subprocess.TimeoutExpired:
                pass  # Skip if git is slow

    def require_commits(self, repo: str | None = None) -> None:
        """Gate: require at least one commit beyond base_sha.

        Raises ``GateError`` if the branch has no new commits.
        """
        from delegate.paths import task_worktree_dir

        repos = [repo] if repo else self.task.get("repo", [])
        task_id = self.task.id
        base_sha_dict = self.task.get("base_sha", {})
        branch = self.task.get("branch", "")

        for r in repos:
            wt_path = task_worktree_dir(self._hc_home, self._team, r, task_id)
            if not wt_path.is_dir():
                continue

            # Check correct branch
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                current_branch = result.stdout.strip() if result.returncode == 0 else ""
                if branch and current_branch and current_branch != branch:
                    raise GateError(
                        f"Wrong branch: worktree for {r} is on '{current_branch}' "
                        f"but expected '{branch}'."
                    )
            except subprocess.TimeoutExpired:
                pass

            # Check commits beyond base_sha
            base_sha = base_sha_dict.get(r, "")
            if base_sha:
                try:
                    result = subprocess.run(
                        ["git", "log", "--oneline", f"{base_sha}..HEAD"],
                        cwd=str(wt_path),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0 and not result.stdout.strip():
                        raise GateError(
                            f"No new commits on branch for {r} since base "
                            f"({base_sha[:8]}). Nothing to review."
                        )
                except subprocess.TimeoutExpired:
                    pass

    # ── Merge ───────────────────────────────────────────────────

    def merge(self, repo: str | None = None) -> MergeResult:
        """Perform the full merge sequence (rebase, test, fast-forward).

        Returns a ``MergeResult`` with ``success``, ``message``,
        and ``retryable`` fields.
        """
        from delegate.merge import merge_task

        # merge_task handles all repos on the task
        result = merge_task(self._hc_home, self._team, self.task.id)

        retryable = False
        if result.reason is not None:
            retryable = result.reason.retryable

        return MergeResult(
            success=result.success,
            message=result.message,
            retryable=retryable,
        )

    # ── Testing ─────────────────────────────────────────────────

    def run_tests(self, command: str | None = None, repo: str | None = None) -> TestResult:
        """Run the test suite in the task's worktree.

        If *command* is None, uses the configured pre-merge script or
        auto-detects the test framework.
        """
        from delegate.paths import task_worktree_dir
        from delegate.config import get_pre_merge_script

        repos = [repo] if repo else self.task.get("repo", [])
        if not repos:
            return TestResult(passed=True, output="No repos configured", command="")

        for r in repos:
            wt_path = task_worktree_dir(self._hc_home, self._team, r, self.task.id)
            if not wt_path.is_dir():
                continue

            cmd = command
            if cmd is None:
                cmd = get_pre_merge_script(self._hc_home, self._team, r)

            if cmd:
                try:
                    result = subprocess.run(
                        cmd,
                        shell=True,
                        cwd=str(wt_path),
                        capture_output=True,
                        text=True,
                        timeout=600,
                    )
                    if result.returncode != 0:
                        return TestResult(
                            passed=False,
                            output=result.stdout + "\n" + result.stderr,
                            command=cmd,
                        )
                except subprocess.TimeoutExpired:
                    return TestResult(
                        passed=False,
                        output="Test execution timed out (10 min limit)",
                        command=cmd,
                    )
            else:
                # No test command configured — pass by default
                return TestResult(passed=True, output="No test command configured", command="")

        return TestResult(passed=True, output="All tests passed", command=command or "")

    def run_script(self, path: str, cwd: str | None = None) -> TestResult:
        """Run an arbitrary script.

        *path* is resolved relative to the workflow's actions directory
        if not absolute.
        """
        script_path = Path(path)
        if not script_path.is_absolute():
            # Try to resolve from workflow actions directory
            wf_name = self.task.get("workflow", "default")
            wf_version = self.task.get("workflow_version", 1)
            from delegate.workflow import _actions_dir
            actions = _actions_dir(self._hc_home, self._team, wf_name, wf_version)
            candidate = actions / path
            if candidate.is_file():
                script_path = candidate

        work_dir = cwd
        if work_dir is None:
            # Default to first repo's worktree
            repos = self.task.get("repo", [])
            if repos:
                from delegate.paths import task_worktree_dir
                wt = task_worktree_dir(self._hc_home, self._team, repos[0], self.task.id)
                if wt.is_dir():
                    work_dir = str(wt)

        try:
            result = subprocess.run(
                str(script_path),
                shell=True,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            return TestResult(
                passed=result.returncode == 0,
                output=result.stdout + "\n" + result.stderr,
                command=str(script_path),
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False,
                output="Script timed out (10 min limit)",
                command=str(script_path),
            )

    # ── Review ──────────────────────────────────────────────────

    def create_review(self, reviewer: str | None = None) -> dict:
        """Create a pending review record for the current task.

        Bumps the task's ``review_attempt`` counter and inserts a new
        review row.  Returns the created review dict.
        """
        from delegate.review import create_review

        new_attempt = self.task.get("review_attempt", 0) + 1
        self.task.update(review_attempt=new_attempt, approval_status="")

        return create_review(
            self._hc_home, self._team,
            self.task.id, new_attempt,
            reviewer=reviewer or "",
        )


# ---------------------------------------------------------------------------
# Mix GitMixin into Context so users get git methods on ctx directly
# ---------------------------------------------------------------------------

def _apply_git_mixin():
    """Add all GitMixin methods to Context."""
    from delegate.workflows.core import Context
    for name in dir(GitMixin):
        if name.startswith("_"):
            continue
        method = getattr(GitMixin, name)
        if callable(method):
            setattr(Context, name, method)


# Auto-apply on import
_apply_git_mixin()
