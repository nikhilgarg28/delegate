"""Workflow engine — declarative task lifecycle as Python classes.

A *workflow* defines the stages a task moves through (e.g. todo →
in_progress → in_review → merging → done) and the behaviour at each
stage (assignment, gates, actions, cleanup).

Authors define stages as ``Stage`` subclasses and group them into a
workflow with the ``@workflow`` decorator.  Delegate copies the
workflow file into its home directory at ``workflow add`` time and
loads it via ``importlib`` at runtime.

Minimal example::

    from delegate.workflow import Stage, workflow

    class Todo(Stage):
        label = "To Do"

    class InProgress(Stage):
        label = "In Progress"

    class Done(Stage):
        label = "Done"
        terminal = True

    class Cancelled(Stage):
        label = "Cancelled"
        terminal = True

    @workflow(name="minimal", version=1)
    def minimal():
        return [Todo, InProgress, Done, Cancelled]

Stage hooks
-----------
``enter(ctx)``  — called **before** the task enters this stage.
                  Use for gates (``ctx.require(…)``), setup (worktrees), etc.
                  Raising ``GateError`` or ``ActionError`` blocks the transition.

``exit(ctx)``   — called when the task is **leaving** this stage.
                  Use for cleanup.

``assign(ctx)`` — called to determine the assignee for this stage.
                  Return an agent name string, or ``None`` to leave assignee
                  unchanged.

``action(ctx)`` — called for ``auto = True`` stages.
                  Must return the *next stage class* (a ``Stage`` subclass).
                  The runtime will transition the task automatically.

Class attributes
----------------
``label``    — human-readable display name (required).
``terminal`` — if True, no transitions out are allowed (default False).
``auto``     — if True, the ``action()`` method is called automatically
               by the runtime without dispatching an agent turn (default False).

Usage from the runtime::

    wf = load_workflow(hc_home, team, task["workflow"], task["workflow_version"])
    stage_cls = wf.stage_map[task["status"]]
    stage = stage_cls()
    stage.enter(ctx)
"""

from __future__ import annotations

import copy
import importlib.util
import logging
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GateError(Exception):
    """Raised by ``enter()`` to block a transition (precondition failed)."""


class ActionError(Exception):
    """Raised by ``action()`` or ``enter()`` on unrecoverable failures."""


# ---------------------------------------------------------------------------
# Stage base class
# ---------------------------------------------------------------------------

class Stage:
    """Base class for workflow stages.

    Subclass and override ``enter``, ``exit``, ``assign``, and/or
    ``action`` to define behaviour.  Set class attributes ``label``,
    ``terminal``, and ``auto`` as needed.
    """

    # ── Required class attribute ──
    label: str = ""

    # ── Optional class attributes ──
    terminal: bool = False
    auto: bool = False

    # The stage's canonical key — derived from the class name at
    # registration time (lowercased, underscored).  E.g. ``InProgress``
    # → ``in_progress``.  Set automatically by ``@workflow``.
    _key: str = ""

    # Valid next-stage keys — set automatically by ``@workflow`` from
    # the ordering + ``terminal`` flags.
    _transitions: set[str] = set()

    # ── Hooks (override in subclasses) ──

    def enter(self, ctx: "Context") -> None:  # noqa: F821 (forward ref)
        """Called before the task enters this stage.

        Override to add gates (``ctx.require(…)``) or setup logic.
        Raise ``GateError`` to block the transition.
        """

    def exit(self, ctx: "Context") -> None:  # noqa: F821
        """Called when the task leaves this stage.

        Override to add cleanup logic.
        """

    def assign(self, ctx: "Context") -> str | None:  # noqa: F821
        """Return the agent name to assign, or None to keep current.

        Override to implement custom assignment logic.
        """
        return None

    def action(self, ctx: "Context") -> type["Stage"] | None:  # noqa: F821
        """For ``auto = True`` stages: run the automated action.

        Must return the next Stage *class* to transition to, or None
        to stay in the current stage (retry on next cycle).
        """
        return None


# ---------------------------------------------------------------------------
# WorkflowDef — the compiled, validated workflow definition
# ---------------------------------------------------------------------------

