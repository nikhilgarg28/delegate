"""In-process MCP tools for agent data/metadata operations.

These tools run inside the daemon process (outside the OS sandbox) and
provide agents with safe access to configuration files
without requiring shell access to ``protected/``.

Each tool closure captures ``hc_home``, ``team``, and ``agent`` so that:
- Agents cannot impersonate other agents (sender identity is baked in).
- All operations go through the model layer (same validation as CLI).
- Config files are only modified via trusted code paths.

Admin operations (``delegate network``, ``delegate team``, ``delegate workflow``)
are intentionally NOT exposed here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _text_result(text: str) -> dict:
    """Wrap a plain string into the MCP tool result format."""
    return {"content": [{"type": "text", "text": text}]}


_MAX_RESULT_BYTES = 800_000  # Stay well under SDK's 1 MB JSON buffer limit


def _json_result(data: Any) -> dict:
    """Wrap a JSON-serialisable object into the MCP tool result format.

    If the data is a list and the serialized result would exceed
    ``_MAX_RESULT_BYTES``, items are dropped from the end until the
    result fits.  This ensures the output is always valid JSON.
    """
    text = json.dumps(data, indent=2, default=str)
    if len(text) > _MAX_RESULT_BYTES and isinstance(data, list) and len(data) > 1:
        # Binary-search for the largest prefix that fits
        lo, hi = 1, len(data)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(json.dumps(data[:mid], indent=2, default=str)) <= _MAX_RESULT_BYTES:
                lo = mid
            else:
                hi = mid - 1
        text = json.dumps(data[:lo], indent=2, default=str)
        text += (
            f"\n\n(showing {lo} of {len(data)} items — result exceeded "
            f"{_MAX_RESULT_BYTES} byte limit. Use filters or task_show for details.)"
        )
    return _text_result(text)


def _error_result(msg: str) -> dict:
    """Return an MCP tool error result."""
    return {"content": [{"type": "text", "text": f"ERROR: {msg}"}], "isError": True}


def _load_artifact_manifest(art_dir: Path) -> list[dict]:
    """Load the artifact manifest for a task, returning [] if missing/corrupt."""
    manifest_path = art_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        return json.loads(manifest_path.read_text())
    except Exception:
        return []


def _format_duration(seconds: float) -> str:
    """Format seconds as a human-readable duration string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


# ---------------------------------------------------------------------------
# Live system resource collection (used by check_resources MCP tool)
# ---------------------------------------------------------------------------


def _parse_nvsmi_value(s: str) -> float | None:
    """Parse a nvidia-smi CSV value like '8 %' or '1791 MiB' into a float."""
    s = s.strip()
    if not s or s == "[N/A]":
        return None
    # Strip non-numeric suffix (e.g. " %", " MiB", " W")
    parts = s.split()
    try:
        return float(parts[0])
    except (ValueError, IndexError):
        return None


def _read_cpu_times() -> list[int]:
    """Read aggregate CPU jiffies from ``/proc/stat`` (first line)."""
    with open("/proc/stat") as f:
        line = f.readline()  # "cpu  user nice sys idle ..."
    return [int(v) for v in line.split()[1:]]


def _collect_live_resources() -> dict:
    """Collect live system utilization — CPU, RAM, GPU(s), disk.

    Designed for Linux.  Each section is independently guarded so a
    failure in one (e.g. no nvidia-smi) doesn't break the others.
    All sections return the full key structure with ``None`` values on
    error so consumers never hit ``KeyError``.
    """
    import os
    import shutil
    import subprocess
    import time

    result: dict = {}

    # --- CPU (two-sample /proc/stat with 0.1s gap) ---
    try:
        t1 = _read_cpu_times()
        time.sleep(0.1)
        t2 = _read_cpu_times()

        delta = [b - a for a, b in zip(t1, t2)]
        total = sum(delta)
        # idle is index 3, iowait is index 4
        idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
        util = round(100.0 * (1 - idle / total), 1) if total > 0 else 0.0

        result["cpu"] = {
            "utilization_pct": util,
            "core_count": os.cpu_count() or 0,
        }
    except Exception:
        result["cpu"] = {"utilization_pct": None, "core_count": os.cpu_count() or 0}

    # --- RAM (/proc/meminfo) ---
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0].rstrip(":")] = int(parts[1])  # kB
                if len(mem) == 2:
                    break
        total_kb = mem["MemTotal"]
        avail_kb = mem["MemAvailable"]
        used_kb = total_kb - avail_kb
        result["ram"] = {
            "total_gb": round(total_kb / 1048576, 1),
            "used_gb": round(used_kb / 1048576, 1),
            "available_gb": round(avail_kb / 1048576, 1),
            "percent": round(100.0 * used_kb / total_kb, 1) if total_kb else 0.0,
        }
    except Exception:
        result["ram"] = {
            "total_gb": None, "used_gb": None, "available_gb": None, "percent": None,
        }

    # --- GPUs (nvidia-smi) ---
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,utilization.gpu,"
                "memory.used,memory.total,power.draw",
                "--format=csv,noheader",
            ],
            capture_output=True, text=True, timeout=3,
        )
        gpus = []
        if proc.returncode == 0:
            for line in proc.stdout.strip().splitlines():
                fields = [f.strip() for f in line.split(",")]
                if len(fields) >= 7:
                    gpus.append({
                        "index": int(fields[0]) if fields[0].isdigit() else 0,
                        "name": fields[1],
                        "temperature_c": _parse_nvsmi_value(fields[2]),
                        "utilization_pct": _parse_nvsmi_value(fields[3]),
                        "memory_used_mib": _parse_nvsmi_value(fields[4]),
                        "memory_total_mib": _parse_nvsmi_value(fields[5]),
                        "power_draw_w": _parse_nvsmi_value(fields[6]),
                    })
        result["gpus"] = gpus
    except Exception:
        result["gpus"] = []

    # --- Disk ---
    try:
        usage = shutil.disk_usage("/")
        result["disk"] = {
            "total_gb": round(usage.total / (1 << 30)),
            "used_gb": round(usage.used / (1 << 30)),
            "free_gb": round(usage.free / (1 << 30)),
            "percent": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0,
        }
    except Exception:
        result["disk"] = {
            "total_gb": None, "used_gb": None, "free_gb": None, "percent": None,
        }

    return result


