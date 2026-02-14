<p align="center">
  <img src="branding/logo.svg" alt="delegate" height="40">
</p>

<p align="center">
  <strong>Not a copilot. A team that ships.</strong><br>
  <sub>AI agents that plan, code, review each other's work, and merge — running locally on your machine.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/delegate-ai/"><img src="https://img.shields.io/pypi/v/delegate-ai" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python 3.12+"></a>
</p>

---

Delegate is a CLI tool that creates a team of AI agents to build software for you. Describe what you want in plain English. Delegate breaks it into tasks, assigns them to agents, manages code reviews between them, and merges the result — while you watch (or don't).

<!-- TODO: Replace with an actual screenshot or demo GIF
<p align="center">
  <img src="docs/screenshot.png" alt="Delegate UI" width="800">
</p>
-->

## Quickstart

```bash
pip install delegate-ai
cd your-project
delegate start --env-file .env    # needs ANTHROPIC_API_KEY in .env
```

That's it. Delegate will:
1. Detect your name from `git config`
2. Create a team with a manager + 5 engineer agents
3. Register the current repo automatically
4. Open a browser with a chat interface

Tell the manager what to build. It handles the rest.

## What happens when you send a task

```
You: "Add a /health endpoint that returns uptime and version"
```

1. **Manager** breaks it down, creates a task, assigns it to an available agent
2. **Agent** gets a git worktree, writes the code, runs tests, submits for review
3. **Reviewer** (another agent) checks the diff, runs the test suite, approves or rejects
4. **You** approve the merge (or set repos to auto-merge)
5. **Merge worker** rebases onto main, runs pre-merge checks, fast-forward merges

All of this is visible in real-time in the web UI — tasks moving, agents working, code being reviewed.

## Key features

**Full development lifecycle.** Tasks flow through `todo → in_progress → in_review → in_approval → merging → done` with agents handling each stage. Rejections cycle back automatically.

**Real git, real branches.** Each agent works in isolated [git worktrees](https://git-scm.com/docs/git-worktree). No magic file systems. Branches are named `delegate/<team>/T0001`. You can inspect them anytime.

**Code review between agents.** Agents don't just write code — they review each other's work. Reviewers check out the branch, run the full test suite, and gate the merge queue. Reviews are visible in the UI with full diffs.

**Merge automation.** Rebase onto main, run pre-merge tests, fast-forward merge. If there are conflicts, Delegate tries a squash-reapply first. True conflicts get escalated with detailed hunks and resolution instructions.

**Customizable workflows.** Define your own task lifecycle in Python:

```python
from delegate.workflow import Stage, workflow

class Deploy(Stage):
    label = "Deploying"
    def enter(self, ctx):
        ctx.run_script("./deploy.sh")

@workflow(name="with-deploy", version=1)
def my_workflow():
    return [Todo, InProgress, InReview, Deploy, Done]
```

**Multi-team, multi-repo.** Run separate teams for different projects, each with their own agents, repos, and workflows.

**Built-in shell.** Run any command from the chat with `/shell ls -la`. Set the working directory with the CWD picker. Output renders inline.

**Keyboard-driven.** `?` for shortcuts, `j/k` navigation, `t/c/a` to switch tabs, `r` to reply, `/` for commands. Vim-style escape mode.

## Architecture

```
~/.delegate/
├── members/              # Human identities (from git config)
│   └── nikhil.yaml
├── teams/
│   └── my-project/
│       ├── agents/       # delegate (manager) + engineer agents
│       │   ├── delegate/ # Manager agent — your delegate
│       │   ├── alice/    # Engineer agent with worktrees, logs, memory
│       │   └── bob/
│       ├── repos/        # Symlinks to your real git repos
│       ├── shared/       # Team-wide shared files
│       └── workflows/    # Registered workflow definitions
└── db.sqlite             # Messages, tasks, events
```

Agents are [Claude Code](https://docs.anthropic.com/en/docs/claude-code) instances. The manager orchestrates — it doesn't write code. Engineers work in git worktrees and communicate through a message bus. The daemon polls for messages and dispatches agent turns as async tasks.

There's no magic. You can `ls` into any agent's directory and see exactly what they're doing. Worklogs, memory journals, context files — it's all plain text.

## Configuration

### Environment

```bash
# Required — your Anthropic API key
ANTHROPIC_API_KEY=sk-ant-...

# Optional
DELEGATE_HOME=~/.delegate    # Override home directory
```

### CLI commands

```bash
delegate start [--port 3548] [--env-file .env]   # Start everything
delegate stop                                     # Stop the daemon
delegate status                                   # Check if running

delegate team add backend --agents 3 --repo /path/to/repo
delegate team list
delegate repo add myteam /path/to/another-repo --test-cmd "pytest -x"
delegate agent add myteam carol --role engineer

delegate workflow init myteam                     # Register default workflow
delegate workflow add myteam ./my-workflow.py     # Register custom workflow
```

### Repo settings

```bash
# Auto-merge when agents approve (skip human approval)
delegate repo set-approval myteam my-repo auto

# Run tests before merging
delegate repo set-test-cmd myteam my-repo "python -m pytest -x -q"
```

## How it works

The **daemon** is the central loop:
- Polls agent inboxes for unread messages
- Dispatches turns (one agent at a time per agent, many agents in parallel)
- Processes the merge queue
- Serves the web UI and SSE streams

**Agents** are stateless between turns. Each turn:
1. Read inbox messages
2. Execute actions (create tasks, write code, send messages, run commands)
3. Write context summary for next turn

The **workflow engine** is a Python DSL. Each task is stamped with a workflow version at creation. Stages define `enter`/`exit`/`action`/`assign` hooks. Built-in functions (`ctx.setup_worktree()`, `ctx.create_review()`, `ctx.merge_task()`, etc.) handle git operations, reviews, and merging.

## Development

```bash
git clone https://github.com/nikhilgarg28/delegate.git
cd delegate
uv sync
uv run delegate start --foreground
```

### Tests

```bash
# Python tests
uv run pytest tests/ -x -q

# Playwright E2E tests (needs npm install first)
npm install
npx playwright install
npx playwright test
```

## License

[MIT](LICENSE)
