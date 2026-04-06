# System Invariants

This document describes the key invariants that underpin the correctness
and security of the Delegate system.  Every contributor should understand
these properties — violating any one of them can compromise agent
isolation, data integrity, or system stability.

---

## 1. Filesystem Isolation

**Invariant:** Agents may only write to their designated directories.
The `protected/` directory and other teams' directories are never
writable from agent bash sessions.

### Implementation

- `add_dirs` (OS sandbox) is narrowed to:
  - The agent's team working directory (`teams/<uuid>/`).
  - The platform temp directory.
  - `.git/` directories of registered repos (all agents, including managers).
- The `protected/` subtree (DB, PID, logs, config, network allowlist)
  lives outside every agent's sandbox.

### Why It Matters

Without filesystem isolation an agent could overwrite the database,
modify another agent's workspace, tamper with the network allowlist,
or corrupt the daemon PID file.

---

## 2. Sandbox Layers (Defence in Depth)

**Invariant:** Security is enforced at multiple independent layers.
Bypassing one layer must not be sufficient to compromise the system.

| Layer | Mechanism | What it guards |
|-------|-----------|---------------|
| **OS sandbox** | `SandboxSettings.add_dirs` | Filesystem write scope |
| **Tool deny list** | `disallowed_tools` | Dangerous git operations |
| **Bash deny patterns** | `denied_bash_patterns` + `can_use_tool` guard (case-insensitive) | `sqlite3`, `git push`, SQL patterns (`DROP TABLE`, etc.) |
| **MCP tool boundary** | In-process MCP tools for data/metadata | DB reads/writes happen inside daemon, outside sandbox |
| **Network allowlist** | `protected/network.yaml` → `SandboxSettings.network` | Domain-level egress filtering |

### Why It Matters

A single-layer sandbox can be circumvented by creative prompt
injection or tool-use chains.  Layered enforcement makes exploitation
exponentially harder.

---

## 3. Git State Management

**Invariant:** Agents never alter branch topology, interact with
remotes, or modify the repository's `.git/` directory outside of
sanctioned `git add` / `git commit` operations in their worktree.

### Implementation

- All topology-changing git commands (`rebase`, `merge`, `push`,
  `pull`, `fetch`, `checkout`, `switch`, `reset --hard`, `branch`,
  `worktree`, `remote`, `filter-branch`, `reflog expire`) are blocked
  via both `disallowed_tools` and `denied_bash_patterns`.
- Worktree creation and deletion are performed exclusively by the
  daemon in `_ensure_task_infra`.
- All agents (workers and managers) get read-write access to repo `.git/` dirs
  (needed for `git add`/`git commit`) but never to the repo working tree itself.

### Why It Matters

Unconstrained git operations can destroy commit history, introduce
unauthorized code into `main`, or create branch name collisions that
break the worktree management system.

---

## 4. UUID Identity

**Invariant:** Teams and members are identified internally by UUIDs.
Human-readable names are display labels only; all database queries and
filesystem paths use UUIDs.

### Implementation

- `register_team_path_mapping(hc_home, name)` creates a stable UUID
  for each team name, persisted in `protected/team_ids.json`.
- `resolve_team_uuid(hc_home, name)` resolves a name to its UUID
  (cached in-process).
- `team_dir(hc_home, name)` returns `teams/<uuid>/`.
- All SQL `WHERE` clauses filter on `team_uuid`, not `team`.
- The `team` column in the database stores the human-readable name
  for display; `team_uuid` stores the UUID for queries.

### Why It Matters

Using names directly would allow collisions when teams are deleted
and recreated, cause filesystem path reuse with stale data, and
break cross-team queries when names contain special characters.

---

## 5. Dependency Ordering

**Invariant:** A task's worktree is only created after all its
`depends_on` dependencies have reached a terminal state (`done` or
`cancelled`).

### Implementation

- `_all_deps_resolved()` in `task.py` checks whether every dependency
  task is in a terminal workflow stage.
- `_ensure_task_infra()` in `web.py` skips worktree creation for
  tasks with unresolved dependencies.
- `update_task()` refuses to **add** new dependencies to a task whose
  existing dependencies are all already resolved (work may have
  started).  Removing dependencies is always allowed.

### Why It Matters

Creating a worktree before dependencies are resolved means the
worktree branches off an older `main` that lacks the dependency's
changes.  Agents then work on stale code, leading to conflicts and
wasted effort.

---

## 6. Daemon Singleton

**Invariant:** At most one daemon process runs per `DELEGATE_HOME` at
any time.

