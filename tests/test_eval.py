"""Tests for scripts/eval.py — charter variant system, quality metrics, and LLM judge."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from scripts.eval import (
    # T0031 — eval runner
    load_benchmark_specs,
    seed_tasks,
    setup_repo,
    check_acceptance_criteria,
    run_eval,
    compare_results,
    _check_single_criterion,
    list_variants,
    load_variant,
    bootstrap_with_variant,
    collect_metrics,
    _collect_db_metrics,
    _collect_task_metrics,
    _count_lint_violations,
    _count_type_errors,
    _compute_complexity,
    _get_diff_size,
    judge_diff,
    judge_run,
    _parse_judge_response,
    _average_scores,
    RUBRIC_DIMENSIONS,
    DEFAULT_RUBRIC,
)


class TestListVariants:
    """Tests for list_variants()."""

    def test_returns_known_variants(self):
        """The shipped sample variants are discoverable."""
        variants = list_variants()
        assert "ship-fast" in variants
        assert "quality-first" in variants

    def test_returns_sorted(self):
        """Variants are returned in sorted order."""
        variants = list_variants()
        assert variants == sorted(variants)

    def test_returns_list_of_strings(self):
        """Each variant name is a plain string."""
        for name in list_variants():
            assert isinstance(name, str)
            assert "/" not in name  # just the directory name, no path


class TestLoadVariant:
    """Tests for load_variant()."""

    def test_loads_ship_fast(self):
        """ship-fast variant loads and overrides constitution."""
        charter = load_variant("ship-fast")
        assert "constitution.md" in charter
        # ship-fast has its own constitution — should differ from default
        assert "ships fast" in charter["constitution.md"].lower()

    def test_loads_quality_first(self):
        """quality-first variant loads and overrides constitution."""
        charter = load_variant("quality-first")
        assert "constitution.md" in charter
        assert "quality" in charter["constitution.md"].lower()

    def test_falls_back_to_defaults(self):
        """Files not overridden by the variant come from the default charter."""
        charter = load_variant("ship-fast")
        # ship-fast only overrides constitution.md and code-review.md
        # communication.md should come from default
        assert "communication.md" in charter
        assert "task-management.md" in charter
        assert "manager.md" in charter

    def test_all_default_files_present(self):
        """Every default charter file is present in the loaded variant."""
        charter = load_variant("ship-fast")
        expected = {
            "constitution.md",
            "communication.md",
            "task-management.md",
            "code-review.md",
            "manager.md",
        }
        assert expected.issubset(set(charter.keys()))

    def test_variant_overrides_differ_from_default(self):
        """Variant files should actually differ from the defaults."""
        ship = load_variant("ship-fast")
        quality = load_variant("quality-first")
        # The two variants should have different constitutions
        assert ship["constitution.md"] != quality["constitution.md"]
        # And different code-review docs
        assert ship["code-review.md"] != quality["code-review.md"]

    def test_nonexistent_variant_raises(self):
        """Loading a variant that doesn't exist raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="no-such-variant"):
            load_variant("no-such-variant")

    def test_returns_dict_of_strings(self):
        """Return value is a dict mapping str -> str."""
        charter = load_variant("quality-first")
        assert isinstance(charter, dict)
        for key, value in charter.items():
            assert isinstance(key, str)
            assert isinstance(value, str)
            assert len(value) > 0


class TestBootstrapWithVariant:
    """Tests for bootstrap_with_variant()."""

    def test_creates_directory_structure(self, tmp_path):
        """bootstrap_with_variant creates the standard team structure."""
        root = tmp_path / "team"
        bootstrap_with_variant(
            root,
            variant_name="ship-fast",
            manager="mgr",
            director="dir",
            agents=["alice"],
        )
        standup = root / ".standup"
        assert standup.is_dir()
        assert (standup / "charter").is_dir()
        assert (standup / "roster.md").is_file()
        assert (standup / "db.sqlite").is_file()
        assert (standup / "team" / "mgr").is_dir()
        assert (standup / "team" / "dir").is_dir()
        assert (standup / "team" / "alice").is_dir()

    def test_applies_variant_constitution(self, tmp_path):
        """The variant's constitution replaces the default one."""
        root = tmp_path / "team"
        bootstrap_with_variant(
            root,
            variant_name="ship-fast",
            manager="mgr",
            director="dir",
            agents=[],
        )
        constitution = root / ".standup" / "charter" / "constitution.md"
        content = constitution.read_text()
        assert "ships fast" in content.lower()

    def test_applies_variant_code_review(self, tmp_path):
        """The variant's code-review.md replaces the default one."""
        root = tmp_path / "team"
        bootstrap_with_variant(
            root,
            variant_name="quality-first",
            manager="mgr",
            director="dir",
            agents=[],
        )
        code_review = root / ".standup" / "charter" / "code-review.md"
        content = code_review.read_text()
        # quality-first has stricter review language
        assert "every concern is blocking" in content.lower()

    def test_non_overridden_files_are_default(self, tmp_path):
        """Charter files not in the variant come from the default templates."""
        root = tmp_path / "team"
        bootstrap_with_variant(
            root,
            variant_name="ship-fast",
            manager="mgr",
            director="dir",
            agents=[],
        )
        # communication.md is not overridden by ship-fast
        comm = root / ".standup" / "charter" / "communication.md"
        assert comm.is_file()
        content = comm.read_text()
        # Should contain default communication protocol content
        assert "communication" in content.lower()

    def test_different_variants_produce_different_charters(self, tmp_path):
        """Two different variants produce different charter content."""
        root_fast = tmp_path / "fast"
        root_quality = tmp_path / "quality"

        bootstrap_with_variant(
            root_fast, variant_name="ship-fast",
            manager="mgr", director="dir", agents=[],
        )
        bootstrap_with_variant(
            root_quality, variant_name="quality-first",
            manager="mgr", director="dir", agents=[],
        )

        fast_const = (root_fast / ".standup" / "charter" / "constitution.md").read_text()
        quality_const = (root_quality / ".standup" / "charter" / "constitution.md").read_text()
        assert fast_const != quality_const

    def test_nonexistent_variant_raises(self, tmp_path):
        """bootstrap_with_variant fails cleanly for unknown variants."""
        root = tmp_path / "team"
        with pytest.raises(FileNotFoundError):
            bootstrap_with_variant(
                root,
                variant_name="does-not-exist",
                manager="mgr",
                director="dir",
            )

    def test_all_charter_files_present(self, tmp_path):
        """After bootstrap_with_variant, all expected charter files exist."""
        root = tmp_path / "team"
        bootstrap_with_variant(
            root,
            variant_name="quality-first",
            manager="mgr",
            director="dir",
            agents=["alice"],
        )
        charter_dir = root / ".standup" / "charter"
        expected = {"constitution.md", "communication.md", "task-management.md",
                    "code-review.md", "manager.md"}
        actual = {f.name for f in charter_dir.glob("*.md")}
        assert expected.issubset(actual)


