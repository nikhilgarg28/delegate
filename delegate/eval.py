"""Eval harness — charter variants, quality metrics, and LLM-as-judge scoring.

Charter variants override the default charter templates used during bootstrap.
A variant is a directory of .md files under boss/charter/variants/<name>/.
Only files that differ from the default need to be included; missing files
fall back to the default charter.

Usage:
    python -m delegate.eval list-variants
    python -m delegate.eval load-variant <variant_name>
    python -m delegate.eval bootstrap --home <dir> --team <name> --variant <name> --manager <m> --agents a,b
    python -m delegate.eval metrics --run-dir /path/to/run
    python -m delegate.eval judge --run-dir /path/to/run [--reps 3]
"""

import argparse
import asyncio
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from delegate.bootstrap import bootstrap
from delegate.paths import (
    db_path as _db_path,
    agents_dir as _agents_dir,
    team_dir as _team_dir,
    base_charter_dir,
)
from delegate.config import set_boss, get_boss
from delegate.task import format_task_id

logger = logging.getLogger(__name__)

VARIANTS_DIR = base_charter_dir() / "variants"

# Default charter filenames (the ones shipped in boss/charter/)
DEFAULT_CHARTER_FILES = [
    "constitution.md",
    "communication.md",
    "task-management.md",
    "code-review.md",
    "manager.md",
    "continuous-improvement.md",
]

# Default team name for eval runs
EVAL_TEAM = "eval"


def list_variants() -> list[str]:
    """Return the names of all available charter variants.

    Each subdirectory of boss/charter/variants/ that contains at least
    one .md file is considered a variant.
    """
    if not VARIANTS_DIR.is_dir():
        return []
    variants = []
    for entry in sorted(VARIANTS_DIR.iterdir()):
        if entry.is_dir() and any(entry.glob("*.md")):
            variants.append(entry.name)
    return variants


def load_variant(variant_name: str) -> dict[str, str]:
    """Load a charter variant, falling back to defaults for missing files.

    Args:
        variant_name: Name of the variant directory under boss/charter/variants/.

    Returns:
        Dict mapping filename (e.g. "constitution.md") to file content.
        Includes all default charter files — overridden by the variant where
        the variant provides its own version.

    Raises:
        FileNotFoundError: If the variant directory does not exist.
    """
    variant_dir = VARIANTS_DIR / variant_name
    if not variant_dir.is_dir():
        raise FileNotFoundError(
            f"Variant '{variant_name}' not found at {variant_dir}"
        )

    # Start with defaults
    result: dict[str, str] = {}
    for filename in DEFAULT_CHARTER_FILES:
        default_path = base_charter_dir() / filename
        if default_path.is_file():
            result[filename] = default_path.read_text()

    # Override with variant files
    for md_file in sorted(variant_dir.glob("*.md")):
        logger.info("Variant '%s' overrides %s", variant_name, md_file.name)
        result[md_file.name] = md_file.read_text()

    return result


def bootstrap_with_variant(
    hc_home: Path,
    team_name: str = EVAL_TEAM,
    variant_name: str = "default",
    manager: str = "manager",
    boss: str = "boss",
    agents: list[str] | None = None,
) -> None:
    """Bootstrap a team under hc_home, then apply a charter variant.

    Wraps the standard bootstrap() to create the full directory structure,
    then writes the variant's charter files as the team's override.md.

    For eval runs, also creates a boss agent directory so the
    sim-boss can have an inbox/outbox.

    Args:
        hc_home: Delegate home directory.
        team_name: Name for the eval team.
        variant_name: Name of the charter variant to apply.
        manager: Name of the manager agent.
        boss: Name of the human boss.
        agents: Additional agent (worker) names.
    """
    # Step 1: Set the boss in config
    set_boss(hc_home, boss)

    # Step 2: normal bootstrap (also creates boss mailbox at hc_home/boss/)
    bootstrap(hc_home, team_name=team_name, manager=manager, agents=agents)

    # Step 3: load variant and write as team override.md
    if variant_name and variant_name != "default":
        charter = load_variant(variant_name)
        td = _team_dir(hc_home, team_name)
        # Combine all variant overrides into a single override.md
        override_parts = []
        for filename, content in sorted(charter.items()):
            override_parts.append(f"<!-- Variant override: {filename} -->\n{content}")
        override_path = td / "override.md"
        override_path.write_text("\n\n".join(override_parts))
        logger.info("Wrote variant charter override to: %s", override_path)

    logger.info(
        "Bootstrapped eval team '%s' at %s with variant '%s'", team_name, hc_home, variant_name
    )


# ---------------------------------------------------------------------------
# Metrics collection (T0032)
# ---------------------------------------------------------------------------