### Implementation

- **`fcntl.flock()`** — an exclusive advisory lock on
  `protected/daemon.lock`.  The OS releases the lock automatically
  when the process exits (even on `SIGKILL`), eliminating stale-lock
  issues.
- **PID file** (`protected/daemon.pid`) — a supplementary check for
  `is_running()` and `stop_daemon()`.
- Foreground mode acquires the lock in `start_daemon()`.
- Background mode acquires the lock inside the child process's
  `_lifespan()` startup.

### Why It Matters

Multiple daemons operating on the same database and worktrees
concurrently would cause data corruption, duplicate agent sessions,
and branch conflicts.

---

## 7. MCP Tool Boundary

**Invariant:** Agents interact with Delegate's data layer (database,
config, mailbox) exclusively through in-process MCP tools.  They
never invoke `delegate` CLI commands or access the database directly.

### Implementation

- `build_agent_tools()` in `mcp_tools.py` exposes:
  - **Communication:** `mailbox_send`, `mailbox_inbox`
  - **Tasks:** `task_create`, `task_list`, `task_show`, `task_assign`,
    `task_status`, `task_comment`, `task_cancel`, `task_attach`,
    `task_detach`
  - **Repos:** `repo_list`, `rebase_to_main`, `task_diff`
  - **Approval:** `task_approve`, `task_reject`
  - **Artifacts:** `artifact_save`, `artifact_list`, `artifact_path`
  - **Background:** `run_background`, `check_background`,
    `cancel_background`, `list_background`
- These tools run in the daemon process, outside the agent's OS
  sandbox, so they can read/write `protected/` files.
- System prompts explicitly instruct agents to use MCP tools instead
  of CLI commands.
- `sqlite3` and SQL patterns (`DROP TABLE`, `DELETE FROM`, `TRUNCATE`,
  `ALTER TABLE`) are in `denied_bash_patterns` as additional safety nets.
- Admin operations (`delegate network`, `delegate team`,
  `delegate workflow`) are **not** exposed via MCP — they remain
  human-only CLI commands.

### Why It Matters

Direct database access from agents would bypass authorization checks,
allow arbitrary data modification, and break the UUID/team isolation
model.

---

## 8. Network Isolation

**Invariant:** Agent network egress is restricted to the domains in
the global allowlist (`protected/network.yaml`).

### Implementation

- The allowlist defaults to a curated set of package manager
  registries, git forges, and ML/data science domains (composed
  from `adapters.CORE_DOMAINS` + `adapters.DOMAIN_GROUPS`).
  Managed via `delegate network show/allow/disallow/reset`.
- `network.yaml` lives in `protected/` — outside agent sandbox.
- The allowlist is read at Telephone creation time and passed to
  `SandboxSettings.network.allowedDomains`.
- If the allowlist changes between turns, the Telephone is
  destroyed and recreated (same pattern as repo-list change
  detection).

### Why It Matters

Without network restrictions, a compromised or jailbroken agent
could exfiltrate sensitive code, call unauthorized APIs, or download
malicious payloads.

---

## 9. Database Migration Safety

**Invariant:** Schema migrations are applied atomically with pre-
migration backups and post-migration health verification.

### Implementation

- Migrations are numbered SQL files in `delegate/migrations/V*.sql`.
- Before each migration, a timestamped backup of the database is
  created in `protected/backups/`.
- After each migration, `_verify_db_health()` checks for expected
  tables.
- On failure, the migration is rolled back and the backup is
  restored.
- The `schema_version` table tracks the current version; migrations
  are idempotent (skipped if already applied).

### Why It Matters

A failed migration without backup could leave the database in an
inconsistent state, losing task history, session data, and mailbox
messages.

---

## Summary

| # | Invariant | Key Mechanism |
|---|-----------|---------------|
| 1 | Filesystem isolation | `add_dirs` narrowed to team dir |
| 2 | Defence in depth | 6 independent security layers |
| 3 | Git state management | `disallowed_tools` + `denied_bash_patterns` |
| 4 | UUID identity | `team_ids.json` + `team_uuid` column |
| 5 | Dependency ordering | `_all_deps_resolved()` gates worktree creation + auto-advance |
| 6 | Daemon singleton | `fcntl.flock()` + PID file |
| 7 | MCP tool boundary | In-process tools, no CLI/DB from agents |
| 8 | Network isolation | `protected/network.yaml` allowlist (curated defaults) |
| 9 | Migration safety | Numbered files + backup + verify |