# ---------------------------------------------------------------------------
# Fixtures for metrics tests
# ---------------------------------------------------------------------------


def _create_db(db_path: Path, sessions=None, messages=None):
    """Create a db.sqlite with sample data."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            task_id INTEGER,
            started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            ended_at TEXT,
            duration_seconds REAL DEFAULT 0.0,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            content TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('chat', 'event'))
        );
    """)
    if sessions:
        for s in sessions:
            conn.execute(
                "INSERT INTO sessions (agent, task_id, duration_seconds, tokens_in, tokens_out, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (s["agent"], s.get("task_id"), s["duration"], s["tokens_in"], s["tokens_out"], s["cost"]),
            )
    if messages:
        for m in messages:
            conn.execute(
                "INSERT INTO messages (sender, recipient, content, type) VALUES (?, ?, ?, ?)",
                (m["sender"], m["recipient"], m["content"], m["type"]),
            )
    conn.commit()
    conn.close()


def _create_task_file(tasks_dir: Path, task_id: int, status: str = "done", title: str = "Test task"):
    """Create a task YAML file."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "id": task_id,
        "title": title,
        "status": status,
        "assignee": "alice",
    }
    path = tasks_dir / f"T{task_id:04d}.yaml"
    path.write_text(yaml.dump(task, default_flow_style=False))


@pytest.fixture
def run_dir(tmp_path):
    """Create a minimal eval run directory with db and tasks."""
    root = tmp_path / "run"
    standup = root / ".standup"
    standup.mkdir(parents=True)

    # Create DB with sample data
    _create_db(
        standup / "db.sqlite",
        sessions=[
            {"agent": "alice", "task_id": 1, "duration": 120.0, "tokens_in": 5000, "tokens_out": 2000, "cost": 0.05},
            {"agent": "alice", "task_id": 2, "duration": 180.0, "tokens_in": 8000, "tokens_out": 3000, "cost": 0.08},
            {"agent": "bob", "task_id": 1, "duration": 60.0, "tokens_in": 2000, "tokens_out": 1000, "cost": 0.02},
        ],
        messages=[
            {"sender": "alice", "recipient": "edison", "content": "Done with task 1", "type": "chat"},
            {"sender": "edison", "recipient": "alice", "content": "Great work", "type": "chat"},
            {"sender": "bob", "recipient": "edison", "content": "Task 2 done", "type": "chat"},
            {"sender": "edison", "recipient": "bob", "content": "LGTM", "type": "chat"},
            {"sender": "system", "recipient": "all", "content": "Task created", "type": "event"},
        ],
    )

    # Create task files
    tasks_dir = standup / "tasks"
    _create_task_file(tasks_dir, 1, status="done")
    _create_task_file(tasks_dir, 2, status="done")
    _create_task_file(tasks_dir, 3, status="in_progress")

    return root


# ---------------------------------------------------------------------------
# DB metrics tests
# ---------------------------------------------------------------------------


class TestCollectDbMetrics:
    """Tests for _collect_db_metrics()."""

    def test_aggregates_session_totals(self, run_dir):
        """Sums tokens, cost, and duration across all sessions."""
        metrics = _collect_db_metrics(run_dir / ".standup" / "db.sqlite")
        assert metrics["total_tokens_in"] == 15000  # 5000 + 8000 + 2000
        assert metrics["total_tokens_out"] == 6000   # 2000 + 3000 + 1000
        assert metrics["total_cost_usd"] == pytest.approx(0.15)
        assert metrics["total_sessions"] == 3
        assert metrics["total_wall_clock_seconds"] == pytest.approx(360.0)

    def test_computes_per_task_averages(self, run_dir):
        """Computes avg sessions/task and avg seconds/task."""
        metrics = _collect_db_metrics(run_dir / ".standup" / "db.sqlite")
        # 3 sessions across 2 distinct task_ids -> 1.5 sessions/task
        assert metrics["avg_sessions_per_task"] == 1.5
        # 360 seconds / 2 tasks -> 180 seconds/task
        assert metrics["avg_seconds_per_task"] == 180.0

    def test_counts_chat_messages_only(self, run_dir):
        """Only counts 'chat' type messages, not 'event'."""
        metrics = _collect_db_metrics(run_dir / ".standup" / "db.sqlite")
        assert metrics["total_messages"] == 4  # 4 chat messages, 1 event excluded

    def test_empty_db(self, tmp_path):
        """Handles a db with no data gracefully."""
        standup = tmp_path / ".standup"
        standup.mkdir()
        _create_db(standup / "db.sqlite")

        metrics = _collect_db_metrics(standup / "db.sqlite")
        assert metrics["total_tokens_in"] == 0
        assert metrics["total_sessions"] == 0
        assert metrics["avg_sessions_per_task"] == 0.0

    def test_missing_db(self, tmp_path):
        """Returns empty dict when db.sqlite doesn't exist."""
        metrics = _collect_db_metrics(tmp_path / "nonexistent.sqlite")
        assert metrics == {}


# ---------------------------------------------------------------------------
# Task metrics tests
# ---------------------------------------------------------------------------


