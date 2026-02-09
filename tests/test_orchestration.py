"""Tests for boss/orchestrator.py — multi-agent spawn/manage logic."""

import os

import pytest
import yaml

from boss.mailbox import deliver, Message
from boss.chat import get_messages
from boss.orchestrator import (
    check_and_clear_stale_pids,
    get_agents_needing_spawn,
    orchestrate_once,
)

TEAM = "testteam"


def _agent_dir(tmp_team, agent):
    return tmp_team / "teams" / TEAM / "agents" / agent


def _set_pid(tmp_team, agent, pid):
    """Helper to set an agent's PID in state.yaml."""
    state_file = _agent_dir(tmp_team, agent) / "state.yaml"
    state = yaml.safe_load(state_file.read_text()) or {}
    state["pid"] = pid
    state_file.write_text(yaml.dump(state, default_flow_style=False))


def _get_pid(tmp_team, agent):
    state_file = _agent_dir(tmp_team, agent) / "state.yaml"
    state = yaml.safe_load(state_file.read_text()) or {}
    return state.get("pid")


def _deliver_msg(tmp_team, to_agent, body="Hello"):
    deliver(tmp_team, TEAM, Message(
        sender="manager",
        recipient=to_agent,
        time="2026-02-08T12:00:00Z",
        body=body,
    ))


class TestCheckAndClearStalePids:
    def test_clears_stale_pid(self, tmp_team):
        _set_pid(tmp_team, "alice", 999999)  # PID that almost certainly doesn't exist
        cleared = check_and_clear_stale_pids(tmp_team, TEAM)
        assert "alice" in cleared
        assert _get_pid(tmp_team, "alice") is None

    def test_keeps_live_pid(self, tmp_team):
        _set_pid(tmp_team, "alice", os.getpid())  # our own PID, definitely alive
        cleared = check_and_clear_stale_pids(tmp_team, TEAM)
        assert "alice" not in cleared
        assert _get_pid(tmp_team, "alice") == os.getpid()

    def test_no_op_when_no_pids(self, tmp_team):
        cleared = check_and_clear_stale_pids(tmp_team, TEAM)
        assert cleared == []

    def test_no_event_on_clear(self, tmp_team):
        """Stale PID clearing is internal housekeeping — no event logged."""
        _set_pid(tmp_team, "alice", 999999)
        check_and_clear_stale_pids(tmp_team, TEAM)
        events = get_messages(tmp_team, msg_type="event")
        assert not any("stale PID" in e["content"] for e in events)


class TestGetAgentsNeedingSpawn:
    def test_no_unread_no_spawn(self, tmp_team):
        assert get_agents_needing_spawn(tmp_team, TEAM) == []

    def test_unread_triggers_spawn(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        result = get_agents_needing_spawn(tmp_team, TEAM)
        assert "alice" in result

    def test_already_running_skipped(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        _set_pid(tmp_team, "alice", os.getpid())
        result = get_agents_needing_spawn(tmp_team, TEAM)
        assert "alice" not in result

    def test_concurrency_limit(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        _deliver_msg(tmp_team, "bob")
        _deliver_msg(tmp_team, "manager")

        result = get_agents_needing_spawn(tmp_team, TEAM, max_concurrent=2)
        assert len(result) <= 2

    def test_concurrency_accounts_for_running(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        _deliver_msg(tmp_team, "bob")
        _set_pid(tmp_team, "manager", os.getpid())  # manager already running

        # max_concurrent=2, 1 already running, so only 1 slot
        result = get_agents_needing_spawn(tmp_team, TEAM, max_concurrent=2)
        assert len(result) <= 1


class TestOrchestrateOnce:
    def test_spawns_agent(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        spawned_agents = []

        def mock_spawn(hc_home, team, agent):
            spawned_agents.append(agent)

        result = orchestrate_once(tmp_team, TEAM, spawn_fn=mock_spawn)
        assert "alice" in result
        assert "alice" in spawned_agents

    def test_no_spawn_if_no_unread(self, tmp_team):
        result = orchestrate_once(tmp_team, TEAM, spawn_fn=lambda h, t, a: None)
        assert result == []

    def test_no_spawn_if_already_running(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        _set_pid(tmp_team, "alice", os.getpid())
        result = orchestrate_once(tmp_team, TEAM, spawn_fn=lambda h, t, a: None)
        assert "alice" not in result

    def test_clears_stale_before_spawning(self, tmp_team):
        _set_pid(tmp_team, "alice", 999999)
        _deliver_msg(tmp_team, "alice")

        spawned_agents = []
        def mock_spawn(hc_home, team, agent):
            spawned_agents.append(agent)

        result = orchestrate_once(tmp_team, TEAM, spawn_fn=mock_spawn)
        # Stale PID cleared, so alice should be spawnable
        assert "alice" in result

    def test_logs_spawn_event(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        orchestrate_once(tmp_team, TEAM, spawn_fn=lambda h, t, a: None)
        events = get_messages(tmp_team, msg_type="event")
        assert any("Alice starting" in e["content"] for e in events)

    def test_handles_spawn_failure(self, tmp_team):
        _deliver_msg(tmp_team, "alice")

        def failing_spawn(hc_home, team, agent):
            raise RuntimeError("spawn failed")

        result = orchestrate_once(tmp_team, TEAM, spawn_fn=failing_spawn)
        assert result == []  # not added to spawned list

        events = get_messages(tmp_team, msg_type="event")
        assert any("failed to start" in e["content"] for e in events)
