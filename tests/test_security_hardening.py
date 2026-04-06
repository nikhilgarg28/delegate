"""Phase 10: Comprehensive security hardening tests.

Covers:
- UUID round-trip (register → resolve → reverse-resolve)
- add_dirs scope validation (protected/ never writable from agents)
- Denied bash patterns completeness
- Sandbox configuration per role
"""

import uuid

import pytest

from delegate.paths import (
    daemon_lock_path,
    invalidate_team_map_cache,
    protected_dir,
    register_team_path,
    resolve_team_name,
    resolve_team_uuid,
    team_dir,
)
from delegate.runtime import (
    DENIED_BASH_PATTERNS,
    DISALLOWED_TOOLS,
    _repo_git_dirs,
    _write_paths_for_role,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_hc(tmp_path):
    """Temporary Delegate home with protected/ and teams/."""
    hc = tmp_path / "hc"
    hc.mkdir()
    (hc / "protected").mkdir()
    (hc / "teams").mkdir()
    invalidate_team_map_cache(hc)
    return hc


# ---------------------------------------------------------------------------
# UUID round-trip tests
# ---------------------------------------------------------------------------

class TestUUIDRoundTrip:
    """Verify that team name ↔ UUID resolution is stable and reversible."""

    def test_register_and_resolve(self, tmp_hc):
        """Registering a team creates a stable name → UUID mapping."""
        uid = uuid.uuid4().hex
        register_team_path(tmp_hc, "myteam", uid)
        assert resolve_team_uuid(tmp_hc, "myteam") == uid

    def test_reverse_resolve(self, tmp_hc):
        """UUID can be resolved back to the team name."""
        uid = uuid.uuid4().hex
        register_team_path(tmp_hc, "myteam", uid)
        assert resolve_team_name(tmp_hc, uid) == "myteam"

    def test_full_round_trip(self, tmp_hc):
        """name → uuid → name round-trip is identity."""
        uid = uuid.uuid4().hex
        register_team_path(tmp_hc, "alpha", uid)

        resolved_uuid = resolve_team_uuid(tmp_hc, "alpha")
        resolved_name = resolve_team_name(tmp_hc, resolved_uuid)
        assert resolved_name == "alpha"

    def test_multiple_teams_independent(self, tmp_hc):
        """Multiple teams have distinct UUIDs."""
        uid_a = uuid.uuid4().hex
        uid_b = uuid.uuid4().hex
        register_team_path(tmp_hc, "alpha", uid_a)
        register_team_path(tmp_hc, "beta", uid_b)

        assert resolve_team_uuid(tmp_hc, "alpha") == uid_a
        assert resolve_team_uuid(tmp_hc, "beta") == uid_b
        assert uid_a != uid_b

    def test_unregistered_team_returns_name(self, tmp_hc):
        """Unregistered team name falls back to name itself."""
        result = resolve_team_uuid(tmp_hc, "no-such-team")
        assert result == "no-such-team"

    def test_unknown_uuid_returns_uuid(self, tmp_hc):
        """Reverse-resolving unknown UUID returns the UUID itself."""
        fake_uuid = "deadbeef1234"
        assert resolve_team_name(tmp_hc, fake_uuid) == fake_uuid

    def test_team_dir_uses_uuid(self, tmp_hc):
        """team_dir() returns path with UUID, not human name."""
        uid = uuid.uuid4().hex
        register_team_path(tmp_hc, "myteam", uid)

        td = team_dir(tmp_hc, "myteam")
        assert uid in str(td)
        assert "myteam" not in str(td)

    def test_re_register_overwrites(self, tmp_hc):
        """Re-registering with a new UUID updates the mapping."""
        uid1 = uuid.uuid4().hex
        uid2 = uuid.uuid4().hex
        register_team_path(tmp_hc, "myteam", uid1)
        register_team_path(tmp_hc, "myteam", uid2)

        assert resolve_team_uuid(tmp_hc, "myteam") == uid2

    def test_persistence_across_cache_invalidation(self, tmp_hc):
        """Mapping survives cache invalidation (disk persistence)."""
        uid = uuid.uuid4().hex
        register_team_path(tmp_hc, "myteam", uid)

        invalidate_team_map_cache(tmp_hc)

        assert resolve_team_uuid(tmp_hc, "myteam") == uid


# ---------------------------------------------------------------------------
# add_dirs scope validation
# ---------------------------------------------------------------------------

class TestAddDirsScope:
    """Verify that sandbox add_dirs never include protected/ or other teams."""

    def test_protected_dir_not_in_worker_paths(self, tmp_hc):
        """Worker write paths must not include protected/."""
        uid = uuid.uuid4().hex
        register_team_path(tmp_hc, "team1", uid)

        paths = _write_paths_for_role(tmp_hc, "team1", "alice", "engineer")
        pdir = str(protected_dir(tmp_hc))
        for p in paths:
            assert not p.startswith(pdir), (
                f"Worker path '{p}' must not be under protected/"
            )

    def test_protected_dir_not_in_manager_paths(self, tmp_hc):
        """Manager write paths must not include protected/."""
        uid = uuid.uuid4().hex
        register_team_path(tmp_hc, "team1", uid)

        paths = _write_paths_for_role(tmp_hc, "team1", "delegate", "manager")
        pdir = str(protected_dir(tmp_hc))
        for p in paths:
            assert not p.startswith(pdir), (
                f"Manager path '{p}' must not be under protected/"
            )

    def test_worker_paths_scoped_to_team(self, tmp_hc):
        """Worker write paths should only reference their team."""
        uid_a = uuid.uuid4().hex
        uid_b = uuid.uuid4().hex
        register_team_path(tmp_hc, "team_a", uid_a)
        register_team_path(tmp_hc, "team_b", uid_b)

        paths = _write_paths_for_role(tmp_hc, "team_a", "alice", "engineer")
        for p in paths:
            assert uid_b not in p, (
                f"Worker in team_a has path referencing team_b UUID"
            )

    def test_manager_paths_scoped_to_team(self, tmp_hc):
        """Manager write paths reference only their own team dir."""
        uid_a = uuid.uuid4().hex
        uid_b = uuid.uuid4().hex
        register_team_path(tmp_hc, "team_a", uid_a)
        register_team_path(tmp_hc, "team_b", uid_b)

        paths = _write_paths_for_role(tmp_hc, "team_a", "delegate", "manager")
        assert len(paths) == 1
        assert uid_a in paths[0]
        assert uid_b not in paths[0]


# ---------------------------------------------------------------------------
# Denied bash patterns completeness
# ---------------------------------------------------------------------------

class TestDeniedBashPatterns:
    """Verify that critical commands are in the deny list."""

    MUST_DENY = [
        "git push",
        "git rebase",
        "git merge",
        "git pull",
        "git fetch",
        "git checkout",
        "git switch",
        "git reset --hard",
        "git reset --soft",
        "git worktree",
        "git branch",
        "git remote",
        "git filter-branch",
        "git reflog expire",
        "sqlite3 ",
        "rm -rf .git",
        "DROP TABLE",
        "DELETE FROM",
        "TRUNCATE ",
        "ALTER TABLE",
    ]

    @pytest.mark.parametrize("pattern", MUST_DENY)
    def test_pattern_in_deny_list(self, pattern):
        """Every critical command must appear in DENIED_BASH_PATTERNS."""
        assert pattern in DENIED_BASH_PATTERNS, (
            f"'{pattern}' missing from DENIED_BASH_PATTERNS"
        )


class TestDisallowedTools:
    """Verify that dangerous tool patterns are blocked."""

    MUST_BLOCK = [
        "Bash(git rebase:*)",
        "Bash(git merge:*)",
        "Bash(git push:*)",
        "Bash(git fetch:*)",
        "Bash(git checkout:*)",
        "Bash(git switch:*)",
        "Bash(git reset --hard:*)",
        "Bash(git reset --soft:*)",
        "Bash(git worktree:*)",
        "Bash(git branch:*)",
        "Bash(git remote:*)",
    ]

    @pytest.mark.parametrize("pattern", MUST_BLOCK)
    def test_tool_in_disallowed_list(self, pattern):
        """Every dangerous tool pattern must be in DISALLOWED_TOOLS."""
        assert pattern in DISALLOWED_TOOLS, (
            f"'{pattern}' missing from DISALLOWED_TOOLS"
        )


# ---------------------------------------------------------------------------
# Daemon lock path in protected
# ---------------------------------------------------------------------------

class TestDaemonLockPath:
    def test_lock_path_in_protected(self, tmp_hc):
        """Daemon lock file should be under protected/."""
        lp = daemon_lock_path(tmp_hc)
        assert str(lp).startswith(str(protected_dir(tmp_hc)))