def _collect_db_metrics(db_file: Path) -> dict:
    """Collect metrics from the run's db.sqlite."""
    metrics: dict = {}

    if not db_file.is_file():
        logger.warning("db.sqlite not found at %s — skipping DB metrics", db_file)
        return metrics

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row

    # Session metrics
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
            COALESCE(SUM(tokens_out), 0) AS total_tokens_out,
            COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd,
            COUNT(*) AS total_sessions,
            COALESCE(SUM(duration_seconds), 0.0) AS total_wall_clock_seconds
        FROM sessions
        """
    ).fetchone()
    metrics["total_tokens_in"] = row["total_tokens_in"]
    metrics["total_tokens_out"] = row["total_tokens_out"]
    metrics["total_cost_usd"] = row["total_cost_usd"]
    metrics["total_sessions"] = row["total_sessions"]
    metrics["total_wall_clock_seconds"] = row["total_wall_clock_seconds"]

    # Per-task averages from sessions (only where task_id is set)
    task_row = conn.execute(
        """
        SELECT
            COUNT(DISTINCT task_id) AS tasks_with_sessions,
            COUNT(*) AS sessions_with_task
        FROM sessions
        WHERE task_id IS NOT NULL
        """
    ).fetchone()
    tasks_with_sessions = task_row["tasks_with_sessions"]
    if tasks_with_sessions > 0:
        metrics["avg_sessions_per_task"] = round(
            task_row["sessions_with_task"] / tasks_with_sessions, 2
        )
        metrics["avg_seconds_per_task"] = round(
            metrics["total_wall_clock_seconds"] / tasks_with_sessions, 2
        )
    else:
        metrics["avg_sessions_per_task"] = 0.0
        metrics["avg_seconds_per_task"] = 0.0

    # Message metrics
    msg_row = conn.execute(
        """
        SELECT COUNT(*) AS total_messages
        FROM messages
        WHERE type = 'chat'
        """
    ).fetchone()
    metrics["total_messages"] = msg_row["total_messages"]

    conn.close()
    return metrics


def _collect_task_metrics(hc_home_or_td: Path) -> dict:
    """Collect task completion metrics.

    Accepts either an ``hc_home`` directory (reads from SQLite) or a
    legacy ``tasks/`` directory (reads YAML files as fallback).
    """
    from delegate.task import list_tasks

    metrics: dict = {}
    completed = 0
    failed = 0

    try:
        all_tasks = list_tasks(hc_home_or_td)
    except Exception:
        # Fallback: maybe it's a raw tasks/ directory path (legacy tests)
        all_tasks = []
        if hc_home_or_td.is_dir():
            for f in sorted(hc_home_or_td.glob("T*.yaml")):
                task = yaml.safe_load(f.read_text())
                if task:
                    all_tasks.append(task)

    for task in all_tasks:
        status = task.get("status", "")
        if status == "done":
            completed += 1
        elif status in ("open", "in_progress", "review"):
            failed += 1

    metrics["tasks_completed"] = completed
    metrics["tasks_failed"] = failed
    return metrics


def _run_tool(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess | None:
    """Run an external tool, returning None if not installed."""
    tool_name = cmd[0]
    if shutil.which(tool_name) is None:
        logger.warning("%s not installed — skipping", tool_name)
        return None
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out", tool_name)
        return None
    except FileNotFoundError:
        logger.warning("%s not found — skipping", tool_name)
        return None


def _get_changed_files(run_dir: Path) -> list[str]:
    """Get list of changed Python files from git diff in the run directory.

    TODO(T0031): Accept a baseline ref (tag or SHA) instead of hardcoding
    HEAD~1, so multi-commit eval runs diff against the correct starting point.
    """
    result = _run_tool(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD~1"],
        cwd=str(run_dir),
    )
    if result is None or result.returncode != 0:
        # Fallback: try to get all tracked Python files
        result = _run_tool(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "--cached"],
            cwd=str(run_dir),
        )
        if result is None or result.returncode != 0:
            return []

    files = [
        f for f in result.stdout.strip().splitlines()
        if f.endswith(".py")
    ]
    return files


def _get_diff_size(run_dir: Path) -> int | None:
    """Get total diff size (lines added + removed) from git.

    TODO(T0031): Accept a baseline ref (tag or SHA) instead of hardcoding
    HEAD~1, so multi-commit eval runs diff against the correct starting point.
    """
    result = _run_tool(
        ["git", "diff", "--stat", "HEAD~1"],
        cwd=str(run_dir),
    )
    if result is None or result.returncode != 0:
        return None

    # Parse the summary line: " N files changed, X insertions(+), Y deletions(-)"
    lines = result.stdout.strip().splitlines()
    if not lines:
        return 0

    total = 0
    summary = lines[-1]
    insertions = re.search(r"(\d+) insertion", summary)
    deletions = re.search(r"(\d+) deletion", summary)
    if insertions:
        total += int(insertions.group(1))
    if deletions:
        total += int(deletions.group(1))
    return total


def _count_lint_violations(run_dir: Path, changed_files: list[str]) -> int | None:
    """Run ruff check on changed files, count violations."""
    if not changed_files:
        return 0

    # Filter to files that actually exist
    existing = [f for f in changed_files if (run_dir / f).is_file()]
    if not existing:
        return 0

    result = _run_tool(
        ["ruff", "check", "--quiet"] + existing,
        cwd=str(run_dir),
    )
    if result is None:
        return None

    # Each non-empty output line is a violation
    output = result.stdout.strip()
    if not output:
        return 0
    return len(output.splitlines())


def _count_type_errors(run_dir: Path, changed_files: list[str]) -> int | None:
    """Run pyright or mypy on changed files, count errors."""
    if not changed_files:
        return 0

    existing = [f for f in changed_files if (run_dir / f).is_file()]
    if not existing:
        return 0

    # Try pyright first, then mypy
    for tool in ["pyright", "mypy"]:
        result = _run_tool(
            [tool] + existing,
            cwd=str(run_dir),
        )
        if result is not None:
            # Count lines containing "error" in stdout+stderr
            all_output = (result.stdout + result.stderr).strip()
            if not all_output:
                return 0
            error_lines = [
                line for line in all_output.splitlines()
                if "error" in line.lower()
            ]
            return len(error_lines)

    return None


def _compute_complexity(run_dir: Path, changed_files: list[str]) -> float | None:
    """Run radon cc on changed files, return average complexity."""
    if not changed_files:
        return 0.0

    existing = [f for f in changed_files if (run_dir / f).is_file()]
    if not existing:
        return 0.0

    result = _run_tool(
        ["radon", "cc", "--average", "--show-complexity"] + existing,
        cwd=str(run_dir),
    )
    if result is None:
        return None

    # radon outputs "Average complexity: X.XX (Y)" on the last non-empty line
    match = re.search(r"Average complexity:\s+([A-F])\s+\(([0-9.]+)\)", result.stdout)
    if match:
        return float(match.group(2))

    return None


def collect_metrics(hc_home: Path, run_dir: Path | None = None) -> dict:
    """Collect all metrics from a completed eval run.

    Gathers data from:
    - db.sqlite: token usage, cost, sessions, messages
    - Task YAML files: completion/failure counts
    - Git diffs + external tools: lint violations, type errors, complexity, diff size

    Args:
        hc_home: The boss home directory for this run.
        run_dir: Optional working directory for git-based metrics.
                 If None, git metrics are skipped.

    Returns:
        Flat dict of metric_name -> numeric value (or None if tool unavailable).
        Suitable for JSON serialization.
    """
    # DB metrics
    metrics = _collect_db_metrics(_db_path(hc_home))

    # Task metrics
    task_metrics = _collect_task_metrics(hc_home)
    metrics.update(task_metrics)

    # Compute messages_per_task
    total_tasks = metrics.get("tasks_completed", 0) + metrics.get("tasks_failed", 0)
    if total_tasks > 0:
        metrics["messages_per_task"] = round(
            metrics.get("total_messages", 0) / total_tasks, 2
        )
    else:
        metrics["messages_per_task"] = 0.0

    # Git-based metrics (only if run_dir is a git repo)
    if run_dir:
        changed_files = _get_changed_files(run_dir)
        metrics["diff_size"] = _get_diff_size(run_dir)
        metrics["lint_violations"] = _count_lint_violations(run_dir, changed_files)
        metrics["type_errors"] = _count_type_errors(run_dir, changed_files)
        metrics["complexity_score"] = _compute_complexity(run_dir, changed_files)

    return metrics


def print_metrics_table(metrics: dict) -> None:
    """Print metrics as a formatted table."""
    print()
    print("=" * 50)
    print("  EVAL RUN SCORECARD")
    print("=" * 50)

    sections = [
        ("Token Usage", [
            ("total_tokens_in", "Tokens in"),
            ("total_tokens_out", "Tokens out"),
            ("total_cost_usd", "Cost (USD)"),
        ]),
        ("Sessions", [
            ("total_sessions", "Total sessions"),
            ("avg_sessions_per_task", "Avg sessions/task"),
            ("total_wall_clock_seconds", "Total wall-clock (s)"),
            ("avg_seconds_per_task", "Avg seconds/task"),
        ]),
        ("Messages", [
            ("total_messages", "Total messages"),
            ("messages_per_task", "Messages/task"),
        ]),
        ("Tasks", [
            ("tasks_completed", "Completed"),
            ("tasks_failed", "Failed"),
        ]),
        ("Code Quality", [
            ("diff_size", "Diff size (lines)"),
            ("lint_violations", "Lint violations"),
            ("type_errors", "Type errors"),
            ("complexity_score", "Avg complexity"),
        ]),
    ]

    for section_name, fields in sections:
        print(f"\n  {section_name}")
        print("  " + "-" * 40)
        for key, label in fields:
            value = metrics.get(key)
            if value is None:
                display = "N/A (tool not installed)"
            elif isinstance(value, float):
                display = f"{value:.2f}"
            else:
                display = str(value)
            print(f"    {label:<28} {display}")

    print()
    print("=" * 50)


# ---------------------------------------------------------------------------
# LLM-as-judge scoring (T0033)
# ---------------------------------------------------------------------------

RUBRIC_DIMENSIONS = ["correctness", "readability", "style", "test_quality", "simplicity"]

DEFAULT_RUBRIC = """\
Score the code diff against the task spec on each dimension (1-5):

