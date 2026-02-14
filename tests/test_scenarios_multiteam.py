"""Multi-team isolation scenarios (Tests 24-27 from scenarios.md).

Backend-only tests verifying that tasks, messages, agents, and branches
are properly scoped per team with no cross-contamination.

All tests run WITHOUT spinning up LLM agents or the SDK.
"""

import pytest
import subprocess
from pathlib import Path

from delegate.bootstrap import bootstrap
from delegate.config import add_member
from delegate.task import create_task, get_task, list_tasks
from delegate.mailbox import send, read_inbox
from delegate.repo import register_repo, create_task_worktree
from delegate.paths import get_team_id


TEAM_A = "backend"
TEAM_B = "frontend"


@pytest.fixture
def hc_two_teams(tmp_path):
    """Bootstrap two teams under the same hc_home."""
    hc = tmp_path / "hc_home"
    hc.mkdir()
    add_member(hc, "nikhil")
    bootstrap(hc, TEAM_A, manager="mgr-a", agents=["alice", "bob"])
    bootstrap(hc, TEAM_B, manager="mgr-b", agents=["carol", "dave"])
    return hc


@pytest.fixture
def shared_repo(tmp_path):
    """Create a git repo that can be registered by both teams."""
    repo_dir = tmp_path / "shared_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)

    # Create initial commit on main branch
    (repo_dir / "README.md").write_text("# Shared Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    return repo_dir


class TestMultiTeamIsolation:
    """Test 24: Two teams, same user (backend)."""

    def test_tasks_scoped_to_team(self, hc_two_teams):
        """Tasks are scoped to their team — list_tasks only shows tasks for the queried team.

        Scenario 24 from scenarios.md (backend only).
        """
        hc = hc_two_teams

        # Create tasks in both teams
        task_a1 = create_task(hc, TEAM_A, title="Backend task 1", assignee="alice")
        task_a2 = create_task(hc, TEAM_A, title="Backend task 2", assignee="bob")
        task_b1 = create_task(hc, TEAM_B, title="Frontend task 1", assignee="carol")
        task_b2 = create_task(hc, TEAM_B, title="Frontend task 2", assignee="dave")

        # Note: Task IDs are globally unique across all teams (global AUTOINCREMENT)
        # but tasks are filtered by team column
        assert task_a1["team"] == TEAM_A
        assert task_b1["team"] == TEAM_B

        # Verify list_tasks for team-backend only shows backend tasks
        backend_tasks = list_tasks(hc, TEAM_A)
        backend_titles = [t["title"] for t in backend_tasks]
        assert "Backend task 1" in backend_titles
        assert "Backend task 2" in backend_titles
        assert "Frontend task 1" not in backend_titles
        assert "Frontend task 2" not in backend_titles
        assert len(backend_tasks) == 2

        # Verify list_tasks for team-frontend only shows frontend tasks
        frontend_tasks = list_tasks(hc, TEAM_B)
        frontend_titles = [t["title"] for t in frontend_tasks]
        assert "Frontend task 1" in frontend_titles
        assert "Frontend task 2" in frontend_titles
        assert "Backend task 1" not in frontend_titles
        assert "Backend task 2" not in frontend_titles
        assert len(frontend_tasks) == 2

    def test_agents_scoped_to_team(self, hc_two_teams):
        """Agents are scoped to their team — agents from one team don't appear in another.

        Scenario 24 from scenarios.md (backend only).
        """
        hc = hc_two_teams

        # Create tasks with team-specific assignees
        create_task(hc, TEAM_A, title="Task for Alice", assignee="alice")
        create_task(hc, TEAM_B, title="Task for Carol", assignee="carol")

        # Verify we can retrieve tasks by team-specific agents
        backend_alice_tasks = list_tasks(hc, TEAM_A, assignee="alice")
        assert len(backend_alice_tasks) == 1
        assert backend_alice_tasks[0]["title"] == "Task for Alice"

        frontend_carol_tasks = list_tasks(hc, TEAM_B, assignee="carol")
        assert len(frontend_carol_tasks) == 1
        assert frontend_carol_tasks[0]["title"] == "Task for Carol"

        # Verify cross-team agent queries return empty
        backend_carol_tasks = list_tasks(hc, TEAM_A, assignee="carol")
        assert len(backend_carol_tasks) == 0

        frontend_alice_tasks = list_tasks(hc, TEAM_B, assignee="alice")
        assert len(frontend_alice_tasks) == 0

    def test_messages_scoped_to_team(self, hc_two_teams):
        """Messages are scoped to their team — messages in one team don't appear in another.

        Scenario 24 from scenarios.md (backend only).
        """
        hc = hc_two_teams

        # Send messages in both teams
        send(hc, TEAM_A, "mgr-a", "alice", "Backend message for Alice")
        send(hc, TEAM_B, "mgr-b", "carol", "Frontend message for Carol")

        # Verify alice's inbox in team-backend shows only backend messages
        alice_inbox = read_inbox(hc, TEAM_A, "alice", unread_only=True)
        assert len(alice_inbox) == 1
        assert alice_inbox[0].body == "Backend message for Alice"
        assert alice_inbox[0].sender == "mgr-a"

        # Verify carol's inbox in team-frontend shows only frontend messages
        carol_inbox = read_inbox(hc, TEAM_B, "carol", unread_only=True)
        assert len(carol_inbox) == 1
        assert carol_inbox[0].body == "Frontend message for Carol"
        assert carol_inbox[0].sender == "mgr-b"

        # Verify cross-team message isolation: alice in team-frontend has no messages
        # (alice doesn't exist in team-frontend, but mailbox should return empty)
        # We can't call read_inbox for non-existent agents, so we verify by checking
        # that sending to the wrong team's inbox doesn't cross-contaminate
        send(hc, TEAM_A, "mgr-a", "bob", "Another backend message")
        bob_inbox = read_inbox(hc, TEAM_A, "bob", unread_only=True)
        assert len(bob_inbox) == 1

        # carol's inbox should still have only 1 message
        carol_inbox_after = read_inbox(hc, TEAM_B, "carol", unread_only=True)
        assert len(carol_inbox_after) == 1


class TestCrossTeamTaskReferences:
    """Test 25: Cross-team task references (backend)."""

    def test_task_ids_independent_per_team(self, hc_two_teams):
        """Task lookups are scoped by team — get_task(team, id) only returns tasks for that team.

        Scenario 25 from scenarios.md (backend only).

        Note: Task IDs use a global AUTOINCREMENT sequence, but get_task() filters by team.
        """
        hc = hc_two_teams

        # Create tasks in both teams
        task_a1 = create_task(hc, TEAM_A, title="Backend T0001", assignee="alice")
        task_a2 = create_task(hc, TEAM_A, title="Backend T0002", assignee="bob")
        task_b1 = create_task(hc, TEAM_B, title="Frontend T0001", assignee="carol")
        task_b2 = create_task(hc, TEAM_B, title="Frontend T0002", assignee="dave")

        # Task IDs are globally unique (global AUTOINCREMENT)
        # Task A1 and A2 are created first, so they get IDs 1 and 2
        # Task B1 and B2 are created next, so they get IDs 3 and 4
        assert task_a1["id"] == 1
        assert task_a2["id"] == 2
        assert task_b1["id"] == 3
        assert task_b2["id"] == 4

        # Verify tasks have correct team assignments
        assert task_a1["team"] == TEAM_A
        assert task_a2["team"] == TEAM_A
        assert task_b1["team"] == TEAM_B
        assert task_b2["team"] == TEAM_B

        # Verify get_task with team scope returns correct task
        backend_task_1 = get_task(hc, TEAM_A, 1)
        assert backend_task_1["title"] == "Backend T0001"
        assert backend_task_1["assignee"] == "alice"
        assert backend_task_1["team"] == TEAM_A

        # Can retrieve task 3 (first task in team B) with team scope
        frontend_task_3 = get_task(hc, TEAM_B, 3)
        assert frontend_task_3["title"] == "Frontend T0001"
        assert frontend_task_3["assignee"] == "carol"
        assert frontend_task_3["team"] == TEAM_B

    def test_no_cross_team_task_lookup(self, hc_two_teams):
        """Tasks are isolated — get_task() filters by team, preventing cross-team access.

        Scenario 25 from scenarios.md (backend only).
        """
        hc = hc_two_teams

        # Create tasks
        task_a = create_task(hc, TEAM_A, title="Backend task", assignee="alice")
        task_b = create_task(hc, TEAM_B, title="Frontend task", assignee="carol")

        # Task A is ID 1, task B is ID 2 (global sequence)
        assert task_a["id"] == 1
        assert task_b["id"] == 2

        # get_task filters by team — can only retrieve tasks for the specified team
        backend_1 = get_task(hc, TEAM_A, 1)
        assert backend_1["title"] == "Backend task"
        assert backend_1["team"] == TEAM_A

        # Trying to get task 1 from team B fails (it doesn't exist in that team)
        try:
            get_task(hc, TEAM_B, 1)
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError as e:
            assert "not found in team frontend" in str(e)

        # But we can get task 2 from team B
        frontend_2 = get_task(hc, TEAM_B, 2)
        assert frontend_2["title"] == "Frontend task"
        assert frontend_2["team"] == TEAM_B


class TestBranchNamespaceIsolation:
    """Test 26: Branch namespace isolation."""

    def test_branch_names_include_team_id(self, hc_two_teams, shared_repo):
        """Branch names include team_id to prevent collisions when teams share a repo.

        Scenario 26 from scenarios.md (backend only).
        """
        hc = hc_two_teams

        # Register the same repo in both teams
        register_repo(hc, TEAM_A, str(shared_repo), name="shared")
        register_repo(hc, TEAM_B, str(shared_repo), name="shared")

        # Create tasks in both teams
        task_a1 = create_task(hc, TEAM_A, title="Backend feature", assignee="alice", repo="shared")
        task_b1 = create_task(hc, TEAM_B, title="Frontend feature", assignee="carol", repo="shared")

        # Create worktrees for both tasks
        wt_a = create_task_worktree(hc, TEAM_A, "shared", task_a1["id"])
        wt_b = create_task_worktree(hc, TEAM_B, "shared", task_b1["id"])

        # Verify worktrees were created
        assert wt_a.exists()
        assert wt_b.exists()

        # Get team IDs
        team_a_id = get_team_id(hc, TEAM_A)
        team_b_id = get_team_id(hc, TEAM_B)

        # Verify team IDs are different (unique 6-char hex)
        assert team_a_id != team_b_id
        assert len(team_a_id) == 6
        assert len(team_b_id) == 6

        # Verify branch names include team_id: delegate/<team_id>/<team>/T<NNNN>
        # Check git branches in the repo
        result = subprocess.run(
            ["git", "branch", "--list"],
            cwd=shared_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        branches = result.stdout

        # Expected branch names - use actual task IDs (global sequence)
        # task_a1 is ID 1, task_b1 is ID 2
        expected_a = f"delegate/{team_a_id}/{TEAM_A}/T{task_a1['id']:04d}"
        expected_b = f"delegate/{team_b_id}/{TEAM_B}/T{task_b1['id']:04d}"

        assert expected_a in branches, f"Expected branch {expected_a} not found in:\n{branches}"
        assert expected_b in branches, f"Expected branch {expected_b} not found in:\n{branches}"

        # Verify no collision — both branches exist simultaneously
        assert expected_a != expected_b

    def test_no_branch_collision_same_task_number(self, hc_two_teams, shared_repo):
        """Teams working on the same repo get isolated branches with team_id prefix.

        Scenario 26 from scenarios.md (backend only).
        """
        hc = hc_two_teams

        # Register the same repo in both teams
        register_repo(hc, TEAM_A, str(shared_repo), name="myrepo")
        register_repo(hc, TEAM_B, str(shared_repo), name="myrepo")

        # Create tasks in both teams
        task_a = create_task(hc, TEAM_A, title="A: Feature X", assignee="alice", repo="myrepo")
        task_b = create_task(hc, TEAM_B, title="B: Feature Y", assignee="carol", repo="myrepo")

        # Task IDs are globally unique (1 and 2), not per-team
        assert task_a["id"] == 1
        assert task_b["id"] == 2

        # Create worktrees — should not collide
        wt_a = create_task_worktree(hc, TEAM_A, "myrepo", task_a["id"])
        wt_b = create_task_worktree(hc, TEAM_B, "myrepo", task_b["id"])

        # Both worktrees exist
        assert wt_a.exists()
        assert wt_b.exists()

        # Verify the worktree paths are different (scoped by team)
        assert wt_a != wt_b
        assert f"/{TEAM_A}/worktrees/" in str(wt_a)
        assert f"/{TEAM_B}/worktrees/" in str(wt_b)

        # Verify branches have team_id prefix for collision avoidance
        result = subprocess.run(
            ["git", "branch", "--list"],
            cwd=shared_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        branches = result.stdout

        team_a_id = get_team_id(hc, TEAM_A)
        team_b_id = get_team_id(hc, TEAM_B)

        # Both branches exist and have different team_id prefixes
        assert f"delegate/{team_a_id}/{TEAM_A}/T0001" in branches
        assert f"delegate/{team_b_id}/{TEAM_B}/T0002" in branches


class TestIndependentWorkflows:
    """Test 27: Independent workflows."""

    def test_teams_use_default_workflow_independently(self, hc_two_teams):
        """Both teams use the default workflow correctly in isolation.

        Scenario 27 from scenarios.md (backend only).

        Note: Per-team custom workflows are not yet implemented. This test
        verifies that both teams operate with the default workflow independently.
        """
        hc = hc_two_teams

        # Create tasks in both teams
        task_a = create_task(hc, TEAM_A, title="Backend workflow test", assignee="alice")
        task_b = create_task(hc, TEAM_B, title="Frontend workflow test", assignee="carol")

        # Verify tasks start with default workflow
        task_a_data = get_task(hc, TEAM_A, task_a["id"])
        task_b_data = get_task(hc, TEAM_B, task_b["id"])

        # Both use default workflow
        assert task_a_data.get("workflow") == "default"
        assert task_b_data.get("workflow") == "default"

        # Both start in 'todo' status
        assert task_a_data["status"] == "todo"
        assert task_b_data["status"] == "todo"

        # Workflows operate independently — changing status in one team doesn't affect the other
        from delegate.task import change_status

        change_status(hc, TEAM_A, task_a["id"], "in_progress")

        # Verify team A task changed
        task_a_updated = get_task(hc, TEAM_A, task_a["id"])
        assert task_a_updated["status"] == "in_progress"

        # Verify team B task unchanged
        task_b_unchanged = get_task(hc, TEAM_B, task_b["id"])
        assert task_b_unchanged["status"] == "todo"

    def test_workflow_isolation_across_teams(self, hc_two_teams):
        """Workflow state changes in one team don't affect the other team.

        Scenario 27 from scenarios.md (backend only).
        """
        hc = hc_two_teams

        # Create multiple tasks in each team
        tasks_a = [
            create_task(hc, TEAM_A, title=f"Backend {i}", assignee="alice")
            for i in range(3)
        ]
        tasks_b = [
            create_task(hc, TEAM_B, title=f"Frontend {i}", assignee="carol")
            for i in range(3)
        ]

        from delegate.task import change_status

        # Move all team A tasks through workflow stages
        for t in tasks_a:
            change_status(hc, TEAM_A, t["id"], "in_progress")

        # Verify team A tasks changed
        for t in tasks_a:
            task_data = get_task(hc, TEAM_A, t["id"])
            assert task_data["status"] == "in_progress"

        # Verify team B tasks remain in todo
        for t in tasks_b:
            task_data = get_task(hc, TEAM_B, t["id"])
            assert task_data["status"] == "todo"
