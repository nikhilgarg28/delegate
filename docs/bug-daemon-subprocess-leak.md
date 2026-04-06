# Bug Report: Daemon Subprocess Leak — Orphaned Claude Processes

**Date**: 2026-03-21
**Severity**: Critical (memory exhaustion, system overload)
**Status**: Diagnosed, fix pending

## Summary

The delegate daemon leaks Claude Code subprocesses on every Telephone rotation
and on error-path removal. Over a 30-hour daemon session, **230 orphaned
processes** accumulated (192 rana-ji + 38 pratap), consuming **46.6 GB of RAM**
and **~14,600 threads** while doing nothing. Zero of them are connected to the
daemon — they are all idle zombies.

## System Impact (snapshot at 2026-03-21T19:30Z)

| Metric              | Actual   | Expected |
|---------------------|----------|----------|
| Claude processes    | 230      | ~15      |
| Total RSS           | 46.6 GB  | ~6 GB    |
| Total threads       | ~14,600  | ~960     |
| System load average | 192      | <10      |
| Daemon FDs          | 404      | ~60      |
| Disk (`~/.delegate`)| 98 GB    | —        |

Only **9 agents** across 4 teams have ever taken a turn in this daemon session.
Each should have exactly 1 subprocess. The remaining 221 are leaked.

## Root Cause

### Leak Path 1: Rotation Cleanup Fails Silently

Every ~5 minutes, agent preambles change (task list / status updates). The
daemon detects this and calls `tel.rotate()`:

```
rotate() → reset()
  → _stale_client = _client   (old subprocess queued for cleanup)
  → _client = None

Next turn → send() → _ensure_client()
  → await _stale_client.disconnect()   ← THIS FAILS SILENTLY
  → _stale_client = None               ← reference dropped, process lives on
  → new client created                  ← another subprocess spawned
```

`_ensure_client()` wraps the disconnect in `try/except Exception: pass`
(telephone.py:639-642). The SDK's `transport.close()` has **multiple nested**
`with suppress(Exception)` blocks around `process.terminate()` and
`process.wait()`. If any step fails — timeout, asyncio cancellation,
I/O error — the exception is silently swallowed and the process is never
killed.

Evidence:
- `delegate` agent reached **generation 105** (105 subprocesses spawned for one agent)
- `lead` agent reached **generation 50**
- 84 preamble-triggered rotations logged
- **Zero** Claude processes have their stdin/stdout sockets connected to the daemon
  (verified by matching socket inodes between daemon FDs and process FDs)
- Manual `kill -TERM <pid>` instantly kills any orphaned process

### Leak Path 2: `exchange.remove()` Doesn't Close

In `runtime.py:1052`, when a fatal error occurs during a turn:

```python
exchange.remove(team, agent)  # pops Telephone from dict, returns it
                               # but NEVER calls tel.close()!
```

The Telephone object (and its subprocess) is silently dropped. The subprocess
keeps running as an orphan child of the daemon.

### Why SIGTERM Should Work But Doesn't Reach The Process

The Claude binary (`claude_agent_sdk/_bundled/claude`) is a native ELF
executable with 64 threads. It does **not** catch SIGTERM — the default
handler would terminate it immediately. Manually sending `kill -TERM <pid>`
kills the process within 500ms.

The problem is that the SDK's `transport.close()` either:
1. Never reaches `process.terminate()` (earlier steps fail/hang), or
2. Calls `terminate()` but `process.wait()` is interrupted by asyncio task
   cancellation, and the `with suppress(Exception)` block discards the error
   before the process actually exits.

## Reproduction

On any delegate daemon with agents that have frequent preamble changes:

1. Start daemon, observe process count: `ps aux | grep 'claude.*stream-json' | wc -l`
2. Wait 1 hour with active agents
3. Process count will grow monotonically — never decreases

## Proposed Fixes

### Fix 1: `exchange.remove()` must close the telephone

```python
# runtime.py — error path in run_turn()
tel = exchange.remove(team, agent)
if tel is not None:
    try:
        await tel.close()
    except Exception:
        pass
```

### Fix 2: `_ensure_client()` — aggressive stale cleanup with SIGKILL fallback

```python
# telephone.py — _ensure_client()
if self._stale_client is not None:
    try:
        await self._stale_client.disconnect()
    except Exception:
        # Fallback: force-kill the subprocess if disconnect() failed
        try:
            proc = getattr(self._stale_client, '_transport', None)
            if proc and hasattr(proc, '_process') and proc._process:
                proc._process.kill()  # SIGKILL
        except Exception:
            pass
    self._stale_client = None
```

### Fix 3: `transport.close()` — add timeout + SIGKILL escalation

```python
# SDK transport/subprocess_cli.py — close()
if self._process.returncode is None:
    self._process.terminate()
    try:
        # Wait up to 5 seconds for graceful shutdown
        with anyio.fail_after(5):
            await self._process.wait()
    except (TimeoutError, Exception):
        # Force kill if SIGTERM didn't work
        with suppress(ProcessLookupError):
            self._process.kill()
            await self._process.wait()
```

### Fix 4: Periodic orphan reaper in daemon loop

Add a background task that periodically checks for child Claude processes
not tracked by the TelephoneExchange and kills them:

```python
async def _reap_orphaned_claudes(exchange, interval=300):
    """Kill child Claude processes not tracked by the exchange."""
    while True:
        await asyncio.sleep(interval)
        tracked_pids = set()
        for tel in exchange._telephones.values():
            if tel._client and tel._client._transport:
                proc = tel._client._transport._process
                if proc:
                    tracked_pids.add(proc.pid)
        for child in psutil.Process().children():
            if child.pid not in tracked_pids and 'claude' in child.name():
                child.terminate()
```

## Immediate Mitigation

Kill all orphaned Claude processes to reclaim ~47 GB RAM:

```bash
# Kill all rana-ji Claude agent subprocesses (daemon will recreate needed ones)
pkill -TERM -P 301708 -f "claude.*stream-json"
```

## Related Data

- Daemon PID: 301708 (started 2026-03-20 13:48:44, uptime ~30h)
- Active teams: ranatrading (12 agents, 483 tasks), poly (6 agents), dlgt (4 agents), trading (3 agents)
- Total sessions in DB: 3,518 (12 open)
- Unread messages: 681 (529 for ranaji)
- Worktree disk: 93 GB (ranatrading alone)
