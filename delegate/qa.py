"""QA utilities — test running, coverage checking, and branch checkout helpers.

By default, peer reviewers handle both code review and QA duties (running
tests, checking coverage, verifying behavior).  A dedicated QA agent role
is optional — teams that want one can assign agents the ``qa`` role and use
``roles/qa.md`` from the charter.

The utility functions here (``run_tests``, ``checkout_branch``,
``check_test_coverage``, ``run_pipeline``) can be used by any reviewer.

Usage:
    python -m delegate.qa review <home> <team> --repo <repo_name> --branch <branch_name>
    python -m delegate.qa process-inbox <home> <team>
"""

import argparse
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from delegate.paths import agent_dir as _resolve_agent_dir
from delegate.mailbox import send, read_inbox, mark_processed, Message
from delegate.chat import log_event
from delegate.task import list_tasks, set_task_branch, change_status, get_task, format_task_id
from delegate.config import get_pre_merge_script
from delegate.bootstrap import get_member_by_role

logger = logging.getLogger(__name__)


def _get_qa_agent_name(hc_home: Path, team: str) -> str:
    """Look up the QA agent by role (state.yaml role: qa)."""
    name = get_member_by_role(hc_home, team, "qa")
    return name or "qa"


REVIEW_REQUEST_PATTERN = re.compile(
    r"REVIEW_REQUEST:\s*repo=(\S+)\s+branch=(\S+)"
)


@dataclass
class ReviewRequest:
    repo: str
    branch: str
    requester: str


@dataclass
class ReviewResult:
    approved: bool
    output: str
    repo: str
    branch: str
    coverage_passed: bool = True
    coverage_output: str = ""


def parse_review_request(msg: Message) -> ReviewRequest | None:
    """Try to parse a review request from an inbox message."""
    match = REVIEW_REQUEST_PATTERN.search(msg.body)
    if match:
        return ReviewRequest(
            repo=match.group(1),
            branch=match.group(2),
            requester=msg.sender,
        )
    return None


def checkout_branch(hc_home: Path, team: str, repo_name: str, branch: str) -> Path:
    """Create a QA worktree for reviewing a branch.

    Uses the repo symlink in ``~/.delegate/repos/<repo_name>`` to create
    a worktree in QA's workspace directory.  If the worktree already exists,
    just switch to the branch.

    Returns the path to the worktree directory.
    """
    from delegate.repo import get_repo_path

    repo_dir = get_repo_path(hc_home, team, repo_name)
    real_repo = repo_dir.resolve()
    if not real_repo.is_dir():
        raise FileNotFoundError(f"Repo '{repo_name}' not found at {real_repo}")

    qa_name = _get_qa_agent_name(hc_home, team)
    qa_workspace = _resolve_agent_dir(hc_home, team, qa_name) / "worktrees"
    qa_workspace.mkdir(parents=True, exist_ok=True)

    # Use a stable name based on the branch
    safe_branch = branch.replace("/", "_")
    wt_dest = qa_workspace / f"{repo_name}-{safe_branch}"

    if wt_dest.exists():
        # Already exists — fetch and check out branch
        fetch_result = subprocess.run(
            ["git", "fetch", "--all"],
            cwd=str(wt_dest),
            capture_output=True,
            check=False,
            text=True,
        )
        if fetch_result.returncode != 0:
            logger.debug(
                "git fetch failed in worktree %s (returncode %d): %s",
                wt_dest, fetch_result.returncode, fetch_result.stderr
            )
        subprocess.run(
            ["git", "checkout", branch],
            cwd=str(wt_dest),
            capture_output=True,
            check=True,
        )
    else:
        # Fetch latest in the repo first
        fetch_result = subprocess.run(
            ["git", "fetch", "--all"],
            cwd=str(real_repo),
            capture_output=True,
            check=False,
            text=True,
        )
        if fetch_result.returncode != 0:
            logger.debug(
                "git fetch failed in repo %s (returncode %d): %s",
                real_repo, fetch_result.returncode, fetch_result.stderr
            )
        # Create worktree
        subprocess.run(
            ["git", "worktree", "add", str(wt_dest), branch],
            cwd=str(real_repo),
            capture_output=True,
            check=True,
        )

    return wt_dest


