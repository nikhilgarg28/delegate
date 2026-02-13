"""Tests for delegate/qa.py — QA agent and review workflow."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from delegate.bootstrap import bootstrap
from delegate.config import set_boss
from delegate.qa import (
    parse_review_request,
    run_tests,
    handle_review_request,
    clone_and_checkout,
    check_test_coverage,
    _extract_task_id_from_branch,
    ReviewRequest,
    ReviewResult,
    MIN_COVERAGE_PERCENT,
)
from delegate.mailbox import Message, deliver, read_inbox, read_outbox
from delegate.chat import get_messages
from delegate.task import create_task, change_status, get_task, assign_task, format_task_id

TEAM = "qateam"


@pytest.fixture
def qa_team(tmp_path):
    """Bootstrap a team that includes a QA agent and create a test repo."""
    hc_home = tmp_path / "hc_home"
    set_boss(hc_home, "boss")
    bootstrap(hc_home, team_name=TEAM, manager="manager", agents=["alice", ("qa", "qa")])

    # Create a simple test repo outside the boss directory
    repo_dir = tmp_path / "repos" / "myapp"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo_dir), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_dir), capture_output=True, check=True,
    )

    # Add a simple Python file + test
    (repo_dir / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo_dir / "test_app.py").write_text(
        "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo_dir), capture_output=True, check=True,
    )

    # Create a feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature-xyz"],
        cwd=str(repo_dir), capture_output=True, check=True,
    )
    (repo_dir / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n"
    )
    (repo_dir / "test_app.py").write_text(
        "from app import add, multiply\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_multiply():\n    assert multiply(2, 3) == 6\n"
    )
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add multiply"],
        cwd=str(repo_dir), capture_output=True, check=True,
    )

    # Switch back to main
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=str(repo_dir), capture_output=True, check=True,
    )

    return hc_home, str(repo_dir)


@pytest.fixture
def qa_team_with_task(qa_team):
    """Extend qa_team with a task in 'in_review' status and a matching branch name."""
    hc_home, repo_path = qa_team

    # Create a task and move it through the workflow to 'in_review'
    task = create_task(hc_home, TEAM, title="Add multiply feature", assignee="manager", repo="myapp")
    assign_task(hc_home, TEAM, task["id"], "alice")
    change_status(hc_home, TEAM, task["id"], "in_progress")
    change_status(hc_home, TEAM, task["id"], "in_review")

    # Create a branch that matches the task ID pattern
    branch_name = f"alice/{format_task_id(task['id'])}"
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo_path, capture_output=True, check=True,
    )
    (Path(repo_path) / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n"
    )
    (Path(repo_path) / "test_app.py").write_text(
        "from app import add, multiply\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_multiply():\n    assert multiply(2, 3) == 6\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add multiply"],
        cwd=repo_path, capture_output=True, check=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=repo_path, capture_output=True, check=True)

    return hc_home, repo_path, task["id"], branch_name


class TestExtractTaskIdFromBranch:
    def test_current_convention(self):
        assert _extract_task_id_from_branch("alice/T0042") == 42

    def test_current_convention_large_id(self):
        assert _extract_task_id_from_branch("bob/T0123") == 123

    def test_legacy_convention_with_slug(self):
        assert _extract_task_id_from_branch("alice/T0042-add-feature") == 42

    def test_old_convention(self):
        assert _extract_task_id_from_branch("alice/backend/0007-build-api") == 7

    def test_no_match(self):
        assert _extract_task_id_from_branch("feature-xyz") is None

    def test_no_match_no_slash(self):
        assert _extract_task_id_from_branch("main") is None

    def test_delegate_team_convention(self):
        """Convention: delegate/<team_id>/<team>/T<NNNN>."""
        # Current format: delegate/<team_id>/<team>/T<NNN>
        assert _extract_task_id_from_branch("delegate/a1b2c3/myteam/T0042") == 42
        # Legacy format: delegate/<team>/T<NNN>
        assert _extract_task_id_from_branch("delegate/myteam/T0042") == 42


class TestParseReviewRequest:
    def test_valid_request(self):
        msg = Message(
            sender="alice",
            recipient="qa",
            time="t",
            body="REVIEW_REQUEST: repo=/path/to/myapp branch=feature-xyz",
        )
        req = parse_review_request(msg)
        assert req is not None
        assert req.repo == "/path/to/myapp"
        assert req.branch == "feature-xyz"
        assert req.requester == "alice"

    def test_invalid_message(self):
        msg = Message(
            sender="alice",
            recipient="qa",
            time="t",
            body="Hey QA, can you review my code?",
        )
        assert parse_review_request(msg) is None

    def test_embedded_in_longer_message(self):
        msg = Message(
            sender="alice",
            recipient="qa",
            time="t",
            body="I finished the feature.\nREVIEW_REQUEST: repo=/repos/myapp branch=feature-abc\nThanks!",
        )
        req = parse_review_request(msg)
        assert req is not None
        assert req.branch == "feature-abc"


class TestCloneAndCheckout:
    def test_clones_and_checks_out(self, qa_team):
        hc_home, repo_path = qa_team
        repo_dir = clone_and_checkout(hc_home, TEAM, repo_path, "feature-xyz")
        assert repo_dir.is_dir()
        assert (repo_dir / "app.py").exists()
        # Should have the multiply function from the feature branch
        content = (repo_dir / "app.py").read_text()
        assert "multiply" in content

    def test_nonexistent_repo_raises(self, qa_team):
        hc_home, _ = qa_team
        with pytest.raises(FileNotFoundError, match="not found"):
            clone_and_checkout(hc_home, TEAM, "/nonexistent/path/repo", "main")


class TestRunTests:
    def test_passing_tests(self, qa_team):
        hc_home, repo_path = qa_team
        repo_dir = clone_and_checkout(hc_home, TEAM, repo_path, "feature-xyz")
        result = run_tests(repo_dir, test_command=f"{sys.executable} -m pytest -v")
        assert result.approved
        assert "passed" in result.output.lower()

    def test_failing_tests(self, qa_team):
        hc_home, repo_path = qa_team
        repo_dir = clone_and_checkout(hc_home, TEAM, repo_path, "feature-xyz")
        # Break a test
        (repo_dir / "test_app.py").write_text(
            "def test_broken():\n    assert False\n"
        )
        result = run_tests(repo_dir, test_command=f"{sys.executable} -m pytest -v")
        assert not result.approved

    def test_no_test_runner(self, tmp_path):
        """An empty directory with no recognizable project structure."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = run_tests(empty_dir)
        assert result.approved  # skips gracefully
        assert "No test runner detected" in result.output


