"""Bounded-context persistent conversation for the Claude Code SDK.

Standalone utility — no knowledge of Delegate's domain model (teams,
agents, tasks, worktrees).  Hand it a preamble and a working
directory, call ``send()`` repeatedly.  The class handles session
resumption, token accounting, permission enforcement via
``can_use_tool``, and automatic context-window rotation.

Internally uses ``ClaudeSDKClient`` (from ``claude_agent_sdk``) to keep a **single persistent
subprocess** across all turns — no process-per-query overhead.

On the **first turn** of each generation the user message sent to
the SDK is::

    {preamble}

    {memory}

    {prompt}

On subsequent turns only the raw ``prompt`` is sent — the
preamble and memory are already in the conversation history
maintained by the subprocess.

When the context window fills up, the session **auto-rotates**:

1. Ask the model to summarise its state (``rotation_prompt``).
2. Store the summary as the new ``memory``.
3. Call ``on_rotation(memory)`` so the caller can persist it.
4. Disconnect the old subprocess and reset conversation state.
5. The next ``send()`` starts a fresh generation with the preamble
   and the updated memory.

Claude Code's own system prompt — which contains all tool-use
instructions — is never overridden.

Usage::

    tel = Telephone(
        preamble="You are a senior Python engineer working on Acme.",
        cwd="/path/to/workdir",
        allowed_write_paths=["/path/to/workdir"],
    )

    async for msg in tel.send("Fix the bug in main.py"):
        print(msg)

    # Later — conversation is automatically resumed (same subprocess).
    # If context is full, send() auto-rotates transparently.
    async for msg in tel.send("Now add tests"):
        print(msg)

    await tel.close()   # clean up the subprocess

    # Or use as an async context manager:
    async with Telephone(preamble="...", cwd="...") as t:
        async for msg in t.send("Hello"):
            print(msg)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

try:
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
except ImportError:  # SDK not installed — tests mock these
    PermissionResultAllow = None  # type: ignore[assignment,misc]
    PermissionResultDeny = None   # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Sentinel for "argument not provided" (distinct from None).
_UNSET = object()


# ---------------------------------------------------------------------------
# SDK monkey-patch: handle unknown message types gracefully
# ---------------------------------------------------------------------------
# The claude_agent_sdk (≤ 0.1.39) raises ``MessageParseError`` on any
# message type it doesn't recognise, including ``rate_limit_event`` which
# the Claude Code CLI started emitting.  Rather than crashing the entire
# turn, we patch the SDK parser to wrap unrecognised types as
# ``SystemMessage(subtype=<original_type>, data=<raw_dict>)`` so the
# rest of Delegate can handle them (e.g. surface a rate-limit toast).
#
# The patch is applied once at import time.  It is safe for tests that
# mock the SDK because ``_install_sdk_parse_patch`` is a no-op when the
# SDK is not installed.

def _install_sdk_parse_patch() -> None:
    """Monkey-patch ``claude_agent_sdk`` to survive unknown message types."""
    try:
        import claude_agent_sdk._internal.message_parser as _mp
        from claude_agent_sdk.types import SystemMessage
    except ImportError:
        return  # SDK not installed — nothing to patch

    _original_parse = _mp.parse_message

    def _patched_parse_message(data: dict) -> Any:  # type: ignore[type-arg]
        try:
            return _original_parse(data)
        except Exception:
            msg_type = data.get("type", "unknown")
            logger.info(
                "SDK parse_message: unrecognised type %r — wrapping as SystemMessage",
                msg_type,
            )
            return SystemMessage(subtype=msg_type, data=data)

    _mp.parse_message = _patched_parse_message  # type: ignore[assignment]
    logger.debug("Installed SDK parse_message monkey-patch")

_install_sdk_parse_patch()


# ---------------------------------------------------------------------------
# Token / cost accounting
# ---------------------------------------------------------------------------

@dataclass
class TelephoneUsage:
    """Token / cost accounting — single source of truth.

    Used both as a per-message snapshot and as a cumulative
    accumulator.  Supports arithmetic (``+``, ``-``, ``+=``) so
    callers can combine per-turn deltas into lifetime totals.

    The ``from_sdk_message()`` classmethod extracts usage from
    a ``ResultMessage`` emitted by the Claude Code SDK.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    # --- Arithmetic ---------------------------------------------------

    def __add__(self, other: TelephoneUsage) -> TelephoneUsage:
        return TelephoneUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    def __iadd__(self, other: TelephoneUsage) -> TelephoneUsage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cost_usd += other.cost_usd
        return self

    def __sub__(self, other: TelephoneUsage) -> TelephoneUsage:
        return TelephoneUsage(
            input_tokens=self.input_tokens - other.input_tokens,
            output_tokens=self.output_tokens - other.output_tokens,
            cache_read_tokens=self.cache_read_tokens - other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens - other.cache_write_tokens,
            cost_usd=self.cost_usd - other.cost_usd,
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, TelephoneUsage):
            return False
        return (
            self.input_tokens == other.input_tokens
            and self.output_tokens == other.output_tokens
            and self.cache_read_tokens == other.cache_read_tokens
            and self.cache_write_tokens == other.cache_write_tokens
            and abs(self.cost_usd - other.cost_usd) < 1e-6
        )

    def __ne__(self, other: Any) -> bool:
        return not self == other

    # --- SDK extraction -----------------------------------------------

    @classmethod
    def from_sdk_message(cls, msg: Any) -> TelephoneUsage:
        """Extract usage from a Claude Code SDK ``ResultMessage``.

        Only ``ResultMessage`` carries usage/cost data.
        ``AssistantMessage`` has ``content`` and ``model`` but no usage
        fields — the SDK aggregates all token/cost info into the single
        ``ResultMessage`` emitted at the end of each ``query()`` call.

        Returns a zero-valued instance for non-ResultMessage inputs.
        """
        if not hasattr(msg, "total_cost_usd"):
            return cls()

        cost = msg.total_cost_usd or 0.0
        usage = getattr(msg, "usage", None)
        tin = tout = cache_read = cache_write = 0

        if usage and isinstance(usage, dict):
            tin = usage.get("input_tokens", 0)
            tout = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_write = usage.get("cache_creation_input_tokens", 0)
        elif usage is not None:
            logger.warning(
                "Unexpected usage type %s on %s — skipping",
                type(usage).__name__, type(msg).__name__,
            )

        return cls(
            input_tokens=tin,
            output_tokens=tout,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost,
        )

