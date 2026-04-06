"""Tests for background process management (delegate.background)."""

import os
import time
from pathlib import Path

import pytest

from delegate.background import (
    launch,
    check,
    tail,
    cancel,
    list_active,
    list_all,
    MAX_CONCURRENT,
)


@pytest.fixture
def agent_dir(tmp_path):
    """Create a temp agent directory for background process tests."""
    ad = tmp_path / "agent"
    ad.mkdir()
    return ad


class TestLaunch:
    def test_launch_returns_process_info(self, agent_dir):
        info = launch(agent_dir, "echo hello", label="test echo")
        assert info.handle
        assert info.pid > 0
        assert info.state == "running"
        assert info.label == "test echo"
        assert info.command == "echo hello"
        # Wait for it to finish
        time.sleep(0.5)
        updated = check(agent_dir, info.handle)
        assert updated.state == "completed"
        assert updated.exit_code == 0

    def test_launch_creates_log_files(self, agent_dir):
        info = launch(agent_dir, "echo hello_world")
        time.sleep(0.5)
        logs = tail(agent_dir, info.handle)
        assert "hello_world" in logs["stdout"]

    def test_launch_captures_stderr(self, agent_dir):
        info = launch(agent_dir, "echo error_msg >&2")
        time.sleep(0.5)
        logs = tail(agent_dir, info.handle)
        assert "error_msg" in logs["stderr"]

    def test_launch_respects_cwd(self, agent_dir, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        info = launch(agent_dir, "pwd", cwd=str(workdir))
        time.sleep(0.5)
        logs = tail(agent_dir, info.handle)
        assert str(workdir) in logs["stdout"]

    def test_launch_concurrency_limit(self, agent_dir):
        """Cannot launch more than MAX_CONCURRENT processes."""
        procs = []
        for i in range(MAX_CONCURRENT):
            procs.append(launch(agent_dir, f"sleep 10", label=f"proc {i}"))
        with pytest.raises(RuntimeError, match="Too many background processes"):
            launch(agent_dir, "sleep 10", label="one too many")
        # Clean up
        for p in procs:
            cancel(agent_dir, p.handle)


class TestCheck:
    def test_check_running(self, agent_dir):
        info = launch(agent_dir, "sleep 10")
        status = check(agent_dir, info.handle)
        assert status.state == "running"
        assert status.exit_code is None
        cancel(agent_dir, info.handle)

    def test_check_completed(self, agent_dir):
        info = launch(agent_dir, "echo done")
        time.sleep(0.5)
        status = check(agent_dir, info.handle)
        assert status.state == "completed"
        assert status.exit_code == 0
        assert status.ended_at is not None

    def test_check_failed(self, agent_dir):
        info = launch(agent_dir, "exit 1")
        time.sleep(0.5)
        status = check(agent_dir, info.handle)
        assert status.state == "failed"

    def test_check_unknown_handle(self, agent_dir):
        result = check(agent_dir, "nonexistent_handle")
        assert result is None

    def test_check_timeout(self, agent_dir):
        """Process exceeding max_runtime is killed."""
        info = launch(agent_dir, "sleep 60", max_runtime=1)
        time.sleep(1.5)
        status = check(agent_dir, info.handle)
        assert status.state == "timed_out"
        assert status.exit_code == -9


class TestTail:
    def test_tail_returns_last_lines(self, agent_dir):
        # Write numbered lines
        info = launch(
            agent_dir,
            "for i in $(seq 1 100); do echo line_$i; done",
        )
        time.sleep(1)
        logs = tail(agent_dir, info.handle, n=5)
        lines = logs["stdout"].strip().splitlines()
        assert len(lines) == 5
        assert "line_100" in lines[-1]

    def test_tail_unknown_handle(self, agent_dir):
        logs = tail(agent_dir, "nonexistent")
        assert logs["stdout"] == ""
        assert logs["stderr"] == ""


class TestCancel:
    def test_cancel_running_process(self, agent_dir):
        info = launch(agent_dir, "sleep 60")
        time.sleep(0.3)
        result = cancel(agent_dir, info.handle)
        assert result.state == "cancelled"
        assert result.exit_code == -15

    def test_cancel_already_done(self, agent_dir):
        info = launch(agent_dir, "echo hi")
        time.sleep(0.5)
        check(agent_dir, info.handle)  # transition to completed
        result = cancel(agent_dir, info.handle)
        assert result.state == "completed"  # not changed

    def test_cancel_unknown(self, agent_dir):
        result = cancel(agent_dir, "nonexistent")
        assert result is None


class TestList:
    def test_list_active(self, agent_dir):
        p1 = launch(agent_dir, "sleep 10", label="proc1")
        p2 = launch(agent_dir, "echo done", label="proc2")
        time.sleep(0.5)
        active = list_active(agent_dir)
        handles = [p.handle for p in active]
        assert p1.handle in handles
        assert p2.handle not in handles  # already done
        cancel(agent_dir, p1.handle)

    def test_list_all(self, agent_dir):
        p1 = launch(agent_dir, "sleep 10", label="proc1")
        p2 = launch(agent_dir, "echo done", label="proc2")
        time.sleep(0.5)
        all_procs = list_all(agent_dir)
        handles = [p.handle for p in all_procs]
        assert p1.handle in handles
        assert p2.handle in handles
        cancel(agent_dir, p1.handle)

    def test_list_empty(self, agent_dir):
        assert list_active(agent_dir) == []
        assert list_all(agent_dir) == []
