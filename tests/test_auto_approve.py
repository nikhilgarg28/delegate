"""Tests for the auto-approver / reviewer module."""

from unittest.mock import patch
import pytest

from delegate.db import get_connection
from delegate.review import get_current_review, set_verdict
from delegate.task import create_task, change_status, get_task
from delegate.config import (
    get_reviewer_config,
    is_reviewer_ai,
    set_reviewer_mode,
    update_reviewer_config,
    # Deprecated aliases (still tested for backwards compat)
    get_auto_approver_config,
    is_auto_approver_enabled,
    set_auto_approver_enabled,
    update_auto_approver_config,
)
from delegate.auto_approve import (
    auto_approve_once,
    extract_diff_files,
    check_sensitive_files,
)


@pytest.fixture()
def team_home(tmp_path):
    """Set up a minimal team home with DB."""
    hc_home = tmp_path / "home"
    team = "acme"
    team_dir = hc_home / "teams" / team
    team_dir.mkdir(parents=True)
    conn = get_connection(hc_home, team)
    conn.close()
    return hc_home, team


def _make_in_approval_task(hc_home, team, title="Fix widgets"):
    """Create a task and move it to in_approval."""
    task = create_task(hc_home, team, title=title, assignee="alice", priority="high", repo=[])
    tid = task["id"]
    change_status(hc_home, team, tid, "in_progress")
    change_status(hc_home, team, tid, "in_review")
    change_status(hc_home, team, tid, "in_approval")
    return get_task(hc_home, team, tid)


GOOD_SCORES = {
    "correctness": 4, "readability": 4, "style": 4,
    "test_quality": 4, "simplicity": 4, "avg": 4.0,
    "reasoning": "Looks good",
}

BAD_SCORES = {
    "correctness": 2, "readability": 2, "style": 2,
    "test_quality": 2, "simplicity": 2, "avg": 2.0,
    "reasoning": "Needs improvement",
}


# --- Config tests ---

class TestReviewerConfig:
    def test_defaults(self, team_home):
        hc_home, team = team_home
        cfg = get_reviewer_config(hc_home, team)
        assert cfg["mode"] == "human"
        assert cfg["threshold"] == 3.5
        assert cfg["model"] == "claude-sonnet-4-20250514"

    def test_is_reviewer_ai_default_false(self, team_home):
        hc_home, team = team_home
        assert is_reviewer_ai(hc_home, team) is False

    def test_set_reviewer_mode(self, team_home):
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        assert is_reviewer_ai(hc_home, team) is True
        set_reviewer_mode(hc_home, team, "human")
        assert is_reviewer_ai(hc_home, team) is False

    def test_update_config(self, team_home):
        hc_home, team = team_home
        result = update_reviewer_config(hc_home, team, mode="ai", threshold=4.0)
        assert result["mode"] == "ai"
        assert result["threshold"] == 4.0
        assert result["model"] == "claude-sonnet-4-20250514"

        cfg = get_reviewer_config(hc_home, team)
        assert cfg["mode"] == "ai"
        assert cfg["threshold"] == 4.0

    def test_legacy_auto_approver_fallback(self, team_home):
        """Legacy auto_approver key in repos.yaml is read transparently."""
        hc_home, team = team_home
        from delegate.config import _read_repos, _write_repos
        data = _read_repos(hc_home, team)
        data["auto_approver"] = {"enabled": True, "threshold": 4.0}
        _write_repos(hc_home, team, data)

        cfg = get_reviewer_config(hc_home, team)
        assert cfg["mode"] == "ai"
        assert cfg["threshold"] == 4.0

    def test_deprecated_aliases_still_work(self, team_home):
        """Deprecated auto_approver functions still work as adapters."""
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        assert is_auto_approver_enabled(hc_home, team) is True

        cfg = get_auto_approver_config(hc_home, team)
        assert cfg["enabled"] is True


# --- Core auto_approve_once tests ---