class TestCollectTaskMetrics:
    """Tests for _collect_task_metrics()."""

    def test_counts_completed_and_failed(self, run_dir):
        """Counts done tasks as completed, others as failed."""
        metrics = _collect_task_metrics(run_dir / ".standup" / "tasks")
        assert metrics["tasks_completed"] == 2
        assert metrics["tasks_failed"] == 1  # the in_progress one

    def test_empty_tasks_dir(self, tmp_path):
        """Returns zeros when tasks dir is empty."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        metrics = _collect_task_metrics(tasks_dir)
        assert metrics["tasks_completed"] == 0
        assert metrics["tasks_failed"] == 0

    def test_missing_tasks_dir(self, tmp_path):
        """Returns zeros when tasks dir doesn't exist."""
        metrics = _collect_task_metrics(tmp_path / "nonexistent")
        assert metrics["tasks_completed"] == 0
        assert metrics["tasks_failed"] == 0


# ---------------------------------------------------------------------------
# External tool metrics tests (mocked subprocess)
# ---------------------------------------------------------------------------


class TestLintViolations:
    """Tests for _count_lint_violations() with mocked subprocess."""

    def test_counts_ruff_output_lines(self, tmp_path):
        """Each line of ruff output is one violation."""
        # Create a dummy Python file
        (tmp_path / "foo.py").write_text("x = 1\n")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "foo.py:1:1: F841 local variable 'x' is assigned to but never used\nfoo.py:2:1: E302 expected 2 blank lines\n"
        mock_result.stderr = ""

        with patch("scripts.eval.shutil.which", return_value="/usr/bin/ruff"), \
             patch("scripts.eval.subprocess.run", return_value=mock_result):
            count = _count_lint_violations(tmp_path, ["foo.py"])
        assert count == 2

    def test_zero_violations(self, tmp_path):
        """Clean code produces zero violations."""
        (tmp_path / "foo.py").write_text("x = 1\n")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("scripts.eval.shutil.which", return_value="/usr/bin/ruff"), \
             patch("scripts.eval.subprocess.run", return_value=mock_result):
            count = _count_lint_violations(tmp_path, ["foo.py"])
        assert count == 0

    def test_ruff_not_installed(self, tmp_path):
        """Returns None when ruff is not installed."""
        (tmp_path / "foo.py").write_text("x = 1\n")

        with patch("scripts.eval.shutil.which", return_value=None):
            count = _count_lint_violations(tmp_path, ["foo.py"])
        assert count is None

    def test_empty_file_list(self, tmp_path):
        """Returns 0 when no files to check."""
        count = _count_lint_violations(tmp_path, [])
        assert count == 0


class TestTypeErrors:
    """Tests for _count_type_errors() with mocked subprocess."""

    def test_counts_pyright_errors(self, tmp_path):
        """Counts lines containing 'error' in pyright output."""
        (tmp_path / "foo.py").write_text("x: int = 'hello'\n")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = 'foo.py:1:10 - error: Expression of type "str" is incompatible\n0 warnings, 1 error\n'
        mock_result.stderr = ""

        with patch("scripts.eval.shutil.which", return_value="/usr/bin/pyright"), \
             patch("scripts.eval.subprocess.run", return_value=mock_result):
            count = _count_type_errors(tmp_path, ["foo.py"])
        assert count == 2  # both lines contain "error"

    def test_no_type_checker_installed(self, tmp_path):
        """Returns None when neither pyright nor mypy is installed."""
        (tmp_path / "foo.py").write_text("x = 1\n")

        with patch("scripts.eval.shutil.which", return_value=None):
            count = _count_type_errors(tmp_path, ["foo.py"])
        assert count is None

    def test_empty_file_list(self, tmp_path):
        """Returns 0 when no files to check."""
        count = _count_type_errors(tmp_path, [])
        assert count == 0


class TestComplexityScore:
    """Tests for _compute_complexity() with mocked subprocess."""

    def test_parses_radon_average(self, tmp_path):
        """Parses average complexity from radon output."""
        (tmp_path / "foo.py").write_text("def f(): pass\n")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "foo.py\n"
            "    F 1:0 f - A (1)\n"
            "\n"
            "1 blocks (classes, functions, methods) analyzed.\n"
            "Average complexity: A (1.0)\n"
        )
        mock_result.stderr = ""

        with patch("scripts.eval.shutil.which", return_value="/usr/bin/radon"), \
             patch("scripts.eval.subprocess.run", return_value=mock_result):
            score = _compute_complexity(tmp_path, ["foo.py"])
        assert score == 1.0

    def test_radon_not_installed(self, tmp_path):
        """Returns None when radon is not installed."""
        (tmp_path / "foo.py").write_text("def f(): pass\n")

        with patch("scripts.eval.shutil.which", return_value=None):
            score = _compute_complexity(tmp_path, ["foo.py"])
        assert score is None

    def test_empty_file_list(self, tmp_path):
        """Returns 0.0 when no files to analyze."""
        score = _compute_complexity(tmp_path, [])
        assert score == 0.0


class TestDiffSize:
    """Tests for _get_diff_size() with mocked subprocess."""

    def test_parses_git_diff_stat(self, tmp_path):
        """Parses insertions and deletions from git diff --stat."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            " scripts/eval.py | 150 +++++++++++++++\n"
            " tests/test_eval.py | 80 ++++++++\n"
            " 2 files changed, 200 insertions(+), 30 deletions(-)\n"
        )
        mock_result.stderr = ""

        with patch("scripts.eval.shutil.which", return_value="/usr/bin/git"), \
             patch("scripts.eval.subprocess.run", return_value=mock_result):
            size = _get_diff_size(tmp_path)
        assert size == 230  # 200 + 30

    def test_insertions_only(self, tmp_path):
        """Handles diffs with only insertions."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = " 1 file changed, 50 insertions(+)\n"
        mock_result.stderr = ""

        with patch("scripts.eval.shutil.which", return_value="/usr/bin/git"), \
             patch("scripts.eval.subprocess.run", return_value=mock_result):
            size = _get_diff_size(tmp_path)
        assert size == 50

    def test_git_not_available(self, tmp_path):
        """Returns None when git is not available."""
        with patch("scripts.eval.shutil.which", return_value=None):
            size = _get_diff_size(tmp_path)
        assert size is None


# ---------------------------------------------------------------------------
# Integration: collect_metrics
# ---------------------------------------------------------------------------