@dataclass
class WorkflowDef:
    """A validated, loaded workflow definition."""

    name: str
    version: int
    stages: list[type[Stage]]       # ordered list of stage classes
    stage_map: dict[str, type[Stage]]  # key → stage class
    transitions: dict[str, set[str]]   # key → set of valid next-stage keys
    initial_stage: str               # key of the first (initial) stage
    terminal_stages: set[str]        # keys of terminal stages
    source_path: str = ""            # original file path (informational)

    def get_stage(self, key: str) -> type[Stage]:
        """Look up a stage class by key.  Raises KeyError if not found."""
        if key not in self.stage_map:
            raise KeyError(
                f"Stage '{key}' not found in workflow '{self.name}' v{self.version}. "
                f"Valid stages: {sorted(self.stage_map.keys())}"
            )
        return self.stage_map[key]

    def validate_transition(self, from_key: str, to_key: str) -> None:
        """Raise ValueError if the transition is not allowed."""
        if from_key in self.terminal_stages:
            raise ValueError(
                f"Cannot transition from terminal stage '{from_key}' "
                f"in workflow '{self.name}' v{self.version}."
            )
        allowed = self.transitions.get(from_key, set())
        if to_key not in allowed:
            raise ValueError(
                f"Invalid transition: '{from_key}' → '{to_key}' in workflow "
                f"'{self.name}' v{self.version}. "
                f"Allowed: {sorted(allowed)}"
            )

    def is_terminal(self, key: str) -> bool:
        return key in self.terminal_stages

    def format_graph(self) -> str:
        """Return a human-readable text representation of the workflow graph."""
        lines = [f"Workflow: {self.name} v{self.version}"]
        lines.append(f"Stages ({len(self.stages)}):")
        for cls in self.stages:
            key = cls._key
            flags = []
            if key == self.initial_stage:
                flags.append("initial")
            if cls.terminal:
                flags.append("terminal")
            if cls.auto:
                flags.append("auto")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            trans = sorted(self.transitions.get(key, set()))
            arrow = " → " + ", ".join(trans) if trans else ""
            lines.append(f"  {key} ({cls.label}){flag_str}{arrow}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# @workflow decorator and global registry
# ---------------------------------------------------------------------------

# Module-level registry populated by @workflow decorator
_workflow_registry: dict[str, WorkflowDef] = {}


def _class_name_to_key(name: str) -> str:
    """Convert CamelCase class name to snake_case key.

    Examples:
        InProgress → in_progress
        Todo → todo
        MergeFailed → merge_failed
        QAReview → qa_review
    """
    import re
    # Insert underscore between lowercase/digit and uppercase
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    # Insert underscore between consecutive uppercase followed by lowercase
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return s.lower()


def _build_transitions(stages: list[type[Stage]]) -> dict[str, set[str]]:
    """Build the default transition map from stage ordering.

    Default rule: each non-terminal stage can transition to:
    - The next stage in the list
    - Any terminal stage (cancelled, error, etc.)

    Stages can override by setting ``_transitions`` explicitly.
    """
    keys = [s._key for s in stages]
    terminal_keys = {s._key for s in stages if s.terminal}
    transitions: dict[str, set[str]] = {}

    for i, stage_cls in enumerate(stages):
        key = stage_cls._key
        if stage_cls.terminal:
            transitions[key] = set()  # no transitions out of terminal
            continue

        if stage_cls._transitions:
            # Explicit transitions defined by the stage class
            transitions[key] = set(stage_cls._transitions)
        else:
            # Default: next stage + all terminal stages
            allowed = set(terminal_keys)
            if i + 1 < len(stages):
                next_key = keys[i + 1]
                # Don't add terminal stages twice
                allowed.add(next_key)
            transitions[key] = allowed

    return transitions


def _validate_workflow(
    name: str,
    version: int,
    stages: list[type[Stage]],
) -> WorkflowDef:
    """Validate and compile a workflow definition."""
    if not stages:
        raise ValueError(f"Workflow '{name}' has no stages.")

    if version < 1:
        raise ValueError(f"Workflow '{name}' version must be >= 1, got {version}.")

    # Assign keys to stage classes
    seen_keys: set[str] = set()
    for cls in stages:
        key = _class_name_to_key(cls.__name__)
        if key in seen_keys:
            raise ValueError(
                f"Workflow '{name}': duplicate stage key '{key}' "
                f"(from class {cls.__name__})."
            )
        seen_keys.add(key)
        cls._key = key

    # Build stage map
    stage_map = {cls._key: cls for cls in stages}

    # Build transitions
    transitions = _build_transitions(stages)

    # Validate: must have at least one terminal stage
    terminal_stages = {cls._key for cls in stages if cls.terminal}
    if not terminal_stages:
        raise ValueError(
            f"Workflow '{name}' has no terminal stages. "
            f"At least one stage must have terminal = True."
        )

    # Validate: every non-terminal stage must have at least one transition
    for cls in stages:
        if not cls.terminal and not transitions.get(cls._key):
            raise ValueError(
                f"Workflow '{name}': non-terminal stage '{cls._key}' "
                f"has no transitions."
            )

    # Validate labels
    for cls in stages:
        if not cls.label:
            raise ValueError(
                f"Workflow '{name}': stage '{cls._key}' (class {cls.__name__}) "
                f"has no label."
            )

    # Validate transition targets exist
    for key, targets in transitions.items():
        for target in targets:
            if target not in stage_map:
                raise ValueError(
                    f"Workflow '{name}': stage '{key}' has transition to "
                    f"unknown stage '{target}'."
                )

    initial = stages[0]._key

    return WorkflowDef(
        name=name,
        version=version,
        stages=stages,
        stage_map=stage_map,
        transitions=transitions,
        initial_stage=initial,
        terminal_stages=terminal_stages,
    )


def workflow(name: str, version: int):
    """Decorator to register a workflow definition.

    The decorated function must return a list of Stage subclasses::

        @workflow(name="default", version=1)
        def default():
            return [Todo, InProgress, InReview, Approved, Merging, Done, Cancelled]

    The workflow is validated and stored in the module-level registry.
    """
    def decorator(func):
        stages = func()
        wf = _validate_workflow(name, version, stages)
        wf.source_path = ""
        _workflow_registry[name] = wf
        return func
    return decorator


# ---------------------------------------------------------------------------
# Loading workflows from disk
# ---------------------------------------------------------------------------

def _workflow_dir(hc_home: Path, team: str, wf_name: str, version: int) -> Path:
    """Return the directory where a workflow version is stored."""
    return hc_home / "teams" / team / "workflows" / wf_name / f"v{version}"


def _workflow_file(hc_home: Path, team: str, wf_name: str, version: int) -> Path:
    """Return the path to a workflow's Python file."""
    return _workflow_dir(hc_home, team, wf_name, version) / "workflow.py"


def _actions_dir(hc_home: Path, team: str, wf_name: str, version: int) -> Path:
    """Return the directory where workflow actions are stored."""
    return _workflow_dir(hc_home, team, wf_name, version) / "actions"


def load_workflow(hc_home: Path, team: str, name: str, version: int) -> WorkflowDef:
    """Load a workflow definition from the team's workflows directory.

    The workflow Python file is imported via ``importlib``, and the
    ``@workflow`` decorator inside it populates the module-level registry.

    Returns:
        The validated ``WorkflowDef``.

    Raises:
        FileNotFoundError: If the workflow file doesn't exist.
        ValueError: If the file doesn't register a workflow with the expected name.
    """
    path = _workflow_file(hc_home, team, name, version)
    if not path.is_file():
        raise FileNotFoundError(
            f"Workflow file not found: {path}\n"
            f"Use 'delegate workflow add {team} <path>' to register a workflow."
        )

    # Clear the registry so we only get this file's workflow
    _workflow_registry.clear()

    # Load the module
    module_name = f"delegate_workflow_{team}_{name}_v{version}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load workflow file: {path}")
    mod = types.ModuleType(module_name)
    mod.__file__ = str(path)

    # Make delegate.workflow importable from the workflow file
    import sys
    if "delegate.workflow" not in sys.modules:
        sys.modules["delegate.workflow"] = sys.modules[__name__]

    spec.loader.exec_module(mod)

    if name not in _workflow_registry:
        registered = list(_workflow_registry.keys())
        raise ValueError(
            f"Workflow file {path} did not register a workflow named '{name}'. "
            f"Registered: {registered}"
        )

    wf = _workflow_registry[name]
    if wf.version != version:
        raise ValueError(
            f"Workflow '{name}' in {path} has version {wf.version}, "
            f"expected {version}."
        )

    wf.source_path = str(path)
    return wf


