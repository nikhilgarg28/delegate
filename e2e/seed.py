"""Seed a temporary Delegate home directory with test data.

Usage:
    python e2e/seed.py <tmp_dir>

Creates a team with agents, tasks in various states, messages, and
file attachments — everything the Playwright UI tests need.
No AI agents or LLM calls are involved.
"""

import sys
from pathlib import Path

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from delegate.config import set_boss
from delegate.bootstrap import bootstrap
from delegate.task import create_task, change_status, attach_file, update_task
from delegate.mailbox import send
from delegate.chat import log_event


BOSS = "testboss"
TEAM = "testteam"
MANAGER = "edison"
AGENTS = ["alice", "bob"]


def seed(hc_home: Path) -> None:
    """Populate hc_home with deterministic test data."""

    # --- Global config ---
    set_boss(hc_home, BOSS)

    # --- Bootstrap team ---
    bootstrap(hc_home, team_name=TEAM, manager=MANAGER, agents=AGENTS)

    # --- Create shared file for attachments ---
    shared_dir = hc_home / "teams" / TEAM / "shared" / "specs"
    shared_dir.mkdir(parents=True, exist_ok=True)
    spec_file = shared_dir / "design-brief.md"
    spec_file.write_text(
        "# Design Brief\n\n"
        "This is a test design brief for the Playwright E2E tests.\n\n"
        "## Requirements\n\n"
        "- Requirement 1\n"
        "- Requirement 2\n"
    )

    # --- Tasks in various states ---

    # T0001: todo
    t1 = create_task(
        hc_home, TEAM,
        title="Set up project scaffolding",
        assignee="alice",
        description="Create the initial project structure with all required directories.",
        priority="high",
    )

    # T0002: in_progress with attachment and description containing a file path
    t2 = create_task(
        hc_home, TEAM,
        title="Implement design system",
        assignee="bob",
        description=(
            "Build the design system based on the spec.\n\n"
            "See the brief: teams/testteam/shared/specs/design-brief.md\n\n"
            "This task depends on T0001."
        ),
        priority="medium",
    )
    change_status(hc_home, TEAM, t2["id"], "in_progress")
    attach_file(
        hc_home, TEAM, t2["id"],
        str(hc_home / "teams" / TEAM / "shared" / "specs" / "design-brief.md"),
    )

    # T0003: done (follow valid transition chain)
    t3 = create_task(
        hc_home, TEAM,
        title="Write README",
        assignee="alice",
        description="Draft the project README with setup instructions.",
        priority="low",
    )
    change_status(hc_home, TEAM, t3["id"], "in_progress")
    change_status(hc_home, TEAM, t3["id"], "in_review")
    change_status(hc_home, TEAM, t3["id"], "in_approval")
    change_status(hc_home, TEAM, t3["id"], "merging")
    change_status(hc_home, TEAM, t3["id"], "done")

    # --- Messages ---
    send(hc_home, TEAM, BOSS, MANAGER, "Please kick off the project.", task_id=t1["id"])
    send(hc_home, TEAM, MANAGER, "alice", "Alice, start with the scaffolding.", task_id=t1["id"])
    send(hc_home, TEAM, "alice", MANAGER, "On it! I'll set up the directories.", task_id=t1["id"])
    send(hc_home, TEAM, MANAGER, BOSS, "Project kicked off. Alice is on scaffolding.", task_id=t1["id"])
    send(hc_home, TEAM, BOSS, MANAGER, "Great, also check T0002 status.")

    # --- System events ---
    log_event(hc_home, TEAM, "Alice started working on T0001", task_id=t1["id"])
    log_event(hc_home, TEAM, "Bob assigned to T0002", task_id=t2["id"])

    print(f"Seeded {hc_home} with team '{TEAM}', 3 tasks, 5 messages, 2 events")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <tmp_dir>", file=sys.stderr)
        sys.exit(1)
    seed(Path(sys.argv[1]))