# ---------------------------------------------------------------------------
# Telephone
# ---------------------------------------------------------------------------

class Telephone:
    """Bounded-context persistent conversation with a Claude Code agent.

    Internally wraps ``ClaudeSDKClient``, keeping a **single
    persistent subprocess** alive across all turns.  ``send()``
    lazily connects on first call; subsequent calls reuse the same
    process.  Token usage is tracked; when it exceeds
    ``max_context_tokens`` the session auto-rotates — asking the
    model to summarise, replacing ``memory`` with the summary,
    disconnecting the subprocess, and starting a fresh one — all
    transparently within ``send()``.

    **Preamble vs Memory**

    * ``preamble`` — static role instructions and constraints.  Never
      changes.  Included on the first turn of every generation.
    * ``memory`` — dynamic accumulated context.  Starts empty (or
      loaded from a prior ``context.md``).  Replaced with the
      rotation summary on each context-window rotation.  Also
      included on the first turn of every generation.

    On turn 0 of any generation the user message is::

        {preamble}\\n\\n{memory}\\n\\n{prompt}

    On turn N > 0, only ``prompt`` is sent — everything else is
    already in the conversation history.

    **Permissions** are enforced per-turn via ``can_use_tool``:

    * *Write isolation*: ``allowed_write_paths`` restricts where
      ``Edit`` / ``Write`` tools can operate.  Pass ``None`` for
      unrestricted writes (e.g. managers).
    * *Bash deny-list*: ``denied_bash_patterns`` blocks commands
      containing specified substrings (e.g. ``"git rebase"``).

    The class is deliberately free of any Delegate-specific concepts
    (teams, agents, mailbox, tasks).  It only depends on
    ``claude_agent_sdk``.

    Args:
        preamble: Static role instructions prepended to the user
            message on the first turn of each generation.
        cwd: Working directory for the agent.
        memory: Dynamic context (e.g. loaded from a prior
            ``context.md``).  Included after the preamble on the first
            turn.  Replaced with the rotation summary on each rotation.
            Default ``""``.
        max_context_tokens: Auto-rotate when cumulative input tokens
            exceed this.  Default 80 000.
        rotation_prompt: Prompt sent to the model before rotating to
            extract a summary.  Set to ``None`` to skip
            summarisation (hard reset only).
        on_rotation: Callback invoked after rotation with the new
            ``memory`` text (or ``None`` if no summary was produced).
            Use this to persist ``context.md``.
        model: Model identifier (e.g. ``"claude-sonnet-4-20250514"``).
        allowed_write_paths: Paths where Edit/Write are permitted.
            Multiple paths supported (multi-repo).  ``None`` means
            unrestricted.  Resolved to absolute paths.
        denied_bash_patterns: Substrings to block in Bash commands.
        add_dirs: Extra directories to expose to the agent.
        permission_mode: SDK permission mode.  Default
            ``"bypassPermissions"``.
        disallowed_tools: Tool patterns to deny at the SDK level
            (complementary to ``can_use_tool``).
        sandbox_enabled: Enable OS-level bash sandboxing (macOS
            Seatbelt / Linux bubblewrap).  When ``True``, bash
            commands are restricted to ``cwd`` + ``add_dirs`` at the
            kernel level — defense-in-depth beyond ``can_use_tool``.
    """

    DEFAULT_ROTATION_PROMPT = (
        "Your session context is about to be rotated. "
        "Please write a concise summary of whatever you have learned - about"
        "the project, codebase, recent tasks, and any other information that may "
        "be useful to you in future sessions.\n"
        "This summary will be provided to you at the start of your "
        "next session so you can pick up where you left off."
    )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        preamble: str,
        cwd: str | Path,
        memory: str = "",
        max_context_tokens: int = 80_000,
        rotation_prompt: str | None = DEFAULT_ROTATION_PROMPT,
        on_rotation: Callable[[str | None], Any] | None = None,
        model: str | None = None,
        allowed_write_paths: list[str | Path] | None = None,
        denied_bash_patterns: list[str] | None = None,
        add_dirs: list[str | Path] | None = None,
        permission_mode: str = "bypassPermissions",
        disallowed_tools: list[str] | None = None,
        sandbox_enabled: bool = False,
        mcp_servers: dict[str, Any] | None = None,
        allowed_domains: list[str] | None = None,
        settings_env: dict[str, str] | None = None,
    ):
        # Stable identity
        self.id: str = uuid.uuid4().hex

        self.preamble = preamble
        self.memory = memory
        self.cwd = Path(cwd).resolve()
        self.max_context_tokens = max_context_tokens
        self.rotation_prompt = rotation_prompt
        self.on_rotation = on_rotation
        self.model = model
        self.permission_mode = permission_mode
        self.add_dirs = [Path(p).resolve() for p in (add_dirs or [])]
        self.disallowed_tools = list(disallowed_tools or [])
        self.sandbox_enabled = sandbox_enabled
        self.mcp_servers: dict[str, Any] = dict(mcp_servers or {})
        self.allowed_domains: list[str] = list(allowed_domains or ["*"])
        self.settings_env: dict[str, str] = dict(settings_env or {})

        # Permission configuration
        self._allowed_write_paths: list[Path] | None = (
            [Path(p).resolve() for p in allowed_write_paths]
            if allowed_write_paths is not None
            else None
        )
        self._denied_bash_patterns: list[str] = list(denied_bash_patterns or [])

        self._client: Any = None  # ClaudeSDKClient instance
        self._stale_client: Any = None  # queued for disconnect on next send
        # Track subprocess PIDs directly — the SDK's async disconnect chain
        # (query.close → transport.close → terminate) is unreliable due to
        # multiple suppress(Exception) blocks and async lock acquisition.
        # We use os.kill(SIGTERM) as the primary cleanup mechanism.
        self._child_pids: set[int] = set()  # all PIDs spawned for this telephone
        self._effective_write_paths: list[Path] | None = (
            list(self._allowed_write_paths) if self._allowed_write_paths is not None else None
        )
        self.usage = TelephoneUsage() # usage from current generation
        self.prior_usage = TelephoneUsage() # usage from previous generations
        self._last_cumulative_cost: float = 0.0  # SDK cost is cumulative; track for delta
        self.turns: int = 0
        self.created_at: float = time.time()
        self.generation: int = 0  # increments on each rotation

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Whether a connected SDK client (subprocess) exists."""
        return self._client is not None

    @property
    def needs_rotation(self) -> bool:
        """Whether cumulative input tokens exceed the budget."""
        return self.usage.input_tokens > self.max_context_tokens

    @property
    def allowed_write_paths(self) -> list[Path] | None:
        """Absolute paths where Edit/Write are allowed (``None`` = unrestricted)."""
        return self._allowed_write_paths

    @allowed_write_paths.setter
    def allowed_write_paths(self, paths: list[str | Path] | None) -> None:
        self._allowed_write_paths = (
            [Path(p).resolve() for p in paths]
            if paths is not None
            else None
        )
    
    def total_usage(self) -> TelephoneUsage:
        return self.usage + self.prior_usage

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Explicitly connect the SDK client (spawns subprocess).

        Optional — ``send()`` connects lazily on the first call.
        """
        await self._ensure_client()

    async def interrupt(self) -> None:
        """Interrupt the current turn and reset conversation state.

        Sends the SDK interrupt signal to stop the in-progress query,
        then resets the telephone so the next send() starts a fresh
        generation (preserving memory).
        """
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception:
                pass
        # Reset conversation so agent starts fresh on next turn
        self.reset()

    @staticmethod
    def _get_client_pid(client: Any) -> int | None:
        """Extract the subprocess PID from a ClaudeSDKClient instance.

        Reaches into SDK internals — fragile, but necessary for reliable
        process cleanup.
        """
        try:
            transport = getattr(client, "_transport", None) or getattr(
                getattr(client, "_query", None), "transport", None
            )
            proc = getattr(transport, "_process", None) if transport else None
            if proc is None:
                return None
            pid = getattr(proc, "pid", None)
            return pid if isinstance(pid, int) else None
        except Exception:
            return None

    def _kill_pid(self, pid: int, sig: int = signal.SIGTERM) -> None:
        """Send a signal to a tracked subprocess PID."""
        try:
            os.kill(pid, sig)
            logger.debug("Telephone %s: sent signal %d to PID %d", self.id[:8], sig, pid)
        except ProcessLookupError:
            pass  # already dead
        except OSError:
            logger.debug("Telephone %s: failed to signal PID %d", self.id[:8], pid, exc_info=True)

    async def _disconnect_client(self, client: Any) -> None:
        """Disconnect a SDK client with direct PID kill as primary mechanism.

        The SDK's async disconnect chain (query.close → transport.close →
        terminate) is unreliable — it has multiple ``suppress(Exception)``
        blocks that silently swallow errors, causing the process to survive.

        We capture the PID first, then attempt SDK disconnect for state
        cleanup, and **always** follow up with os.kill(SIGTERM) to ensure
        the subprocess actually dies.
        """
        pid = self._get_client_pid(client)
        # Attempt SDK disconnect for clean conversation state teardown.
        try:
            await client.disconnect()
        except Exception:
            pass
        # Always kill the subprocess directly — don't trust the SDK chain.
        if pid:
            self._kill_pid(pid, signal.SIGTERM)
            self._child_pids.discard(pid)

    async def close(self) -> None:
        """Disconnect the SDK client and release the subprocess."""
        for client in (self._client, self._stale_client):
            if client is not None:
                await self._disconnect_client(client)
        self._client = None
        self._stale_client = None
        # Belt-and-suspenders: kill ALL tracked PIDs that may have been
        # missed (e.g., from failed rotations where reset() was never called).
        for pid in list(self._child_pids):
            self._kill_pid(pid, signal.SIGTERM)
        self._child_pids.clear()

    async def __aenter__(self) -> "Telephone":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self.close()
        return False

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def _build_turn0_prompt(self, prompt: str) -> str:
        """Build the composite first-turn message.

        Layout::

            {preamble}

            {memory}       ← only if non-empty

            {prompt}
        """
        parts = ["## PREAMBLE", self.preamble]
        if self.memory and self.memory.strip():
            parts.append("## MEMORY")
            parts.append(self.memory)
        parts.append(prompt)
        return "\n\n".join(parts)

    async def send(
        self,
        prompt: str,
        *,
        allowed_write_paths: Any = _UNSET,
    ) -> AsyncIterator[Any]:
        """Send a prompt and stream response messages.

        On the first turn of each generation the full composite
        message (``preamble + memory + prompt``) is sent.  On
        subsequent turns only ``prompt`` is sent — the rest is
        already in the subprocess's conversation history.

        If the context window is full (``needs_rotation``), the session
        automatically rotates before processing the prompt.

        Args:
            prompt: The user message.
            allowed_write_paths: Override write-path restrictions for
                this turn.  ``None`` = unrestricted; omit to keep the
                session default.

        Yields:
            SDK message objects (``AssistantMessage``, ``SystemMessage``,
            ``ResultMessage``, etc.).
        """
        # --- Auto-rotate if context is full ---
        if self.needs_rotation:
            await self.rotate()

        # Update effective write paths for this turn's guard callback.
        if allowed_write_paths is _UNSET:
            self._effective_write_paths = self._allowed_write_paths
        elif allowed_write_paths is None:
            self._effective_write_paths = None
        else:
            self._effective_write_paths = [Path(p).resolve() for p in allowed_write_paths]

        await self._ensure_client()

        # On the first turn of each generation, include preamble + memory.
        effective_prompt = (
            self._build_turn0_prompt(prompt)
            if self.turns == 0
            else prompt
        )

        # ClaudeSDKClient.query() accepts plain strings — no need for
        # the AsyncIterable wrapper that the old query() function required
        # when can_use_tool was set.
        await self._client.query(effective_prompt)

        prompt_too_long = False
        async for msg in self._client.receive_response():
            self._track_message(msg)
            # Detect "Prompt is too long" from Claude Code CLI — the context
            # window is full but usage.input_tokens is 0 so needs_rotation
            # never fires.  Flag it and force a rotation after yielding.
            if hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "text") and "prompt is too long" in (block.text or "").lower():
                        prompt_too_long = True
            yield msg

        self.turns += 1

        # Force rotation on prompt-too-long so the next send() starts fresh.
        if prompt_too_long and self.turns > 0:
            logger.info(
                "Telephone %s: prompt too long detected — forcing rotation",
                self.id[:8],
            )
            await self.rotate(summary_prompt=None)

    async def rotate(self, summary_prompt: str | None = _UNSET) -> str | None:
        """Rotate the conversation — summarise, update memory, reset.

        If the conversation is active and a summary prompt is available
        (defaults to ``self.rotation_prompt``), it is sent to the
        current session and the model's text response becomes the
        new ``memory``.  The ``on_rotation`` callback is then invoked
        with the new memory, and conversation state is reset.

        Called automatically by ``send()`` when ``needs_rotation``
        is true.

        Args:
            summary_prompt: Override the rotation prompt for this call.
                Pass ``None`` to skip summarisation.  Omit to use the
                telephone's ``rotation_prompt``.

        Returns:
            The summary text (new memory), or ``None``.
        """
        prompt = self.rotation_prompt if summary_prompt is _UNSET else summary_prompt
        summary: str | None = None

        if prompt and self._client is not None:
            parts: list[str] = []
            # Temporarily disable rotation to avoid infinite recursion:
            # this is a summary turn, not a real user turn.
            saved_max = self.max_context_tokens
            self.max_context_tokens = float("inf")  # type: ignore[assignment]
            try:
                async for msg in self.send(prompt):
                    if hasattr(msg, "content"):
                        for block in msg.content:
                            if hasattr(block, "text"):
                                parts.append(block.text)
            finally:
                self.max_context_tokens = saved_max
            summary = "\n".join(parts).strip() or None

        logger.info(
            "Telephone %s rotating (gen %d → %d, %d turns, %d input tokens)",
            self.id[:8], self.generation, self.generation + 1,
            self.turns, self.usage.input_tokens,
        )

        # Update memory with the summary (persists across reset).
        self.memory = summary or ""

        # Notify caller so they can persist to disk.
        if self.on_rotation is not None:
            self.on_rotation(self.memory)

        self.reset()
        return summary

    def reset(self) -> None:
        """Hard reset — discard conversation state and mint new id.

        The current client (subprocess) is queued for cleanup —
        it will be disconnected when the next ``send()`` connects a
        fresh client, or when ``close()`` is called.

        **Does not clear ``memory``** — it persists across generations.
        The caller can set ``telephone.memory = ""`` explicitly if
        needed.
        """
        if self._client is not None:
            self._stale_client = self._client
            self._client = None
        self.id = uuid.uuid4().hex
        self.turns = 0
        self.created_at = time.time()
        # roll over usage from this generation to the prior generations
        self.prior_usage += self.usage
        self.usage = TelephoneUsage()
        # New subprocess starts with cumulative cost = 0
        self._last_cumulative_cost = 0.0

        self.generation += 1

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> None:
        """Lazily connect a ``ClaudeSDKClient``, creating one if needed.

        Also cleans up any stale client left over from a prior
        ``reset()``.  Uses SIGKILL as a fallback if the normal
        disconnect path fails — this prevents the runaway subprocess
        leak that previously accumulated hundreds of orphaned processes.
        """
        if self._client is not None:
            return

        # Disconnect the old subprocess from a previous generation.
        if self._stale_client is not None:
            await self._disconnect_client(self._stale_client)
            self._stale_client = None

        from claude_agent_sdk import ClaudeSDKClient

        options = self._build_options()
        self._client = ClaudeSDKClient(options)
        await self._client.connect()

        # Track the subprocess PID for reliable cleanup.
        pid = self._get_client_pid(self._client)
        if pid:
            self._child_pids.add(pid)
            logger.debug("Telephone %s: spawned subprocess PID %d (gen %d)", self.id[:8], pid, self.generation)

    def _build_options(self) -> Any:
        """Assemble ``ClaudeAgentOptions`` for the client.

        Never sets ``system_prompt`` — we rely on Claude Code's own
        system prompt for tool-use instructions.  Our preamble and
        memory are prepended to the user message in ``send()`` instead.

        No ``resume`` parameter is needed — the ``ClaudeSDKClient``
        keeps a single persistent subprocess that maintains
        conversation state across ``query()`` calls.

        **Permission model**: when a ``can_use_tool`` guard is active
        we must NOT pass ``--permission-mode bypassPermissions``.
        ``bypassPermissions`` causes Claude Code to auto-approve every
        tool call *before* consulting the ``--permission-prompt-tool``
        (stdio), which means our guard is never invoked — Edit / Write
        calls go straight through unchecked.

        By omitting ``permission_mode`` when the guard is set, Claude
        Code falls back to its default mode and routes every permission
        check through the stdio permission-prompt-tool, which the SDK
        forwards to our ``can_use_tool`` callback.  Bash commands are
        still auto-approved by the sandbox (``autoAllowBashIfSandboxed:
        true``), so only Edit/Write/MultiEdit go through the callback.
        """
        from claude_agent_sdk import ClaudeAgentOptions

        kw: dict[str, Any] = {
            "cwd": str(self.cwd),
        }

        if self.model:
            kw["model"] = self.model

        if self.add_dirs:
            kw["add_dirs"] = [str(d) for d in self.add_dirs]

        if self.disallowed_tools:
            kw["disallowed_tools"] = list(self.disallowed_tools)

        # Permission enforcement callback
        guard = self._make_guard()
        if guard is not None:
            kw["can_use_tool"] = guard
            # CRITICAL: do NOT set permission_mode when guard is active.
            # bypassPermissions short-circuits Claude Code's permission
            # system, preventing can_use_tool from ever being called.
            # Without it, Claude Code routes permission checks through
            # the stdio permission-prompt-tool → our guard callback.
        else:
            # No guard — fall back to permission_mode for unattended use.
            if self.permission_mode:
                kw["permission_mode"] = self.permission_mode

        # OS-level sandbox for bash commands
        if self.sandbox_enabled:
            sandbox_config: dict[str, Any] = {
                "enabled": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
            }
            # Network domain allowlist for the sandbox proxy.
            #
            # Claude Code routes ALL outbound network from sandboxed bash
            # commands through a local HTTP proxy.  The proxy checks each
            # connection against ``sandbox.network.allowedDomains`` and
            # returns 403 for anything not listed.
            #
            # Without this key the proxy blocks *everything* — including
            # pip, npm, curl, etc.  We always pass the domain list so
            # that agents can reach package registries, git forges, etc.
            #
            # The list comes from ``delegate network allow/disallow``
            # and defaults to common package-manager & git-forge domains.
            if self.allowed_domains:
                sandbox_config["network"] = {
                    "allowedDomains": list(self.allowed_domains),
                }
            kw["sandbox"] = sandbox_config

        # Environment variables for every bash command.
        # Passed via Claude Code's ``settings.env`` — applies automatically
        # to every bash invocation (no ``source`` needed).  Used to redirect
        # package-manager caches to a writable, team-shared directory.
        if self.settings_env:
            kw["settings"] = json.dumps({"env": self.settings_env})

        # In-process MCP servers (run in daemon process, outside sandbox)
        if self.mcp_servers:
            kw["mcp_servers"] = dict(self.mcp_servers)

        return ClaudeAgentOptions(**kw)

    # Tools that can write files — must be checked against allowed_write_paths.
    # Uses a broad set to catch current and future Claude Code tools.  New
    # write-capable tools added by the SDK will be caught if they are NOT in
    # the read-only set below.
    _WRITE_TOOLS = frozenset({
        "Edit", "Write", "MultiEdit", "NotebookEdit",
    })

    # Tools known to be read-only — never need write-path checks.
    _READ_ONLY_TOOLS = frozenset({
        "Read", "Grep", "Glob", "LS", "NotebookRead", "View",
        "Bash",  # handled separately via deny-list + OS sandbox
        "TodoRead", "TodoWrite",  # internal
    })

    def _make_guard(self):
        """Build a ``can_use_tool`` callback for path/command enforcement.

        Returns ``None`` if there are no restrictions to enforce.

        The guard reads ``self._effective_write_paths`` at each
        invocation so that per-turn ``allowed_write_paths`` overrides
        in ``send()`` take effect without reconnecting the client.

        **Path resolution**: relative paths in tool inputs are resolved
        against ``self.cwd`` (the Telephone's working directory), NOT
        the daemon's ``os.getcwd()``.  This matches Claude Code's own
        path resolution.  Symlinks are resolved so that symlink-based
        escapes (e.g. ``repos/<name>/file`` → real repo) are caught.

        **Tool coverage**: any tool NOT in ``_READ_ONLY_TOOLS`` that
        carries a ``file_path`` or ``notebook_path`` parameter is
        checked.  This future-proofs against new write tools.
        """
        has_write_restriction = self._allowed_write_paths is not None
        has_bash_restriction = bool(self._denied_bash_patterns)

        if not has_write_restriction and not has_bash_restriction:
            return None

        # Capture *self* so the guard reads _effective_write_paths
        # dynamically — it may change between turns.
        telephone = self
        _bash_deny = self._denied_bash_patterns
        _read_only = self._READ_ONLY_TOOLS

        # Build allow/deny helpers that return SDK types when available,
        # falling back to plain dicts for test environments without the SDK.
        def _allow():
            if PermissionResultAllow is not None:
                return PermissionResultAllow()
            return {"behavior": "allow"}

        def _deny(message: str):
            if PermissionResultDeny is not None:
                return PermissionResultDeny(message=message)
            return {"behavior": "deny", "message": message}

        async def _guard(
            tool_name: str,
            tool_input: dict[str, Any],
            _context: Any,
        ):
            # --- Write-path isolation ---
            _write_paths = telephone._effective_write_paths
            if _write_paths is not None and tool_name not in _read_only:
                # Check all path-like parameters (file_path, notebook_path, etc.)
                file_path = (
                    tool_input.get("file_path", "")
                    or tool_input.get("notebook_path", "")
                )
                if file_path:
                    # Resolve relative to Telephone CWD, not daemon CWD.
                    p = Path(file_path)
                    resolved = (
                        p.resolve()
                        if p.is_absolute()
                        else (telephone.cwd / p).resolve()
                    )
                    if not any(
                        resolved == wp or _is_under(resolved, wp)
                        for wp in _write_paths
                    ):
                        return _deny(
                            f"Write denied: {file_path} is outside "
                            f"allowed paths {[str(p) for p in _write_paths]}"
                        )

            # --- Bash deny-list ---
            if tool_name == "Bash" and _bash_deny:
                cmd = tool_input.get("command", "")
                cmd_upper = cmd.upper()
                for pattern in _bash_deny:
                    if pattern.upper() in cmd_upper:
                        return _deny(f"Command denied: contains '{pattern}'")

            return _allow()

        return _guard

    def _track_message(self, msg: Any) -> None:
        """Update token accounting from a ``ResultMessage``.

        The SDK's ``total_cost_usd`` is **cumulative** across the
        conversation session (all queries on the same subprocess),
        while ``usage.input_tokens`` / ``output_tokens`` are per-query.
        We convert the cumulative cost into a per-query delta so that
        ``self.usage`` only accumulates incremental values.
        """
        delta = TelephoneUsage.from_sdk_message(msg)
        if delta.input_tokens or delta.output_tokens:
            # Convert cumulative cost → per-query delta
            cumulative_cost = delta.cost_usd
            delta.cost_usd = max(0.0, cumulative_cost - self._last_cumulative_cost)
            self._last_cumulative_cost = cumulative_cost
            self.usage += delta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_under(child: Path, parent: Path) -> bool:
    """Check if *child* is a descendant of *parent* (both resolved)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
