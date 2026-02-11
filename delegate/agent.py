"""Agent helpers — prompt builders, logging, worktree management.

This module provides the building blocks used by ``delegate.runtime``
to execute agent turns:

- ``build_system_prompt()`` / ``build_user_message()`` — prompt construction
- ``AgentLogger`` — structured per-agent logger
- Token / worklog extraction helpers
- Git worktree setup for repo-backed tasks

The actual turn execution loop lives in ``delegate.runtime``.
"""

import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from delegate.paths import agent_dir as _resolve_agent_dir, agents_dir, base_charter_dir
from delegate.mailbox import read_inbox, recent_processed
from delegate.task import format_task_id

logger = logging.getLogger(__name__)

# Default idle timeout in seconds (10 minutes)
DEFAULT_IDLE_TIMEOUT = 600

# Context window: how many recent processed messages to include per turn
CONTEXT_MSGS_SAME_SENDER = 5   # from the primary sender of the new message
CONTEXT_MSGS_OTHERS = 3         # most recent from anyone else

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


def _branch_name(hc_home: Path, team: str, task_id: int, title: str = "") -> str:
    """Compute the branch name for a task.

    Format: ``delegate/<team_id>/<team>/T<task_id>``
    The team_id (6-char hex) prevents collisions when a team is deleted and
    recreated; the team name keeps branches human-readable.
    """
    from delegate.paths import get_team_id
    tid = get_team_id(hc_home, team)
    return f"delegate/{tid}/{team}/{format_task_id(task_id)}"


def push_task_branch(hc_home: Path, team: str, task: dict) -> bool:
    """Push the task's branch to origin in all repos.

    Returns True if at least one push succeeded, False otherwise.
    """
    # NOTE: Removed push_branch() call. Delegate works with local branches only.
    # Worktrees share the same .git directory, so all branches are visible locally.
    # The merge worker (merge.py) works with local branches directly.
    return True


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
        branch = current_task.get("branch", "")
        task_id = current_task["id"]
        worktree_block = f"""
GIT WORKTREE:
    Your working directory is set to the task worktree automatically for {format_task_id(task_id)}.
    Your branch: {branch}

    - Commit your changes frequently with clear messages.
    - Do NOT switch branches — stay on {branch}.
    - Your branch is local-only and will be merged by the merge worker when approved.
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

    {python} -m delegate.mailbox send {hc_home} {team} {agent} <recipient> "<message>" --task <task_id>

The --task flag is REQUIRED when the message relates to a specific task. Omit it only for
messages to/from {boss_name} or general messages not tied to any task.

Examples:
    {python} -m delegate.mailbox send {hc_home} {team} {agent} {boss_name} "Here is my update..."
    {python} -m delegate.mailbox send {hc_home} {team} {agent} {manager_name} "Status update on T0042..." --task 42

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
) -> str:
    """Build the user message from unread inbox messages + assigned tasks."""
    parts = []

    # Previous session context (cold start only)
    if include_context:
        ad = _resolve_agent_dir(hc_home, team, agent)
        context = ad / "context.md"
        if context.exists() and context.read_text().strip():
            parts.append(
                f"=== PREVIOUS SESSION CONTEXT ===\n{context.read_text().strip()}"
            )

    # --- Recent conversation history (processed messages for context) ---
    messages = read_inbox(hc_home, team, agent, unread_only=True)
    if messages:
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

    # --- New messages — act on the first, read the rest for context ---
    if messages:
        parts.append(f"=== NEW MESSAGES ({len(messages)}) ===")
        for i, msg in enumerate(messages, 1):
            if i == 1:
                parts.append(f">>> ACTION REQUIRED — Message {i}/{len(messages)} <<<")
            else:
                parts.append(f"--- Upcoming message {i}/{len(messages)} (for context only) ---")
            parts.append(f"[{msg.time}] From {msg.sender}:\n{msg.body}")
        parts.append(
            "\n\U0001f449 Respond to the ACTION REQUIRED message above (message 1). "
            "You may read the other messages for context and adapt your "
            "response accordingly, but only take action on message 1. "
            "The remaining messages will be delivered for action in subsequent turns."
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

    return "\n".join(parts)


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
# Token / worklog helpers
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    """Token usage extracted from a single ResultMessage."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0


def _collect_tokens_from_message(msg: Any) -> TokenUsage:
    """Extract authoritative token usage from a ``ResultMessage``.

    Only ``ResultMessage`` carries usage/cost data in the Claude Code SDK.
    ``AssistantMessage`` has ``content`` and ``model`` but no usage fields —
    the SDK aggregates all token/cost info into the single ``ResultMessage``
    emitted at the end of each ``query()`` call.

    The ``usage`` dict follows the Anthropic API shape::

        {
            "input_tokens": int,
            "output_tokens": int,
            "cache_creation_input_tokens": int,   # tokens written to cache
            "cache_read_input_tokens": int,        # tokens served from cache
        }
    """
    msg_type = type(msg).__name__
    if not hasattr(msg, "total_cost_usd"):
        logger.debug("Skipping %s (no total_cost_usd attr)", msg_type)
        return TokenUsage()

    cost = msg.total_cost_usd or 0.0
    usage = getattr(msg, "usage", None)
    tin = tout = cache_read = cache_write = 0

    if usage and isinstance(usage, dict):
        tin = usage.get("input_tokens", 0)
        tout = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
    elif usage is not None:
        logger.warning(
            "Unexpected usage type %s on %s — skipping token extraction",
            type(usage).__name__, msg_type,
        )

    logger.debug(
        "Token extract from %s: in=%d out=%d cache_read=%d cache_write=%d cost=%.6f",
        msg_type, tin, tout, cache_read, cache_write, cost,
    )
    return TokenUsage(
        input=tin, output=tout,
        cache_read=cache_read, cache_write=cache_write,
        cost_usd=cost,
    )


