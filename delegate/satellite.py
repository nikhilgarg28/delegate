"""Satellite daemon — poll coordinator for work and execute agent turns locally.

The satellite is a lightweight async process that:

1. Polls the coordinator's ``/internal/satellite/poll`` endpoint for agents
   that have unread messages and are assigned to this satellite.
2. For each agent needing a turn, fetches config from the coordinator,
   ensures local repo clones and worktrees exist, then runs the turn
   using a local ``Telephone`` subprocess with HTTP-backed MCP tools.
3. After each turn, pushes commits to the shared remote (GitHub/GitLab)
   and reports session metrics back to the coordinator.

The satellite has **no local database** — the coordinator's SQLite DB is
the single source of truth.  All mailbox, task, and session operations
are proxied via HTTP.

Filesystem layout on satellite::

    ~/.delegate-satellite/
      config.yaml            # coordinator_url, satellite_id, auth_token
      repos/<team>/<repo>/   # local clones from GitHub
      worktrees/<team>/<repo>/T0001/  # task worktrees
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

# Default home for satellite state
_DEFAULT_SAT_HOME = Path.home() / ".delegate-satellite"


@dataclass
class SatelliteDaemon:
    """Satellite worker daemon.

    Polls the coordinator for work, executes turns locally with
    HTTP-backed MCP tools, and pushes results via git.
    """

    coordinator_url: str
    satellite_id: str
    auth_token: str
    poll_interval: float = 2.0
    max_concurrent: int = 8
    sat_home: Path = field(default_factory=lambda: _DEFAULT_SAT_HOME)

    def __post_init__(self) -> None:
        self.sat_home.mkdir(parents=True, exist_ok=True)
        (self.sat_home / "repos").mkdir(exist_ok=True)
        (self.sat_home / "worktrees").mkdir(exist_ok=True)

        self._client = httpx.Client(
            base_url=self.coordinator_url,
            headers={"Authorization": f"Bearer {self.auth_token}"},
            timeout=60.0,
        )
        self._in_flight: set[tuple[str, str]] = set()
        self._in_flight_lock = asyncio.Lock()
        self._exchange: Any = None  # TelephoneExchange, lazily created

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._client.get(path, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict | None = None) -> dict:
        resp = self._client.post(path, json=data or {})
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Repo / worktree management
    # ------------------------------------------------------------------

    def _repo_clone_dir(self, team: str, repo_name: str) -> Path:
        return self.sat_home / "repos" / team / repo_name

    def _worktree_dir(self, team: str, repo_name: str, task_id: int) -> Path:
        return self.sat_home / "worktrees" / team / repo_name / f"T{task_id:04d}"

    def _ensure_repo_clone(self, team: str, repo_name: str, remote_url: str) -> Path:
        """Clone the repo from remote_url if it doesn't exist locally."""
        clone_dir = self._repo_clone_dir(team, repo_name)
        if clone_dir.exists() and (clone_dir / ".git").exists():
            # Fetch latest
            subprocess.run(
                ["git", "fetch", "--all"],
                cwd=str(clone_dir),
                capture_output=True,
                check=False,
                timeout=120,
            )
            return clone_dir

        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Cloning %s → %s", remote_url, clone_dir)
        subprocess.run(
            ["git", "clone", remote_url, str(clone_dir)],
            capture_output=True,
            check=True,
            timeout=300,
        )
        return clone_dir

    def _ensure_worktree(
        self, team: str, repo_name: str, task_id: int, branch: str, remote_url: str,
    ) -> Path:
        """Ensure a git worktree exists for the given task."""
        wt_dir = self._worktree_dir(team, repo_name, task_id)
        if wt_dir.exists():
            # Pull latest on the worktree
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=str(wt_dir),
                capture_output=True,
                check=False,
                timeout=120,
            )
            return wt_dir

        # Ensure base repo clone exists
        clone_dir = self._ensure_repo_clone(team, repo_name, remote_url)

        # Try to check out existing remote branch, or create new
        wt_dir.parent.mkdir(parents=True, exist_ok=True)

        # Check if the branch exists on remote
        check = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{branch}"],
            cwd=str(clone_dir),
            capture_output=True,
            text=True,
        )

        if check.returncode == 0:
            # Branch exists on remote — track it
            subprocess.run(
                ["git", "worktree", "add", str(wt_dir), "--track", "-b", branch, f"origin/{branch}"],
                cwd=str(clone_dir),
                capture_output=True,
                check=True,
                timeout=60,
            )
        else:
            # New branch — create from default branch
            default_branch = self._get_default_branch(str(clone_dir))
            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(wt_dir), default_branch],
                cwd=str(clone_dir),
                capture_output=True,
                check=True,
                timeout=60,
            )

        return wt_dir

    def _get_default_branch(self, repo_dir: str) -> str:
        """Detect default branch (main or master)."""
        for candidate in ("main", "master"):
            result = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return candidate
        return "main"

    def _push_branch(self, worktree_dir: Path, branch: str) -> bool:
        """Push the branch to origin."""
        result = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=str(worktree_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error("Push failed for %s: %s", branch, result.stderr)
            return False
        logger.info("Pushed branch '%s' to origin", branch)
        return True

    # ------------------------------------------------------------------
    # MCP server factory (for remote tools)
    # ------------------------------------------------------------------

    def _mcp_server_factory(self, team: str, agent: str) -> Any:
        """Create an MCP server with HTTP-backed tools for this satellite."""
        from delegate.mcp_tools_remote import create_remote_mcp_server
        return create_remote_mcp_server(
            self.coordinator_url, self.auth_token, team, agent,
        )

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def _dispatch_turn(self, team: str, agent: str) -> None:
        """Execute a single turn for an agent on this satellite."""
        try:
            # Get agent config from coordinator
            config = self._get("/internal/agent/config", {"team": team, "agent": agent})
            role = config["role"]
            model = config.get("model", "sonnet")

            # Claim messages from coordinator
            claim_result = self._post("/internal/mailbox/claim", {
                "team": team,
                "agent": agent,
                "limit": 50,
            })
            messages = claim_result.get("messages", [])
            if not messages:
                return

            # Determine task_id from first message
            task_id = messages[0].get("task_id")

            # Start session on coordinator
            session_result = self._post("/internal/session/start", {
                "team": team,
                "agent": agent,
                "task_id": task_id,
            })
            session_id = session_result["session_id"]

            # Broadcast turn started
            self._post("/internal/activity/turn-event", {
                "event_type": "turn_started",
                "agent": agent,
                "team": team,
                "task_id": task_id,
                "sender": messages[0].get("sender", ""),
            })

            # If there's a task, ensure worktree exists
            if task_id:
                try:
                    task = self._get("/internal/task/show", {"team": team, "task_id": str(task_id)})
                    repos = task.get("repo", [])
                    branch = task.get("branch", "")

                    # Get repo remote URLs
                    repo_list = self._get("/internal/repo/list", {"team": team})
                    repo_map = {r["name"]: r for r in repo_list.get("repos", [])}

                    for repo_name in repos:
                        repo_info = repo_map.get(repo_name, {})
                        remote_url = repo_info.get("remote_url", "")
                        if remote_url and branch:
                            self._ensure_worktree(team, repo_name, task_id, branch, remote_url)
                except Exception:
                    logger.exception("Failed to set up worktree for %s/%s", team, agent)

            # Build prompt from messages
            prompt_parts = []
            for m in messages:
                sender = m.get("sender", "unknown")
                body = m.get("body", "")
                prompt_parts.append(f"[From {sender}]: {body}")
            prompt = "\n\n".join(prompt_parts)

            # Get or create Telephone
            if self._exchange is None:
                from delegate.runtime import TelephoneExchange
                self._exchange = TelephoneExchange()

            from delegate.runtime import run_turn
            # For satellite, we use run_turn with remote MCP tools
            result = await run_turn(
                self.sat_home,  # use satellite home for local paths
                team,
                agent,
                exchange=self._exchange,
                mcp_server_factory=self._mcp_server_factory,
            )

            # Mark messages as processed on coordinator
            msg_ids = [m["id"] for m in messages if m.get("id")]
            if msg_ids:
                self._post("/internal/mailbox/mark-processed", {
                    "team": team,
                    "message_ids": msg_ids,
                })

            # Push commits if task has worktrees
            if task_id:
                try:
                    task = self._get("/internal/task/show", {"team": team, "task_id": str(task_id)})
                    repos = task.get("repo", [])
                    branch = task.get("branch", "")
                    for repo_name in repos:
                        wt = self._worktree_dir(team, repo_name, task_id)
                        if wt.exists() and branch:
                            self._push_branch(wt, branch)
                except Exception:
                    logger.exception("Failed to push after turn for %s/%s", team, agent)

            # End session on coordinator
            self._post("/internal/session/end", {
                "team": team,
                "session_id": session_id,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "cost_usd": result.cost_usd,
                "cache_read_tokens": result.cache_read,
                "cache_write_tokens": result.cache_write,
            })

            # Broadcast turn ended
            self._post("/internal/activity/turn-event", {
                "event_type": "turn_ended",
                "agent": agent,
                "team": team,
                "task_id": task_id,
            })

            logger.info(
                "Turn complete | agent=%s | team=%s | tokens=%d | cost=$%.4f",
                agent, team, result.tokens_in + result.tokens_out, result.cost_usd,
            )

        except Exception:
            logger.exception("Turn failed | agent=%s | team=%s", agent, team)
            # Try to broadcast turn ended on error
            try:
                self._post("/internal/activity/turn-event", {
                    "event_type": "turn_ended",
                    "agent": agent,
                    "team": team,
                })
            except Exception:
                pass
        finally:
            self._in_flight.discard((team, agent))

    # ------------------------------------------------------------------
    # Main poll loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main satellite loop — poll coordinator and dispatch turns."""
        logger.info(
            "Satellite '%s' started — polling %s every %.1fs",
            self.satellite_id, self.coordinator_url, self.poll_interval,
        )

        sem = asyncio.Semaphore(self.max_concurrent)
        tasks: set[asyncio.Task] = set()
        retry_delay = self.poll_interval

        while True:
            try:
                # Poll coordinator for work
                result = self._get("/internal/satellite/poll", {
                    "satellite_id": self.satellite_id,
                })
                agents = result.get("agents", [])
                retry_delay = self.poll_interval  # reset on success

                for agent_info in agents:
                    team = agent_info["team"]
                    agent = agent_info["agent"]
                    key = (team, agent)

                    async with self._in_flight_lock:
                        if key in self._in_flight:
                            continue
                        self._in_flight.add(key)

                    async def _bounded_turn(t: str, a: str) -> None:
                        async with sem:
                            await self._dispatch_turn(t, a)

                    task = asyncio.create_task(_bounded_turn(team, agent))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)

            except httpx.ConnectError:
                logger.warning(
                    "Cannot reach coordinator at %s — retrying in %.0fs",
                    self.coordinator_url, retry_delay,
                )
                retry_delay = min(retry_delay * 2, 60.0)  # exponential backoff
            except httpx.HTTPStatusError as e:
                logger.error("Coordinator returned %d: %s", e.response.status_code, e.response.text)
                retry_delay = min(retry_delay * 2, 60.0)
            except asyncio.CancelledError:
                logger.info("Satellite loop cancelled")
                # Cancel all in-flight turns
                for t in list(tasks):
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                # Close telephone exchange
                if self._exchange is not None:
                    await self._exchange.close_all()
                raise
            except Exception:
                logger.exception("Error during satellite poll cycle")
                retry_delay = min(retry_delay * 2, 60.0)

            await asyncio.sleep(retry_delay)
