"""Workflow context — builtins available to Stage hooks.

The ``Context`` object is passed to every ``enter``, ``exit``,
``assign``, and ``action`` method.  It provides:

- **Task state** — ``ctx.task`` (read), ``ctx.task.update(**fields)``.
- **Routing** — ``ctx.agents()``, ``ctx.pick()``, ``ctx.manager``, ``ctx.human``.
- **Communication** — ``ctx.notify()``, ``ctx.log()``.
- **Metadata** — ``ctx.get_metadata()``, ``ctx.set_metadata()``.
- **Control flow** — ``ctx.require()``, ``ctx.fail()``.

Git-specific operations (worktrees, reviews, merge) live in
``delegate.workflows.git`` and are mixed in automatically.

All methods pre-bind ``hc_home``, ``team``, and ``task_id`` so the
workflow author never touches file paths or database details.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from delegate.config import SYSTEM_USER
from delegate.workflow import GateError, ActionError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TaskView — read-only view + update method
# ---------------------------------------------------------------------------

class TaskView:
    """Read-only view of a task with an ``update()`` method for mutations."""

    def __init__(self, task: dict, hc_home: Path, team: str):
        self._task = task
        self._hc_home = hc_home
        self._team = team

    def __getattr__(self, name: str) -> Any:
        try:
            return self._task[name]
        except KeyError:
            raise AttributeError(
                f"Task has no field '{name}'. "
                f"Available: {sorted(self._task.keys())}"
            )

    def __getitem__(self, key: str) -> Any:
        return self._task[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._task.get(key, default)

    @property
    def has_commits(self) -> bool:
        """True if any repo has commits beyond base_sha."""
        commits = self._task.get("commits", {})
        if not commits:
            return False
        return any(bool(shas) for shas in commits.values())

    def update(self, **kwargs: Any) -> None:
        """Update task fields in the database and refresh the local view."""
        from delegate.task import update_task
        updated = update_task(self._hc_home, self._team, self._task["id"], **kwargs)
        self._task.update(updated)

    def to_dict(self) -> dict:
        """Return the underlying task dict (copy)."""
        return dict(self._task)


# ---------------------------------------------------------------------------
# AgentInfo — lightweight agent descriptor
# ---------------------------------------------------------------------------

@dataclass
class AgentInfo:
    """Lightweight agent info for routing decisions."""
    name: str
    role: str
    active_task_count: int = 0


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class Context:
    """Context object passed to workflow stage hooks.

    Pre-binds ``hc_home``, ``team``, and task so workflow authors
    interact with clean, high-level methods.

    Git-specific methods are mixed in from ``delegate.workflows.git``
    at construction time (see ``__init_subclass__``).
    """

    def __init__(self, hc_home: Path, team: str, task_dict: dict):
        self._hc_home = hc_home
        self._team = team
        self.task = TaskView(task_dict, hc_home, team)

    # ── Properties ──────────────────────────────────────────────

    @property
    def manager(self) -> str:
        """The team's manager agent name."""
        from delegate.bootstrap import get_member_by_role
        name = get_member_by_role(self._hc_home, self._team, "manager")
        return name or "delegate"

    @property
    def human(self) -> str:
        """The default human member name."""
        from delegate.config import get_default_human
        return get_default_human(self._hc_home) or "human"

    @property
    def system(self) -> str:
        """The system user identity (for automated events)."""
        return SYSTEM_USER

    @property
    def boss(self) -> str:
        """Deprecated — use ``human`` instead."""
        return self.human

    # ── Agent routing ───────────────────────────────────────────

    def agents(self, role: str | None = None) -> list[AgentInfo]:
        """List agents on the team, optionally filtered by role.

        Returns ``AgentInfo`` objects with ``name``, ``role``, and
        ``active_task_count``.
        """
        import yaml
        from delegate.paths import agents_dir as _agents_dir
        from delegate.task import list_tasks

        agents_root = _agents_dir(self._hc_home, self._team)
        if not agents_root.is_dir():
            return []

        # Get active task counts
        active_tasks = list_tasks(self._hc_home, self._team, status="in_progress")
        task_counts: dict[str, int] = {}
        for t in active_tasks:
            a = t.get("assignee", "")
            if a:
                task_counts[a] = task_counts.get(a, 0) + 1

        result = []
        for d in sorted(agents_root.iterdir()):
            if not d.is_dir():
                continue
            state_file = d / "state.yaml"
            if not state_file.is_file():
                continue
            try:
                state = yaml.safe_load(state_file.read_text()) or {}
            except Exception:
                continue
            agent_name = d.name
            agent_role = state.get("role", "engineer")
            if role and agent_role != role:
                continue
            result.append(AgentInfo(
                name=agent_name,
                role=agent_role,
                active_task_count=task_counts.get(agent_name, 0),
            ))

        return result

    def pick(
        self,
        role: str | None = None,
        exclude: str | list[str] | None = None,
    ) -> str | None:
        """Pick the least-loaded agent matching criteria.

        Returns the agent name, or None if no candidates.
        """
        candidates = self.agents(role=role)

        if exclude:
            if isinstance(exclude, str):
                exclude = [exclude]
            exclude_set = set(exclude)
            candidates = [a for a in candidates if a.name not in exclude_set]

        if not candidates:
            return None

        return min(candidates, key=lambda a: a.active_task_count).name

    # ── Communication ───────────────────────────────────────────

    def notify(self, recipient: str, body: str, task_id: int | None = None,
               sender: str | None = None) -> int | None:
        """Send a message to an agent's inbox.

        *sender* defaults to ``system`` for automated notifications.
        Returns the message ID, or None if delivery failed.
        """
        from delegate.mailbox import Message, deliver

        if task_id is None:
            task_id = self.task.id

        if sender is None:
            sender = SYSTEM_USER

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        msg = Message(
            sender=sender,
            recipient=recipient,
            time=now,
            body=body,
            task_id=task_id,
        )

        try:
            return deliver(self._hc_home, self._team, msg)
        except Exception as exc:
            logger.warning("Failed to deliver notification to %s: %s", recipient, exc)
            return None

    def log(self, message: str, task_id: int | None = None) -> int:
        """Log a system event to the team's activity feed.

        Returns the event ID.
        """
        from delegate.chat import log_event

        if task_id is None:
            task_id = self.task.id

        return log_event(self._hc_home, self._team, message, task_id=task_id)

    # ── Metadata ────────────────────────────────────────────────

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Read a value from the task's free-form metadata dict."""
        meta = self.task.get("metadata", {})
        if isinstance(meta, str):
            import json
            meta = json.loads(meta) if meta else {}
        return meta.get(key, default)

    def set_metadata(self, key: str, value: Any) -> None:
        """Set a value in the task's free-form metadata dict.

        Performs a partial merge — existing keys are preserved.
        """
        meta = self.task.get("metadata", {})
        if isinstance(meta, str):
            import json
            meta = json.loads(meta) if meta else {}
        meta[key] = value
        self.task.update(metadata=meta)

    # ── Control flow ────────────────────────────────────────────

    def require(self, condition: Any, message: str) -> None:
        """Assert a precondition.  Raises ``GateError`` if falsy."""
        if not condition:
            raise GateError(message)

    def fail(self, message: str) -> None:
        """Transition the task to the error state, assigned to the human.

        Raises ``ActionError`` which the runtime catches and handles.
        """
        raise ActionError(message)
