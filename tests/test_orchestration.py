"""Tests for delegate/runtime.py — single-turn agent dispatch logic."""

import pytest

from delegate.mailbox import deliver, Message
from delegate.chat import get_messages
from delegate.runtime import list_ai_agents, agents_with_unread

TEAM = "testteam"


def _deliver_msg(tmp_team, to_agent, body="Hello"):
    deliver(tmp_team, TEAM, Message(
        sender="manager",
        recipient=to_agent,
        time="2026-02-08T12:00:00Z",
        body=body,
    ))


class TestListAiAgents:
    def test_lists_non_boss_agents(self, tmp_team):
        agents = list_ai_agents(tmp_team, TEAM)
        # tmp_team fixture creates manager, alice, bob, sarah — all non-boss
        assert "alice" in agents
        assert "bob" in agents

    def test_excludes_boss(self, tmp_team):
        agents = list_ai_agents(tmp_team, TEAM)
        # The boss (edison in some fixtures) should not appear
        for a in agents:
            assert a != "boss"


class TestAgentsWithUnread:
    def test_no_unread_returns_empty(self, tmp_team):
        assert agents_with_unread(tmp_team, TEAM) == []

    def test_unread_returns_agent(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        result = agents_with_unread(tmp_team, TEAM)
        assert "alice" in result

    def test_multiple_agents_with_unread(self, tmp_team):
        _deliver_msg(tmp_team, "alice")
        _deliver_msg(tmp_team, "bob")
        result = agents_with_unread(tmp_team, TEAM)
        assert "alice" in result
        assert "bob" in result