def load_workflow_cached(
    hc_home: Path, team: str, name: str, version: int,
    _cache: dict[tuple, WorkflowDef] = {},
) -> WorkflowDef:
    """Load a workflow with a per-process cache (keyed on team+name+version)."""
    key = (str(hc_home), team, name, version)
    if key not in _cache:
        _cache[key] = load_workflow(hc_home, team, name, version)
    return _cache[key]


def list_workflows(hc_home: Path, team: str) -> list[dict[str, Any]]:
    """List all registered workflows for a team.

    Returns a list of dicts with keys: name, version, stages, initial, terminals.
    """
    wf_base = hc_home / "teams" / team / "workflows"
    if not wf_base.is_dir():
        return []

    results = []
    for wf_dir in sorted(wf_base.iterdir()):
        if not wf_dir.is_dir():
            continue
        wf_name = wf_dir.name
        # Find the latest version
        versions = []
        for v_dir in wf_dir.iterdir():
            if v_dir.is_dir() and v_dir.name.startswith("v"):
                try:
                    versions.append(int(v_dir.name[1:]))
                except ValueError:
                    continue
        if not versions:
            continue
        latest = max(versions)
        try:
            wf = load_workflow(hc_home, team, wf_name, latest)
            results.append({
                "name": wf.name,
                "version": wf.version,
                "all_versions": sorted(versions),
                "stages": [
                    {
                        "key": cls._key,
                        "label": cls.label,
                        "terminal": cls.terminal,
                        "auto": cls.auto,
                    }
                    for cls in wf.stages
                ],
                "transitions": {k: sorted(v) for k, v in wf.transitions.items()},
                "initial": wf.initial_stage,
                "terminals": sorted(wf.terminal_stages),
            })
        except Exception as exc:
            logger.warning("Could not load workflow '%s' v%d: %s", wf_name, latest, exc)

    return results


