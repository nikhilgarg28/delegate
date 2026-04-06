"""Daemon management — start/stop the background web UI + routing loop.

The daemon runs uvicorn serving the FastAPI app (delegate.web) with
the message router and agent orchestrator running as background tasks.

Singleton enforcement uses two complementary mechanisms:

1. **PID file** (``protected/daemon.pid``) — human-readable, used for
   ``stop_daemon`` and ``is_running``.
2. **``fcntl.flock()``** (``protected/daemon.lock``) — advisory exclusive
   lock held for the lifetime of the process.  The OS releases the lock
   automatically when the process exits (even on SIGKILL), so stale PID
   files cannot cause a new daemon to refuse to start.

Functions:
    start_daemon(hc_home, port, ...) — start in background, write PID
    stop_daemon(hc_home) — read PID file, send SIGTERM
    is_running(hc_home) — check if the daemon PID is alive
"""

import fcntl
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from delegate.paths import daemon_pid_path, daemon_lock_path, ensure_protected
from delegate.logging_setup import configure_logging, log_file_path

logger = logging.getLogger(__name__)

# _acquire_lock / _release_lock are called by the lifespan in web.py
# to hold the singleton lock for the daemon's lifetime.


def _acquire_lock(hc_home: Path) -> int:
    """Acquire an exclusive lock on the daemon lock file.

    Returns the file descriptor (must be kept open for the lock to hold).
    Raises ``RuntimeError`` if another daemon already holds the lock.
    """
    lock_path = daemon_lock_path(hc_home)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        os.close(fd)
        raise RuntimeError(
            "Another delegate daemon is already running "
            "(could not acquire exclusive lock)."
        )
    # Write our PID into the lock file for debugging
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _release_lock(fd: int) -> None:
    """Release the daemon lock."""
    if fd < 0:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    try:
        os.close(fd)
    except (OSError, ValueError):
        pass


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
    dev: bool = False,
    host: str = "127.0.0.1",
) -> int | None:
    """Start the daemon.

    If *foreground* is True, runs uvicorn in the current process (blocking).
    Otherwise, spawns a background subprocess and writes its PID.

    When *dev* is True, the esbuild frontend watcher is started alongside
    the server for live rebuilds during development.

    Returns the PID of the spawned process (or None if foreground).
    """
    hc_home.mkdir(parents=True, exist_ok=True)

    # Run the teams→projects filesystem migration BEFORE ensure_protected()
    # creates the new directory structure.  This way rename() just works —
    # no merge-contents logic needed.  Safe to remove once all users have
    # upgraded past 0.2.x.
    from delegate.migrations.migrate_teams_to_projects import migrate_teams_to_projects
    migrate_teams_to_projects(hc_home)

    ensure_protected(hc_home)

    # Attempt to acquire the exclusive flock first — this is the
    # authoritative singleton check (survives stale PID files).
    # For foreground mode we hold it ourselves; for background mode
    # the child process will acquire its own lock.
    alive, existing_pid = is_running(hc_home)
    if alive:
        raise RuntimeError(f"Daemon already running with PID {existing_pid}")

    # Check port availability early so the user gets a clear error
    # instead of a silent background failure.  SO_REUSEADDR matches
    # uvicorn's behaviour — without it we'd reject ports in TIME_WAIT
    # (e.g. immediately after a SIGKILL stop).
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            raise RuntimeError(
                f"Port {port} is already in use. "
                f"Stop the other process or use --port to pick a different port."
            )

    # Warn if binding to a non-loopback address without authentication
    if host != "127.0.0.1" and host != "localhost":
        from delegate.auth import is_passphrase_enabled
        if not is_passphrase_enabled(hc_home):
            logger.warning(
                "SECURITY WARNING: Binding to %s without a passphrase. "
                "All endpoints (including shell exec) are unauthenticated. "
                "Run 'delegate auth set-passphrase' to secure the API, "
                "or use --host 127.0.0.1 to restrict to localhost.",
                host,
            )

    # Set environment variables for the web app
    env = os.environ.copy()
    env["DELEGATE_HOME"] = str(hc_home)
    env["DELEGATE_DAEMON"] = "1"
    env["DELEGATE_INTERVAL"] = str(interval)
    env["DELEGATE_MAX_CONCURRENT"] = str(max_concurrent)
    env["DELEGATE_PORT"] = str(port)
    if token_budget is not None:
        env["DELEGATE_TOKEN_BUDGET"] = str(token_budget)
    if dev:
        env["DELEGATE_DEV"] = "1"

    if foreground:
        # Run in current process (blocking).
        # Lock acquisition and PID cleanup are handled by the lifespan
        # in web.py — don't duplicate here or the lock will be acquired
        # twice in the same process.
        os.environ.update(env)
        configure_logging(hc_home, console=True)
        import uvicorn

        pid_path = daemon_pid_path(hc_home)
        pid_path.write_text(str(os.getpid()))

        try:
            uvicorn.run(
                "delegate.web:create_app",
                factory=True,
                host=host,
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
        "--host", host,
        "--port", str(port),
        "--log-level", "info",
    ]

    stderr_fh = open(log_fp, "a")  # noqa: SIM115 — kept open for subprocess lifetime
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        start_new_session=True,
    )

    # Write PID
    pid_path = daemon_pid_path(hc_home)
    pid_path.write_text(str(proc.pid))
    logger.info("Daemon started with PID %d on port %d", proc.pid, port)

    return proc.pid


