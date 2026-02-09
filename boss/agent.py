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
    python -m boss.agent <home> <team> <agent_name> [--idle-timeout 600]
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from boss.paths import agent_dir as _resolve_agent_dir, agents_dir, base_charter_dir
from boss.mailbox import read_inbox, mark_inbox_read
from boss.task import format_task_id

logger = logging.getLogger(__name__)

# Default idle timeout in seconds (10 minutes)
DEFAULT_IDLE_TIMEOUT = 600


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


def _get_current_task(hc_home: Path, agent: str) -> dict | None:
    """Get the agent's current task dict, preferring in_progress then open."""
    try:
        from boss.task import list_tasks
        tasks = list_tasks(hc_home, assignee=agent, status="in_progress")
        if len(tasks) == 1:
            return tasks[0]
        tasks = list_tasks(hc_home, assignee=agent, status="open")
        if len(tasks) == 1:
            return tasks[0]
    except Exception:
        pass
    return None


def _get_current_task_id(hc_home: Path, agent: str) -> int | None:
    """Get the ID of the agent's current task, if exactly one."""
    task = _get_current_task(hc_home, agent)
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


def _branch_name(agent: str, task_id: int, title: str = "") -> str:
    """Compute the branch name for an agent's task.

    Format: ``<agent>/T<task_id>``
    """
    return f"{agent}/{format_task_id(task_id)}"


def setup_task_worktree(
    hc_home: Path,
    team: str,
    agent: str,
    task: dict,
) -> Path | None:
    """Set up a git worktree for the agent's current task if it has a repo.

    Creates the worktree, sets the task's ``branch`` field, and returns the
    worktree path.  Returns *None* if the task has no repo or the repo isn't
    registered.
    """
    repo_name = task.get("repo", "")
    if not repo_name:
        return None

    task_id = task["id"]
    title = task.get("title", "")
    branch = _branch_name(agent, task_id, title)

    try:
        from boss.repo import create_agent_worktree
        wt_path = create_agent_worktree(
            hc_home, team, repo_name, agent, task_id, branch=branch,
        )
    except (FileNotFoundError, Exception) as exc:
        logger.warning(
            "Could not create worktree for task %s (repo=%s): %s",
            task_id, repo_name, exc,
        )
        return None

    # Record the branch on the task
    from boss.task import set_task_branch
    set_task_branch(hc_home, task_id, branch)

    logger.info(
        "Worktree ready for %s on %s: %s (branch %s)",
        agent, task_id, wt_path, branch,
    )
    return wt_path


def push_task_branch(hc_home: Path, task: dict) -> bool:
    """Push the task's branch to origin.

    Returns True on success, False otherwise.
    """
    repo_name = task.get("repo", "")
    branch = task.get("branch", "")
    if not repo_name or not branch:
        return False

    try:
        from boss.repo import push_branch
        return push_branch(hc_home, repo_name, branch)
    except Exception as exc:
        logger.warning("Failed to push branch %s: %s", branch, exc)
        return False


def cleanup_task_worktree(
    hc_home: Path,
    team: str,
    agent: str,
    task: dict,
) -> None:
    """Push the branch and remove the agent's worktree for a completed task."""
    repo_name = task.get("repo", "")
    if not repo_name:
        return

    task_id = task["id"]

    # Push first
    push_task_branch(hc_home, task)

    # Then remove worktree
    try:
        from boss.repo import remove_agent_worktree
        remove_agent_worktree(hc_home, team, repo_name, agent, task_id)
    except Exception as exc:
        logger.warning(
            "Could not remove worktree for %s: %s", task_id, exc,
        )


