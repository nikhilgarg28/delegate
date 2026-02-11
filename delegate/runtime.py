"""Unified agent runtime — single-turn executor.

The daemon dispatches ``run_turn()`` for each agent that has unread
messages.  Each call:

1. Reads unread inbox messages, builds prompt
2. Calls ``claude_code_sdk.query()`` (spawns a short-lived ``claude`` process)
3. Processes the response (tokens, worklogs)
4. Marks the first unread message as processed
5. Optionally runs a reflection follow-up (20% chance)
6. Writes worklog, saves context, ends session

All agents are "always online" — there is no PID tracking or subprocess
management.  The daemon owns the event loop and dispatches turns as
asyncio tasks with a semaphore for concurrency control.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from delegate.logging_setup import log_caller

from delegate.agent import (
    AgentLogger,
    build_system_prompt,
    build_user_message,
    build_reflection_message,
    _check_reflection_due,
    _agent_dir,
    _read_state,
    _next_worklog_number,
    _process_turn_messages,
    TurnTokens,
    SENIORITY_MODELS,
    DEFAULT_SENIORITY,
)
from delegate.mailbox import (
    read_inbox,
    mark_seen_batch,
    mark_processed,
)
from delegate.task import format_task_id

logger = logging.getLogger(__name__)

# Tools that agents are never allowed to use.
# Agents work in task-scoped worktrees and must not perform git operations
# that alter branch topology or interact with remotes — the merge worker
# handles rebasing and merging via controlled temporary branches.
DISALLOWED_TOOLS = [
    "Bash(git rebase:*)",
    "Bash(git merge:*)",
    "Bash(git pull:*)",
    "Bash(git push:*)",
    "Bash(git fetch:*)",
    "Bash(git checkout:*)",
    "Bash(git switch:*)",
    "Bash(git reset --hard:*)",
    "Bash(git worktree:*)",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def list_ai_agents(hc_home: Path, team: str) -> list[str]:
    """Return names of AI agents for a team (excludes the boss).

    Used to filter ``agents_with_unread()`` results — the boss is a
    human and should not have turns dispatched.
    """
    import yaml
    from delegate.paths import agents_dir as _agents_dir

    adir = _agents_dir(hc_home, team)
    if not adir.is_dir():
        return []
    agents = []
    for d in sorted(adir.iterdir()):
        if not d.is_dir():
            continue
        state_file = d / "state.yaml"
        if not state_file.exists():
            continue
        state = yaml.safe_load(state_file.read_text()) or {}
        if state.get("role") != "boss":
            agents.append(d.name)
    return agents


def _write_worklog(ad: Path, lines: list[str]) -> None:
    """Write worklog lines to the agent's logs directory."""
    log_num = _next_worklog_number(ad)
    logs_dir = ad / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{log_num}.worklog.md"
    log_path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Turn result
# ---------------------------------------------------------------------------

@dataclass
class TurnResult:
    """Result of a single agent turn."""

    agent: str
    team: str
    session_id: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Core: run a single turn for one agent
# ---------------------------------------------------------------------------

