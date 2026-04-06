# Delegate Workflow System

## Overview

Delegate is a multi-agent coding tool where a manager agent (Delegate) coordinates a team of coding agents and humans through structured workflows. The workflow system provides the backbone: stages define what happens, transitions define the order, and the daemon orchestrates execution while maintaining the illusion that Delegate is personally managing everything.

## Core Concepts

### Workflow

A workflow is a directed graph of stages connected by transitions. It is defined as a Python object.

```python
engineering = Workflow(
    name="engineering",
    version="1",
    description="Standard development workflow. Agent codes, human reviews, merge.",
    use_when="Single well-defined task: bug fix, feature, refactor.",
    stages=[Coding, Review, Revise, Merging, Done],
    transitions={
        (Coding, "done"):                   Review,
        (Coding, "blocked"):                Coding,      # re-enters after resolution
        (Review, "approved"):               Merging,
        (Review, "approved_with_edits"):    Merging,
        (Review, "changes_requested"):      Revise,
        (Revise, "done"):                   Review,
        (Merging, "done"):                  Done,
        (Merging, "conflict"):              Review,
    },
    initial=Coding,
)
```

Validation runs at import time: all stages reachable from initial, all declared outcomes have transitions, non-terminal stages have outgoing transitions.

### Stages

A stage is a Python class. There are four types:

```python
class Stage:
    outcomes: list[str] = []        # class variable, declared statically

    async def guard(self, ctx) -> GuardResult | bool:
        """Precondition check. Return False to block entry (task waits and retries)."""
        return True

    async def assign(self, ctx) -> str | DelegatePicks | None:
        """Who should work on this. Direct member ID or DelegatePicks for Delegate to choose."""
        return None

    async def enter(self, ctx) -> None:
        """Side effects when entering stage (notifications, setup)."""
        pass

    async def exit(self, ctx) -> None:
        """Side effects when leaving stage (cleanup)."""
        pass

    def instructions(self, ctx) -> str:
        """For agent stages: what the agent should focus on in this stage."""
        return ""
```

**AgentStage** — an AI agent does the work. The agent gets a session with coding tools (Read, Edit, Write, Bash) plus a `finish` tool to signal an outcome. The agent also always has access to `"blocked"` as an implicit outcome.

```python
class Coding(AgentStage):
    outcomes = ["done"]

    async def assign(self, ctx):
        return DelegatePicks(
            candidates=ctx.team.agents,
            instruction="Pick agent most familiar with this area.",
        )

    def instructions(self, ctx):
        base = "Implement the changes described in the task brief."
        if ctx.review_comments:
            base += "\n\nPrevious review feedback:\n"
            for c in ctx.review_comments:
                base += f"- {c.by}: {c.text}\n"
        return base
```

**HumanStage** — a human does something and picks an outcome. The `action()` method returns a `HumanAction` describing what the UI should show.

```python
class Review(HumanStage):
    outcomes = ["approved", "approved_with_edits", "changes_requested"]

    async def assign(self, ctx):
        return DelegatePicks(
            candidates=ctx.team.humans,
            exclude=[ctx.task.last_assignee_for("Coding")],
            instruction="Pick a reviewer.",
        )

    async def action(self, ctx) -> HumanAction:
        return HumanAction(
            viewer=DiffViewer(files=ctx.task.changed_files),
            editable=True,
            comment_enabled=True,
        )
```

**AutoStage** — runs code immediately, returns an outcome. No human or agent involvement.

```python
class Merging(AutoStage):
    outcomes = ["done", "conflict"]

    async def action(self, ctx) -> str:
        results = {}
        for repo in ctx.task.repos:
            if not await ctx.git.has_changes(repo, ctx.task.branch):
                continue
            result = await ctx.git.merge(repo=repo, branch=ctx.task.branch, into="main")
            results[repo] = result
        conflicts = [r for r, res in results.items() if res.conflict]
        return "conflict" if conflicts else "done"
```

**TerminalStage** — end state. No outcomes, no transitions out.

```python
class Done(TerminalStage):
    async def enter(self, ctx):
        if "slack" in ctx.capabilities:
            await ctx.slack.post(f"✅ {ctx.task.title} merged")
```

### Stage Lifecycle

When a task enters a stage, the engine runs this sequence:

```
guard() → assign() → enter() → [dispatch based on stage type] → exit()
```

