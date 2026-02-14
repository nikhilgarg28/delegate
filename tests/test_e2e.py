"""End-to-end tests — message passing, routing, and runtime dispatch without the UI.

These tests exercise the full pipeline:
    bootstrap → send → route → deliver → dispatch (via agents_with_unread)
using real file I/O and the SQLite database, with no mocking.
"""

import pytest
import yaml

from delegate.bootstrap import bootstrap
from delegate.config import set_boss, get_boss
from delegate.mailbox import send, read_inbox, read_outbox, deliver, Message, agents_with_unread
from delegate.router import route_once, BossQueue
from delegate.chat import get_messages, log_event
from delegate.task import create_task, get_task, change_status, assign_task, format_task_id
from delegate.paths import (
    boss_person_dir,
    agent_dir,
    agents_dir,
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
    bootstrap(home, team_name=TEAM_A, manager="edison", agents=["alice", "bob", ("sarah", "qa")])

    # Team beta: manager + 1 worker
    bootstrap(home, team_name=TEAM_B, manager="maria", agents=["charlie"])

    return home


# ---------------------------------------------------------------------------
# Sanity: directory structure
# ---------------------------------------------------------------------------


class TestBossyStructure:
    """Verify the bootstrapped directory layout."""

    def test_boss_dir_is_global(self, hc):
        """Boss directory lives at hc/boss/, outside any team."""
        dd = boss_person_dir(hc)
        assert dd.is_dir()
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
        """Boss sends a message; it is delivered immediately."""
        send(hc, TEAM_A, DIRECTOR, "edison", "Please start the sprint.")

        # Manager received it immediately (no router needed)
        inbox = read_inbox(hc, TEAM_A, "edison", unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == DIRECTOR
        assert inbox[0].body == "Please start the sprint."

        # Already delivered — nothing pending
        assert read_outbox(hc, TEAM_A, DIRECTOR, pending_only=True) == []

        # Logged in SQLite
        msgs = get_messages(hc, TEAM_A, msg_type="chat")
        assert len(msgs) == 1
        assert msgs[0]["sender"] == DIRECTOR

    def test_manager_to_boss(self, hc):
        """Manager replies to boss; delivered immediately."""
        send(hc, TEAM_A, "edison", DIRECTOR, "Sprint started, 3 tasks assigned.")

        inbox = read_inbox(hc, TEAM_A, DIRECTOR, unread_only=True)
        assert len(inbox) == 1
        assert inbox[0].sender == "edison"

    def test_manager_to_worker(self, hc):
        """Manager assigns work to a team member."""
        send(hc, TEAM_A, "edison", "alice", "Please work on T0001.")

        inbox = read_inbox(hc, TEAM_A, "alice")
        assert len(inbox) == 1
        assert "T0001" in inbox[0].body

    def test_worker_to_qa(self, hc):
        """Worker sends a review request to QA."""
        send(hc, TEAM_A, "alice", "sarah", "REVIEW_REQUEST: repo=myapp branch=alice/T0001")

        inbox = read_inbox(hc, TEAM_A, "sarah")
        assert len(inbox) == 1
        assert "REVIEW_REQUEST" in inbox[0].body

    def test_full_conversation(self, hc):
        """Multi-step conversation: boss → manager → worker → QA → manager → boss."""
        send(hc, TEAM_A, DIRECTOR, "edison", "Start task T0001")
        send(hc, TEAM_A, "edison", "alice", "Alice, work on T0001 please")
        send(hc, TEAM_A, "alice", "sarah", "REVIEW_REQUEST: T0001")
        send(hc, TEAM_A, "sarah", "edison", "T0001 MERGED")
        send(hc, TEAM_A, "edison", DIRECTOR, "T0001 is done!")

        # Verify the full chain in SQLite
        all_msgs = get_messages(hc, TEAM_A, msg_type="chat")
        assert len(all_msgs) == 5

        senders = [m["sender"] for m in all_msgs]
        assert senders == [DIRECTOR, "edison", "alice", "sarah", "edison"]

        recipients = [m["recipient"] for m in all_msgs]
        assert recipients == ["edison", "alice", "sarah", "edison", DIRECTOR]

        # Boss inbox has the final report
        inbox = read_inbox(hc, TEAM_A, DIRECTOR)
        # Boss gets messages from edison (2) — the first and the last
        boss_msgs = [m for m in inbox if m.recipient == DIRECTOR]
        assert any("done" in m.body for m in boss_msgs)


# ---------------------------------------------------------------------------
# E2E: cross-team boss messaging
# ---------------------------------------------------------------------------


class TestCrossTeamBoss:
    """Boss communicates with multiple teams; messages delivered immediately."""

    def test_boss_messages_delivered_to_correct_recipients(self, hc):
        """Boss sends to both teams; each manager gets only their message."""
        send(hc, TEAM_A, DIRECTOR, "edison", "Alpha: start sprint 1")
        send(hc, TEAM_B, DIRECTOR, "maria", "Beta: start sprint 1")

        # Edison got the alpha message
        edison_inbox = read_inbox(hc, TEAM_A, "edison")
        assert len(edison_inbox) == 1
        assert "Alpha" in edison_inbox[0].body

        # Maria got the beta message
        maria_inbox = read_inbox(hc, TEAM_B, "maria")
        assert len(maria_inbox) == 1
        assert "Beta" in maria_inbox[0].body

    def test_boss_inbox_receives_per_team(self, hc):
        """Messages from managers land in the boss's per-team inbox."""
        send(hc, TEAM_A, "edison", DIRECTOR, "Alpha report")
        send(hc, TEAM_B, "maria", DIRECTOR, "Beta report")

        alpha_inbox = read_inbox(hc, TEAM_A, DIRECTOR)
        assert len(alpha_inbox) == 1
        assert alpha_inbox[0].body == "Alpha report"

        beta_inbox = read_inbox(hc, TEAM_B, DIRECTOR)
        assert len(beta_inbox) == 1
        assert beta_inbox[0].body == "Beta report"

    def test_boss_queue_notifies_on_incoming(self, hc):
        """BossQueue is populated when route_once finds boss messages."""
        dq = BossQueue()
        send(hc, TEAM_A, "edison", DIRECTOR, "Urgent question")
        route_once(hc, TEAM_A, boss_queue=dq, boss_name=DIRECTOR)

        msgs = dq.get_all()
        assert len(msgs) == 1
        assert msgs[0].body == "Urgent question"


# ---------------------------------------------------------------------------
# E2E: runtime dispatch (agents_with_unread replaces orchestrator)
# ---------------------------------------------------------------------------


class TestRuntimeDispatch:
    """Route messages, then verify agents_with_unread detects agents needing turns."""

    def test_unread_triggers_dispatch(self, hc):
        """After routing, agents with unread inbox messages are detected."""
        send(hc, TEAM_A, "edison", "alice", "Work on T0001")
        route_once(hc, TEAM_A)

        needy = agents_with_unread(hc, TEAM_A)
        assert "alice" in needy

    def test_no_dispatch_without_messages(self, hc):
        """Agents with empty inboxes are NOT flagged for dispatch."""
        needy = agents_with_unread(hc, TEAM_A)
        assert needy == []

    def test_multiple_agents_detected(self, hc):
        """agents_with_unread detects all agents with unread messages."""
        send(hc, TEAM_A, "edison", "alice", "Task for alice")
        send(hc, TEAM_A, "edison", "bob", "Task for bob")
        route_once(hc, TEAM_A)

        needy = agents_with_unread(hc, TEAM_A)
        assert set(needy) >= {"alice", "bob"}

    def test_full_pipeline_boss_to_dispatch(self, hc):
        """Boss → manager → worker → runtime detects unread."""
        # Boss kicks off
        send(hc, TEAM_A, DIRECTOR, "edison", "Assign T0001 to alice")
        route_once(hc, TEAM_A, boss_name=DIRECTOR)

        # Manager delegates
        send(hc, TEAM_A, "edison", "alice", "Alice, please work on T0001")
        route_once(hc, TEAM_A)

        # Runtime should detect alice (unread inbox)
        needy = agents_with_unread(hc, TEAM_A)
        assert "alice" in needy


# ---------------------------------------------------------------------------
# E2E: tasks + messaging integration
# ---------------------------------------------------------------------------


class TestTasksAndMessaging:
    """Tasks created globally, then assigned and tracked via messaging."""

    def test_create_assign_and_notify(self, hc):
        """Create a task, assign it, then message the assignee."""
        # Create a team-scoped task
        task = create_task(hc, TEAM_A, title="Fix pagination bug", assignee="manager", description="Off by one")
        task_id = task["id"]
        assert task["status"] == "todo"

        # Assign to alice
        assign_task(hc, TEAM_A, task_id, "alice")
        updated = get_task(hc, TEAM_A, task_id)
        assert updated["assignee"] == "alice"

        # Manager notifies alice
        send(hc, TEAM_A, "edison", "alice", f"You're assigned {format_task_id(task_id)}. Start working.")
        route_once(hc, TEAM_A)

        inbox = read_inbox(hc, TEAM_A, "alice")
        assert len(inbox) == 1
        assert f"{format_task_id(task_id)}" in inbox[0].body

        # Alice works, updates status
        change_status(hc, TEAM_A, task_id, "in_progress")
        assert get_task(hc, TEAM_A, task_id)["status"] == "in_progress"

        # Alice sends review request
        send(hc, TEAM_A, "alice", "sarah", f"REVIEW_REQUEST: {format_task_id(task_id)}")
        route_once(hc, TEAM_A)

        qa_inbox = read_inbox(hc, TEAM_A, "sarah")
        assert len(qa_inbox) == 1
        assert "REVIEW_REQUEST" in qa_inbox[0].body

        # Reviewer approves: in_review → in_approval → merging → done
        change_status(hc, TEAM_A, task_id, "in_review")
        assert get_task(hc, TEAM_A, task_id)["status"] == "in_review"
        change_status(hc, TEAM_A, task_id, "in_approval")
        assert get_task(hc, TEAM_A, task_id)["status"] == "in_approval"
        change_status(hc, TEAM_A, task_id, "merging")
        assert get_task(hc, TEAM_A, task_id)["status"] == "merging"
        change_status(hc, TEAM_A, task_id, "done")
        assert get_task(hc, TEAM_A, task_id)["status"] == "done"


# ---------------------------------------------------------------------------
# E2E: uniqueness enforcement
# ---------------------------------------------------------------------------


class TestUniquenessEnforcement:
    """Agent names must be unique within a team but may be reused across teams."""

    def test_duplicate_name_within_team_rejected(self, hc):
        """Cannot create a team with duplicate agent names."""
        with pytest.raises(ValueError, match="Duplicate names"):
            bootstrap(hc, team_name="gamma", manager="alice", agents=["alice"])

    def test_boss_name_as_agent_rejected(self, hc):
        """Cannot use the human member's name as an agent name."""
        with pytest.raises(ValueError, match="conflicts with.*human member"):
            bootstrap(hc, team_name="gamma", manager=DIRECTOR, agents=["dave"])

    def test_same_name_across_teams_rejected(self, hc):
        """Agent names must be globally unique — reuse across teams is rejected."""
        # "alice" is already in team alpha — should fail in a new team
        with pytest.raises(ValueError, match='Agent name "alice" already exists on team "alpha". Names must be globally unique.'):
            bootstrap(hc, team_name="gamma", manager="alice", agents=["bob"])

    def test_unique_names_across_teams_accepted(self, hc):
        """Teams with completely unique names are accepted."""
        bootstrap(hc, team_name="gamma", manager="frank", agents=["grace"])
        assert agent_dir(hc, "gamma", "frank").is_dir()
        assert agent_dir(hc, "gamma", "grace").is_dir()
