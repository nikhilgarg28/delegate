"""Eval harness — charter variants, quality metrics, and LLM-as-judge scoring.

Charter variants override the default charter templates used during bootstrap.
A variant is a directory of .md files under scripts/charter/variants/<name>/.
Only files that differ from the default need to be included; missing files
fall back to the default charter.

Usage:
    python -m scripts.eval list-variants
    python -m scripts.eval load-variant <variant_name>
    python -m scripts.eval bootstrap --root <dir> --variant <name> --manager <m> --director <d> --agents a,b
    python -m scripts.eval metrics --run-dir /path/to/run
    python -m scripts.eval judge --run-dir /path/to/run [--reps 3]
"""

import argparse
import json
import logging
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import yaml

from scripts.bootstrap import bootstrap, CHARTER_DIR

logger = logging.getLogger(__name__)

VARIANTS_DIR = CHARTER_DIR / "variants"

# Default charter filenames (the ones shipped in scripts/charter/)
DEFAULT_CHARTER_FILES = [
    "constitution.md",
    "communication.md",
    "task-management.md",
    "code-review.md",
    "manager.md",
]


def list_variants() -> list[str]:
    """Return the names of all available charter variants.

    Each subdirectory of scripts/charter/variants/ that contains at least
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
        variant_name: Name of the variant directory under scripts/charter/variants/.

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
        default_path = CHARTER_DIR / filename
        if default_path.is_file():
            result[filename] = default_path.read_text()

    # Override with variant files
    for md_file in sorted(variant_dir.glob("*.md")):
        logger.info("Variant '%s' overrides %s", variant_name, md_file.name)
        result[md_file.name] = md_file.read_text()

    return result


def bootstrap_with_variant(
    root: Path,
    variant_name: str,
    manager: str = "manager",
    director: str = "director",
    agents: list[str] | None = None,
) -> None:
    """Bootstrap a team directory, then apply a charter variant.

    Wraps the standard bootstrap() to create the full directory structure,
    then overwrites the charter files with the variant's versions.

    Args:
        root: Team root directory.
        variant_name: Name of the charter variant to apply.
        manager: Name of the manager agent.
        director: Name of the human director.
        agents: Additional agent (worker) names.
    """
    # Step 1: normal bootstrap
    bootstrap(root, manager=manager, director=director, agents=agents)

    # Step 2: load variant and overwrite charter files
    charter = load_variant(variant_name)
    charter_dest = root / ".standup" / "charter"
    charter_dest.mkdir(parents=True, exist_ok=True)

    for filename, content in charter.items():
        dest = charter_dest / filename
        dest.write_text(content)
        logger.info("Wrote variant charter file: %s", dest)

    logger.info(
        "Bootstrapped team at %s with variant '%s'", root, variant_name
    )


# ---------------------------------------------------------------------------
# Metrics collection (T0032)
# ---------------------------------------------------------------------------


def _collect_db_metrics(db_path: Path) -> dict:
    """Collect metrics from the run's db.sqlite."""
    metrics: dict = {}

    if not db_path.is_file():
        logger.warning("db.sqlite not found at %s — skipping DB metrics", db_path)
        return metrics

    conn = sqlite3.connect(str(db_path))
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


def _collect_task_metrics(tasks_dir: Path) -> dict:
    """Collect task completion metrics from YAML task files."""
    metrics: dict = {}
    completed = 0
    failed = 0
    total_tasks = 0

    if not tasks_dir.is_dir():
        logger.warning("Tasks directory not found at %s", tasks_dir)
        metrics["tasks_completed"] = 0
        metrics["tasks_failed"] = 0
        metrics["messages_per_task"] = 0.0
        return metrics

    for f in sorted(tasks_dir.glob("T*.yaml")):
        task = yaml.safe_load(f.read_text())
        if task is None:
            continue
        total_tasks += 1
        status = task.get("status", "")
        if status == "done":
            completed += 1
        elif status in ("open", "in_progress", "review"):
            # Tasks not completed by end of run count as failed
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


