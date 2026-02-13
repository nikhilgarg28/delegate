"""Workflow context — builtins available to Stage hooks.

The ``Context`` object is passed to every ``enter``, ``exit``,
``assign``, and ``action`` method.  It provides:

- **Task state** — ``ctx.task`` (read), ``ctx.task.update(**fields)``.
- **Workspace** — ``ctx.setup_worktree()``, ``ctx.teardown_worktree()``.
- **Git gates** — ``ctx.require_clean_worktree()``, ``ctx.require_commits()``.
- **Merge** — ``ctx.merge()``.
- **Testing** — ``ctx.run_tests()``, ``ctx.run_script()``.
- **Review** — ``ctx.create_review()``.
- **Routing** — ``ctx.agents()``, ``ctx.pick()``, ``ctx.manager``, ``ctx.human``.
- **Communication** — ``ctx.notify()``, ``ctx.log()``.
- **Control flow** — ``ctx.require()``, ``ctx.fail()``.

All methods pre-bind ``hc_home``, ``team``, and ``task_id`` so the
workflow author never touches file paths or database details.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delegate.workflow import GateError, ActionError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TaskView — read-only view + update method
# ---------------------------------------------------------------------------

class TaskView:
    """Read-only view of a task with an ``update()`` method for mutations."""

    def __init__(self, task: dict, hc_home: Path, team: str):
        self._task = task
        self._hc_home = hc_home
        self._team = team

    def __getattr__(self, name: str) -> Any:
        try:
            return self._task[name]
        except KeyError:
            raise AttributeError(
                f"Task has no field '{name}'. "
                f"Available: {sorted(self._task.keys())}"
            )

    def __getitem__(self, key: str) -> Any:
        return self._task[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._task.get(key, default)

    @property
    def has_commits(self) -> bool:
        """True if any repo has commits beyond base_sha."""
        commits = self._task.get("commits", {})
        if not commits:
            return False
        return any(bool(shas) for shas in commits.values())

    def update(self, **kwargs: Any) -> None:
        """Update task fields in the database and refresh the local view."""
        from delegate.task import update_task
        updated = update_task(self._hc_home, self._team, self._task["id"], **kwargs)
        self._task.update(updated)

    def to_dict(self) -> dict:
        """Return the underlying task dict (copy)."""
        return dict(self._task)


# ---------------------------------------------------------------------------
# AgentInfo — lightweight agent descriptor
# ---------------------------------------------------------------------------

@dataclass
class AgentInfo:
    """Lightweight agent info for routing decisions."""
    name: str
    role: str
    active_task_count: int = 0


# ---------------------------------------------------------------------------
# MergeResult
# ---------------------------------------------------------------------------

@dataclass
class MergeResultView:
    """Result of a merge attempt, exposed to workflow authors."""
    success: bool
    message: str
    retryable: bool = False


# ---------------------------------------------------------------------------
# TestResult
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    """Result of running tests."""
    passed: bool
    output: str
    command: str = ""


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class Context:
    """Context object passed to workflow stage hooks.

    Pre-binds ``hc_home``, ``team``, and task so workflow authors
    interact with clean, high-level methods.
    """

    def __init__(self, hc_home: Path, team: str, task_dict: dict):
        self._hc_home = hc_home
        self._team = team
        self.task = TaskView(task_dict, hc_home, team)

    # ── Properties ──────────────────────────────────────────────

    @property
    def manager(self) -> str:
        """The team's manager agent name."""
        from delegate.bootstrap import get_member_by_role
        name = get_member_by_role(self._hc_home, self._team, "manager")
        return name or "manager"

    @property
    def human(self) -> str:
        """The default human member name."""
        from delegate.config import get_default_human
        return get_default_human(self._hc_home) or "human"

    @property
    def boss(self) -> str:
        """Deprecated — use ``human`` instead."""
        return self.human

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
        from delegate.task import format_task_id

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
        from delegate.task import format_task_id

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

    def merge(self, repo: str | None = None) -> MergeResultView:
        """Perform the full merge sequence (rebase, test, fast-forward).

        Returns a ``MergeResultView`` with ``success``, ``message``,
        and ``retryable`` fields.
        """
        from delegate.merge import merge_task

        # merge_task handles all repos on the task
        result = merge_task(self._hc_home, self._team, self.task.id)

        retryable = False
        if result.reason is not None:
            retryable = result.reason.retryable

        return MergeResultView(
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
            wf_name = self.task.get("workflow", "standard")
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

    # ── Agent routing ───────────────────────────────────────────

    def agents(self, role: str | None = None) -> list[AgentInfo]:
        """List agents on the team, optionally filtered by role.

        Returns ``AgentInfo`` objects with ``name``, ``role``, and
        ``active_task_count``.
        """
        import yaml
        from delegate.paths import agents_dir as _agents_dir
        from delegate.task import list_tasks

        agents_root = _agents_dir(self._hc_home, self._team)
        if not agents_root.is_dir():
            return []

        # Get active task counts
        active_tasks = list_tasks(self._hc_home, self._team, status="in_progress")
        task_counts: dict[str, int] = {}
        for t in active_tasks:
            a = t.get("assignee", "")
            if a:
                task_counts[a] = task_counts.get(a, 0) + 1

        result = []
        for d in sorted(agents_root.iterdir()):
            if not d.is_dir():
                continue
            state_file = d / "state.yaml"
            if not state_file.is_file():
                continue
            try:
                state = yaml.safe_load(state_file.read_text()) or {}
            except Exception:
                continue
            agent_name = d.name
            agent_role = state.get("role", "engineer")
            if role and agent_role != role:
                continue
            result.append(AgentInfo(
                name=agent_name,
                role=agent_role,
                active_task_count=task_counts.get(agent_name, 0),
            ))

        return result

    def pick(
        self,
        role: str | None = None,
        exclude: str | list[str] | None = None,
    ) -> str | None:
        """Pick the least-loaded agent matching criteria.

        Returns the agent name, or None if no candidates.
        """
        candidates = self.agents(role=role)

        if exclude:
            if isinstance(exclude, str):
                exclude = [exclude]
            exclude_set = set(exclude)
            candidates = [a for a in candidates if a.name not in exclude_set]

        if not candidates:
            return None

        return min(candidates, key=lambda a: a.active_task_count).name

    # ── Communication ───────────────────────────────────────────

    def notify(self, recipient: str, body: str, task_id: int | None = None) -> int | None:
        """Send a message to an agent's inbox.

        Returns the message ID, or None if delivery failed.
        """
        from delegate.mailbox import Message, deliver

        if task_id is None:
            task_id = self.task.id

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        msg = Message(
            sender=self.human,
            recipient=recipient,
            time=now,
            body=body,
            task_id=task_id,
        )

        try:
            return deliver(self._hc_home, self._team, msg)
        except Exception as exc:
            logger.warning("Failed to deliver notification to %s: %s", recipient, exc)
            return None

    def log(self, message: str, task_id: int | None = None) -> int:
        """Log a system event to the team's activity feed.

        Returns the event ID.
        """
        from delegate.chat import log_event

        if task_id is None:
            task_id = self.task.id

        return log_event(self._hc_home, self._team, message, task_id=task_id)

    # ── Control flow ────────────────────────────────────────────

    def require(self, condition: Any, message: str) -> None:
        """Assert a precondition.  Raises ``GateError`` if falsy."""
        if not condition:
            raise GateError(message)

    def fail(self, message: str) -> None:
        """Transition the task to the error state, assigned to the human.

        Raises ``ActionError`` which the runtime catches and handles.
        """
        raise ActionError(message)
