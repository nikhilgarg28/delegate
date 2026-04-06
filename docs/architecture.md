# Architecture

This document describes Delegate's internal architecture — the module
structure, extension points, and key design decisions.  For user-facing
documentation see [README.md](../README.md); for system invariants see
[invariants.md](invariants.md).

---

## Module Map

```
delegate/
├── adapters.py          # Technology-specific adapters (probes, domains)
├── agent.py             # Agent state, prompt construction (legacy path)
├── background.py        # Background process manager for long-running commands
├── bootstrap.py         # Team/agent bootstrap and member lookup
├── cli.py               # Click CLI (delegate start, team add, etc.)
├── config.py            # Per-team and global configuration (repos.yaml, config.yaml)
├── daemon.py            # Daemon lifecycle (start, stop, singleton lock)
├── db.py                # SQLite database, migrations, session tracking
├── env.py               # Environment detection (Python, Node, Rust, Go, Ruby, Nix, conda)
├── mcp_tools.py         # MCP tool definitions (mailbox, tasks, repos, artifacts, background)
├── names.py             # Agent name generation
├── network.py           # Network allowlist management
├── paths.py             # Centralized path computations
├── prompt.py            # Prompt class — preamble + user message construction
├── repo.py              # Repository registration and management
├── runtime.py           # Telephone creation, sandbox config, turn dispatch
├── task.py              # Task CRUD, status transitions, auto-advance
├── telephone.py         # Claude Code SDK subprocess wrapper
├── uploads.py           # File upload handling for web UI
├── web.py               # FastAPI web server, SSE streams
├── workflow.py          # Workflow engine (stages, transitions, guards)
│
├── charter/             # Markdown charter files (team values, role practices)
│   ├── roles/           # Per-role instructions (manager.md, engineer.md, researcher.md, …)
│   ├── addons/          # Conditional capability addons (ml.md, …)
│   └── variants/        # Alternative value/review presets (quality-first, ship-fast)
│
└── workflows/           # Built-in workflow implementations
    ├── default.py       # Engineering workflow (todo → in_progress → in_review → … → done)
    ├── research.py      # Research workflow (todo → researching → reporting → done)
    └── git.py           # Git mixin (worktree, merge, artifacts, review helpers)
```

---

## Adapter System (`delegate/adapters.py`)

Delegate's core infrastructure is technology-neutral.  All technology-specific
code lives in `adapters.py` behind three registries.  This means adding support
for new hardware or a new domain group is a single-class or single-function
addition — no changes to core modules required.

### 1. Environment Probes

Probes gather hardware context for researcher agents so they can make
informed decisions about batch sizes, device placement, and package versions.

```python
from delegate.adapters import ENVIRONMENT_PROBES, probe_environment

# Registry: name → callable returning list[str]
ENVIRONMENT_PROBES = {
    "gpu_nvidia": _probe_nvidia_gpu,    # nvidia-smi
    "cuda_toolkit": _probe_cuda_toolkit, # nvcc --version
    "cpu": _probe_cpu,                   # os.cpu_count()
    "ram": _probe_ram,                   # /proc/meminfo
    "disk": _probe_disk,                 # shutil.disk_usage("/")
}

# Cached orchestrator — runs all probes, joins results
info = probe_environment()  # "GPU 0: RTX 4090, …\nCPU cores: 16\n…"
```

**Extending:** To add AMD ROCm GPU detection, add a new probe:

```python
@_register_probe("gpu_amd")
def _probe_amd_gpu() -> list[str]:
    result = subprocess.run(["rocm-smi", ...], ...)
    return [f"GPU: {line}" for line in result.stdout.splitlines()]
```

The probe will automatically be included in the hardware context block
for researcher agents.

### 2. Network Domain Groups

Domains are split into thematic groups.  All groups are included in the
default allowlist, but the separation enables per-team composition.

