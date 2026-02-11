"""Merge worker — rebase, test, fast-forward merge for approved tasks.

The merge sequence for a task in ``in_approval`` with an approved review
(or ``approval == 'auto'`` on the repo):

1. Create a disposable worktree + temp branch from the feature branch.
2. ``git rebase --onto main <base_sha> <temp>``  — rebase in the temp worktree.
3. If conflict: remove temp worktree/branch, escalate to manager.
4. Run pre-merge script / tests inside the temp worktree.
5. If tests fail: remove temp worktree/branch, escalate to manager.
6. Fast-forward main:
   - If user has ``main`` checked out AND dirty → **fail** (auto-retry).
   - If user has ``main`` checked out AND clean → ``git merge --ff-only``
     (updates ref AND working tree).
   - If user is on another branch → ``git update-ref`` with CAS (ref-only).
7. Set task to ``done``.
8. Clean up: remove temp worktree/branch, feature branch, agent worktree.

Key invariants:
- The **main repo working directory is never touched** during rebase/test.
  The only time the working tree may advance is when the user has ``main``
  checked out cleanly — then ``merge --ff-only`` updates it in lockstep.
- The **feature branch and agent worktree are never modified** during the
  merge attempt.  Only on success are they cleaned up.  On failure, the
  agent could resume work without any manual recovery.

Failure handling:
- ``merge_task()`` is a **pure** merge function — it returns a result but
  never changes task status or assignee itself.
- ``merge_once()`` inspects the ``MergeFailureReason`` on failures and
  routes them:
  - **Retryable** failures (dirty main, transient ref conflicts) are
    silently retried up to 3 times (``merge_attempts``).
  - **Non-retryable** failures (rebase conflict, test failure, worktree
    error) are immediately escalated: status → ``merge_failed``, assign
    to manager, send notification.
  - After 3 retries, retryable failures also escalate to manager.

The merge worker is called from the daemon loop (via ``merge_once``).
"""

import enum
import logging
import shlex
import subprocess
import uuid
from pathlib import Path

from delegate.config import get_repo_approval, get_pre_merge_script
from delegate.notify import notify_conflict
from delegate.review import get_current_review
from delegate.task import (
    get_task, change_status, update_task, list_tasks,
    format_task_id, transition_task, assign_task,
)
from delegate.chat import log_event
from delegate.repo import get_repo_path, remove_task_worktree

logger = logging.getLogger(__name__)

MAX_MERGE_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Failure reason enum
# ---------------------------------------------------------------------------

class MergeFailureReason(enum.Enum):
    """Structured reasons for merge failures.

    Each member carries a human-readable ``short_message`` and a
    ``retryable`` flag that determines the routing policy in
    ``merge_once()``.
    """

    REBASE_CONFLICT   = ("Rebase conflict", False)
    PRE_MERGE_FAILED  = ("Pre-merge checks failed", False)
    WORKTREE_ERROR    = ("Could not create merge worktree", False)
    DIRTY_MAIN        = ("main has uncommitted changes", True)
    FF_NOT_POSSIBLE   = ("Fast-forward not possible", True)
    UPDATE_REF_FAILED = ("Atomic ref update failed", True)

    def __init__(self, short_message: str, retryable: bool):
        self.short_message = short_message
        self.retryable = retryable


class MergeResult:
    """Result of a merge attempt."""

    def __init__(
        self,
        task_id: int,
        success: bool,
        message: str,
        reason: MergeFailureReason | None = None,
    ):
        self.task_id = task_id
        self.success = success
        self.message = message
        self.reason = reason  # None on success

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAIL"
        tag = f", reason={self.reason.name}" if self.reason else ""
        return f"MergeResult({format_task_id(self.task_id)}, {status}, {self.message!r}{tag})"


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
# Temp worktree lifecycle
# ---------------------------------------------------------------------------

