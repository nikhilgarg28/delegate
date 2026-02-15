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

Delegate creates a persistent team of AI agents on your local machine. Describe what you want in plain English — Delegate breaks it into tasks, assigns them to agents, manages code reviews between them, and merges the result. You watch it all happen in a real-time web UI, or check in later. The team remembers your codebase across tasks.

> **Note:** Delegate currently works with **local git repositories** — agents commit directly to branches on your machine. Support for remote repositories (GitHub, GitLab), external tools (Slack, Linear), and CI/CD integrations is coming soon - stay tuned!

<!-- TODO: Replace with an actual screenshot or demo GIF
<p align="center">
  <img src="docs/screenshot.png" alt="Delegate UI" width="800">
</p>
-->

### How is this different?

Most AI coding tools give you **one agent, one task, then it's gone**. Delegate gives you a **team that persists** — agents accumulate context about your codebase, your patterns, and each other over weeks of work. And unlike terminal-based tools, you get a **browser UI** where you can watch agents work in real-time, review diffs, approve merges, and run shell commands — all without leaving the browser.

## Quickstart

```bash
pip install delegate-ai
delegate start # needs claude code login or ANTHROPIC_API_KEY in ENV
```

That's it. Delegate will create a default team with a manager + 5 engineer agents
and open the console in the browser. You tell delegate agent what to build and it
handles the rest.

## What happens when you send a task

```
You: "Add a /health endpoint that returns uptime and version"
```

1. **Manager** breaks it down, creates one or more tasks, assigns to available agents
2. **Agent** gets a git worktree, writes the code, runs tests, submits for review
3. **Reviewer** (another agent) checks the diff, runs the test suite, approves or rejects
4. **You** approve the merge (or set repos to auto-merge)
5. **Merge worker** rebases onto main, runs pre-merge checks, fast-forward merges

All of this is visible in real-time in the web UI — tasks moving, agents working, code being reviewed.

## Key features

**Persistent teams, not disposable agents.** Create a team once, use it across hundreds of tasks. Agents maintain memory — journals, notes, context files — so they learn your codebase, conventions, and patterns over time. 

**One team per project** - Spin up a team for each project, running for as long 
as you need with zero cost when not in use, all on your local filesystem.

**Browser UI with real-time visibility.** Watch agents pick up tasks, write code, and review each other's work — live. Approve merges, browse diffs, inspect files, and run shell commands — all from the browser.

**Full development lifecycle.** Agents don't just write code but also review each other's code. By default tasks flow through `todo → in_progress → in_review → in_approval → merging → done` with agents handling each stage.

**Customizable workflows.** Not happy with default task workflow? Define your own task lifecycle in Python:

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

**Real git, real branches.** Each agent works in isolated [git worktrees](https://git-scm.com/docs/git-worktree). No magic file systems. Branches are named `delegate/<team>/T0001`. You can inspect them anytime.

**Mix models by role.** By default, the manager runs on Claude Opus and engineers run on Claude Sonnet — strong reasoning where it matters, cost-efficient execution everywhere else. Configurable per agent.

**Team charter in markdown.** Edit a markdown file to set review standards, communication norms, and team values. 

**Built-in shell.** Run any command from the chat with `/shell ls -la`. Output renders inline.

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

Agents are [Claude Code](https://docs.anthropic.com/en/docs/claude-code) instances. A delegate agent manages other agents on your behalf. Engineers work in git worktrees and communicate through a message bus. The daemon polls for messages and dispatches agent turns as async tasks. All storage is in local files - either plaintext or sqlite.

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

### Set Auto Approval

By default, Delegate expects you to do a final code review and give explicit
approval before merging into your local repo's main. If you wanted, you can set 
it to auto approval:

```bash
delegate repo set-approval myteam my-repo auto
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

## Roadmap

Delegate is under active development. Here's what's coming:

- **Sandboxing & permissions** — run agents in containers with fine-grained filesystem and network access controls, so you can safely let them operate on production repos.
- **More powerful workflows** — conditional transitions, parallel stages, human-in-the-loop checkpoints, and webhook triggers.
- **External tool integrations** — GitHub (PRs, issues), Slack (notifications, commands), Linear (task sync), and CI/CD pipelines (GitHub Actions, etc.).
- **Remote repositories** — push to and pull from remote Git hosts, not just local repos.
- **Exportable team templates** — package a team's configuration (agents, workflows, charter, repo settings) as a shareable template so others can spin up an identical setup in one command.

If any of these are particularly important to you, open an issue — it helps prioritize.

## About

Delegate is built by a solo developer as a side project — and built *with* Delegate. No VC funding, no growth targets — just a tool I wanted for myself and decided to open-source. MIT licensed, free forever.

If you find it useful, star the repo or say hi in an issue. Bug reports and contributions are welcome.

## License

[MIT](LICENSE)