# Keep old name as alias for backward compatibility in tests
clone_and_checkout = checkout_branch


def run_tests(repo_path: Path, test_command: str | None = None) -> ReviewResult:
    """Run tests in the given repo directory."""
    if test_command is None:
        if (repo_path / "pyproject.toml").exists() or (repo_path / "tests").is_dir():
            test_command = "python -m pytest -v"
        elif (repo_path / "package.json").exists():
            test_command = "npm test"
        elif (repo_path / "Makefile").exists():
            test_command = "make test"
        else:
            return ReviewResult(
                approved=True,
                output="No test runner detected, skipping tests.",
                repo=repo_path.name,
                branch="unknown",
            )

    try:
        result = subprocess.run(
            test_command.split(),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=300,
        )
        return ReviewResult(
            approved=result.returncode == 0,
            output=result.stdout + result.stderr,
            repo=repo_path.name,
            branch="unknown",
        )
    except subprocess.TimeoutExpired as e:
        logger.warning(
            "Test command timed out after 300s in %s: %s",
            repo_path.name, test_command
        )
        return ReviewResult(
            approved=False,
            output="Tests timed out after 300 seconds.",
            repo=repo_path.name,
            branch="unknown",
        )


def run_pre_merge_script(repo_path: Path, script: str) -> ReviewResult:
    """Run a pre-merge script in the given repo directory.

    Args:
        repo_path: Path to the checked-out repo.
        script: Shell command to execute before merge.

    Returns:
        ReviewResult with output from the script.
    """
    import shlex

    cmd = shlex.split(script)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = result.stdout + result.stderr
        return ReviewResult(
            approved=result.returncode == 0,
            output=output,
            repo=repo_path.name,
            branch="unknown",
        )
    except subprocess.TimeoutExpired as e:
        logger.warning(
            "Pre-merge script timed out after 600s in %s: %s",
            repo_path.name, script
        )
        return ReviewResult(
            approved=False,
            output="Pre-merge script timed out after 600 seconds.",
            repo=repo_path.name,
            branch="unknown",
        )


MIN_COVERAGE_PERCENT = 60


def check_test_coverage(repo_path: Path, min_coverage: int = MIN_COVERAGE_PERCENT) -> tuple[bool, str]:
    """Check test coverage in the repo using pytest-cov.

    Returns (passed, output) where passed is True if coverage meets the minimum
    threshold or if coverage tools are not available.
    """
    if not ((repo_path / "pyproject.toml").exists() or (repo_path / "tests").is_dir()):
        return True, "No Python project detected, skipping coverage check."

    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "--cov=.", "--cov-report=term-missing", "-q"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr

        # Parse total coverage percentage from pytest-cov output
        # Format: "TOTAL    123    45    63%"
        for line in output.splitlines():
            if line.startswith("TOTAL") or line.startswith("TOTAL "):
                parts = line.split()
                for part in parts:
                    if part.endswith("%"):
                        try:
                            pct = int(part.rstrip("%"))
                            if pct >= min_coverage:
                                return True, f"Coverage: {pct}% (minimum: {min_coverage}%)\n{output}"
                            else:
                                return False, (
                                    f"Coverage: {pct}% is below minimum {min_coverage}%. "
                                    f"Please add tests to improve coverage.\n{output}"
                                )
                        except ValueError:
                            continue

        # If we couldn't parse coverage (e.g. pytest-cov not installed), pass gracefully
        if "no module named" in output.lower() or "coverage" not in output.lower():
            return True, "Coverage tools not available, skipping coverage check."

        return True, f"Could not parse coverage percentage.\n{output}"

    except subprocess.TimeoutExpired as e:
        logger.warning(
            "Coverage check timed out after 300s in %s: pytest --cov",
            repo_path.name
        )
        return False, "Coverage check timed out after 300 seconds."
    except FileNotFoundError as e:
        logger.debug(
            "Python executable not found when running coverage check in %s",
            repo_path.name
        )
        return True, "Python not found, skipping coverage check."


