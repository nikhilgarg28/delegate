"""Merge worker — rebase, test, fast-forward merge for approved tasks.

The merge sequence for a task in ``in_approval`` with an approved review
(or ``approval == 'auto'`` on the repo):

1. Create a temporary branch mirroring the feature branch name with ``_merge/<uuid>`` inserted.
2. ``git rebase --onto main <base_sha> <temp>``  — rebase onto latest main.
3. If conflict: delete temp branch, set task to ``conflict``, notify manager.
4. Run test suite on the temp branch.
5. If tests fail: delete temp branch, set task to ``conflict``, notify manager.
6. ``git update-ref refs/heads/main <temp-tip> <main-tip>``  — atomic CAS.
7. Set task to ``done``.
8. Clean up: delete temp branch, feature branch, prune.

The real feature branch is **never modified** — if anything fails, we
just delete the temp branch and the original branch remains intact.

The merge worker is called from the daemon loop (via ``merge_once``).
"""

import logging
import shlex
import subprocess
import uuid
from pathlib import Path

from delegate.config import get_repo_approval, get_pre_merge_script
from delegate.notify import notify_conflict
from delegate.review import get_current_review
from delegate.task import get_task, change_status, update_task, list_tasks, format_task_id
from delegate.chat import log_event
from delegate.repo import get_repo_path, remove_task_worktree

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


def _create_temp_branch(repo_dir: str, source_branch: str) -> str:
    """Create a temporary branch from source_branch for merge attempt.

    The temp branch mirrors the feature branch structure with ``_merge/<uuid>``
    inserted before the task id segment::

        delegate/3f5776/myteam/T0001  →  delegate/3f5776/myteam/_merge/a1b2c3d4e5f6/T0001
        delegate/myteam/T0001         →  delegate/myteam/_merge/a1b2c3d4e5f6/T0001

    This makes it easy to see which task the temp branch belongs to and
    to clean up stale temp branches later.

    Returns the temp branch name.
    """
    uid = uuid.uuid4().hex[:12]
    # Insert _merge/<uuid> just before the last path segment (the task id)
    parts = source_branch.rsplit("/", 1)
    if len(parts) == 2:
        temp_name = f"{parts[0]}/_merge/{uid}/{parts[1]}"
    else:
        temp_name = f"_merge/{uid}/{source_branch}"
    result = _run_git(["branch", temp_name, source_branch], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"Could not create temp branch {temp_name}: {result.stderr}")
    return temp_name


def _delete_temp_branch(repo_dir: str, temp_branch: str) -> None:
    """Delete a temporary merge branch (best effort)."""
    # Make sure we're on main first so we're not on the branch we're deleting
    _run_git(["checkout", "main"], cwd=repo_dir)
    _run_git(["branch", "-D", temp_branch], cwd=repo_dir)


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