async def run_turn(
    hc_home: Path,
    team: str,
    agent: str,
    *,
    sdk_query: Any = None,
    sdk_options_class: Any = None,
) -> TurnResult:
    """Run a single turn for an agent.

    One call to ``run_turn`` processes the oldest unread message.
    If the 20% reflection coin-flip lands, a second (reflection) turn
    is appended within the same session.

    Returns a ``TurnResult`` with token usage and cost.
    """
    from delegate.chat import (
        start_session,
        end_session,
        update_session_tokens,
        update_session_task,
        log_event,
    )

    if sdk_query is None or sdk_options_class is None:
        from claude_code_sdk import (
            query as default_query,
            ClaudeCodeOptions as DefaultOptions,
        )
        sdk_query = sdk_query or default_query
        sdk_options_class = sdk_options_class or DefaultOptions

    alog = AgentLogger(agent)
    result = TurnResult(agent=agent, team=team)

    # --- Setup ---
    ad = _agent_dir(hc_home, team, agent)
    state = _read_state(ad)
    seniority = state.get("seniority", DEFAULT_SENIORITY)
    role = state.get("role", "engineer")

    # Set logging caller context for all log lines during this turn
    _prev_caller = log_caller.set(f"{agent}:{role}")
    model = SENIORITY_MODELS.get(seniority, SENIORITY_MODELS[DEFAULT_SENIORITY])
    token_budget = state.get("token_budget")
    max_turns = max(1, token_budget // 4000) if token_budget else None

    # Determine task from first unread message's task_id
    inbox_peek = read_inbox(hc_home, team, agent, unread_only=True)
    first_msg = inbox_peek[0] if inbox_peek else None
    current_task_id: int | None = first_msg.task_id if first_msg else None
    current_task: dict | None = None
    workspace: Path = ad / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    if current_task_id is not None:
        try:
            from delegate.task import get_task as _get_task
            current_task = _get_task(hc_home, team, current_task_id)
            repos = current_task.get("repo", [])
            if repos:
                from delegate.repo import get_task_worktree_path
                wt = get_task_worktree_path(hc_home, team, repos[0], current_task_id)
                if wt.is_dir():
                    workspace = wt
        except Exception:
            logger.debug("Could not resolve task %s for workspace", current_task_id)

    # Start session
    session_id = start_session(hc_home, team, agent, task_id=current_task_id)
    result.session_id = session_id

    alog.session_start_log(
        task_id=current_task_id,
        model=model,
        token_budget=token_budget,
        workspace=workspace,
        session_id=session_id,
    )

    # Build SDK options
    def _build_options() -> Any:
        sys_prompt = build_system_prompt(
            hc_home, team, agent,
            current_task=current_task,
            workspace_path=workspace,
        )
        kw: dict[str, Any] = dict(
            system_prompt=sys_prompt,
            cwd=str(workspace),
            permission_mode="bypassPermissions",
            add_dirs=[str(hc_home)],
            disallowed_tools=DISALLOWED_TOOLS,
        )
        if model:
            kw["model"] = model
        if max_turns:
            kw["max_turns"] = max_turns
        return sdk_options_class(**kw)

    options = _build_options()

    # Build user message
    user_msg = build_user_message(hc_home, team, agent, include_context=True)

    # --- Main turn ---
    task_label = format_task_id(current_task_id) if current_task_id else ""
    worklog_lines: list[str] = [
        f"# Worklog — {agent}",
        f"Task: {task_label}" if task_label else "Task: (none)",
        f"Session: {datetime.now(timezone.utc).isoformat()}",
        f"\n## Turn 1\n{user_msg}",
    ]

    # Mark messages as seen (reuse the inbox_peek we already fetched)
    messages = inbox_peek
    seen_ids = [m.filename for m in messages if m.filename]
    if seen_ids:
        mark_seen_batch(hc_home, team, seen_ids)
    for inbox_msg in messages:
        alog.message_received(inbox_msg.sender, len(inbox_msg.body))

    alog.turn_start(1, user_msg)

    turn = TurnTokens()
    turn_tools: list[str] = []

    try:
        async for msg in sdk_query(prompt=user_msg, options=options):
            _process_turn_messages(
                msg, alog, turn, turn_tools, worklog_lines,
                agent=agent, task_label=task_label,
            )
    except Exception as exc:
        alog.session_error(exc)
        result.error = str(exc)
        result.turns = 1
        end_session(
            hc_home, team, session_id,
            tokens_in=turn.input, tokens_out=turn.output,
            cost_usd=turn.cost_usd,
            cache_read_tokens=turn.cache_read,
            cache_write_tokens=turn.cache_write,
        )
        _write_worklog(ad, worklog_lines)
        log_caller.reset(_prev_caller)
        return result

    # Log turn end
    alog.turn_end(
        1,
        tokens_in=turn.input,
        tokens_out=turn.output,
        cost_usd=turn.cost_usd,
        cumulative_tokens_in=turn.input,
        cumulative_tokens_out=turn.output,
        cumulative_cost=turn.cost_usd,
        tool_calls=turn_tools or None,
    )

    # Persist running totals
    update_session_tokens(
        hc_home, team, session_id,
        tokens_in=turn.input,
        tokens_out=turn.output,
        cost_usd=turn.cost_usd,
        cache_read_tokens=turn.cache_read,
        cache_write_tokens=turn.cache_write,
    )

    # Mark the first unread message as processed
    first_unread = read_inbox(hc_home, team, agent, unread_only=True)
    if first_unread and first_unread[0].filename:
        mark_processed(hc_home, team, first_unread[0].filename)
        alog.mail_marked_read(first_unread[0].filename)

    # Re-check task association (may have been assigned during the turn)
    if current_task_id is None:
        try:
            from delegate.task import list_tasks as _list_tasks
            open_tasks = _list_tasks(hc_home, team, assignee=agent, status="in_progress")
            if open_tasks:
                current_task_id = open_tasks[0]["id"]
                update_session_task(hc_home, team, session_id, current_task_id)
                alog.info(
                    "Task association updated | task=%s",
                    format_task_id(current_task_id),
                )
        except Exception:
            pass

    # --- Optional reflection turn (20% chance) ---
    total = TurnTokens(
        input=turn.input, output=turn.output,
        cache_read=turn.cache_read, cache_write=turn.cache_write,
        cost_usd=turn.cost_usd,
    )
    turn_num = 1

    if _check_reflection_due():
        turn_num = 2
        ref_msg = build_reflection_message(hc_home, team, agent)
        worklog_lines.append(f"\n## Turn 2 (reflection)\n{ref_msg}")
        alog.turn_start(2, ref_msg)

        ref = TurnTokens()
        ref_tools: list[str] = []

        try:
            ref_options = _build_options()  # rebuild for fresh system prompt
            async for msg in sdk_query(prompt=ref_msg, options=ref_options):
                _process_turn_messages(
                    msg, alog, ref, ref_tools, worklog_lines,
                    agent=agent, task_label=task_label,
                )

            total.input += ref.input
            total.output += ref.output
            total.cache_read += ref.cache_read
            total.cache_write += ref.cache_write
            total.cost_usd += ref.cost_usd

            alog.turn_end(
                2,
                tokens_in=ref.input,
                tokens_out=ref.output,
                cost_usd=ref.cost_usd,
                cumulative_tokens_in=total.input,
                cumulative_tokens_out=total.output,
                cumulative_cost=total.cost_usd,
                tool_calls=ref_tools or None,
            )

            # DO NOT mark mail during reflection — no inbox message was acted on
            alog.info("Reflection turn completed")
        except Exception as exc:
            alog.error("Reflection turn failed: %s", exc)

    # --- Finalize ---
    result.tokens_in = total.input
    result.tokens_out = total.output
    result.cache_read = total.cache_read
    result.cache_write = total.cache_write
    result.cost_usd = total.cost_usd
    result.turns = turn_num

    # End session
    end_session(
        hc_home, team, session_id,
        tokens_in=total.input, tokens_out=total.output,
        cost_usd=total.cost_usd,
        cache_read_tokens=total.cache_read,
        cache_write_tokens=total.cache_write,
    )

    # Log session summary
    total_tokens = total.input + total.output
    alog.session_end_log(
        turns=turn_num,
        tokens_in=total.input,
        tokens_out=total.output,
        cost_usd=total.cost_usd,
    )

    # Write worklog
    _write_worklog(ad, worklog_lines)

    # Save context.md for next session
    (ad / "context.md").write_text(
        f"Last session: {datetime.now(timezone.utc).isoformat()}\n"
        f"Turns: {turn_num}\n"
        f"Tokens: {total_tokens}\n"
    )

    # Restore logging caller context
    log_caller.reset(_prev_caller)

    return result
