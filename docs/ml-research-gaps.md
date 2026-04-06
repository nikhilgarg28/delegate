# ML Research Gaps: Enabling Supervised Learning via Autoresearch

> **Status: All 6 gaps implemented.**  The infrastructure is now generic and
> technology-neutral — see [architecture.md](architecture.md) for the adapter
> system that decouples core infrastructure from specific hardware and ML
> frameworks.  The examples below reference a specific ML trading pipeline
> that motivated the analysis, but the resulting implementation works for
> any research workflow.

This document identifies the gaps between Delegate's current researcher/autoresearch
infrastructure and the requirements for autonomous supervised ML training pipelines.
Each gap is scoped as an actionable task with clear acceptance criteria.

**Target use case**: A researcher agent autonomously executing multi-step ML workflows
(data validation, model training on GPU, backtesting, deployment) on a GPU server,
with iterative experimentation and structured artifact management.

**Reference pipeline** (Phase 2A — Supervised ML Training):

1. Validate data completeness
2. Run first model training on GPU
3. Train additional models
4. Walk-forward backtest results, feature importance analysis
5. Wire trained outputs into config, deploy to staging

---

## Current State: What Autoresearch Already Provides

The researcher role and research workflow (commits `2e3080e`–`5dd12a1`) added:

- **Researcher charter** (`charter/roles/researcher.md`): Autonomous modify → run →
  evaluate → keep/discard loop with results.tsv tracking and periodic progress reports.
- **Research workflow** (`workflows/research.py`): `todo → researching → reporting → done`
  lifecycle with human review at the end, loopback from reporting → researching.
- **Sandbox relaxation** (`runtime.py`): Researchers get `git reset --hard`,
  `git checkout`, `git branch` permissions for discarding failed experiments.
- **Worktree isolation**: Standard per-task isolated git worktree with auto-setup of
  Python venv, node_modules, etc.
- **MCP tools**: Full task management (create, list, show, status, comment, attach),
  mailbox communication, repo listing, rebase-to-main.