def find_claude_pids(
    *,
    ppid_filter: int | None = None,
    exclude_pids: set[int] | None = None,
) -> list[int]:
    """Scan ``/proc`` for Claude processes matching the given criteria.

    Args:
        ppid_filter: If set, only return processes whose parent PID matches.
        exclude_pids: PIDs to skip (e.g. tracked Telephone subprocesses).

    Returns a list of matching PIDs.
    """
    exclude = exclude_pids or set()
    my_pid = os.getpid()
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            child_pid = int(entry)
            if child_pid == my_pid or child_pid in exclude:
                continue
            with open(f"/proc/{entry}/stat") as f:
                parts = f.read().split()
            ppid = int(parts[3])
            comm = parts[1].strip("()")
            if "claude" not in comm:
                continue
            if ppid_filter is not None and ppid != ppid_filter:
                continue
            found.append(child_pid)
        except (FileNotFoundError, ProcessLookupError, ValueError,
                IndexError, PermissionError):
            continue
    return found


def sweep_orphaned_claude_processes() -> int:
    """Kill Claude processes reparented to PID 1 (orphans from a dead daemon).

    Returns the number of processes killed.
    """
    pids = find_claude_pids(ppid_filter=1)
    for pid in pids:
        try:
            logger.info("Killing reparented Claude orphan PID %d", pid)
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if pids:
        logger.info("Swept %d orphaned Claude process(es)", len(pids))
    return len(pids)


def stop_daemon(hc_home: Path, timeout: float = 15.0) -> bool:
    """Stop the running daemon.

    Sends SIGTERM and waits up to *timeout* seconds for the process to exit.
    If still alive after timeout, sends SIGKILL.

    After the daemon exits, sweeps for orphaned Claude subprocesses that
    got reparented to PID 1 during shutdown.

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
            os.kill(pid, 0)
            time.sleep(poll_interval)
        except (OSError, ProcessLookupError):
            elapsed = time.time() - start_time
            logger.info("Daemon stopped (%.1fs)", elapsed)
            break
    else:
        # Timeout expired — force kill
        logger.warning("Daemon did not stop after %.1fs — sending SIGKILL", timeout)
        try:
            os.kill(pid, signal.SIGKILL)
            logger.info("Sent SIGKILL to daemon PID %d", pid)
        except (OSError, ProcessLookupError) as e:
            logger.warning("Failed to SIGKILL daemon PID %d: %s", pid, e)

        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except (OSError, ProcessLookupError):
                logger.info("Daemon force-killed")
                break
        else:
            logger.error("Daemon PID %d did not respond to SIGKILL", pid)

    # Cleanup — always runs regardless of how the daemon exited
    pid_path = daemon_pid_path(hc_home)
    pid_path.unlink(missing_ok=True)
    sweep_orphaned_claude_processes()
    return True
