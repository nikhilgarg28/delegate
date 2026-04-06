"""Tests for multi-process merge pool infrastructure.

Verifies that:
  1. MergeResult and MergeFailureReason survive pickle round-trips
     (required for ProcessPoolExecutor serialisation).
  2. _run_in_merge_pool falls back to the thread pool when the process
     pool is not initialised (tests / non-daemon contexts).
"""

import asyncio
import pickle

import pytest

from delegate.merge import MergeFailureReason, MergeResult


# ---------------------------------------------------------------------------
# Pickle round-trip tests
# ---------------------------------------------------------------------------

class TestMergeResultPickle:
    """MergeResult must survive pickle for ProcessPoolExecutor transport."""

    def test_success_result(self):
        mr = MergeResult(42, True, "Merged successfully")
        restored = pickle.loads(pickle.dumps(mr))
        assert restored.task_id == 42
        assert restored.success is True
        assert restored.message == "Merged successfully"
        assert restored.reason is None
        assert restored.conflict_context == ""

    def test_failure_with_reason(self):
        mr = MergeResult(
            7, False, "Rebase conflict in main.py",
            reason=MergeFailureReason.REBASE_CONFLICT,
            conflict_context="<<<< HEAD\nfoo\n====\nbar\n>>>>",
        )
        restored = pickle.loads(pickle.dumps(mr))
        assert restored.task_id == 7
        assert restored.success is False
        assert restored.reason is MergeFailureReason.REBASE_CONFLICT
        assert restored.retryable is False
        assert "foo" in restored.conflict_context

    def test_retryable_failure(self):
        mr = MergeResult(
            3, False, "main dirty",
            reason=MergeFailureReason.DIRTY_MAIN,
        )
        restored = pickle.loads(pickle.dumps(mr))
        assert restored.retryable is True
        assert restored.reason is MergeFailureReason.DIRTY_MAIN

    def test_list_of_results(self):
        """merge_once returns list[MergeResult] — the whole list must pickle."""
        results = [
            MergeResult(1, True, "OK"),
            MergeResult(2, False, "fail", reason=MergeFailureReason.PRE_MERGE_FAILED),
        ]
        restored = pickle.loads(pickle.dumps(results))
        assert len(restored) == 2
        assert restored[0].success is True
        assert restored[1].reason is MergeFailureReason.PRE_MERGE_FAILED


class TestMergeFailureReasonPickle:
    """Every MergeFailureReason member must survive pickle."""

    @pytest.mark.parametrize("member", list(MergeFailureReason))
    def test_each_member(self, member):
        restored = pickle.loads(pickle.dumps(member))
        assert restored is member
        assert restored.short_message == member.short_message
        assert restored.retryable == member.retryable


# ---------------------------------------------------------------------------
# _run_in_merge_pool fallback test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_pool_fallback_to_thread_pool():
    """When _merge_pool is None, _run_in_merge_pool falls back to _run_in_db_pool."""
    from delegate.web import _run_in_merge_pool, _merge_pool

    # _merge_pool should be None outside lifespan
    assert _merge_pool is None

    # Should still work — falls back to thread pool
    result = await _run_in_merge_pool(sorted, [3, 1, 2])
    assert result == [1, 2, 3]