def _extract_task_id_from_branch(branch: str) -> int | None:
    """Extract a task ID from a branch name, if present.

    Supports formats:
        delegate/<team_id>/<team>/T<id>  (current convention)
        delegate/<team>/T<id>            (legacy)
        <agent>/T<id>                    (legacy)
        <agent>/T<id>-<slug>             (legacy)
        <agent>/<project>/<id>-<slug>    (legacy)
    """
    # Try current convention: delegate/<team_id>/<team>/T<id>
    # Also matches legacy: delegate/<team>/T<id>
    match = re.match(r"delegate/[^/]+/(?:[^/]+/)?T(\d+)(?:-|$)", branch)
    if match:
        return int(match.group(1))
    # Try legacy naming convention: <agent>/T<id> (with optional legacy slug)
    match = re.match(r"[^/]+/T(\d+)(?:-|$)", branch)
    if match:
        return int(match.group(1))
    # Try old convention: <agent>/<project>/<id>-<slug>
    match = re.match(r"[^/]+/[^/]+/(\d+)-", branch)
    if match:
        return int(match.group(1))
    return None


def _auto_detect_task_branch(hc_home: Path, team: str, branch: str) -> None:
    """Try to match a branch name to a task and store it."""
    branch_match = re.match(r"([^/]+)/([^/]+)/(\d+)-", branch)
    if not branch_match:
        # Also try the new naming convention: <team>/T<id>
        branch_match = re.match(r"([^/]+)/T(\d+)", branch)
        if branch_match:
            task_number = int(branch_match.group(2))
            try:
                set_task_branch(hc_home, team, task_number, branch)
            except FileNotFoundError:
                logger.debug("No task %s found for branch %s", task_number, branch)
            return
        return
    task_number = int(branch_match.group(3))
    try:
        set_task_branch(hc_home, team, task_number, branch)
    except FileNotFoundError:
        logger.debug("No task %s found for branch %s", task_number, branch)


def handle_review_request(
    hc_home: Path,
    team: str,
    req: ReviewRequest,
    test_command: str | None = None,
) -> ReviewResult:
    """Full QA pipeline: clone, checkout, test, check coverage, update task status, report.

    On approval (tests pass + coverage sufficient):
        - Sets task status to 'in_approval' (ready for merge queue)
        - Reports APPROVED to requester and manager
    On rejection (tests fail or coverage insufficient):
        - Sets task status back to 'in_progress'
        - Reports CHANGES_REQUESTED to requester and manager
    """
    _auto_detect_task_branch(hc_home, team, req.branch)
    task_id = _extract_task_id_from_branch(req.branch)
    log_event(hc_home, team, f"QA reviewing {req.requester.capitalize()}'s changes ({req.branch})", task_id=task_id)

    try:
        wt_path = checkout_branch(hc_home, team, req.repo, req.branch)
    except Exception as e:
        result = ReviewResult(
            approved=False,
            output=f"Failed to checkout branch: {e}",
            repo=req.repo,
            branch=req.branch,
        )
        _report_result(hc_home, team, req, result)
        _update_task_on_rejection(hc_home, team, task_id, req)
        return result

    # Check for a configured pre-merge script, then fall back to test_command
    script = get_pre_merge_script(hc_home, team, req.repo)
    if test_command is not None:
        # Explicit test_command overrides pre-merge script
        result = run_tests(wt_path, test_command)
    elif script is not None:
        result = run_pre_merge_script(wt_path, script)
    else:
        # No script, no explicit command — auto-detect
        result = run_tests(wt_path)
    result.repo = req.repo
    result.branch = req.branch

    # If tests passed, also check coverage
    if result.approved:
        cov_passed, cov_output = check_test_coverage(wt_path)
        result.coverage_passed = cov_passed
        result.coverage_output = cov_output
        if not cov_passed:
            result.approved = False
            result.output = result.output + f"\n\nCoverage check failed:\n{cov_output}"

    # Update task status based on result
    if result.approved and task_id is not None:
        _update_task_on_approval(hc_home, team, task_id)
    elif not result.approved and task_id is not None:
        _update_task_on_rejection(hc_home, team, task_id, req)

    _report_result(hc_home, team, req, result)
    return result