def _run_pre_merge(
    repo_dir: str,
    branch: str,
    hc_home: Path | None = None,
    team: str | None = None,
    repo_name: str | None = None,
) -> tuple[bool, str]:
    """Run the pre-merge script (or fall back to auto-detection).

    If a ``pre_merge_script`` is configured for the repo, it is executed as
    a single shell command.

    When no script is configured, falls back to auto-detection based on
    project files (pyproject.toml, package.json, Makefile).

    Returns (success, output).
    """
    # Check out the branch in the repo (it's already rebased)
    result = _run_git(["checkout", branch], cwd=repo_dir)
    if result.returncode != 0:
        return False, f"Could not checkout {branch}: {result.stderr}"

    # Check for a configured pre-merge script
    script: str | None = None
    if hc_home is not None and team is not None and repo_name:
        script = get_pre_merge_script(hc_home, team, repo_name)

    if script is not None:
        cmd = shlex.split(script)
        try:
            script_result = subprocess.run(
                cmd,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            output = script_result.stdout + script_result.stderr
            ok = script_result.returncode == 0
            return ok, output if not ok else f"Pre-merge script passed:\n{output}"
        except subprocess.TimeoutExpired:
            return False, "Pre-merge script timed out after 600 seconds."
        finally:
            _run_git(["checkout", "main"], cwd=repo_dir)

    # Fall back to auto-detection (no script configured)
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


# Keep old names as aliases for backward compatibility
_run_tests = _run_pre_merge
_run_pipeline = _run_pre_merge


def _ff_merge(repo_dir: str, branch: str) -> tuple[bool, str]:
    """Fast-forward merge the branch into main using atomic update-ref.

    Uses ``git update-ref`` with a compare-and-swap (CAS) to atomically
    advance main to the tip of *branch*.  This avoids needing to checkout
    main and ensures no concurrent push can race.

    Falls back to ``git merge --ff-only`` if update-ref fails (e.g. if
    branch is not a descendant of main).

    Returns (success, output).
    """
    # Get current main tip for CAS
    main_result = _run_git(["rev-parse", "main"], cwd=repo_dir)
    if main_result.returncode != 0:
        return False, f"Could not resolve main: {main_result.stderr}"
    main_tip = main_result.stdout.strip()

    # Get branch tip
    branch_result = _run_git(["rev-parse", branch], cwd=repo_dir)
    if branch_result.returncode != 0:
        return False, f"Could not resolve {branch}: {branch_result.stderr}"
    branch_tip = branch_result.stdout.strip()

    # Verify branch is a descendant of main (fast-forward check)
    ancestor_check = _run_git(["merge-base", "--is-ancestor", "main", branch], cwd=repo_dir)
    if ancestor_check.returncode != 0:
        return False, f"Fast-forward not possible: {branch} is not a descendant of main"

    # Atomic CAS: update main to branch tip only if main is still at main_tip
    result = _run_git(
        ["update-ref", "refs/heads/main", branch_tip, main_tip],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        return False, f"Atomic update-ref failed (concurrent push?): {result.stderr}"

    return True, f"main fast-forwarded to {branch_tip[:12]}"


def _other_unmerged_tasks_on_branch(
    hc_home: Path,
    team: str,
    branch: str,
    exclude_task_id: int,
) -> bool:
    """Check whether any other task shares *branch* and is not yet done.

    Returns ``True`` when at least one other task on the same branch still
    has a non-``done`` status, meaning the branch should be kept alive.
    """
    all_tasks = list_tasks(hc_home, team)
    for t in all_tasks:
        if t["id"] == exclude_task_id:
            continue
        if t.get("branch") == branch and t.get("status") != "done":
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

    All rebase/test work is done on a temporary branch (``_merge/<uuid>``).
    The real feature branch is never modified — if anything fails we just
    delete the temp branch and the original remains intact.

    For multi-repo tasks, each repo is rebased, tested, and merged
    independently.  If any repo fails, the entire task is marked as
    ``conflict`` and the merge is aborted.

    Args:
        hc_home: Delegate home directory.
        team: Team name.
        task_id: Task ID.
        skip_tests: Skip test execution (for emergencies).

    Returns:
        MergeResult indicating success or failure.
    """
    task = get_task(hc_home, team, task_id)
    branch = task.get("branch", "")
    repos: list[str] = task.get("repo", [])

    if not branch:
        return MergeResult(task_id, False, "No branch set on task")

    if not repos:
        return MergeResult(task_id, False, "No repo set on task")

    # Resolve all repos and verify they exist
    repo_dirs: dict[str, str] = {}
    for repo_name in repos:
        repo_dir = get_repo_path(hc_home, team, repo_name)
        real_repo = repo_dir.resolve()
        if not real_repo.is_dir():
            return MergeResult(task_id, False, f"repo not found: {real_repo}")
        repo_dirs[repo_name] = str(real_repo)

    log_event(hc_home, team, f"{format_task_id(task_id)} merge started ({branch})", task_id=task_id)

    # Step 0: Remove task worktrees in all repos.
    for repo_name in repos:
        try:
            remove_task_worktree(hc_home, team, repo_name, task_id)
            logger.info("Removed worktree for %s (%s) before merge", format_task_id(task_id), repo_name)
        except Exception as exc:
            logger.warning("Could not remove worktree for %s (%s) before merge: %s", format_task_id(task_id), repo_name, exc)

    base_sha_dict: dict = task.get("base_sha", {})
    merge_base_dict: dict[str, str] = {}
    merge_tip_dict: dict[str, str] = {}

    # Track temp branches so we can clean them up
    temp_branches: dict[str, str] = {}  # repo_name -> temp_branch_name

    for repo_name in repos:
        repo_str = repo_dirs[repo_name]

        # Step 1: Create a temp branch from the feature branch
        try:
            temp_branch = _create_temp_branch(repo_str, branch)
        except RuntimeError as exc:
            change_status(hc_home, team, task_id, "conflict")
            log_event(hc_home, team, f"{format_task_id(task_id)} could not create temp branch ({repo_name})", task_id=task_id)
            return MergeResult(task_id, False, str(exc))
        temp_branches[repo_name] = temp_branch

        # Step 2: Rebase the TEMP branch onto main (feature branch untouched)
        base_sha = base_sha_dict.get(repo_name, "")
        ok, output = _rebase_branch(repo_str, temp_branch, base_sha=base_sha)
        if not ok:
            _delete_temp_branch(repo_str, temp_branch)
            change_status(hc_home, team, task_id, "conflict")
            notify_conflict(hc_home, team, task, conflict_details=f"[{repo_name}] {output[:500]}")
            log_event(hc_home, team, f"{format_task_id(task_id)} merge conflict during rebase ({repo_name})", task_id=task_id)
            return MergeResult(task_id, False, f"Rebase conflict in {repo_name}: {output[:200]}")

        # Step 3: Run pre-merge script / tests on the temp branch (optional)
        if not skip_tests:
            ok, output = _run_pre_merge(repo_str, temp_branch, hc_home=hc_home, team=team, repo_name=repo_name)
            if not ok:
                _delete_temp_branch(repo_str, temp_branch)
                change_status(hc_home, team, task_id, "conflict")
                notify_conflict(hc_home, team, task, conflict_details=f"[{repo_name}] Pre-merge checks failed:\n{output[:500]}")
                log_event(hc_home, team, f"{format_task_id(task_id)} merge blocked — pre-merge checks failed ({repo_name})", task_id=task_id)
                return MergeResult(task_id, False, f"Pre-merge checks failed in {repo_name}: {output[:200]}")

        # Step 4: Fast-forward merge main to the temp branch tip (atomic CAS)
        pre_merge = _run_git(["rev-parse", "main"], cwd=repo_str)
        merge_base_dict[repo_name] = pre_merge.stdout.strip() if pre_merge.returncode == 0 else ""

        ok, output = _ff_merge(repo_str, temp_branch)
        if not ok:
            _delete_temp_branch(repo_str, temp_branch)
            change_status(hc_home, team, task_id, "conflict")
            notify_conflict(hc_home, team, task, conflict_details=f"[{repo_name}] {output[:500]}")
            log_event(hc_home, team, f"{format_task_id(task_id)} merge failed ({repo_name})", task_id=task_id)
            return MergeResult(task_id, False, f"Merge failed in {repo_name}: {output[:200]}")

        post_merge = _run_git(["rev-parse", "main"], cwd=repo_str)
        merge_tip_dict[repo_name] = post_merge.stdout.strip() if post_merge.returncode == 0 else ""

    # Step 5: Record per-repo merge_base and merge_tip, then mark as done
    update_task(hc_home, team, task_id, merge_base=merge_base_dict, merge_tip=merge_tip_dict)
    change_status(hc_home, team, task_id, "done")
    log_event(hc_home, team, f"{format_task_id(task_id)} merged to main ✓", task_id=task_id)

    # Step 6: Clean up temp branches
    for repo_name, temp_branch in temp_branches.items():
        _delete_temp_branch(repo_dirs[repo_name], temp_branch)

    # Step 7: Clean up feature branches (best effort).
    shared = _other_unmerged_tasks_on_branch(hc_home, team, branch, exclude_task_id=task_id)
    if shared:
        logger.info(
            "Skipping branch deletion for %s — other unmerged tasks share branch %s",
            format_task_id(task_id), branch,
        )
    else:
        for repo_name in repos:
            _cleanup_branch(repo_dirs[repo_name], branch)

    # Step 8: Clean up task worktrees (best effort).
    if not shared:
        for repo_name in repos:
            try:
                remove_task_worktree(hc_home, team, repo_name, task_id)
            except Exception as exc:
                logger.warning("Could not remove worktree for %s (%s): %s", task_id, repo_name, exc)

    return MergeResult(task_id, True, "Merged successfully")


def merge_once(hc_home: Path, team: str) -> list[MergeResult]:
    """Scan for tasks ready to merge and process them.

    A task is ready to merge if:
    - status == 'in_approval'
    - has an approved review verdict (for manual-approval repos)
    - OR the repo has approval == 'auto'

    Returns list of merge results.
    """
    tasks = list_tasks(hc_home, team, status="in_approval")
    results = []

    for task in tasks:
        task_id = task["id"]
        repos: list[str] = task.get("repo", [])

        if not repos:
            # No repo — can't auto-merge, just skip
            continue

        # Check approval mode — use the first repo's setting (most common case)
        approval_mode = get_repo_approval(hc_home, team, repos[0])

        if approval_mode == "auto":
            # Auto-merge: no boss approval needed
            change_status(hc_home, team, task_id, "merging")
            result = merge_task(hc_home, team, task_id)
            results.append(result)
        elif approval_mode == "manual":
            # Manual: only merge if boss has approved (check reviews table)
            review = get_current_review(hc_home, team, task_id)
            if review and review.get("verdict") == "approved":
                change_status(hc_home, team, task_id, "merging")
                result = merge_task(hc_home, team, task_id)
                results.append(result)
            else:
                logger.debug(
                    "%s: needs boss approval (verdict=%s)",
                    task_id, review.get("verdict") if review else "no review",
                )
        else:
            logger.warning(
                "%s: unknown approval mode '%s' for repos %s",
                task_id, approval_mode, repos,
            )

    return results