class TestCollectMetrics:
    """Integration tests for collect_metrics()."""

    def test_returns_all_expected_keys(self, run_dir):
        """collect_metrics returns all documented metric keys."""
        with patch("scripts.eval._get_changed_files", return_value=[]), \
             patch("scripts.eval._get_diff_size", return_value=0), \
             patch("scripts.eval._count_lint_violations", return_value=0), \
             patch("scripts.eval._count_type_errors", return_value=None), \
             patch("scripts.eval._compute_complexity", return_value=None):
            metrics = collect_metrics(run_dir)

        expected_keys = {
            "total_tokens_in", "total_tokens_out", "total_cost_usd",
            "total_sessions", "avg_sessions_per_task",
            "total_wall_clock_seconds", "avg_seconds_per_task",
            "total_messages", "messages_per_task",
            "tasks_completed", "tasks_failed",
            "diff_size", "lint_violations", "type_errors", "complexity_score",
        }
        assert expected_keys == set(metrics.keys())

    def test_computes_messages_per_task(self, run_dir):
        """messages_per_task = total_messages / (completed + failed)."""
        with patch("scripts.eval._get_changed_files", return_value=[]), \
             patch("scripts.eval._get_diff_size", return_value=0), \
             patch("scripts.eval._count_lint_violations", return_value=0), \
             patch("scripts.eval._count_type_errors", return_value=None), \
             patch("scripts.eval._compute_complexity", return_value=None):
            metrics = collect_metrics(run_dir)

        # 4 chat messages / 3 total tasks = 1.33
        assert metrics["messages_per_task"] == pytest.approx(1.33, abs=0.01)

    def test_values_are_json_serializable(self, run_dir):
        """All metric values are numbers or None (JSON-safe)."""
        import json

        with patch("scripts.eval._get_changed_files", return_value=[]), \
             patch("scripts.eval._get_diff_size", return_value=0), \
             patch("scripts.eval._count_lint_violations", return_value=0), \
             patch("scripts.eval._count_type_errors", return_value=None), \
             patch("scripts.eval._compute_complexity", return_value=None):
            metrics = collect_metrics(run_dir)

        # Should not raise
        serialized = json.dumps(metrics)
        assert isinstance(serialized, str)

        for key, value in metrics.items():
            assert value is None or isinstance(value, (int, float)), \
                f"Metric '{key}' has non-numeric value: {value!r}"

    def test_handles_no_tasks(self, tmp_path):
        """Handles a run dir with db but no tasks."""
        root = tmp_path / "run"
        standup = root / ".standup"
        standup.mkdir(parents=True)
        (standup / "tasks").mkdir()
        _create_db(standup / "db.sqlite")

        with patch("scripts.eval._get_changed_files", return_value=[]), \
             patch("scripts.eval._get_diff_size", return_value=0), \
             patch("scripts.eval._count_lint_violations", return_value=0), \
             patch("scripts.eval._count_type_errors", return_value=None), \
             patch("scripts.eval._compute_complexity", return_value=None):
            metrics = collect_metrics(root)

        assert metrics["tasks_completed"] == 0
        assert metrics["tasks_failed"] == 0
        assert metrics["messages_per_task"] == 0.0


# ---------------------------------------------------------------------------
# LLM-as-judge tests (T0033)
# ---------------------------------------------------------------------------

# Sample LLM response used across judge tests
_SAMPLE_JUDGE_JSON = json.dumps({
    "correctness": 4,
    "readability": 5,
    "style": 3,
    "test_quality": 4,
    "simplicity": 5,
    "reasoning": "Code is correct and readable but style could improve.",
})

_SAMPLE_JUDGE_JSON_ALT = json.dumps({
    "correctness": 3,
    "readability": 4,
    "style": 4,
    "test_quality": 3,
    "simplicity": 4,
    "reasoning": "Decent implementation with some room for improvement.",
})


class TestParseJudgeResponse:
    """Tests for _parse_judge_response()."""

    def test_parses_clean_json(self):
        """Parses a well-formed JSON response."""
        result = _parse_judge_response(_SAMPLE_JUDGE_JSON)
        assert result["correctness"] == 4
        assert result["readability"] == 5
        assert result["style"] == 3
        assert result["test_quality"] == 4
        assert result["simplicity"] == 5
        assert result["reasoning"] == "Code is correct and readable but style could improve."

    def test_strips_markdown_fences(self):
        """Handles JSON wrapped in markdown code fences."""
        wrapped = f"```json\n{_SAMPLE_JUDGE_JSON}\n```"
        result = _parse_judge_response(wrapped)
        assert result["correctness"] == 4

    def test_strips_bare_fences(self):
        """Handles JSON wrapped in bare ``` fences."""
        wrapped = f"```\n{_SAMPLE_JUDGE_JSON}\n```"
        result = _parse_judge_response(wrapped)
        assert result["correctness"] == 4

    def test_rejects_missing_dimension(self):
        """Raises ValueError when a rubric dimension is missing."""
        incomplete = json.dumps({
            "correctness": 4,
            "readability": 5,
            # missing style, test_quality, simplicity
            "reasoning": "incomplete",
        })
        with pytest.raises(ValueError, match="Missing required dimension"):
            _parse_judge_response(incomplete)

    def test_rejects_out_of_range_score(self):
        """Raises ValueError when a score is outside 1-5."""
        bad_score = json.dumps({
            "correctness": 6,
            "readability": 5,
            "style": 3,
            "test_quality": 4,
            "simplicity": 5,
            "reasoning": "bad",
        })
        with pytest.raises(ValueError, match="must be 1-5"):
            _parse_judge_response(bad_score)

    def test_rejects_zero_score(self):
        """Raises ValueError when a score is 0."""
        bad_score = json.dumps({
            "correctness": 0,
            "readability": 5,
            "style": 3,
            "test_quality": 4,
            "simplicity": 5,
            "reasoning": "bad",
        })
        with pytest.raises(ValueError, match="must be 1-5"):
            _parse_judge_response(bad_score)

    def test_rejects_missing_reasoning(self):
        """Raises ValueError when reasoning is missing."""
        no_reasoning = json.dumps({
            "correctness": 4,
            "readability": 5,
            "style": 3,
            "test_quality": 4,
            "simplicity": 5,
        })
        with pytest.raises(ValueError, match="Missing required key: reasoning"):
            _parse_judge_response(no_reasoning)

    def test_rejects_invalid_json(self):
        """Raises json.JSONDecodeError on invalid JSON."""
        with pytest.raises(json.JSONDecodeError):
            _parse_judge_response("not json at all")

    def test_coerces_float_scores_to_int(self):
        """Float scores like 4.0 are coerced to int."""
        float_scores = json.dumps({
            "correctness": 4.0,
            "readability": 5.0,
            "style": 3.0,
            "test_quality": 4.0,
            "simplicity": 5.0,
            "reasoning": "ok",
        })
        result = _parse_judge_response(float_scores)
        assert isinstance(result["correctness"], int)
        assert result["correctness"] == 4


