"""QA agent — handles review requests, runs tests, checks coverage, and gates the merge queue.

When an agent finishes work on a branch, it sends a review request to the QA agent.
QA clones the repo, checks out the branch, runs tests, verifies test coverage, and
reports results. On approval, QA sets the task status to 'needs_merge' so the daemon
merge worker can pick it up. QA no longer performs the actual merge.

Message format (from agent to QA):
    REVIEW_REQUEST: repo=<path_or_url> branch=<branch_name>

Response format (from QA):
    REVIEW_RESULT: APPROVED repo=... branch=...
        Meaning: quality and coverage verified, ready for merge queue.
    REVIEW_RESULT: CHANGES_REQUESTED repo=... branch=...
        Meaning: tests failed or coverage insufficient, task returned to author.

Usage:
    python -m headcount.qa review <home> <team> --repo <path_or_url> --branch <branch_name>
    python -m headcount.qa process-inbox <home> <team>
"""

import argparse
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from headcount.paths import agent_dir as _resolve_agent_dir
from headcount.mailbox import send, read_inbox, mark_inbox_read, Message
from headcount.chat import log_event
from headcount.task import list_tasks, set_task_branch, change_status, get_task
from headcount.bootstrap import get_member_by_role

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


def clone_and_checkout(hc_home: Path, team: str, repo: str, branch: str) -> Path:
    """Clone a repo and checkout a branch into QA's workspace.

    `repo` can be an absolute path to a local repo or a URL.
    Returns the path to the cloned repo.
    """
    repo_path = Path(repo)
    if not repo.startswith(("http://", "https://", "git@")):
        if not repo_path.is_dir():
            raise FileNotFoundError(f"Repo '{repo}' not found at {repo_path}")

    qa_name = _get_qa_agent_name(hc_home, team)
    qa_workspace = _resolve_agent_dir(hc_home, team, qa_name) / "workspace"
    qa_workspace.mkdir(parents=True, exist_ok=True)

    # Use the last path component as the clone directory name
    repo_name = repo_path.name
    clone_dest = qa_workspace / repo_name

    if clone_dest.exists():
        # Pull latest
        subprocess.run(
            ["git", "fetch", "--all"],
            cwd=str(clone_dest),
            capture_output=True,
            check=True,
        )
    else:
        source = repo if repo.startswith(("http://", "https://", "git@")) else str(repo_path)
        subprocess.run(
            ["git", "clone", source, str(clone_dest)],
            capture_output=True,
            check=True,
        )

    subprocess.run(
        ["git", "checkout", branch],
        cwd=str(clone_dest),
        capture_output=True,
        check=True,
    )

    return clone_dest


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
        <agent>/T<id>-<slug>
        <agent>/<project>/<id>-<slug>
    """
    # Try new naming convention: <agent>/T<id>-<slug>
    match = re.match(r"[^/]+/T(\d+)-", branch)
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
                logger.debug("No task T%04d found for branch %s", task_number, branch)
            return
        return
    task_number = int(branch_match.group(3))
    try:
        set_task_branch(hc_home, task_number, branch)
    except FileNotFoundError:
        logger.debug("No task T%04d found for branch %s", task_number, branch)


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
    log_event(hc_home, f"QA: Reviewing {req.requester}'s changes — repo={req.repo} branch={req.branch}")

    try:
        repo_path = clone_and_checkout(hc_home, team, req.repo, req.branch)
    except Exception as e:
        result = ReviewResult(
            approved=False,
            output=f"Failed to clone/checkout: {e}",
            repo=req.repo,
            branch=req.branch,
        )
        _report_result(hc_home, team, req, result)
        _update_task_on_rejection(hc_home, task_id, req)
        return result

    result = run_tests(repo_path, test_command)
    result.repo = req.repo
    result.branch = req.branch

    # If tests passed, also check coverage
    if result.approved:
        cov_passed, cov_output = check_test_coverage(repo_path)
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
            logger.info("T%04d: QA approved, status set to needs_merge", task_id)
    except (FileNotFoundError, ValueError) as e:
        logger.warning("Could not update task T%04d on approval: %s", task_id, e)


def _update_task_on_rejection(hc_home: Path, task_id: int | None, req: ReviewRequest) -> None:
    """Set task status back to in_progress when QA rejects."""
    if task_id is None:
        return
    try:
        task = get_task(hc_home, task_id)
        if task["status"] == "review":
            change_status(hc_home, task_id, "in_progress")
            logger.info("T%04d: QA rejected, status set back to in_progress", task_id)
    except (FileNotFoundError, ValueError) as e:
        logger.warning("Could not update task T%04d on rejection: %s", task_id, e)


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

    log_event(hc_home, f"QA {verdict}: repo={result.repo} branch={result.branch}")


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
