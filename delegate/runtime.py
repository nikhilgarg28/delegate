"""Unified agent runtime — single-turn executor.

The daemon dispatches ``run_turn()`` for each agent that has unread
messages.  Each call:

1. Reads unread inbox messages, builds prompt
2. Calls ``claude_code_sdk.query()`` (spawns a short-lived ``claude`` process)
3. Processes the response (tokens, worklogs)
4. Marks consumed messages as processed
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

import yaml

from delegate.agent import (
    AgentLogger,
    build_system_prompt,
    build_user_message,
    build_reflection_message,
    _check_reflection_due,
    _get_current_task,
    get_task_workspace,
    _agent_dir,
    _read_state,
    _next_worklog_number,
    _process_turn_messages,
    SENIORITY_MODELS,
    DEFAULT_SENIORITY,
)
from delegate.mailbox import (
    read_inbox,
    mark_seen_batch,
)
from delegate.task import format_task_id
from delegate.paths import agents_dir as _agents_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def list_ai_agents(hc_home: Path, team: str) -> list[str]:
    """Return names of AI agents for a team (excludes the boss).

    Used by the daemon loop to determine which agents can have turns
    dispatched.
    """
    adir = _agents_dir(hc_home, team)
    if not adir.is_dir():
        return []
    agents: list[str] = []
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


def agents_with_unread(hc_home: Path, team: str) -> list[str]:
    """Return AI agents that have at least one unread delivered message."""
    from delegate.mailbox import has_unread

    return [a for a in list_ai_agents(hc_home, team) if has_unread(hc_home, team, a)]


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
    task_id: int | None = None
    tokens_in: int = 0
    tokens_out: int = 0
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

    One call to ``run_turn`` processes the batch of unread messages that
    ``build_user_message()`` selects (up to K messages sharing the same
    ``task_id``).  If the 20% reflection coin-flip lands, a second
    (reflection) turn is appended within the same session.

    Returns a ``TurnResult`` with token usage and cost.
    """
    from claude_code_sdk import (
        query as default_query,
        ClaudeCodeOptions as DefaultOptions,
    )
    from delegate.chat import (
        start_session,
        end_session,
        update_session_tokens,
        update_session_task,
        log_event,
    )
    from delegate.mailbox import mark_processed_batch

    sdk_query = sdk_query or default_query
    sdk_options_class = sdk_options_class or DefaultOptions

    alog = AgentLogger(agent)
    result = TurnResult(agent=agent, team=team)

    # --- Setup ---
    ad = _agent_dir(hc_home, team, agent)
    state = _read_state(ad)
    seniority = state.get("seniority", DEFAULT_SENIORITY)
    model = SENIORITY_MODELS.get(seniority, SENIORITY_MODELS[DEFAULT_SENIORITY])
    token_budget = state.get("token_budget")
    max_turns = max(1, token_budget // 4000) if token_budget else None

    current_task = _get_current_task(hc_home, team, agent)
    current_task_id = current_task["id"] if current_task else None
    workspace = get_task_workspace(hc_home, team, agent, current_task)

    # Start session
    session_id = start_session(hc_home, team, agent, task_id=current_task_id)
    result.session_id = session_id
    result.task_id = current_task_id

    task_label = f" on {format_task_id(current_task_id)}" if current_task_id else ""
    log_event(
        hc_home, team,
        f"{agent.capitalize()} is online{task_label} [model={model}]",
        task_id=current_task_id,
    )

    alog.session_start_log(
        task_id=current_task_id,
        model=model,
        token_budget=token_budget,
        workspace=workspace,
        session_id=session_id,
    )

    # Build user message — captures exactly which message IDs are in the prompt
    user_msg, turn_msg_ids = build_user_message(hc_home, team, agent, include_context=True)

    # Mark included messages as seen
    if turn_msg_ids:
        mark_seen_batch(hc_home, team, turn_msg_ids)
    messages = read_inbox(hc_home, team, agent, unread_only=True)
    for inbox_msg in messages[:len(turn_msg_ids)]:
        alog.message_received(inbox_msg.sender, len(inbox_msg.body))

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
        )
        if model:
            kw["model"] = model
        if max_turns:
            kw["max_turns"] = max_turns
        return sdk_options_class(**kw)

    options = _build_options()

    # --- Main turn ---
    worklog_lines: list[str] = [
        f"# Worklog — {agent}",
        f"Session: {datetime.now(timezone.utc).isoformat()}",
        f"\n## Turn 1\n{user_msg}",
    ]

    alog.turn_start(1, user_msg)

    turn_tokens_in = 0
    turn_tokens_out = 0
    turn_cost = 0.0
    turn_tools: list[str] = []

    try:
        async for msg in sdk_query(prompt=user_msg, options=options):
            turn_tokens_in, turn_tokens_out, turn_cost = _process_turn_messages(
                msg, alog, turn_tokens_in, turn_tokens_out, turn_cost,
                turn_tools, worklog_lines,
            )
    except Exception as exc:
        alog.session_error(exc)
        result.error = str(exc)
        result.turns = 1
        try:
            end_session(
                hc_home, team, session_id,
                tokens_in=turn_tokens_in, tokens_out=turn_tokens_out,
                cost_usd=turn_cost,
            )
        except Exception:
            logger.exception("Failed to end session after error | agent=%s", agent)
        _write_worklog(ad, worklog_lines)

        total_tokens = turn_tokens_in + turn_tokens_out
        cost_str = f" · ${turn_cost:.4f}" if turn_cost else ""
        log_event(
            hc_home, team,
            f"{agent.capitalize()} went offline ({total_tokens:,} tokens{cost_str})",
            task_id=current_task_id,
        )
        return result

    # Log turn end
    alog.turn_end(
        1,
        tokens_in=turn_tokens_in,
        tokens_out=turn_tokens_out,
        cost_usd=turn_cost,
        cumulative_tokens_in=turn_tokens_in,
        cumulative_tokens_out=turn_tokens_out,
        cumulative_cost=turn_cost,
        tool_calls=turn_tools or None,
    )

    # Persist running totals
    update_session_tokens(
        hc_home, team, session_id,
        tokens_in=turn_tokens_in,
        tokens_out=turn_tokens_out,
        cost_usd=turn_cost,
    )

    # Mark exactly the messages that were in the prompt as processed
    if turn_msg_ids:
        mark_processed_batch(hc_home, team, turn_msg_ids)
        for mid in turn_msg_ids:
            alog.mail_marked_read(mid)

    # Re-check task association (may have been assigned during the turn)
    if current_task_id is None:
        current_task = _get_current_task(hc_home, team, agent)
        if current_task is not None:
            current_task_id = current_task["id"]
            result.task_id = current_task_id
            update_session_task(hc_home, team, session_id, current_task_id)
            alog.info(
                "Task association updated | task=%s",
                format_task_id(current_task_id),
            )

    # --- Optional reflection turn (20% chance) ---
    total_tokens_in = turn_tokens_in
    total_tokens_out = turn_tokens_out
    total_cost = turn_cost
    turn_num = 1

    if _check_reflection_due():
        turn_num = 2
        ref_msg = build_reflection_message(hc_home, team, agent)
        worklog_lines.append(f"\n## Turn 2 (reflection)\n{ref_msg}")
        alog.turn_start(2, ref_msg)

        ref_tin = 0
        ref_tout = 0
        ref_cost = 0.0
        ref_tools: list[str] = []

        try:
            ref_options = _build_options()
            async for msg in sdk_query(prompt=ref_msg, options=ref_options):
                ref_tin, ref_tout, ref_cost = _process_turn_messages(
                    msg, alog, ref_tin, ref_tout, ref_cost,
                    ref_tools, worklog_lines,
                )

            total_tokens_in += ref_tin
            total_tokens_out += ref_tout
            total_cost += ref_cost

            alog.turn_end(
                2,
                tokens_in=ref_tin,
                tokens_out=ref_tout,
                cost_usd=ref_cost,
                cumulative_tokens_in=total_tokens_in,
                cumulative_tokens_out=total_tokens_out,
                cumulative_cost=total_cost,
                tool_calls=ref_tools or None,
            )
            alog.info("Reflection turn completed")
        except Exception as exc:
            alog.error("Reflection turn failed: %s", exc)

    # --- Finalize ---
    result.tokens_in = total_tokens_in
    result.tokens_out = total_tokens_out
    result.cost_usd = total_cost
    result.turns = turn_num

    # End session
    try:
        end_session(
            hc_home, team, session_id,
            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            cost_usd=total_cost,
        )
    except Exception:
        logger.exception("Failed to end session | agent=%s", agent)

    # Log session summary
    total_tokens = total_tokens_in + total_tokens_out
    cost_str = f" · ${total_cost:.4f}" if total_cost else ""
    log_event(
        hc_home, team,
        f"{agent.capitalize()} went offline ({total_tokens:,} tokens{cost_str})",
        task_id=current_task_id,
    )

    alog.session_end_log(
        turns=turn_num,
        tokens_in=total_tokens_in,
        tokens_out=total_tokens_out,
        cost_usd=total_cost,
    )

    # Write worklog
    _write_worklog(ad, worklog_lines)

    # Save context.md for next session
    (ad / "context.md").write_text(
        f"Last session: {datetime.now(timezone.utc).isoformat()}\n"
        f"Turns: {turn_num}\n"
        f"Tokens: {total_tokens}\n"
    )

    return result
