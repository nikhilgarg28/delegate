# John's Context

## Current State
- **T0033 (LLM-as-judge scoring)**: Status = `review`. Submitted to edison for review.
  - Branch: `eval-harness/john/0033-llm-judge-scoring`
  - Commit: `367dfef` — Add LLM-as-judge scoring for eval runs
  - All 69 tests passing in `tests/test_eval.py`

## What Was Built (T0033)
Added to `scripts/eval.py`:
- `judge_diff(diff, task_spec, rubric)` — one-shot LLM call via `anthropic` SDK
- `judge_run(run_dir, reps=3)` — scores all tasks in a run, averages across reps
- CLI: `python -m scripts.eval judge --run-dir /path --reps 3`
- Helper functions: `_call_llm`, `_parse_judge_response`, `_average_scores`, `_get_full_diff`, `print_judge_results`
- Added `anthropic>=0.40.0` as dependency in `pyproject.toml`

## Key Design Decisions
- Used `anthropic` SDK (not `claude-code-sdk`) — simpler for one-shot prompt, no agentic overhead
- Retry once on JSON parse failure in `judge_diff`
- `_parse_judge_response` strips markdown fences, validates score ranges 1-5, coerces floats to int
- `judge_run` uses full repo diff for all tasks (per-task diffs depend on T0031 eval runner — marked with TODO)
- `_average_scores` collects reasoning strings into a list rather than losing them

## Dependencies
- T0033 depends on T0031 (eval runner) for per-task diffs. Currently uses full repo diff as fallback.

## What's Next
- Await review feedback from edison on T0033
- T0033 is blocked on T0031 for full integration (per-task diffs), but the scoring logic is complete and testable independently
