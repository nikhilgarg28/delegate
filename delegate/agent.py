"""Agent runtime — long-lived event loop using ClaudeSDKClient.

Each agent process:
1. Connects to Claude via ClaudeSDKClient (persistent session)
2. Processes unread inbox messages as the first turn
3. Watches inbox/new/ for new files (kqueue/inotify — zero CPU while idle)
4. Sends follow-up queries within the same session when new messages arrive
5. On idle timeout or error: saves context.md, clears PID, exits

When a task has a registered repo, the agent automatically creates a git
worktree in its own directory and uses it as the working directory.  Branch
naming follows ``<agent>/T<task_id>-<slug>``.

The daemon respawns the agent if it dies and new messages arrive.

Usage:
    python -m delegate.agent <home> <team> <agent_name> [--idle-timeout 600]
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from delegate.paths import agent_dir as _resolve_agent_dir, agents_dir, base_charter_dir
from delegate.mailbox import read_inbox, mark_seen_batch, mark_processed_batch, has_unread, recent_processed
from delegate.task import format_task_id

logger = logging.getLogger(__name__)

# Default idle timeout in seconds (10 minutes)
DEFAULT_IDLE_TIMEOUT = 600

# Context window: how many recent processed messages to include per turn
CONTEXT_MSGS_SAME_SENDER = 5   # from the primary sender of the new message
CONTEXT_MSGS_OTHERS = 3         # most recent from anyone else

# Maximum unread messages to include in a single turn.  The agent is told to
# act on ALL of them, and ALL are marked processed after the turn completes.
# Any remaining messages are queued for the next turn.
MAX_MESSAGES_PER_TURN = 3

# Model mapping by seniority
SENIORITY_MODELS = {
    "senior": "opus",
    "junior": "sonnet",
}
DEFAULT_SENIORITY = "junior"


# ---------------------------------------------------------------------------
# AgentLogger — structured, per-agent session logger
# ---------------------------------------------------------------------------

class AgentLogger:
    """Rich structured logger for agent sessions.

    Wraps Python's logging module with agent-specific context (agent name,
    turn number) automatically included in every log line.  Provides
    convenience methods for the major logging events in an agent session.

    Log format:
        [agent:<name>] [turn:<N>] <message>
    """

    def __init__(self, agent_name: str, base_logger: logging.Logger | None = None):
        self.agent = agent_name
        self._logger = base_logger or logging.getLogger(f"delegate.agent.{agent_name}")
        self.turn: int = 0
        self.session_start: float = time.monotonic()

    def _prefix(self) -> str:
        """Build a structured prefix for log lines."""
        return f"[agent:{self.agent}] [turn:{self.turn}]"

    # -- Convenience log methods with auto-prefix -------------------------

    def debug(self, msg: str, *args: Any) -> None:
        self._logger.debug(f"{self._prefix()} {msg}", *args)

    def info(self, msg: str, *args: Any) -> None:
        self._logger.info(f"{self._prefix()} {msg}", *args)

    def warning(self, msg: str, *args: Any) -> None:
        self._logger.warning(f"{self._prefix()} {msg}", *args)

    def error(self, msg: str, *args: Any, exc_info: bool = False) -> None:
        self._logger.error(f"{self._prefix()} {msg}", *args, exc_info=exc_info)

    # -- Session lifecycle ------------------------------------------------

    def session_start_log(
        self,
        *,
        task_id: int | None,
        model: str | None = None,
        token_budget: int | None = None,
        workspace: Path | None = None,
        session_id: int | None = None,
        max_turns: int | None = None,
    ) -> None:
        """Log session start with all relevant parameters."""
        parts = [f"Session started (session_id={session_id})"]
        if task_id is not None:
            parts.append(f"task={format_task_id(task_id)}")
        if model:
            parts.append(f"model={model}")
        if token_budget:
            parts.append(f"token_budget={token_budget:,}")
        if max_turns is not None:
            parts.append(f"max_turns={max_turns}")
        if workspace:
            parts.append(f"workspace={workspace}")
        self.info(" | ".join(parts))

    def session_end_log(
        self,
        *,
        turns: int,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        exit_reason: str = "normal",
    ) -> None:
        """Log session end with full summary."""
        elapsed = time.monotonic() - self.session_start
        total_tokens = tokens_in + tokens_out
        self.info(
            "Session ended | reason=%s | turns=%d | tokens=%s (in=%s, out=%s) "
            "| cost=$%.4f | duration=%.1fs",
            exit_reason, turns,
            f"{total_tokens:,}", f"{tokens_in:,}", f"{tokens_out:,}",
            cost_usd, elapsed,
        )

    # -- Turn lifecycle ---------------------------------------------------

    def turn_start(self, turn_num: int, message_preview: str = "") -> None:
        """Log the start of a new turn."""
        self.turn = turn_num
        preview = message_preview[:100]
        if len(message_preview) > 100:
            preview += "..."
        self.info("Turn %d started | input_preview=%r", turn_num, preview)

    def turn_end(
        self,
        turn_num: int,
        *,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        cumulative_tokens_in: int,
        cumulative_tokens_out: int,
        cumulative_cost: float,
        tool_calls: list[str] | None = None,
    ) -> None:
        """Log the end of a turn with token/cost details."""
        self.turn = turn_num
        turn_tokens = tokens_in + tokens_out
        cumul_tokens = cumulative_tokens_in + cumulative_tokens_out
        parts = [
            f"Turn {turn_num} complete",
            f"turn_tokens={turn_tokens:,} (in={tokens_in:,}, out={tokens_out:,})",
            f"cumulative_tokens={cumul_tokens:,} (in={cumulative_tokens_in:,}, out={cumulative_tokens_out:,})",
            f"turn_cost=${cost_usd:.4f}",
            f"cumulative_cost=${cumulative_cost:.4f}",
        ]
        if tool_calls:
            parts.append(f"tools=[{', '.join(tool_calls)}]")
        self.info(" | ".join(parts))

    # -- Message routing --------------------------------------------------

    def message_received(self, sender: str, content_length: int) -> None:
        """Log an incoming message from a sender."""
        self.info(
            "Message received | from=%s | length=%d chars", sender, content_length,
        )

    def message_sent(self, recipient: str, content_length: int) -> None:
        """Log an outgoing message to a recipient."""
        self.info(
            "Message sent | to=%s | length=%d chars", recipient, content_length,
        )

    def mail_marked_read(self, filename: str) -> None:
        """Log when a message is marked as read."""
        self.debug("Mail marked read | file=%s", filename)

    # -- Tool calls -------------------------------------------------------

    def tool_call(self, tool_name: str, args_summary: str = "") -> None:
        """Log a tool call with brief args."""
        if args_summary:
            summary = args_summary[:120]
            if len(args_summary) > 120:
                summary += "..."
            self.debug("Tool call | tool=%s | args=%s", tool_name, summary)
        else:
            self.debug("Tool call | tool=%s", tool_name)

    # -- Errors -----------------------------------------------------------

    def session_error(self, error: Exception) -> None:
        """Log a session-level error with traceback."""
        self.error(
            "Session error | type=%s | message=%s",
            type(error).__name__, str(error), exc_info=True,
        )

    # -- Connection -------------------------------------------------------

    def client_connecting(self) -> None:
        """Log SDK client connection attempt."""
        self.info("Connecting to Claude SDK client")

    def client_connected(self) -> None:
        """Log successful SDK connection."""
        self.info("Claude SDK client connected")

    def client_disconnected(self) -> None:
        """Log SDK client disconnection."""
        self.info("Claude SDK client disconnected")

    # -- Idle / waiting ---------------------------------------------------

    def waiting_for_mail(self, timeout: float) -> None:
        """Log that the agent is waiting for new mail."""
        self.debug("Waiting for inbox messages | timeout=%ds", timeout)

    def idle_timeout(self, timeout: float) -> None:
        """Log idle timeout exit."""
        self.info("Idle timeout reached (%ds), shutting down", timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent_dir(hc_home: Path, team: str, agent: str) -> Path:
    d = _resolve_agent_dir(hc_home, team, agent)
    if not d.is_dir():
        raise ValueError(f"Agent '{agent}' not found at {d}")
    return d


def _read_state(ad: Path) -> dict:
    state_file = ad / "state.yaml"
    if state_file.exists():
        return yaml.safe_load(state_file.read_text()) or {}
    return {}


def _write_state(ad: Path, state: dict) -> None:
    (ad / "state.yaml").write_text(
        yaml.dump(state, default_flow_style=False)
    )


def _next_worklog_number(ad: Path) -> int:
    logs_dir = ad / "logs"
    if not logs_dir.is_dir():
        return 1
    nums = []
    for f in logs_dir.glob("*.worklog.md"):
        try:
            nums.append(int(f.stem.split(".")[0]))
        except (ValueError, IndexError):
            pass
    return max(nums, default=0) + 1


def _get_current_task(hc_home: Path, team: str, agent: str) -> dict | None:
    """Get the agent's current task dict, preferring in_progress then open."""
    try:
        from delegate.task import list_tasks
        tasks = list_tasks(hc_home, team, assignee=agent, status="in_progress")
        if len(tasks) == 1:
            return tasks[0]
        tasks = list_tasks(hc_home, team, assignee=agent, status="todo")
        if len(tasks) == 1:
            return tasks[0]
    except Exception:
        pass
    return None


