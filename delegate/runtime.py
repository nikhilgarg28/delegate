"""Unified agent runtime — single-turn executor.

The daemon dispatches ``run_turn()`` for each agent that has unread
messages.  Each call:

1. Reads unread inbox messages and selects a batch of ≤5 messages that
   share the same ``task_id`` as the first message.
2. Marks the selected messages as *seen*.
3. Resolves the task (if any) and all repo worktree paths.
4. Builds the user message with bidirectional conversation history,
   task context, and the selected messages.
5. Calls ``claude_code_sdk.query()`` — streaming tool summaries to the
   in-memory ring buffer, SSE subscribers, and the worklog.
6. Marks ALL selected messages as *processed*.
7. Optionally runs a reflection follow-up (1-in-10 coin flip).
8. Finalises the session: writes worklog, saves context, ends session.

All agents are "always online" — there is no PID tracking or subprocess
management.  The daemon owns the event loop and dispatches turns as
asyncio tasks with a semaphore for concurrency control.
"""

import logging
import random
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
    _agent_dir,
    _read_state,
    _next_worklog_number,
    _process_turn_messages,
    TurnTokens,
    SENIORITY_MODELS,
    DEFAULT_SENIORITY,
    MAX_BATCH_SIZE,
)
from delegate.mailbox import (
    read_inbox,
    mark_seen_batch,
    mark_processed_batch,
    Message,
)
from delegate.task import format_task_id
from delegate.activity import broadcast as broadcast_activity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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

# Reflection: 1-in-10 coin flip per turn
REFLECTION_PROBABILITY = 0.1

# In-memory turn counter per agent (module-level; single-process safe)
_turn_counts: dict[str, int] = {}


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
# Message selection — pick ≤K messages with the same task_id
# ---------------------------------------------------------------------------

def _select_batch(inbox: list[Message], max_size: int = MAX_BATCH_SIZE) -> list[Message]:
    """Select up to *max_size* messages from *inbox* that share the
    same ``task_id`` as the first message.

    The inbox is assumed to be sorted by id (oldest first).
    Both ``task_id = None`` and ``task_id = N`` are valid grouping keys.
    """
    if not inbox:
        return []

    target_task_id = inbox[0].task_id
    batch: list[Message] = []
    for msg in inbox:
        if msg.task_id != target_task_id:
            break
        batch.append(msg)
        if len(batch) >= max_size:
            break
    return batch


# ---------------------------------------------------------------------------
# Workspace resolution — multi-repo worktree paths
# ---------------------------------------------------------------------------

def _resolve_workspace(
    hc_home: Path,
    team: str,
    agent: str,
    task: dict | None,
) -> tuple[Path, dict[str, Path]]:
    """Determine the cwd and per-repo worktree paths for a turn.

    Returns ``(cwd, workspace_paths)`` where *cwd* is the working
    directory to pass to the SDK and *workspace_paths* maps each repo
    name to its worktree path (for the user message).

    Falls back to the agent's own workspace directory when there is no
    task or no repos.
    """
    from delegate.repo import get_task_worktree_path

    ad = _agent_dir(hc_home, team, agent)
    fallback = ad / "workspace"
    fallback.mkdir(parents=True, exist_ok=True)

    if not task:
        return fallback, {}

    repos: list[str] = task.get("repo", [])
    if not repos:
        return fallback, {}

    workspace_paths: dict[str, Path] = {}
    cwd: Path = fallback

    for i, repo_name in enumerate(repos):
        wt = get_task_worktree_path(hc_home, team, repo_name, task["id"])
        if wt.is_dir():
            workspace_paths[repo_name] = wt
            if i == 0:
                cwd = wt  # first available worktree is the cwd

    return cwd, workspace_paths


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
# Tool-summary extractor (feeds ring buffer + SSE + worklog)
# ---------------------------------------------------------------------------

