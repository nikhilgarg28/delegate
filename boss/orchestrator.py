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

from boss.paths import agents_dir as _agents_dir, agent_dir as _agent_dir
from boss.mailbox import read_inbox
from boss.chat import log_event

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

    cmd = [sys.executable, "-m", "boss.agent", str(hc_home), team, agent]
    proc = subprocess.Popen(cmd)
    logger.info("Spawned agent %s with PID %d", agent, proc.pid)


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
            logger.warning("Agent %s has stale PID %d, clearing", agent, pid)
            state["pid"] = None
            _write_state(ad, state)
            # Stale PID cleared — internal housekeeping, no event logged
            cleared.append(agent)

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

        # Check for unread messages
        unread = read_inbox(hc_home, team, agent, unread_only=True)
        if unread:
            needs_spawn.append(agent)

    # Respect concurrency limit
    available_slots = max(0, max_concurrent - currently_running)
    return needs_spawn[:available_slots]


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
        logger.info("Spawning agent: %s", agent)
        log_event(hc_home, f"{agent.capitalize()} starting")

        if spawn_fn is not None:
            try:
                spawn_fn(hc_home, team, agent)
                spawned.append(agent)
            except Exception:
                logger.exception("Failed to spawn agent %s", agent)
                log_event(hc_home, f"{agent.capitalize()} failed to start")
        else:
            spawned.append(agent)

    return spawned