def _extract_tool_calls_rich(msg: Any) -> list[dict]:
    """Extract tool call names and key inputs from a response message.

    Returns a list of dicts like ``{"name": "Bash", "summary": "ls -la"}``
    for richer worklog / logging output.
    """
    tools: list[dict] = []
    if not hasattr(msg, "content"):
        return tools
    for block in msg.content:
        if not hasattr(block, "name"):
            continue
        name = block.name
        inp = getattr(block, "input", {}) or {}
        if name == "Bash":
            summary = inp.get("command", "")
        elif name in ("Edit", "Write", "Read", "MultiEdit"):
            summary = inp.get("file_path", "")
        elif name == "Grep" or name == "Glob":
            summary = inp.get("pattern", "")
        else:
            # Generic: show tool name with input keys
            summary = ", ".join(sorted(inp.keys())[:3]) if inp else ""
        tools.append({"name": name, "summary": summary})
    return tools


def _append_to_worklog(
    lines: list[str],
    msg: Any,
    *,
    agent: str = "",
    task_label: str = "",
) -> None:
    """Append assistant / tool content from a message to the worklog.

    Enriched version: logs tool name + key inputs for observability.
    Also emits structured logger.info lines for the unified log.
    """
    prefix = f"{agent}" if agent else ""
    if task_label:
        prefix = f"{prefix} | {task_label}" if prefix else task_label

    if not hasattr(msg, "content"):
        return

    for block in msg.content:
        if hasattr(block, "text"):
            text = block.text or ""
            preview = text[:200].replace("\n", " ")
            lines.append(f"**Assistant**: {text}\n")
            if prefix:
                logger.info("%s | %s", prefix, preview)
        elif hasattr(block, "name"):
            name = block.name
            inp = getattr(block, "input", {}) or {}
            # Build detail string per tool type
            if name == "Bash":
                detail = inp.get("command", "(no command)")
                lines.append(f"**Tool: Bash** | `{detail}`\n")
                if prefix:
                    logger.info("%s | bash: %s", prefix, detail)
            elif name in ("Edit", "Write"):
                fpath = inp.get("file_path", "")
                lines.append(f"**Tool: {name}** | `{fpath}`\n")
                if prefix:
                    logger.info("%s | %s: %s", prefix, name.lower(), fpath)
            elif name == "Read":
                fpath = inp.get("file_path", "")
                lines.append(f"**Tool: Read** | `{fpath}`\n")
                if prefix:
                    logger.info("%s | read: %s", prefix, fpath)
            elif name == "MultiEdit":
                fpath = inp.get("file_path", "")
                lines.append(f"**Tool: MultiEdit** | `{fpath}`\n")
                if prefix:
                    logger.info("%s | multi-edit: %s", prefix, fpath)
            else:
                keys = ", ".join(sorted(inp.keys())[:3]) if inp else ""
                detail = f"{name}({keys})" if keys else name
                lines.append(f"**Tool: {detail}**\n")
                if prefix:
                    logger.info("%s | tool: %s", prefix, detail)


# Backward-compat alias for tests that import the old name
def _extract_tool_calls(msg: Any) -> list[str]:
    """Extract tool call names from a response message (compat shim)."""
    return [t["name"] for t in _extract_tool_calls_rich(msg)]


# ---------------------------------------------------------------------------
# Turn processing helper
# ---------------------------------------------------------------------------

@dataclass
class TurnTokens:
    """Mutable accumulator for token usage across a turn."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0

    def add(self, usage: TokenUsage) -> None:
        self.input += usage.input
        self.output += usage.output
        self.cache_read += usage.cache_read
        self.cache_write += usage.cache_write
        if usage.cost_usd > 0:
            self.cost_usd = usage.cost_usd  # ResultMessage gives cumulative cost


def _process_turn_messages(
    msg: Any,
    alog: AgentLogger,
    turn_tokens: TurnTokens,
    turn_tools: list[str],
    worklog_lines: list[str],
    *,
    agent: str = "",
    task_label: str = "",
) -> None:
    """Process a single response message: extract tokens, tools, worklog.

    Mutates ``turn_tokens`` and ``turn_tools`` in place.
    """
    usage = _collect_tokens_from_message(msg)
    turn_tokens.add(usage)

    # Log tool calls
    tools = _extract_tool_calls_rich(msg)
    for t in tools:
        alog.tool_call(t["name"], t.get("summary", ""))
        turn_tools.append(t["name"])

    _append_to_worklog(worklog_lines, msg, agent=agent, task_label=task_label)
