"""Merge worker — rebase, test, fast-forward merge for approved tasks.

The merge sequence for a task in ``in_approval`` with an approved review verdict
(or ``approval == 'auto'`` on the repo):

1. Create a **temporary worktree** for the feature branch — the main repo's
   working tree is never touched.
2. ``git rebase --onto main <base_sha>`` in the temp worktree.
3. If conflict: set task to ``conflict``, notify manager, abort.
4. Run test suite in the temp worktree.
5. If tests fail: set task to ``conflict``, notify manager.
6. Atomic fast-forward via ``git update-ref`` with compare-and-swap:
   verify ``main`` is an ancestor of the rebased branch tip, then
   ``git update-ref refs/heads/main <branch_tip> <old_main>`` — fails
   if another merge moved ``main`` in the meantime.
7. Set task to ``done``.
8. Clean up: remove temp worktree, agent worktree, delete branch, prune.

The merge worker is called from the daemon loop (via ``merge_once``).
"""

import logging
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from delegate.config import get_repo_approval, get_pre_merge_script
from delegate.notify import notify_conflict
from delegate.task import get_task, change_status, update_task, list_tasks, format_task_id
from delegate.chat import log_event
from delegate.repo import get_repo_path, remove_agent_worktree

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


# ---------------------------------------------------------------------------
# Temporary worktree helpers
# ---------------------------------------------------------------------------

