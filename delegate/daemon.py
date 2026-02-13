"""Daemon management — start/stop the background web UI + routing loop.

The daemon runs uvicorn serving the FastAPI app (delegate.web) with
the message router and agent orchestrator running as background tasks.

The daemon PID is written to ``~/.delegate/daemon.pid``.

Functions:
    start_daemon(hc_home, port, ...) — start in background, write PID
    stop_daemon(hc_home) — read PID file, send SIGTERM
    is_running(hc_home) — check if the daemon PID is alive
"""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from delegate.paths import daemon_pid_path
from delegate.logging_setup import configure_logging, log_file_path

logger = logging.getLogger(__name__)


def is_running(hc_home: Path) -> tuple[bool, int | None]:
    """Check if the daemon is running.

    Returns (alive, pid). If pid file is missing or stale, returns (False, None).
    """
    pid_path = daemon_pid_path(hc_home)
    if not pid_path.exists():
        return False, None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False, None

    try:
        os.kill(pid, 0)
        return True, pid
    except (OSError, ProcessLookupError):
        # Stale PID file — clean up
        pid_path.unlink(missing_ok=True)
        return False, None


def start_daemon(
    hc_home: Path,
    port: int = 3548,
    interval: float = 1.0,
    max_concurrent: int = 32,
    token_budget: int | None = None,
    foreground: bool = False,
) -> int | None:
    """Start the daemon.

    If *foreground* is True, runs uvicorn in the current process (blocking).
    Otherwise, spawns a background subprocess and writes its PID.

    Returns the PID of the spawned process (or None if foreground).
    """
    alive, existing_pid = is_running(hc_home)
    if alive:
        raise RuntimeError(f"Daemon already running with PID {existing_pid}")

    hc_home.mkdir(parents=True, exist_ok=True)

    # Set environment variables for the web app
    env = os.environ.copy()
    env["DELEGATE_HOME"] = str(hc_home)
    env["DELEGATE_DAEMON"] = "1"
    env["DELEGATE_INTERVAL"] = str(interval)
    env["DELEGATE_MAX_CONCURRENT"] = str(max_concurrent)
    env["DELEGATE_PORT"] = str(port)
    if token_budget is not None:
        env["DELEGATE_TOKEN_BUDGET"] = str(token_budget)

    if foreground:
        # Run in current process (blocking)
        os.environ.update(env)
        configure_logging(hc_home, console=True)
        import uvicorn

        pid_path = daemon_pid_path(hc_home)
        pid_path.write_text(str(os.getpid()))

        try:
            uvicorn.run(
                "delegate.web:create_app",
                factory=True,
                host="0.0.0.0",
                port=port,
                log_level="info",
                timeout_graceful_shutdown=15,
            )
        finally:
            pid_path.unlink(missing_ok=True)
        return None

    # Spawn background process — redirect stderr to the log file
    hc_home.mkdir(parents=True, exist_ok=True)
    log_fp = log_file_path(hc_home)

    cmd = [
        sys.executable, "-m", "uvicorn",
        "delegate.web:create_app",
        "--factory",
        "--host", "0.0.0.0",
        "--port", str(port),
        "--log-level", "info",
    ]

    stderr_fh = open(log_fp, "a")  # noqa: SIM115 — kept open for subprocess lifetime
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        start_new_session=True,
    )

    # Write PID
    pid_path = daemon_pid_path(hc_home)
    pid_path.write_text(str(proc.pid))
    logger.info("Daemon started with PID %d on port %d", proc.pid, port)

    return proc.pid


def stop_daemon(hc_home: Path, timeout: float = 15.0) -> bool:
    """Stop the running daemon.

    Sends SIGTERM and waits up to *timeout* seconds for the process to exit.
    If still alive after timeout, sends SIGKILL.

    Returns True if a daemon was stopped, False if none was running.
    """
    alive, pid = is_running(hc_home)
    if not alive or pid is None:
        logger.info("No running daemon found")
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("Sent SIGTERM to daemon PID %d", pid)
    except OSError as e:
        logger.warning("Failed to kill daemon PID %d: %s", pid, e)
        pid_path = daemon_pid_path(hc_home)
        pid_path.unlink(missing_ok=True)
        return False

    # Wait for process to exit with timeout
    logger.info("Waiting for daemon to stop...")
    start_time = time.time()
    poll_interval = 0.1
    while time.time() - start_time < timeout:
        try:
            os.kill(pid, 0)  # Check if process is still alive
            time.sleep(poll_interval)
        except (OSError, ProcessLookupError):
            # Process is gone
            elapsed = time.time() - start_time
            logger.info("Daemon stopped (%.1fs)", elapsed)
            pid_path = daemon_pid_path(hc_home)
            pid_path.unlink(missing_ok=True)
            return True

    # Timeout expired — force kill
    logger.warning("Daemon did not stop after %.1fs — sending SIGKILL", timeout)
    try:
        os.kill(pid, signal.SIGKILL)
        logger.info("Sent SIGKILL to daemon PID %d", pid)
    except (OSError, ProcessLookupError) as e:
        logger.warning("Failed to SIGKILL daemon PID %d: %s", pid, e)

    # Wait briefly for SIGKILL to take effect
    for _ in range(10):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except (OSError, ProcessLookupError):
            logger.info("Daemon force-killed")
            pid_path = daemon_pid_path(hc_home)
            pid_path.unlink(missing_ok=True)
            return True

    # Still alive after SIGKILL (very unlikely)
    logger.error("Daemon PID %d did not respond to SIGKILL", pid)
    pid_path = daemon_pid_path(hc_home)
    pid_path.unlink(missing_ok=True)
    return True
