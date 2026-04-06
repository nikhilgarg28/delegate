"""Tests for delegate.telephone.Telephone.

These tests call the live Claude Code SDK and are gated behind the
``--run-llm`` pytest flag.  Run them with::

    pytest tests/test_telephone.py --run-llm -v

They require a valid Claude authentication (``claude login`` or
``ANTHROPIC_API_KEY`` in the environment).

⚠️  Each test spawns a real agent turn — expect API charges.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure claude_agent_sdk is importable even when the real package isn't
# installed.  Unit tests only need ClaudeAgentOptions to behave as a
# simple data container.  If the real SDK *is* installed we leave it alone
# so the live (--run-llm) tests work.
# ---------------------------------------------------------------------------
try:
    import claude_agent_sdk as _real_sdk  # noqa: F401 — just probe availability
except ImportError:
    _fake_sdk = types.ModuleType("claude_agent_sdk")

    class _FakeOptions:
        """Minimal stand-in for ClaudeAgentOptions."""
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
            for attr in (
                "system_prompt", "resume", "model", "max_turns",
                "add_dirs", "disallowed_tools", "can_use_tool",
                "cwd", "permission_mode", "sandbox",
            ):
                if not hasattr(self, attr):
                    setattr(self, attr, None)

    class _FakeSandboxSettings:
        """Minimal stand-in for SandboxSettings."""
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    _fake_sdk.ClaudeAgentOptions = _FakeOptions  # type: ignore[attr-defined]
    _fake_sdk.SandboxSettings = _FakeSandboxSettings  # type: ignore[attr-defined]
    _fake_sdk.ResultMessage = type("ResultMessage", (), {})  # type: ignore[attr-defined]
    sys.modules["claude_agent_sdk"] = _fake_sdk

from delegate.telephone import Telephone, TelephoneUsage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIMPLE_PREAMBLE = (
    "You are a test assistant.  Keep answers to one sentence.  "
    "Do NOT use any tools unless explicitly asked."
)

def _collect_text(messages: list) -> str:
    """Extract concatenated text from a list of SDK messages."""
    parts: list[str] = []
    for msg in messages:
        if hasattr(msg, "content"):
            for block in msg.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
    return "\n".join(parts).strip()


async def _send_and_collect(telephone: Telephone, prompt: str, **kw) -> list:
    """Send a prompt, collect all messages into a list."""
    msgs: list = []
    async for msg in telephone.send(prompt, **kw):
        msgs.append(msg)
    return msgs


# ---------------------------------------------------------------------------
# Unit tests (no LLM) — verify pure logic
# ---------------------------------------------------------------------------

class TestTelephoneUnit:
    """Tests that don't call the SDK — verify construction and guards.

    These are NOT marked ``llm`` — they run in normal CI.
    """

    def test_init_defaults(self, tmp_path):
        """Telephone initialises with sane defaults."""
        t = Telephone(preamble="hello", cwd=tmp_path)
        assert t.id is not None and len(t.id) == 32  # uuid hex
        assert t.preamble == "hello"
        assert t.memory == ""
        assert t._client is None
        assert t.turns == 0
        assert t.generation == 0
        assert t.is_active is False
        assert t.needs_rotation is False
        assert t.usage.input_tokens == 0
        assert t._allowed_write_paths is None

    def test_init_with_memory(self, tmp_path):
        """Telephone can be initialised with prior memory."""
        t = Telephone(preamble="hello", cwd=tmp_path, memory="prior context")
        assert t.memory == "prior context"

    def test_init_write_paths_resolved(self, tmp_path):
        """Write paths are resolved to absolute paths."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            allowed_write_paths=["./relative", "/absolute/path"],
        )
        for p in t.allowed_write_paths:
            assert p.is_absolute()

    def test_reset_clears_state_but_not_memory(self, tmp_path):
        """reset() zeroes conversation state but preserves memory."""
        t = Telephone(preamble="hello", cwd=tmp_path, memory="important context")
        old_id = t.id
        t._client = MagicMock()  # simulate a connected client
        t.usage.input_tokens = 50_000
        t.turns = 10
        t.reset()
        assert t._client is None  # moved to _stale_client
        assert t._stale_client is not None  # queued for cleanup
        assert t.turns == 0
        assert t.usage.input_tokens == 0
        assert t.id != old_id  # new UUID
        assert t.generation == 1  # incremented
        assert t.total_usage().input_tokens == 50_000
        assert t.memory == "important context"  # preserved!

    def test_needs_rotation(self, tmp_path):
        """needs_rotation triggers when input exceeds threshold."""
        t = Telephone(preamble="hello", cwd=tmp_path, max_context_tokens=100)
        assert not t.needs_rotation
        t.usage.input_tokens = 101
        assert t.needs_rotation

    def test_write_paths_setter(self, tmp_path):
        """allowed_write_paths setter resolves new paths."""
        t = Telephone(preamble="hello", cwd=tmp_path)
        assert t.allowed_write_paths is None
        t.allowed_write_paths = [tmp_path / "a", tmp_path / "b"]
        assert len(t.allowed_write_paths) == 2
        t.allowed_write_paths = None
        assert t.allowed_write_paths is None

    def test_build_options(self, tmp_path):
        """Options: no system_prompt, no resume (client handles state)."""
        t = Telephone(
            preamble="my preamble",
            cwd=tmp_path,
            model="claude-sonnet-4-20250514",
            disallowed_tools=["Bash(git push:*)"],
        )
        opts = t._build_options()
        # system_prompt should never be set — we use Claude's own
        assert opts.system_prompt is None
        # resume should never be set — ClaudeSDKClient maintains state internally
        assert opts.resume is None
        assert opts.model == "claude-sonnet-4-20250514"
        assert "Bash(git push:*)" in opts.disallowed_tools
        # sandbox should be absent when not enabled
        assert opts.sandbox is None

    def test_build_options_sandbox_enabled(self, tmp_path):
        """When sandbox_enabled=True, options include SandboxSettings dict."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            sandbox_enabled=True,
        )
        opts = t._build_options()
        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is True
        assert opts.sandbox["autoAllowBashIfSandboxed"] is True
        assert opts.sandbox["allowUnsandboxedCommands"] is False

    def test_build_options_sandbox_disabled_by_default(self, tmp_path):
        """sandbox_enabled defaults to False — no sandbox in options."""
        t = Telephone(preamble="hello", cwd=tmp_path)
        assert t.sandbox_enabled is False
        opts = t._build_options()
        assert opts.sandbox is None

    def test_turn0_prompt_preamble_only(self, tmp_path):
        """Turn 0 with no memory: preamble + prompt."""
        t = Telephone(preamble="You are a pirate.", cwd=tmp_path)
        result = t._build_turn0_prompt("Hello")
        assert result == "## PREAMBLE\n\nYou are a pirate.\n\nHello"

    def test_turn0_prompt_with_memory(self, tmp_path):
        """Turn 0 with memory: preamble + memory + prompt."""
        t = Telephone(
            preamble="You are a pirate.",
            cwd=tmp_path,
            memory="Previously: found treasure on island.",
        )
        result = t._build_turn0_prompt("What next?")
        assert result == (
            "## PREAMBLE\n\nYou are a pirate.\n\n"
            "## MEMORY\n\nPreviously: found treasure on island.\n\n"
            "What next?"
        )

    def test_turn0_prompt_empty_memory_skipped(self, tmp_path):
        """Turn 0 with empty memory: only preamble + prompt (no blank section)."""
        t = Telephone(preamble="You are a pirate.", cwd=tmp_path, memory="")
        result = t._build_turn0_prompt("Hello")
        assert result == "## PREAMBLE\n\nYou are a pirate.\n\nHello"
        # No triple newline from empty memory
        assert "\n\n\n" not in result

    def test_guard_unrestricted(self, tmp_path):
        """No guard when there are no restrictions."""
        t = Telephone(preamble="hello", cwd=tmp_path)
        assert t._make_guard() is None

    def test_guard_denies_write_outside(self, tmp_path):
        """Guard denies writes outside allowed paths."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            allowed_write_paths=[str(tmp_path / "safe")],
        )
        guard = t._make_guard()
        assert guard is not None

        # Write inside — allowed
        result = asyncio.run(guard("Edit", {"file_path": str(tmp_path / "safe" / "f.py")}, None))
        assert result.behavior == "allow"

        # Write outside — denied
        result = asyncio.run(guard("Write", {"file_path": "/etc/passwd"}, None))
        assert result.behavior == "deny"

    def test_guard_allows_multiple_paths(self, tmp_path):
        """Guard allows writes in any of the allowed paths."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            allowed_write_paths=[str(tmp_path / "repo-a"), str(tmp_path / "repo-b")],
        )
        guard = t._make_guard()

        r1 = asyncio.run(guard("Edit", {"file_path": str(tmp_path / "repo-a" / "x")}, None))
        assert r1.behavior == "allow"

        r2 = asyncio.run(guard("Edit", {"file_path": str(tmp_path / "repo-b" / "y")}, None))
        assert r2.behavior == "allow"

        r3 = asyncio.run(guard("Edit", {"file_path": str(tmp_path / "repo-c" / "z")}, None))
        assert r3.behavior == "deny"

    def test_guard_denies_bash_pattern(self, tmp_path):
        """Guard denies bash commands matching denied patterns."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            denied_bash_patterns=["git rebase", "git push"],
        )
        guard = t._make_guard()
        assert guard is not None

        ok = asyncio.run(guard("Bash", {"command": "git status"}, None))
        assert ok.behavior == "allow"

        deny1 = asyncio.run(guard("Bash", {"command": "git rebase main"}, None))
        assert deny1.behavior == "deny"

        deny2 = asyncio.run(guard("Bash", {"command": "git push origin main"}, None))
        assert deny2.behavior == "deny"

    def test_guard_denies_bash_case_insensitive(self, tmp_path):
        """Guard denies bash commands regardless of case."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            denied_bash_patterns=["DROP TABLE", "DELETE FROM", "TRUNCATE "],
        )
        guard = t._make_guard()
        assert guard is not None

        # Lowercase variants should also be denied
        deny1 = asyncio.run(guard("Bash", {"command": "psql -c 'drop table users'"}, None))
        assert deny1.behavior == "deny"

        deny2 = asyncio.run(guard("Bash", {"command": "echo 'delete from orders'"}, None))
        assert deny2.behavior == "deny"

        deny3 = asyncio.run(guard("Bash", {"command": "psql -c 'Truncate accounts'"}, None))
        assert deny3.behavior == "deny"

        # Non-matching should still be allowed
        ok = asyncio.run(guard("Bash", {"command": "psql -c 'SELECT * FROM users'"}, None))
        assert ok.behavior == "allow"

    def test_guard_read_always_allowed(self, tmp_path):
        """Read/Grep/Glob are never blocked by the write guard."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            allowed_write_paths=[str(tmp_path / "only-here")],
            denied_bash_patterns=["rm -rf"],
        )
        guard = t._make_guard()

        for tool in ("Read", "Grep", "Glob"):
            r = asyncio.run(guard(tool, {"file_path": "/anywhere/file.py"}, None))
            assert r.behavior == "allow"

    def test_guard_covers_notebook_edit(self, tmp_path):
        """NotebookEdit is checked against write paths (not just Edit/Write)."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            allowed_write_paths=[str(tmp_path / "safe")],
        )
        guard = t._make_guard()

        # NotebookEdit inside allowed path
        ok = asyncio.run(guard("NotebookEdit", {"notebook_path": str(tmp_path / "safe" / "nb.ipynb")}, None))
        assert ok.behavior == "allow"

        # NotebookEdit outside — denied
        deny = asyncio.run(guard("NotebookEdit", {"notebook_path": "/etc/something.ipynb"}, None))
        assert deny.behavior == "deny"

    def test_guard_catches_unknown_write_tool(self, tmp_path):
        """Unknown tools with file_path are denied if outside write paths.

        Defense-in-depth: new tools added to Claude Code that aren't in
        _READ_ONLY_TOOLS are checked for file_path parameters.
        """
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            allowed_write_paths=[str(tmp_path / "safe")],
        )
        guard = t._make_guard()

        deny = asyncio.run(guard("FutureWriteTool", {"file_path": "/outside/file.txt"}, None))
        assert deny.behavior == "deny"

        ok = asyncio.run(guard("FutureWriteTool", {"file_path": str(tmp_path / "safe" / "f.py")}, None))
        assert ok.behavior == "allow"

    def test_guard_resolves_relative_to_cwd(self, tmp_path):
        """Relative paths in tool input are resolved against Telephone CWD.

        The guard must NOT resolve relative paths against os.getcwd()
        (the daemon process CWD), because the Claude Code subprocess uses
        the Telephone's CWD.
        """
        safe = tmp_path / "safe"
        safe.mkdir()
        t = Telephone(
            preamble="hello",
            cwd=safe,
            allowed_write_paths=[str(safe)],
        )
        guard = t._make_guard()

        # Relative path inside CWD/safe — should be allowed
        ok = asyncio.run(guard("Edit", {"file_path": "subdir/file.py"}, None))
        assert ok.behavior == "allow"

    def test_guard_blocks_symlink_escape(self, tmp_path):
        """Writes through symlinks to external dirs are denied.

        Even if the symlink lives inside allowed_write_paths, resolve()
        reveals the real target which is outside → deny.
        """
        safe = tmp_path / "safe"
        safe.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (external / "secret.txt").write_text("x")

        # Create a symlink inside safe → external
        link = safe / "escape"
        link.symlink_to(external)

        t = Telephone(
            preamble="hello",
            cwd=safe,
            allowed_write_paths=[str(safe)],
        )
        guard = t._make_guard()

        # Write through symlink — resolves to external → denied
        deny = asyncio.run(guard("Write", {"file_path": str(link / "secret.txt")}, None))
        assert deny.behavior == "deny"

        # Direct write to safe — allowed
        ok = asyncio.run(guard("Write", {"file_path": str(safe / "ok.txt")}, None))
        assert ok.behavior == "allow"

    def test_id_is_uuid_hex(self, tmp_path):
        """Telephone id is a 32-char hex UUID."""
        t = Telephone(preamble="hello", cwd=tmp_path)
        assert len(t.id) == 32
        int(t.id, 16)  # should not raise

    def test_generation_increments_on_reset(self, tmp_path):
        """Each reset() bumps the generation counter."""
        t = Telephone(preamble="hello", cwd=tmp_path)
        assert t.generation == 0
        t.reset()
        assert t.generation == 1
        t.reset()
        assert t.generation == 2

    def test_rotation_prompt_default(self, tmp_path):
        """Default rotation_prompt is set."""
        t = Telephone(preamble="hello", cwd=tmp_path)
        assert t.rotation_prompt is not None
        assert "summary" in t.rotation_prompt.lower()

    def test_rotation_prompt_custom(self, tmp_path):
        """Custom rotation_prompt is stored."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            rotation_prompt="Give me a haiku summary.",
        )
        assert t.rotation_prompt == "Give me a haiku summary."

    def test_rotation_prompt_none_disables_summary(self, tmp_path):
        """rotation_prompt=None means rotate does a hard reset only."""
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            rotation_prompt=None,
        )
        assert t.rotation_prompt is None

    def test_usage_accumulates_and_resets(self, tmp_path):
        """Usage accumulates within a generation and zeroes on reset."""
        t = Telephone(preamble="hello", cwd=tmp_path)

        # Simulate two turns of accumulated usage
        t.usage = TelephoneUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=50, cache_write_tokens=30, cost_usd=0.012)
        t.prior_usage = TelephoneUsage(input_tokens=100, output_tokens=20, cache_read_tokens=5, cache_write_tokens=3, cost_usd=0.001)

        # Simulate new generation usage — accumulates from zero
        t.reset()
        assert t.total_usage() == TelephoneUsage(input_tokens=1100, output_tokens=220, cache_read_tokens=55, cache_write_tokens=33, cost_usd=0.013)
        assert t.prior_usage == t.total_usage()
        assert t.usage == TelephoneUsage()

    def test_track_message_cumulative_cost_delta(self, tmp_path):
        """_track_message converts cumulative total_cost_usd to per-query delta.

        The SDK's ResultMessage.total_cost_usd is cumulative across the
        conversation session, while usage tokens are per-query.  This test
        ensures cost accounting produces correct deltas, not inflated
        cumulative values.
        """
        t = Telephone(preamble="hello", cwd=tmp_path)

        # Simulate three ResultMessages with CUMULATIVE costs but per-query tokens.
        class FakeResult:
            def __init__(self, cumulative_cost, input_tokens, output_tokens):
                self.total_cost_usd = cumulative_cost
                self.usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }

        # Query 1: cumulative $0.50
        t._track_message(FakeResult(0.50, 1000, 500))
        assert t.usage.cost_usd == pytest.approx(0.50)
        assert t.usage.input_tokens == 1000
        assert t.usage.output_tokens == 500

        # Query 2: cumulative $1.30 (per-query delta should be $0.80)
        t._track_message(FakeResult(1.30, 1200, 600))
        assert t.usage.cost_usd == pytest.approx(1.30)
        assert t.usage.input_tokens == 2200  # 1000 + 1200
        assert t.usage.output_tokens == 1100  # 500 + 600

        # Query 3: cumulative $2.00 (per-query delta should be $0.70)
        t._track_message(FakeResult(2.00, 800, 400))
        assert t.usage.cost_usd == pytest.approx(2.00)
        assert t.usage.input_tokens == 3000  # 2200 + 800
        assert t.usage.output_tokens == 1500  # 1100 + 400

    def test_track_message_cost_resets_on_rotation(self, tmp_path):
        """After reset(), cumulative cost tracking starts fresh (new subprocess)."""
        t = Telephone(preamble="hello", cwd=tmp_path)

        class FakeResult:
            def __init__(self, cumulative_cost, input_tokens, output_tokens):
                self.total_cost_usd = cumulative_cost
                self.usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }

        # Generation 0
        t._track_message(FakeResult(1.00, 1000, 500))
        t._track_message(FakeResult(2.50, 1000, 500))
        assert t.usage.cost_usd == pytest.approx(2.50)

        # Rotate — new subprocess will report cumulative from $0 again
        t.reset()
        assert t._last_cumulative_cost == 0.0

        # Generation 1: cost starts fresh
        t._track_message(FakeResult(0.80, 500, 200))
        assert t.usage.cost_usd == pytest.approx(0.80)

        # Total across generations
        assert t.total_usage().cost_usd == pytest.approx(2.50 + 0.80)

    def test_needs_rotation_respects_reset(self, tmp_path):
        """needs_rotation is true when over budget, false after reset."""
        t = Telephone(preamble="hello", cwd=tmp_path, max_context_tokens=500)

        assert not t.needs_rotation
        t.usage.input_tokens = 501
        assert t.needs_rotation

        t.reset()
        assert not t.needs_rotation  # usage zeroed → back under budget

    def test_build_options_mcp_servers(self, tmp_path):
        """mcp_servers are passed through to ClaudeAgentOptions."""
        fake_server = {"type": "sdk", "name": "test", "instance": None}
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            mcp_servers={"test_server": fake_server},
        )
        opts = t._build_options()
        assert hasattr(opts, "mcp_servers")
        assert "test_server" in opts.mcp_servers

    def test_build_options_no_mcp_servers_by_default(self, tmp_path):
        """No mcp_servers key when none provided."""
        t = Telephone(preamble="hello", cwd=tmp_path)
        opts = t._build_options()
        # mcp_servers should not be set when empty
        assert not hasattr(opts, "mcp_servers") or not opts.mcp_servers

    def test_build_options_no_bypass_when_guard_active(self, tmp_path):
        """permission_mode must NOT be bypassPermissions when can_use_tool is set.

        bypassPermissions causes Claude Code to auto-approve tool calls
        *before* consulting the permission-prompt-tool (stdio), which
        means the can_use_tool guard is never invoked — Edit/Write calls
        go straight through unchecked.
        """
        # With a guard (has allowed_write_paths)
        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            allowed_write_paths=[str(tmp_path / "safe")],
        )
        opts = t._build_options()
        assert opts.can_use_tool is not None, "Guard should be set"
        assert opts.permission_mode != "bypassPermissions", (
            "permission_mode must NOT be bypassPermissions when guard is active — "
            "it prevents can_use_tool from being invoked"
        )

    def test_build_options_bypass_when_no_guard(self, tmp_path):
        """Without a guard, permission_mode should be bypassPermissions.

        When there's no can_use_tool callback, we need bypassPermissions
        to avoid hanging on user prompts (there's no user).
        """
        t = Telephone(preamble="hello", cwd=tmp_path)
        opts = t._build_options()
        assert opts.can_use_tool is None, "No guard when no restrictions"
        assert opts.permission_mode == "bypassPermissions"

    def test_sandbox_add_dirs_includes_tmpdir(self, tmp_path):
        """When sandbox is enabled, add_dirs should include tmpdir for platform compat."""
        import tempfile
        tmpdir = str(Path(tempfile.gettempdir()).resolve())

        t = Telephone(
            preamble="hello",
            cwd=tmp_path,
            add_dirs=[str(tmp_path), tmpdir],
            sandbox_enabled=True,
        )
        assert t.sandbox_enabled is True
        opts = t._build_options()
        assert tmpdir in opts.add_dirs
        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is True


