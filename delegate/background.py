"""Background process management for long-running commands.

Researchers (and potentially other roles) need to run commands that take
minutes to hours (GPU training, large data processing, backtests).  Claude
Code's bash tool has a ~2-minute timeout, so these commands must be launched
as detached background processes.

This module provides a simple process manager scoped to a (team, agent) pair:

    handle = launch(agent_dir, cmd, cwd=worktree, label="train epoch 50")
    status = check(agent_dir, handle)   # -> {state, exit_code, tail}
    output = tail(agent_dir, handle, n=50)
    cancel(agent_dir, handle)

Processes are tracked via a JSON manifest at:
    ``<agent_dir>/.bg/<handle>/meta.json``

Stdout/stderr are captured to log files in the same directory.

The user command is wrapped in a shell script that writes the exit code to
``<handle>/exitcode`` on completion.  This avoids relying on PID-based
detection (which is unreliable for detached processes / zombies).

The daemon calls these functions from MCP tool handlers (outside the OS
sandbox), so agents never touch the manifest or PID files directly.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum number of concurrent background processes per agent.
MAX_CONCURRENT = 5

# Default tail lines when checking status.  Keep low to avoid flooding
# the agent's context with verbose training output.  Agents can override
# with tail_lines=N when they need more detail.
DEFAULT_TAIL_LINES = 15

# Maximum runtime in seconds (4 hours).  Processes exceeding this are
# killed automatically on the next ``check()`` call.
DEFAULT_MAX_RUNTIME = 4 * 3600


@dataclass
class ProcessInfo:
    """Metadata for a background process."""
    handle: str
    pid: int
    command: str
    cwd: str
    label: str
    started_at: float  # time.time()
    max_runtime: float
    state: str = "running"  # running | completed | failed | cancelled | timed_out
    exit_code: int | None = None
    ended_at: float | None = None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _bg_dir(agent_dir: Path) -> Path:
    """Background process root: ``<agent_dir>/.bg/``."""
    return agent_dir / ".bg"


def _proc_dir(agent_dir: Path, handle: str) -> Path:
    return _bg_dir(agent_dir) / handle


def _meta_path(agent_dir: Path, handle: str) -> Path:
    return _proc_dir(agent_dir, handle) / "meta.json"


def _stdout_path(agent_dir: Path, handle: str) -> Path:
    return _proc_dir(agent_dir, handle) / "stdout.log"


def _stderr_path(agent_dir: Path, handle: str) -> Path:
    return _proc_dir(agent_dir, handle) / "stderr.log"


def _exitcode_path(agent_dir: Path, handle: str) -> Path:
    """Sentinel file written by the wrapper script on process exit."""
    return _proc_dir(agent_dir, handle) / "exitcode"


def _save_meta(agent_dir: Path, info: ProcessInfo) -> None:
    p = _meta_path(agent_dir, info.handle)
    p.write_text(json.dumps(asdict(info), indent=2))


def _load_meta(agent_dir: Path, handle: str) -> ProcessInfo | None:
    p = _meta_path(agent_dir, handle)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return ProcessInfo(**data)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def launch(
    agent_dir: Path,
    command: str,
    *,
    cwd: str | None = None,
    label: str = "",
    max_runtime: float = DEFAULT_MAX_RUNTIME,
    env: dict[str, str] | None = None,
) -> ProcessInfo:
    """Launch a command as a detached background process.

    The command is wrapped in a shell script that captures the exit code
    to a file on completion, making status detection reliable regardless
    of process group or zombie state.

    Returns a ``ProcessInfo`` with the handle for later queries.

    Raises ``RuntimeError`` if the agent has too many concurrent processes.
    """
    # Enforce concurrency limit
    active = list_active(agent_dir)
    if len(active) >= MAX_CONCURRENT:
        raise RuntimeError(
            f"Too many background processes ({len(active)}/{MAX_CONCURRENT}). "
            f"Wait for one to finish or cancel one first."
        )

    handle = uuid.uuid4().hex[:12]
    pdir = _proc_dir(agent_dir, handle)
    pdir.mkdir(parents=True, exist_ok=True)

    exitcode_file = str(_exitcode_path(agent_dir, handle))
    stdout_file = str(_stdout_path(agent_dir, handle))
    stderr_file = str(_stderr_path(agent_dir, handle))

    # Wrap the command in a subshell so that even ``exit N`` inside the
    # user command doesn't skip the exit-code capture.  ``{ cmd; }``
    # would cause ``exit`` to terminate the whole wrapper; ``( cmd )``
    # runs it in a child process whose exit code we can capture.
    wrapper = (
        f'( {command} ) > {shlex.quote(stdout_file)} 2> {shlex.quote(stderr_file)}\n'
        f'_ec=$?\n'
        f'echo $_ec > {shlex.quote(exitcode_file)}\n'
        f'exit $_ec\n'
    )

    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    proc = subprocess.Popen(
        ["sh", "-c", wrapper],
        cwd=cwd or str(agent_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=merged_env,
        start_new_session=True,  # detach from parent process group
    )

    info = ProcessInfo(
        handle=handle,
        pid=proc.pid,
        command=command,
        cwd=cwd or str(agent_dir),
        label=label or command[:80],
        started_at=time.time(),
        max_runtime=max_runtime,
    )
    _save_meta(agent_dir, info)

    logger.info(
        "Background process launched: handle=%s pid=%d cmd=%r",
        handle, proc.pid, command[:120],
    )
    return info


def check(agent_dir: Path, handle: str) -> ProcessInfo | None:
    """Check the status of a background process.

    Detection relies on the ``exitcode`` sentinel file written by the
    wrapper script.  If the file exists, the process has finished.
    If the process has exceeded ``max_runtime``, it is killed.

    Returns None if the handle is unknown.
    """
    info = _load_meta(agent_dir, handle)
    if info is None:
        return None

    if info.state != "running":
        return info  # already terminal

    # Primary check: did the wrapper write an exit code file?
    ec_path = _exitcode_path(agent_dir, handle)
    if ec_path.exists():
        try:
            info.exit_code = int(ec_path.read_text().strip())
        except (ValueError, OSError):
            info.exit_code = _infer_exit_code(agent_dir, handle)
        info.state = "completed" if info.exit_code == 0 else "failed"
        info.ended_at = time.time()
        _save_meta(agent_dir, info)
        return info

    # Secondary: check if the PID is gone (crash before writing exitcode)
    if not _pid_alive(info.pid):
        info.exit_code = _infer_exit_code(agent_dir, handle)
        info.state = "completed" if info.exit_code == 0 else "failed"
        info.ended_at = time.time()
        _save_meta(agent_dir, info)
        return info

    # Check timeout
    elapsed = time.time() - info.started_at
    if elapsed > info.max_runtime:
        logger.warning(
            "Background process %s (pid=%d) exceeded max runtime (%.0fs), killing",
            handle, info.pid, info.max_runtime,
        )
        _kill_process(info.pid)
        info.state = "timed_out"
        info.exit_code = -9
        info.ended_at = time.time()
        _save_meta(agent_dir, info)
        return info

    return info


def tail(agent_dir: Path, handle: str, n: int = DEFAULT_TAIL_LINES) -> dict[str, str]:
    """Return the last *n* lines of stdout and stderr for a process."""
    result: dict[str, str] = {}
    for name, path_fn in [("stdout", _stdout_path), ("stderr", _stderr_path)]:
        p = path_fn(agent_dir, handle)
        if p.exists():
            try:
                lines = p.read_text(errors="replace").splitlines()
                result[name] = "\n".join(lines[-n:])
            except Exception:
                result[name] = "(error reading log)"
        else:
            result[name] = ""
    return result


def cancel(agent_dir: Path, handle: str) -> ProcessInfo | None:
    """Cancel (kill) a running background process.

    Returns updated ProcessInfo, or None if handle unknown.
    """
    info = check(agent_dir, handle)  # refresh state first
    if info is None:
        return None

    if info.state != "running":
        return info  # already terminal

    _kill_process(info.pid)
    info.state = "cancelled"
    info.exit_code = -15
    info.ended_at = time.time()
    _save_meta(agent_dir, info)

    logger.info("Background process cancelled: handle=%s pid=%d", handle, info.pid)
    return info


def list_active(agent_dir: Path) -> list[ProcessInfo]:
    """Return all active (running) background processes for the agent."""
    bg = _bg_dir(agent_dir)
    if not bg.is_dir():
        return []
    active = []
    for d in sorted(bg.iterdir()):
        if not d.is_dir():
            continue
        info = _load_meta(agent_dir, d.name)
        if info is not None and info.state == "running":
            # Refresh state (might have exited)
            info = check(agent_dir, d.name)
            if info is not None and info.state == "running":
                active.append(info)
    return active


def list_all(agent_dir: Path) -> list[ProcessInfo]:
    """Return all background processes (active and completed) for the agent."""
    bg = _bg_dir(agent_dir)
    if not bg.is_dir():
        return []
    result = []
    for d in sorted(bg.iterdir()):
        if not d.is_dir():
            continue
        info = _load_meta(agent_dir, d.name)
        if info is not None:
            # Refresh running ones
            if info.state == "running":
                info = check(agent_dir, d.name)
            if info is not None:
                result.append(info)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive using /proc (Linux) with kill(0) fallback."""
    # /proc check is more reliable — avoids false positives from zombies
    proc_path = Path(f"/proc/{pid}")
    if proc_path.exists():
        try:
            status = (proc_path / "status").read_text()
            # Zombies show "State: Z" — treat as not alive
            for line in status.splitlines():
                if line.startswith("State:"):
                    return "Z" not in line
        except (OSError, PermissionError):
            pass
    # Fallback: kill -0
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_process(pid: int) -> None:
    """Kill a process and its process group (best-effort)."""
    try:
        # Kill the whole process group (since we used start_new_session)
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    # Give it a moment, then SIGKILL if still alive
    time.sleep(0.5)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _infer_exit_code(agent_dir: Path, handle: str) -> int:
    """Infer exit code when exitcode file is missing (process crashed hard).

    Heuristic: if stderr log contains common error patterns, assume failure.
    Otherwise assume failure (exit 1) — a process that can't write its own
    exit code almost certainly didn't exit cleanly.
    """
    stderr_p = _stderr_path(agent_dir, handle)
    if stderr_p.exists():
        try:
            content = stderr_p.read_text(errors="replace")
            # Check for common failure indicators
            lower = content.lower()
            if any(pattern in lower for pattern in [
                "traceback", "error:", "fatal:", "killed", "segfault",
                "out of memory", "runtime error",
            ]):
                return 1
        except Exception:
            pass
    return 1