def get_latest_version(hc_home: Path, team: str, name: str) -> int | None:
    """Return the latest version number for a workflow, or None if not found."""
    wf_dir = hc_home / "teams" / team / "workflows" / name
    if not wf_dir.is_dir():
        return None
    versions = []
    for v_dir in wf_dir.iterdir():
        if v_dir.is_dir() and v_dir.name.startswith("v"):
            try:
                versions.append(int(v_dir.name[1:]))
            except ValueError:
                continue
    return max(versions) if versions else None


# ---------------------------------------------------------------------------
# Workflow registration (used by CLI `workflow add`)
# ---------------------------------------------------------------------------

def register_workflow(
    hc_home: Path,
    team: str,
    source_path: Path,
) -> WorkflowDef:
    """Register a workflow from a source file.

    1. Loads and validates the workflow file.
    2. Checks that the version is higher than any existing version.
    3. Copies the file (and any referenced actions) to the team's
       workflows directory.

    Args:
        hc_home: Delegate home directory.
        team: Team name.
        source_path: Path to the workflow Python file.

    Returns:
        The validated WorkflowDef.

    Raises:
        FileNotFoundError: If source_path doesn't exist.
        ValueError: If validation fails or version is not higher.
    """
    import shutil

    source_path = Path(source_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Workflow file not found: {source_path}")

    # Verify team exists
    team_dir = hc_home / "teams" / team
    if not team_dir.is_dir():
        raise FileNotFoundError(f"Team '{team}' not found at {team_dir}")

    # Load and validate by importing the file temporarily
    _workflow_registry.clear()

    module_name = f"delegate_workflow_register_{source_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, str(source_path))
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load workflow file: {source_path}")

    mod = types.ModuleType(module_name)
    mod.__file__ = str(source_path)

    import sys
    if "delegate.workflow" not in sys.modules:
        sys.modules["delegate.workflow"] = sys.modules[__name__]

    spec.loader.exec_module(mod)

    if not _workflow_registry:
        raise ValueError(
            f"Workflow file {source_path} did not register any workflow. "
            f"Use @workflow(name=..., version=...) decorator."
        )

    # Get the registered workflow (take the first one)
    wf_name = next(iter(_workflow_registry))
    wf = _workflow_registry[wf_name]

    # Check version is higher than existing
    current_latest = get_latest_version(hc_home, team, wf_name)
    if current_latest is not None and wf.version <= current_latest:
        raise ValueError(
            f"Workflow '{wf_name}' version {wf.version} is not higher than "
            f"existing version {current_latest}. Bump the version number."
        )

    # Copy to destination
    dest_dir = _workflow_dir(hc_home, team, wf_name, wf.version)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_file = dest_dir / "workflow.py"
    shutil.copy2(str(source_path), str(dest_file))

    # Copy adjacent action files if an 'actions' directory exists next to the source
    source_actions = source_path.parent / "actions"
    if source_actions.is_dir():
        dest_actions = dest_dir / "actions"
        if dest_actions.exists():
            shutil.rmtree(str(dest_actions))
        shutil.copytree(str(source_actions), str(dest_actions))
        logger.info(
            "Copied actions directory from %s to %s",
            source_actions, dest_actions,
        )

    wf.source_path = str(dest_file)
    logger.info(
        "Registered workflow '%s' v%d for team '%s' at %s",
        wf_name, wf.version, team, dest_file,
    )

    return wf


def update_actions(
    hc_home: Path,
    team: str,
    wf_name: str,
    source_path: Path,
) -> None:
    """Update the actions directory for an existing workflow version.

    Does NOT bump the version — this is for script-only changes that
    don't affect the stage graph.

    Args:
        hc_home: Delegate home directory.
        team: Team name.
        wf_name: Workflow name.
        source_path: Path to the actions directory to copy.
    """
    import shutil

    source_path = Path(source_path).resolve()
    if not source_path.is_dir():
        raise FileNotFoundError(f"Actions directory not found: {source_path}")

    version = get_latest_version(hc_home, team, wf_name)
    if version is None:
        raise ValueError(
            f"No workflow '{wf_name}' found for team '{team}'. "
            f"Register it first with 'delegate workflow add'."
        )

    dest_actions = _actions_dir(hc_home, team, wf_name, version)
    if dest_actions.exists():
        shutil.rmtree(str(dest_actions))
    shutil.copytree(str(source_path), str(dest_actions))

    logger.info(
        "Updated actions for workflow '%s' v%d (team '%s') from %s",
        wf_name, version, team, source_path,
    )