def _get_current_task_id(hc_home: Path, team: str, agent: str) -> int | None:
    """Get the ID of the agent's current task, if exactly one."""
    task = _get_current_task(hc_home, team, agent)
    return task["id"] if task else None


# ---------------------------------------------------------------------------
# Git worktree helpers
# ---------------------------------------------------------------------------

def _slugify(title: str, max_len: int = 40) -> str:
    """Convert a task title to a branch-safe slug.

    Example: "Build the REST API endpoint" -> "build-the-rest-api-endpoint"
    """
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len].rstrip("-")


def _branch_name(team: str, task_id: int, title: str = "") -> str:
    """Compute the branch name for a task.

    Format: ``delegate/<team>/T<task_id>``
    The team-scoped prefix keeps branches organized across teams.
    """
    return f"delegate/{team}/{format_task_id(task_id)}"


def setup_task_worktree(
    hc_home: Path,
    team: str,
    agent: str,
    task: dict,
) -> Path | None:
    """Set up git worktrees for the agent's current task repos.

    For multi-repo tasks, creates a worktree in each repo using the same
    branch name.  Returns the path to the **first** repo's worktree (primary
    workspace).  Returns *None* if the task has no repos.
    """
    repos: list[str] = task.get("repo", [])
    if not repos:
        return None

    task_id = task["id"]
    title = task.get("title", "")
    branch = _branch_name(team, task_id, title)
    primary_wt: Path | None = None

    from delegate.repo import create_agent_worktree

    for repo_name in repos:
        try:
            wt_path = create_agent_worktree(
                hc_home, team, repo_name, agent, task_id, branch=branch,
            )
            if primary_wt is None:
                primary_wt = wt_path
            logger.info(
                "Worktree ready for %s on %s in %s: %s (branch %s)",
                agent, task_id, repo_name, wt_path, branch,
            )
        except (FileNotFoundError, Exception) as exc:
            logger.warning(
                "Could not create worktree for task %s (repo=%s): %s",
                task_id, repo_name, exc,
            )

    # Record the branch on the task
    if primary_wt is not None:
        from delegate.task import set_task_branch
        set_task_branch(hc_home, team, task_id, branch)

    return primary_wt


def push_task_branch(hc_home: Path, team: str, task: dict) -> bool:
    """Push the task's branch to origin in all repos.

    Returns True if at least one push succeeded, False otherwise.
    """
    repos: list[str] = task.get("repo", [])
    branch = task.get("branch", "")
    if not repos or not branch:
        return False

    any_ok = False
    from delegate.repo import push_branch
    for repo_name in repos:
        try:
            if push_branch(hc_home, team, repo_name, branch):
                any_ok = True
        except Exception as exc:
            logger.warning("Failed to push branch %s in repo %s: %s", branch, repo_name, exc)
    return any_ok