class TestAutoApproveOnce:
    def test_returns_none_when_disabled(self, team_home):
        hc_home, team = team_home
        result = auto_approve_once(hc_home, team)
        assert result is None

    @patch("delegate.eval.judge_diff")
    @patch("delegate.auto_approve._get_task_diff")
    def test_approves_above_threshold(self, mock_diff, mock_judge, team_home):
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        task = _make_in_approval_task(hc_home, team)

        mock_diff.return_value = {"_default": "diff content here"}
        mock_judge.return_value = GOOD_SCORES

        result = auto_approve_once(hc_home, team)

        assert result is not None
        assert result["verdict"] == "approved"
        assert result["task_id"] == task["id"]
        assert result["scores"]["avg"] == 4.0

        updated = get_task(hc_home, team, task["id"])
        assert updated["approval_status"] == "approved"

        review = get_current_review(hc_home, team, task["id"])
        assert review["verdict"] == "approved"
        assert review["reviewer"] == "auto-approver"

    @patch("delegate.notify.notify_rejection")
    @patch("delegate.eval.judge_diff")
    @patch("delegate.auto_approve._get_task_diff")
    def test_rejects_below_threshold(self, mock_diff, mock_judge, mock_notify, team_home):
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        task = _make_in_approval_task(hc_home, team)

        mock_diff.return_value = {"_default": "diff content here"}
        mock_judge.return_value = BAD_SCORES

        result = auto_approve_once(hc_home, team)

        assert result is not None
        assert result["verdict"] == "rejected"
        assert result["task_id"] == task["id"]

        updated = get_task(hc_home, team, task["id"])
        assert updated["approval_status"] == "rejected"
        assert updated["status"] == "rejected"

        review = get_current_review(hc_home, team, task["id"])
        assert review["verdict"] == "rejected"
        assert review["reviewer"] == "auto-approver"

        mock_notify.assert_called_once()

    @patch("delegate.eval.judge_diff")
    @patch("delegate.auto_approve._get_task_diff")
    def test_skips_already_reviewed(self, mock_diff, mock_judge, team_home):
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        task = _make_in_approval_task(hc_home, team)

        # Human already approved
        attempt = task["review_attempt"]
        set_verdict(hc_home, team, task["id"], attempt, "approved", reviewer="human")

        result = auto_approve_once(hc_home, team)

        assert result is None
        mock_judge.assert_not_called()

    @patch("delegate.notify.notify_rejection")
    @patch("delegate.eval.judge_diff")
    @patch("delegate.auto_approve._get_task_diff")
    def test_human_can_override_auto_reject(self, mock_diff, mock_judge, mock_notify, team_home):
        """After auto-reject, a human can still approve via set_verdict."""
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        task = _make_in_approval_task(hc_home, team)

        mock_diff.return_value = {"_default": "diff content"}
        mock_judge.return_value = BAD_SCORES

        auto_approve_once(hc_home, team)

        review = get_current_review(hc_home, team, task["id"])
        assert review["verdict"] == "rejected"

        # Human overrides
        set_verdict(hc_home, team, task["id"], review["attempt"], "approved",
                    summary="Actually this is fine", reviewer="human")
        review2 = get_current_review(hc_home, team, task["id"])
        assert review2["verdict"] == "approved"
        assert review2["reviewer"] == "human"

    @patch("delegate.merge._sort_merge_candidates")
    @patch("delegate.eval.judge_diff")
    @patch("delegate.auto_approve._get_task_diff")
    def test_processes_in_merge_order(self, mock_diff, mock_judge, mock_sort, team_home):
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")

        task1 = _make_in_approval_task(hc_home, team, title="First task")
        task2 = _make_in_approval_task(hc_home, team, title="Second task")

        # Sort returns task2 first (higher merge priority)
        mock_sort.return_value = [
            get_task(hc_home, team, task2["id"]),
            get_task(hc_home, team, task1["id"]),
        ]

        mock_diff.return_value = {"_default": "diff"}
        mock_judge.return_value = GOOD_SCORES

        result = auto_approve_once(hc_home, team)

        assert result["task_id"] == task2["id"]

    @patch("delegate.eval.judge_diff")
    @patch("delegate.auto_approve._get_task_diff")
    def test_truncates_large_diff(self, mock_diff, mock_judge, team_home):
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        _make_in_approval_task(hc_home, team)

        large_diff = "x" * 150_000
        mock_diff.return_value = {"_default": large_diff}
        mock_judge.return_value = GOOD_SCORES

        auto_approve_once(hc_home, team)

        call_args = mock_judge.call_args
        diff_arg = call_args[0][0]
        assert len(diff_arg) <= 100_000 + 100  # 100K + truncation note
        assert "truncated" in diff_arg

    def test_returns_none_no_candidates(self, team_home):
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        result = auto_approve_once(hc_home, team)
        assert result is None

    @patch("delegate.eval.judge_diff")
    @patch("delegate.auto_approve._get_task_diff")
    def test_skips_sensitive_files(self, mock_diff, mock_judge, team_home):
        """Diffs touching sensitive files are skipped (not approved or rejected)."""
        hc_home, team = team_home
        set_reviewer_mode(hc_home, team, "ai")
        task = _make_in_approval_task(hc_home, team)

        mock_diff.return_value = {
            "_default": (
                "diff --git a/src/main.py b/src/main.py\n"
                "--- a/src/main.py\n"
                "+++ b/src/main.py\n"
                "@@ -1 +1 @@\n"
                "-old\n+new\n"
                "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
                "--- a/.github/workflows/ci.yml\n"
                "+++ b/.github/workflows/ci.yml\n"
                "@@ -1 +1 @@\n"
                "-old\n+new\n"
            )
        }

        result = auto_approve_once(hc_home, team)

        assert result is not None
        assert result["verdict"] == "skipped"
        assert result["reason"] == "sensitive_files"
        assert ".github/workflows/ci.yml" in result["files"]
        mock_judge.assert_not_called()

        # Task should still be in_approval (not approved or rejected)
        updated = get_task(hc_home, team, task["id"])
        assert updated["status"] == "in_approval"


