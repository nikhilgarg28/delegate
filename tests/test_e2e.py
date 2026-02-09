"""End-to-end tests — message passing, routing, orchestration without the UI.

These tests exercise the full pipeline:
    bootstrap → send → route → deliver → orchestrate
using real file I/O and the SQLite database, with no mocking.
"""

import os

import pytest
import yaml

from boss.bootstrap import bootstrap
from boss.config import set_boss, get_boss
from boss.mailbox import send, read_inbox, read_outbox, deliver, Message
from boss.router import route_once, BossQueue
from boss.orchestrator import orchestrate_once, get_agents_needing_spawn
from boss.chat import get_messages, log_event
from boss.task import create_task, get_task, change_status, assign_task, format_task_id
from boss.paths import (
    boss_person_dir,
    agent_dir,
    agents_dir,
    tasks_dir,
    db_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DIRECTOR = "nikhil"
TEAM_A = "alpha"
TEAM_B = "beta"


@pytest.fixture
def hc(tmp_path):
    """Fully bootstrapped boss home with two teams and a global boss."""
    home = tmp_path / "hc"
    set_boss(home, DIRECTOR)

    # Team alpha: manager + 2 workers + QA
    bootstrap(home, team_name=TEAM_A, manager="edison", agents=["alice", "bob"], qa="sarah")

    # Team beta: manager + 1 worker (unique names enforced)
    bootstrap(home, team_name=TEAM_B, manager="maria", agents=["charlie"])

    return home


# ---------------------------------------------------------------------------
# Sanity: directory structure
# ---------------------------------------------------------------------------


class TestBossyStructure:
    """Verify the bootstrapped directory layout."""

    def test_boss_mailbox_is_global(self, hc):
        """Boss mailbox lives at hc/boss/, outside any team."""
        dd = boss_person_dir(hc)
        assert dd.is_dir()
        assert (dd / "inbox" / "new").is_dir()
        assert (dd / "outbox" / "new").is_dir()
        # NOT inside any team
        assert not (agents_dir(hc, TEAM_A) / DIRECTOR).exists()
        assert not (agents_dir(hc, TEAM_B) / DIRECTOR).exists()

    def test_team_agents_exist(self, hc):
        """Each team's agents have their own bossies."""
        for name in ["edison", "alice", "bob", "sarah"]:
            assert agent_dir(hc, TEAM_A, name).is_dir()
        for name in ["maria", "charlie"]:
            assert agent_dir(hc, TEAM_B, name).is_dir()

    def test_boss_config(self, hc):
        """Boss name is set in global config."""
        assert get_boss(hc) == DIRECTOR


# ---------------------------------------------------------------------------
# E2E: single-team message passing
# ---------------------------------------------------------------------------


class TestSingleTeamMessaging:
    """Boss ↔ manager ↔ worker message flow within one team."""

    def test_boss_to_manager(self, hc):
        """Boss sends a message; router delivers it to the manager."""
        # Boss sends via global outbox
        send(hc, TEAM_A, DIRECTOR, "edison", "Please start the sprint.")

        # Verify message is in boss's global outbox
        pending = read_outbox(hc, TEAM_A, DIRECTOR, pending_only=True)
        assert len(pending) == 1
        assert pending[0].recipient == "edison"

        # Route
        routed = route_once(hc, TEAM_A, boss_name=DIRECTOR)
        assert routed == 1

        # Manager received it
        inbox = read_inbox(hc, TEAM_A, "edison", unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == DIRECTOR
        assert inbox[0].body == "Please start the sprint."

        # Boss's outbox is now empty (moved to cur)
        assert read_outbox(hc, TEAM_A, DIRECTOR, pending_only=True) == []

        # Logged in SQLite
        msgs = get_messages(hc, msg_type="chat")
        assert len(msgs) == 1
        assert msgs[0]["sender"] == DIRECTOR

    def test_manager_to_boss(self, hc):
        """Manager replies to boss; delivered to boss's global inbox."""
        send(hc, TEAM_A, "edison", DIRECTOR, "Sprint started, 3 tasks assigned.")
        routed = route_once(hc, TEAM_A, boss_name=DIRECTOR)
        assert routed == 1

        # Boss's global inbox has the message
        inbox = read_inbox(hc, TEAM_A, DIRECTOR, unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == "edison"

    def test_manager_to_worker(self, hc):
        """Manager assigns work to a team member."""
        send(hc, TEAM_A, "edison", "alice", "Please work on T0001.")
        routed = route_once(hc, TEAM_A)
        assert routed == 1

        inbox = read_inbox(hc, TEAM_A, "alice")
        assert len(inbox) == 1
        assert "T0001" in inbox[0].body

    def test_worker_to_qa(self, hc):
        """Worker sends a review request to QA."""
        send(hc, TEAM_A, "alice", "sarah", "REVIEW_REQUEST: repo=myapp branch=alice/T0001")
        route_once(hc, TEAM_A)

        inbox = read_inbox(hc, TEAM_A, "sarah")
        assert len(inbox) == 1
        assert "REVIEW_REQUEST" in inbox[0].body

    def test_full_conversation(self, hc):
        """Multi-step conversation: boss → manager → worker → QA → manager → boss."""
        # 1. Boss kicks off
        send(hc, TEAM_A, DIRECTOR, "edison", "Start task T0001")
        route_once(hc, TEAM_A, boss_name=DIRECTOR)

        # 2. Manager delegates to alice
        send(hc, TEAM_A, "edison", "alice", "Alice, work on T0001 please")
        route_once(hc, TEAM_A)

        # 3. Alice does work, sends review request to QA
        send(hc, TEAM_A, "alice", "sarah", "REVIEW_REQUEST: T0001")
        route_once(hc, TEAM_A)

        # 4. QA approves
        send(hc, TEAM_A, "sarah", "edison", "T0001 MERGED")
        route_once(hc, TEAM_A)

        # 5. Manager reports to boss
        send(hc, TEAM_A, "edison", DIRECTOR, "T0001 is done!")
        route_once(hc, TEAM_A, boss_name=DIRECTOR)

        # Verify the full chain in SQLite
        all_msgs = get_messages(hc, msg_type="chat")
        assert len(all_msgs) == 5

        senders = [m["sender"] for m in all_msgs]
        assert senders == [DIRECTOR, "edison", "alice", "sarah", "edison"]

        recipients = [m["recipient"] for m in all_msgs]
        assert recipients == ["edison", "alice", "sarah", "edison", DIRECTOR]

        # Boss inbox has the final report
        inbox = read_inbox(hc, TEAM_A, DIRECTOR)
        assert len(inbox) == 1
        assert "done" in inbox[0].body


# ---------------------------------------------------------------------------
# E2E: cross-team boss messaging
# ---------------------------------------------------------------------------


class TestCrossTeamBoss:
    """Boss communicates with multiple teams; messages don't leak."""

    def test_boss_messages_routed_to_correct_team(self, hc):
        """Boss sends to both teams; each manager gets only their message."""
        send(hc, TEAM_A, DIRECTOR, "edison", "Alpha: start sprint 1")
        send(hc, TEAM_B, DIRECTOR, "maria", "Beta: start sprint 1")

        # Route team A
        routed_a = route_once(hc, TEAM_A, boss_name=DIRECTOR)
        assert routed_a == 1

        # Route team B
        routed_b = route_once(hc, TEAM_B, boss_name=DIRECTOR)
        assert routed_b == 1

        # Edison got the alpha message
        edison_inbox = read_inbox(hc, TEAM_A, "edison")
        assert len(edison_inbox) == 1
        assert "Alpha" in edison_inbox[0].body

        # Maria got the beta message
        maria_inbox = read_inbox(hc, TEAM_B, "maria")
        assert len(maria_inbox) == 1
        assert "Beta" in maria_inbox[0].body

    def test_boss_inbox_receives_from_both_teams(self, hc):
        """Messages from managers in different teams all arrive in one boss inbox."""
        send(hc, TEAM_A, "edison", DIRECTOR, "Alpha report")
        send(hc, TEAM_B, "maria", DIRECTOR, "Beta report")

        route_once(hc, TEAM_A, boss_name=DIRECTOR)
        route_once(hc, TEAM_B, boss_name=DIRECTOR)

        inbox = read_inbox(hc, TEAM_A, DIRECTOR)  # team arg doesn't matter for boss
        assert len(inbox) == 2
        bodies = {m.body for m in inbox}
        assert bodies == {"Alpha report", "Beta report"}

    def test_boss_queue_notifies_on_incoming(self, hc):
        """BossQueue is populated when an agent messages the boss."""
        dq = BossQueue()
        send(hc, TEAM_A, "edison", DIRECTOR, "Urgent question")
        route_once(hc, TEAM_A, boss_queue=dq, boss_name=DIRECTOR)

        msgs = dq.get_all()
        assert len(msgs) == 1
        assert msgs[0].body == "Urgent question"

    def test_no_cross_team_message_leak(self, hc):
        """A boss message to team A is NOT delivered when routing team B."""
        send(hc, TEAM_A, DIRECTOR, "edison", "Only for alpha")

        # Route team B first — should not deliver this message
        routed_b = route_once(hc, TEAM_B, boss_name=DIRECTOR)
        assert routed_b == 0

        # Route team A — now it should deliver
        routed_a = route_once(hc, TEAM_A, boss_name=DIRECTOR)
        assert routed_a == 1

        edison_inbox = read_inbox(hc, TEAM_A, "edison")
        assert len(edison_inbox) == 1


# ---------------------------------------------------------------------------
# E2E: orchestrator spawning
# ---------------------------------------------------------------------------


class TestOrchestration:
    """Route messages, then verify orchestrator detects agents needing spawn."""

    def test_unread_triggers_spawn_candidate(self, hc):
        """After routing, agents with unread inbox messages are spawn candidates."""
        send(hc, TEAM_A, "edison", "alice", "Work on T0001")
        route_once(hc, TEAM_A)

        candidates = get_agents_needing_spawn(hc, TEAM_A)
        assert "alice" in candidates

    def test_no_spawn_without_messages(self, hc):
        """Agents with empty inboxes are NOT spawn candidates."""
        candidates = get_agents_needing_spawn(hc, TEAM_A)
        assert candidates == []

    def test_orchestrate_with_mock_spawn(self, hc):
        """orchestrate_once calls spawn_fn for agents with unread messages."""
        send(hc, TEAM_A, "edison", "alice", "Task for alice")
        send(hc, TEAM_A, "edison", "bob", "Task for bob")
        route_once(hc, TEAM_A)

        spawned = []

        def mock_spawn(home, team, agent):
            spawned.append((team, agent))

        result = orchestrate_once(hc, TEAM_A, spawn_fn=mock_spawn)
        assert set(result) == {"alice", "bob"}
        assert set(a for _, a in spawned) == {"alice", "bob"}

    def test_full_pipeline_boss_to_spawn(self, hc):
        """Boss → manager → worker → orchestrator detects spawn need."""
        # Boss kicks off
        send(hc, TEAM_A, DIRECTOR, "edison", "Assign T0001 to alice")
        route_once(hc, TEAM_A, boss_name=DIRECTOR)

        # Manager delegates
        send(hc, TEAM_A, "edison", "alice", "Alice, please work on T0001")
        route_once(hc, TEAM_A)

        # Orchestrator should want to spawn alice (unread inbox)
        spawned = []
        orchestrate_once(hc, TEAM_A, spawn_fn=lambda h, t, a: spawned.append(a))
        assert "alice" in spawned

        # Also check events were logged
        events = get_messages(hc, msg_type="event")
        assert any("Paging Alice" in e["content"] for e in events)

    def test_concurrency_limit_respected(self, hc):
        """Orchestrator respects max_concurrent across the team."""
        # Give all 4 agents (edison, alice, bob, sarah) messages
        for agent in ["edison", "alice", "bob", "sarah"]:
            deliver(hc, TEAM_A, Message(
                sender=DIRECTOR, recipient=agent,
                time="2026-02-08T12:00:00Z", body=f"Work, {agent}!",
            ))

        spawned = []
        orchestrate_once(
            hc, TEAM_A, max_concurrent=2,
            spawn_fn=lambda h, t, a: spawned.append(a),
        )
        assert len(spawned) <= 2

    def test_running_agent_not_double_spawned(self, hc):
        """An agent with a live PID is not spawned again."""
        send(hc, TEAM_A, "edison", "alice", "More work")
        route_once(hc, TEAM_A)

        # Simulate alice already running
        state_file = agent_dir(hc, TEAM_A, "alice") / "state.yaml"
        state = yaml.safe_load(state_file.read_text()) or {}
        state["pid"] = os.getpid()  # current PID, definitely alive
        state_file.write_text(yaml.dump(state, default_flow_style=False))

        spawned = []
        orchestrate_once(hc, TEAM_A, spawn_fn=lambda h, t, a: spawned.append(a))
        assert "alice" not in spawned


# ---------------------------------------------------------------------------
# E2E: tasks + messaging integration
# ---------------------------------------------------------------------------


class TestTasksAndMessaging:
    """Tasks created globally, then assigned and tracked via messaging."""

    def test_create_assign_and_notify(self, hc):
        """Create a task, assign it, then message the assignee."""
        # Create a global task
        task = create_task(hc, title="Fix pagination bug", description="Off by one")
        task_id = task["id"]
        assert task["status"] == "open"

        # Assign to alice
        assign_task(hc, task_id, "alice")
        updated = get_task(hc, task_id)
        assert updated["assignee"] == "alice"

        # Manager notifies alice
        send(hc, TEAM_A, "edison", "alice", f"You're assigned {format_task_id(task_id)}. Start working.")
        route_once(hc, TEAM_A)

        inbox = read_inbox(hc, TEAM_A, "alice")
        assert len(inbox) == 1
        assert f"{format_task_id(task_id)}" in inbox[0].body

        # Alice works, updates status
        change_status(hc, task_id, "in_progress")
        assert get_task(hc, task_id)["status"] == "in_progress"

        # Alice sends review request
        send(hc, TEAM_A, "alice", "sarah", f"REVIEW_REQUEST: {format_task_id(task_id)}")
        route_once(hc, TEAM_A)

        qa_inbox = read_inbox(hc, TEAM_A, "sarah")
        assert len(qa_inbox) == 1
        assert "REVIEW_REQUEST" in qa_inbox[0].body

        # QA approves: review → needs_merge → merged
        change_status(hc, task_id, "review")
        assert get_task(hc, task_id)["status"] == "review"
        change_status(hc, task_id, "needs_merge")
        assert get_task(hc, task_id)["status"] == "needs_merge"
        change_status(hc, task_id, "merged")
        assert get_task(hc, task_id)["status"] == "merged"


# ---------------------------------------------------------------------------
# E2E: uniqueness enforcement
# ---------------------------------------------------------------------------


class TestUniquenessEnforcement:
    """Agent names must be globally unique across all teams."""

    def test_duplicate_name_across_teams_rejected(self, hc):
        """Cannot create a team with an agent name already used in another team."""
        with pytest.raises(ValueError, match="already used in other teams"):
            bootstrap(hc, team_name="gamma", manager="alice", agents=["dave"])
            # "alice" is already in team alpha

    def test_boss_name_as_agent_rejected(self, hc):
        """Cannot use the boss's name as an agent name."""
        with pytest.raises(ValueError, match="conflicts with.*boss"):
            bootstrap(hc, team_name="gamma", manager=DIRECTOR, agents=["dave"])

    def test_unique_names_across_teams_accepted(self, hc):
        """Teams with completely unique names are accepted."""
        bootstrap(hc, team_name="gamma", manager="frank", agents=["grace"])
        assert agent_dir(hc, "gamma", "frank").is_dir()
        assert agent_dir(hc, "gamma", "grace").is_dir()