# ---------------------------------------------------------------------------
# Tool factory — builds all MCP tools for a given agent context
# ---------------------------------------------------------------------------


def build_agent_tools(hc_home: Path, team: str, agent: str) -> list:
    """Build the list of MCP tool definitions for an agent.

    Returns a list of decorated tool functions ready to pass to
    ``create_sdk_mcp_server(tools=[...])``.

    Raises ``ImportError`` if ``claude_agent_sdk`` is not available.
    """
    from claude_agent_sdk import tool
    from delegate.runtime import _sandbox_for_role, _read_state
    from delegate.paths import agent_dir as _ad

    # Resolve the agent's role so background commands respect the sandbox.
    _agent_state = _read_state(_ad(hc_home, team, agent))
    _agent_role = _agent_state.get("role", "engineer")
    _, _denied_patterns = _sandbox_for_role(_agent_role)

    # -----------------------------------------------------------------------
    # Mailbox tools
    # -----------------------------------------------------------------------

    @tool(
        "mailbox_send",
        "Send a message to another team member. This is the ONLY way to communicate with others.",
        {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "message": {"type": "string"},
                "task_id": {
                    "type": ["integer", "null"],
                    "description": "Optional task ID to associate the message with. Omit or pass null for messages not related to a specific task.",
                },
            },
            "required": ["recipient", "message"],
        },
    )
    async def mailbox_send(args: dict) -> dict:
        try:
            from delegate.mailbox import send

            recipient = args["recipient"]
            message = args["message"]
            task_id = args.get("task_id")
            # Defense-in-depth: convert 0 to None (task IDs start at 1;
            # some MCP clients may default missing int params to 0)
            if task_id == 0:
                task_id = None

            send(
                hc_home,
                team,
                agent,           # sender is baked in — no impersonation
                recipient,
                message,
                task_id=task_id,
            )
            result = f"Message sent to {recipient}"
            if task_id:
                result += f" (task T{task_id:04d})"
            return _text_result(result)
        except Exception as e:
            logger.exception("mailbox_send failed")
            return _error_result(str(e))

    @tool(
        "mailbox_inbox",
        "Check your inbox for unread messages.",
        {},
    )
    async def mailbox_inbox(args: dict) -> dict:
        try:
            from delegate.mailbox import read_inbox

            messages = read_inbox(hc_home, team, agent, unread_only=True)
            if not messages:
                return _text_result("No unread messages.")
            result = []
            for m in messages:
                entry = {
                    "from": m.sender,
                    "body": m.body,
                    "task_id": m.task_id,
                    "timestamp": str(m.timestamp) if hasattr(m, "timestamp") else None,
                }
                result.append(entry)
            return _json_result(result)
        except Exception as e:
            logger.exception("mailbox_inbox failed")
            return _error_result(str(e))

    # -----------------------------------------------------------------------
    # Task tools
    # -----------------------------------------------------------------------

    @tool(
        "task_create",
        "Create a new task for the team. Returns the created task.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "description": "low, medium, high, or critical"},
                "repo": {"type": "string", "description": "Repository name for the task"},
                "depends_on": {"type": "string", "description": "Comma-separated task IDs this depends on"},
                "workflow": {"type": "string", "description": "Workflow name (default: 'default'). Use 'research' for autonomous experiment tasks."},
            },
            "required": ["title"],
        },
    )
    async def task_create(args: dict) -> dict:
        try:
            from delegate.task import create_task

            kwargs: dict[str, Any] = {
                "title": args["title"],
                "assignee": agent,  # default to creating agent
            }
            if args.get("description"):
                kwargs["description"] = args["description"]
            if args.get("priority"):
                kwargs["priority"] = args["priority"]
            if args.get("repo"):
                kwargs["repo"] = args["repo"]
            if args.get("depends_on"):
                # Parse comma-separated task IDs
                try:
                    deps = [int(x.strip()) for x in args["depends_on"].split(",")]
                    kwargs["depends_on"] = deps
                except ValueError:
                    return _error_result(
                        "depends_on must be comma-separated integers (e.g. '1,2,3')"
                    )
            if args.get("workflow"):
                kwargs["workflow_name"] = args["workflow"]

            task = create_task(hc_home, team, **kwargs)
            return _json_result(task)
        except Exception as e:
            logger.exception("task_create failed")
            return _error_result(str(e))

    @tool(
        "task_list",
        "List tasks (summary view). Returns id, title, status, assignee, priority, and a few other fields. "
        "Excludes done/cancelled tasks by default — pass status='done' to see them. "
        "Use task_show(task_id) for full details on a specific task.",
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by task status (e.g. 'todo', 'in_progress', 'done'). Without this, done/cancelled tasks are excluded."},
                "assignee": {"type": "string", "description": "Filter by assignee name"},
            },
            "required": [],
        },
    )
    async def task_list(args: dict) -> dict:
        try:
            from delegate.task import list_tasks, TERMINAL_STATUSES, SUMMARY_FIELDS

            kwargs: dict[str, Any] = {}
            if args.get("status"):
                kwargs["status"] = args["status"]
            else:
                kwargs["exclude_statuses"] = TERMINAL_STATUSES
            if args.get("assignee"):
                kwargs["assignee"] = args["assignee"]

            tasks = list_tasks(hc_home, team, **kwargs)

            # Return summary-only to stay within SDK buffer limits
            summaries = [
                {k: t[k] for k in SUMMARY_FIELDS if k in t}
                for t in tasks
            ]
            return _json_result(summaries)
        except Exception as e:
            logger.exception("task_list failed")
            return _error_result(str(e))

    @tool(
        "task_show",
        "Show detailed information about a specific task.",
        {"task_id": int},
    )
    async def task_show(args: dict) -> dict:
        try:
            from delegate.task import get_task

            task = get_task(hc_home, team, args["task_id"])
            return _json_result(task)
        except Exception as e:
            logger.exception("task_show failed")
            return _error_result(str(e))

    @tool(
        "task_assign",
        "Assign a task to a team member.",
        {"task_id": int, "assignee": str},
    )
    async def task_assign(args: dict) -> dict:
        try:
            from delegate.task import assign_task, get_task
            from delegate.mailbox import send as send_message

            task_id = args["task_id"]
            assignee = args["assignee"]

            assign_task(hc_home, team, task_id, assignee)

            # Auto-notify: send mailbox message so the assignee gets a turn
            if assignee != agent:
                task = get_task(hc_home, team, task_id)
                title = task.get("title", f"T{task_id:04d}")
                send_message(
                    hc_home, team, agent, assignee,
                    f"Task T{task_id:04d} ({title}) has been assigned to you. Please review and begin work.",
                    task_id=task_id,
                )

            return _text_result(
                f"Task T{task_id:04d} assigned to {assignee}"
            )
        except Exception as e:
            logger.exception("task_assign failed")
            return _error_result(str(e))

    @tool(
        "task_status",
        "Change the status of a task (e.g. 'in_progress', 'in_review', 'done').",
        {"task_id": int, "new_status": str},
    )
    async def task_status(args: dict) -> dict:
        try:
            from delegate.task import change_status

            change_status(hc_home, team, args["task_id"], args["new_status"])
            return _text_result(
                f"Task T{args['task_id']:04d} status changed to {args['new_status']}"
            )
        except Exception as e:
            logger.exception("task_status failed")
            return _error_result(str(e))

    @tool(
        "task_comment",
        "Add a durable comment/note to a task (specs, findings, decisions).",
        {"task_id": int, "body": str},
    )
    async def task_comment(args: dict) -> dict:
        try:
            from delegate.task import add_comment

            add_comment(
                hc_home, team, args["task_id"],
                author=agent,  # baked-in identity
                body=args["body"],
            )
            return _text_result(
                f"Comment added to T{args['task_id']:04d}"
            )
        except Exception as e:
            logger.exception("task_comment failed")
            return _error_result(str(e))

    @tool(
        "task_cancel",
        "Cancel a task (manager only — cleans up worktrees and branches).",
        {"task_id": int},
    )
    async def task_cancel(args: dict) -> dict:
        try:
            from delegate.task import cancel_task

            cancel_task(hc_home, team, args["task_id"])
            return _text_result(f"Task T{args['task_id']:04d} cancelled")
        except Exception as e:
            logger.exception("task_cancel failed")
            return _error_result(str(e))

    @tool(
        "task_attach",
        "Attach a file to a task.",
        {"task_id": int, "file_path": str},
    )
    async def task_attach(args: dict) -> dict:
        try:
            from delegate.task import update_task, get_task

            task = get_task(hc_home, team, args["task_id"])
            attachments = list(task.get("attachments", []))
            if args["file_path"] not in attachments:
                attachments.append(args["file_path"])
            update_task(hc_home, team, args["task_id"], attachments=attachments)
            return _text_result(
                f"Attached {args['file_path']} to T{args['task_id']:04d}"
            )
        except Exception as e:
            logger.exception("task_attach failed")
            return _error_result(str(e))

    @tool(
        "task_detach",
        "Remove a file attachment from a task.",
        {"task_id": int, "file_path": str},
    )
    async def task_detach(args: dict) -> dict:
        try:
            from delegate.task import update_task, get_task

            task = get_task(hc_home, team, args["task_id"])
            attachments = list(task.get("attachments", []))
            if args["file_path"] in attachments:
                attachments.remove(args["file_path"])
            update_task(hc_home, team, args["task_id"], attachments=attachments)
            return _text_result(
                f"Detached {args['file_path']} from T{args['task_id']:04d}"
            )
        except Exception as e:
            logger.exception("task_detach failed")
            return _error_result(str(e))

    # -----------------------------------------------------------------------
    # Repo tools
    # -----------------------------------------------------------------------

    @tool(
        "repo_list",
        "List all registered repositories for the team.",
        {},
    )
    async def repo_list(args: dict) -> dict:
        try:
            from delegate.repo import list_repos

            repos = list_repos(hc_home, team)
            if not repos:
                return _text_result("No repositories registered.")
            return _json_result(repos)
        except Exception as e:
            logger.exception("repo_list failed")
            return _error_result(str(e))

    # -----------------------------------------------------------------------
    # Git tools
    # -----------------------------------------------------------------------

    @tool(
        "rebase_to_main",
        "Rebase the current task branch onto latest main using diff-apply. "
        "Computes the feature branch's own diff, resets hard to main, then "
        "re-applies only the feature's changes. Clean hunks are staged "
        "automatically; conflicting hunks get <<<<<<< markers. Returns "
        "had_conflicts: true/false. Updates base_sha to main HEAD. "
        "Fails if the working tree is dirty.",
        {"task_id": int},
    )
    async def rebase_to_main(args: dict) -> dict:
        try:
            import subprocess
            from delegate.task import get_task, update_task, format_task_id
            from delegate.repo import get_task_worktree_path, get_repo_path

            task_id = args["task_id"]
            task = get_task(hc_home, team, task_id)

            branch = task.get("branch")
            if not branch:
                return _error_result(f"Task {format_task_id(task_id)} has no branch")

            repos = task.get("repo", [])
            if not repos:
                return _error_result(f"Task {format_task_id(task_id)} has no repos")

            result_data = {
                "task_id": task_id,
                "branch": branch,
                "repos": {},
            }

            for repo_name in repos:
                # Get paths
                worktree_path = get_task_worktree_path(hc_home, team, repo_name, task_id)
                if not worktree_path.exists():
                    return _error_result(
                        f"Worktree not found for {repo_name}: {worktree_path}"
                    )

                repo_path = get_repo_path(hc_home, team, repo_name)
                wt_str = str(worktree_path)

                # Check for uncommitted changes
                diff_check = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=wt_str,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if diff_check.stdout.strip():
                    return _error_result(
                        f"Working tree is dirty in {repo_name}. "
                        f"Commit or stash changes before rebasing."
                    )

                # Check for staged changes
                staged_check = subprocess.run(
                    ["git", "diff", "--cached", "--name-only"],
                    cwd=wt_str,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if staged_check.stdout.strip():
                    return _error_result(
                        f"Working tree has staged changes in {repo_name}. "
                        f"Commit or unstage changes before rebasing."
                    )

                # Get current default branch HEAD
                from delegate.repo import get_default_branch
                db = get_default_branch(wt_str)
                main_sha_result = subprocess.run(
                    ["git", "rev-parse", db],
                    cwd=wt_str,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if main_sha_result.returncode != 0:
                    return _error_result(
                        f"Failed to get {db} HEAD in {repo_name}: "
                        f"{main_sha_result.stderr}"
                    )

                new_main_sha = main_sha_result.stdout.strip()

                # Determine the base SHA for the feature diff.
                # Use the task's stored base_sha if available, otherwise
                # compute the merge-base between the default branch and HEAD.
                base_sha = (task.get("base_sha") or {}).get(repo_name)
                if not base_sha:
                    mb_result = subprocess.run(
                        ["git", "merge-base", db, "HEAD"],
                        cwd=wt_str,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if mb_result.returncode != 0:
                        return _error_result(
                            f"Failed to compute merge-base in {repo_name}: "
                            f"{mb_result.stderr}"
                        )
                    base_sha = mb_result.stdout.strip()

                # Compute the feature branch's own diff (only the agent's changes)
                diff_result = subprocess.run(
                    ["git", "diff", "--binary", f"{base_sha}..HEAD"],
                    cwd=wt_str,
                    capture_output=True,
                    timeout=120,
                )
                if diff_result.returncode != 0:
                    return _error_result(
                        f"Failed to compute feature diff in {repo_name}: "
                        f"{diff_result.stderr.decode('utf-8', errors='replace')}"
                    )

                feature_patch = diff_result.stdout  # bytes (binary diff)

                # Reset hard to main (clean slate with all merged files)
                reset_result = subprocess.run(
                    ["git", "reset", "--hard", db],
                    cwd=wt_str,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if reset_result.returncode != 0:
                    return _error_result(
                        f"git reset --hard {db} failed in {repo_name}: "
                        f"{reset_result.stderr}"
                    )

                # Apply the feature diff onto main
                had_conflicts = False
                if feature_patch.strip():
                    apply_result = subprocess.run(
                        ["git", "apply", "--index", "--3way"],
                        cwd=wt_str,
                        input=feature_patch,
                        capture_output=True,
                        timeout=120,
                    )
                    if apply_result.returncode != 0:
                        # --3way exits non-zero when there are conflicts,
                        # but still applies clean hunks and stages them.
                        # Check if there are conflict markers in the working tree.
                        conflict_check = subprocess.run(
                            ["git", "diff", "--name-only", "--diff-filter=U"],
                            cwd=wt_str,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        # Also check for unmerged paths via ls-files
                        unmerged_check = subprocess.run(
                            ["git", "ls-files", "--unmerged"],
                            cwd=wt_str,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        if unmerged_check.stdout.strip():
                            had_conflicts = True
                        else:
                            # apply failed for a non-conflict reason
                            return _error_result(
                                f"git apply failed in {repo_name}: "
                                f"{apply_result.stderr.decode('utf-8', errors='replace')}"
                            )

                result_data["repos"][repo_name] = {
                    "new_base_sha": new_main_sha,
                    "had_conflicts": had_conflicts,
                    "status": "applied_conflicts" if had_conflicts else "applied_clean",
                }

            # Update task base_sha for all repos
            base_sha_dict = {
                repo_name: data["new_base_sha"]
                for repo_name, data in result_data["repos"].items()
            }
            update_task(hc_home, team, task_id, base_sha=base_sha_dict)

            # Reconcile main-prefer files: reset configured patterns to
            # main's version so agents don't carry stale shared-file edits.
            from delegate.config import get_main_prefer_files
            from fnmatch import fnmatch

            all_reconciled: dict[str, list[str]] = {}
            for repo_name, data in result_data["repos"].items():
                if data.get("had_conflicts"):
                    continue
                wt_str = str(get_task_worktree_path(hc_home, team, repo_name, task_id))
                patterns = get_main_prefer_files(hc_home, team, repo_name)
                if not patterns:
                    continue
                from delegate.repo import get_default_branch
                db = get_default_branch(wt_str)
                diff_r = subprocess.run(
                    ["git", "diff", "--name-only", f"{db}..HEAD"],
                    cwd=wt_str, capture_output=True, text=True, timeout=30,
                )
                if diff_r.returncode != 0:
                    continue
                changed = diff_r.stdout.strip().splitlines()
                to_reset = [
                    f for f in changed
                    if any(fnmatch(f, p) or f.endswith(f"/{p}") or f == p for p in patterns)
                ]
                if to_reset:
                    for f in to_reset:
                        subprocess.run(
                            ["git", "checkout", db, "--", f],
                            cwd=wt_str, capture_output=True, text=True, timeout=30,
                        )
                    subprocess.run(
                        ["git", "add"] + to_reset,
                        cwd=wt_str, capture_output=True, text=True, timeout=30,
                    )
                    all_reconciled[repo_name] = to_reset
                    data["reconciled_files"] = to_reset

            if all_reconciled:
                result_data["reconciled_files"] = all_reconciled

            had_any_conflicts = any(
                data.get("had_conflicts") for data in result_data["repos"].values()
            )
            result_data["had_conflicts"] = had_any_conflicts

            if had_any_conflicts:
                result_data["message"] = (
                    f"Rebased {format_task_id(task_id)} onto main with conflicts. "
                    f"Files with <<<<<<< markers need manual resolution. "
                    f"After resolving: git add -A && git commit."
                )
            else:
                result_data["message"] = (
                    f"Successfully rebased {format_task_id(task_id)} onto main. "
                    f"Changes are staged. Review with 'git status' and commit when ready."
                )

            return _json_result(result_data)

        except Exception as e:
            logger.exception("rebase_to_main failed")
            return _error_result(str(e))

    # -----------------------------------------------------------------------
    # Review tools (used by reviewer agents)
    # -----------------------------------------------------------------------

    @tool(
        "task_diff",
        "Get the diff for a task in review. Returns diff text, task spec, "
        "sensitive file warnings (if any), and whether a rebase is needed.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID to get the diff for"},
            },
            "required": ["task_id"],
        },
    )
    async def task_diff(args: dict) -> dict:
        try:
            import subprocess
            from delegate.task import get_task, get_task_diff as _get_task_diff, format_task_id
            from delegate.auto_approve import check_sensitive_files, MAX_DIFF_CHARS
            from delegate.repo import get_default_branch, get_task_worktree_path

            task_id = args["task_id"]
            task = get_task(hc_home, team, task_id)

            if task.get("status") != "in_approval":
                return _error_result(
                    f"Task {format_task_id(task_id)} is not in_approval "
                    f"(status: {task.get('status')})"
                )

            # Get diff
            try:
                diff_dict = _get_task_diff(hc_home, team, task_id)
            except Exception as exc:
                return _error_result(f"Failed to get diff: {exc}")

            # Combine multi-repo diffs
            parts = []
            for repo_name, diff_text in diff_dict.items():
                if len(diff_dict) > 1:
                    parts.append(f"# Repo: {repo_name}\n{diff_text}")
                else:
                    parts.append(diff_text)
            combined_diff = "\n\n".join(parts)

            # Check sensitive files
            sensitive = check_sensitive_files(combined_diff)

            # Check if branch is behind main
            rebase_needed = False
            repos = task.get("repo", [])
            for repo_name in repos:
                wt_path = get_task_worktree_path(hc_home, team, repo_name, task_id)
                if not wt_path.exists():
                    continue
                wt_str = str(wt_path)
                db = get_default_branch(wt_str)
                # merge-base of branch HEAD vs main HEAD
                mb = subprocess.run(
                    ["git", "merge-base", db, "HEAD"],
                    cwd=wt_str, capture_output=True, text=True, timeout=30,
                )
                main_head = subprocess.run(
                    ["git", "rev-parse", db],
                    cwd=wt_str, capture_output=True, text=True, timeout=30,
                )
                if (mb.returncode == 0 and main_head.returncode == 0
                        and mb.stdout.strip() != main_head.stdout.strip()):
                    rebase_needed = True
                    break

            # Truncate large diffs
            if len(combined_diff) > MAX_DIFF_CHARS:
                combined_diff = combined_diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated at 100K chars ...]"

            result = {
                "task_id": task_id,
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "diff": combined_diff,
                "sensitive_files": sensitive,
                "rebase_needed": rebase_needed,
            }
            return _json_result(result)

        except Exception as e:
            logger.exception("task_diff failed")
            return _error_result(str(e))

    @tool(
        "task_approve",
        "Approve a task that is in_approval status. Sets the verdict and marks "
        "the task for auto-merge.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID to approve"},
                "summary": {"type": "string", "description": "Brief approval summary"},
            },
            "required": ["task_id", "summary"],
        },
    )
    async def task_approve(args: dict) -> dict:
        try:
            from delegate.task import get_task, update_task, format_task_id
            from delegate.review import get_current_review, set_verdict
            from delegate.chat import log_event as _log_event

            task_id = args["task_id"]
            summary = args.get("summary", "")
            task = get_task(hc_home, team, task_id)

            if task.get("status") != "in_approval":
                return _error_result(
                    f"Task {format_task_id(task_id)} is not in_approval "
                    f"(status: {task.get('status')})"
                )

            attempt = task.get("review_attempt", 0)
            if attempt > 0:
                set_verdict(
                    hc_home, team, task_id, attempt, "approved",
                    summary=summary, reviewer=agent,
                )

            update_task(hc_home, team, task_id, approval_status="approved")

            _log_event(
                hc_home, team,
                f"{format_task_id(task_id)} approved by {agent}: {summary[:80]}",
                task_id=task_id,
            )

            return _text_result(f"Task {format_task_id(task_id)} approved")

        except Exception as e:
            logger.exception("task_approve failed")
            return _error_result(str(e))

    @tool(
        "task_reject",
        "Reject a task that is in_approval status. Sets the verdict, changes "
        "status to rejected, and notifies the manager.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID to reject"},
                "reason": {"type": "string", "description": "Specific rejection reason with actionable feedback"},
            },
            "required": ["task_id", "reason"],
        },
    )
    async def task_reject(args: dict) -> dict:
        try:
            from delegate.task import get_task, update_task, change_status, format_task_id
            from delegate.review import get_current_review, set_verdict
            from delegate.notify import notify_rejection
            from delegate.chat import log_event as _log_event

            task_id = args["task_id"]
            reason = args.get("reason", "")
            task = get_task(hc_home, team, task_id)

            if task.get("status") != "in_approval":
                return _error_result(
                    f"Task {format_task_id(task_id)} is not in_approval "
                    f"(status: {task.get('status')})"
                )

            attempt = task.get("review_attempt", 0)
            if attempt > 0:
                set_verdict(
                    hc_home, team, task_id, attempt, "rejected",
                    summary=reason, reviewer=agent,
                )

            update_task(
                hc_home, team, task_id,
                rejection_reason=f"Reviewer {agent}: {reason}",
                approval_status="rejected",
            )
            change_status(hc_home, team, task_id, "rejected")

            notify_rejection(hc_home, team, task, reason=f"Reviewer {agent} rejected: {reason}")

            _log_event(
                hc_home, team,
                f"{format_task_id(task_id)} rejected by {agent}: {reason[:80]}",
                task_id=task_id,
            )

            return _text_result(f"Task {format_task_id(task_id)} rejected")

        except Exception as e:
            logger.exception("task_reject failed")
            return _error_result(str(e))

    # -----------------------------------------------------------------------
    # Artifact management tools
    # -----------------------------------------------------------------------

    from delegate.adapters import DEFAULT_ARTIFACT_CATEGORIES as _art_cats
    _art_category_list = list(_art_cats.keys())

    @tool(
        "artifact_save",
        "Save a file as a named artifact for a task (e.g. checkpoint, report, data). "
        "Copies the file from the worktree to the persistent artifacts directory. "
        "Artifacts survive worktree teardown.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID to save artifact for"},
                "source_path": {"type": "string", "description": "Absolute path to the file to save"},
                "artifact_name": {"type": "string", "description": "Name for the artifact"},
                "category": {
                    "type": "string",
                    "description": f"Category: one of {_art_category_list} (determines subdirectory)",
                    "enum": _art_category_list,
                },
            },
            "required": ["task_id", "source_path", "artifact_name", "category"],
        },
    )
    async def artifact_save(args: dict) -> dict:
        try:
            import shutil
            from delegate.paths import task_artifacts_dir, ARTIFACT_CATEGORIES

            task_id = args["task_id"]
            source = Path(args["source_path"])
            name = args["artifact_name"]
            category = args["category"]

            if not source.exists():
                return _error_result(f"Source file not found: {source}")

            art_dir = task_artifacts_dir(hc_home, team, task_id)
            dest_dir = art_dir / ARTIFACT_CATEGORIES.get(category, category)
            dest_dir.mkdir(parents=True, exist_ok=True)

            dest = dest_dir / name
            if source.is_dir():
                shutil.copytree(str(source), str(dest), dirs_exist_ok=True)
            else:
                shutil.copy2(str(source), str(dest))

            # Update manifest
            manifest = _load_artifact_manifest(art_dir)
            manifest_path = art_dir / "manifest.json"

            import time as _time
            manifest.append({
                "name": name,
                "category": category,
                "path": str(dest),
                "size_bytes": dest.stat().st_size if dest.is_file() else -1,
                "saved_at": _time.time(),
                "saved_by": agent,
            })
            manifest_path.write_text(json.dumps(manifest, indent=2, default=str))

            return _json_result({
                "artifact": name,
                "path": str(dest),
                "message": f"Artifact '{name}' saved to {dest}",
            })
        except Exception as e:
            logger.exception("artifact_save failed")
            return _error_result(str(e))

    @tool(
        "artifact_list",
        "List all saved artifacts for a task.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    )
    async def artifact_list(args: dict) -> dict:
        try:
            from delegate.paths import task_artifacts_dir

            art_dir = task_artifacts_dir(hc_home, team, args["task_id"])
            manifest = _load_artifact_manifest(art_dir)
            if not manifest:
                return _text_result(f"No artifacts for T{args['task_id']:04d}")
            return _json_result(manifest)
        except Exception as e:
            logger.exception("artifact_list failed")
            return _error_result(str(e))

    @tool(
        "artifact_path",
        "Get the absolute path to a saved artifact (for use in scripts or follow-up tasks).",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task ID"},
                "artifact_name": {"type": "string", "description": "Name of the artifact"},
            },
            "required": ["task_id", "artifact_name"],
        },
    )
    async def artifact_path(args: dict) -> dict:
        try:
            from delegate.paths import task_artifacts_dir

            art_dir = task_artifacts_dir(hc_home, team, args["task_id"])
            manifest = _load_artifact_manifest(art_dir)
            if not manifest:
                return _error_result(f"No artifacts for T{args['task_id']:04d}")
            for entry in manifest:
                if entry["name"] == args["artifact_name"]:
                    return _json_result({"path": entry["path"], "name": entry["name"]})
            return _error_result(f"Artifact '{args['artifact_name']}' not found")
        except Exception as e:
            logger.exception("artifact_path failed")
            return _error_result(str(e))

    # -----------------------------------------------------------------------
    # Background process tools (long-running commands for researchers)
    # -----------------------------------------------------------------------

    @tool(
        "run_background",
        "Launch a long-running command as a background process (e.g. GPU training, "
        "data processing). Returns a handle to check status later. Use this for any "
        "command expected to run longer than 2 minutes.",
        {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run (e.g. 'python train.py --epochs 50')",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (absolute path). Defaults to agent's workspace.",
                },
                "label": {
                    "type": "string",
                    "description": "Short human-readable label for this process (e.g. 'experiment v3')",
                },
                "max_hours": {
                    "type": "number",
                    "description": "Maximum runtime in hours before auto-kill (default: 4)",
                },
            },
            "required": ["command"],
        },
    )
    async def run_background(args: dict) -> dict:
        try:
            from delegate.background import launch
            from delegate.paths import agent_dir as _agent_dir

            # Enforce the same sandbox deny-list as the Bash tool.
            cmd = args["command"]
            cmd_upper = cmd.upper()
            for pattern in _denied_patterns:
                if pattern.upper() in cmd_upper:
                    return _error_result(
                        f"Command blocked by sandbox policy: contains '{pattern}'"
                    )

            ad = _agent_dir(hc_home, team, agent)
            max_runtime = (args.get("max_hours") or 4) * 3600

            info = launch(
                ad,
                args["command"],
                cwd=args.get("cwd"),
                label=args.get("label", ""),
                max_runtime=max_runtime,
            )
            return _json_result({
                "handle": info.handle,
                "pid": info.pid,
                "label": info.label,
                "message": (
                    f"Background process started (handle={info.handle}). "
                    f"Use check_background to monitor progress."
                ),
            })
        except Exception as e:
            logger.exception("run_background failed")
            return _error_result(str(e))

    @tool(
        "check_background",
        "Check the status of a background process. Returns state "
        "(running/completed/failed/cancelled/timed_out), exit code, "
        "elapsed time, and a brief tail of stdout/stderr. "
        "Prefer grep on the log file for specific metrics over increasing tail_lines.",
        {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Process handle returned by run_background",
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "Lines from end of output (default: 15, keep low to save tokens)",
                },
            },
            "required": ["handle"],
        },
    )
    async def check_background(args: dict) -> dict:
        try:
            import asyncio
            from delegate.background import check, tail as bg_tail, DEFAULT_TAIL_LINES
            from delegate.paths import agent_dir as _agent_dir

            ad = _agent_dir(hc_home, team, agent)
            handle = args["handle"]

            # check() may block (time.sleep during timeout kill)
            info = await asyncio.to_thread(check, ad, handle)
            if info is None:
                return _error_result(f"Unknown background process handle: {handle}")

            n = args.get("tail_lines") or DEFAULT_TAIL_LINES
            logs = bg_tail(ad, handle, n=n)

            import time
            elapsed = (info.ended_at or time.time()) - info.started_at

            result = {
                "handle": info.handle,
                "state": info.state,
                "exit_code": info.exit_code,
                "label": info.label,
                "elapsed_seconds": round(elapsed, 1),
                "elapsed_human": _format_duration(elapsed),
                "stdout_tail": logs.get("stdout", ""),
                "stderr_tail": logs.get("stderr", ""),
            }
            return _json_result(result)
        except Exception as e:
            logger.exception("check_background failed")
            return _error_result(str(e))

    @tool(
        "cancel_background",
        "Cancel (kill) a running background process.",
        {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Process handle returned by run_background",
                },
            },
            "required": ["handle"],
        },
    )
    async def cancel_background(args: dict) -> dict:
        try:
            import asyncio
            from delegate.background import cancel
            from delegate.paths import agent_dir as _agent_dir

            ad = _agent_dir(hc_home, team, agent)
            # cancel() calls _kill_process which blocks with time.sleep(0.5)
            info = await asyncio.to_thread(cancel, ad, args["handle"])
            if info is None:
                return _error_result(f"Unknown background process handle: {args['handle']}")
            return _text_result(f"Process {info.handle} cancelled (was pid {info.pid})")
        except Exception as e:
            logger.exception("cancel_background failed")
            return _error_result(str(e))

    @tool(
        "list_background",
        "List all background processes (running and completed) for this agent.",
        {},
    )
    async def list_background(args: dict) -> dict:
        try:
            from delegate.background import list_all
            from delegate.paths import agent_dir as _agent_dir
            import time

            ad = _agent_dir(hc_home, team, agent)
            procs = list_all(ad)
            result = []
            for info in procs:
                elapsed = (info.ended_at or time.time()) - info.started_at
                result.append({
                    "handle": info.handle,
                    "state": info.state,
                    "label": info.label,
                    "exit_code": info.exit_code,
                    "elapsed": _format_duration(elapsed),
                    "command": info.command[:120],
                })
            if not result:
                return _text_result("No background processes.")
            return _json_result(result)
        except Exception as e:
            logger.exception("list_background failed")
            return _error_result(str(e))

    # -----------------------------------------------------------------------
    # System resource tools
    # -----------------------------------------------------------------------

    @tool(
        "check_resources",
        "Check live system resource utilization: CPU, RAM, disk, and per-GPU "
        "stats (utilization %, VRAM, temperature, power). Returns structured "
        "JSON — use this instead of parsing nvidia-smi or /proc manually. "
        "Call before launching compute-heavy work to pick the best GPU or "
        "verify available memory.",
        {"type": "object", "properties": {}},
    )
    async def check_resources(args: dict) -> dict:
        try:
            import asyncio
            data = await asyncio.to_thread(_collect_live_resources)
            return _json_result(data)
        except Exception as e:
            logger.exception("check_resources failed")
            return _error_result(str(e))

    return [
        mailbox_send,
        mailbox_inbox,
        task_create,
        task_list,
        task_show,
        task_assign,
        task_status,
        task_comment,
        task_cancel,
        task_attach,
        task_detach,
        repo_list,
        rebase_to_main,
        task_diff,
        task_approve,
        task_reject,
        artifact_save,
        artifact_list,
        artifact_path,
        run_background,
        check_background,
        cancel_background,
        list_background,
        check_resources,
    ]


def create_agent_mcp_server(hc_home: Path, team: str, agent: str):
    """Create an MCP server with all agent tools wired to the given context.

    Returns an MCP server object ready for ``Telephone(mcp_servers={...})``,
    or ``None`` if the SDK is not available (e.g. in test environments).
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError:
        logger.debug("claude_agent_sdk not available — skipping MCP server creation")
        return None

    tools = build_agent_tools(hc_home, team, agent)
    return create_sdk_mcp_server("delegate", tools=tools)