def _update_task_on_approval(hc_home: Path, team: str, task_id: int) -> None:
    """Set task status to in_approval when QA approves."""
    try:
        task = get_task(hc_home, team, task_id)
        if task["status"] == "in_review":
            change_status(hc_home, team, task_id, "in_approval")
            logger.info("%s: QA approved, status set to in_approval", task_id)
    except (FileNotFoundError, ValueError) as e:
        logger.warning("Could not update task %s on approval: %s", task_id, e)


def _update_task_on_rejection(hc_home: Path, team: str, task_id: int | None, req: ReviewRequest) -> None:
    """Set task status back to in_progress when QA rejects."""
    if task_id is None:
        return
    try:
        task = get_task(hc_home, team, task_id)
        if task["status"] == "in_review":
            change_status(hc_home, team, task_id, "in_progress")
            logger.info("%s: QA rejected, status set back to in_progress", task_id)
    except (FileNotFoundError, ValueError) as e:
        logger.warning("Could not update task %s on rejection: %s", task_id, e)


def _report_result(hc_home: Path, team: str, req: ReviewRequest, result: ReviewResult) -> None:
    """Send review results back to the requester and manager."""
    qa_name = _get_qa_agent_name(hc_home, team)
    verdict = "APPROVED" if result.approved else "CHANGES_REQUESTED"
    summary = f"REVIEW_RESULT: {verdict} repo={result.repo} branch={result.branch}"

    if result.approved:
        detail = "Quality and coverage verified, ready for merge queue."
        if result.coverage_output:
            detail += f"\n\n{result.coverage_output[:300]}"
    else:
        detail = result.output[:500] if result.output else "(no output)"
        if result.coverage_output and not result.coverage_passed:
            detail += f"\n\nCoverage issue: {result.coverage_output[:300]}"

    message = f"{summary}\n\n{detail}"

    # Report to requester
    try:
        send(hc_home, team, qa_name, req.requester, message)
    except ValueError:
        logger.warning("Could not send result to %s", req.requester)

    # Report to manager
    manager_name = get_member_by_role(hc_home, team, "manager") or "manager"
    try:
        send(hc_home, team, qa_name, manager_name, message)
    except ValueError:
        logger.warning("Could not send result to manager")

    task_id = _extract_task_id_from_branch(result.branch)
    if result.approved:
        log_event(hc_home, team, f"QA approved ({result.branch}) \u2713", task_id=task_id)
    else:
        log_event(hc_home, team, f"QA changes requested ({result.branch})", task_id=task_id)


def process_inbox(hc_home: Path, team: str) -> list[ReviewResult]:
    """Process all pending review requests in QA agent's inbox."""
    qa_name = _get_qa_agent_name(hc_home, team)
    messages = read_inbox(hc_home, team, qa_name, unread_only=True)
    results = []

    for msg in messages:
        req = parse_review_request(msg)
        if req:
            result = handle_review_request(hc_home, team, req)
            results.append(result)
        else:
            logger.warning("Unrecognized message in QA inbox: %s", msg.body[:100])

        if msg.filename:
            mark_processed(hc_home, team, msg.filename)

    return results


def main():
    parser = argparse.ArgumentParser(description="QA agent")
    sub = parser.add_subparsers(dest="command", required=True)

    # review
    p_review = sub.add_parser("review", help="Review a specific branch")
    p_review.add_argument("home", type=Path)
    p_review.add_argument("team")
    p_review.add_argument("--repo", required=True, help="Path or URL of the repo")
    p_review.add_argument("--branch", required=True)
    p_review.add_argument("--test-command")

    # process-inbox
    p_inbox = sub.add_parser("process-inbox", help="Process all pending review requests")
    p_inbox.add_argument("home", type=Path)
    p_inbox.add_argument("team")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.command == "review":
        req = ReviewRequest(repo=args.repo, branch=args.branch, requester="manual")
        result = handle_review_request(args.home, args.team, req, test_command=args.test_command)
        verdict = "APPROVED" if result.approved else "CHANGES_REQUESTED"
        print(f"QA {verdict}: {result.output[:200]}")

    elif args.command == "process-inbox":
        results = process_inbox(args.home, args.team)
        for r in results:
            verdict = "APPROVED" if r.approved else "CHANGES_REQUESTED"
            print(f"  {r.repo}/{r.branch}: {verdict}")
        if not results:
            print("No review requests to process.")


if __name__ == "__main__":
    main()