class TestAverageScores:
    """Tests for _average_scores()."""

    def test_averages_single_entry(self):
        """A single score dict averages to itself."""
        scores = [{
            "correctness": 4, "readability": 5, "style": 3,
            "test_quality": 4, "simplicity": 5, "reasoning": "good",
        }]
        avg = _average_scores(scores)
        assert avg["correctness"] == 4.0
        assert avg["readability"] == 5.0
        assert avg["avg"] == pytest.approx(4.2)
        assert avg["reasoning"] == ["good"]

    def test_averages_multiple_entries(self):
        """Averages across multiple score dicts."""
        scores = [
            {"correctness": 4, "readability": 5, "style": 3,
             "test_quality": 4, "simplicity": 5, "reasoning": "first"},
            {"correctness": 2, "readability": 3, "style": 5,
             "test_quality": 2, "simplicity": 3, "reasoning": "second"},
        ]
        avg = _average_scores(scores)
        assert avg["correctness"] == 3.0   # (4+2)/2
        assert avg["readability"] == 4.0   # (5+3)/2
        assert avg["style"] == 4.0         # (3+5)/2
        assert avg["test_quality"] == 3.0  # (4+2)/2
        assert avg["simplicity"] == 4.0    # (5+3)/2
        assert avg["avg"] == pytest.approx(3.6)  # (3+4+4+3+4)/5
        assert avg["reasoning"] == ["first", "second"]

    def test_empty_list(self):
        """Returns empty dict for empty input."""
        assert _average_scores([]) == {}

    def test_rounds_to_two_decimals(self):
        """Averaged scores are rounded to 2 decimal places."""
        scores = [
            {"correctness": 4, "readability": 5, "style": 3,
             "test_quality": 4, "simplicity": 5, "reasoning": "a"},
            {"correctness": 3, "readability": 4, "style": 4,
             "test_quality": 3, "simplicity": 4, "reasoning": "b"},
            {"correctness": 5, "readability": 3, "style": 5,
             "test_quality": 5, "simplicity": 3, "reasoning": "c"},
        ]
        avg = _average_scores(scores)
        assert avg["correctness"] == 4.0   # (4+3+5)/3 = 4.0
        assert avg["readability"] == 4.0   # (5+4+3)/3 = 4.0
        assert avg["style"] == 4.0         # (3+4+5)/3 = 4.0
        assert avg["test_quality"] == 4.0  # (4+3+5)/3 = 4.0
        assert avg["simplicity"] == 4.0    # (5+4+3)/3 = 4.0


class TestJudgeDiff:
    """Tests for judge_diff() with mocked LLM calls."""

    def test_returns_scores_with_avg(self):
        """judge_diff returns all dimensions + avg + reasoning."""
        with patch("scripts.eval._call_llm", return_value=_SAMPLE_JUDGE_JSON):
            result = judge_diff("diff content", "task spec")

        assert result["correctness"] == 4
        assert result["readability"] == 5
        assert result["style"] == 3
        assert result["test_quality"] == 4
        assert result["simplicity"] == 5
        assert result["avg"] == pytest.approx(4.2)
        assert "reasoning" in result

    def test_retries_on_parse_failure(self):
        """Retries once when the first response fails to parse."""
        with patch("scripts.eval._call_llm", side_effect=[
            "not valid json",
            _SAMPLE_JUDGE_JSON,
        ]):
            result = judge_diff("diff", "spec")

        assert result["correctness"] == 4

    def test_raises_after_two_failures(self):
        """Raises ValueError when both attempts fail."""
        with patch("scripts.eval._call_llm", return_value="not json"):
            with pytest.raises(ValueError, match="Failed to parse"):
                judge_diff("diff", "spec")

    def test_uses_custom_rubric(self):
        """Custom rubric text is passed to the LLM."""
        custom_rubric = "Custom rubric text"
        captured_calls = []

        def mock_llm(system, user, model="claude-sonnet-4-20250514"):
            captured_calls.append(system)
            return _SAMPLE_JUDGE_JSON

        with patch("scripts.eval._call_llm", side_effect=mock_llm):
            judge_diff("diff", "spec", rubric=custom_rubric)

        assert custom_rubric in captured_calls[0]

    def test_default_rubric_used(self):
        """DEFAULT_RUBRIC is used when no rubric is provided."""
        captured_calls = []

        def mock_llm(system, user, model="claude-sonnet-4-20250514"):
            captured_calls.append(system)
            return _SAMPLE_JUDGE_JSON

        with patch("scripts.eval._call_llm", side_effect=mock_llm):
            judge_diff("diff", "spec")

        # Check the system prompt includes the default rubric content
        assert "correctness" in captured_calls[0]
        assert "simplicity" in captured_calls[0]