class TestHandleReviewRequest:
    def test_full_review_approved(self, qa_team):
        hc_home, repo_path = qa_team
        req = ReviewRequest(repo=repo_path, branch="feature-xyz", requester="alice")
        result = handle_review_request(hc_home, TEAM, req, test_command=f"{sys.executable} -m pytest -v")
        assert result.approved

        # QA should have sent results to alice and manager
        qa_outbox = read_outbox(hc_home, TEAM, "qa", pending_only=False)
        assert len(qa_outbox) >= 1

        # Event should be logged
        events = get_messages(hc_home, TEAM, msg_type="event")
        assert any("QA" in e["content"] for e in events)

    def test_full_review_changes_requested(self, qa_team):
        hc_home, repo_path = qa_team
        # Create a failing branch
        subprocess.run(["git", "checkout", "-b", "broken"], cwd=repo_path, capture_output=True, check=True)
        (Path(repo_path) / "test_app.py").write_text("def test_fail():\n    assert False\n")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "broken"], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo_path, capture_output=True, check=True)

        req = ReviewRequest(repo=repo_path, branch="broken", requester="alice")
        result = handle_review_request(hc_home, TEAM, req, test_command=f"{sys.executable} -m pytest -v")
        assert not result.approved

        events = get_messages(hc_home, TEAM, msg_type="event")
        assert any("changes requested" in e["content"] for e in events)

    def test_nonexistent_repo(self, qa_team):
        hc_home, _ = qa_team
        req = ReviewRequest(repo="/nonexistent/path", branch="main", requester="alice")
        result = handle_review_request(hc_home, TEAM, req)
        assert not result.approved
        assert "clone/checkout" in result.output.lower() or "not found" in result.output.lower()


class TestCheckTestCoverage:
    def test_no_python_project(self, tmp_path):
        """Non-Python bossies should pass gracefully."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        passed, output = check_test_coverage(empty_dir)
        assert passed
        assert "No Python project" in output

    @patch("delegate.qa.subprocess.run")
    def test_coverage_above_threshold(self, mock_run):
        """Coverage above minimum should pass."""
        mock_result = MagicMock()
        mock_result.stdout = "Name    Stmts   Miss  Cover\n-------\nTOTAL      100     20    80%\n"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        repo = Path("/fake/repo")
        # Need pyproject.toml to exist for the check to run
        with patch.object(Path, "exists", return_value=True):
            passed, output = check_test_coverage(repo, min_coverage=60)
        assert passed
        assert "80%" in output

    @patch("delegate.qa.subprocess.run")
    def test_coverage_below_threshold(self, mock_run):
        """Coverage below minimum should fail."""
        mock_result = MagicMock()
        mock_result.stdout = "Name    Stmts   Miss  Cover\n-------\nTOTAL      100     60    40%\n"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        repo = Path("/fake/repo")
        with patch.object(Path, "exists", return_value=True):
            passed, output = check_test_coverage(repo, min_coverage=60)
        assert not passed
        assert "40%" in output
        assert "below minimum" in output

    @patch("delegate.qa.subprocess.run")
    def test_coverage_tools_not_available(self, mock_run):
        """When pytest-cov is not installed, should pass gracefully."""
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "ERROR: No module named pytest_cov"
        mock_run.return_value = mock_result

        repo = Path("/fake/repo")
        with patch.object(Path, "exists", return_value=True):
            passed, output = check_test_coverage(repo)
        assert passed
        assert "not available" in output.lower() or "skipping" in output.lower()

    @patch("delegate.qa.subprocess.run")
    def test_coverage_timeout(self, mock_run):
        """Timeout should fail."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=300)

        repo = Path("/fake/repo")
        with patch.object(Path, "exists", return_value=True):
            passed, output = check_test_coverage(repo)
        assert not passed
        assert "timed out" in output.lower()


