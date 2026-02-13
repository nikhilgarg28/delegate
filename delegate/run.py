"""Daemon entry point — starts the web UI + daemon loop via uvicorn.

Usage:
    python -m delegate.run <home> <team> [--port 3548] [--kick "message"]
    python -m delegate.run <home> <team> --no-reload   # disable auto-restart

Auto-reload is enabled by default: uvicorn watches `delegate/` for file
changes and restarts the worker process, bringing the daemon loop back
up with the new code.  Pass ``--no-reload`` to disable this (e.g. in
production or CI).
"""

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from delegate.mailbox import send as mailbox_send
from delegate.bootstrap import get_member_by_role
from delegate.config import get_boss
from delegate.logging_setup import configure_logging

logger = logging.getLogger("daemon")


def main():
    parser = argparse.ArgumentParser(description="Delegate daemon")
    parser.add_argument("home", type=Path, help="Delegate home directory (~/.delegate)")
    parser.add_argument("team", help="Default team name (for kick message)")
    parser.add_argument("--interval", type=float, default=1.0, help="Poll interval in seconds")
    parser.add_argument("--port", type=int, default=3548, help="Web UI port")
    parser.add_argument("--max-concurrent", type=int, default=32, help="Max concurrent agents")
    parser.add_argument(
        "--token-budget", type=int, default=None,
        help="Default token budget per agent session",
    )
    parser.add_argument(
        "--kick", type=str, default=None,
        help="Send this message to the manager on startup to bootstrap the conversation",
    )
    parser.add_argument(
        "--no-reload", action="store_true",
        help="Disable auto-reload on code changes",
    )
    args = parser.parse_args()

    hc_home = args.home.resolve()

    # --- Unified logging (file + console) ---
    configure_logging(hc_home, console=True)

    # --- Send kick message (once, before uvicorn spawns workers) ---
    if args.kick:
        try:
            boss_name = get_boss(hc_home) or "boss"
            manager_name = get_member_by_role(hc_home, args.team, "manager") or "manager"
            mailbox_send(hc_home, args.team, boss_name, manager_name, args.kick)
            logger.info("Sent kick message from %s to %s: %s", boss_name, manager_name, args.kick[:80])
        except Exception:
            logger.exception("Failed to send kick message")

    # --- Configure the app via environment variables ---
    os.environ["DELEGATE_HOME"] = str(hc_home)
    os.environ["DELEGATE_DAEMON"] = "1"
    os.environ["DELEGATE_INTERVAL"] = str(args.interval)
    os.environ["DELEGATE_MAX_CONCURRENT"] = str(args.max_concurrent)
    if args.token_budget is not None:
        os.environ["DELEGATE_TOKEN_BUDGET"] = str(args.token_budget)

    # --- Start uvicorn (reload on by default) ---
    reload = not args.no_reload
    uvicorn.run(
        "delegate.web:create_app",
        factory=True,
        host="0.0.0.0",
        port=args.port,
        reload=reload,
        reload_dirs=["delegate"] if reload else None,
        log_level="info",
        timeout_graceful_shutdown=15,
    )


if __name__ == "__main__":
    main()