def _create_merge_worktree(
    repo_dir: str,
    branch: str,
    task_id: int,
    repo_name: str,
) -> tuple[str | None, str]:
    """Create a temporary worktree checked out to *branch*.

    Returns ``(worktree_path, error_msg)``.  On failure *worktree_path* is
    ``None`` and *error_msg* describes the problem.
    """
    wt_path = Path(tempfile.mkdtemp(
        prefix=f"delegate-merge-T{task_id:04d}-{repo_name}-",
    ))
    # --force: allow even if branch is checked out elsewhere (safety net).
    result = _run_git(
        ["worktree", "add", "--force", str(wt_path), branch],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        shutil.rmtree(wt_path, ignore_errors=True)
        return None, f"Could not create merge worktree: {result.stderr}"
    return str(wt_path), ""


def _remove_merge_worktree(repo_dir: str, wt_path: str) -> None:
    """Remove a temporary merge worktree (best effort)."""
    _run_git(["worktree", "remove", wt_path, "--force"], cwd=repo_dir)
    _run_git(["worktree", "prune"], cwd=repo_dir)
    # Belt and suspenders — remove directory if git left it
    if Path(wt_path).exists():
        shutil.rmtree(wt_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Rebase (runs in temp worktree, never touches main repo working tree)
# ---------------------------------------------------------------------------

def _rebase_branch(
    wt_dir: str,
    base_sha: str,
) -> tuple[bool, str]:
    """Rebase the current branch onto main inside a worktree.

    The worktree must already be checked out to the feature branch.
    No branch argument is needed — git rebases the current HEAD.

    Uses ``--onto`` to replay only commits after *base_sha*::

        git rebase --onto main <base_sha>

    *base_sha* is mandatory — it is always recorded at task creation time.

    Returns ``(success, output)``.
    """
    if not base_sha:
        return False, "base_sha is required but was empty — cannot rebase safely"

    rebase_cmd = ["rebase", "--onto", "main", base_sha]

    result = _run_git(rebase_cmd, cwd=wt_dir)
    if result.returncode != 0:
        _run_git(["rebase", "--abort"], cwd=wt_dir)
        return False, result.stderr + result.stdout

    return True, result.stdout


# ---------------------------------------------------------------------------
# Pre-merge tests (runs in temp worktree, no git checkout needed)
# ---------------------------------------------------------------------------

def _run_pre_merge(
    work_dir: str,
    *,
    hc_home: Path | None = None,
    team: str | None = None,
    repo_name: str | None = None,
) -> tuple[bool, str]:
    """Run the pre-merge script (or fall back to auto-detection).

    *work_dir* is the directory where tests should run — typically the
    temp merge worktree which is already checked out to the rebased branch.
    No ``git checkout`` is performed.

    Returns ``(success, output)``.
    """
    # Check for a configured pre-merge script
    script: str | None = None
    if hc_home is not None and team is not None and repo_name:
        script = get_pre_merge_script(hc_home, team, repo_name)

    if script is not None:
        cmd = shlex.split(script)
        try:
            script_result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            output = script_result.stdout + script_result.stderr
            ok = script_result.returncode == 0
            return ok, output if not ok else f"Pre-merge script passed:\n{output}"
        except subprocess.TimeoutExpired:
            return False, "Pre-merge script timed out after 600 seconds."

    # Fall back to auto-detection (no script configured)
    test_cmd: list[str] | None = None
    work_path = Path(work_dir)
    if (work_path / "pyproject.toml").exists() or (work_path / "tests").is_dir():
        test_cmd = ["python", "-m", "pytest", "-x", "-q"]
    elif (work_path / "package.json").exists():
        test_cmd = ["npm", "test"]
    elif (work_path / "Makefile").exists():
        test_cmd = ["make", "test"]

    if test_cmd is None:
        return True, "No test runner detected, skipping tests."

    try:
        test_result = subprocess.run(
            test_cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = test_result.stdout + test_result.stderr
        return test_result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Tests timed out after 300 seconds."


# Keep old names as aliases for backward compatibility
_run_tests = _run_pre_merge
_run_pipeline = _run_pre_merge


# ---------------------------------------------------------------------------
# Atomic fast-forward via update-ref (never touches working tree)
# ---------------------------------------------------------------------------

def _ff_merge_update_ref(repo_dir: str, branch: str) -> tuple[bool, str]:
    """Fast-forward ``main`` to *branch* tip using ``git update-ref``.

    1. Resolve current ``main`` and *branch* SHAs.
    2. Verify *main* is an ancestor of *branch* (fast-forward check).
    3. Atomically advance ``refs/heads/main`` via compare-and-swap:
       ``git update-ref refs/heads/main <new> <old>`` — fails if another
       process moved ``main`` between steps 1 and 3.

    The main repo's working tree is **never modified**.

    Returns ``(success, output)``.
    """
    main_result = _run_git(["rev-parse", "refs/heads/main"], cwd=repo_dir)
    if main_result.returncode != 0:
        return False, f"Could not resolve main: {main_result.stderr}"
    main_sha = main_result.stdout.strip()

    branch_result = _run_git(["rev-parse", f"refs/heads/{branch}"], cwd=repo_dir)
    if branch_result.returncode != 0:
        return False, f"Could not resolve {branch}: {branch_result.stderr}"
    branch_sha = branch_result.stdout.strip()

    if main_sha == branch_sha:
        return True, "Already up to date."

    # Fast-forward check: main must be an ancestor of branch tip
    ancestor_check = _run_git(
        ["merge-base", "--is-ancestor", main_sha, branch_sha],
        cwd=repo_dir,
    )
    if ancestor_check.returncode != 0:
        return False, (
            f"Fast-forward not possible: main ({main_sha[:10]}) is not an "
            f"ancestor of {branch} ({branch_sha[:10]})"
        )

    # Atomic CAS: update main to branch_sha, expecting old value main_sha.
    # If another merge moved main in the meantime, this fails.
    update_result = _run_git(
        ["update-ref", "refs/heads/main", branch_sha, main_sha],
        cwd=repo_dir,
    )
    if update_result.returncode != 0:
        return False, (
            f"update-ref CAS failed (concurrent merge?): {update_result.stderr}"
        )

    return True, f"main fast-forwarded {main_sha[:10]} → {branch_sha[:10]}"


# Keep the old name as an alias (tests may reference it)
_ff_merge = _ff_merge_update_ref


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main merge entry point
# ---------------------------------------------------------------------------

def merge_task(
    hc_home: Path,
    team: str,
    task_id: int,
    skip_tests: bool = False,
) -> MergeResult:
    """Execute the full merge sequence for a task.

    All git operations (rebase, test, merge) happen in a **temporary
    worktree** — the main repo's working tree is never touched, avoiding
    race conditions with agents or the user.

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

    # Step 0: Remove agent worktrees so the branch is free to check out.
    dri = task.get("dri", "") or task.get("assignee", "")
    if dri:
        for repo_name in repos:
            try:
                remove_agent_worktree(hc_home, team, repo_name, dri, task_id)
                logger.info("Removed worktree for %s (%s) before merge", format_task_id(task_id), repo_name)
            except Exception as exc:
                logger.warning("Could not remove worktree for %s (%s) before merge: %s", format_task_id(task_id), repo_name, exc)

    base_sha_dict: dict = task.get("base_sha", {})

    # Validate base_sha is present for all repos (should always be set at creation)
    for repo_name in repos:
        if not base_sha_dict.get(repo_name):
            return MergeResult(
                task_id, False,
                f"Missing base_sha for repo {repo_name} — "
                f"cannot rebase safely (task may predate eager base_sha recording)",
            )

    merge_base_dict: dict[str, str] = {}
    merge_tip_dict: dict[str, str] = {}

    for repo_name in repos:
        repo_str = repo_dirs[repo_name]
        wt_path: str | None = None

        try:
            # Step 1: Create temp worktree for the feature branch
            wt_path, err = _create_merge_worktree(repo_str, branch, task_id, repo_name)
            if wt_path is None:
                change_status(hc_home, team, task_id, "conflict")
                notify_conflict(hc_home, team, task, conflict_details=f"[{repo_name}] {err}")
                log_event(hc_home, team, f"{format_task_id(task_id)} merge failed — worktree error ({repo_name})", task_id=task_id)
                return MergeResult(task_id, False, f"Worktree error in {repo_name}: {err[:200]}")

            # Step 2: Rebase onto main (inside the temp worktree)
            base_sha = base_sha_dict[repo_name]
            ok, output = _rebase_branch(wt_path, base_sha)
            if not ok:
                change_status(hc_home, team, task_id, "conflict")
                notify_conflict(hc_home, team, task, conflict_details=f"[{repo_name}] {output[:500]}")
                log_event(hc_home, team, f"{format_task_id(task_id)} merge conflict during rebase ({repo_name})", task_id=task_id)
                return MergeResult(task_id, False, f"Rebase conflict in {repo_name}: {output[:200]}")

            # Step 3: Run pre-merge script / tests (in the temp worktree)
            if not skip_tests:
                ok, output = _run_pre_merge(wt_path, hc_home=hc_home, team=team, repo_name=repo_name)
                if not ok:
                    change_status(hc_home, team, task_id, "conflict")
                    notify_conflict(hc_home, team, task, conflict_details=f"[{repo_name}] Pre-merge checks failed:\n{output[:500]}")
                    log_event(hc_home, team, f"{format_task_id(task_id)} merge blocked — pre-merge checks failed ({repo_name})", task_id=task_id)
                    return MergeResult(task_id, False, f"Pre-merge checks failed in {repo_name}: {output[:200]}")

            # Step 4: Atomic fast-forward via update-ref CAS
            pre_merge = _run_git(["rev-parse", "refs/heads/main"], cwd=repo_str)
            merge_base_dict[repo_name] = pre_merge.stdout.strip() if pre_merge.returncode == 0 else ""

            ok, output = _ff_merge_update_ref(repo_str, branch)
            if not ok:
                change_status(hc_home, team, task_id, "conflict")
                notify_conflict(hc_home, team, task, conflict_details=f"[{repo_name}] {output[:500]}")
                log_event(hc_home, team, f"{format_task_id(task_id)} merge failed ({repo_name})", task_id=task_id)
                return MergeResult(task_id, False, f"Merge failed in {repo_name}: {output[:200]}")

            post_merge = _run_git(["rev-parse", "refs/heads/main"], cwd=repo_str)
            merge_tip_dict[repo_name] = post_merge.stdout.strip() if post_merge.returncode == 0 else ""

        finally:
            # Always clean up the temp worktree
            if wt_path is not None:
                _remove_merge_worktree(repo_str, wt_path)

    # Step 5: Record per-repo merge_base and merge_tip, then mark as done
    update_task(hc_home, team, task_id, merge_base=merge_base_dict, merge_tip=merge_tip_dict)
    change_status(hc_home, team, task_id, "done")
    log_event(hc_home, team, f"{format_task_id(task_id)} merged to main ✓", task_id=task_id)

    # Step 6: Clean up branches (best effort).
    shared = _other_unmerged_tasks_on_branch(hc_home, team, branch, exclude_task_id=task_id)
    if shared:
        logger.info(
            "Skipping branch deletion for %s — other unmerged tasks share branch %s",
            format_task_id(task_id), branch,
        )
    else:
        for repo_name in repos:
            _cleanup_branch(repo_dirs[repo_name], branch)

    # Step 7: Clean up agent worktrees (best effort).
    if dri and not shared:
        for repo_name in repos:
            try:
                remove_agent_worktree(hc_home, team, repo_name, dri, task_id)
            except Exception as exc:
                logger.warning("Could not remove worktree for %s (%s): %s", task_id, repo_name, exc)

    return MergeResult(task_id, True, "Merged successfully")


def merge_once(hc_home: Path, team: str) -> list[MergeResult]:
    """Scan for tasks ready to merge and process them.

    A task is ready to merge if:
    - status == 'in_approval'
    - current review verdict == 'approved' (for manual-approval repos)
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
            result = merge_task(hc_home, team, task_id)
            results.append(result)
        elif approval_mode == "manual":
            # Manual: only merge if boss has approved (check reviews table)
            from delegate.review import get_current_review
            review = get_current_review(hc_home, team, task_id)
            verdict = review.get("verdict") if review else None
            if verdict == "approved":
                result = merge_task(hc_home, team, task_id)
                results.append(result)
            else:
                logger.debug(
                    "%s: needs boss approval (review verdict=%s)",
                    task_id, verdict,
                )
        else:
            logger.warning(
                "%s: unknown approval mode '%s' for repos %s",
                task_id, approval_mode, repos,
            )

    return results
