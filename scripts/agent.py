"""Agent runtime — long-lived event loop using ClaudeSDKClient.

Each agent process:
1. Connects to Claude via ClaudeSDKClient (persistent session)
2. Processes unread inbox messages as the first turn
3. Watches inbox/new/ for new files (kqueue/inotify — zero CPU while idle)
4. Sends follow-up queries within the same session when new messages arrive
5. On idle timeout or error: saves context.md, clears PID, exits

The daemon respawns the agent if it dies and new messages arrive.

Usage:
    python -m scripts.agent <root> <agent_name> [--idle-timeout 600]
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from scripts.mailbox import read_inbox, mark_inbox_read

logger = logging.getLogger(__name__)

# Default idle timeout in seconds (10 minutes)
DEFAULT_IDLE_TIMEOUT = 600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent_dir(root: Path, agent: str) -> Path:
    d = root / ".standup" / "team" / agent
    if not d.is_dir():
        raise ValueError(f"Agent '{agent}' not found at {d}")
    return d


def _read_state(agent_dir: Path) -> dict:
    state_file = agent_dir / "state.yaml"
    if state_file.exists():
        return yaml.safe_load(state_file.read_text()) or {}
    return {}


def _write_state(agent_dir: Path, state: dict) -> None:
    (agent_dir / "state.yaml").write_text(
        yaml.dump(state, default_flow_style=False)
    )


def _next_worklog_number(agent_dir: Path) -> int:
    logs_dir = agent_dir / "logs"
    if not logs_dir.is_dir():
        return 1
    nums = []
    for f in logs_dir.glob("*.worklog.md"):
        try:
            nums.append(int(f.stem.split(".")[0]))
        except (ValueError, IndexError):
            pass
    return max(nums, default=0) + 1


def _get_current_task_id(root: Path, agent: str) -> int | None:
    """Get the ID of the agent's current task, if exactly one.

    Checks in_progress first (most specific), then falls back to open.
    At session start, tasks are typically still 'open' because the agent
    sets them to 'in_progress' during the session.
    """
    try:
        from scripts.task import list_tasks
        # Prefer in_progress (most specific)
        tasks = list_tasks(root, assignee=agent, status="in_progress")
        if len(tasks) == 1:
            return tasks[0]["id"]
        # Fall back to open (task assigned but not yet started)
        tasks = list_tasks(root, assignee=agent, status="open")
        if len(tasks) == 1:
            return tasks[0]["id"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_system_prompt(root: Path, agent: str) -> str:
    """Build a minimal system prompt: identity + commands + file pointers."""
    standup = root / ".standup"
    agent_dir = standup / "team" / agent
    python = sys.executable

    # Look up role and key teammates
    from scripts.bootstrap import get_member_by_role
    state = yaml.safe_load((agent_dir / "state.yaml").read_text()) or {}
    role = state.get("role", "worker")
    director_name = get_member_by_role(root, "director") or "director"
    manager_name = get_member_by_role(root, "manager") or "manager"

    # Build file pointers list
    file_pointers = [
        f"  {standup}/charter/constitution.md  — team values and working agreements",
        f"  {standup}/charter/communication.md — messaging protocol details",
        f"  {standup}/charter/task-management.md — task workflow",
        f"  {standup}/charter/code-review.md   — review and merge process",
    ]
    if role == "manager":
        file_pointers.append(
            f"  {standup}/charter/manager.md       — your responsibilities as manager"
        )
    file_pointers += [
        f"  {standup}/roster.md                — who is on the team",
        f"  {standup}/team/*/bio.md            — teammate backgrounds",
    ]
    files_block = "\n".join(file_pointers)

    return f"""\
You are {agent} (role: {role}), a team member in the Standup system.

CRITICAL: You communicate ONLY by running shell commands. Your conversational
replies are NOT seen by anyone — they only go to an internal log. To send a
message that another agent or the director will read, you MUST run:

    {python} -m scripts.mailbox send {root} {agent} <recipient> "<message>"

Examples:
    {python} -m scripts.mailbox send {root} {agent} {director_name} "Here is my update..."
    {python} -m scripts.mailbox send {root} {agent} {manager_name} "Status update..."

Other commands:
    # Task management
    {python} -m scripts.task create {root} --title "..." [--description "..."] [--project "..."] [--priority high]
    {python} -m scripts.task list {root} [--status open] [--assignee <name>]
    {python} -m scripts.task assign {root} <task_id> <assignee>
    {python} -m scripts.task status {root} <task_id> <new_status>
    {python} -m scripts.task show {root} <task_id>

    # Check your inbox
    {python} -m scripts.mailbox inbox {root} {agent}

Your workspace: {agent_dir}/workspace/
Team data:      {standup}/

