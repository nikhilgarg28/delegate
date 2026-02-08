"""Daemon entry point — starts the web UI + daemon loop via uvicorn.

Usage:
    python -m scripts.run <root> [--port 8000] [--kick "message"]
    python -m scripts.run <root> --no-reload   # disable auto-restart

Auto-reload is enabled by default: uvicorn watches `scripts/` for file
changes and restarts the worker process, bringing the daemon loop back
up with the new code.  Pass ``--no-reload`` to disable this (e.g. in
production or CI).
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from scripts.mailbox import send as mailbox_send
from scripts.bootstrap import get_member_by_role


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("daemon")


def main():
    parser = argparse.ArgumentParser(description="Standup daemon")
    parser.add_argument("root", type=Path, help="Team root directory")
    parser.add_argument("--interval", type=float, default=1.0, help="Poll interval in seconds")
    parser.add_argument("--port", type=int, default=8000, help="Web UI port")
    parser.add_argument("--max-concurrent", type=int, default=3, help="Max concurrent agents")
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

    root = args.root.resolve()

    # --- Send kick message (once, before uvicorn spawns workers) ---
    if args.kick:
        try:
            director_name = get_member_by_role(root, "director") or "director"
            manager_name = get_member_by_role(root, "manager") or "manager"
            mailbox_send(root, director_name, manager_name, args.kick)
            logger.info("Sent kick message from %s to %s: %s", director_name, manager_name, args.kick[:80])
        except Exception:
            logger.exception("Failed to send kick message")

    # --- Configure the app via environment variables ---
    os.environ["STANDUP_ROOT"] = str(root)
    os.environ["STANDUP_DAEMON"] = "1"
    os.environ["STANDUP_INTERVAL"] = str(args.interval)
    os.environ["STANDUP_MAX_CONCURRENT"] = str(args.max_concurrent)
    if args.token_budget is not None:
        os.environ["STANDUP_TOKEN_BUDGET"] = str(args.token_budget)

    # --- Start uvicorn (reload on by default) ---
    reload = not args.no_reload
    uvicorn.run(
        "scripts.web:create_app",
        factory=True,
        host="0.0.0.0",
        port=args.port,
        reload=reload,
        reload_dirs=["scripts"] if reload else None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