```python
from delegate.adapters import CORE_DOMAINS, DOMAIN_GROUPS, build_default_domains

CORE_DOMAINS = [
    "pypi.org", "registry.npmjs.org", "crates.io",  # package managers
    "github.com", "*.github.com", ...                # git forges
]

DOMAIN_GROUPS = {
    "ml": ["download.pytorch.org", "huggingface.co", "*.huggingface.co",
           "conda.anaconda.org", "repo.anaconda.com", ...],
}

DEFAULT_DOMAINS = build_default_domains()  # CORE + all groups
```

**Extending:** To add a finance domain group:

```python
DOMAIN_GROUPS["finance"] = ["polygon.io", "api.alpaca.markets", ...]
```

### 3. Artifact Categories

Artifacts are persistent outputs that survive worktree teardown.  Categories
determine the subdirectory structure under `artifacts/T{id}/`.

```python
from delegate.adapters import DEFAULT_ARTIFACT_CATEGORIES

DEFAULT_ARTIFACT_CATEGORIES = {
    "model": "models",     # Trained models, checkpoints
    "log": "logs",         # Training logs, experiment records
    "report": "reports",   # Evaluation reports, summaries
    "data": "data",        # Processed datasets, feature stores
    "output": "outputs",   # Generic outputs
}
```

The `artifact_save` MCP tool schema and `setup_artifacts()` both derive
their category lists from this constant.

---

## Charter Addon System

Role charters (e.g. `researcher.md`) contain generic, technology-neutral
practices.  Technology-specific guidance lives in addon files under
`charter/addons/` and is conditionally included based on detected
capabilities.

```
charter/
├── roles/
│   ├── researcher.md    # Generic: experiment loop, autonomy, git discipline, …
│   ├── engineer.md
│   └── manager.md
└── addons/
    └── ml.md            # ML-specific: conda, CUDA, PyTorch index URLs
```

**How addons are loaded:**

1. The prompt builder reads the role charter (e.g. `researcher.md`)
2. It calls `_applicable_addons()` which checks capabilities:
   - `ml` addon is included when GPU hardware is detected (not "GPU: None")
3. Matching addon files are appended to the role charter

This keeps role charters clean and focused on methodology, while
technology-specific setup instructions are loaded only when relevant.

**Adding an addon:** Create `charter/addons/<name>.md` and add detection
logic to `_applicable_addons()` in `prompt.py`.

---

## Research Infrastructure

The research workflow (`todo → researching → reporting → done`) enables
autonomous iterative experimentation.  The supporting infrastructure is
role-neutral — it works for ML training, data analysis, optimization, or
any long-running experimental workflow.

### Background Process Manager (`delegate/background.py`)

Wraps long-running commands in detached subshell processes with:

- Exit code capture via sentinel file (avoids zombie/PID detection issues)
- Stdout/stderr log files for tailing
- Timeout enforcement (default: 4 hours)
- Concurrency limit (5 per agent)
- Process lifecycle tracking in JSON manifest

Exposed via four MCP tools: `run_background`, `check_background`,
`cancel_background`, `list_background`.

### Artifact Persistence (`delegate/paths.py`, `delegate/workflows/git.py`)

```
teams/{team}/artifacts/T{id}/
├── manifest.json        # Catalog of saved artifacts
├── models/
├── logs/
├── reports/
├── data/
└── outputs/
```

Artifacts persist after worktree teardown.  The `$ARTIFACTS_DIR` environment
variable is injected into researcher agent sessions.  Three MCP tools
manage artifacts: `artifact_save`, `artifact_list`, `artifact_path`.

### Task Auto-Advance (`delegate/task.py`)

When a task reaches a terminal status (`done` or `cancelled`), dependent
tasks with all dependencies resolved are automatically advanced to their
first working stage:

- Default workflow: `todo → in_progress`
- Research workflow: `todo → researching`

This enables pipeline chaining where completing one research task
automatically kicks off the next.

### Researcher Sandbox Relaxation

Researchers get three git commands restored (removed from deny lists):

- `git reset --hard` — discard failed experiments
- `git checkout` — switch between experiment branches
- `git branch` — create/manage experiment branches

All other git restrictions (push, rebase, merge, fetch, etc.) remain enforced.

---

## Sandbox Architecture (6 Layers)

