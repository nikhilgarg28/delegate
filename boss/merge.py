"""Merge worker — rebase, test, fast-forward merge for approved tasks.

The merge sequence for a task in ``needs_merge`` with ``approval_status == 'approved'``
(or ``approval == 'auto'`` on the repo):

1. ``git rebase --onto main <base_sha> <branch>``  — rebase the agent's commits onto latest main.
2. If conflict: set task to ``conflict``, notify manager, abort.
3. Run test suite on the rebased branch.
4. If tests fail: set task to ``conflict``, notify manager.
5. ``git merge --ff-only <branch>`` — atomic fast-forward of main.
6. Set task to ``merged``.
7. Clean up: remove worktree, delete branch, prune.

The merge worker is called from the daemon loop (via ``merge_once``).
"""

import logging
import shlex
import subprocess
from pathlib import Path

from boss.config import get_repo_approval, get_repo_test_cmd, get_repo_pipeline
from boss.notify import notify_conflict
from boss.task import get_task, change_status, update_task, list_tasks, format_task_id
from boss.chat import log_event
from boss.repo import get_repo_path, remove_agent_worktree

logger = logging.getLogger(__name__)


class MergeResult:
    """Result of a merge attempt."""

    def __init__(self, task_id: int, success: bool, message: str):
        self.task_id = task_id
        self.success = success
        self.message = message

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAIL"
        return f"MergeResult({format_task_id(self.task_id)}, {status}, {self.message!r})"