This infrastructure is well-suited for **code optimization** tasks (e.g., "minimize
val_bpb by modifying model architecture"). The following gaps block its use for
**ML training and data science** workflows.

---

## Gap 1: Long-Running Command Execution

**Priority**: P0 — Blocks all GPU training tasks

### Problem

Claude Code's bash tool has a default timeout of ~120 seconds. The `run_script` helper
in `workflows/git.py:307` caps at 600 seconds (10 minutes). GPU training runs routinely
take 30 minutes to several hours. When a researcher agent runs:

```bash
python train.py --epochs 50 --data timescale://localhost/market_data
```

…the bash command will be killed after the timeout, and the agent will see a timeout
error instead of training results.

There is no mechanism in `Telephone` to configure per-role or per-task bash timeouts.
The `ClaudeAgentOptions` passed to the SDK do not include a `bashTimeout` parameter
(checked: `telephone.py:687–742`).

### What Needs to Change

#### Option A: Background Process + Polling Pattern (Recommended)

Add guidance and tooling for the researcher to launch long-running processes detached
and poll for completion:

1. **Researcher charter addition**: Document the pattern for long-running commands:
   ```
   # For commands that take >2 minutes:
   nohup python train.py > train.log 2>&1 & echo $!
   # Then poll:
   tail -20 train.log
   # Check if still running:
   kill -0 <pid> 2>/dev/null && echo RUNNING || echo DONE
   ```

2. **MCP tool `run_background`** (optional, stronger): A daemon-side tool that:
   - Launches a subprocess detached from the agent's bash session
   - Returns a handle/ID immediately
   - Provides `check_background(handle)` to poll status + tail output
   - Stores stdout/stderr in a managed log file under the task's artifact directory
   - Enforces a configurable max runtime (default 4h, override via task metadata)

3. **Researcher charter update**: Add a "Long-Running Experiments" section explaining
   when and how to use background execution vs inline bash.

#### Option B: Extended Bash Timeout for Researchers

Add a `bash_timeout` field to `Telephone` and set it high (e.g., 14400s / 4h) for
researcher roles. Simpler but blocks the agent's turn for the entire training duration
— the agent cannot do anything else or report progress while waiting.

### Acceptance Criteria

- [ ] A researcher agent can launch a GPU training script that runs for 2+ hours
- [ ] The agent receives the training output (metrics, loss curves) when complete
- [ ] The agent can check on training progress mid-run (tail logs)
- [ ] The agent is not blocked from sending progress messages during training
- [ ] Training process survives agent context-window rotation

### Files to Modify

- `delegate/charter/roles/researcher.md` — add long-running command patterns
- `delegate/mcp_tools.py` — add `run_background` / `check_background` tools (if Option A with MCP)
- `delegate/telephone.py` — add `bash_timeout` parameter (if Option B)
- `delegate/runtime.py` — pass timeout config from role/task metadata

---

## Gap 2: GPU and Hardware Awareness

**Priority**: P1 — Training will work by accident but waste tokens on discovery

### Problem

The researcher charter and system prompt contain no information about the execution
environment's hardware. When a researcher agent starts a training task, it will:

1. Not know whether a GPU is available → may try CPU training (10–100x slower)
2. Not know the GPU model/memory → may OOM with wrong batch sizes
3. Not know CUDA version → may install incompatible PyTorch
4. Not know available RAM/disk → may crash on large datasets
5. Waste 5–10 tool calls probing the system (`nvidia-smi`, `lscpu`, `free -h`, etc.)

### What Needs to Change

#### 3a. Hardware Context in Researcher Prompt

Probe hardware at Telephone creation time and inject into the system prompt:

```python
# In runtime.py or prompt.py, for researcher role:
def _probe_hardware() -> str:
    """Gather hardware context for ML researchers."""
    info = []
    # GPU
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version,compute_cap",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info.append(f"GPU: {result.stdout.strip()}")
    except FileNotFoundError:
        info.append("GPU: None (CPU only)")

    # CUDA
    try:
        result = subprocess.run(
            ["nvcc", "--version"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "release" in line.lower():
                    info.append(f"CUDA: {line.strip()}")
                    break
    except FileNotFoundError:
        pass

    # CPU / RAM
    import os
    info.append(f"CPU cores: {os.cpu_count()}")
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    gb = int(line.split()[1]) / (1024 * 1024)
                    info.append(f"RAM: {gb:.1f} GB")
                    break
    except Exception:
        pass

    # Disk
    try:
        result = subprocess.run(
            ["df", "-h", "--output=avail", "/"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if len(lines) > 1:
                info.append(f"Disk free: {lines[1].strip()}")
    except Exception:
        pass

    return "\n".join(info)
```

Inject this into the researcher's preamble:

```
## Hardware Environment
GPU: NVIDIA RTX 4090, 24576 MiB, Driver 550.54.15, Compute 8.9
CUDA: release 12.4, V12.4.131
CPU cores: 16
RAM: 64.0 GB
Disk free: 412G
```

#### 3b. Task Metadata for Hardware Requirements

Allow task metadata to specify hardware requirements that the researcher can read:

```python
task_create(
    title="Train return predictor",
    workflow="research",
    metadata={
        "gpu_required": True,
        "min_gpu_memory_gb": 16,
        "estimated_runtime_hours": 2,
        "cuda_version": "12.4",
    }
)
```

The researcher charter should instruct agents to check `metadata.gpu_required`
and validate hardware before starting training.

### Acceptance Criteria

- [ ] Researcher agents know GPU availability, model, and memory at prompt time
- [ ] Agents select correct PyTorch/TensorFlow CUDA variant without trial-and-error
- [ ] Agents can set appropriate batch sizes based on known GPU memory
- [ ] Hardware info is refreshed on Telephone recreation (not stale)

### Files to Modify

- `delegate/runtime.py` — add `_probe_hardware()`, inject into researcher preamble
- `delegate/prompt.py` — add hardware section to researcher prompt template
- `delegate/charter/roles/researcher.md` — add "Hardware Awareness" section

---

## Gap 3: Artifact and Model Persistence

**Priority**: P1 — Training works but models are lost on worktree teardown

### Problem

The research workflow tracks experiments via git commits and a `results.tsv` file in
the worktree. ML training produces large artifacts that don't belong in git:

- **Model checkpoints**: `.pt`, `.pkl`, `.onnx`, `.safetensors` — 100MB to 10GB+
- **Training logs**: TensorBoard event files, CSV loss logs
- **Evaluation outputs**: Confusion matrices, feature importance plots, backtest reports
- **Hyperparameter configs**: YAML/JSON files linking a model to its training config

When the task reaches `done`, `teardown_worktree()` in `workflows/research.py:87–89`
deletes the worktree (best-effort), destroying all artifacts that weren't committed
to git.

There is currently:
- No concept of an artifact directory that survives task completion
- No model registry or versioned storage
- No way to reference "the model produced by task T0042" from a follow-up task
- No attachment mechanism for binary blobs (task_attach stores a path string, not the file)

### What Needs to Change

#### 4a. Task Artifacts Directory

Create a persistent artifacts directory per task that survives worktree teardown:

```
~/.delegate/projects/<team>/artifacts/T<NNNN>/
├── models/
│   ├── return_predictor_v1.pt
│   └── regime_classifier_v1.pt
├── logs/
│   └── training_run_001/
│       └── events.out.tfevents.1234567890
├── reports/
│   ├── backtest_results.csv
│   └── feature_importance.png
└── manifest.json   # structured index of artifacts
```

Implementation:
- `delegate/paths.py`: Add `task_artifacts_dir(hc_home, team, task_id)` function
- `delegate/workflows/research.py`: In `Researching.enter()`, create the artifacts
  directory and inject its path as an environment variable (`ARTIFACTS_DIR`)
- `delegate/workflows/research.py`: In `Done.enter()`, do NOT delete the artifacts
  directory (only delete the worktree)
- `delegate/runtime.py`: Add artifacts dir to `allowed_write_paths` for the researcher

#### 4b. Artifact MCP Tools

```python
@tool("artifact_save", "Save a file as a named artifact for the current task.", {...})
async def artifact_save(args: dict) -> dict:
    # args: task_id, source_path (in worktree), artifact_name, category (model/log/report)
    # Copies file from worktree to artifacts dir
    # Updates manifest.json
    # Returns artifact path

@tool("artifact_list", "List artifacts for a task.", {...})
async def artifact_list(args: dict) -> dict:
    # Returns manifest.json contents

@tool("artifact_path", "Get the absolute path to a saved artifact.", {...})
async def artifact_path(args: dict) -> dict:
    # args: task_id, artifact_name
    # Returns the path so follow-up tasks can reference it
```

#### 4c. Researcher Charter Update

Add "Artifact Management" section:
```markdown
### Artifact Management

- Save model checkpoints and large outputs to `$ARTIFACTS_DIR` (injected env var),
  NOT to the git worktree. Git is for code, artifacts dir is for binary outputs.
- Use `artifact_save` to register important artifacts (best model, final report).
- Reference artifacts by task ID in your results.tsv so they can be traced.
- Structure: `$ARTIFACTS_DIR/models/`, `$ARTIFACTS_DIR/logs/`, `$ARTIFACTS_DIR/reports/`
```

### Acceptance Criteria

- [ ] Model checkpoints are saved to a directory that survives worktree teardown
- [ ] Artifacts are addressable by task ID from other tasks
- [ ] Artifact directory path is available to the researcher as an environment variable
- [ ] `manifest.json` tracks artifact metadata (name, size, timestamp, category)
- [ ] Follow-up tasks (e.g., "deploy model from T0042") can locate artifacts
- [ ] Worktree cleanup in `Done.enter()` does not delete artifacts

### Files to Modify

- `delegate/paths.py` — add `task_artifacts_dir()`
- `delegate/workflows/research.py` — create artifacts dir on enter, preserve on done
- `delegate/runtime.py` — add artifacts dir to write paths, inject `ARTIFACTS_DIR` env var
- `delegate/mcp_tools.py` — add `artifact_save`, `artifact_list`, `artifact_path`
- `delegate/charter/roles/researcher.md` — add artifact management section

---

## Gap 4: Multi-Stage Pipeline / Task Chaining

**Priority**: P1 — Each task works alone but the 5-step pipeline requires manual sequencing

### Problem

Phase 2A is a **sequential pipeline** where each step depends on the previous:

```
T1: Validate data
    ↓ (data confirmed complete)
T2: Train return predictor
    ↓ (model checkpoint saved)
T3: Train regime classifier + volatility forecaster
    ↓ (model checkpoints saved)
T4: Walk-forward backtest
    ↓ (backtest report with metrics)
T5: Wire into config, deploy to paper trading
```

The current system supports `depends_on` in `task_create`, but:

1. **No auto-trigger**: When T1 reaches `done`, T2 doesn't automatically start.
   The manager must notice and manually transition T2 from `todo` → `researching`.

2. **No output passing**: T2 needs to know "data validation passed, here's the
   dataset spec". T3 needs T2's model checkpoint path. There's no structured
   mechanism for passing outputs between tasks beyond mailbox messages (ephemeral)
   or task comments (unstructured).

3. **No pipeline-level status**: There's no way to see "Phase 2A is 60% complete"
   — only individual task statuses.

### What Needs to Change

#### 5a. Auto-Advance on Dependency Resolution

When a task transitions to `done`, check if any tasks that `depends_on` it now have
all dependencies satisfied. If so, auto-transition them from `todo` to the first
working stage.

```python
# In delegate/task.py or delegate/workflows/core.py:
def _check_dependents(hc_home, team, completed_task_id):
    """Auto-advance tasks whose dependencies are now all resolved."""
    dependents = list_tasks(hc_home, team, depends_on_includes=completed_task_id)
    for task in dependents:
        if task["status"] != "todo":
            continue
        deps = task.get("depends_on", [])
        all_done = all(
            get_task(hc_home, team, dep_id).get("status") == "done"
            for dep_id in deps
        )
        if all_done:
            change_status(hc_home, team, task["id"], "researching")
```

Hook this into `Done.enter()` for both default and research workflows.

#### 5b. Task Output Field

Add a structured `output` field to tasks (stored in metadata or a dedicated column):

```python
# Researcher writes output when transitioning to reporting:
task_update(task_id, metadata={
    "output": {
        "best_model": "/artifacts/T0002/models/return_predictor_v3.pt",
        "val_sharpe": 1.42,
        "val_mse": 0.0023,
        "config_patch": {"model.return_predictor.path": "..."},
    }
})

# Next task reads predecessor output:
predecessor = task_show(depends_on[0])
model_path = predecessor["metadata"]["output"]["best_model"]
```

#### 5c. Pipeline MCP Tool (Optional)

A convenience tool to create a full pipeline in one call:

```python
@tool("pipeline_create", "Create a sequence of dependent research tasks.", {...})
async def pipeline_create(args: dict) -> dict:
    # args: tasks = [{title, description, repo, ...}, ...]
    # Creates tasks with depends_on chaining: T[n].depends_on = [T[n-1].id]
    # Returns list of created task IDs
```

### Acceptance Criteria

- [ ] When T1 completes, T2 automatically transitions to `researching` (if depends_on satisfied)
- [ ] Task output (model paths, metrics) is stored in structured metadata
- [ ] Downstream tasks can read upstream task outputs via `task_show`
- [ ] A 5-task pipeline can be created with a single manager action
- [ ] Pipeline progress is visible (N of M tasks complete)

### Files to Modify

- `delegate/task.py` — add `_check_dependents()`, hook into status transitions
- `delegate/workflows/research.py` — call `_check_dependents` in `Done.enter()`
- `delegate/workflows/default.py` — call `_check_dependents` in `Done.enter()`
- `delegate/mcp_tools.py` — add `pipeline_create` tool (optional)
- `delegate/task.py` — document `metadata.output` convention

---

## Gap 5: ML Environment Setup

**Priority**: P1 — First training run may fail or be very slow without proper env

### Problem

The `env.py` auto-detection handles `requirements.txt` / `pyproject.toml` with pip/uv
but does not account for:

1. **CUDA-specific packages**: `pip install torch` gets the CPU version by default.
   The correct install is:
   ```
   pip install torch --index-url https://download.pytorch.org/whl/cu124
   ```
   The auto-generated `setup.sh` will install the wrong variant.

2. **Conda/Mamba environments**: Common in ML projects. `env.py` has no conda
   detection. If the repo has `environment.yml` or `conda-lock.yml`, the setup
   script won't know what to do.

3. **Large binary installs**: PyTorch alone is ~2.5 GB. With CUDA dependencies, a
   full ML environment can take 10–20 minutes to install. The `run_script` timeout
   of 600s in `workflows/git.py:313` may kill the install.

4. **PyTorch index URL not in network allowlist**: `download.pytorch.org` is not
   in the default allowed domains (`network.py:53–60`).

### What Needs to Change

#### 6a. Conda/Mamba Detection in env.py

```python
# In env.py, add conda stack detection:
def _detect_conda(directory: Path) -> StackInfo | None:
    if _has_file(directory, "environment.yml"):
        return StackInfo(
            setup_lines=[
                'if command -v mamba &>/dev/null; then',
                '  mamba env create -f environment.yml -p ./.conda-env --quiet || '
                '  mamba env update -f environment.yml -p ./.conda-env --quiet',
                'elif command -v conda &>/dev/null; then',
                '  conda env create -f environment.yml -p ./.conda-env --quiet || '
                '  conda env update -f environment.yml -p ./.conda-env --quiet',
                'fi',
                'conda activate ./.conda-env',
            ],
            ...
        )
```

#### 6b. PyTorch Index URL in Network Allowlist Defaults

Add to `DEFAULT_DOMAINS` in `network.py`:
```python
# ── ML package registries ──
"download.pytorch.org",
"data.pyg.org",
"huggingface.co",
"*.huggingface.co",
```

#### 6c. Extended Setup Timeout

The `run_script` timeout in `workflows/git.py:313` should be configurable or
extended for ML repos. Consider reading a `metadata.setup_timeout` field from
the task, defaulting to 600s but allowing 1800s (30min) for heavy installs.

#### 6d. Researcher Charter: Environment Section

Add to researcher charter:
```markdown
### ML Environment Setup

- If the repo uses conda/mamba (`environment.yml`), prefer that over pip.
- For PyTorch with GPU: always install with the CUDA index URL matching the
  system's CUDA version (check `nvcc --version`).
- If setup.sh doesn't handle GPU packages correctly, modify it and commit
  the fix as your first experiment (baseline establishment).
```

### Acceptance Criteria

- [ ] Repos with `environment.yml` get conda-based setup scripts
- [ ] PyTorch CUDA packages can be downloaded (domain in allowlist)
- [ ] ML environment installs of up to 20 minutes succeed without timeout
- [ ] Researcher agents know to install GPU-enabled packages, not CPU-only

### Files to Modify

- `delegate/env.py` — add `_detect_conda()` and integrate into `generate_env_scripts()`
- `delegate/network.py` — add ML domains to `DEFAULT_DOMAINS`
- `delegate/workflows/git.py` — make `run_script` timeout configurable
- `delegate/charter/roles/researcher.md` — add ML environment guidance

---

## Gap 6: Deployment Handoff (Research → Engineering)

**Priority**: P2 — Needed for step 5 only; workaround exists

### Problem

The final pipeline step — "Wire trained models into `config/trading.yaml`, deploy to
paper trading" — requires:

1. **Writing to the main repo** (not a research worktree) to update config files
2. **Merging changes** through the standard review pipeline (config changes are sensitive)
3. **Restarting services** or triggering a deployment

The researcher:
- Can only write within their worktree + agent dir
- Cannot push to remotes or modify main branch (sandbox restrictions)
- Has no service management tools (no `systemctl`, `docker`, etc. in sandbox)

### What Needs to Change

#### 7a. Research-to-Engineering Task Handoff

The researcher's final experiment should produce structured output that a follow-up
**engineering** task (default workflow, with review/merge) consumes:

```python
# Researcher sets task output:
task_comment(task_id, body=json.dumps({
    "deployment_config": {
        "model.return_predictor.path": "/artifacts/T0002/models/return_pred_v3.pt",
        "model.return_predictor.version": "v3",
        "model.regime_classifier.path": "/artifacts/T0003/models/regime_cls_v2.pt",
        "model.volatility_forecaster.path": "/artifacts/T0003/models/vol_forecast_v1.pt",
    },
    "config_file": "config/trading.yaml",
    "validation_metrics": {
        "sharpe_ratio": 1.42,
        "max_drawdown": -0.08,
    },
}))
```

The manager then creates an engineering task:
```python
task_create(
    title="Deploy ML models from T0002-T0004 to paper trading",
    description="Update config/trading.yaml with model paths from research tasks. "
                "Run validation suite. Deploy to paper trading environment.",
    workflow="default",  # goes through standard review/merge pipeline
    repo="trading-system",
    depends_on=[task_id_of_backtest],
)
```

#### 7b. Deployment Script Support (Optional)

For teams that want automated deployment after merge, add a post-merge hook:

```python
class Done(Stage):  # in default workflow
    def enter(self, ctx):
        ...
        # Run deploy script if configured
        deploy_cmd = ctx.task.get("metadata", {}).get("post_merge_cmd")
        if deploy_cmd:
            ctx.run_script(deploy_cmd)
```

### Acceptance Criteria

- [ ] Research task output includes structured deployment configuration
- [ ] Follow-up engineering task can read research task output
- [ ] Config changes go through standard code review (not bypassed)
- [ ] Deployment can be triggered post-merge (optional, configurable)

### Files to Modify

- `delegate/charter/roles/researcher.md` — add "Deployment Handoff" section
- `delegate/workflows/default.py` — add optional post-merge hook in `Done.enter()`
- Documentation for the manager on creating deployment follow-up tasks

---

---

## Implementation Order

Recommended sequence based on dependency and impact:

| Order | Gap | Priority | Effort | Depends On |
|-------|-----|----------|--------|------------|
| 1 | Gap 1: Long-running commands | P0 | M | — |
| 2 | Gap 2: Hardware awareness | P1 | S | — |
| 3 | Gap 3: Artifact persistence | P1 | M | — |
| 4 | Gap 5: ML environment setup | P1 | M | — |
| 5 | Gap 4: Pipeline chaining | P1 | L | Gap 3 |
| 6 | Gap 6: Deployment handoff | P2 | S | Gap 3, Gap 4 |

**Effort key**: S = small (1–2 files, < 100 LOC), M = medium (3–5 files, 100–300 LOC),
L = large (5+ files, 300+ LOC, needs tests)

Gaps 1–3 can be worked in parallel. Gap 4 depends on Gap 3 (artifacts needed for
output passing). Gap 6 depends on Gap 3 and Gap 4.

---

## Task Breakdown for Delegate

These can be created directly as Delegate tasks:

### P0 Tasks (Must-have for any ML training)

```
T: Add background process execution for long-running researcher commands
  Workflow: default
  Description: Add run_background/check_background MCP tools or researcher charter
  guidance for nohup+polling pattern. Researcher must be able to launch GPU training
  (2+ hours) and monitor progress without bash timeout killing the process.
```

### P1 Tasks (Needed for full pipeline)

```
T: Add hardware context to researcher system prompt
  Workflow: default
  Description: Probe GPU (nvidia-smi), CUDA (nvcc), CPU cores, RAM, disk at
  Telephone creation time. Inject as "## Hardware Environment" section in
  researcher preamble. Refresh on Telephone recreation.

T: Add persistent artifact directory for research tasks
  Workflow: default
  Description: Create ~/.delegate/projects/<team>/artifacts/T<NNNN>/ on research
  task start. Inject ARTIFACTS_DIR env var. Add to allowed_write_paths. Do NOT
  delete on worktree teardown. Add artifact_save/artifact_list/artifact_path MCP
  tools. Update researcher charter with artifact management guidance.

T: Add conda/mamba environment detection to env.py
  Workflow: default
  Description: Detect environment.yml and generate conda-based setup.sh.
  Add download.pytorch.org and huggingface.co to default network allowlist.
  Make run_script timeout configurable (default 600s, allow 1800s for ML).
  Update researcher charter with ML environment guidance.

T: Add auto-advance for tasks with resolved dependencies
  Workflow: default
  Description: When a task reaches 'done', check if dependent tasks now have
  all dependencies satisfied. If so, auto-transition from 'todo' to first
  working stage. Add task output convention (metadata.output). Add optional
  pipeline_create MCP tool for creating chained task sequences.
```

### P2 Tasks (Polish and safety)

```
T: Add research-to-engineering deployment handoff
  Workflow: default
  Description: Document structured output convention for research tasks that
  produce deployable artifacts. Add optional post-merge hook in default workflow
  Done stage. Document manager pattern for creating deployment follow-up tasks.
```