| # | Layer | Mechanism | Scope |
|---|-------|-----------|-------|
| 1 | Write-path isolation | `can_use_tool` callback | Per-tool file path checks |
| 2 | Git command deny | `disallowed_tools` list | Hidden from agent entirely |
| 3 | Bash deny patterns | Substring match in guard | `git push`, `sqlite3`, SQL patterns |
| 4 | OS sandbox | macOS Seatbelt / Linux bubblewrap | Kernel-level filesystem restriction |
| 5 | Network allowlist | `protected/network.yaml` | Domain-level egress filtering |
| 6 | MCP tool boundary | In-process daemon tools | Config access outside sandbox |

Bash deny patterns are case-insensitive (compared via `.upper()`).  SQL
patterns (e.g. `DROP TABLE`, `DELETE FROM`, `TRUNCATE`, `ALTER TABLE`)
are defined inline in `DENIED_BASH_PATTERNS`.

---

## Environment Detection (`delegate/env.py`)

Auto-detects project tooling and generates `.delegate/setup.sh` and
`.delegate/premerge.sh`:

| Priority | Detector | Trigger Files |
|----------|----------|---------------|
| 1 | Poetry | `poetry.lock` |
| 2 | UV | `uv.lock` |
| 3 | Conda/Mamba | `environment.yml` / `environment.yaml` |
| 4 | Pip | `pyproject.toml` / `requirements.txt` |
| 5 | Node | `package.json` |
| 6 | Rust | `Cargo.toml` |
| 7 | Go | `go.mod` |
| 8 | Ruby | `Gemfile` |
| 9 | Nix | `default.nix` / `flake.nix` |

Multi-language repos are supported — the detector scans root + top-level
subdirectories and composes setup scripts for all detected stacks.

Conda/mamba detection prefers `mamba` when available and creates a
local `.conda-env` directory in the worktree (not a global conda env).

---

## Prompt Construction (`delegate/prompt.py`)

The `Prompt` class builds two outputs per agent turn:

1. **Preamble** (stable per generation) — charter + role + addons + identity + tools + hardware context + inlined notes + reference files
2. **User message** (per turn) — task context + inbox messages + history

The preamble is rebuilt each turn to detect changes (new repos, config
updates).  If the preamble changes, the Telephone subprocess is rotated
(context window reset with a summary of prior work).

Hardware context (`=== HARDWARE ENVIRONMENT ===`) is only included for
researcher role agents and is cached for the process lifetime.

A parallel legacy path (`agent.build_system_prompt()`) produces
byte-identical output and is maintained for backward compatibility.
Both paths delegate to `adapters.probe_environment()` and
`format_hardware_block()`.

---

## Package Cache Sharing (`delegate/network.py`)

All agents in a team share a package cache at
`teams/{team}/.pkg-cache/`.  Environment variables for every major
package manager are redirected:

| Manager | Env Var |
|---------|---------|
| pip | `PIP_CACHE_DIR` |
| uv | `UV_CACHE_DIR` |
| npm | `npm_config_cache` |
| yarn | `YARN_CACHE_FOLDER` |
| cargo | `CARGO_HOME` |
| go | `GOMODCACHE` |
| gem/bundler | `GEM_HOME`, `BUNDLE_PATH` |
| gradle | `GRADLE_USER_HOME` |
| nuget | `NUGET_PACKAGES` |
| cocoapods | `CP_HOME_DIR` |
| pub | `PUB_CACHE` |
| composer | `COMPOSER_CACHE_DIR` |
| hex/mix | `HEX_HOME`, `MIX_HOME` |

This ensures downloads succeed inside the sandbox (system-wide caches
are unwritable) and avoids redundant downloads across agents.

---

## Key Design Decisions

**Why a single `adapters.py` instead of a plugin package?**
Delegate is a single-repo project.  A plugin system would add complexity
without payoff — adapters are added by editing one file.  If the adapter
count grows significantly, the module can be split into a package.

**Why does the researcher charter not mention specific technologies?**
The researcher role is generic — it works for ML training, financial
modeling, data analysis, or any iterative experimentation.  Technology-
specific guidance (conda, CUDA, PyTorch) lives in charter addons that
are loaded conditionally based on detected hardware.