def _run_git(args: list[str], cwd: str, **kwargs) -> subprocess.CompletedProcess:
    """Helper to run a git command."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        **kwargs,
    )


def _rebase_branch(repo_dir: str, branch: str, base_sha: str | None = None) -> tuple[bool, str]:
    """Rebase the branch onto main.

    When *base_sha* is provided the rebase uses ``--onto``::

        git rebase --onto main <base_sha> <branch>

    This ensures only the commits *after* base_sha are replayed onto main,
    which is important when main has been reset/reverted since the task
    started.  When base_sha is ``None`` or empty the original behaviour is
    preserved (``git rebase main <branch>``).

    Stashes any unstaged changes before rebasing and restores them after,
    so that untracked/modified files in the working directory (e.g. generated
    static assets) don't cause ``git rebase`` to fail.

    Returns (success, output).
    """
    # Stash any uncommitted changes (including untracked files) as a safety net
    stash_result = _run_git(["stash", "--include-untracked"], cwd=repo_dir)
    did_stash = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

    if base_sha:
        rebase_cmd = ["rebase", "--onto", "main", base_sha, branch]
    else:
        rebase_cmd = ["rebase", "main", branch]
    result = _run_git(rebase_cmd, cwd=repo_dir)
    if result.returncode != 0:
        # Abort the failed rebase to leave repo in clean state
        _run_git(["rebase", "--abort"], cwd=repo_dir)
        # Restore stashed changes even on failure
        if did_stash:
            _run_git(["stash", "pop"], cwd=repo_dir)
        return False, result.stderr + result.stdout

    # Restore stashed changes after successful rebase
    if did_stash:
        _run_git(["stash", "pop"], cwd=repo_dir)

    return True, result.stdout


def _run_pipeline(
    repo_dir: str,
    branch: str,
    hc_home: Path | None = None,
    repo_name: str | None = None,
) -> tuple[bool, str]:
    """Run the configured pipeline (or fall back to auto-detection).

    If a ``pipeline`` is configured for the repo, each step is executed in
    order.  Execution stops on the first failure, reporting which step failed.

    If no pipeline is configured but a legacy ``test_cmd`` exists, it is
    treated as a single-step pipeline (handled by :func:`get_repo_pipeline`).

    When neither pipeline nor test_cmd is configured, falls back to
    auto-detection based on project files (pyproject.toml, package.json,
    Makefile).

    Returns (success, output).
    """
    # Check out the branch in the repo (it's already rebased)
    result = _run_git(["checkout", branch], cwd=repo_dir)
    if result.returncode != 0:
        return False, f"Could not checkout {branch}: {result.stderr}"

    # Check for a configured pipeline first
    pipeline: list[dict] | None = None

    if hc_home is not None and repo_name:
        pipeline = get_repo_pipeline(hc_home, repo_name)

    if pipeline is not None:
        # Run each step in order
        all_output: list[str] = []
        try:
            for step in pipeline:
                step_name = step["name"]
                step_cmd = shlex.split(step["run"])
                try:
                    step_result = subprocess.run(
                        step_cmd,
                        cwd=repo_dir,
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    step_output = step_result.stdout + step_result.stderr
                    all_output.append(f"[{step_name}] {step_output}")
                    if step_result.returncode != 0:
                        return False, f"Step '{step_name}' failed:\n" + "\n".join(all_output)
                except subprocess.TimeoutExpired:
                    all_output.append(f"[{step_name}] Timed out after 300 seconds.")
                    return False, f"Step '{step_name}' failed:\n" + "\n".join(all_output)
            return True, "\n".join(all_output)
        finally:
            _run_git(["checkout", "main"], cwd=repo_dir)

    # Fall back to auto-detection (no pipeline or test_cmd configured)
    test_cmd: list[str] | None = None
    repo_path = Path(repo_dir)
    if (repo_path / "pyproject.toml").exists() or (repo_path / "tests").is_dir():
        test_cmd = ["python", "-m", "pytest", "-x", "-q"]
    elif (repo_path / "package.json").exists():
        test_cmd = ["npm", "test"]
    elif (repo_path / "Makefile").exists():
        test_cmd = ["make", "test"]

    if test_cmd is None:
        return True, "No test runner detected, skipping tests."

    try:
        test_result = subprocess.run(
            test_cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = test_result.stdout + test_result.stderr
        return test_result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Tests timed out after 300 seconds."
    finally:
        # Switch back to main
        _run_git(["checkout", "main"], cwd=repo_dir)


# Keep old name as alias for backward compatibility
_run_tests = _run_pipeline


def _ff_merge(repo_dir: str, branch: str) -> tuple[bool, str]:
    """Fast-forward merge the branch into main.

    Returns (success, output).
    """
    # Ensure we're on main
    result = _run_git(["checkout", "main"], cwd=repo_dir)
    if result.returncode != 0:
        return False, f"Could not checkout main: {result.stderr}"

    # Fast-forward merge
    result = _run_git(["merge", "--ff-only", branch], cwd=repo_dir)
    if result.returncode != 0:
        return False, f"Fast-forward merge failed: {result.stderr}"

    return True, result.stdout


def _other_unmerged_tasks_on_branch(
    hc_home: Path,
    branch: str,
    exclude_task_id: int,
) -> bool:
    """Check whether any other task shares *branch* and is not yet merged.

    Returns ``True`` when at least one other task on the same branch still
    has a non-``merged`` status, meaning the branch should be kept alive.
    """
    all_tasks = list_tasks(hc_home)
    for t in all_tasks:
        if t["id"] == exclude_task_id:
            continue
        if t.get("branch") == branch and t.get("status") != "merged":
            return True
    return False


def _cleanup_branch(repo_dir: str, branch: str) -> None:
    """Delete the merged branch and prune worktrees."""
    _run_git(["branch", "-d", branch], cwd=repo_dir)
    _run_git(["worktree", "prune"], cwd=repo_dir)


def merge_task(
    hc_home: Path,
    team: str,
    task_id: int,
    skip_tests: bool = False,
) -> MergeResult:
    """Execute the full merge sequence for a task.

    Args:
        hc_home: Boss home directory.
        team: Team name.
        task_id: Task ID.
        skip_tests: Skip test execution (for emergencies).

    Returns:
        MergeResult indicating success or failure.
    """
    task = get_task(hc_home, task_id)
    branch = task.get("branch", "")
    repo_name = task.get("repo", "")

    if not branch:
        return MergeResult(task_id, False, "No branch set on task")

    if not repo_name:
        return MergeResult(task_id, False, "No repo set on task")

    # Resolve the repo via symlink
    repo_dir = get_repo_path(hc_home, repo_name)
    real_repo = repo_dir.resolve()
    if not real_repo.is_dir():
        return MergeResult(task_id, False, f"Repo not found: {real_repo}")

    repo_str = str(real_repo)

    log_event(hc_home, f"{format_task_id(task_id)} merge started ({branch})")

    # Step 0: Remove agent worktree if branch is still checked out there.
    # The worktree is no longer needed once the task is in needs_merge status,
    # and git refuses to rebase/checkout a branch that is checked out elsewhere.
    assignee = task.get("assignee", "")
    if assignee:
        try:
            remove_agent_worktree(hc_home, team, repo_name, assignee, task_id)
            logger.info("Removed worktree for %s before merge", format_task_id(task_id))
        except Exception as exc:
            logger.warning("Could not remove worktree for %s before merge: %s", format_task_id(task_id), exc)

    # Step 1: Rebase onto main
    base_sha = task.get("base_sha", "")
    ok, output = _rebase_branch(repo_str, branch, base_sha=base_sha)
    if not ok:
        change_status(hc_home, task_id, "conflict")
        notify_conflict(hc_home, team, task, conflict_details=output[:500])
        log_event(hc_home, f"{format_task_id(task_id)} merge conflict during rebase")
        return MergeResult(task_id, False, f"Rebase conflict: {output[:200]}")

    # Step 2: Run pipeline / tests (optional)
    if not skip_tests:
        ok, output = _run_pipeline(repo_str, branch, hc_home=hc_home, repo_name=repo_name)
        if not ok:
            change_status(hc_home, task_id, "conflict")
            notify_conflict(hc_home, team, task, conflict_details=f"Pipeline failed:\n{output[:500]}")
            log_event(hc_home, f"{format_task_id(task_id)} merge blocked \u2014 pipeline failed")
            return MergeResult(task_id, False, f"Pipeline failed: {output[:200]}")

    # Step 3: Fast-forward merge
    # Capture HEAD of main before the merge (merge_base).
    # Ensure we read main's HEAD, not the current branch.
    pre_merge = _run_git(["rev-parse", "main"], cwd=repo_str)
    merge_base_sha = pre_merge.stdout.strip() if pre_merge.returncode == 0 else ""

    ok, output = _ff_merge(repo_str, branch)
    if not ok:
        change_status(hc_home, task_id, "conflict")
        notify_conflict(hc_home, team, task, conflict_details=output[:500])
        log_event(hc_home, f"{format_task_id(task_id)} merge failed (fast-forward)")
        return MergeResult(task_id, False, f"Merge failed: {output[:200]}")

    # Capture HEAD of main after the merge (merge_tip)
    post_merge = _run_git(["rev-parse", "main"], cwd=repo_str)
    merge_tip_sha = post_merge.stdout.strip() if post_merge.returncode == 0 else ""

    # Step 4: Record merge_base and merge_tip, then mark as merged
    update_task(hc_home, task_id, merge_base=merge_base_sha, merge_tip=merge_tip_sha)
    change_status(hc_home, task_id, "merged")
    log_event(hc_home, f"{format_task_id(task_id)} merged to main \u2713")

    # Step 5: Clean up branch (best effort).
    # Only delete the branch if no other unmerged tasks still reference it.
    if _other_unmerged_tasks_on_branch(hc_home, branch, exclude_task_id=task_id):
        logger.info(
            "Skipping branch deletion for %s — other unmerged tasks share branch %s",
            format_task_id(task_id), branch,
        )
    else:
        _cleanup_branch(repo_str, branch)

    # Step 6: Clean up the agent's worktree (best effort, may already be removed in Step 0).
    # Same guard: skip if other unmerged tasks share the branch (they may need the worktree).
    if assignee:
        if _other_unmerged_tasks_on_branch(hc_home, branch, exclude_task_id=task_id):
            logger.info(
                "Skipping worktree removal for %s — other unmerged tasks share branch %s",
                format_task_id(task_id), branch,
            )
        else:
            try:
                remove_agent_worktree(hc_home, team, repo_name, assignee, task_id)
            except Exception as exc:
                logger.warning("Could not remove worktree for %s: %s", task_id, exc)

    return MergeResult(task_id, True, "Merged successfully")


def merge_once(hc_home: Path, team: str) -> list[MergeResult]:
    """Scan for tasks ready to merge and process them.

    A task is ready to merge if:
    - status == 'needs_merge'
    - approval_status == 'approved' (for manual-approval repos)
    - OR the repo has approval == 'auto'

    Returns list of merge results.
    """
    tasks = list_tasks(hc_home, status="needs_merge")
    results = []

    for task in tasks:
        task_id = task["id"]
        repo_name = task.get("repo", "")

        if not repo_name:
            # No repo — can't auto-merge, just skip
            continue

        # Check approval mode
        approval_mode = get_repo_approval(hc_home, repo_name)

        if approval_mode == "auto":
            # Auto-merge: no boss approval needed
            result = merge_task(hc_home, team, task_id)
            results.append(result)
        elif approval_mode == "manual":
            # Manual: only merge if boss has approved
            if task.get("approval_status") == "approved":
                result = merge_task(hc_home, team, task_id)
                results.append(result)
            else:
                logger.debug(
                    "%s: needs boss approval (approval_status=%s)",
                    task_id, task.get("approval_status", ""),
                )
        else:
            logger.warning(
                "%s: unknown approval mode '%s' for repo '%s'",
                task_id, approval_mode, repo_name,
            )

    return results