def cleanup_task_worktree(
    hc_home: Path,
    team: str,
    agent: str,
    task: dict,
) -> None:
    """Push the branch and remove the agent's worktrees for a completed task."""
    repos: list[str] = task.get("repo", [])
    if not repos:
        return

    task_id = task["id"]

    # Push first
    push_task_branch(hc_home, team, task)

    # Then remove worktrees in all repos
    from delegate.repo import remove_agent_worktree
    for repo_name in repos:
        try:
            remove_agent_worktree(hc_home, team, repo_name, agent, task_id)
        except Exception as exc:
            logger.warning(
                "Could not remove worktree for %s in repo %s: %s", task_id, repo_name, exc,
            )


def _ensure_task_branch_metadata(
    hc_home: Path,
    team: str,
    agent: str,
    task: dict,
    wt_path: Path,
) -> None:
    """Backfill branch and base_sha on a task when already-existing worktree is reused.

    base_sha is now always recorded at task creation time, so this should
    rarely need to do anything.  Kept as a defensive safety net for tasks
    created before eager recording was added.
    """
    task_id = task["id"]
    needs_update = False
    updates: dict = {}

    # Backfill branch name (use team-scoped naming)
    if not task.get("branch"):
        branch = _branch_name(team, task_id, task.get("title", ""))
        updates["branch"] = branch
        needs_update = True
        logger.warning(
            "Backfilling branch=%s on task %s — branch should have been set earlier",
            branch, task_id,
        )

    # Backfill base_sha (per-repo dict) from main HEAD in the worktree
    existing_base_sha: dict = task.get("base_sha", {})
    repos: list[str] = task.get("repo", [])
    if not existing_base_sha and repos:
        logger.warning(
            "Backfilling base_sha on task %s — base_sha should have been set at creation",
            task_id,
        )
        import subprocess as _sp
        base_sha_dict: dict[str, str] = {}
        for repo_name in repos:
            try:
                result = _sp.run(
                    ["git", "merge-base", "main", "HEAD"],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                sha = result.stdout.strip()
                if sha:
                    base_sha_dict[repo_name] = sha
                    logger.info("Backfilled base_sha[%s]=%s on task %s", repo_name, sha[:8], task_id)
            except Exception as exc:
                logger.warning("Could not backfill base_sha for task %s repo %s: %s", task_id, repo_name, exc)
        if base_sha_dict:
            updates["base_sha"] = base_sha_dict
            needs_update = True

    if needs_update:
        from delegate.task import update_task
        update_task(hc_home, team, task_id, **updates)


def get_task_workspace(
    hc_home: Path,
    team: str,
    agent: str,
    task: dict | None,
) -> Path:
    """Return the best workspace directory for the agent.

    If the agent's current task has a repo, returns (and ensures) the worktree
    path.  Otherwise returns the generic ``<agent>/workspace/`` directory.

    When the worktree already exists but the task is missing branch or
    base_sha metadata (e.g. after a restart), this function backfills them
    so that downstream diff and merge tooling works correctly.
    """
    ad = _resolve_agent_dir(hc_home, team, agent)
    default_workspace = ad / "workspace"
    default_workspace.mkdir(parents=True, exist_ok=True)

    if task is None:
        return default_workspace

    repos: list[str] = task.get("repo", [])
    if not repos:
        return default_workspace

    # Use first repo as primary workspace
    first_repo = repos[0]
    from delegate.repo import get_worktree_path
    wt_path = get_worktree_path(hc_home, team, first_repo, agent, task["id"])
    if wt_path.is_dir():
        _ensure_task_branch_metadata(hc_home, team, agent, task, wt_path)
        return wt_path

    # Create worktrees in all repos, return the first
    created = setup_task_worktree(hc_home, team, agent, task)
    return created if created else default_workspace


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_system_prompt(
    hc_home: Path,
    team: str,
    agent: str,
    current_task: dict | None = None,
    workspace_path: Path | None = None,
) -> str:
    """Build the system prompt, ordered for maximal prompt-cache reuse.

    Layout (top = most shared / stable, bottom = most specific / volatile):

    1. TEAM CHARTER — identical for all agents on the team (cache prefix)
    2. ROLE CHARTER — from charter/roles/<role>.md (shared per role)
    3. TEAM OVERRIDES — per-team customizations
    4. AGENT IDENTITY — name, role, seniority, boss name
    5. COMMANDS — mailbox, task CLI (includes agent-specific paths)
    6. REFLECTIONS — inlined from notes/reflections.md (agent-specific)
    7. REFERENCE FILES — file pointers (journals, notes, shared, roster, bios)
    8. WORKSPACE — worktree / git info (task-specific, most volatile)
    """
    ad = _resolve_agent_dir(hc_home, team, agent)
    python = sys.executable

    # Look up role and key teammates
    from delegate.bootstrap import get_member_by_role
    from delegate.config import get_boss

    state = yaml.safe_load((ad / "state.yaml").read_text()) or {}
    role = state.get("role", "engineer")
    seniority = state.get("seniority", DEFAULT_SENIORITY)
    boss_name = get_boss(hc_home) or "boss"
    manager_name = get_member_by_role(hc_home, team, "manager") or "manager"

    # --- 1. Universal charter (shared across ALL agents) ---
    charter_dir = base_charter_dir()
    universal_charter_files = [
        "values.md",
        "communication.md",
        "task-management.md",
        "code-review.md",
        "continuous-improvement.md",
    ]
    charter_sections = []
    for fname in universal_charter_files:
        fpath = charter_dir / fname
        if fpath.is_file():
            charter_sections.append(fpath.read_text().strip())

    charter_block = "\n\n---\n\n".join(charter_sections)

    # --- 2. Role-specific charter (e.g. roles/manager.md, roles/engineer.md) ---
    # Map generic roles to their charter filename
    _role_file_map = {
        "worker": "engineer.md",   # legacy: workers map to engineer role charter
    }
    role_charter_name = _role_file_map.get(role, f"{role}.md")
    role_block = ""
    role_path = charter_dir / "roles" / role_charter_name
    if role_path.is_file():
        content = role_path.read_text().strip()
        if content:
            role_block = f"\n\n---\n\n{content}"

    # --- 3. Team override charter ---
    override_block = ""
    team_override = hc_home / "teams" / team / "override.md"
    if team_override.exists():
        content = team_override.read_text().strip()
        if content:
            override_block = f"\n\n---\n\n# Team Overrides\n\n{content}"

    # --- 6. Reflections & feedback (inline if present) ---
    inlined_notes_block = ""

    # Reflections — lessons learned from past work
    reflections_path = ad / "notes" / "reflections.md"
    if reflections_path.is_file():
        content = reflections_path.read_text().strip()
        if content:
            inlined_notes_block += (
                "\n\n=== YOUR REFLECTIONS ===\n"
                "(Lessons learned from past work — apply these going forward.)\n\n"
                f"{content}"
            )

    # Feedback — received from teammates and reviews
    feedback_path = ad / "notes" / "feedback.md"
    if feedback_path.is_file():
        content = feedback_path.read_text().strip()
        if content:
            inlined_notes_block += (
                "\n\n=== FEEDBACK YOU'VE RECEIVED ===\n"
                "(From teammates and reviews — use this to improve.)\n\n"
                f"{content}"
            )

    # --- 7. Reference files (pointers for dynamic/large content) ---
    # Files that are inlined above are excluded from pointers.
    _inlined_notes = {"reflections.md", "feedback.md"}

    roster = hc_home / "teams" / team / "roster.md"
    agents_root = agents_dir(hc_home, team)
    shared = hc_home / "teams" / team / "shared"

    file_pointers = [
        f"  {roster}                     — team roster",
        f"  {agents_root}/*/bio.md       — teammate backgrounds",
    ]

    # Agent's own journals and notes
    journals_dir = ad / "journals"
    notes_dir = ad / "notes"
    if journals_dir.is_dir() and any(journals_dir.iterdir()):
        file_pointers.append(
            f"  {journals_dir}/T*.md          — your past task journals"
        )
    if notes_dir.is_dir():
        for note_file in sorted(notes_dir.glob("*.md")):
            if note_file.name in _inlined_notes:
                continue  # already inlined above
            file_pointers.append(
                f"  {note_file}  — {note_file.stem.replace('-', ' ')}"
            )

    # Team shared knowledge base
    if shared.is_dir() and any(shared.iterdir()):
        file_pointers.append(
            f"  {shared}/                     — team shared docs, specs, scripts"
        )

    files_block = "\n".join(file_pointers)

    # --- 8. Workspace / worktree ---
    ws = workspace_path or (ad / "workspace")
    worktree_block = ""
    if current_task and current_task.get("repo"):
        repos: list[str] = current_task["repo"]
        branch = current_task.get("branch", "")
        task_id = current_task["id"]
        repos_str = ", ".join(repos)
        worktree_block = f"""
GIT WORKTREE:
    You are working in git worktrees for task {format_task_id(task_id)}.
    Repos: {repos_str}
    Your branch: {branch}
    Primary worktree path: {ws}

    - Commit your changes frequently with clear messages.
    - Do NOT switch branches — stay on {branch}.
    - When the task is done, push your branch in each repo:
        git push origin {branch}
    - Other agents have their own worktrees and cannot interfere with your work.
"""

    return f"""\
=== TEAM CHARTER ===

{charter_block}{role_block}{override_block}

=== AGENT IDENTITY ===

You are {agent} (role: {role}, seniority: {seniority}), a team member in the Delegate system.
{boss_name} is the human boss. You report to {manager_name} (manager).

CRITICAL: You communicate ONLY by running shell commands. Your conversational
replies are NOT seen by anyone — they only go to an internal log. To send a
message that another agent or {boss_name} will read, you MUST run:

    {python} -m delegate.mailbox send {hc_home} {team} {agent} <recipient> "<message>"

Examples:
    {python} -m delegate.mailbox send {hc_home} {team} {agent} {boss_name} "Here is my update..."
    {python} -m delegate.mailbox send {hc_home} {team} {agent} {manager_name} "Status update..."

Other commands:
    # Task management
    {python} -m delegate.task create {hc_home} {team} --title "..." [--description "..."] [--priority high] [--repo <repo_name>]
    {python} -m delegate.task list {hc_home} {team} [--status open] [--assignee <name>]
    {python} -m delegate.task assign {hc_home} {team} <task_id> <assignee>
    {python} -m delegate.task status {hc_home} {team} <task_id> <new_status>
    {python} -m delegate.task show {hc_home} {team} <task_id>
    {python} -m delegate.task attach {hc_home} {team} <task_id> <file_path>
    {python} -m delegate.task detach {hc_home} {team} <task_id> <file_path>

    # Check your inbox
    {python} -m delegate.mailbox inbox {hc_home} {team} {agent}
{inlined_notes_block}

REFERENCE FILES (read as needed):
{files_block}

Your workspace: {ws}
Team data:      {hc_home}/teams/{team}/
{worktree_block}"""


REFLECTION_PROBABILITY = 0.2  # ~1 in 5 turns trigger a reflection prompt


def _check_reflection_due() -> bool:
    """Return True with ~20% probability (random coin flip)."""
    import random
    return random.random() < REFLECTION_PROBABILITY


def build_user_message(
    hc_home: Path,
    team: str,
    agent: str,
    include_context: bool = False,
) -> tuple[str, list[str]]:
    """Build the user message from unread inbox messages + assigned tasks.

    Returns ``(prompt_text, included_msg_ids)`` where *included_msg_ids* is
    the list of message IDs (as strings) that were included in the prompt.
    After the turn completes, exactly these IDs should be marked as processed.

    At most ``MAX_MESSAGES_PER_TURN`` unread messages are included.  The agent
    is told to act on ALL of them.  Remaining messages stay unprocessed and
    will be picked up in a subsequent turn.
    """
    parts: list[str] = []
    included_msg_ids: list[str] = []

    # Previous session context (cold start only)
    if include_context:
        ad = _resolve_agent_dir(hc_home, team, agent)
        context = ad / "context.md"
        if context.exists() and context.read_text().strip():
            parts.append(
                f"=== PREVIOUS SESSION CONTEXT ===\n{context.read_text().strip()}"
            )

    # --- Recent conversation history (processed messages for context) ---
    all_unread = read_inbox(hc_home, team, agent, unread_only=True)
    # Take at most K messages for this turn
    messages = all_unread[:MAX_MESSAGES_PER_TURN]
    remaining = len(all_unread) - len(messages)

    if messages:
        included_msg_ids = [m.filename for m in messages if m.filename]
        primary_sender = messages[0].sender

        # Fetch recent processed messages from the primary sender
        history_same = recent_processed(
            hc_home, team, agent, from_sender=primary_sender,
            limit=CONTEXT_MSGS_SAME_SENDER,
        )
        # Fetch recent processed messages from anyone else
        history_others = [
            m for m in recent_processed(hc_home, team, agent, limit=CONTEXT_MSGS_OTHERS * 2)
            if m.sender != primary_sender
        ][:CONTEXT_MSGS_OTHERS]

        if history_same or history_others:
            parts.append("=== RECENT CONVERSATION HISTORY ===")
            parts.append("(Previously processed messages — for context only.)\n")
            for msg in sorted(history_same + history_others, key=lambda m: m.id or 0):
                parts.append(f"[{msg.time}] From {msg.sender}:\n{msg.body}\n")

    # --- New messages — act on ALL of them ---
    if messages:
        parts.append(f"=== NEW MESSAGES ({len(messages)}) ===")
        for i, msg in enumerate(messages, 1):
            parts.append(f">>> Message {i}/{len(messages)} <<<")
            parts.append(f"[{msg.time}] From {msg.sender}:\n{msg.body}")
        if remaining > 0:
            parts.append(
                f"\n({remaining} more message(s) queued — they will arrive in your next turn.)"
            )
        parts.append(
            "\n\U0001f449 Respond to ALL messages above. Where possible, consolidate "
            "replies to the same sender into a single message. Do NOT send "
            "acknowledgment-only messages (e.g. \"Got it\", \"Thanks\", \"Standing by\"). "
            "The system automatically shows delivery and read status to senders. "
            "Only send a message when you have substantive information, a question, "
            "or a deliverable."
        )
    else:
        parts.append("No new messages.")

    # Current task assignments
    try:
        from delegate.task import list_tasks
        tasks = list_tasks(hc_home, team, assignee=agent)
        if tasks:
            active = [t for t in tasks if t["status"] in ("todo", "in_progress")]
            if active:
                parts.append("\n=== YOUR ASSIGNED TASKS ===")
                for t in active:
                    parts.append(
                        f"- {format_task_id(t['id'])} ({t['status']}): {t['title']}"
                        + (f"\n  {t['description']}" if t.get("description") else "")
                    )
    except Exception:
        pass

    return "\n".join(parts), included_msg_ids


def build_reflection_message(hc_home: Path, team: str, agent: str) -> str:
    """Build a dedicated reflection-only user message (no inbox content)."""
    ad = _resolve_agent_dir(hc_home, team, agent)
    journals_dir = ad / "journals"
    reflections_path = ad / "notes" / "reflections.md"
    feedback_path = ad / "notes" / "feedback.md"

    parts = [
        "=== REFLECTION TURN ===",
        "",
        "This is a dedicated reflection turn — no inbox messages to process.",
        "Please do the following:",
        f"1. Review your recent task journals in {journals_dir}/",
        f"2. Update {reflections_path} with patterns, lessons, and goals.",
        "   Keep it concise — bullet points, not essays.",
        f"3. Optionally review {feedback_path} and incorporate learnings.",
        "4. This file is inlined in your prompt, so future turns benefit "
        "from what you write here.",
    ]

    # Include context.md so the agent has session memory
    context = ad / "context.md"
    if context.exists() and context.read_text().strip():
        parts.insert(0, f"=== PREVIOUS SESSION CONTEXT ===\n{context.read_text().strip()}\n")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Inbox watcher
# ---------------------------------------------------------------------------

async def wait_for_inbox(
    hc_home: Path,
    team: str,
    agent: str,
    timeout: float,
    poll_interval: float = 0.5,
) -> bool:
    """Poll the DB until an unread message appears or *timeout* seconds elapse."""
    # If there are already unread messages, return immediately
    if has_unread(hc_home, team, agent):
        return True

    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        if has_unread(hc_home, team, agent):
            return True

    return False


# ---------------------------------------------------------------------------
# Token / worklog helpers
# ---------------------------------------------------------------------------

def _collect_tokens_from_message(msg: Any) -> tuple[int, int, float]:
    """Extract (tokens_in, tokens_out, cost_usd) from a ResultMessage.

    Only ``ResultMessage`` carries usage/cost data in the Claude Code SDK.
    ``AssistantMessage`` has ``content`` and ``model`` but no usage fields —
    the SDK aggregates all token/cost info into the single ``ResultMessage``
    emitted at the end of each ``client.query()`` call.

    With per-turn sessions (each turn uses a fresh ``session_id``),
    ``total_cost_usd`` and ``usage`` reflect **this turn only**, so callers
    should simply sum them across turns.
    """
    msg_type = type(msg).__name__
    if hasattr(msg, "total_cost_usd"):
        cost = msg.total_cost_usd or 0.0
        tin = 0
        tout = 0
        usage = getattr(msg, "usage", None)
        if usage and isinstance(usage, dict):
            tin = usage.get("input_tokens", 0)
            tout = usage.get("output_tokens", 0)
        elif usage is not None:
            logger.warning(
                "Unexpected usage type %s on %s — skipping token extraction",
                type(usage).__name__, msg_type,
            )
        logger.debug(
            "Token extract from %s: usage=%r cost_usd=%r -> tin=%d tout=%d cost=%.6f",
            msg_type, usage, msg.total_cost_usd, tin, tout, cost,
        )
        return tin, tout, cost
    logger.debug("Skipping %s (no total_cost_usd attr)", msg_type)
    return 0, 0, 0.0


def _extract_tool_calls(msg: Any) -> list[str]:
    """Extract tool call names from a response message."""
    tools = []
    if hasattr(msg, "content"):
        for block in msg.content:
            if hasattr(block, "name"):
                tools.append(block.name)
    return tools


def _append_to_worklog(lines: list[str], msg: Any) -> None:
    """Append assistant / tool content from a message to the worklog."""
    if hasattr(msg, "content"):
        for block in msg.content:
            if hasattr(block, "text"):
                lines.append(f"**Assistant**: {block.text}\n")
            elif hasattr(block, "name"):
                lines.append(f"**Tool**: {block.name}\n")


# ---------------------------------------------------------------------------
# Turn processing helper
# ---------------------------------------------------------------------------

def _process_turn_messages(
    msg: Any,
    alog: AgentLogger,
    turn_tokens_in: int,
    turn_tokens_out: int,
    turn_cost: float,
    turn_tools: list[str],
    worklog_lines: list[str],
) -> tuple[int, int, float]:
    """Process a single response message: extract tokens, tools, worklog.

    Returns updated (turn_tokens_in, turn_tokens_out, turn_cost).
    """
    tin, tout, cost = _collect_tokens_from_message(msg)
    turn_tokens_in += tin
    turn_tokens_out += tout
    if cost > 0:
        turn_cost = cost

    # Log tool calls
    tools = _extract_tool_calls(msg)
    for tool_name in tools:
        alog.tool_call(tool_name)
        turn_tools.append(tool_name)

    _append_to_worklog(worklog_lines, msg)
    return turn_tokens_in, turn_tokens_out, turn_cost


# ---------------------------------------------------------------------------
# Session context & shared helpers
# ---------------------------------------------------------------------------

@dataclass
class _SessionContext:
    """Mutable state shared across session setup, turn processing, and teardown."""

    hc_home: Path
    team: str
    agent: str
    ad: Path
    alog: AgentLogger
    session_id: int
    current_task: dict | None
    current_task_id: int | None
    token_budget: int | None
    max_turns: int | None
    workspace: Path
    seniority: str = DEFAULT_SENIORITY
    model: str | None = None
    worklog_lines: list[str] = field(default_factory=list)
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    turn: int = 0
    exit_reason: str = "normal"


def _session_setup(
    hc_home: Path,
    team: str,
    agent: str,
) -> _SessionContext:
    """Set up a new agent session: PID guard, session tracking, logging.

    Returns a _SessionContext with all shared state initialized.
    Raises RuntimeError if the agent is already running.
    """
    from delegate.chat import start_session, log_event

    ad = _agent_dir(hc_home, team, agent)
    alog = AgentLogger(agent)

    # Guard against double-start
    state = _read_state(ad)
    if state.get("pid") is not None:
        raise RuntimeError(f"Agent {agent} already running with PID {state['pid']}")

    # Set PID
    state["pid"] = os.getpid()
    _write_state(ad, state)

    # Read seniority and resolve model
    seniority = state.get("seniority", DEFAULT_SENIORITY)
    model = SENIORITY_MODELS.get(seniority, SENIORITY_MODELS[DEFAULT_SENIORITY])

    # Read token budget and compute max turns
    token_budget = state.get("token_budget")
    max_turns = None
    if token_budget:
        max_turns = max(1, token_budget // 4000)

    # Session tracking
    current_task = _get_current_task(hc_home, team, agent)
    current_task_id = current_task["id"] if current_task else None
    session_id = start_session(hc_home, team, agent, task_id=current_task_id)
    task_label = f" on {format_task_id(current_task_id)}" if current_task_id else ""
    log_event(hc_home, team, f"{agent.capitalize()} is online{task_label} [model={model}]", task_id=current_task_id)

    # Workspace
    workspace = get_task_workspace(hc_home, team, agent, current_task)

    # Worklog
    worklog_lines: list[str] = [
        f"# Worklog — {agent}",
        f"Session: {datetime.now(timezone.utc).isoformat()}",
    ]

    # Log session start
    alog.session_start_log(
        task_id=current_task_id,
        model=model,
        token_budget=token_budget,
        workspace=workspace,
        session_id=session_id,
        max_turns=max_turns,
    )

    return _SessionContext(
        hc_home=hc_home,
        team=team,
        agent=agent,
        ad=ad,
        alog=alog,
        session_id=session_id,
        current_task=current_task,
        current_task_id=current_task_id,
        token_budget=token_budget,
        max_turns=max_turns,
        workspace=workspace,
        seniority=seniority,
        model=model,
        worklog_lines=worklog_lines,
    )


def _session_teardown(ctx: _SessionContext) -> str:
    """Finalize a session: log summary, write worklog, save context, clear PID.

    Returns the worklog content string.
    """
    from delegate.chat import end_session, log_event

    # Log session end summary
    ctx.alog.session_end_log(
        turns=ctx.turn,
        tokens_in=ctx.total_tokens_in,
        tokens_out=ctx.total_tokens_out,
        cost_usd=ctx.total_cost_usd,
        exit_reason=ctx.exit_reason,
    )

    # End session in DB
    end_session(
        ctx.hc_home, ctx.team, ctx.session_id,
        tokens_in=ctx.total_tokens_in,
        tokens_out=ctx.total_tokens_out,
        cost_usd=ctx.total_cost_usd,
    )
    total_tokens = ctx.total_tokens_in + ctx.total_tokens_out
    tokens_fmt = f"{total_tokens:,}"
    cost_str = f" \u00b7 ${ctx.total_cost_usd:.4f}" if ctx.total_cost_usd else ""
    log_event(ctx.hc_home, ctx.team, f"{ctx.agent.capitalize()} went offline ({tokens_fmt} tokens{cost_str})", task_id=ctx.current_task_id)

    # Write worklog
    worklog_content = "\n".join(ctx.worklog_lines)
    log_num = _next_worklog_number(ctx.ad)
    log_path = ctx.ad / "logs" / f"{log_num}.worklog.md"
    log_path.write_text(worklog_content)

    # Save context.md
    context_path = ctx.ad / "context.md"
    context_path.write_text(
        f"Last session: {datetime.now(timezone.utc).isoformat()}\n"
        f"Turns: {ctx.turn}\n"
        f"Tokens: {ctx.total_tokens_in + ctx.total_tokens_out}\n"
    )

    # Clear PID
    state = _read_state(ctx.ad)
    state["pid"] = None
    _write_state(ctx.ad, state)

    return worklog_content


def _finish_turn(
    ctx: _SessionContext,
    turn_num: int,
    turn_tokens_in: int,
    turn_tokens_out: int,
    turn_cost: float,
    turn_tools: list[str],
    msg_ids_to_process: list[str] | None = None,
) -> None:
    """Post-response turn wrap-up: accumulate totals, log, persist, mark mail.

    *msg_ids_to_process* is the exact list of message IDs that were included
    in the prompt for this turn.  All of them are marked as processed so
    they won't appear again.  If ``None`` (e.g. reflection turns), no
    messages are marked.

    Updates *ctx* in place with new cumulative totals.
    """
    from delegate.chat import update_session_tokens, update_session_task

    # With per-turn sessions, cost and tokens are per-turn deltas — just sum.
    ctx.total_tokens_in += turn_tokens_in
    ctx.total_tokens_out += turn_tokens_out
    ctx.total_cost_usd += turn_cost

    ctx.turn = turn_num

    # Log turn end with full details
    ctx.alog.turn_end(
        turn_num,
        tokens_in=turn_tokens_in,
        tokens_out=turn_tokens_out,
        cost_usd=turn_cost,
        cumulative_tokens_in=ctx.total_tokens_in,
        cumulative_tokens_out=ctx.total_tokens_out,
        cumulative_cost=ctx.total_cost_usd,
        tool_calls=turn_tools if turn_tools else None,
    )

    # Persist running totals (crash-safe)
    update_session_tokens(
        ctx.hc_home, ctx.team, ctx.session_id,
        tokens_in=ctx.total_tokens_in,
        tokens_out=ctx.total_tokens_out,
        cost_usd=ctx.total_cost_usd,
    )

    # Mark exactly the messages that were in the prompt as processed
    if msg_ids_to_process:
        from delegate.mailbox import mark_processed_batch
        mark_processed_batch(ctx.hc_home, ctx.team, msg_ids_to_process)
        for mid in msg_ids_to_process:
            ctx.alog.mail_marked_read(mid)

    # Re-check task association (may set up worktree if task acquired a repo)
    if ctx.current_task_id is None:
        ctx.current_task = _get_current_task(ctx.hc_home, ctx.team, ctx.agent)
        if ctx.current_task is not None:
            ctx.current_task_id = ctx.current_task["id"]
            update_session_task(ctx.hc_home, ctx.team, ctx.session_id, ctx.current_task_id)
            ctx.alog.info(
                "Task association updated | task=%s",
                format_task_id(ctx.current_task_id),
            )



# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------

async def run_agent_loop(
    hc_home: Path,
    team: str,
    agent: str,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    client_class: Any = None,
    sdk_options_class: Any = None,
) -> str:
    """Run the agent event loop. Returns the final worklog content."""
    from claude_code_sdk import (
        ClaudeSDKClient as DefaultClient,
        ClaudeCodeOptions as DefaultOptions,
        ResultMessage,
    )

    client_class = client_class or DefaultClient
    sdk_options_class = sdk_options_class or DefaultOptions

    ctx = _session_setup(hc_home, team, agent)

    # Build SDK options
    system_prompt = build_system_prompt(
        hc_home, team, agent,
        current_task=ctx.current_task,
        workspace_path=ctx.workspace,
    )
    options_kwargs: dict[str, Any] = dict(
        system_prompt=system_prompt,
        cwd=str(ctx.workspace),
        permission_mode="bypassPermissions",
        add_dirs=[str(hc_home)],
    )
    if ctx.model:
        options_kwargs["model"] = ctx.model
    if ctx.token_budget:
        options_kwargs["max_turns"] = ctx.max_turns

    options = sdk_options_class(**options_kwargs)

    try:
        ctx.alog.client_connecting()
        client = client_class(options)
        await client.connect()
        ctx.alog.client_connected()

        try:
            # --- First turn ---
            turn = 1
            user_msg, turn_msg_ids = build_user_message(hc_home, team, agent, include_context=True)
            ctx.worklog_lines.append(f"\n## Turn {turn}\n{user_msg}")

            # Mark included messages as seen
            if turn_msg_ids:
                mark_seen_batch(hc_home, team, turn_msg_ids)
            for inbox_msg in read_inbox(hc_home, team, agent, unread_only=True)[:len(turn_msg_ids)]:
                ctx.alog.message_received(inbox_msg.sender, len(inbox_msg.body))

            ctx.alog.turn_start(turn, user_msg)

            turn_tokens_in = 0
            turn_tokens_out = 0
            turn_cost = 0.0
            turn_tools: list[str] = []

            # Each turn uses a fresh session_id so context doesn't grow
            await client.query(user_msg, session_id=f"turn-{turn}")
            async for msg in client.receive_response():
                turn_tokens_in, turn_tokens_out, turn_cost = _process_turn_messages(
                    msg, ctx.alog, turn_tokens_in, turn_tokens_out, turn_cost,
                    turn_tools, ctx.worklog_lines,
                )

            _finish_turn(ctx, turn, turn_tokens_in, turn_tokens_out, turn_cost, turn_tools,
                         msg_ids_to_process=turn_msg_ids)

            # --- Event loop: wait for new inbox messages ---
            while True:
                ctx.alog.waiting_for_mail(idle_timeout)
                has_mail = await wait_for_inbox(hc_home, team, agent, timeout=idle_timeout)
                if not has_mail:
                    ctx.exit_reason = "idle_timeout"
                    ctx.alog.idle_timeout(idle_timeout)
                    break

                turn = ctx.turn + 1
                # build_user_message returns (text, msg_ids) — IDs are captured
                # here and passed through so _finish_turn marks exactly these.
                user_msg, turn_msg_ids = build_user_message(hc_home, team, agent, include_context=True)
                ctx.worklog_lines.append(f"\n## Turn {turn}\n{user_msg}")

                # Mark included messages as seen
                if turn_msg_ids:
                    mark_seen_batch(hc_home, team, turn_msg_ids)
                    for mid in turn_msg_ids:
                        ctx.alog.info("Message %s included in turn %d", mid, turn)

                ctx.alog.turn_start(turn, user_msg)

                turn_tokens_in = 0
                turn_tokens_out = 0
                turn_cost = 0.0
                turn_tools = []

                await client.query(user_msg, session_id=f"turn-{turn}")
                async for msg in client.receive_response():
                    turn_tokens_in, turn_tokens_out, turn_cost = _process_turn_messages(
                        msg, ctx.alog, turn_tokens_in, turn_tokens_out, turn_cost,
                        turn_tools, ctx.worklog_lines,
                    )

                _finish_turn(ctx, turn, turn_tokens_in, turn_tokens_out, turn_cost, turn_tools,
                             msg_ids_to_process=turn_msg_ids)

                # After processing a real message, coin-flip for a reflection turn
                if _check_reflection_due():
                    turn = ctx.turn + 1
                    user_msg = build_reflection_message(hc_home, team, agent)
                    ctx.worklog_lines.append(f"\n## Turn {turn} (reflection)\n{user_msg}")
                    ctx.alog.turn_start(turn, user_msg)

                    turn_tokens_in = 0
                    turn_tokens_out = 0
                    turn_cost = 0.0
                    turn_tools = []

                    await client.query(user_msg, session_id=f"turn-{turn}")
                    async for msg in client.receive_response():
                        turn_tokens_in, turn_tokens_out, turn_cost = _process_turn_messages(
                            msg, ctx.alog, turn_tokens_in, turn_tokens_out, turn_cost,
                            turn_tools, ctx.worklog_lines,
                        )

                    _finish_turn(ctx, turn, turn_tokens_in, turn_tokens_out, turn_cost, turn_tools)
                    ctx.alog.info("Reflection turn completed")

        finally:
            await client.disconnect()
            ctx.alog.client_disconnected()

    except Exception as exc:
        ctx.exit_reason = "error"
        ctx.alog.session_error(exc)
        raise

    finally:
        try:
            worklog_content = _session_teardown(ctx)
        except Exception:
            # Teardown must never prevent the process from exiting cleanly.
            # If it fails, log the error and ensure the PID is cleared so
            # the orchestrator doesn't think we're still alive.
            ctx.alog.logger.exception("Session teardown failed")
            try:
                from delegate.chat import end_session
                end_session(
                    ctx.hc_home, ctx.team, ctx.session_id,
                    tokens_in=ctx.total_tokens_in,
                    tokens_out=ctx.total_tokens_out,
                    cost_usd=ctx.total_cost_usd,
                )
            except Exception:
                ctx.alog.logger.exception("Fallback end_session also failed")
            try:
                state = _read_state(ctx.ad)
                state["pid"] = None
                _write_state(ctx.ad, state)
            except Exception:
                ctx.alog.logger.exception("Fallback PID clear also failed")
            worklog_content = ""

    return worklog_content


# Keep the old name as an alias so existing call-sites work during transition
async def run_agent(
    hc_home: Path,
    team: str,
    agent: str,
    sdk_query: Any = None,
    sdk_options_class: Any = None,
    idle_timeout: float = 0,
) -> str:
    """Legacy wrapper — runs a single-turn agent (no event loop)."""
    if sdk_query is not None:
        return await _run_agent_oneshot(hc_home, team, agent, sdk_query, sdk_options_class)
    return await run_agent_loop(
        hc_home, team, agent,
        idle_timeout=idle_timeout,
        sdk_options_class=sdk_options_class,
    )


async def _run_agent_oneshot(
    hc_home: Path,
    team: str,
    agent: str,
    sdk_query: Any,
    sdk_options_class: Any,
) -> str:
    """Original one-shot agent run (used by old tests)."""
    ctx = _session_setup(hc_home, team, agent)

    try:
        system_prompt = build_system_prompt(
            hc_home, team, agent,
            current_task=ctx.current_task,
            workspace_path=ctx.workspace,
        )
        user_message, turn_msg_ids = build_user_message(hc_home, team, agent)

        options_kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            cwd=str(ctx.workspace),
            permission_mode="bypassPermissions",
            add_dirs=[str(hc_home)],
        )
        if ctx.token_budget:
            options_kwargs["max_turns"] = ctx.max_turns

        options = sdk_options_class(**options_kwargs)

        # Oneshot worklog has a slightly different format (legacy)
        ctx.worklog_lines.extend([
            f"\n## User Message\n{user_message}",
            "\n## Conversation\n",
        ])

        # Mark included messages as seen
        if turn_msg_ids:
            mark_seen_batch(hc_home, team, turn_msg_ids)
        for inbox_msg in read_inbox(hc_home, team, agent, unread_only=True)[:len(turn_msg_ids)]:
            ctx.alog.message_received(inbox_msg.sender, len(inbox_msg.body))

        ctx.alog.turn_start(1, user_message)

        turn_tokens_in = 0
        turn_tokens_out = 0
        turn_cost = 0.0
        turn_tools: list[str] = []

        async for message in sdk_query(prompt=user_message, options=options):
            turn_tokens_in, turn_tokens_out, turn_cost = _process_turn_messages(
                message, ctx.alog, turn_tokens_in, turn_tokens_out, turn_cost,
                turn_tools, ctx.worklog_lines,
            )

        _finish_turn(ctx, 1, turn_tokens_in, turn_tokens_out, turn_cost, turn_tools,
                     msg_ids_to_process=turn_msg_ids)

        return "\n".join(ctx.worklog_lines)

    except Exception as exc:
        ctx.alog.session_error(exc)
        raise

    finally:
        try:
            _session_teardown(ctx)
        except Exception:
            ctx.alog.logger.exception("Session teardown failed (oneshot)")
            try:
                from delegate.chat import end_session
                end_session(
                    ctx.hc_home, ctx.team, ctx.session_id,
                    tokens_in=ctx.total_tokens_in,
                    tokens_out=ctx.total_tokens_out,
                    cost_usd=ctx.total_cost_usd,
                )
            except Exception:
                pass
            try:
                state = _read_state(ctx.ad)
                state["pid"] = None
                _write_state(ctx.ad, state)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Run an agent")
    parser.add_argument("home", type=Path, help="Delegate home directory")
    parser.add_argument("team", help="Team name")
    parser.add_argument("agent", help="Agent name")
    parser.add_argument(
        "--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
        help=f"Seconds to wait for new messages before exiting (default {DEFAULT_IDLE_TIMEOUT})",
    )
    args = parser.parse_args()

    # Configure logging with structured format for agent sessions
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    asyncio.run(run_agent_loop(args.home, args.team, args.agent, idle_timeout=args.idle_timeout))


if __name__ == "__main__":
    main()
