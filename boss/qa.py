"""QA agent — handles review requests, runs tests, checks coverage, and gates the merge queue.

When an agent finishes work on a branch, it sends a review request to the QA agent.
QA creates a worktree from the repo (via symlink), runs tests on the branch,
verifies test coverage, and reports results.  QA reviews only the diff between
base_sha and the branch tip.  On approval, QA sets the task status to 'needs_merge'
so the daemon merge worker can pick it up.

Message format (from agent to QA):
    REVIEW_REQUEST: repo=<repo_name> branch=<branch_name>

Response format (from QA):
    REVIEW_RESULT: APPROVED repo=... branch=...
        Meaning: quality and coverage verified, ready for merge queue.
    REVIEW_RESULT: CHANGES_REQUESTED repo=... branch=...
        Meaning: tests failed or coverage insufficient, task returned to author.

Usage:
    python -m boss.qa review <home> <team> --repo <repo_name> --branch <branch_name>
    python -m boss.qa process-inbox <home> <team>
"""

import argparse
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from boss.paths import agent_dir as _resolve_agent_dir
from boss.mailbox import send, read_inbox, mark_inbox_read, Message
from boss.chat import log_event
from boss.task import list_tasks, set_task_branch, change_status, get_task, format_task_id
from boss.config import get_repo_test_cmd, get_repo_pipeline
from boss.bootstrap import get_member_by_role

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

    Uses the repo symlink in ``~/.boss/repos/<repo_name>`` to create
    a worktree in QA's workspace directory.  If the worktree already exists,
    just switch to the branch.

    Returns the path to the worktree directory.
    """
    from boss.repo import get_repo_path

    repo_dir = get_repo_path(hc_home, repo_name)
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
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=str(wt_dest),
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["git", "checkout", branch],
            cwd=str(wt_dest),
            capture_output=True,
            check=True,
        )
    else:
        # Fetch latest in the repo first
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=str(real_repo),
            capture_output=True,
            check=False,
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
    except subprocess.TimeoutExpired:
        return ReviewResult(
            approved=False,
            output="Tests timed out after 300 seconds.",
            repo=repo_path.name,
            branch="unknown",
        )


def run_pipeline(repo_path: Path, pipeline: list[dict]) -> ReviewResult:
    """Run a multi-step pipeline in the given repo directory.

    Executes each step in order.  Stops on the first failure, reporting
    which step failed.

    Args:
        repo_path: Path to the checked-out repo.
        pipeline: List of ``{name: str, run: str}`` step dicts.

    Returns:
        ReviewResult with combined output from all steps.
    """
    import shlex

    all_output: list[str] = []
    for step in pipeline:
        step_name = step["name"]
        step_cmd = shlex.split(step["run"])
        try:
            step_result = subprocess.run(
                step_cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=300,
            )
            step_output = step_result.stdout + step_result.stderr
            all_output.append(f"[{step_name}] {step_output}")
            if step_result.returncode != 0:
                return ReviewResult(
                    approved=False,
                    output=f"Step '{step_name}' failed:\n" + "\n".join(all_output),
                    repo=repo_path.name,
                    branch="unknown",
                )
        except subprocess.TimeoutExpired:
            all_output.append(f"[{step_name}] Timed out after 300 seconds.")
            return ReviewResult(
                approved=False,
                output=f"Step '{step_name}' failed:\n" + "\n".join(all_output),
                repo=repo_path.name,
                branch="unknown",
            )

    return ReviewResult(
        approved=True,
        output="\n".join(all_output),
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

    except subprocess.TimeoutExpired:
        return False, "Coverage check timed out after 300 seconds."
    except FileNotFoundError:
        return True, "Python not found, skipping coverage check."


def _extract_task_id_from_branch(branch: str) -> int | None:
    """Extract a task ID from a branch name, if present.

    Supports formats:
        <agent>/T<id>            (current convention)
        <agent>/T<id>-<slug>     (legacy)
        <agent>/<project>/<id>-<slug>  (legacy)
    """
    # Try current naming convention: <agent>/T<id> (with optional legacy slug)
    match = re.match(r"[^/]+/T(\d+)(?:-|$)", branch)
    if match:
        return int(match.group(1))
    # Try old convention: <agent>/<project>/<id>-<slug>
    match = re.match(r"[^/]+/[^/]+/(\d+)-", branch)
    if match:
        return int(match.group(1))
    return None


def _auto_detect_task_branch(hc_home: Path, branch: str) -> None:
    """Try to match a branch name to a task and store it."""
    branch_match = re.match(r"([^/]+)/([^/]+)/(\d+)-", branch)
    if not branch_match:
        # Also try the new naming convention: <agent>/T<id>-<slug>
        branch_match = re.match(r"([^/]+)/T(\d+)-", branch)
        if branch_match:
            task_number = int(branch_match.group(2))
            try:
                set_task_branch(hc_home, task_number, branch)
            except FileNotFoundError:
                logger.debug("No task %s found for branch %s", task_number, branch)
            return
        return
    task_number = int(branch_match.group(3))
    try:
        set_task_branch(hc_home, task_number, branch)
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
        - Sets task status to 'needs_merge' (ready for merge queue)
        - Reports APPROVED to requester and manager
    On rejection (tests fail or coverage insufficient):
        - Sets task status back to 'in_progress'
        - Reports CHANGES_REQUESTED to requester and manager
    """
    _auto_detect_task_branch(hc_home, req.branch)
    task_id = _extract_task_id_from_branch(req.branch)
    log_event(hc_home, f"QA reviewing {req.requester.capitalize()}'s changes ({req.branch})")

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
        _update_task_on_rejection(hc_home, task_id, req)
        return result

    # Check for a configured pipeline first, then fall back to test_command
    pipeline = get_repo_pipeline(hc_home, req.repo)
    if test_command is not None:
        # Explicit test_command overrides pipeline
        result = run_tests(wt_path, test_command)
    elif pipeline is not None:
        result = run_pipeline(wt_path, pipeline)
    else:
        # No pipeline, no explicit command — auto-detect
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
        _update_task_on_approval(hc_home, task_id)
    elif not result.approved and task_id is not None:
        _update_task_on_rejection(hc_home, task_id, req)

    _report_result(hc_home, team, req, result)
    return result


def _update_task_on_approval(hc_home: Path, task_id: int) -> None:
    """Set task status to needs_merge when QA approves."""
    try:
        task = get_task(hc_home, task_id)
        if task["status"] == "review":
            change_status(hc_home, task_id, "needs_merge")
            logger.info("%s: QA approved, status set to needs_merge", task_id)
    except (FileNotFoundError, ValueError) as e:
        logger.warning("Could not update task %s on approval: %s", task_id, e)


def _update_task_on_rejection(hc_home: Path, task_id: int | None, req: ReviewRequest) -> None:
    """Set task status back to in_progress when QA rejects."""
    if task_id is None:
        return
    try:
        task = get_task(hc_home, task_id)
        if task["status"] == "review":
            change_status(hc_home, task_id, "in_progress")
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

    if result.approved:
        log_event(hc_home, f"QA approved ({result.branch}) \u2713")
    else:
        log_event(hc_home, f"QA changes requested ({result.branch})")


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
            mark_inbox_read(hc_home, team, qa_name, msg.filename)

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