class TestTaskStatusTransitions:
    def test_approval_sets_in_approval(self, qa_team_with_task):
        """When QA approves, task status should transition to in_approval."""
        hc_home, repo_path, task_id, branch_name = qa_team_with_task
        req = ReviewRequest(repo=repo_path, branch=branch_name, requester="alice")

        # Mock coverage check to pass (avoid needing pytest-cov in test repo)
        with patch("delegate.qa.check_test_coverage", return_value=(True, "Coverage: 85% (minimum: 60%)")):
            result = handle_review_request(hc_home, TEAM, req, test_command=f"{sys.executable} -m pytest -v")

        assert result.approved
        task = get_task(hc_home, TEAM, task_id)
        assert task["status"] == "in_approval"

    def test_rejection_sets_in_progress(self, qa_team_with_task):
        """When QA rejects (tests fail), task status should go back to in_progress."""
        hc_home, repo_path, task_id, branch_name = qa_team_with_task

        # Create a broken branch with the same task ID pattern
        subprocess.run(
            ["git", "checkout", branch_name],
            cwd=repo_path, capture_output=True, check=True,
        )
        (Path(repo_path) / "test_app.py").write_text("def test_fail():\n    assert False\n")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Break tests"],
            cwd=repo_path, capture_output=True, check=True,
        )
        subprocess.run(["git", "checkout", "main"], cwd=repo_path, capture_output=True, check=True)

        req = ReviewRequest(repo=repo_path, branch=branch_name, requester="alice")
        result = handle_review_request(hc_home, TEAM, req, test_command=f"{sys.executable} -m pytest -v")

        assert not result.approved
        task = get_task(hc_home, TEAM, task_id)
        assert task["status"] == "in_progress"

    def test_coverage_failure_sets_in_progress(self, qa_team_with_task):
        """When coverage is insufficient, task should go back to in_progress."""
        hc_home, repo_path, task_id, branch_name = qa_team_with_task
        req = ReviewRequest(repo=repo_path, branch=branch_name, requester="alice")

        # Mock coverage check to fail
        with patch("delegate.qa.check_test_coverage", return_value=(False, "Coverage: 30% is below minimum 60%.")):
            result = handle_review_request(hc_home, TEAM, req, test_command=f"{sys.executable} -m pytest -v")

        assert not result.approved
        assert "Coverage check failed" in result.output
        task = get_task(hc_home, TEAM, task_id)
        assert task["status"] == "in_progress"

    def test_no_task_id_still_works(self, qa_team):
        """When branch doesn't match a task, QA should still work without errors."""
        hc_home, repo_path = qa_team
        req = ReviewRequest(repo=repo_path, branch="feature-xyz", requester="alice")

        with patch("delegate.qa.check_test_coverage", return_value=(True, "Coverage OK")):
            result = handle_review_request(hc_home, TEAM, req, test_command=f"{sys.executable} -m pytest -v")

        assert result.approved  # should still pass, just no task status update


class TestUpdatedReviewMessages:
    def test_approved_message_mentions_merge_queue(self, qa_team):
        """APPROVED messages should mention merge queue readiness."""
        hc_home, repo_path = qa_team
        req = ReviewRequest(repo=repo_path, branch="feature-xyz", requester="alice")

        with patch("delegate.qa.check_test_coverage", return_value=(True, "Coverage: 85%")):
            result = handle_review_request(hc_home, TEAM, req, test_command=f"{sys.executable} -m pytest -v")

        assert result.approved
        # Check outbox for the APPROVED message
        qa_outbox = read_outbox(hc_home, TEAM, "qa", pending_only=False)
        approved_msgs = [m for m in qa_outbox if "APPROVED" in m.body]
        assert len(approved_msgs) >= 1
        assert "merge queue" in approved_msgs[0].body.lower()

    def test_rejected_message_includes_details(self, qa_team):
        """CHANGES_REQUESTED messages should include failure details."""
        hc_home, repo_path = qa_team
        # Create a failing branch
        subprocess.run(["git", "checkout", "-b", "broken"], cwd=repo_path, capture_output=True, check=True)
        (Path(repo_path) / "test_app.py").write_text("def test_fail():\n    assert False\n")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "broken"], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo_path, capture_output=True, check=True)

        req = ReviewRequest(repo=repo_path, branch="broken", requester="alice")
        result = handle_review_request(hc_home, TEAM, req, test_command=f"{sys.executable} -m pytest -v")

        assert not result.approved
        qa_outbox = read_outbox(hc_home, TEAM, "qa", pending_only=False)
        rejected_msgs = [m for m in qa_outbox if "CHANGES_REQUESTED" in m.body]
        assert len(rejected_msgs) >= 1
