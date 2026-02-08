"""Tests for scripts/qa.py — QA agent and review workflow."""

import subprocess

import pytest

from scripts.bootstrap import bootstrap
from scripts.qa import (
    parse_review_request,
    run_tests,
    handle_review_request,
    clone_and_checkout,
    ReviewRequest,
    ReviewResult,
)
from scripts.mailbox import Message, deliver, read_inbox, read_outbox
from scripts.chat import get_messages


@pytest.fixture
def qa_team(tmp_path):
    """Bootstrap a team that includes a QA agent and create a test repo."""
    root = tmp_path / "team"
    bootstrap(root, manager="manager", director="director", agents=["alice", "qa"])

    # Create a simple test repo outside the standup directory
    repo_dir = tmp_path / "repos" / "myapp"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)
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

    return root, str(repo_dir)


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
        root, repo_path = qa_team
        repo_dir = clone_and_checkout(root, repo_path, "feature-xyz")
        assert repo_dir.is_dir()
        assert (repo_dir / "app.py").exists()
        # Should have the multiply function from the feature branch
        content = (repo_dir / "app.py").read_text()
        assert "multiply" in content

    def test_nonexistent_repo_raises(self, qa_team):
        root, _ = qa_team
        with pytest.raises(FileNotFoundError, match="not found"):
            clone_and_checkout(root, "/nonexistent/path/repo", "main")


class TestRunTests:
    def test_passing_tests(self, qa_team):
        root, repo_path = qa_team
        repo_dir = clone_and_checkout(root, repo_path, "feature-xyz")
        result = run_tests(repo_dir, test_command="python -m pytest -v")
        assert result.approved
        assert "passed" in result.output.lower()

    def test_failing_tests(self, qa_team):
        root, repo_path = qa_team
        repo_dir = clone_and_checkout(root, repo_path, "feature-xyz")
        # Break a test
        (repo_dir / "test_app.py").write_text(
            "def test_broken():\n    assert False\n"
        )
        result = run_tests(repo_dir, test_command="python -m pytest -v")
        assert not result.approved

    def test_no_test_runner(self, tmp_path):
        """An empty directory with no recognizable project structure."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = run_tests(empty_dir)
        assert result.approved  # skips gracefully
        assert "No test runner detected" in result.output


class TestHandleReviewRequest:
    def test_full_pipeline_approved(self, qa_team):
        root, repo_path = qa_team
        req = ReviewRequest(repo=repo_path, branch="feature-xyz", requester="alice")
        result = handle_review_request(root, req, test_command="python -m pytest -v")
        assert result.approved

        # QA should have sent results to alice and manager
        qa_outbox = read_outbox_for(root, "qa")
        assert len(qa_outbox) >= 1

        # Event should be logged
        events = get_messages(root, msg_type="event")
        assert any("QA" in e["content"] for e in events)

    def test_full_pipeline_changes_requested(self, qa_team):
        root, repo_path = qa_team
        # Create a failing branch
        repo_dir = qa_team[1]
        subprocess.run(["git", "checkout", "-b", "broken"], cwd=repo_dir, capture_output=True, check=True)
        (Path(repo_dir) / "test_app.py").write_text("def test_fail():\n    assert False\n")
        subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "broken"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo_dir, capture_output=True, check=True)

        req = ReviewRequest(repo=repo_path, branch="broken", requester="alice")
        result = handle_review_request(root, req, test_command="python -m pytest -v")
        assert not result.approved

        events = get_messages(root, msg_type="event")
        assert any("CHANGES_REQUESTED" in e["content"] for e in events)

    def test_nonexistent_repo(self, qa_team):
        root, _ = qa_team
        req = ReviewRequest(repo="/nonexistent/path", branch="main", requester="alice")
        result = handle_review_request(root, req)
        assert not result.approved
        assert "clone/checkout" in result.output.lower() or "not found" in result.output.lower()


# need this import for Path usage in test
from pathlib import Path


def read_outbox_for(root, agent):
    """Helper: read all outbox messages (pending + routed) for an agent."""
    from scripts.mailbox import read_outbox
    return read_outbox(root, agent, pending_only=False)
