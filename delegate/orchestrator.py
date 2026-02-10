"""Multi-agent orchestrator — daemon logic to spawn and manage agent processes.

The orchestrator checks which agents have unread inbox messages,
spawns them if they're not already running, and manages concurrency.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

from delegate.paths import agents_dir as _agents_dir, agent_dir as _agent_dir
from delegate.mailbox import has_unread
from delegate.chat import log_event

logger = logging.getLogger(__name__)


def spawn_agent_subprocess(
    hc_home: Path, team: str, agent: str, token_budget: int | None = None,
) -> None:
    """Spawn an agent as a background subprocess.

    Writes token_budget to the agent's state.yaml before launching
    so the agent process picks it up on startup.
    """
    ad = _agent_dir(hc_home, team, agent)
    # Write token budget to agent state before spawning
    if token_budget is not None:
        state_file = ad / "state.yaml"
        state = yaml.safe_load(state_file.read_text()) or {}
        if state.get("token_budget") is None:
            state["token_budget"] = token_budget
            state_file.write_text(yaml.dump(state, default_flow_style=False))

    cmd = [sys.executable, "-m", "delegate.agent", str(hc_home), team, agent]
    proc = subprocess.Popen(cmd)
    logger.info(
        "Spawned agent %s | pid=%d | team=%s | token_budget=%s",
        agent, proc.pid, team, token_budget,
    )


def _read_state(ad: Path) -> dict:
    state_file = ad / "state.yaml"
    if state_file.exists():
        return yaml.safe_load(state_file.read_text()) or {}
    return {}


def _write_state(ad: Path, state: dict) -> None:
    (ad / "state.yaml").write_text(
        yaml.dump(state, default_flow_style=False)
    )


def _list_agents(hc_home: Path, team: str) -> list[str]:
    """List AI agents (excludes the boss, who is a human)."""
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


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def check_and_clear_stale_pids(hc_home: Path, team: str) -> list[str]:
    """Detect agents with stale PIDs (process no longer running) and clear them.

    Returns list of agent names that had stale PIDs cleared.
    """
    cleared = []
    for agent in _list_agents(hc_home, team):
        ad = _agent_dir(hc_home, team, agent)
        state = _read_state(ad)
        pid = state.get("pid")

        if pid is not None and not _is_pid_alive(pid):
            logger.warning(
                "Stale PID detected | agent=%s | pid=%d | team=%s — clearing",
                agent, pid, team,
            )
            state["pid"] = None
            _write_state(ad, state)
            cleared.append(agent)

    if cleared:
        logger.info(
            "Cleared stale PIDs | team=%s | agents=[%s]",
            team, ", ".join(cleared),
        )
    return cleared


def get_agents_needing_spawn(hc_home: Path, team: str, max_concurrent: int = 3) -> list[str]:
    """Determine which agents need to be spawned.

    An agent needs spawning if:
    1. It has unread inbox messages
    2. It doesn't have a running PID

    Returns at most `max_concurrent - currently_running` agents.
    """
    agents = _list_agents(hc_home, team)
    currently_running = 0
    needs_spawn = []

    for agent in agents:
        ad = _agent_dir(hc_home, team, agent)
        state = _read_state(ad)
        pid = state.get("pid")

        if pid is not None:
            currently_running += 1
            continue

        # Check for unread messages (fast DB check — no file I/O)
        if has_unread(hc_home, team, agent):
            needs_spawn.append(agent)
            logger.debug("Agent needs spawn | agent=%s", agent)

    # Respect concurrency limit
    available_slots = max(0, max_concurrent - currently_running)
    result = needs_spawn[:available_slots]

    logger.debug(
        "Spawn check | team=%s | running=%d | need_spawn=%d | available_slots=%d | spawning=%d",
        team, currently_running, len(needs_spawn), available_slots, len(result),
    )
    return result


def orchestrate_once(
    hc_home: Path,
    team: str,
    max_concurrent: int = 3,
    spawn_fn=None,
) -> list[str]:
    """Run one orchestration cycle.

    1. Clear stale PIDs
    2. Find agents needing spawn
    3. Spawn them via spawn_fn

    spawn_fn(hc_home, team, agent_name) is called for each agent to spawn.
    If None, just returns the list without spawning (useful for testing).

    Returns list of agent names spawned.
    """
    check_and_clear_stale_pids(hc_home, team)
    to_spawn = get_agents_needing_spawn(hc_home, team, max_concurrent)

    spawned = []
    for agent in to_spawn:
        logger.info("Spawning agent: %s | team=%s", agent, team)
        log_event(hc_home, team, f"Paging {agent.capitalize()}")

        if spawn_fn is not None:
            try:
                spawn_fn(hc_home, team, agent)
                spawned.append(agent)
                logger.info("Agent spawned successfully | agent=%s", agent)
            except Exception:
                logger.exception(
                    "Failed to spawn agent | agent=%s | team=%s", agent, team,
                )
                log_event(hc_home, team, f"Paging {agent.capitalize()} failed")
        else:
            spawned.append(agent)

    if spawned:
        logger.info(
            "Orchestration cycle complete | team=%s | spawned=[%s]",
            team, ", ".join(spawned),
        )
    return spawned
