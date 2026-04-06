"""Authentication for Delegate — satellite bearer tokens and web UI sessions.

Two authentication layers:

1. **Satellite bearer tokens** — satellites authenticate to the coordinator
   via ``Authorization: Bearer <token>`` on ``/internal/*`` routes.
   Tokens are stored in ``protected/satellites.yaml``.

2. **Web UI passphrase** — when a passphrase is configured, all non-internal
   routes require a signed session cookie.  The passphrase hash is stored in
   ``protected/config.yaml``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Satellite token management
# ---------------------------------------------------------------------------

_SATELLITES_FILE = "satellites.yaml"


def _satellites_path(hc_home: Path) -> Path:
    return hc_home / "protected" / _SATELLITES_FILE


def _read_satellites(hc_home: Path) -> dict:
    p = _satellites_path(hc_home)
    if p.exists():
        return yaml.safe_load(p.read_text()) or {}
    return {}


def _write_satellites(hc_home: Path, data: dict) -> None:
    p = _satellites_path(hc_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def add_satellite(hc_home: Path, name: str) -> str:
    """Register a new satellite and return its bearer token.

    The token is displayed once and cannot be retrieved later.
    """
    data = _read_satellites(hc_home)
    satellites = data.get("satellites", {})
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    satellites[name] = {
        "token_hash": token_hash,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    data["satellites"] = satellites
    _write_satellites(hc_home, data)
    return token


def remove_satellite(hc_home: Path, name: str) -> bool:
    """Remove a satellite. Returns True if it existed."""
    data = _read_satellites(hc_home)
    satellites = data.get("satellites", {})
    if name not in satellites:
        return False
    del satellites[name]
    data["satellites"] = satellites
    _write_satellites(hc_home, data)
    return True


def list_satellites(hc_home: Path) -> list[dict]:
    """Return list of registered satellites (without token hashes)."""
    data = _read_satellites(hc_home)
    satellites = data.get("satellites", {})
    result = []
    for name, meta in satellites.items():
        result.append({
            "name": name,
            "created_at": meta.get("created_at", ""),
        })
    return result


def validate_satellite_token(hc_home: Path, token: str) -> str | None:
    """Validate a bearer token. Returns the satellite name if valid, else None."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    data = _read_satellites(hc_home)
    satellites = data.get("satellites", {})
    for name, meta in satellites.items():
        if hmac.compare_digest(meta.get("token_hash", ""), token_hash):
            return name
    return None


# ---------------------------------------------------------------------------
# Web UI passphrase authentication
# ---------------------------------------------------------------------------

_COOKIE_NAME = "delegate_session"
_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days


def set_passphrase(hc_home: Path, passphrase: str) -> None:
    """Store a passphrase hash in config.yaml using argon2id."""
    from argon2 import PasswordHasher
    from delegate.config import _read, _write

    ph = PasswordHasher()
    data = _read(hc_home)
    data["passphrase_hash"] = ph.hash(passphrase)
    # Generate a signing key for session cookies
    if "session_secret" not in data:
        data["session_secret"] = secrets.token_hex(32)
    _write(hc_home, data)


def disable_passphrase(hc_home: Path) -> None:
    """Remove passphrase from config.yaml (disables web auth)."""
    from delegate.config import _read, _write

    data = _read(hc_home)
    data.pop("passphrase_hash", None)
    _write(hc_home, data)


def is_passphrase_enabled(hc_home: Path) -> bool:
    """Check if a passphrase has been configured."""
    from delegate.config import _read

    data = _read(hc_home)
    return bool(data.get("passphrase_hash"))


def verify_passphrase(hc_home: Path, passphrase: str) -> bool:
    """Verify a passphrase against the stored hash.

    Supports both argon2id (preferred) and legacy SHA-256 hashes.
    If a legacy SHA-256 hash is verified successfully, it is automatically
    upgraded to argon2id in place.
    """
    from delegate.config import _read

    data = _read(hc_home)
    stored_hash = data.get("passphrase_hash")
    if not stored_hash:
        return False

    # Argon2 hashes start with "$argon2"
    if stored_hash.startswith("$argon2"):
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError
        ph = PasswordHasher()
        try:
            return ph.verify(stored_hash, passphrase)
        except VerifyMismatchError:
            return False

    # Legacy: SHA-256 hex digest (64 chars)
    candidate = hashlib.sha256(passphrase.encode()).hexdigest()
    if hmac.compare_digest(stored_hash, candidate):
        # Auto-upgrade to argon2id on successful verification
        set_passphrase(hc_home, passphrase)
        logger.info("Upgraded passphrase hash from SHA-256 to argon2id")
        return True
    return False


def _get_session_secret(hc_home: Path) -> str:
    """Return the session signing secret, creating one if needed."""
    from delegate.config import _read, _write

    data = _read(hc_home)
    secret = data.get("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        data["session_secret"] = secret
        _write(hc_home, data)
    return secret


def create_session_cookie(hc_home: Path) -> str:
    """Create a signed session cookie value."""
    secret = _get_session_secret(hc_home)
    payload = {
        "created": int(time.time()),
        "nonce": secrets.token_hex(8),
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    sig = hmac.new(secret.encode(), payload_json.encode(), hashlib.sha256).hexdigest()
    return f"{payload_json}.{sig}"


def validate_session_cookie(hc_home: Path, cookie_value: str) -> bool:
    """Validate a signed session cookie."""
    if not cookie_value or "." not in cookie_value:
        return False
    try:
        payload_json, sig = cookie_value.rsplit(".", 1)
        secret = _get_session_secret(hc_home)
        expected = hmac.new(secret.encode(), payload_json.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        payload = json.loads(payload_json)
        created = payload.get("created", 0)
        if time.time() - created > _COOKIE_MAX_AGE:
            return False
        return True
    except Exception:
        return False
