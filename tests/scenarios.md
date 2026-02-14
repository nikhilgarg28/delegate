Test 1: Happy path
  Create task → worktree created → agent commits → review → approve → merge → done
  Verify: main moved forward, worktree cleaned up, branch deleted, base tag deleted

Test 2: Two tasks merging sequentially
  Create T001 and T002 → both complete → approve T001 → merge succeeds
  → approve T002 → rebase onto new main → merge succeeds
  Verify: main has both sets of commits, linear history

Test 3: Merge conflict
  Create T001 → complete → while pending approval, manually commit to main 
  touching the same file → approve T001 → merge fails on rebase
  Verify: main untouched, temp branch cleaned up, task in merge_failed, 
  original task branch intact

Test 4: Dirty main
  Checkout main in user repo, make uncommitted changes
  → approve a task → merge should block
  Verify: status is merge_failed with "dirty main" message,
  user's uncommitted changes are untouched

Test 5: Concurrent agents
  Create T001 and T002 simultaneously → both assigned to different agents
  → both working at the same time in separate worktrees
  Verify: no git lock errors, no cross-contamination, branches are independent

Test 6: Daemon crash recovery
  Start two tasks → kill daemon mid-work → restart daemon
  → run git worktree prune on startup
  Verify: tasks are in a recoverable state, no orphaned worktrees blocking branches

Test 7: Cancel mid-work
  Create task → agent is working → cancel task
  Verify: agent process killed, worktree removed, branch cleaned up

Test 8: Multi-repo task
  Create task spanning two repos → agent works in both → merge
  Verify: both repos have their changes, all-or-nothing (if one fails, neither merges)
Tier 2: Workflow engine
Test 9: Happy path through all stages
  todo → in_progress → in_review → in_approval → merging → done
  Verify: each stage transition fires, assign() called correctly,
  enter()/guard() called in right order

Test 10: Guard rejection
  Agent marks done but worktree is dirty or no commits
  → transition to in_review should fail
  Verify: task stays in_progress, agent gets error message

Test 11: Review cycle
  in_review → changes requested → back to in_progress → 
  in_review again → approved
  Verify: review_attempt incremented, comments carry forward

Test 12: Max review cycles
  Bounce between in_review and in_progress 3 times
  Verify: escalates to human after max cycles

Test 13: Rejection flow
  in_approval → rejected with reason → back to in_progress →
  complete again → in_review → in_approval → approved
  Verify: rejection reason delivered to manager, full cycle works

Test 14: Error recovery
  Force an error (bad hook, infrastructure failure)
  → task enters error state → human resolves → moves to in_progress
  Verify: error state is escapable, task can complete

Test 15: Custom workflow
  Define a minimal custom workflow with 3 stages
  → run a task through it
  Verify: custom stages execute, default workflow unaffected
Tier 3: UI (Playwright)
Test 16: Page loads, sidebar shows agents and tasks
  Verify: agents listed with status, tasks listed with status

Test 17: Send message, see response
  Type message → send → Delegate responds
  Verify: message appears in chat, Delegate response appears,
  activity stream shows Delegate working

Test 18: Task lifecycle in UI
  Create task via chat → watch it move through sidebar stages
  → approve when it reaches in_approval → see it merge
  Verify: sidebar updates in real time via SSE, status transitions visible

Test 19: Side panel
  Click task ID → panel opens → click agent name → panel stacks
  → click back → previous panel → click X → all closed
  Verify: panel stack works, max depth 3, escape closes all

Test 20: "Needs you" banner
  Get a task to in_approval stage
  Verify: banner appears above chat input, count is correct,
  task ID is clickable, banner disappears after approval

Test 21: Agent activity stream
  Start a task → watch sidebar agent entry
  Verify: tool calls appear in real time under agent name,
  clicking agent opens panel with full activity log

Test 22: Shell command
  Type /shell git status → output appears inline
  Verify: output styled as code block, doesn't go to Delegate

Test 23: Merge failure UI
  Trigger a merge failure → verify banner, task status in sidebar,
  side panel shows error details and retry/send back buttons
Tier 4: Multi-team isolation
Test 24: Two teams, same user
  Create team-backend and team-frontend → create tasks in both
  Verify: switching teams shows different agents, tasks, chat history

Test 25: Cross-team task ID visibility
  Team A references a Team B task ID in chat
  Verify: task ID is clickable, opens correct task in side panel

Test 26: Branch namespace isolation
  Both teams work on same repo → different branches
  Verify: delegate/backend/alice/BE-001 and delegate/frontend/carol/FE-001
  don't collide

Test 27: Independent workflows
  Team A uses default workflow, Team B uses custom workflow
  Verify: each team's tasks follow their own workflow stages