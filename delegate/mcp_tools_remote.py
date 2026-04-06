"""HTTP-backed MCP tools for satellite agents.

Same tool signatures as ``mcp_tools.py:build_agent_tools()`` but every
closure makes HTTP calls to the coordinator's ``/internal/*`` endpoints
instead of directly accessing the local database.

Agent identity (team, agent) is baked into closures — same security model
as the local version.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


_MAX_RESULT_BYTES = 800_000


def _json_result(data: Any) -> dict:
    text = json.dumps(data, indent=2, default=str)
    if len(text) > _MAX_RESULT_BYTES and isinstance(data, list) and len(data) > 1:
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
    return {"content": [{"type": "text", "text": f"ERROR: {msg}"}], "isError": True}


def build_remote_agent_tools(
    coordinator_url: str,
    auth_token: str,
    team: str,
    agent: str,
) -> list:
    """Build MCP tool definitions that proxy calls to the coordinator.

    Each tool makes HTTP requests to ``coordinator_url/internal/*``.
    """
    from claude_agent_sdk import tool

    client = httpx.Client(
        base_url=coordinator_url,
        headers={"Authorization": f"Bearer {auth_token}"},
        timeout=60.0,
    )

    def _post(path: str, data: dict | None = None) -> dict:
        resp = client.post(path, json=data or {})
        resp.raise_for_status()
        return resp.json()

    def _get(path: str, params: dict | None = None) -> dict:
        resp = client.get(path, params=params or {})
        resp.raise_for_status()
        return resp.json()

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
                    "description": "Optional task ID to associate the message with.",
                },
            },
            "required": ["recipient", "message"],
        },
    )
    async def mailbox_send(args: dict) -> dict:
        try:
            task_id = args.get("task_id")
            if task_id == 0:
                task_id = None
            result = _post("/internal/mailbox/send", {
                "team": team,
                "sender": agent,
                "recipient": args["recipient"],
                "message": args["message"],
                "task_id": task_id,
            })
            text = f"Message sent to {args['recipient']}"
            if task_id:
                text += f" (task T{task_id:04d})"
            return _text_result(text)
        except Exception as e:
            logger.exception("remote mailbox_send failed")
            return _error_result(str(e))

    @tool(
        "mailbox_inbox",
        "Check your inbox for unread messages.",
        {},
    )
    async def mailbox_inbox(args: dict) -> dict:
        try:
            result = _get("/internal/mailbox/inbox", {
                "team": team,
                "agent": agent,
            })
            messages = result.get("messages", [])
            if not messages:
                return _text_result("No unread messages.")
            return _json_result(messages)
        except Exception as e:
            logger.exception("remote mailbox_inbox failed")
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
            },
            "required": ["title"],
        },
    )
    async def task_create(args: dict) -> dict:
        try:
            payload: dict[str, Any] = {
                "team": team,
                "title": args["title"],
                "assignee": agent,
            }
            if args.get("description"):
                payload["description"] = args["description"]
            if args.get("priority"):
                payload["priority"] = args["priority"]
            if args.get("repo"):
                payload["repo"] = args["repo"]
            if args.get("depends_on"):
                try:
                    deps = [int(x.strip()) for x in args["depends_on"].split(",")]
                    payload["depends_on"] = deps
                except ValueError:
                    return _error_result("depends_on must be comma-separated integers")
            result = _post("/internal/task/create", payload)
            return _json_result(result)
        except Exception as e:
            logger.exception("remote task_create failed")
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
            from delegate.task import TERMINAL_STATUSES, SUMMARY_FIELDS

            params: dict[str, str] = {"team": team}
            if args.get("status"):
                params["status"] = args["status"]
            if args.get("assignee"):
                params["assignee"] = args["assignee"]
            tasks = _get("/internal/task/list", params)

            # Exclude done/cancelled by default (can't push to SQL over HTTP)
            if not args.get("status"):
                tasks = [t for t in tasks if t.get("status") not in TERMINAL_STATUSES]

            # Return summary-only to stay within SDK buffer limits
            summaries = [
                {k: t[k] for k in SUMMARY_FIELDS if k in t}
                for t in tasks
            ]
            return _json_result(summaries)
        except Exception as e:
            logger.exception("remote task_list failed")
            return _error_result(str(e))

    @tool(
        "task_show",
        "Show detailed information about a specific task.",
        {"task_id": int},
    )
    async def task_show(args: dict) -> dict:
        try:
            result = _get("/internal/task/show", {
                "team": team,
                "task_id": str(args["task_id"]),
            })
            return _json_result(result)
        except Exception as e:
            logger.exception("remote task_show failed")
            return _error_result(str(e))

    @tool(
        "task_assign",
        "Assign a task to a team member.",
        {"task_id": int, "assignee": str},
    )
    async def task_assign(args: dict) -> dict:
        try:
            _post("/internal/task/assign", {
                "team": team,
                "task_id": args["task_id"],
                "assignee": args["assignee"],
            })
            return _text_result(f"Task T{args['task_id']:04d} assigned to {args['assignee']}")
        except Exception as e:
            logger.exception("remote task_assign failed")
            return _error_result(str(e))

    @tool(
        "task_status",
        "Change the status of a task (e.g. 'in_progress', 'in_review', 'done').",
        {"task_id": int, "new_status": str},
    )
    async def task_status(args: dict) -> dict:
        try:
            _post("/internal/task/status", {
                "team": team,
                "task_id": args["task_id"],
                "new_status": args["new_status"],
            })
            return _text_result(f"Task T{args['task_id']:04d} status changed to {args['new_status']}")
        except Exception as e:
            logger.exception("remote task_status failed")
            return _error_result(str(e))

    @tool(
        "task_comment",
        "Add a durable comment/note to a task (specs, findings, decisions).",
        {"task_id": int, "body": str},
    )
    async def task_comment(args: dict) -> dict:
        try:
            _post("/internal/task/comment", {
                "team": team,
                "task_id": args["task_id"],
                "author": agent,
                "body": args["body"],
            })
            return _text_result(f"Comment added to T{args['task_id']:04d}")
        except Exception as e:
            logger.exception("remote task_comment failed")
            return _error_result(str(e))

    @tool(
        "task_cancel",
        "Cancel a task (manager only — cleans up worktrees and branches).",
        {"task_id": int},
    )
    async def task_cancel(args: dict) -> dict:
        try:
            _post("/internal/task/cancel", {
                "team": team,
                "task_id": args["task_id"],
            })
            return _text_result(f"Task T{args['task_id']:04d} cancelled")
        except Exception as e:
            logger.exception("remote task_cancel failed")
            return _error_result(str(e))

    @tool(
        "task_attach",
        "Attach a file to a task.",
        {"task_id": int, "file_path": str},
    )
    async def task_attach(args: dict) -> dict:
        try:
            _post("/internal/task/attach", {
                "team": team,
                "task_id": args["task_id"],
                "file_path": args["file_path"],
            })
            return _text_result(f"Attached {args['file_path']} to T{args['task_id']:04d}")
        except Exception as e:
            logger.exception("remote task_attach failed")
            return _error_result(str(e))

    @tool(
        "task_detach",
        "Remove a file attachment from a task.",
        {"task_id": int, "file_path": str},
    )
    async def task_detach(args: dict) -> dict:
        try:
            _post("/internal/task/detach", {
                "team": team,
                "task_id": args["task_id"],
                "file_path": args["file_path"],
            })
            return _text_result(f"Detached {args['file_path']} from T{args['task_id']:04d}")
        except Exception as e:
            logger.exception("remote task_detach failed")
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
            result = _get("/internal/repo/list", {"team": team})
            repos = result.get("repos", [])
            if not repos:
                return _text_result("No repositories registered.")
            return _json_result(repos)
        except Exception as e:
            logger.exception("remote repo_list failed")
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
    ]


def create_remote_mcp_server(
    coordinator_url: str,
    auth_token: str,
    team: str,
    agent: str,
):
    """Create an MCP server with HTTP-backed tools for a satellite agent.

    Returns an MCP server object ready for ``Telephone(mcp_servers={...})``,
    or None if the SDK is not available.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError:
        logger.debug("claude_agent_sdk not available — skipping remote MCP server")
        return None

    tools = build_remote_agent_tools(coordinator_url, auth_token, team, agent)
    return create_sdk_mcp_server("delegate", tools=tools)