def _extract_tool_summary(block: Any) -> tuple[str, str]:
    """Extract a ``(tool_name, detail)`` pair from an AssistantMessage block.

    Returns ``("", "")`` if the block is not a tool invocation.
    """
    if not hasattr(block, "name"):
        return "", ""

    name = block.name
    inp = getattr(block, "input", {}) or {}

    if name == "Bash":
        return name, (inp.get("command", "") or "")[:120]
    elif name in ("Edit", "Write", "Read", "MultiEdit"):
        return name, inp.get("file_path", "")
    elif name in ("Grep", "Glob"):
        return name, inp.get("pattern", "")
    else:
        keys = ", ".join(sorted(inp.keys())[:3]) if inp else ""
        return name, f"{name}({keys})" if keys else name


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

    Selects ≤5 unread messages that share the same ``task_id``, resolves
    the task and worktree paths, builds a prompt with bidirectional
    history, executes the turn (streaming tool summaries to the activity
    ring buffer / SSE), then marks every selected message as processed.

    If the 1-in-10 reflection coin-flip lands, a second (reflection)
    turn is appended within the same session.

    Returns a ``TurnResult`` with token usage and cost.
    """
    from delegate.chat import (
        start_session,
        end_session,
        update_session_tokens,
        update_session_task,
    )

    # --- SDK setup ---
    if sdk_query is None or sdk_options_class is None:
        try:
            from claude_code_sdk import (
                query as default_query,
                ClaudeCodeOptions as DefaultOptions,
            )
            sdk_query = sdk_query or default_query
            sdk_options_class = sdk_options_class or DefaultOptions
        except ImportError:
            raise RuntimeError(
                "claude_code_sdk is required for agent turns "
                "(install with: pip install claude-code-sdk)"
            )

    alog = AgentLogger(agent)
    result = TurnResult(agent=agent, team=team)

    # --- Agent setup ---
    ad = _agent_dir(hc_home, team, agent)
    state = _read_state(ad)
    seniority = state.get("seniority", DEFAULT_SENIORITY)
    role = state.get("role", "engineer")

    # Set logging caller context for all log lines during this turn
    _prev_caller = log_caller.set(f"{agent}:{role}")
    model = SENIORITY_MODELS.get(seniority, SENIORITY_MODELS[DEFAULT_SENIORITY])
    token_budget = state.get("token_budget")
    max_turns = max(1, token_budget // 4000) if token_budget else None

    # --- Message selection: pick ≤5 with same task_id ---
    inbox = read_inbox(hc_home, team, agent, unread_only=True)
    batch = _select_batch(inbox)

    if not batch:
        log_caller.reset(_prev_caller)
        return result  # nothing to do

    current_task_id: int | None = batch[0].task_id
    current_task: dict | None = None

    if current_task_id is not None:
        try:
            from delegate.task import get_task as _get_task
            current_task = _get_task(hc_home, team, current_task_id)
        except Exception:
            logger.debug("Could not resolve task %s", current_task_id)

    # --- Workspace resolution ---
    workspace, workspace_paths = _resolve_workspace(
        hc_home, team, agent, current_task,
    )

    # --- Mark selected messages as seen ---
    seen_ids = [m.filename for m in batch if m.filename]
    if seen_ids:
        mark_seen_batch(hc_home, team, seen_ids)

    for inbox_msg in batch:
        alog.message_received(inbox_msg.sender, len(inbox_msg.body))

    # --- Start session ---
    session_id = start_session(hc_home, team, agent, task_id=current_task_id)
    result.session_id = session_id

    alog.session_start_log(
        task_id=current_task_id,
        model=model,
        token_budget=token_budget,
        workspace=workspace,
        session_id=session_id,
    )

    # --- Build SDK options (stable system prompt) ---
    def _build_options() -> Any:
        sys_prompt = build_system_prompt(hc_home, team, agent)
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

    # --- Build user message (task context + history + messages) ---
    user_msg = build_user_message(
        hc_home, team, agent,
        messages=batch,
        current_task=current_task,
        workspace_paths=workspace_paths or None,
    )

    task_label = format_task_id(current_task_id) if current_task_id else ""
    worklog_lines: list[str] = [
        f"# Worklog — {agent}",
        f"Task: {task_label}" if task_label else "Task: (none)",
        f"Session: {datetime.now(timezone.utc).isoformat()}",
        f"Messages in batch: {len(batch)}",
        f"\n## Turn 1\n{user_msg}",
    ]

    alog.turn_start(1, user_msg)

    # --- Main turn: execute SDK query ---
    turn = TurnTokens()
    turn_tools: list[str] = []

    try:
        async for msg in sdk_query(prompt=user_msg, options=options):
            # Standard processing: tokens, worklog, tool list
            _process_turn_messages(
                msg, alog, turn, turn_tools, worklog_lines,
                agent=agent, task_label=task_label,
            )

            # Stream tool summaries to activity ring buffer + SSE
            if hasattr(msg, "content"):
                for block in msg.content:
                    tool_name, detail = _extract_tool_summary(block)
                    if tool_name:
                        broadcast_activity(agent, tool_name, detail)

    except Exception as exc:
        alog.session_error(exc)
        result.error = str(exc)
        result.turns = 1
        # Still mark messages as processed so they don't replay forever
        _mark_batch_processed(hc_home, team, batch)
        try:
            end_session(
                hc_home, team, session_id,
                tokens_in=turn.input, tokens_out=turn.output,
                cost_usd=turn.cost_usd,
                cache_read_tokens=turn.cache_read,
                cache_write_tokens=turn.cache_write,
            )
        except Exception:
            logger.exception("Failed to end session after error")
        _write_worklog(ad, worklog_lines)
        log_caller.reset(_prev_caller)
        return result

    # --- Post-turn bookkeeping ---
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

    update_session_tokens(
        hc_home, team, session_id,
        tokens_in=turn.input,
        tokens_out=turn.output,
        cost_usd=turn.cost_usd,
        cache_read_tokens=turn.cache_read,
        cache_write_tokens=turn.cache_write,
    )

    # Mark ALL messages in the batch as processed
    _mark_batch_processed(hc_home, team, batch)

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

    # --- Optional reflection turn (1-in-10 coin flip) ---
    total = TurnTokens(
        input=turn.input, output=turn.output,
        cache_read=turn.cache_read, cache_write=turn.cache_write,
        cost_usd=turn.cost_usd,
    )
    turn_num = 1

    # Increment in-memory turn counter
    _turn_counts[agent] = _turn_counts.get(agent, 0) + 1

    if random.random() < REFLECTION_PROBABILITY:
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

    # --- Finalize session ---
    result.tokens_in = total.input
    result.tokens_out = total.output
    result.cache_read = total.cache_read
    result.cache_write = total.cache_write
    result.cost_usd = total.cost_usd
    result.turns = turn_num

    end_session(
        hc_home, team, session_id,
        tokens_in=total.input, tokens_out=total.output,
        cost_usd=total.cost_usd,
        cache_read_tokens=total.cache_read,
        cache_write_tokens=total.cache_write,
    )

    # Log session summary
    alog.session_end_log(
        turns=turn_num,
        tokens_in=total.input,
        tokens_out=total.output,
        cost_usd=total.cost_usd,
    )

    # Write worklog
    _write_worklog(ad, worklog_lines)

    # Save context.md for next session
    total_tokens = total.input + total.output
    (ad / "context.md").write_text(
        f"Last session: {datetime.now(timezone.utc).isoformat()}\n"
        f"Turns: {turn_num}\n"
        f"Tokens: {total_tokens}\n"
    )

    # Restore logging caller context
    log_caller.reset(_prev_caller)

    return result


# ---------------------------------------------------------------------------
# Helpers (post-turn)
# ---------------------------------------------------------------------------

def _mark_batch_processed(hc_home: Path, team: str, batch: list[Message]) -> None:
    """Mark all messages in the batch as processed."""
    ids = [m.filename for m in batch if m.filename]
    if ids:
        mark_processed_batch(hc_home, team, ids)