class TestJudgeRun:
    """Tests for judge_run() with mocked LLM calls."""

    @pytest.fixture
    def judge_run_dir(self, tmp_path):
        """Create a minimal run directory for judge tests."""
        root = tmp_path / "run"
        standup = root / ".standup"
        tasks_dir = standup / "tasks"
        tasks_dir.mkdir(parents=True)

        # Create two task files
        _create_task_file(tasks_dir, 1, status="done", title="Implement feature A")
        _create_task_file(tasks_dir, 2, status="done", title="Implement feature B")

        return root

    def test_scores_all_tasks(self, judge_run_dir):
        """judge_run scores every task in the run directory."""
        with patch("scripts.eval._call_llm", return_value=_SAMPLE_JUDGE_JSON), \
             patch("scripts.eval._get_full_diff", return_value="some diff"):
            results = judge_run(judge_run_dir, reps=1)

        assert "T0001" in results["tasks"]
        assert "T0002" in results["tasks"]
        assert "overall" in results

    def test_averages_across_reps(self, judge_run_dir):
        """Scores are averaged across multiple reps."""
        responses = [_SAMPLE_JUDGE_JSON, _SAMPLE_JUDGE_JSON_ALT]

        call_count = [0]
        def mock_llm(system, user, model="claude-sonnet-4-20250514"):
            idx = call_count[0] % len(responses)
            call_count[0] += 1
            return responses[idx]

        with patch("scripts.eval._call_llm", side_effect=mock_llm), \
             patch("scripts.eval._get_full_diff", return_value="some diff"):
            results = judge_run(judge_run_dir, reps=2)

        # T0001 should have averaged scores from the two different responses
        t1 = results["tasks"]["T0001"]
        assert t1["correctness"] == pytest.approx(3.5)  # (4+3)/2
        assert t1["readability"] == pytest.approx(4.5)   # (5+4)/2

    def test_overall_averages_across_tasks(self, judge_run_dir):
        """Overall scores average across all tasks."""
        with patch("scripts.eval._call_llm", return_value=_SAMPLE_JUDGE_JSON), \
             patch("scripts.eval._get_full_diff", return_value="some diff"):
            results = judge_run(judge_run_dir, reps=1)

        overall = results["overall"]
        # With identical scores for both tasks, overall = task scores
        assert overall["correctness"] == 4.0
        assert overall["avg"] == pytest.approx(4.2)

    def test_handles_empty_tasks_dir(self, tmp_path):
        """Returns empty results when no tasks exist."""
        root = tmp_path / "run"
        (root / ".standup" / "tasks").mkdir(parents=True)

        results = judge_run(root, reps=1)
        assert results == {"tasks": {}, "overall": {}}

    def test_handles_missing_tasks_dir(self, tmp_path):
        """Returns empty results when tasks dir doesn't exist."""
        root = tmp_path / "run"
        (root / ".standup").mkdir(parents=True)

        results = judge_run(root, reps=1)
        assert results == {"tasks": {}, "overall": {}}

    def test_skips_task_on_all_reps_failing(self, judge_run_dir):
        """Skips a task entirely if all reps fail to parse."""
        call_count = [0]
        def mock_llm(system, user, model="claude-sonnet-4-20250514"):
            call_count[0] += 1
            # judge_diff retries once per rep, so 2 reps × 2 attempts = 4 calls for T0001
            if call_count[0] <= 4:
                return "bad json"
            return _SAMPLE_JUDGE_JSON

        with patch("scripts.eval._call_llm", side_effect=mock_llm), \
             patch("scripts.eval._get_full_diff", return_value="diff"):
            results = judge_run(judge_run_dir, reps=2)

        assert "T0001" not in results["tasks"]
        assert "T0002" in results["tasks"]

    def test_number_of_llm_calls(self, judge_run_dir):
        """Verifies correct number of LLM calls: tasks × reps."""
        call_count = [0]
        def mock_llm(system, user, model="claude-sonnet-4-20250514"):
            call_count[0] += 1
            return _SAMPLE_JUDGE_JSON

        with patch("scripts.eval._call_llm", side_effect=mock_llm), \
             patch("scripts.eval._get_full_diff", return_value="diff"):
            judge_run(judge_run_dir, reps=3)

        # 2 tasks × 3 reps = 6 calls
        assert call_count[0] == 6


class TestRubricConstants:
    """Tests for rubric constants and configuration."""

    def test_rubric_dimensions_complete(self):
        """All expected dimensions are defined."""
        expected = {"correctness", "readability", "style", "test_quality", "simplicity"}
        assert set(RUBRIC_DIMENSIONS) == expected

    def test_default_rubric_mentions_all_dimensions(self):
        """DEFAULT_RUBRIC text references every dimension."""
        for dim in RUBRIC_DIMENSIONS:
            assert dim in DEFAULT_RUBRIC


# ---------------------------------------------------------------------------
# Eval runner tests (T0031)
# ---------------------------------------------------------------------------

# Sample benchmark spec for testing
_SAMPLE_SPEC = {
    "title": "Fix pagination bug",
    "description": "Fix the off-by-one error in paginate.py.",
    "repo_setup": [
        {"path": "src/paginate.py", "content": "def paginate(): pass\n"},
        {"path": "tests/test_paginate.py", "content": "def test_paginate(): pass\n"},
    ],
    "acceptance_criteria": [
        {"file_exists": {"path": "src/paginate.py"}},
        {"grep_match": {"path": "src/paginate.py", "pattern": "def paginate"}},
    ],
    "timeout_seconds": 120,
    "tags": ["bugfix"],
}

_SAMPLE_SPEC_2 = {
    "title": "Add CSV reporter",
    "description": "Create a CSV reporter module.",
    "repo_setup": [
        {"path": "src/reporter.py", "content": "# reporter\n"},
    ],
    "acceptance_criteria": [
        {"file_exists": {"path": "src/reporter.py"}},
    ],
    "timeout_seconds": 300,
    "tags": ["feature"],
}


@pytest.fixture
def specs_dir(tmp_path):
    """Create a directory with sample benchmark spec YAML files."""
    d = tmp_path / "benchmarks" / "tasks"
    d.mkdir(parents=True)
    (d / "fix_pagination.yaml").write_text(yaml.dump(_SAMPLE_SPEC))
    (d / "add_reporter.yaml").write_text(yaml.dump(_SAMPLE_SPEC_2))
    return d


class TestLoadBenchmarkSpecs:
    """Tests for load_benchmark_specs()."""

    def test_loads_all_specs(self, specs_dir):
        """Loads all YAML specs from the directory."""
        specs = load_benchmark_specs(specs_dir)
        assert len(specs) == 2
        titles = {s["title"] for s in specs}
        assert "Fix pagination bug" in titles
        assert "Add CSV reporter" in titles

    def test_includes_source_file(self, specs_dir):
        """Each spec includes _source_file metadata."""
        specs = load_benchmark_specs(specs_dir)
        for spec in specs:
            assert "_source_file" in spec

    def test_empty_directory(self, tmp_path):
        """Returns empty list for empty directory."""
        d = tmp_path / "empty"
        d.mkdir()
        assert load_benchmark_specs(d) == []

    def test_nonexistent_directory(self, tmp_path):
        """Returns empty list for nonexistent directory."""
        assert load_benchmark_specs(tmp_path / "nope") == []

    def test_skips_invalid_yaml(self, tmp_path):
        """Skips files without a 'title' field."""
        d = tmp_path / "specs"
        d.mkdir()
        (d / "good.yaml").write_text(yaml.dump({"title": "Good", "description": "ok"}))
        (d / "bad.yaml").write_text(yaml.dump({"description": "no title"}))
        specs = load_benchmark_specs(d)
        assert len(specs) == 1

    def test_loads_real_benchmarks(self):
        """Loads actual benchmark tasks from the repo."""
        real_dir = Path(__file__).parent.parent / "benchmarks" / "tasks"
        if real_dir.is_dir():
            specs = load_benchmark_specs(real_dir)
            assert len(specs) > 0