def get_task_workspace(
    hc_home: Path,
    team: str,
    agent: str,
    task: dict | None,
) -> Path:
    """Return the best workspace directory for the agent.

    If the agent's current task has a repo, returns (and ensures) the worktree
    path.  Otherwise returns the generic ``<agent>/workspace/`` directory.
    """
    ad = _resolve_agent_dir(hc_home, team, agent)
    default_workspace = ad / "workspace"
    default_workspace.mkdir(parents=True, exist_ok=True)

    if task is None:
        return default_workspace

    repo_name = task.get("repo", "")
    if not repo_name:
        return default_workspace

    # Check if worktree already exists
    from boss.repo import get_worktree_path
    wt_path = get_worktree_path(hc_home, team, repo_name, agent, task["id"])
    if wt_path.is_dir():
        return wt_path

    # Create it
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
    """Build a minimal system prompt: identity + commands + file pointers.

    If the agent has a current task with an associated repo, includes
    worktree and git workflow instructions.
    """
    ad = _resolve_agent_dir(hc_home, team, agent)
    python = sys.executable

    # Look up role and key teammates
    from boss.bootstrap import get_member_by_role
    from boss.config import get_boss

    state = yaml.safe_load((ad / "state.yaml").read_text()) or {}
    role = state.get("role", "worker")
    boss_name = get_boss(hc_home) or "boss"
    manager_name = get_member_by_role(hc_home, team, "manager") or "manager"

    # Build charter file pointers (base charter from package)
    charter_dir = base_charter_dir()
    file_pointers = [
        f"  {charter_dir}/constitution.md  — team values and working agreements",
        f"  {charter_dir}/communication.md — messaging protocol details",
        f"  {charter_dir}/task-management.md — task workflow",
        f"  {charter_dir}/code-review.md   — review and merge process",
    ]
    if role == "manager":
        file_pointers.append(
            f"  {charter_dir}/manager.md       — your responsibilities as manager"
        )

    # Team override charter
    team_override = hc_home / "teams" / team / "override.md"
    if team_override.exists():
        file_pointers.append(
            f"  {team_override} — team-specific overrides"
        )

    # Roster and teammate bios
    roster = hc_home / "teams" / team / "roster.md"
    agents_root = agents_dir(hc_home, team)
    file_pointers += [
        f"  {roster}                — who is on the team",
        f"  {agents_root}/*/bio.md  — teammate backgrounds",
    ]
    files_block = "\n".join(file_pointers)

    # Workspace
    ws = workspace_path or (ad / "workspace")

    # Worktree / git section
    worktree_block = ""
    if current_task and current_task.get("repo"):
        repo_name = current_task["repo"]
        branch = current_task.get("branch", "")
        task_id = current_task["id"]
        worktree_block = f"""
GIT WORKTREE:
    You are working in a git worktree for task {format_task_id(task_id)} (repo: {repo_name}).
    Your branch: {branch}
    Worktree path: {ws}

    - Commit your changes frequently with clear messages.
    - Do NOT switch branches — stay on {branch}.
    - When the task is done, push your branch:
        git push origin {branch}
    - Other agents have their own worktrees and cannot interfere with your work.
"""

    return f"""\
You are {agent} (role: {role}), a team member in the Boss system.
{boss_name} is the human boss. You report to {manager_name} (manager).

CRITICAL: You communicate ONLY by running shell commands. Your conversational
replies are NOT seen by anyone — they only go to an internal log. To send a
message that another agent or {boss_name} will read, you MUST run:

    {python} -m boss.mailbox send {hc_home} {team} {agent} <recipient> "<message>"

Examples:
    {python} -m boss.mailbox send {hc_home} {team} {agent} {boss_name} "Here is my update..."
    {python} -m boss.mailbox send {hc_home} {team} {agent} {manager_name} "Status update..."

Other commands:
    # Task management
    {python} -m boss.task create {hc_home} --title "..." [--description "..."] [--priority high] [--repo <repo_name>]
    {python} -m boss.task list {hc_home} [--status open] [--assignee <name>]
    {python} -m boss.task assign {hc_home} <task_id> <assignee>
    {python} -m boss.task status {hc_home} <task_id> <new_status>
    {python} -m boss.task show {hc_home} <task_id>

    # Check your inbox
    {python} -m boss.mailbox inbox {hc_home} {team} {agent}

Your workspace: {ws}
Team data:      {hc_home}/teams/{team}/
{worktree_block}
IMPORTANT FILES — read these as needed:
{files_block}
"""


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

    # Unread messages — show ALL for context, but ask the agent to act on
    # only the first one.  After the turn, only that first message is marked
    # read; subsequent turns handle the rest one-by-one.
    messages = read_inbox(hc_home, team, agent, unread_only=True)
    if messages:
        parts.append(f"=== NEW MESSAGES ({len(messages)}) ===")
        for i, msg in enumerate(messages, 1):
            if i == 1:
                parts.append(f">>> ACTION REQUIRED — Message {i}/{len(messages)} <<<")
            else:
                parts.append(f"--- Upcoming message {i}/{len(messages)} (for context only) ---")
            parts.append(f"[{msg.time}] From {msg.sender}:\n{msg.body}")
        parts.append(
            "\n👉 Respond to the ACTION REQUIRED message above (message 1). "
            "You may read the other messages for context and adapt your "
            "response accordingly, but only take action on message 1. "
            "The remaining messages will be delivered for action in subsequent turns."
        )
    else:
        parts.append("No new messages.")

    # Current task assignments
    try:
        from boss.task import list_tasks
        tasks = list_tasks(hc_home, assignee=agent)
        if tasks:
            active = [t for t in tasks if t["status"] in ("open", "in_progress")]
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