IMPORTANT FILES — read these as needed:
{files_block}
"""


def build_user_message(
    root: Path,
    agent: str,
    include_context: bool = False,
) -> str:
    """Build the user message from unread inbox messages + assigned tasks.

    When *include_context* is True the agent's context.md is prepended
    (used on cold start to restore understanding from the previous session).
    """
    parts = []

    # Previous session context (cold start only)
    if include_context:
        agent_dir = root / ".standup" / "team" / agent
        context = agent_dir / "context.md"
        if context.exists() and context.read_text().strip():
            parts.append(
                f"=== PREVIOUS SESSION CONTEXT ===\n{context.read_text().strip()}"
            )

    # Unread messages
    messages = read_inbox(root, agent, unread_only=True)
    if messages:
        parts.append("=== NEW MESSAGES ===")
        for msg in messages:
            parts.append(f"[{msg.time}] From {msg.sender}:\n{msg.body}")
    else:
        parts.append("No new messages.")

    # Current task assignments
    try:
        from scripts.task import list_tasks
        tasks = list_tasks(root, assignee=agent)
        if tasks:
            active = [t for t in tasks if t["status"] in ("open", "in_progress")]
            if active:
                parts.append("\n=== YOUR ASSIGNED TASKS ===")
                for t in active:
                    parts.append(
                        f"- T{t['id']:04d} ({t['status']}): {t['title']}"
                        + (f"\n  {t['description']}" if t.get("description") else "")
                    )
    except Exception:
        pass

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Inbox watcher
# ---------------------------------------------------------------------------

async def wait_for_inbox(
    root: Path,
    agent: str,
    timeout: float,
) -> bool:
    """Block until a new file appears in inbox/new/ or *timeout* seconds elapse.

    Uses ``watchfiles`` (kqueue on macOS, inotify on Linux) so no CPU is
    consumed while waiting.

    Returns True if new mail arrived, False on timeout.
    """
    from watchfiles import awatch, Change

    inbox_new = root / ".standup" / "team" / agent / "inbox" / "new"

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
    """Extract (tokens_in, tokens_out, cost_usd) from a ResultMessage."""
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
            "Token extract from %s: usage=%r cost_usd=%r -> tin=%d tout=%d cost=%.4f",
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
    root: Path,
    agent: str,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    client_class: Any = None,
    sdk_options_class: Any = None,
) -> str:
    """Run the agent event loop. Returns the final worklog content.

    *client_class* and *sdk_options_class* can be injected for testing.
    """
    from claude_code_sdk import (
        ClaudeSDKClient as DefaultClient,
        ClaudeCodeOptions as DefaultOptions,
        ResultMessage,
    )

    client_class = client_class or DefaultClient
    sdk_options_class = sdk_options_class or DefaultOptions

    agent_dir = _agent_dir(root, agent)

    # Guard against double-start
    state = _read_state(agent_dir)
    if state.get("pid") is not None:
        raise RuntimeError(f"Agent {agent} already running with PID {state['pid']}")

    # Set PID
    state["pid"] = os.getpid()
    _write_state(agent_dir, state)

    # Read token budget
    token_budget = state.get("token_budget")

    # Session tracking
    from scripts.chat import start_session, end_session, log_event, update_session_task
    current_task_id = _get_current_task_id(root, agent)
    session_id = start_session(root, agent, task_id=current_task_id)
    task_label = f" on T{current_task_id:04d}" if current_task_id else ""
    log_event(root, f"Agent {agent} session started{task_label}")

    total_tokens_in = 0
    total_tokens_out = 0
    total_cost_usd = 0.0

    # Worklog
    worklog_lines: list[str] = [
        f"# Worklog — {agent}",
        f"Session: {datetime.now(timezone.utc).isoformat()}",
    ]

    # Workspace
    workspace = agent_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Build SDK options
    system_prompt = build_system_prompt(root, agent)
    options_kwargs: dict[str, Any] = dict(
        system_prompt=system_prompt,
        cwd=str(workspace),
        permission_mode="bypassPermissions",
        add_dirs=[str(root / ".standup")],
    )
    if token_budget:
        options_kwargs["max_turns"] = max(1, token_budget // 4000)

    options = sdk_options_class(**options_kwargs)

    try:
        client = client_class(options)
        await client.connect()

        try:
            # --- First turn: include context.md for cold-start recovery ---
            user_msg = build_user_message(root, agent, include_context=True)
            worklog_lines.append(f"\n## Turn 1\n{user_msg}")

            await client.query(user_msg, session_id="main")
            async for msg in client.receive_response():
                tin, tout, cost = _collect_tokens_from_message(msg)
                total_tokens_in += tin
                total_tokens_out += tout
                total_cost_usd += cost
                _append_to_worklog(worklog_lines, msg)

            # Mark inbox read
            for m in read_inbox(root, agent, unread_only=True):
                if m.filename:
                    mark_inbox_read(root, agent, m.filename)

            # Re-check task association (agent may have set a task to in_progress)
            if current_task_id is None:
                current_task_id = _get_current_task_id(root, agent)
                if current_task_id is not None:
                    update_session_task(root, session_id, current_task_id)

            turn = 1

            # --- Event loop: wait for new inbox messages ---
            while True:
                has_mail = await wait_for_inbox(root, agent, timeout=idle_timeout)
                if not has_mail:
                    logger.info("Agent %s idle timeout (%ds), exiting", agent, idle_timeout)
                    break

                turn += 1
                user_msg = build_user_message(root, agent)
                worklog_lines.append(f"\n## Turn {turn}\n{user_msg}")

                await client.query(user_msg, session_id="main")
                async for msg in client.receive_response():
                    tin, tout, cost = _collect_tokens_from_message(msg)
                    total_tokens_in += tin
                    total_tokens_out += tout
                    total_cost_usd += cost
                    _append_to_worklog(worklog_lines, msg)

                # Mark inbox read
                for m in read_inbox(root, agent, unread_only=True):
                    if m.filename:
                        mark_inbox_read(root, agent, m.filename)

                # Re-check task association after each turn
                if current_task_id is None:
                    current_task_id = _get_current_task_id(root, agent)
                    if current_task_id is not None:
                        update_session_task(root, session_id, current_task_id)

        finally:
            await client.disconnect()

    finally:
        # End session
        end_session(
            root, session_id,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            cost_usd=total_cost_usd,
        )
        total_tokens = total_tokens_in + total_tokens_out
        cost_str = f", ${total_cost_usd:.4f}" if total_cost_usd else ""
        log_event(root, f"Agent {agent} session ended ({total_tokens} tokens{cost_str})")

        # Write worklog
        worklog_content = "\n".join(worklog_lines)
        log_num = _next_worklog_number(agent_dir)
        log_path = agent_dir / "logs" / f"{log_num}.worklog.md"
        log_path.write_text(worklog_content)

        # Save context.md for next cold start
        context_path = agent_dir / "context.md"
        context_path.write_text(
            f"Last session: {datetime.now(timezone.utc).isoformat()}\n"
            f"Turns: {turn if 'turn' in dir() else 0}\n"
            f"Tokens: {total_tokens_in + total_tokens_out}\n"
        )

        # Clear PID
        state = _read_state(agent_dir)
        state["pid"] = None
        _write_state(agent_dir, state)

    return worklog_content


# Keep the old name as an alias so existing call-sites work during transition
async def run_agent(
    root: Path,
    agent: str,
    sdk_query: Any = None,
    sdk_options_class: Any = None,
    idle_timeout: float = 0,
) -> str:
    """Legacy wrapper — runs a single-turn agent (no event loop).

    When *sdk_query* is provided (testing), it falls back to the old
    one-shot code path for backward-compatible tests.
    """
    if sdk_query is not None:
        return await _run_agent_oneshot(root, agent, sdk_query, sdk_options_class)
    return await run_agent_loop(
        root, agent,
        idle_timeout=idle_timeout,
        sdk_options_class=sdk_options_class,
    )


async def _run_agent_oneshot(
    root: Path,
    agent: str,
    sdk_query: Any,
    sdk_options_class: Any,
) -> str:
    """Original one-shot agent run (used by old tests)."""
    agent_dir = _agent_dir(root, agent)

    state = _read_state(agent_dir)
    if state.get("pid") is not None:
        raise RuntimeError(f"Agent {agent} already running with PID {state['pid']}")

    state["pid"] = os.getpid()
    _write_state(agent_dir, state)

    token_budget = state.get("token_budget")
    current_task_id = _get_current_task_id(root, agent)

    from scripts.chat import start_session, end_session, log_event
    session_id = start_session(root, agent, task_id=current_task_id)
    task_label = f" on T{current_task_id:04d}" if current_task_id else ""
    log_event(root, f"Agent {agent} session started{task_label}")

    total_tokens_in = 0
    total_tokens_out = 0
    total_cost_usd = 0.0

    try:
        system_prompt = build_system_prompt(root, agent)
        user_message = build_user_message(root, agent)

        workspace = agent_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        options_kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            cwd=str(workspace),
            permission_mode="bypassPermissions",
            add_dirs=[str(root / ".standup")],
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
            total_cost_usd += cost
            _append_to_worklog(worklog_lines, message)

        worklog_content = "\n".join(worklog_lines)

        log_num = _next_worklog_number(agent_dir)
        log_path = agent_dir / "logs" / f"{log_num}.worklog.md"
        log_path.write_text(worklog_content)

        for msg in read_inbox(root, agent, unread_only=True):
            if msg.filename:
                mark_inbox_read(root, agent, msg.filename)

        return worklog_content

    finally:
        end_session(
            root, session_id,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            cost_usd=total_cost_usd,
        )
        total_tokens = total_tokens_in + total_tokens_out
        cost_str = f", ${total_cost_usd:.4f}" if total_cost_usd else ""
        log_event(root, f"Agent {agent} session ended ({total_tokens} tokens{cost_str})")

        state = _read_state(agent_dir)
        state["pid"] = None
        _write_state(agent_dir, state)


def main():
    parser = argparse.ArgumentParser(description="Run an agent")
    parser.add_argument("root", type=Path, help="Team root directory")
    parser.add_argument("agent", help="Agent name")
    parser.add_argument(
        "--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
        help=f"Seconds to wait for new messages before exiting (default {DEFAULT_IDLE_TIMEOUT})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_agent_loop(args.root, args.agent, idle_timeout=args.idle_timeout))


if __name__ == "__main__":
    main()