class TestSeedTasks:
    """Tests for seed_tasks()."""

    def test_creates_tasks(self, tmp_path, specs_dir):
        """Creates tasks from benchmark specs."""
        from scripts.bootstrap import bootstrap
        root = tmp_path / "team"
        bootstrap(root, manager="mgr", director="dir", agents=["alice"])

        specs = load_benchmark_specs(specs_dir)
        created = seed_tasks(root, specs)
        assert len(created) == 2
        for task in created:
            assert "id" in task
            assert task["status"] == "open"
            assert task["project"] == "eval-run"

    def test_task_ids_are_unique(self, tmp_path, specs_dir):
        """Each seeded task gets a unique ID."""
        from scripts.bootstrap import bootstrap
        root = tmp_path / "team"
        bootstrap(root, manager="mgr", director="dir", agents=["alice"])

        specs = load_benchmark_specs(specs_dir)
        created = seed_tasks(root, specs)
        ids = [t["id"] for t in created]
        assert len(ids) == len(set(ids))


class TestSetupRepo:
    """Tests for setup_repo()."""

    def test_creates_files(self, tmp_path):
        """Creates files specified in repo_setup."""
        setup_repo(tmp_path, [_SAMPLE_SPEC])
        assert (tmp_path / "src" / "paginate.py").is_file()
        assert (tmp_path / "tests" / "test_paginate.py").is_file()

    def test_file_content(self, tmp_path):
        """Files contain the specified content."""
        setup_repo(tmp_path, [_SAMPLE_SPEC])
        content = (tmp_path / "src" / "paginate.py").read_text()
        assert "def paginate" in content

    def test_creates_directories(self, tmp_path):
        """Creates parent directories as needed."""
        spec = {
            "title": "test",
            "repo_setup": [
                {"path": "deep/nested/dir/file.py", "content": "# deep\n"},
            ],
        }
        setup_repo(tmp_path, [spec])
        assert (tmp_path / "deep" / "nested" / "dir" / "file.py").is_file()

    def test_no_repo_setup(self, tmp_path):
        """Does nothing when specs have no repo_setup."""
        spec = {"title": "test", "description": "no setup"}
        setup_repo(tmp_path, [spec])
        # No error, no files created beyond tmp_path


class TestCheckAcceptanceCriteria:
    """Tests for check_acceptance_criteria() and _check_single_criterion()."""

    def test_file_exists_passes(self, tmp_path):
        """file_exists passes when the file is present."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "paginate.py").write_text("code")

        result = _check_single_criterion(
            tmp_path, {"file_exists": {"path": "src/paginate.py"}}
        )
        assert result["passed"] is True
        assert result["type"] == "file_exists"

    def test_file_exists_fails(self, tmp_path):
        """file_exists fails when the file is missing."""
        result = _check_single_criterion(
            tmp_path, {"file_exists": {"path": "src/missing.py"}}
        )
        assert result["passed"] is False

    def test_grep_match_passes(self, tmp_path):
        """grep_match passes when pattern is found in file."""
        (tmp_path / "code.py").write_text("def paginate(items): pass\n")

        result = _check_single_criterion(
            tmp_path, {"grep_match": {"path": "code.py", "pattern": "def paginate"}}
        )
        assert result["passed"] is True

    def test_grep_match_fails(self, tmp_path):
        """grep_match fails when pattern is not found."""
        (tmp_path / "code.py").write_text("def other(): pass\n")

        result = _check_single_criterion(
            tmp_path, {"grep_match": {"path": "code.py", "pattern": "def paginate"}}
        )
        assert result["passed"] is False

    def test_grep_match_regex_special_chars(self, tmp_path):
        """grep_match handles regex special chars (e.g., parentheses)."""
        (tmp_path / "code.py").write_text("start = (page - 1) * page_size\n")

        result = _check_single_criterion(
            tmp_path,
            {"grep_match": {"path": "code.py", "pattern": "\\(page - 1\\)"}},
        )
        assert result["passed"] is True

    def test_grep_match_literal_fallback(self, tmp_path):
        """grep_match falls back to literal match on invalid regex."""
        (tmp_path / "code.py").write_text("value = foo[bar\n")

        # This is an invalid regex pattern (unclosed bracket)
        result = _check_single_criterion(
            tmp_path,
            {"grep_match": {"path": "code.py", "pattern": "foo[bar"}},
        )
        assert result["passed"] is True

    def test_grep_match_missing_file(self, tmp_path):
        """grep_match fails when file doesn't exist."""
        result = _check_single_criterion(
            tmp_path,
            {"grep_match": {"path": "missing.py", "pattern": "anything"}},
        )
        assert result["passed"] is False

    def test_full_acceptance_check(self, tmp_path):
        """check_acceptance_criteria returns results for all specs."""
        setup_repo(tmp_path, [_SAMPLE_SPEC])

        results = check_acceptance_criteria(tmp_path, [_SAMPLE_SPEC])
        assert "Fix pagination bug" in results
        criteria = results["Fix pagination bug"]
        assert len(criteria) == 2
        assert criteria[0]["passed"] is True  # file_exists
        assert criteria[1]["passed"] is True  # grep_match

    def test_unknown_criterion_type(self, tmp_path):
        """Unknown criterion types are reported as failed."""
        result = _check_single_criterion(
            tmp_path, {"unknown_check": {"key": "val"}}
        )
        assert result["passed"] is False
        assert result["type"] == "unknown"