- correctness: Does the code do what the spec asks? (5 = fully correct, 1 = completely wrong)
- readability: Is the code clear and well-structured? (5 = very clear, 1 = incomprehensible)
- style: Is it idiomatic for the language/framework? (5 = idiomatic, 1 = non-idiomatic)
- test_quality: Are tests meaningful and well-written? (5 = thorough, 1 = no tests or meaningless)
- simplicity: Is the solution appropriately simple? (5 = elegantly simple, 1 = over-engineered)
"""

_JUDGE_SYSTEM_PROMPT = """\
You are a code reviewer. Score this diff against the task spec using the following rubric. \
Return JSON only — no markdown fences, no commentary outside the JSON object.

{rubric}

Return a JSON object with exactly these keys:
- correctness (int, 1-5)
- readability (int, 1-5)
- style (int, 1-5)
- test_quality (int, 1-5)
- simplicity (int, 1-5)
- reasoning (string, 1-3 sentences explaining the scores)
"""


def _call_llm(system: str, user: str, model: str = "claude-sonnet-4-20250514") -> str:
    """Make a single LLM API call and return the text response.

    Uses the anthropic SDK for a straightforward one-shot prompt.
    """
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # Extract text from the response
    return message.content[0].text


def _parse_judge_response(text: str) -> dict:
    """Parse the JSON response from the judge LLM.

    Handles common issues like markdown code fences around JSON.

    Raises:
        ValueError: If the response cannot be parsed as valid judge JSON.
    """
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag)
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        cleaned = cleaned.strip()

    data = json.loads(cleaned)

    # Validate required keys and value ranges
    for dim in RUBRIC_DIMENSIONS:
        if dim not in data:
            raise ValueError(f"Missing required dimension: {dim}")
        score = data[dim]
        if not isinstance(score, (int, float)) or not (1 <= score <= 5):
            raise ValueError(f"Score for {dim} must be 1-5, got {score!r}")
        data[dim] = int(data[dim])

    if "reasoning" not in data:
        raise ValueError("Missing required key: reasoning")

    return data


def judge_diff(diff: str, task_spec: str, rubric: str = DEFAULT_RUBRIC) -> dict:
    """Score a git diff against a task spec using an LLM judge.

    Calls Claude with a fixed reviewer prompt and rubric.  Retries once on
    JSON parse failure.

    Args:
        diff: The git diff text to evaluate.
        task_spec: The task specification the diff should satisfy.
        rubric: Scoring rubric text (uses DEFAULT_RUBRIC if not provided).

    Returns:
        Dict with keys: correctness, readability, style, test_quality,
        simplicity (each int 1-5), avg (float), and reasoning (str).
    """
    system = _JUDGE_SYSTEM_PROMPT.format(rubric=rubric)
    user_msg = f"## Task Spec\n\n{task_spec}\n\n## Git Diff\n\n```diff\n{diff}\n```"

    last_error = None
    for attempt in range(2):  # retry once on parse failure
        try:
            raw = _call_llm(system, user_msg)
            scores = _parse_judge_response(raw)
            # Compute average
            dim_scores = [scores[d] for d in RUBRIC_DIMENSIONS]
            scores["avg"] = round(sum(dim_scores) / len(dim_scores), 2)
            return scores
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            last_error = exc
            logger.warning(
                "Judge parse failed (attempt %d/2): %s", attempt + 1, exc
            )

    raise ValueError(f"Failed to parse judge response after 2 attempts: {last_error}")


def judge_run(hc_home: Path, reps: int = 3, run_dir: Path | None = None) -> dict:
    """Score all tasks in an eval run using LLM-as-judge.

    For each task, extracts the git diff and task spec, calls judge_diff()
    multiple times (reps), and averages the scores.

    Args:
        hc_home: Delegate home directory.
        reps: Number of independent judge calls per task (averaged).
        run_dir: Optional working directory for git diff.

    Returns:
        Dict with:
        - tasks: {task_id: {dim: avg_score, ..., avg: float, reasoning: [str, ...]}}
        - overall: {dim: avg_score, ..., avg: float}
    """
    from delegate.task import list_tasks as _lt, format_task_id as _fmt

    all_tasks = _lt(hc_home)

    # Collect task specs
    tasks_data = {}
    for task in all_tasks:
        if task is None:
            continue
        task_id = _fmt(task["id"])  # e.g. "T0001"
        tasks_data[task_id] = task

    if not tasks_data:
        logger.warning("No tasks found in %s", hc_home)
        return {"tasks": {}, "overall": {}}

    # Get the full diff for the run
    full_diff = _get_full_diff(run_dir) if run_dir else "(no diff available)"

    task_scores = {}
    for task_id, task in tasks_data.items():
        title = task.get("title", "")
        description = task.get("description", "")
        task_spec = f"Title: {title}\n\nDescription:\n{description}"

        # Use the full diff for now (per-task diffs depend on eval runner)
        diff = full_diff

        rep_scores = []
        for r in range(reps):
            try:
                scores = judge_diff(diff, task_spec)
                rep_scores.append(scores)
            except ValueError:
                logger.warning(
                    "Skipping rep %d for %s: judge_diff failed", r + 1, task_id
                )

        if not rep_scores:
            logger.warning("All reps failed for %s — skipping", task_id)
            continue

        # Average across reps
        averaged = _average_scores(rep_scores)
        task_scores[task_id] = averaged

    # Overall averages across all tasks
    overall = _average_scores(list(task_scores.values())) if task_scores else {}

    return {"tasks": task_scores, "overall": overall}


def _get_full_diff(run_dir: Path) -> str:
    """Get the full git diff text for the run directory.

    TODO(T0031): Accept a baseline ref so multi-commit runs diff correctly.
    """
    result = _run_tool(
        ["git", "diff", "HEAD~1"],
        cwd=str(run_dir),
    )
    if result is None or result.returncode != 0:
        # Fallback to cached diff
        result = _run_tool(
            ["git", "diff", "--cached"],
            cwd=str(run_dir),
        )
        if result is None or result.returncode != 0:
            return "(no diff available)"

    return result.stdout


def _average_scores(score_dicts: list[dict]) -> dict:
    """Average numeric scores across a list of score dicts.

    Also collects reasoning strings into a list.
    """
    if not score_dicts:
        return {}

    averaged: dict = {}
    for dim in RUBRIC_DIMENSIONS:
        values = [s[dim] for s in score_dicts if dim in s]
        if values:
            averaged[dim] = round(sum(values) / len(values), 2)

    # Compute overall average from dimension averages
    dim_values = [averaged[d] for d in RUBRIC_DIMENSIONS if d in averaged]
    if dim_values:
        averaged["avg"] = round(sum(dim_values) / len(dim_values), 2)

    # Collect reasoning strings
    reasonings = [s.get("reasoning", "") for s in score_dicts if s.get("reasoning")]
    if reasonings:
        averaged["reasoning"] = reasonings

    return averaged


def print_judge_results(results: dict) -> None:
    """Print judge results as a formatted table."""
    print()
    print("=" * 60)
    print("  LLM-AS-JUDGE SCORES")
    print("=" * 60)

    tasks = results.get("tasks", {})
    if not tasks:
        print("\n  No tasks scored.")
        print("=" * 60)
        return

    for task_id, scores in sorted(tasks.items()):
        print(f"\n  {task_id}")
        print("  " + "-" * 50)
        for dim in RUBRIC_DIMENSIONS:
            val = scores.get(dim)
            if val is not None:
                print(f"    {dim:<20} {val:.1f}")
        avg = scores.get("avg")
        if avg is not None:
            print(f"    {'average':<20} {avg:.2f}")
        reasonings = scores.get("reasoning", [])
        if reasonings:
            # Show first reasoning as summary
            r = reasonings[0] if isinstance(reasonings, list) else reasonings
            print(f"    reasoning: {r}")

    overall = results.get("overall", {})
    if overall:
        print(f"\n  OVERALL")
        print("  " + "-" * 50)
        for dim in RUBRIC_DIMENSIONS:
            val = overall.get(dim)
            if val is not None:
                print(f"    {dim:<20} {val:.1f}")
        avg = overall.get("avg")
        if avg is not None:
            print(f"    {'average':<20} {avg:.2f}")

    print()
    print("=" * 60)


# ---------------------------------------------------------------------------
# Eval runner orchestration (T0031)
# ---------------------------------------------------------------------------


def load_benchmark_specs(suite_dir: Path) -> list[dict]:
    """Load all benchmark task specs from a suite directory.

    Args:
        suite_dir: Bossy containing benchmark YAML files.

    Returns:
        List of parsed benchmark spec dicts (each has title, description,
        acceptance_criteria, etc.).
    """
    specs = []
    if not suite_dir.is_dir():
        logger.warning("Suite directory not found: %s", suite_dir)
        return specs

    for yaml_file in sorted(suite_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_file.read_text())
            if data and "title" in data:
                data["_source_file"] = str(yaml_file)
                specs.append(data)
                logger.info("Loaded benchmark spec: %s", data["title"])
        except Exception:
            logger.exception("Failed to load benchmark spec from %s", yaml_file)

    return specs


def seed_tasks(hc_home: Path, specs: list[dict]) -> list[dict]:
    """Create tasks from benchmark specs via the task system.

    Args:
        hc_home: Delegate home directory.
        specs: List of benchmark spec dicts.

    Returns:
        List of created task dicts (with IDs assigned).
    """
    from delegate.task import create_task

    created = []
    for spec in specs:
        task = create_task(
            hc_home,
            title=spec["title"],
            description=spec.get("description", ""),
            priority="high",
        )
        created.append(task)
        logger.info("Seeded task %s: %s", task["id"], spec["title"])

    return created


def setup_repo(run_dir: Path, specs: list[dict]) -> None:
    """Set up repo files from benchmark spec repo_setup entries.

    Creates any files specified in the repo_setup section of each spec
    in the eval working directory.

    Args:
        run_dir: Working directory for the eval run.
        specs: List of benchmark spec dicts.
    """
    for spec in specs:
        for entry in spec.get("repo_setup", []):
            path = run_dir / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(entry.get("content", ""))
            logger.info("Set up repo file: %s", path)


def check_acceptance_criteria(run_dir: Path, specs: list[dict]) -> dict[str, list[dict]]:
    """Run acceptance criteria checks from benchmark specs.

    Each criterion is checked and returns pass/fail with details.

    Args:
        run_dir: Working directory for the eval run.
        specs: List of benchmark spec dicts with acceptance_criteria.

    Returns:
        Dict mapping task title to list of {type, details, passed} dicts.
    """
    results: dict[str, list[dict]] = {}

    for spec in specs:
        title = spec["title"]
        criteria_results = []

        for criterion in spec.get("acceptance_criteria", []):
            result = _check_single_criterion(run_dir, criterion)
            criteria_results.append(result)

        results[title] = criteria_results

    return results


def _check_single_criterion(run_dir: Path, criterion: dict) -> dict:
    """Check a single acceptance criterion.

    Returns dict with: type, details, passed (bool), error (optional).
    """
    if "file_exists" in criterion:
        spec = criterion["file_exists"]
        path = run_dir / spec["path"]
        passed = path.is_file()
        return {
            "type": "file_exists",
            "details": {"path": spec["path"]},
            "passed": passed,
            "error": None if passed else f"File not found: {spec['path']}",
        }

    elif "tests_pass" in criterion:
        spec = criterion["tests_pass"]
        cmd = spec["command"]
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=120, cwd=str(run_dir),
            )
            passed = result.returncode == 0
            return {
                "type": "tests_pass",
                "details": {"command": cmd},
                "passed": passed,
                "error": None if passed else result.stderr[:500],
            }
        except subprocess.TimeoutExpired:
            return {
                "type": "tests_pass",
                "details": {"command": cmd},
                "passed": False,
                "error": "Command timed out",
            }

    elif "grep_match" in criterion:
        spec = criterion["grep_match"]
        path = run_dir / spec["path"]
        pattern = spec["pattern"]
        if not path.is_file():
            return {
                "type": "grep_match",
                "details": {"path": spec["path"], "pattern": pattern},
                "passed": False,
                "error": f"File not found: {spec['path']}",
            }
        content = path.read_text()
        try:
            passed = bool(re.search(pattern, content))
        except re.error:
            # Pattern may contain regex special chars intended as literals
            # Fall back to literal string matching
            passed = pattern in content
        return {
            "type": "grep_match",
            "details": {"path": spec["path"], "pattern": pattern},
            "passed": passed,
            "error": None if passed else f"Pattern not found in {spec['path']}",
        }

    elif "command_succeeds" in criterion:
        spec = criterion["command_succeeds"]
        cmd = spec["command"]
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=120, cwd=str(run_dir),
            )
            passed = result.returncode == 0
            return {
                "type": "command_succeeds",
                "details": {"command": cmd},
                "passed": passed,
                "error": None if passed else result.stderr[:500],
            }
        except subprocess.TimeoutExpired:
            return {
                "type": "command_succeeds",
                "details": {"command": cmd},
                "passed": False,
                "error": "Command timed out",
            }

    return {
        "type": "unknown",
        "details": criterion,
        "passed": False,
        "error": f"Unknown criterion type: {list(criterion.keys())}",
    }


def _run_daemon_loop(
    hc_home: Path,
    team: str,
    stop_event: threading.Event,
    interval: float = 1.0,
    max_concurrent: int = 3,
    token_budget: int | None = None,
) -> None:
    """Run the daemon loop (router + orchestrator) in a thread.

    This replicates the logic from delegate/web.py:_daemon_loop but runs
    synchronously in a thread instead of as an async task.
    """
    from delegate.router import route_once
    from delegate.orchestrator import orchestrate_once, spawn_agent_subprocess

    def _spawn(h: Path, t: str, a: str) -> None:
        spawn_agent_subprocess(h, t, a, token_budget=token_budget)

    logger.info("Eval daemon loop started — polling every %.1fs", interval)

    while not stop_event.is_set():
        try:
            routed = route_once(hc_home, team)
            if routed > 0:
                logger.info("Routed %d message(s)", routed)

            spawned = orchestrate_once(
                hc_home, team,
                max_concurrent=max_concurrent,
                spawn_fn=_spawn,
            )
            if spawned:
                logger.info("Spawned agents: %s", ", ".join(spawned))
        except Exception:
            logger.exception("Error during eval daemon cycle")

        stop_event.wait(timeout=interval)

    logger.info("Eval daemon loop stopped")


def _poll_tasks_done(hc_home: Path, task_count: int, timeout: float) -> bool:
    """Poll until all tasks reach 'done' status or timeout.

    Returns True if all tasks completed, False on timeout.
    """
    from delegate.task import list_tasks

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        tasks = list_tasks(hc_home)
        done_count = sum(1 for t in tasks if t["status"] == "done")
        total = len(tasks)

        if total > 0 and done_count >= task_count:
            logger.info("All %d tasks completed", task_count)
            return True

        elapsed = time.monotonic() - start
        logger.info(
            "Progress: %d/%d tasks done (%.0fs elapsed, %.0fs remaining)",
            done_count, task_count, elapsed, timeout - elapsed,
        )
        time.sleep(5.0)

    logger.warning("Timeout reached (%.0fs) — not all tasks completed", timeout)
    return False


def run_eval(
    variant: str,
    suite: str | Path,
    timeout: float = 600,
    dry_run: bool = False,
    manager: str = "manager",
    boss: str = "boss",
    agents: list[str] | None = None,
    max_concurrent: int = 3,
    token_budget: int | None = None,
) -> dict:
    """Run a full eval: bootstrap, seed tasks, run agents, check results.

    Args:
        variant: Charter variant name (e.g. "ship-fast").
        suite: Path to benchmark tasks directory.
        timeout: Max seconds to wait for all tasks to complete.
        dry_run: If True, validate the pipeline without spawning agents.
        manager: Name of the manager agent.
        boss: Name of the boss.
        agents: Worker agent names (default: ["alice", "bob"]).
        max_concurrent: Max concurrent agent processes.
        token_budget: Token budget per agent session.

    Returns:
        Structured results dict with keys:
        - run_dir: Path to the eval run directory
        - hc_home: Path to the boss home directory
        - variant: Charter variant used
        - suite: Benchmark suite path
        - dry_run: Whether this was a dry run
        - tasks_seeded: Number of tasks created
        - completed: Whether all tasks finished
        - timed_out: Whether the run timed out
        - acceptance: Acceptance criteria results
        - metrics: Raw metrics from db.sqlite
        - started_at: ISO timestamp
        - ended_at: ISO timestamp
        - duration_seconds: Total wall-clock time
    """
    suite_dir = Path(suite)
    agents = agents or ["alice", "bob"]
    started_at = datetime.now(timezone.utc)

    # 1. Create a temp directory for the eval run's boss home
    hc_home = Path(tempfile.mkdtemp(prefix="eval-hc-"))
    # Also create a temp working directory for repo files
    run_dir = Path(tempfile.mkdtemp(prefix="eval-run-"))
    logger.info("Eval run: hc_home=%s run_dir=%s", hc_home, run_dir)

    results: dict = {
        "run_dir": str(run_dir),
        "hc_home": str(hc_home),
        "variant": variant,
        "suite": str(suite_dir),
        "dry_run": dry_run,
        "started_at": started_at.isoformat(),
    }

    team = EVAL_TEAM

    try:
        # 2. Bootstrap a fresh team with the variant
        bootstrap_with_variant(
            hc_home,
            team_name=team,
            variant_name=variant,
            manager=manager,
            boss=boss,
            agents=agents,
        )
        logger.info("Bootstrapped eval team at %s with variant '%s'", hc_home, variant)

        # 3. Load benchmark specs
        specs = load_benchmark_specs(suite_dir)
        if not specs:
            results["error"] = "No benchmark specs found"
            results["tasks_seeded"] = 0
            return results

        # 4. Set up repo files from specs
        setup_repo(run_dir, specs)

        # 5. Seed the task queue
        created_tasks = seed_tasks(hc_home, specs)
        results["tasks_seeded"] = len(created_tasks)
        logger.info("Seeded %d tasks", len(created_tasks))

        # Build task specs dict for sim-boss
        task_specs = {s["title"]: s.get("description", "") for s in specs}

        if dry_run:
            # Validate only — don't start agents
            logger.info("Dry run — skipping agent execution")
            results["completed"] = False
            results["timed_out"] = False
            results["acceptance"] = {}
            results["metrics"] = {}
            return results

        # 6. Start the sim-boss in a background thread
        from delegate.sim_boss import start_sim_boss_thread

        sim_thread, sim_stop = start_sim_boss_thread(
            hc_home, team, task_specs, poll_interval=2.0,
        )
        logger.info("Sim-boss started")

        # 7. Start the daemon (router + orchestrator) in a background thread
        daemon_stop = threading.Event()
        daemon_thread = threading.Thread(
            target=_run_daemon_loop,
            args=(hc_home, team, daemon_stop),
            kwargs={
                "interval": 1.0,
                "max_concurrent": max_concurrent,
                "token_budget": token_budget,
            },
            daemon=True,
            name="eval-daemon",
        )
        daemon_thread.start()
        logger.info("Eval daemon started")

        # Send a kick message from boss to manager to start work
        from delegate.mailbox import send as mailbox_send

        task_list_msg = "Here are the tasks for this eval run:\n"
        for task in created_tasks:
            task_list_msg += f"- {format_task_id(task['id'])}: {task['title']}\n"
        task_list_msg += "\nPlease assign and complete all tasks."
        mailbox_send(hc_home, team, boss, manager, task_list_msg)

        try:
            # 8. Poll until all tasks reach 'done' or timeout
            all_done = _poll_tasks_done(hc_home, len(created_tasks), timeout)
            results["completed"] = all_done
            results["timed_out"] = not all_done

        finally:
            # 9. Stop daemon + sim-boss
            daemon_stop.set()
            sim_stop.set()
            daemon_thread.join(timeout=10)
            sim_thread.join(timeout=10)
            logger.info("Stopped daemon and sim-boss")

        # 10. Run acceptance criteria checks
        acceptance = check_acceptance_criteria(run_dir, specs)
        results["acceptance"] = acceptance

        # 11. Collect raw metrics
        metrics = collect_metrics(hc_home, run_dir=run_dir)
        results["metrics"] = metrics

    finally:
        ended_at = datetime.now(timezone.utc)
        results["ended_at"] = ended_at.isoformat()
        results["duration_seconds"] = (ended_at - started_at).total_seconds()

        # Save results as JSON
        results_dir = run_dir / "results"
        results_dir.mkdir(exist_ok=True)
        results_file = results_dir / "run_results.json"
        results_file.write_text(json.dumps(results, indent=2, default=str))
        logger.info("Results saved to %s", results_file)

    return results


def compare_results(results_dir: Path) -> None:
    """Load multiple run results and print a side-by-side comparison table.

    Args:
        results_dir: Bossy containing run result JSON files, or parent
            directory containing multiple run bossies each with results/.
    """
    # Find all run_results.json files
    result_files = sorted(results_dir.glob("**/run_results.json"))
    if not result_files:
        print(f"No run results found in {results_dir}")
        return

    runs = []
    for f in result_files:
        try:
            data = json.loads(f.read_text())
            runs.append(data)
        except Exception:
            logger.warning("Failed to load %s", f)

    if not runs:
        print("No valid run results to compare.")
        return

    # Print comparison table
    print()
    print("=" * 80)
    print("  EVAL RUN COMPARISON")
    print("=" * 80)

    # Header
    labels = []
    for r in runs:
        variant = r.get("variant", "unknown")
        dry = " (dry)" if r.get("dry_run") else ""
        labels.append(f"{variant}{dry}")

    col_width = max(20, max(len(l) for l in labels) + 2)
    header = f"  {'Metric':<30}" + "".join(f"{l:>{col_width}}" for l in labels)
    print(f"\n{header}")
    print("  " + "-" * (30 + col_width * len(runs)))

    # Rows
    rows = [
        ("Tasks seeded", "tasks_seeded", None),
        ("Completed", "completed", None),
        ("Timed out", "timed_out", None),
        ("Duration (s)", "duration_seconds", ".1f"),
    ]

    # Metric rows (from nested metrics dict)
    metric_rows = [
        ("Total tokens in", "total_tokens_in", ",d"),
        ("Total tokens out", "total_tokens_out", ",d"),
        ("Total cost (USD)", "total_cost_usd", ".4f"),
        ("Total sessions", "total_sessions", "d"),
        ("Total messages", "total_messages", "d"),
        ("Tasks completed", "tasks_completed", "d"),
        ("Tasks failed", "tasks_failed", "d"),
        ("Messages/task", "messages_per_task", ".2f"),
        ("Avg sessions/task", "avg_sessions_per_task", ".2f"),
        ("Avg seconds/task", "avg_seconds_per_task", ".1f"),
    ]

    for label, key, fmt in rows:
        values = []
        for r in runs:
            val = r.get(key)
            if val is None:
                values.append("—")
            elif fmt:
                values.append(f"{val:{fmt}}")
            else:
                values.append(str(val))
        row_str = f"  {label:<30}" + "".join(f"{v:>{col_width}}" for v in values)
        print(row_str)

    print()
    print(f"  {'--- Metrics ---':<30}")

    for label, key, fmt in metric_rows:
        values = []
        for r in runs:
            metrics = r.get("metrics", {})
            val = metrics.get(key)
            if val is None:
                values.append("—")
            elif fmt:
                try:
                    values.append(f"{val:{fmt}}")
                except (ValueError, TypeError):
                    values.append(str(val))
            else:
                values.append(str(val))
        row_str = f"  {label:<30}" + "".join(f"{v:>{col_width}}" for v in values)
        print(row_str)

    # Acceptance criteria summary
    print()
    print(f"  {'--- Acceptance ---':<30}")

    # Print acceptance as pass/total
    acc_values = []
    for r in runs:
        acceptance = r.get("acceptance", {})
        total = sum(len(v) for v in acceptance.values())
        passed = sum(
            sum(1 for c in v if c.get("passed"))
            for v in acceptance.values()
        )
        acc_values.append(f"{passed}/{total}" if total > 0 else "—")
    row_str = f"  {'Criteria passed':<30}" + "".join(
        f"{v:>{col_width}}" for v in acc_values
    )
    print(row_str)

    print()
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description="Eval harness — charter variants, metrics, judge scoring, and eval runner"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list-variants
    sub.add_parser("list-variants", help="List available charter variants")

    # load-variant
    p_load = sub.add_parser(
        "load-variant", help="Load and display a charter variant"
    )
    p_load.add_argument("variant_name", help="Name of the variant to load")

    # bootstrap
    p_boot = sub.add_parser(
        "bootstrap",
        help="Bootstrap a team with a charter variant applied",
    )
    p_boot.add_argument("--home", type=Path, required=True, help="Delegate home directory")
    p_boot.add_argument("--team", default=EVAL_TEAM, help=f"Team name (default: {EVAL_TEAM})")
    p_boot.add_argument("--variant", required=True, help="Charter variant name")
    p_boot.add_argument("--manager", required=True, help="Manager name")
    p_boot.add_argument("--boss", required=True, help="Boss name")
    p_boot.add_argument(
        "--agents", default="", help="Comma-separated worker names"
    )

    # metrics
    p_metrics = sub.add_parser(
        "metrics", help="Collect and display quality metrics from an eval run"
    )
    p_metrics.add_argument(
        "--home", type=Path, required=True,
        help="Path to the eval run's boss home directory",
    )
    p_metrics.add_argument(
        "--run-dir", type=Path, default=None,
        help="Path to working directory for git-based metrics",
    )

    # judge
    p_judge = sub.add_parser(
        "judge", help="Run LLM-as-judge scoring on an eval run"
    )
    p_judge.add_argument(
        "--home", type=Path, required=True,
        help="Path to the eval run's boss home directory",
    )
    p_judge.add_argument(
        "--run-dir", type=Path, default=None,
        help="Path to working directory for git diff",
    )
    p_judge.add_argument(
        "--reps", type=int, default=3,
        help="Number of independent judge calls per task (default: 3)",
    )

    # run — eval runner
    p_run = sub.add_parser(
        "run", help="Run a full eval: bootstrap, seed tasks, run agents, check results"
    )
    p_run.add_argument(
        "--variant", required=True, help="Charter variant name (e.g. ship-fast)"
    )
    p_run.add_argument(
        "--suite", type=Path, required=True,
        help="Path to benchmark tasks directory",
    )
    p_run.add_argument(
        "--timeout", type=float, default=600,
        help="Max seconds to wait for completion (default: 600)",
    )
    p_run.add_argument(
        "--dry-run", action="store_true",
        help="Validate pipeline without spawning agents",
    )
    p_run.add_argument(
        "--agents", default="alice,bob",
        help="Comma-separated worker names (default: alice,bob)",
    )
    p_run.add_argument(
        "--max-concurrent", type=int, default=3,
        help="Max concurrent agent processes (default: 3)",
    )
    p_run.add_argument(
        "--token-budget", type=int, default=None,
        help="Token budget per agent session",
    )

    # compare — side-by-side comparison
    p_compare = sub.add_parser(
        "compare", help="Compare multiple eval run results side-by-side"
    )
    p_compare.add_argument(
        "--results-dir", type=Path, required=True,
        help="Bossy containing run results (or parent of multiple run dirs)",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.command == "list-variants":
        variants = list_variants()
        if variants:
            print("Available variants:")
            for v in variants:
                print(f"  - {v}")
        else:
            print("No variants found.")

    elif args.command == "load-variant":
        try:
            charter = load_variant(args.variant_name)
        except FileNotFoundError as e:
            print(f"Error: {e}")
            return
        print(f"Variant '{args.variant_name}' — {len(charter)} charter files:")
        for filename in sorted(charter):
            lines = charter[filename].count("\n")
            print(f"  {filename} ({lines} lines)")

    elif args.command == "bootstrap":
        agents = [a.strip() for a in args.agents.split(",") if a.strip()]
        bootstrap_with_variant(
            args.home,
            team_name=args.team,
            variant_name=args.variant,
            manager=args.manager,
            boss=args.boss,
            agents=agents,
        )
        all_names = [args.manager, args.boss] + agents
        print(
            f"Bootstrapped team '{args.team}' at {args.home} with variant '{args.variant}' "
            f"and members: {', '.join(all_names)}"
        )

    elif args.command == "metrics":
        hc_home = args.home
        metrics = collect_metrics(hc_home, run_dir=getattr(args, "run_dir", None))
        print_metrics_table(metrics)

    elif args.command == "judge":
        hc_home = args.home
        results = judge_run(hc_home, reps=args.reps, run_dir=getattr(args, "run_dir", None))
        print_judge_results(results)

    elif args.command == "run":
        agent_names = [a.strip() for a in args.agents.split(",") if a.strip()]
        results = run_eval(
            variant=args.variant,
            suite=args.suite,
            timeout=args.timeout,
            dry_run=args.dry_run,
            agents=agent_names,
            max_concurrent=args.max_concurrent,
            token_budget=args.token_budget,
        )
        print(f"\nEval run {'(dry run) ' if args.dry_run else ''}complete.")
        print(f"  Run directory: {results['run_dir']}")
        print(f"  HC home: {results.get('hc_home', 'N/A')}")
        print(f"  Variant: {results['variant']}")
        print(f"  Tasks seeded: {results.get('tasks_seeded', 0)}")
        if not args.dry_run:
            print(f"  Completed: {results.get('completed', False)}")
            print(f"  Duration: {results.get('duration_seconds', 0):.1f}s")
            # Print acceptance summary
            acceptance = results.get("acceptance", {})
            total = sum(len(v) for v in acceptance.values())
            passed = sum(
                sum(1 for c in v if c.get("passed"))
                for v in acceptance.values()
            )
            print(f"  Acceptance criteria: {passed}/{total} passed")

    elif args.command == "compare":
        compare_results(args.results_dir)


if __name__ == "__main__":
    main()