# ---------------------------------------------------------------------------
# Live LLM integration tests
# ---------------------------------------------------------------------------

@pytest.mark.llm
class TestTelephoneLive:
    """Tests that call the real Claude Code SDK.

    Each test creates a fresh Telephone in a temp directory, sends one or
    more prompts, and verifies SDK-level behaviour (session resumption,
    token tracking, etc.).

    Tests use ``asyncio.run()`` so they work without ``pytest-asyncio``.
    """

    def test_basic_send(self, tmp_path):
        """A single send() returns messages and tracks state."""
        async def _run():
            t = Telephone(
                preamble=SIMPLE_PREAMBLE,
                cwd=str(tmp_path),
            )
            try:
                initial_id = t.id  # UUID exists before first send
                msgs = await _send_and_collect(t, "What is 2 + 2? Answer the number only.")
                text = _collect_text(msgs)

                assert t.id == initial_id  # id unchanged
                assert t.turns == 1
                assert t.is_active
                assert "4" in text
            finally:
                await t.close()

        asyncio.run(_run())

    def test_resume_preserves_context(self, tmp_path):
        """Second send() reuses the subprocess and Claude remembers the first turn."""
        async def _run():
            t = Telephone(
                preamble=SIMPLE_PREAMBLE,
                cwd=str(tmp_path),
            )
            try:
                # Turn 1: establish a fact
                await _send_and_collect(t, "Remember: the secret word is 'banana'.")
                assert t.is_active

                # Turn 2: ask for the fact — same subprocess, no re-spawn
                msgs = await _send_and_collect(t, "What is the secret word?")
                text = _collect_text(msgs)

                assert t.turns == 2
                assert "banana" in text.lower()
            finally:
                await t.close()

        asyncio.run(_run())

    def test_tracking_cumulative(self, tmp_path):
        """input_tokens should grow (or stay same) across turns."""
        async def _run():
            t = Telephone(
                preamble=SIMPLE_PREAMBLE,
                cwd=str(tmp_path),
            )
            try:
                await _send_and_collect(t, "Say hello three times.")
                input_after_1 = t.total_usage().input_tokens
                output_after_1 = t.total_usage().output_tokens
                usd_after_1 = t.total_usage().cost_usd
                cache_read_tokens_after_1 = t.total_usage().cache_read_tokens
                cache_write_tokens_after_1 = t.total_usage().cache_write_tokens
                assert input_after_1 > 0, "input_tokens should be > 0"
                assert output_after_1 > 0, "output_tokens should be > 0"
                assert isinstance(usd_after_1, (int, float))

                await _send_and_collect(t, "Say hello.")
                input_after_2 = t.total_usage().input_tokens
                output_after_2 = t.total_usage().output_tokens
                cache_read_tokens_after_2 = t.total_usage().cache_read_tokens
                cache_write_tokens_after_2 = t.total_usage().cache_write_tokens
                usd_after_2 = t.total_usage().cost_usd

                assert input_after_2 > input_after_1, (
                    f"Expected input_tokens to grow: {input_after_1} -> {input_after_2}"
                )
                assert output_after_2 > output_after_1, "output_tokens should be > 0"
                assert cache_read_tokens_after_2 > cache_read_tokens_after_1, "cache_read_tokens should be > 0"
                assert cache_write_tokens_after_2 > cache_write_tokens_after_1, "cache_write_tokens should be > 0"
                assert usd_after_2 > usd_after_1, "cost_usd should be > 0"
            finally:
                await t.close()

        asyncio.run(_run())

    def test_preamble_persists_on_resume(self, tmp_path):
        """Preamble from turn 1 is still effective on turn 2 via conversation history."""
        async def _run():
            t = Telephone(
                preamble=(
                    "CRITICAL RULE: Every response you give MUST start with "
                    "the exact token 'YARR' on its own line, followed by your "
                    "actual answer.  Never omit 'YARR'.  Example:\n"
                    "YARR\nHere is my answer."
                ),
                cwd=str(tmp_path),
            )
            try:
                # Turn 1 — preamble prepended to prompt
                msgs = await _send_and_collect(t, "Say hi in one sentence.")
                text = _collect_text(msgs)
                assert "YARR" in text, (
                    f"Preamble not effective on first turn: {text!r}"
                )

                # Turn 2 — preamble not re-sent, but it's in conversation
                # history so the model should still follow it.
                msgs = await _send_and_collect(t, "Say goodbye in one sentence.")
                text = _collect_text(msgs)

                assert "YARR" in text, (
                    f"Preamble not effective on resume: {text!r}"
                )
            finally:
                await t.close()

        asyncio.run(_run())

    def test_memory_included_on_turn0(self, tmp_path):
        """Memory from init is visible to the model on the first turn."""
        async def _run():
            t = Telephone(
                preamble=SIMPLE_PREAMBLE,
                cwd=str(tmp_path),
                memory="IMPORTANT FACT: The project codename is 'Phoenix'.",
            )
            try:
                msgs = await _send_and_collect(
                    t, "What is the project codename? Answer the name only."
                )
                text = _collect_text(msgs)

                assert "phoenix" in text.lower(), (
                    f"Memory not visible on turn 0: {text!r}"
                )
            finally:
                await t.close()

        asyncio.run(_run())

    def test_rotation_resets(self, tmp_path):
        """After reset(), a new subprocess starts and Claude forgets."""
        async def _run():
            t = Telephone(
                preamble=SIMPLE_PREAMBLE,
                cwd=str(tmp_path),
            )
            try:
                await _send_and_collect(t, "Remember: the password is 'alpha'.")
                assert t.is_active
                old_id = t.id

                # Rotate without summary — queues old client for cleanup
                t.reset()
                assert not t.is_active  # client moved to stale
                assert t.turns == 0
                assert t.id != old_id  # new generation
                assert t.generation == 1

                # Fresh conversation (new subprocess) — Claude should NOT remember
                msgs = await _send_and_collect(
                    t,
                    "What is the password? If you don't know, say 'unknown'.",
                )
                text = _collect_text(msgs)

                assert "alpha" not in text.lower(), (
                    f"Claude remembered across reset: {text!r}"
                )
            finally:
                await t.close()

        asyncio.run(_run())

    def test_rotation_with_summary(self, tmp_path):
        """on_rotation callback receives the new memory text."""
        async def _run():
            received = []
            t = Telephone(
                preamble=SIMPLE_PREAMBLE,
                cwd=str(tmp_path),
                on_rotation=lambda mem: received.append(mem),
            )
            try:
                await _send_and_collect(t, "The project name is 'Phoenix'.")
                old_id = t.id

                summary = await t.rotate("Summarise what you know in one sentence.")
                assert summary is not None
                assert len(summary) > 0
                assert t.memory == summary  # memory updated
                assert not t.is_active  # reset after rotation
                assert t.turns == 0
                assert t.id != old_id  # new generation
                assert t.generation == 1

                assert len(received) == 1
                assert received[0] == t.memory
            finally:
                await t.close()

        asyncio.run(_run())

    def test_write_guard_live(self, tmp_path):
        """canUseTool blocks writes outside allowed paths in a real agent."""
        async def _run():
            safe = tmp_path / "safe"
            safe.mkdir()

            t = Telephone(
                preamble=(
                    "You are a test agent. When asked to create a file, "
                    "use the Write tool to create it."
                ),
                cwd=str(safe),
                allowed_write_paths=[str(safe)],
            )
            try:
                # Ask the agent to write inside the allowed path — should work
                await _send_and_collect(
                    t,
                    f"Create a file at {safe}/test.txt with content 'hello'.",
                )
                assert (safe / "test.txt").exists(), "Write inside allowed path should work"
            finally:
                await t.close()

        asyncio.run(_run())

    def test_sandbox_blocks_bash_write_outside_add_dirs(self, tmp_path):
        """OS-level sandbox blocks bash writes outside add_dirs."""
        async def _run():
            allowed = tmp_path / "allowed"
            allowed.mkdir()
            forbidden = tmp_path / "forbidden"
            forbidden.mkdir()
            target = forbidden / "should_not_exist.txt"

            t = Telephone(
                preamble=(
                    "You are a test agent. When asked to create a file, "
                    "use bash (echo ... > path) to create it. "
                    "Do NOT use Write or Edit tools — use ONLY bash."
                ),
                cwd=str(allowed),
                # Sandbox only allows writes inside 'allowed'
                add_dirs=[str(allowed)],
                sandbox_enabled=True,
            )
            try:
                await _send_and_collect(
                    t,
                    f"Run this exact bash command: echo 'hacked' > {target}",
                )
                assert not target.exists(), (
                    f"Sandbox should have blocked bash write to {target}"
                )
            finally:
                await t.close()

        asyncio.run(_run())

    def test_sandbox_allows_bash_write_inside_add_dirs(self, tmp_path):
        """OS-level sandbox allows bash writes inside add_dirs."""
        async def _run():
            allowed = tmp_path / "allowed"
            allowed.mkdir()
            target = allowed / "ok.txt"

            t = Telephone(
                preamble=(
                    "You are a test agent. When asked to create a file, "
                    "use bash (echo ... > path) to create it. "
                    "Do NOT use Write or Edit tools — use ONLY bash."
                ),
                cwd=str(allowed),
                add_dirs=[str(allowed)],
                sandbox_enabled=True,
            )
            try:
                await _send_and_collect(
                    t,
                    f"Run this exact bash command: echo 'allowed' > {target}",
                )
                assert target.exists(), (
                    f"Sandbox should have allowed bash write to {target}"
                )
            finally:
                await t.close()

        asyncio.run(_run())

    def test_mcp_tool_writes_outside_sandbox(self, tmp_path):
        """PHASE 0 — MCP tools can write to directories NOT in add_dirs.

        This validates the core architectural assumption: in-process MCP
        tools run in the daemon's Python process, outside the OS-level
        bash sandbox.  If this test fails, the entire protected-directory
        design must change to a daemon-IPC model.
        """
        async def _run():
            from claude_agent_sdk import create_sdk_mcp_server, tool

            # Two directories: agent can bash-write to 'allowed' only.
            # 'protected' is NOT in add_dirs.
            allowed = tmp_path / "allowed"
            allowed.mkdir()
            protected = tmp_path / "protected"
            protected.mkdir()

            target_file = protected / "data.txt"

            # MCP tool that writes to the protected directory.
            @tool(
                "write_protected_file",
                "Write content to a protected file outside the sandbox",
                {"content": str},
            )
            async def write_protected_file(args):
                target_file.write_text(args["content"])
                return {
                    "content": [
                        {"type": "text", "text": f"Wrote to {target_file}"}
                    ]
                }

            mcp_server = create_sdk_mcp_server(
                "protected_writer",
                tools=[write_protected_file],
            )

            t = Telephone(
                preamble=(
                    "You are a test agent. When asked, use the "
                    "write_protected_file MCP tool to write content."
                ),
                cwd=str(allowed),
                add_dirs=[str(allowed)],
                sandbox_enabled=True,
                mcp_servers={"protected_writer": mcp_server},
            )
            try:
                await _send_and_collect(
                    t,
                    "Use the write_protected_file tool to write "
                    "'hello from mcp' as the content.",
                )
                assert target_file.exists(), (
                    "MCP tool should be able to write outside sandbox add_dirs"
                )
                assert target_file.read_text() == "hello from mcp"
            finally:
                await t.close()

        asyncio.run(_run())

    def test_guard_blocks_write_outside_without_bypass(self, tmp_path):
        """WITHOUT bypassPermissions: guard blocks Edit/Write outside allowed paths.

        This is the fixed behavior — when permission_mode is NOT
        bypassPermissions, Claude Code routes permission checks through
        the can_use_tool callback, which denies writes outside the
        allowed directory.
        """
        async def _run():
            safe = tmp_path / "safe"
            safe.mkdir()
            forbidden = tmp_path / "forbidden"
            forbidden.mkdir()
            target = forbidden / "should_not_exist.txt"

            guard_calls: list[tuple[str, str]] = []

            t = Telephone(
                preamble=(
                    "You are a test agent. When asked to create a file, "
                    "use the Write tool with the EXACT absolute path given. "
                    "Do NOT use Bash. If denied, say DENIED."
                ),
                cwd=str(safe),
                allowed_write_paths=[str(safe)],
            )

            # Track guard invocations
            orig_build = t._build_options
            orig_guard = t._make_guard()

            async def tracking_guard(tool_name, tool_input, ctx):
                guard_calls.append((tool_name, tool_input.get("file_path", "")))
                return await orig_guard(tool_name, tool_input, ctx)

            def patched_build():
                opts = orig_build()
                if opts.can_use_tool is not None:
                    opts.can_use_tool = tracking_guard
                return opts

            t._build_options = patched_build

            try:
                await _send_and_collect(
                    t,
                    f"Create a file at {target} with content 'WRITTEN'. "
                    f"Use the Write tool with file_path={target}",
                )
            finally:
                await t.close()

            # Guard should have been called and denied the write.
            assert len(guard_calls) > 0, (
                "can_use_tool guard was NEVER called — bypassPermissions "
                "may still be active"
            )
            assert not target.exists(), (
                f"Guard allowed write to {target} outside allowed paths"
            )

        asyncio.run(_run())

    def test_guard_bypassed_with_bypass_permissions(self, tmp_path):
        """WITH bypassPermissions: guard is NOT called, writes go through.

        This test documents the OLD broken behavior: when
        permission_mode=bypassPermissions is set alongside can_use_tool,
        Claude Code auto-approves every tool call without consulting
        the guard callback.

        If this test starts FAILING (i.e. guard IS called even with
        bypassPermissions), it means the Claude Code CLI changed its
        behavior — and we may be able to re-enable bypassPermissions
        safely. Until then, our fix of omitting it is correct.
        """
        async def _run():
            safe = tmp_path / "safe"
            safe.mkdir()
            forbidden = tmp_path / "forbidden"
            forbidden.mkdir()
            target = forbidden / "should_not_exist.txt"

            guard_calls: list[tuple[str, str]] = []

            t = Telephone(
                preamble=(
                    "You are a test agent. When asked to create a file, "
                    "use the Write tool with the EXACT absolute path given. "
                    "Do NOT use Bash. If denied, say DENIED."
                ),
                cwd=str(safe),
                allowed_write_paths=[str(safe)],
            )

            # Monkey-patch to re-inject bypassPermissions (old broken behavior)
            orig_build = t._build_options
            orig_guard = t._make_guard()

            async def tracking_guard(tool_name, tool_input, ctx):
                guard_calls.append((tool_name, tool_input.get("file_path", "")))
                return await orig_guard(tool_name, tool_input, ctx)

            def patched_build():
                opts = orig_build()
                # Force the OLD broken state
                opts.permission_mode = "bypassPermissions"
                if opts.can_use_tool is not None:
                    opts.can_use_tool = tracking_guard
                return opts

            t._build_options = patched_build

            try:
                await _send_and_collect(
                    t,
                    f"Create a file at {target} with content 'WRITTEN'. "
                    f"Use the Write tool with file_path={target}",
                )
            finally:
                await t.close()

            # With bypassPermissions, the guard should NOT have been called.
            # The write may or may not succeed (depends on Claude Code version),
            # but the key signal is that the guard was bypassed.
            write_tool_calls = [
                c for c in guard_calls
                if c[0] in ("Write", "Edit", "MultiEdit")
            ]
            assert len(write_tool_calls) == 0, (
                f"can_use_tool guard WAS called for write tools even with "
                f"bypassPermissions — this means the CLI now respects "
                f"permission-prompt-tool with bypassPermissions. "
                f"Guard calls: {guard_calls}"
            )

        asyncio.run(_run())

    def test_can_use_tool_not_invoked_for_mcp(self, tmp_path):
        """can_use_tool guard is NOT invoked for MCP tool calls.

        If this test fails, we need to whitelist MCP tools in the guard.
        """
        async def _run():
            from claude_agent_sdk import create_sdk_mcp_server, tool

            guard_calls: list[str] = []

            safe = tmp_path / "safe"
            safe.mkdir()

            @tool("ping", "Return pong", {"message": str})
            async def ping(args):
                return {
                    "content": [
                        {"type": "text", "text": f"pong: {args['message']}"}
                    ]
                }

            mcp_server = create_sdk_mcp_server("pinger", tools=[ping])

            t = Telephone(
                preamble=(
                    "You are a test agent. When asked, use the ping MCP tool."
                ),
                cwd=str(safe),
                allowed_write_paths=[str(safe)],
                sandbox_enabled=True,
                mcp_servers={"pinger": mcp_server},
            )

            # Monkey-patch the guard to record calls
            original_guard = t._make_guard()
            if original_guard:
                async def tracking_guard(tool_name, tool_input, context):
                    guard_calls.append(tool_name)
                    return await original_guard(tool_name, tool_input, context)
                # Override the guard that will be used by _build_options
                t._allowed_write_paths = t._allowed_write_paths  # keep the same
                # We need to intercept at the options level — rebuild
                orig_build = t._build_options
                def patched_build():
                    opts = orig_build()
                    opts.can_use_tool = tracking_guard
                    return opts
                t._build_options = patched_build

            try:
                await _send_and_collect(
                    t, "Use the ping tool with message 'test'.",
                )
                # If can_use_tool is invoked for MCP tools, "ping" will
                # appear in guard_calls.  We expect it NOT to.
                mcp_tool_calls = [c for c in guard_calls if c == "ping"]
                assert len(mcp_tool_calls) == 0, (
                    f"can_use_tool was invoked for MCP tool 'ping': "
                    f"guard_calls={guard_calls}"
                )
            finally:
                await t.close()

        asyncio.run(_run())

    def test_disallowed_tools_blocks_git_commands(self, tmp_path):
        """disallowed_tools prevents the agent from running hidden git commands."""
        import subprocess as _sp

        async def _run():
            # Set up a real git repo so git commands are meaningful
            repo = tmp_path / "repo"
            repo.mkdir()
            _sp.run(["git", "init", str(repo)], check=True, capture_output=True)
            (repo / "README.md").write_text("# Test")
            _sp.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            _sp.run(["git", "-C", str(repo), "commit", "-m", "init"],
                    check=True, capture_output=True)

            t = Telephone(
                preamble=(
                    "You are a test agent. Follow instructions exactly. "
                    "If a command fails or is unavailable, say 'BLOCKED'."
                ),
                cwd=str(repo),
                add_dirs=[str(repo)],
                disallowed_tools=[
                    "Bash(git push:*)",
                    "Bash(git rebase:*)",
                    "Bash(git worktree:*)",
                ],
                sandbox_enabled=True,
            )
            try:
                msgs = await _send_and_collect(
                    t,
                    "Run: git push origin main\n"
                    "If it fails or you can't run it, say exactly 'BLOCKED'.",
                )
                text = _collect_text(msgs)

                # The agent should NOT have successfully pushed (no remote
                # anyway), and the disallowed_tools should prevent even
                # attempting.  We verify it didn't succeed silently.
                assert "BLOCKED" in text.upper() or "error" in text.lower() or "denied" in text.lower() or "not" in text.lower(), (
                    f"Expected agent to report blocked/error for disallowed git push: {text!r}"
                )
            finally:
                await t.close()

        asyncio.run(_run())