class TestRunEvalDryRun:
    """Tests for run_eval() in dry-run mode."""

    def test_dry_run_creates_directory(self, specs_dir):
        """Dry run creates a run directory."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
        )
        assert "run_dir" in results
        assert Path(results["run_dir"]).is_dir()

    def test_dry_run_seeds_tasks(self, specs_dir):
        """Dry run seeds tasks from benchmark specs."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
        )
        assert results["tasks_seeded"] == 2

    def test_dry_run_sets_variant(self, specs_dir):
        """Dry run records the variant used."""
        results = run_eval(
            variant="quality-first",
            suite=specs_dir,
            dry_run=True,
        )
        assert results["variant"] == "quality-first"

    def test_dry_run_flag_set(self, specs_dir):
        """Dry run sets dry_run=True in results."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
        )
        assert results["dry_run"] is True
        assert results["completed"] is False
        assert results["timed_out"] is False

    def test_dry_run_bootstraps_team(self, specs_dir):
        """Dry run creates the full team directory structure."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
        )
        run_dir = Path(results["run_dir"])
        assert (run_dir / ".standup").is_dir()
        assert (run_dir / ".standup" / "charter").is_dir()
        assert (run_dir / ".standup" / "roster.md").is_file()

    def test_dry_run_applies_variant(self, specs_dir):
        """Dry run applies the charter variant."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
        )
        run_dir = Path(results["run_dir"])
        constitution = (run_dir / ".standup" / "charter" / "constitution.md").read_text()
        assert "ships fast" in constitution.lower()

    def test_dry_run_sets_up_repo_files(self, specs_dir):
        """Dry run creates repo setup files from specs."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
        )
        run_dir = Path(results["run_dir"])
        # From _SAMPLE_SPEC
        assert (run_dir / "src" / "paginate.py").is_file()
        # From _SAMPLE_SPEC_2
        assert (run_dir / "src" / "reporter.py").is_file()

    def test_dry_run_saves_results_json(self, specs_dir):
        """Dry run saves results as JSON."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
        )
        run_dir = Path(results["run_dir"])
        results_file = run_dir / "results" / "run_results.json"
        assert results_file.is_file()

        loaded = json.loads(results_file.read_text())
        assert loaded["variant"] == "ship-fast"
        assert loaded["dry_run"] is True
        assert loaded["tasks_seeded"] == 2

    def test_dry_run_with_custom_agents(self, specs_dir):
        """Dry run works with custom agent names."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
            agents=["worker1", "worker2", "worker3"],
        )
        run_dir = Path(results["run_dir"])
        assert (run_dir / ".standup" / "team" / "worker1").is_dir()
        assert (run_dir / ".standup" / "team" / "worker2").is_dir()
        assert (run_dir / ".standup" / "team" / "worker3").is_dir()

    def test_dry_run_has_timestamps(self, specs_dir):
        """Dry run includes timing information."""
        results = run_eval(
            variant="ship-fast",
            suite=specs_dir,
            dry_run=True,
        )
        assert "started_at" in results
        assert "ended_at" in results
        assert "duration_seconds" in results
        assert results["duration_seconds"] >= 0

    def test_dry_run_no_specs_returns_error(self, tmp_path):
        """Dry run with empty suite returns error."""
        empty_suite = tmp_path / "empty_suite"
        empty_suite.mkdir()
        results = run_eval(
            variant="ship-fast",
            suite=empty_suite,
            dry_run=True,
        )
        assert results.get("error") is not None
        assert results["tasks_seeded"] == 0

    def test_dry_run_different_variants_produce_different_results(self, specs_dir):
        """Different variants produce different team configurations."""
        r1 = run_eval(variant="ship-fast", suite=specs_dir, dry_run=True)
        r2 = run_eval(variant="quality-first", suite=specs_dir, dry_run=True)

        d1 = Path(r1["run_dir"])
        d2 = Path(r2["run_dir"])

        c1 = (d1 / ".standup" / "charter" / "constitution.md").read_text()
        c2 = (d2 / ".standup" / "charter" / "constitution.md").read_text()
        assert c1 != c2


class TestCompareResults:
    """Tests for compare_results()."""

    def test_prints_comparison(self, tmp_path, capsys):
        """compare_results prints a comparison table."""
        # Create two run result files
        r1 = {
            "variant": "ship-fast",
            "tasks_seeded": 3,
            "completed": True,
            "timed_out": False,
            "duration_seconds": 120.5,
            "metrics": {
                "total_tokens_in": 5000,
                "total_tokens_out": 2000,
                "total_cost_usd": 0.05,
                "total_sessions": 3,
                "total_messages": 10,
                "tasks_completed": 3,
                "tasks_failed": 0,
                "messages_per_task": 3.33,
                "avg_sessions_per_task": 1.0,
                "avg_seconds_per_task": 40.0,
            },
            "acceptance": {
                "Task 1": [{"passed": True}],
                "Task 2": [{"passed": True}, {"passed": False}],
            },
        }
        r2 = {
            "variant": "quality-first",
            "tasks_seeded": 3,
            "completed": True,
            "timed_out": False,
            "duration_seconds": 200.0,
            "metrics": {
                "total_tokens_in": 8000,
                "total_tokens_out": 3000,
                "total_cost_usd": 0.08,
                "total_sessions": 5,
                "total_messages": 15,
                "tasks_completed": 3,
                "tasks_failed": 0,
                "messages_per_task": 5.0,
                "avg_sessions_per_task": 1.67,
                "avg_seconds_per_task": 66.7,
            },
            "acceptance": {
                "Task 1": [{"passed": True}],
                "Task 2": [{"passed": True}, {"passed": True}],
            },
        }

        d1 = tmp_path / "run1" / "results"
        d2 = tmp_path / "run2" / "results"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        (d1 / "run_results.json").write_text(json.dumps(r1))
        (d2 / "run_results.json").write_text(json.dumps(r2))

        compare_results(tmp_path)

        output = capsys.readouterr().out
        assert "EVAL RUN COMPARISON" in output
        assert "ship-fast" in output
        assert "quality-first" in output
        assert "Criteria passed" in output

    def test_no_results_found(self, tmp_path, capsys):
        """Handles empty results directory gracefully."""
        compare_results(tmp_path)
        output = capsys.readouterr().out
        assert "No run results found" in output