1. **guard()** — precondition check. Returns `True` (proceed), `False` (block, task waits), or `GuardResult(False, reason="...")`. The engine retries periodically when `retry_after` is set on the GuardResult.
2. **assign()** — returns a direct member ID (engine uses it) or `DelegatePicks` (Delegate chooses from candidates). Returns `None` if no assignment needed.
3. **enter()** — side effects: send notifications, set up data, log.
4. **dispatch** — depends on stage type:
   - `AgentStage`: daemon spawns an agent session
   - `HumanStage`: daemon notifies human, waits for response
   - `AutoStage`: engine calls `action()`, uses return value as outcome, transitions immediately
   - `TerminalStage`: task is complete
5. **exit()** — cleanup when leaving (runs before the next stage's guard).

### Outcomes

Each stage declares its outcomes as a class-level list of strings. These are the possible results of that stage. Every outcome must have a corresponding transition in the workflow (enforced at validation time).

For AgentStages, the agent signals its outcome by calling the `finish` tool with a `result` parameter constrained to the stage's outcome enum plus `"blocked"` (always available). The `finish` tool is the only way for an agent to end its work on a stage.

For HumanStages, the outcome comes from the UI — the human picks one of the available outcomes (e.g. clicks "Approve" or "Request Changes").

For AutoStages, the outcome is the return value of `action()`.

### Guard

Guard returning `False` means "not yet" — the task stays in its current stage and retries later. Common uses:

```python
# Dependency gating
async def guard(self, ctx):
    for dep_id in ctx.task.data.get("depends_on", []):
        dep = await ctx.store.load(dep_id)
        if dep.status != "done":
            return GuardResult(False, reason=f"Waiting for {dep.id}")
    return True

# Deploy freeze
async def guard(self, ctx):
    if ctx.team.config.get("deploy_freeze"):
        return GuardResult(False, reason="Deploy freeze active")
    return True

# CI status
async def guard(self, ctx):
    ci = await ctx.git.get_ci_status(ctx.task.branch)
    if ci.status != "passing":
        return GuardResult(False, reason=f"CI not passing: {ci.summary}")
    return True
```

### Human Actions

HumanStage's `action()` returns a `HumanAction` that describes what the UI should render. It composes from a small set of primitives:

```python
@dataclass
class HumanAction:
    viewer: Viewer              # what to show
    editable: bool = False      # can the human edit inline?
    checklist: list[str] | None = None
    comment_enabled: bool = True

# Viewer types
Viewer = DiffViewer | SpecViewer | InstructionViewer | CommandViewer | StatusViewer

@dataclass
class DiffViewer:
    files: list[str]

@dataclass
class SpecViewer:
    spec: dict

@dataclass
class InstructionViewer:
    title: str
    body: str
    url: str | None = None

@dataclass
class CommandViewer:
    command: str
    cwd: str
    description: str

@dataclass
class StatusViewer:
    service: str
    dashboard_url: str
```

Convenience constructors for common patterns:

```python
def ReviewCode(files):
    return HumanAction(viewer=DiffViewer(files=files), editable=True, comment_enabled=True)

def ManualTest(instructions, url=None, checklist=None):
    return HumanAction(viewer=InstructionViewer(title="Manual Testing", body=instructions, url=url), checklist=checklist)

def Approve(description):
    return HumanAction(viewer=InstructionViewer(title="Approval Required", body=description))

def ReviewSpec(spec):
    return HumanAction(viewer=SpecViewer(spec=spec), editable=True, comment_enabled=True)
```

The `approved_with_edits` outcome: when a HumanAction has `editable=True`, the UI allows the human to make changes before approving. If they edit, the outcome is `approved_with_edits` instead of `approved`. The engine writes their edits to the worktree, commits with their attribution, and records a `HumanEdit` event in task history. Downstream stages and agents see these edits in context and are instructed not to revert them.

### Webhook Stages

For external event-driven stages (CI callbacks, client approvals, third-party integrations):

```python
class WebhookStage(Stage):
    timeout: timedelta | None = None

    def webhook_filter(self, ctx) -> dict:
        """What events to listen for. E.g. {"source": "github", "event": "check_suite"}."""
        raise NotImplementedError

    async def on_webhook(self, payload: dict, ctx) -> str | None:
        """Process webhook payload. Return outcome to transition, or None to keep waiting."""
        raise NotImplementedError

    async def on_timeout(self, ctx) -> str:
        """Called if timeout expires before a webhook resolves the stage."""
        return "timeout"
```

When a task enters a WebhookStage, the engine registers a listener with the WebhookRouter based on `webhook_filter()`. Incoming webhooks are matched against active listeners and dispatched. Returning `None` from `on_webhook` means "received but not done yet" — the stage keeps waiting.

```python
class CIComplete(WebhookStage):
    outcomes = ["pass", "fail", "timeout"]
    timeout = timedelta(minutes=30)

    def webhook_filter(self, ctx):
        return {"source": "github", "event": "check_suite", "branch": ctx.task.branch}

    async def on_webhook(self, payload, ctx) -> str:
        ctx.task.data["ci_results"] = payload.get("summary", "")
        return "pass" if payload["conclusion"] == "success" else "fail"
```

The daemon exposes a `POST /webhooks/{source}` endpoint. The WebhookRouter matches incoming payloads to registered listeners and invokes the appropriate stage's `on_webhook`.

### Blocked

Every AgentStage implicitly supports `"blocked"` as an outcome. When an agent calls `finish(result="blocked", question="...")`, the engine:

1. Sets task status to `"blocked"`.
2. Records a `Blocked` event in history.
3. Notifies Delegate, who either resolves it directly or escalates to a human.

When resolved, the daemon creates a new agent session for the task with the resolution included in context. The task remains in the same stage — it doesn't transition.

```
Agent calls finish(result="blocked", question="Is Redis available?")
→ Task status: blocked, stays in Coding stage
→ Delegate is notified, asks human
→ Human answers: "Yes, redis.staging.internal:6379"
→ New agent session created with context including Q&A
→ Agent continues work, eventually calls finish(result="done")
```

## Task

```python
@dataclass
class Task:
    id: str
    title: str
    brief: str
    workflow: str
    workflow_version: str
    current_stage: str
    status: str                 # active, blocked, waiting_for_human, error, stuck, done
    assignee: str | None
    repos: list[str]
    data: dict                  # arbitrary JSON, opaque to system, persists across stages
    history: list[TaskEvent]    # append-only event log
    parent_id: str | None       # if created by planning workflow
    created_at: datetime
    stage_entered_at: datetime

    def create_subtask(self, **kwargs) -> "Task":
        """Create child task. Engine flushes to DB after stage completes."""
        ...
```

### Task Data

The `data` dict carries arbitrary state between stages. Agents write to it via `set_task_data` tool. Stages read from it via `ctx.task.data`. Examples: analysis results, spec documents, CI results, build IDs, client feedback.

### Task History

Append-only list of typed events. Never modified after creation.

```python
TaskEvent = (
    TaskCreated | StageTransition | Assignment | Blocked | BlockResolved
    | HumanEdit | Comment | DataChanged | ErrorOccurred | LogEntry
)

@dataclass
class StageTransition:
    at: datetime
    from_stage: str
    to_stage: str
    outcome: str
    forced: bool = False

@dataclass
class Assignment:
    at: datetime
    stage: str
    assignee: str
    assigned_by: str        # "delegate" | "direct"
    reason: str | None

@dataclass
class Blocked:
    at: datetime
    stage: str
    reason: str
    question: str | None

@dataclass
class BlockResolved:
    at: datetime
    stage: str
    resolution: str
    resolved_by: str

@dataclass
class HumanEdit:
    at: datetime
    stage: str
    files: list[str]
    by: str
    summary: str
```

## Context

Every stage method receives a `WorkflowContext`:

```python
@dataclass
class WorkflowContext:
    task: Task
    team: Team
    assignee: str | None
    workflow: Workflow
    _capabilities: dict[str, Any]

    def cap(self, name: str) -> Any:
        if name not in self._capabilities:
            raise CapabilityNotAvailable(name, self.team.name)
        return self._capabilities[name]

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._capabilities

    @property
    def git(self): return self.cap("git")

    @property
    def slack(self): return self.cap("slack")

    @property
    def review_comments(self) -> list[Comment]:
        return [e for e in self.task.history if isinstance(e, Comment)]

    @property
    def previous_blocks(self) -> list[tuple[Blocked, BlockResolved | None]]:
        ...
```

### Capabilities

Capabilities are integration clients injected into context. Loaded from config at daemon startup. Workflow code accesses them via `ctx.cap("name")` or convenience properties.

```toml
# .delegate/config.toml
[capabilities.slack]
module = "delegate.capabilities.slack:SlackCapability"
token_env = "SLACK_BOT_TOKEN"
default_channel = "#eng"

[capabilities.github]
module = "delegate.capabilities.github:GitHubCapability"
token_env = "GITHUB_TOKEN"
```

A capability is just a Python class with a constructor that takes config:

```python
class SlackCapability:
    def __init__(self, token, default_channel):
        self.client = SlackClient(token)
        self.default_channel = default_channel

    async def post(self, message, channel=None):
        await self.client.chat_postMessage(
            channel=channel or self.default_channel, text=message
        )
```

If a capability isn't configured, `ctx.cap()` raises `CapabilityNotAvailable`. Stages can check `"slack" in ctx.capabilities` before using one.

## Daemon, Delegate, and Agent Interaction

### Communication Topology

No actor talks directly to another. The daemon mediates everything.

```
Human ←→ Daemon ←→ Delegate (manager agent)
                ←→ Agent-1, Agent-2, ... (coding agents)
                ←→ External services (git, slack, CI)
```

### Delegate as Manager

Delegate is a persistent LLM agent with a long-running conversation. It doesn't see workflow code — it sees natural language descriptions of workflows and uses tools to manage tasks. Its system prompt includes team info, workflow descriptions, and the current task board.

Delegate's tools:

| Tool | What it does | What daemon does behind it |
|------|-------------|---------------------------|
| `create_task(title, brief, workflow, repos, data)` | Create a new task | Daemon creates task, runs workflow code (initial stage guard, assign, instructions), returns stage info and candidate pool to Delegate |
| `advance_task(task_id, outcome)` | Move task to next stage | Daemon runs workflow code (exit hook, transition lookup, next stage guard, enter hook, assign, instructions), returns next stage info and candidate pool. Auto stages are chained internally. |
| `assign_task(task_id, assignee, message?)` | Assign someone to work on a task | Daemon updates task, spawns agent session or notifies human |
| `resolve_block(task_id, resolution)` | Answer a blocked agent's question | Daemon creates new agent session with resolution in context |
| `reply(message)` | Send message to human | Daemon delivers to human |
| `message_agent(task_id, message)` | Send message to an agent working on a task | Daemon injects into agent's session |

The key insight: `create_task` and `advance_task` return structured data generated by running workflow code. To Delegate, it feels like calling a library. The workflow is Delegate's process documentation made executable.

Example `create_task` return:
```json
{
    "task_id": "BE-045",
    "stage": "Coding",
    "stage_type": "agent",
    "instructions": "Implement the changes described in the task brief.",
    "candidates": {
        "pool": ["agent-1", "agent-2", "agent-3"],
        "instruction": "Pick agent most familiar with this area.",
        "exclude": []
    }
}
```

Example `advance_task` return:
```json
{
    "transition": {
        "from": "Coding",
        "to": "Review",
        "stage_type": "human",
        "action": {"type": "ReviewCode", "files": ["src/rate_limit.py"], "editable": true},
        "candidates": {"pool": ["nikhil", "sarah"], "exclude": ["agent-3"]}
    }
}
```

Or on guard failure:
```json
{
    "transition": null,
    "guard_failure": {"stage": "Merging", "reason": "CI not passing"}
}
```

### Agent Sessions

Agents are logical identities, not persistent processes. Each task assignment gets its own session. When an agent finishes or is blocked, the session ends. If the same agent is assigned a new task, a new session is created.

Agent tools:
- `Read`, `Edit`, `Write`, `Bash` — coding tools, scoped to task worktree
- `set_task_data(key, value)` — persist structured data in task
- `get_task_data(key)` — read from task data
- `finish(result, summary, question?)` — signal outcome; `result` is one of the stage's outcomes or `"blocked"`

The agent's prompt is assembled from: task brief + stage instructions + history context (previous stage path, review comments, block resolutions, human edits, spec context).

### Event Flow

Daemon feeds engine events to Delegate as messages in its conversation:

```
[human] nikhil: "Add rate limiting to the API"
→ Delegate: create_task(...) + assign_task(...) + reply(...)

Agent finishes → daemon tells Delegate: "Agent-3 finished BE-045 (Coding): done."
→ Delegate: advance_task(BE-045, "done") + assign_task(BE-045, "nikhil") + reply(...)

Agent blocked → daemon tells Delegate: "Agent-3 stuck on BE-044: Is Redis available?"
→ Delegate: reply("@nikhil, agent needs to know...") OR resolve_block(if Delegate knows)

Human action (review approved) → daemon tells Delegate: "Nikhil approved BE-045"
→ Delegate: advance_task(BE-045, "approved") → auto-chains through Merging → Done
```

Delegate communicates in its own voice. The human sees Delegate as the manager. The daemon is invisible infrastructure.

### Error Handling and Escape Hatches

Three failure modes:

1. **Hook error** — guard/enter/exit/action threw an exception. Task status → `"error"`. Human notified with traceback.
2. **Bad state** — no valid transition for the given outcome. Task status → `"stuck"`. Human uses force-transition.
3. **Guard rejection** — guard returned `False`. Task stays in current stage, retries periodically.

Human escape hatches (CLI or UI):
```
delegate task retry BE-042              # re-run failed hook
delegate task force-transition BE-042 Testing  # skip to stage
delegate task rewind BE-042 Coding      # go back
delegate task complete BE-042 "Fixed it myself"  # force outcome
```

## Multi-Repo Support

A task can span multiple repos. All are mounted under one worktree root:

```
/worktrees/BE-042/
├── backend/        ← worktree, branch task/BE-042
├── frontend/       ← worktree, branch task/BE-042
└── shared-lib/     ← worktree, branch task/BE-042
```

`task.repos` lists which repos the task touches. The agent's cwd is `task.worktree_root`. Merge and review stages operate on all changed repos.

## Example Workflows

### Standard Engineering

```
Coding (agent) → Review (human) → Revise (agent, loop) → Merging (auto) → Done
```

### Planning → Engineering

```
Planning workflow:
  Analyze (agent) → DraftSpec (agent) → SpecReview (human) → CreateTasks (auto) → Done

Each created task enters the Engineering workflow independently.
```

Planning produces a structured spec in `task.data["spec"]`. Human reviews and optionally edits it. `CreateTasks` reads the approved spec and creates child tasks with `task.create_subtask()`.

### Bug Triage

```
Triage (human, categorize) → [critical: create hotfix subtask, normal: create eng subtask, wontfix: Done]
```

### Approval Chain (dynamic)

Self-loop with guard: `DynamicApproval → DynamicApproval (loop until all approved) → Published`

The guard checks `task.data["approved_by"]` against `task.data["required_approvers"]`. Each iteration assigns the next approver.

### Autonomous Research

```
Todo → Researching (agent, loops autonomously) → Reporting (human reviews) → Done
                                                      ↓ needs more experiments
                                                  Researching (loops back)
```

The built-in `research` workflow ships with Delegate. The researcher agent enters an autonomous experiment loop: modify code → run → evaluate → keep/discard → repeat. The agent uses `git reset --hard` to discard failed experiments and keeps successful ones as commits. When done, the task moves to `Reporting` where the human reviews results. The human can send it back to `Researching` for more experiments or move to `Done`.

Key difference from engineering workflows: no review/merge pipeline. Research produces experiment logs and insights, not merge-ready PRs.

### Deployment with Monitoring

```
DeployCanary (auto) → MonitorCanary (auto, guard retries on timer) → DeployFull (auto) → Done
                         ↓ degraded
                      Rollback (auto) → Investigate (human)
```

## Testing

### Static validation

```python
from delegate.testing import validate_workflow
errors = validate_workflow(engineering)
assert errors == []
```

### Unit testing stages

```python
from delegate.testing import mock_context

async def test_merging_returns_conflict():
    ctx = mock_context(git=MockGit(merge_result=MergeResult(conflict=True)))
    stage = Merging()
    outcome = await stage.action(ctx)
    assert outcome == "conflict"
```

### Workflow simulation

```python
from delegate.testing import WorkflowSimulator

async def test_happy_path():
    sim = WorkflowSimulator(engineering)
    task = await sim.run_path(["done", "approved", "done"])
    assert task.current_stage == "Done"

async def test_revision_loop():
    sim = WorkflowSimulator(engineering)
    task = await sim.create_task()
    task = await sim.signal(task, "done")               # Coding → Review
    task = await sim.signal(task, "changes_requested")   # Review → Revise
    task = await sim.signal(task, "done")               # Revise → Review
    task = await sim.signal(task, "approved")            # Review → Merging → Done
    assert task.current_stage == "Done"
```

## Build Order

```
Phase 1: Data models (task.py, workflow.py, actions.py) — pure dataclasses, no IO
Phase 2: Persistence (store.py) — CRUD on tasks, SQLite or JSON for alpha
Phase 3: Engine (engine.py) — transition function, lifecycle, error handling
Phase 4: Context and capabilities (context.py, capabilities/) — wire into engine
Phase 5: Agent integration (agent_bridge.py) — session management, finish tool
Phase 6: Human actions and API (api.py, commands.py) — HTTP endpoints, CLI
Phase 7: Real workflows (workflows/) — engineering, planning
```