# --- Sensitive file detection tests ---

class TestSensitiveFileDetection:
    """Unit tests for extract_diff_files and check_sensitive_files."""

    def test_extract_diff_files(self):
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n"
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n+++ b/README.md\n"
        )
        files = extract_diff_files(diff)
        assert files == {"src/app.py", "README.md"}

    def test_extract_renamed_file(self):
        diff = "diff --git a/old_name.py b/new_name.py\n"
        files = extract_diff_files(diff)
        assert "old_name.py" in files
        assert "new_name.py" in files

    def test_instruction_files_blocked(self):
        for name in ["CLAUDE.md", "claude.md", "AGENTS.md", "agents.md",
                      ".cursorrules", ".claude/instructions.md",
                      ".github/copilot-instructions.md"]:
            diff = f"diff --git a/{name} b/{name}\n"
            matched = check_sensitive_files(diff)
            assert matched, f"Expected {name} to be blocked"

    def test_ci_files_blocked(self):
        for name in [".github/workflows/ci.yml", ".github/workflows/deploy.yaml",
                      ".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml",
                      ".travis.yml"]:
            diff = f"diff --git a/{name} b/{name}\n"
            matched = check_sensitive_files(diff)
            assert matched, f"Expected {name} to be blocked"

    def test_secret_files_blocked(self):
        for name in [".env", ".env.production", "server.pem", "private.key",
                      "credentials.json", "secrets.yaml"]:
            diff = f"diff --git a/{name} b/{name}\n"
            matched = check_sensitive_files(diff)
            assert matched, f"Expected {name} to be blocked"

    def test_nested_env_blocked(self):
        """A .env file in a subdirectory should still be blocked."""
        diff = "diff --git a/config/.env b/config/.env\n"
        matched = check_sensitive_files(diff)
        assert matched
        assert "config/.env" in matched

    def test_docker_files_blocked(self):
        for name in ["Dockerfile", "Dockerfile.prod", "docker-compose.yml",
                      "docker-compose.override.yaml"]:
            diff = f"diff --git a/{name} b/{name}\n"
            matched = check_sensitive_files(diff)
            assert matched, f"Expected {name} to be blocked"

    def test_delegate_files_blocked(self):
        for name in ["override.md", ".delegate/setup.sh", ".delegate/premerge.sh",
                      "setup.sh", "premerge.sh"]:
            diff = f"diff --git a/{name} b/{name}\n"
            matched = check_sensitive_files(diff)
            assert matched, f"Expected {name} to be blocked"

    def test_normal_files_not_blocked(self):
        """Regular source files should pass through fine."""
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "diff --git a/tests/test_app.py b/tests/test_app.py\n"
            "diff --git a/README.md b/README.md\n"
            "diff --git a/package.json b/package.json\n"
        )
        matched = check_sensitive_files(diff)
        assert matched == []

    def test_empty_diff(self):
        assert check_sensitive_files("") == []
        assert extract_diff_files("") == set()
