"""QA agent — handles review requests, runs tests, and provides quality feedback.

When an agent finishes work on a branch, it sends a review request to the QA agent.
QA clones the repo, checks out the branch, runs tests/inspections, and reports results.

Message format (from agent to QA):
    REVIEW_REQUEST: repo=<path_or_url> branch=<branch_name>

Response format (from QA):
    REVIEW_RESULT: APPROVED repo=... branch=...
    REVIEW_RESULT: CHANGES_REQUESTED repo=... branch=...

Usage:
    python scripts/qa.py review <root> --repo <path_or_url> --branch <branch_name>
    python scripts/qa.py process-inbox <root>
"""

import argparse
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.mailbox import send, read_inbox, mark_inbox_read, Message
from scripts.chat import log_event
from scripts.task import list_tasks, set_task_branch

logger = logging.getLogger(__name__)

QA_AGENT = "qa"

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


def clone_and_checkout(root: Path, repo: str, branch: str) -> Path:
    """Clone a repo and checkout a branch into QA's workspace.

    `repo` can be an absolute path to a local repo or a URL.
    Returns the path to the cloned repo.
    """
    repo_path = Path(repo)
    if not repo.startswith(("http://", "https://", "git@")):
        if not repo_path.is_dir():
            raise FileNotFoundError(f"Repo '{repo}' not found at {repo_path}")

    qa_workspace = root / ".standup" / "team" / QA_AGENT / "workspace"
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
    """Run tests in the given repo directory.

    If no test_command is specified, looks for common test runners.
    """
    if test_command is None:
        # Auto-detect test command
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


def _auto_detect_task_branch(root: Path, branch: str) -> None:
    """Try to match a branch name to a task and store it.

    Branch pattern: <agent>/<project>/<task_number>-<short-name>
    """
    # Parse the branch name: e.g. "alice/backend/0034-relative-timestamps"
    branch_match = re.match(r"([^/]+)/([^/]+)/(\d+)-", branch)
    if not branch_match:
        return
    task_number = int(branch_match.group(3))
    try:
        set_task_branch(root, task_number, branch)
    except FileNotFoundError:
        logger.debug("No task T%04d found for branch %s", task_number, branch)


def handle_review_request(
    root: Path,
    req: ReviewRequest,
    test_command: str | None = None,
) -> ReviewResult:
    """Full QA pipeline: clone, checkout, test, report."""
    _auto_detect_task_branch(root, req.branch)
    log_event(root, f"QA: Reviewing {req.requester}'s changes — repo={req.repo} branch={req.branch}")

    try:
        repo_path = clone_and_checkout(root, req.repo, req.branch)
    except Exception as e:
        result = ReviewResult(
            approved=False,
            output=f"Failed to clone/checkout: {e}",
            repo=req.repo,
            branch=req.branch,
        )
        _report_result(root, req, result)
        return result

    result = run_tests(repo_path, test_command)
    result.repo = req.repo
    result.branch = req.branch

    _report_result(root, req, result)
    return result


def _report_result(root: Path, req: ReviewRequest, result: ReviewResult) -> None:
    """Send review results back to the requester and manager."""
    verdict = "APPROVED" if result.approved else "CHANGES_REQUESTED"
    summary = f"REVIEW_RESULT: {verdict} repo={result.repo} branch={result.branch}"
    detail = result.output[:500] if result.output else "(no output)"
    message = f"{summary}\n\n{detail}"

    # Report to requester
    try:
        send(root, QA_AGENT, req.requester, message)
    except ValueError:
        logger.warning("Could not send result to %s", req.requester)

    # Report to manager
    try:
        send(root, QA_AGENT, "manager", message)
    except ValueError:
        logger.warning("Could not send result to manager")

    log_event(root, f"QA {verdict}: repo={result.repo} branch={result.branch}")


def process_inbox(root: Path) -> list[ReviewResult]:
    """Process all pending review requests in QA agent's inbox."""
    messages = read_inbox(root, QA_AGENT, unread_only=True)
    results = []

    for msg in messages:
        req = parse_review_request(msg)
        if req:
            result = handle_review_request(root, req)
            results.append(result)
        else:
            logger.warning("Unrecognized message in QA inbox: %s", msg.body[:100])

        if msg.filename:
            mark_inbox_read(root, QA_AGENT, msg.filename)

    return results


def main():
    parser = argparse.ArgumentParser(description="QA agent")
    sub = parser.add_subparsers(dest="command", required=True)

    # review
    p_review = sub.add_parser("review", help="Review a specific branch")
    p_review.add_argument("root", type=Path)
    p_review.add_argument("--repo", required=True, help="Path or URL of the repo")
    p_review.add_argument("--branch", required=True)
    p_review.add_argument("--test-command")

    # process-inbox
    p_inbox = sub.add_parser("process-inbox", help="Process all pending review requests")
    p_inbox.add_argument("root", type=Path)

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.command == "review":
        req = ReviewRequest(repo=args.repo, branch=args.branch, requester="manual")
        result = handle_review_request(args.root, req, test_command=args.test_command)
        verdict = "APPROVED" if result.approved else "CHANGES_REQUESTED"
        print(f"QA {verdict}: {result.output[:200]}")

    elif args.command == "process-inbox":
        results = process_inbox(args.root)
        for r in results:
            verdict = "APPROVED" if r.approved else "CHANGES_REQUESTED"
            print(f"  {r.repo}/{r.branch}: {verdict}")
        if not results:
            print("No review requests to process.")


if __name__ == "__main__":
    main()