def _merge_worktree_dir(hc_home: Path, team: str, uid: str, task_id: int) -> Path:
    """Worktree path for a merge attempt.

    Layout: ``teams/<team>/worktrees/_merge/<uid>/T<id>/``
    """
    return (
        hc_home / "teams" / team / "worktrees" / "_merge"
        / uid / format_task_id(task_id)
    )


def _create_temp_worktree(
    repo_dir: str,
    source_branch: str,
    wt_path: Path,
) -> tuple[str, str]:
    """Create a disposable worktree + temp branch from *source_branch*.

    The temp branch mirrors the feature branch structure with
    ``_merge/<uuid>`` inserted before the task-id segment::

        delegate/3f5776/myteam/T0001  →  delegate/3f5776/myteam/_merge/a1b2c3d4e5f6/T0001

    Returns ``(temp_branch_name, uid)``.

    Raises ``RuntimeError`` on failure.
    """
    uid = uuid.uuid4().hex[:12]

    # Derive temp branch name (insert _merge/<uid> before last segment)
    parts = source_branch.rsplit("/", 1)
    if len(parts) == 2:
        temp_branch = f"{parts[0]}/_merge/{uid}/{parts[1]}"
    else:
        temp_branch = f"_merge/{uid}/{source_branch}"

    # Create worktree + branch in one atomic command.
    # ``git worktree add -b <branch> <path> <start>`` creates a new branch
    # at <start> and checks it out in the new worktree.
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(
        ["worktree", "add", "-b", temp_branch, str(wt_path), source_branch],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not create merge worktree: {result.stderr.strip()}"
        )
    return temp_branch, uid


def _remove_temp_worktree(repo_dir: str, wt_path: Path, temp_branch: str) -> None:
    """Remove a disposable merge worktree and its branch (best-effort)."""
    if wt_path.exists():
        _run_git(["worktree", "remove", str(wt_path), "--force"], cwd=repo_dir)
    _run_git(["branch", "-D", temp_branch], cwd=repo_dir)
    _run_git(["worktree", "prune"], cwd=repo_dir)
    # Clean up empty parent directories under _merge/
    try:
        parent = wt_path.parent
        while parent.name != "_merge" and parent != parent.parent:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
            else:
                break
        # Remove _merge/ itself if empty
        if parent.name == "_merge" and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass  # best-effort cleanup


# ---------------------------------------------------------------------------
# Rebase (runs inside temp worktree)
# ---------------------------------------------------------------------------

def _rebase_onto_main(wt_dir: str, base_sha: str | None = None) -> tuple[bool, str]:
    """Rebase the current branch onto main inside the temp worktree.

    When *base_sha* is provided::

        git rebase --onto main <base_sha> HEAD

    This replays only the commits after ``base_sha`` onto current main.
    When *base_sha* is empty, falls back to ``git rebase main``.

    Returns ``(success, output)``.
    """
    if base_sha:
        rebase_cmd = ["rebase", "--onto", "main", base_sha]
    else:
        rebase_cmd = ["rebase", "main"]

    result = _run_git(rebase_cmd, cwd=wt_dir)
    if result.returncode != 0:
        _run_git(["rebase", "--abort"], cwd=wt_dir)
        return False, result.stderr + result.stdout

    return True, result.stdout


# ---------------------------------------------------------------------------
# Pre-merge tests (runs inside temp worktree)
# ---------------------------------------------------------------------------

