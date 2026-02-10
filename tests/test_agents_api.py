"""Tests for the GET /teams/{team}/agents API — last_active_at and current_task fields."""

import os
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from delegate.task import create_task, change_status, assign_task
from delegate.web import create_app

TEAM = "testteam"


@pytest.fixture
def client(tmp_team):
    """Create a FastAPI test client with a bootstrapped team root."""
    app = create_app(hc_home=tmp_team)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

class TestAgentsEndpoint:
    def test_agents_returns_list(self, client):
        resp = client.get(f"/teams/{TEAM}/agents")
        assert resp.status_code == 200
        agents = resp.json()
        assert isinstance(agents, list)
        assert len(agents) > 0

    def test_agents_have_required_fields(self, client):
        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        for agent in agents:
            assert "name" in agent
            assert "role" in agent
            assert "last_active_at" in agent
            assert "current_task" in agent

    def test_agents_excludes_boss(self, client):
        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        names = [a["name"] for a in agents]
        assert "nikhil" not in names  # boss name from conftest


# ---------------------------------------------------------------------------
# last_active_at
# ---------------------------------------------------------------------------

class TestLastActiveAt:
    def test_last_active_at_none_without_logs(self, client, tmp_team):
        """Agents with no worklog files still return a last_active_at (from state.yaml mtime)."""
        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        # All agents should have last_active_at (at least from state.yaml)
        for agent in agents:
            assert agent["last_active_at"] is not None

    def test_last_active_at_from_worklog(self, client, tmp_team):
        """Creating a worklog file should update last_active_at."""
        from delegate.paths import agent_dir
        ad = agent_dir(tmp_team, TEAM, "alice")
        logs_dir = ad / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Create a worklog file
        wl = logs_dir / "1.worklog.md"
        wl.write_text("# Session 1\nDid some work.")

        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        alice = next(a for a in agents if a["name"] == "alice")
        assert alice["last_active_at"] is not None

        # Parse the timestamp — should be valid ISO format
        ts = datetime.fromisoformat(alice["last_active_at"])
        assert ts.tzinfo is not None  # timezone-aware

    def test_last_active_at_uses_most_recent_worklog(self, client, tmp_team):
        """last_active_at should reflect the most recently modified worklog."""
        from delegate.paths import agent_dir
        ad = agent_dir(tmp_team, TEAM, "bob")
        logs_dir = ad / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Create two worklog files with different mtimes
        wl1 = logs_dir / "1.worklog.md"
        wl1.write_text("# Session 1")

        # Ensure a measurable time gap
        time.sleep(0.05)

        wl2 = logs_dir / "2.worklog.md"
        wl2.write_text("# Session 2")

        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        bob = next(a for a in agents if a["name"] == "bob")

        ts = datetime.fromisoformat(bob["last_active_at"])
        expected_mtime = datetime.fromtimestamp(wl2.stat().st_mtime, tz=timezone.utc)
        # Within 1 second tolerance
        assert abs((ts - expected_mtime).total_seconds()) < 1.0

    def test_last_active_at_is_iso_format(self, client, tmp_team):
        """last_active_at should be a valid ISO 8601 timestamp."""
        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        for agent in agents:
            if agent["last_active_at"]:
                # Should parse without error
                dt = datetime.fromisoformat(agent["last_active_at"])
                assert dt.year >= 2020


# ---------------------------------------------------------------------------
# current_task
# ---------------------------------------------------------------------------

class TestCurrentTask:
    def test_current_task_null_when_idle(self, client):
        """Agents with no in_progress task should have current_task = null."""
        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        for agent in agents:
            assert agent["current_task"] is None

    def test_current_task_with_in_progress_task(self, client, tmp_team):
        """Assigning an in_progress task to an agent should populate current_task."""
        task = create_task(tmp_team, TEAM, title="Build the widget")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")

        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        alice = next(a for a in agents if a["name"] == "alice")

        assert alice["current_task"] is not None
        assert alice["current_task"]["id"] == task["id"]
        assert alice["current_task"]["title"] == "Build the widget"

    def test_current_task_null_for_open_task(self, client, tmp_team):
        """An 'open' task assigned to an agent should NOT appear as current_task."""
        task = create_task(tmp_team, TEAM, title="Pending task")
        assign_task(tmp_team, TEAM, task["id"], "bob")
        # Task is still 'open', not 'in_progress'

        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        bob = next(a for a in agents if a["name"] == "bob")
        assert bob["current_task"] is None

    def test_current_task_null_after_review(self, client, tmp_team):
        """A task moved to 'review' should no longer be the current_task."""
        task = create_task(tmp_team, TEAM, title="Review me")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")
        change_status(tmp_team, TEAM, task["id"], "review")

        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        alice = next(a for a in agents if a["name"] == "alice")
        assert alice["current_task"] is None

    def test_current_task_structure(self, client, tmp_team):
        """current_task should have exactly 'id' and 'title' keys."""
        task = create_task(tmp_team, TEAM, title="Structured task")
        assign_task(tmp_team, TEAM, task["id"], "bob")
        change_status(tmp_team, TEAM, task["id"], "in_progress")

        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        bob = next(a for a in agents if a["name"] == "bob")

        assert set(bob["current_task"].keys()) == {"id", "title"}

    def test_other_agents_unaffected(self, client, tmp_team):
        """Only the assigned agent should show current_task — others stay null."""
        task = create_task(tmp_team, TEAM, title="Alice's task")
        assign_task(tmp_team, TEAM, task["id"], "alice")
        change_status(tmp_team, TEAM, task["id"], "in_progress")

        resp = client.get(f"/teams/{TEAM}/agents")
        agents = resp.json()
        bob = next(a for a in agents if a["name"] == "bob")
        assert bob["current_task"] is None


# ---------------------------------------------------------------------------
# /agents (global) endpoint
# ---------------------------------------------------------------------------

class TestGlobalAgentsEndpoint:
    def test_global_agents_include_new_fields(self, client, tmp_team):
        """The /agents endpoint should also include last_active_at and current_task."""
        resp = client.get("/agents")
        assert resp.status_code == 200
        agents = resp.json()
        for agent in agents:
            assert "last_active_at" in agent
            assert "current_task" in agent