def collect_metrics(run_dir: Path) -> dict:
    """Collect all metrics from a completed eval run directory.

    Gathers data from:
    - db.sqlite: token usage, cost, sessions, messages
    - Task YAML files: completion/failure counts
    - Git diffs + external tools: lint violations, type errors, complexity, diff size

    Args:
        run_dir: Path to the eval run's team root directory.

    Returns:
        Flat dict of metric_name -> numeric value (or None if tool unavailable).
        Suitable for JSON serialization.
    """
    run_dir = Path(run_dir)
    standup = run_dir / ".standup"

    # DB metrics
    metrics = _collect_db_metrics(standup / "db.sqlite")

    # Task metrics
    task_metrics = _collect_task_metrics(standup / "tasks")
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


def judge_run(run_dir: Path, reps: int = 3) -> dict:
    """Score all tasks in an eval run directory using LLM-as-judge.

    For each task, extracts the git diff and task spec, calls judge_diff()
    multiple times (reps), and averages the scores.

    Args:
        run_dir: Path to the eval run's team root directory.
        reps: Number of independent judge calls per task (averaged).

    Returns:
        Dict with:
        - tasks: {task_id: {dim: avg_score, ..., avg: float, reasoning: [str, ...]}}
        - overall: {dim: avg_score, ..., avg: float}
    """
    run_dir = Path(run_dir)
    standup = run_dir / ".standup"
    tasks_dir = standup / "tasks"

    if not tasks_dir.is_dir():
        logger.warning("No tasks directory at %s", tasks_dir)
        return {"tasks": {}, "overall": {}}

    # Collect task specs
    tasks_data = {}
    for f in sorted(tasks_dir.glob("T*.yaml")):
        task = yaml.safe_load(f.read_text())
        if task is None:
            continue
        task_id = f.stem  # e.g. "T0001"
        tasks_data[task_id] = task

    if not tasks_data:
        logger.warning("No tasks found in %s", tasks_dir)
        return {"tasks": {}, "overall": {}}

    # Get the full diff for the run
    # TODO(T0031): Once eval runner provides per-task diffs, use those instead
    full_diff = _get_full_diff(run_dir)

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


def main():
    parser = argparse.ArgumentParser(
        description="Eval harness — charter variants, metrics, and judge scoring"
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
    p_boot.add_argument("--root", type=Path, required=True, help="Team root directory")
    p_boot.add_argument("--variant", required=True, help="Charter variant name")
    p_boot.add_argument("--manager", required=True, help="Manager name")
    p_boot.add_argument("--director", required=True, help="Director name")
    p_boot.add_argument(
        "--agents", default="", help="Comma-separated worker names"
    )

    # metrics
    p_metrics = sub.add_parser(
        "metrics", help="Collect and display quality metrics from an eval run"
    )
    p_metrics.add_argument(
        "--run-dir", type=Path, required=True,
        help="Path to the eval run's team root directory",
    )

    # judge
    p_judge = sub.add_parser(
        "judge", help="Run LLM-as-judge scoring on an eval run"
    )
    p_judge.add_argument(
        "--run-dir", type=Path, required=True,
        help="Path to the eval run's team root directory",
    )
    p_judge.add_argument(
        "--reps", type=int, default=3,
        help="Number of independent judge calls per task (default: 3)",
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
            args.root,
            variant_name=args.variant,
            manager=args.manager,
            director=args.director,
            agents=agents,
        )
        all_names = [args.manager, args.director] + agents
        print(
            f"Bootstrapped team at {args.root} with variant '{args.variant}' "
            f"and members: {', '.join(all_names)}"
        )

    elif args.command == "metrics":
        run_dir = args.run_dir
        if not (run_dir / ".standup").is_dir():
            print(f"Error: {run_dir} does not look like a team root directory "
                  "(no .standup/ found)")
            return
        metrics = collect_metrics(run_dir)
        print_metrics_table(metrics)

    elif args.command == "judge":
        run_dir = args.run_dir
        if not (run_dir / ".standup").is_dir():
            print(f"Error: {run_dir} does not look like a team root directory "
                  "(no .standup/ found)")
            return
        results = judge_run(run_dir, reps=args.reps)
        print_judge_results(results)


if __name__ == "__main__":
    main()