def _run_pre_merge(
    wt_dir: str,
    hc_home: Path | None = None,
    team: str | None = None,
    repo_name: str | None = None,
) -> tuple[bool, str]:
    """Run pre-merge script or auto-detected tests inside the temp worktree.

    The temp worktree already has the rebased code checked out, so no
    ``git checkout`` is needed.

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
                cwd=wt_dir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            output = script_result.stdout + script_result.stderr
            ok = script_result.returncode == 0
            return ok, output if not ok else f"Pre-merge script passed:\n{output}"
        except subprocess.TimeoutExpired:
            return False, "Pre-merge script timed out after 600 seconds."
        except OSError as exc:
            return False, f"Pre-merge script failed to start: {exc}"

    # Fall back to auto-detection (no script configured)
    test_cmd: list[str] | None = None
    wt_path = Path(wt_dir)
    if (wt_path / "pyproject.toml").exists() or (wt_path / "tests").is_dir():
        test_cmd = ["python", "-m", "pytest", "-x", "-q"]
    elif (wt_path / "package.json").exists():
        test_cmd = ["npm", "test"]
    elif (wt_path / "Makefile").exists():
        test_cmd = ["make", "test"]

    if test_cmd is None:
        return True, "No test runner detected, skipping tests."

    try:
        test_result = subprocess.run(
            test_cmd,
            cwd=wt_dir,
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
# Fast-forward merge (operates on refs only — no checkout needed)
# ---------------------------------------------------------------------------

def _ff_merge(repo_dir: str, branch: str) -> tuple[bool, str]:
    """Fast-forward merge the branch into main.

    Behaviour depends on the user's checkout state in the main repo:

    - **main checked out + dirty** → fail (protect uncommitted work).
    - **main checked out + clean** → ``git merge --ff-only`` (updates ref
      AND working tree so the user doesn't see phantom dirty files).
    - **other branch checked out** → ``git update-ref`` with CAS (ref-only,
      user's working tree is untouched).

    Returns ``(success, output)``.
    """
    # Get branch tip
    branch_result = _run_git(["rev-parse", branch], cwd=repo_dir)
    if branch_result.returncode != 0:
        return False, f"Could not resolve {branch}: {branch_result.stderr}"
    branch_tip = branch_result.stdout.strip()

    # Verify branch is a descendant of main (fast-forward check)
    ancestor_check = _run_git(
        ["merge-base", "--is-ancestor", "main", branch], cwd=repo_dir,
    )
    if ancestor_check.returncode != 0:
        return False, f"Fast-forward not possible: {branch} is not a descendant of main"

    # Check what the user has checked out in the main repo
    head_result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    user_branch = head_result.stdout.strip() if head_result.returncode == 0 else ""

    if user_branch == "main":
        # User is on main — check for uncommitted changes
        status_result = _run_git(["status", "--porcelain"], cwd=repo_dir)
        dirty = status_result.stdout.strip()
        if dirty:
            return False, (
                "Main repo has uncommitted changes on main — "
                "commit or stash them before merging.\n"
                f"Dirty files:\n{dirty[:500]}"
            )

        # Clean main checkout: use merge --ff-only to update ref + working tree
        result = _run_git(["merge", "--ff-only", branch], cwd=repo_dir)
        if result.returncode != 0:
            return False, f"Fast-forward merge failed: {result.stderr}"
        return True, f"main fast-forwarded to {branch_tip[:12]} (working tree updated)"

    else:
        # User is on another branch: move ref only via atomic CAS
        main_result = _run_git(["rev-parse", "main"], cwd=repo_dir)
        if main_result.returncode != 0:
            return False, f"Could not resolve main: {main_result.stderr}"
        main_tip = main_result.stdout.strip()

        result = _run_git(
            ["update-ref", "refs/heads/main", branch_tip, main_tip],
            cwd=repo_dir,
        )
        if result.returncode != 0:
            return False, f"Atomic update-ref failed (concurrent push?): {result.stderr}"
        return True, f"main fast-forwarded to {branch_tip[:12]} (ref-only, user on {user_branch})"


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


def _cleanup_after_merge(
    hc_home: Path,
    team: str,
    task_id: int,
    branch: str,
    repos: list[str],
    repo_dirs: dict[str, str],
    temp_worktrees: dict[str, tuple[Path, str]],
) -> None:
    """Clean up after a successful merge.

    Removes temp worktrees/branches, and if no sibling tasks share the
    feature branch, also removes the feature branch and agent worktree.
    """
    # 1. Remove temp worktrees and branches
    for repo_name, (wt_path, temp_branch) in temp_worktrees.items():
        _remove_temp_worktree(repo_dirs[repo_name], wt_path, temp_branch)

    # 2. Clean up feature branch + agent worktree (if no siblings need it)
    shared = _other_unmerged_tasks_on_branch(hc_home, team, branch, exclude_task_id=task_id)
    if shared:
        logger.info(
            "Skipping branch deletion for %s — other unmerged tasks share branch %s",
            format_task_id(task_id), branch,
        )
        return

    for rn in repos:
        rd = repo_dirs[rn]
        _run_git(["branch", "-d", branch], cwd=rd)
        _run_git(["worktree", "prune"], cwd=rd)
        try:
            remove_task_worktree(hc_home, team, rn, task_id)
        except Exception as exc:
            logger.warning(
                "Could not remove agent worktree for %s (%s): %s",
                format_task_id(task_id), rn, exc,
            )


# ---------------------------------------------------------------------------
# Main merge sequence
# ---------------------------------------------------------------------------

def merge_task(
    hc_home: Path,
    team: str,
    task_id: int,
    skip_tests: bool = False,
) -> MergeResult:
    """Execute the full merge sequence for a task.

    This is a **pure** merge function: it attempts rebase → test → ff-merge
    and returns a ``MergeResult``.  It does **not** change the task's status
    or assignee — that is the caller's responsibility (``merge_once``).

    All rebase/test work is done in a disposable worktree with a temporary
    branch.  The feature branch, agent worktree, and main repo working
    directory are **never touched** during the merge attempt.

    On success: temp worktree/branch removed, feature branch/agent worktree
    cleaned up, merge_base/merge_tip recorded, task marked ``done``.

    On failure: only the temp worktree/branch is removed.  The feature
    branch and agent worktree remain intact for the agent to resume.

    Args:
        hc_home: Delegate home directory.
        team: Team name.
        task_id: Task ID.
        skip_tests: Skip test execution (for emergencies).

    Returns:
        MergeResult indicating success or failure (with reason).
    """
    task = get_task(hc_home, team, task_id)
    branch = task.get("branch", "")
    repos: list[str] = task.get("repo", [])

    if not branch:
        return MergeResult(task_id, False, "No branch set on task",
                           reason=MergeFailureReason.WORKTREE_ERROR)

    if not repos:
        return MergeResult(task_id, False, "No repo set on task",
                           reason=MergeFailureReason.WORKTREE_ERROR)

    # Resolve all repos and verify they exist
    repo_dirs: dict[str, str] = {}
    for repo_name in repos:
        repo_dir = get_repo_path(hc_home, team, repo_name)
        real_repo = repo_dir.resolve()
        if not real_repo.is_dir():
            return MergeResult(task_id, False, f"repo not found: {real_repo}",
                               reason=MergeFailureReason.WORKTREE_ERROR)
        repo_dirs[repo_name] = str(real_repo)

    log_event(hc_home, team, f"{format_task_id(task_id)} merge started ({branch})", task_id=task_id)

    base_sha_dict: dict = task.get("base_sha", {})
    merge_base_dict: dict[str, str] = {}
    merge_tip_dict: dict[str, str] = {}

    # Track temp worktrees so we can clean them up
    temp_worktrees: dict[str, tuple[Path, str]] = {}  # repo_name -> (wt_path, temp_branch)

    for repo_name in repos:
        repo_str = repo_dirs[repo_name]

        # Step 1: Create a disposable worktree + temp branch from the feature branch.
        #         The feature branch and agent worktree are NOT touched.
        uid = uuid.uuid4().hex[:12]
        wt_path = _merge_worktree_dir(hc_home, team, uid, task_id)
        try:
            temp_branch, uid = _create_temp_worktree(repo_str, branch, wt_path)
        except RuntimeError as exc:
            log_event(
                hc_home, team,
                f"{format_task_id(task_id)} could not create merge worktree ({repo_name})",
                task_id=task_id,
            )
            return MergeResult(task_id, False, str(exc),
                               reason=MergeFailureReason.WORKTREE_ERROR)
        temp_worktrees[repo_name] = (wt_path, temp_branch)

        wt_str = str(wt_path)

        # Step 2: Rebase the TEMP branch onto main (inside the temp worktree).
        #         Feature branch is untouched.
        base_sha = base_sha_dict.get(repo_name, "")
        ok, output = _rebase_onto_main(wt_str, base_sha=base_sha)
        if not ok:
            _remove_temp_worktree(repo_str, wt_path, temp_branch)
            log_event(
                hc_home, team,
                f"{format_task_id(task_id)} merge conflict during rebase ({repo_name})",
                task_id=task_id,
            )
            return MergeResult(
                task_id, False,
                f"Rebase conflict in {repo_name}: {output[:200]}",
                reason=MergeFailureReason.REBASE_CONFLICT,
            )

        # Step 3: Run pre-merge script / tests inside the temp worktree.
        if not skip_tests:
            ok, output = _run_pre_merge(wt_str, hc_home=hc_home, team=team, repo_name=repo_name)
            if not ok:
                _remove_temp_worktree(repo_str, wt_path, temp_branch)
                log_event(
                    hc_home, team,
                    f"{format_task_id(task_id)} merge blocked — pre-merge checks failed ({repo_name})",
                    task_id=task_id,
                )
                return MergeResult(
                    task_id, False,
                    f"Pre-merge checks failed in {repo_name}: {output[:200]}",
                    reason=MergeFailureReason.PRE_MERGE_FAILED,
                )

        # Step 4: Fast-forward merge main to the temp branch tip (atomic CAS).
        pre_merge = _run_git(["rev-parse", "main"], cwd=repo_str)
        merge_base_dict[repo_name] = pre_merge.stdout.strip() if pre_merge.returncode == 0 else ""

        ok, output = _ff_merge(repo_str, temp_branch)
        if not ok:
            _remove_temp_worktree(repo_str, wt_path, temp_branch)
            log_event(
                hc_home, team,
                f"{format_task_id(task_id)} merge failed ({repo_name})",
                task_id=task_id,
            )
            # Classify the ff-merge failure
            if "uncommitted" in output.lower():
                reason = MergeFailureReason.DIRTY_MAIN
            elif "not a descendant" in output.lower() or "not possible" in output.lower():
                reason = MergeFailureReason.FF_NOT_POSSIBLE
            elif "update-ref failed" in output.lower() or "concurrent" in output.lower():
                reason = MergeFailureReason.UPDATE_REF_FAILED
            else:
                reason = MergeFailureReason.FF_NOT_POSSIBLE
            return MergeResult(
                task_id, False,
                f"Merge failed in {repo_name}: {output[:200]}",
                reason=reason,
            )

        post_merge = _run_git(["rev-parse", "main"], cwd=repo_str)
        merge_tip_dict[repo_name] = post_merge.stdout.strip() if post_merge.returncode == 0 else ""

    # Step 5: Record per-repo merge_base and merge_tip, then mark as done.
    update_task(hc_home, team, task_id, merge_base=merge_base_dict, merge_tip=merge_tip_dict)
    change_status(hc_home, team, task_id, "done")
    log_event(hc_home, team, f"{format_task_id(task_id)} merged to main \u2713", task_id=task_id)

    # Step 6: Clean up temp worktrees/branches + feature branch + agent worktree.
    _cleanup_after_merge(hc_home, team, task_id, branch, repos, repo_dirs, temp_worktrees)

    return MergeResult(task_id, True, "Merged successfully")


def _get_manager_name(hc_home: Path, team: str) -> str:
    """Look up the manager agent name for this team."""
    from delegate.bootstrap import get_member_by_role
    return get_member_by_role(hc_home, team, "manager") or "manager"


def _handle_merge_failure(
    hc_home: Path,
    team: str,
    task_id: int,
    result: MergeResult,
) -> None:
    """Route a merge failure based on the failure reason.

    - **Retryable** failures: increment ``merge_attempts``.  If still below
      ``MAX_MERGE_ATTEMPTS``, silently revert to ``in_approval`` (will be
      retried next daemon cycle).  Otherwise, escalate.
    - **Non-retryable** failures (or max retries exhausted): set status to
      ``merge_failed``, assign to manager, send ``notify_conflict``.
    """
    reason = result.reason
    if reason is None:
        reason = MergeFailureReason.WORKTREE_ERROR  # defensive fallback

    task = get_task(hc_home, team, task_id)
    detail = reason.short_message
    manager = _get_manager_name(hc_home, team)

    if reason.retryable:
        current_attempts = task.get("merge_attempts", 0) + 1
        update_task(hc_home, team, task_id,
                    merge_attempts=current_attempts,
                    status_detail=detail)

        if current_attempts < MAX_MERGE_ATTEMPTS:
            # Silent retry: revert status back to in_approval for next cycle
            change_status(hc_home, team, task_id, "in_approval", suppress_log=True)
            # Assign back to manager so the merge worker picks it up
            assign_task(hc_home, team, task_id, manager, suppress_log=True)
            logger.info(
                "%s: retryable failure (%s), attempt %d/%d — will retry",
                format_task_id(task_id), reason.name,
                current_attempts, MAX_MERGE_ATTEMPTS,
            )
            return

        # Max retries exhausted → escalate
        logger.warning(
            "%s: retryable failure (%s) but max attempts (%d) reached — escalating",
            format_task_id(task_id), reason.name, MAX_MERGE_ATTEMPTS,
        )

    # Escalate: merge_failed + assign to manager + notify
    update_task(hc_home, team, task_id, status_detail=detail)
    transition_task(hc_home, team, task_id, "merge_failed", manager)
    notify_conflict(
        hc_home, team, task,
        conflict_details=f"{detail}: {result.message[:500]}",
    )


def merge_once(hc_home: Path, team: str) -> list[MergeResult]:
    """Scan for tasks ready to merge and process them.

    A task is ready to merge if:
    - status == 'in_approval'
    - has an approved review verdict (for manual-approval repos)
    - OR the repo has approval == 'auto'

    When a task enters ``merging``, the assignee is switched to the
    manager (since the merge worker acts on the manager's behalf).

    On failure, ``_handle_merge_failure()`` routes the failure based on
    the ``MergeFailureReason``: retryable failures are silently retried
    (up to ``MAX_MERGE_ATTEMPTS``), while non-retryable failures are
    escalated to the manager.

    Returns list of merge results.
    """
    tasks = list_tasks(hc_home, team, status="in_approval")
    results = []
    manager = _get_manager_name(hc_home, team)

    for task in tasks:
        task_id = task["id"]
        repos: list[str] = task.get("repo", [])

        if not repos:
            # No repo — can't auto-merge, just skip
            continue

        # Check approval mode — use the first repo's setting (most common case)
        approval_mode = get_repo_approval(hc_home, team, repos[0])

        ready = False
        if approval_mode == "auto":
            ready = True
        elif approval_mode == "manual":
            review = get_current_review(hc_home, team, task_id)
            if review and review.get("verdict") == "approved":
                ready = True
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

        if not ready:
            continue

        # Transition to merging with assignee = manager
        transition_task(hc_home, team, task_id, "merging", manager)

        result = merge_task(hc_home, team, task_id)
        results.append(result)

        if not result.success:
            _handle_merge_failure(hc_home, team, task_id, result)

    return results