# ---------------------------------------------------------------------------
# Inbox watcher
# ---------------------------------------------------------------------------

async def wait_for_inbox(
    hc_home: Path,
    team: str,
    agent: str,
    timeout: float,
) -> bool:
    """Block until a new file appears in inbox/new/ or *timeout* seconds elapse."""
    from watchfiles import awatch, Change

    inbox_new = _resolve_agent_dir(hc_home, team, agent) / "inbox" / "new"

    # If there are already unread messages, return immediately
    if any(inbox_new.iterdir()):
        return True

    stop_event = asyncio.Event()

    async def _watch() -> bool:
        async for changes in awatch(inbox_new, stop_event=stop_event):
            if any(c[0] == Change.added for c in changes):
                return True
        return False

    try:
        return await asyncio.wait_for(_watch(), timeout=timeout)
    except asyncio.TimeoutError:
        stop_event.set()
        return False


# ---------------------------------------------------------------------------
# Token / worklog helpers
# ---------------------------------------------------------------------------

def _collect_tokens_from_message(msg: Any) -> tuple[int, int, float]:
    """Extract (tokens_in, tokens_out, cost_usd) from a ResultMessage.

    The SDK's ``total_cost_usd`` on ResultMessage is a **cumulative session
    total** — it reflects the running cost across all turns in the persistent
    CLI process.  Callers should treat the returned cost as an absolute value
    (not a delta) and replace (not sum) their running total with it.

    Token counts from ``usage`` are per-turn deltas and can be summed.
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
        logger.debug(
            "Token extract from %s: usage=%r cost_usd=%r -> tin=%d tout=%d cost=%.6f",
            msg_type, usage, msg.total_cost_usd, tin, tout, cost,
        )
        return tin, tout, cost
    logger.debug("Skipping %s (no total_cost_usd attr)", msg_type)
    return 0, 0, 0.0


def _append_to_worklog(lines: list[str], msg: Any) -> None:
    """Append assistant / tool content from a message to the worklog."""
    if hasattr(msg, "content"):
        for block in getattr(msg, "content", []):
            if hasattr(block, "text"):
                lines.append(f"**Assistant**: {block.text}\n")
            elif hasattr(block, "name"):
                lines.append(f"**Tool**: {block.name}\n")


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

    ad = _agent_dir(hc_home, team, agent)

    # Guard against double-start
    state = _read_state(ad)
    if state.get("pid") is not None:
        raise RuntimeError(f"Agent {agent} already running with PID {state['pid']}")

    # Set PID
    state["pid"] = os.getpid()
    _write_state(ad, state)

    # Read token budget
    token_budget = state.get("token_budget")

    # Session tracking
    from boss.chat import start_session, end_session, log_event, update_session_task, update_session_tokens
    current_task = _get_current_task(hc_home, agent)
    current_task_id = current_task["id"] if current_task else None
    session_id = start_session(hc_home, agent, task_id=current_task_id)
    task_label = f" on {format_task_id(current_task_id)}" if current_task_id else ""
    log_event(hc_home, f"{agent.capitalize()} started{task_label}")

    total_tokens_in = 0
    total_tokens_out = 0
    total_cost_usd = 0.0

    # Worklog
    worklog_lines: list[str] = [
        f"# Worklog — {agent}",
        f"Session: {datetime.now(timezone.utc).isoformat()}",
    ]

    # Workspace — use worktree if the current task has a repo
    workspace = get_task_workspace(hc_home, team, agent, current_task)

    # Build SDK options
    system_prompt = build_system_prompt(
        hc_home, team, agent,
        current_task=current_task,
        workspace_path=workspace,
    )
    options_kwargs: dict[str, Any] = dict(
        system_prompt=system_prompt,
        cwd=str(workspace),
        permission_mode="bypassPermissions",
        add_dirs=[str(hc_home)],
    )
    if token_budget:
        options_kwargs["max_turns"] = max(1, token_budget // 4000)

    options = sdk_options_class(**options_kwargs)

    try:
        client = client_class(options)
        await client.connect()

        try:
            # --- First turn: include context.md for cold-start recovery ---
            user_msg = build_user_message(hc_home, team, agent, include_context=True)
            worklog_lines.append(f"\n## Turn 1\n{user_msg}")

            await client.query(user_msg, session_id="main")
            async for msg in client.receive_response():
                tin, tout, cost = _collect_tokens_from_message(msg)
                total_tokens_in += tin
                total_tokens_out += tout
                # cost is cumulative session total from SDK — replace, don't sum
                if cost > 0:
                    total_cost_usd = cost
                _append_to_worklog(worklog_lines, msg)

            # Persist running totals after each turn (crash-safe)
            update_session_tokens(
                hc_home, session_id,
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out,
                cost_usd=total_cost_usd,
            )
            logger.info(
                "Turn 1 tokens: in=%d out=%d cost=$%.6f",
                total_tokens_in, total_tokens_out, total_cost_usd,
            )

            # Mark only the first unread message as read (one-at-a-time processing)
            _first = read_inbox(hc_home, team, agent, unread_only=True)
            if _first and _first[0].filename:
                mark_inbox_read(hc_home, team, agent, _first[0].filename)

            # Re-check task association (may set up worktree if task acquired a repo)
            if current_task_id is None:
                current_task = _get_current_task(hc_home, agent)
                if current_task is not None:
                    current_task_id = current_task["id"]
                    update_session_task(hc_home, session_id, current_task_id)

            turn = 1

            # --- Event loop: wait for new inbox messages ---
            while True:
                has_mail = await wait_for_inbox(hc_home, team, agent, timeout=idle_timeout)
                if not has_mail:
                    logger.info("Agent %s idle timeout (%ds), exiting", agent, idle_timeout)
                    break

                turn += 1
                user_msg = build_user_message(hc_home, team, agent)
                worklog_lines.append(f"\n## Turn {turn}\n{user_msg}")

                await client.query(user_msg, session_id="main")
                async for msg in client.receive_response():
                    tin, tout, cost = _collect_tokens_from_message(msg)
                    total_tokens_in += tin
                    total_tokens_out += tout
                    # cost is cumulative session total from SDK — replace, don't sum
                    if cost > 0:
                        total_cost_usd = cost
                    _append_to_worklog(worklog_lines, msg)

                # Persist running totals after each turn (crash-safe)
                update_session_tokens(
                    hc_home, session_id,
                    tokens_in=total_tokens_in,
                    tokens_out=total_tokens_out,
                    cost_usd=total_cost_usd,
                )
                logger.info(
                    "Turn %d tokens: in=%d out=%d cost=$%.6f",
                    turn, total_tokens_in, total_tokens_out, total_cost_usd,
                )

                # Mark only the first unread message as read (one-at-a-time)
                _first = read_inbox(hc_home, team, agent, unread_only=True)
                if _first and _first[0].filename:
                    mark_inbox_read(hc_home, team, agent, _first[0].filename)

                # Re-check task association
                if current_task_id is None:
                    current_task = _get_current_task(hc_home, agent)
                    if current_task is not None:
                        current_task_id = current_task["id"]
                        update_session_task(hc_home, session_id, current_task_id)

        finally:
            await client.disconnect()

    finally:
        # End session
        end_session(
            hc_home, session_id,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            cost_usd=total_cost_usd,
        )
        total_tokens = total_tokens_in + total_tokens_out
        tokens_fmt = f"{total_tokens:,}"
        cost_str = f" \u00b7 ${total_cost_usd:.4f}" if total_cost_usd else ""
        log_event(hc_home, f"{agent.capitalize()} finished ({tokens_fmt} tokens{cost_str})")

        # Write worklog
        worklog_content = "\n".join(worklog_lines)
        log_num = _next_worklog_number(ad)
        log_path = ad / "logs" / f"{log_num}.worklog.md"
        log_path.write_text(worklog_content)

        # Save context.md
        context_path = ad / "context.md"
        context_path.write_text(
            f"Last session: {datetime.now(timezone.utc).isoformat()}\n"
            f"Turns: {turn if 'turn' in dir() else 0}\n"
            f"Tokens: {total_tokens_in + total_tokens_out}\n"
        )

        # Clear PID
        state = _read_state(ad)
        state["pid"] = None
        _write_state(ad, state)

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
    ad = _agent_dir(hc_home, team, agent)

    state = _read_state(ad)
    if state.get("pid") is not None:
        raise RuntimeError(f"Agent {agent} already running with PID {state['pid']}")

    state["pid"] = os.getpid()
    _write_state(ad, state)

    token_budget = state.get("token_budget")
    current_task = _get_current_task(hc_home, agent)
    current_task_id = current_task["id"] if current_task else None

    from boss.chat import start_session, end_session, log_event
    session_id = start_session(hc_home, agent, task_id=current_task_id)
    task_label = f" on {format_task_id(current_task_id)}" if current_task_id else ""
    log_event(hc_home, f"{agent.capitalize()} started{task_label}")

    total_tokens_in = 0
    total_tokens_out = 0
    total_cost_usd = 0.0

    try:
        workspace = get_task_workspace(hc_home, team, agent, current_task)
        system_prompt = build_system_prompt(
            hc_home, team, agent,
            current_task=current_task,
            workspace_path=workspace,
        )
        user_message = build_user_message(hc_home, team, agent)

        options_kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            cwd=str(workspace),
            permission_mode="bypassPermissions",
            add_dirs=[str(hc_home)],
        )
        if token_budget:
            options_kwargs["max_turns"] = max(1, token_budget // 4000)

        options = sdk_options_class(**options_kwargs)

        worklog_lines = [
            f"# Worklog — {agent}",
            f"Session: {datetime.now(timezone.utc).isoformat()}",
            f"\n## User Message\n{user_message}",
            "\n## Conversation\n",
        ]

        async for message in sdk_query(prompt=user_message, options=options):
            tin, tout, cost = _collect_tokens_from_message(message)
            total_tokens_in += tin
            total_tokens_out += tout
            # cost is cumulative session total from SDK — replace, don't sum
            if cost > 0:
                total_cost_usd = cost
            _append_to_worklog(worklog_lines, message)

        worklog_content = "\n".join(worklog_lines)

        log_num = _next_worklog_number(ad)
        log_path = ad / "logs" / f"{log_num}.worklog.md"
        log_path.write_text(worklog_content)

        # Mark only the first unread message as read (one-at-a-time)
        _first = read_inbox(hc_home, team, agent, unread_only=True)
        if _first and _first[0].filename:
            mark_inbox_read(hc_home, team, agent, _first[0].filename)

        return worklog_content

    finally:
        end_session(
            hc_home, session_id,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            cost_usd=total_cost_usd,
        )
        total_tokens = total_tokens_in + total_tokens_out
        tokens_fmt = f"{total_tokens:,}"
        cost_str = f" \u00b7 ${total_cost_usd:.4f}" if total_cost_usd else ""
        log_event(hc_home, f"{agent.capitalize()} finished ({tokens_fmt} tokens{cost_str})")

        state = _read_state(ad)
        state["pid"] = None
        _write_state(ad, state)


def main():
    parser = argparse.ArgumentParser(description="Run an agent")
    parser.add_argument("home", type=Path, help="Boss home directory")
    parser.add_argument("team", help="Team name")
    parser.add_argument("agent", help="Agent name")
    parser.add_argument(
        "--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
        help=f"Seconds to wait for new messages before exiting (default {DEFAULT_IDLE_TIMEOUT})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_agent_loop(args.home, args.team, args.agent, idle_timeout=args.idle_timeout))


if __name__ == "__main__":
    main()
