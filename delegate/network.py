"""Network allowlist management.

The global network allowlist lives at ``protected/network.yaml`` and
controls which domains agents are permitted to contact.  The file is
outside the agent sandbox so agents cannot tamper with it.

Format::

    allowedDomains:
      - "pypi.org"
      - "*.github.com"
      # ...or add your own

The default allowlist covers common package managers and git forges so
agents can install dependencies and fetch code out of the box.  You can
customise it with the ``delegate network`` CLI commands:

    delegate network allow  <domain>
    delegate network disallow <domain>
    delegate network reset          # restore defaults
    delegate network list

The allowlist is read at Telephone creation time and changes trigger
Telephone recreation (same pattern as repo-list change detection).

Implementation note
~~~~~~~~~~~~~~~~~~~
Claude Code's sandbox proxy blocks *all* outbound network unless
``sandbox.network.allowedDomains`` is explicitly set.  Bare ``"*"``
does NOT work as a wildcard — each domain must be listed individually
or use the ``*.example.com`` pattern.
"""

import re
import logging
from pathlib import Path
from typing import Any

import yaml

from delegate.paths import network_config_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default domain allowlist
#
# These domains are allowed by default so that agents can install
# dependencies and interact with common dev infrastructure without
# manual configuration.  Edit via ``delegate network`` CLI.
# ---------------------------------------------------------------------------

# The canonical domain list is composed from adapter domain groups.
# See ``delegate.adapters`` for the individual groups (CORE_DOMAINS,
# DOMAIN_GROUPS).  This module re-exports the composed list so that
# existing callers don't need to change.
from delegate.adapters import build_default_domains as _build_default_domains

DEFAULT_DOMAINS: list[str] = _build_default_domains()
"""Curated list of domains agents commonly need.  Exported so that
other modules (e.g. tests, CLI help text) can reference it."""

# ---------------------------------------------------------------------------
# Package-manager cache environment variables
#
# Claude Code's sandbox restricts writes to the agent's cwd + add_dirs.
# System-wide caches (e.g. ~/.cache/uv, ~/.npm) are therefore unwritable.
# We redirect every major package manager's cache to a shared team-level
# directory so that:
#   1) Downloads succeed inside the sandbox
#   2) All agents in the same team share a warm cache
#
# The cache root is computed at Telephone creation time and injected
# via Claude Code's ``settings.env`` mechanism (applies to every bash
# command automatically).
# ---------------------------------------------------------------------------

PKG_CACHE_ENV_VARS: dict[str, str] = {
    # Python
    "PIP_CACHE_DIR":       "{cache_root}/pip",
    "UV_CACHE_DIR":        "{cache_root}/uv",
    # Node
    "npm_config_cache":    "{cache_root}/npm",
    "YARN_CACHE_FOLDER":   "{cache_root}/yarn",
    "PNPM_HOME":           "{cache_root}/pnpm",
    # Rust
    "CARGO_HOME":          "{cache_root}/cargo",
    # Go
    "GOMODCACHE":          "{cache_root}/gomod",
    # Ruby
    "GEM_HOME":            "{cache_root}/gem",
    "BUNDLE_PATH":         "{cache_root}/bundle",
    # Java / Kotlin
    "GRADLE_USER_HOME":    "{cache_root}/gradle",
    # Maven: no dedicated env-var — ``-Dmaven.repo.local`` is needed
    # on the command line.  GRADLE_USER_HOME covers Gradle repos.
    # .NET
    "NUGET_PACKAGES":      "{cache_root}/nuget",
    # Swift / iOS
    "CP_HOME_DIR":         "{cache_root}/cocoapods",
    # Dart / Flutter
    "PUB_CACHE":           "{cache_root}/pub",
    # PHP
    "COMPOSER_CACHE_DIR":  "{cache_root}/composer",
    # Elixir
    "HEX_HOME":            "{cache_root}/hex",
    "MIX_HOME":            "{cache_root}/mix",
}
"""Template for cache-redirect env vars.  ``{cache_root}`` is replaced
with the actual team-level cache directory at runtime."""


def build_cache_env(cache_root: str | Path) -> dict[str, str]:
    """Return a concrete env dict with all cache dirs pointing to *cache_root*.

    Usage::

        env = build_cache_env("/path/to/teams/<uuid>/.pkg-cache")
        # → {"PIP_CACHE_DIR": "/path/to/teams/<uuid>/.pkg-cache/pip", ...}
    """
    root = str(cache_root)
    return {k: v.format(cache_root=root) for k, v in PKG_CACHE_ENV_VARS.items()}


def _default_config() -> dict[str, Any]:
    """Return a fresh copy of the default config (avoids shared-list mutation)."""
    return {"allowedDomains": list(DEFAULT_DOMAINS)}

# Domain validation pattern:
#   - "*.example.com" (wildcard subdomain)
#   - "example.com" (exact domain)
#   - "sub.example.com" (exact domain with subdomain)
_DOMAIN_PATTERN = re.compile(
    r"^((\*\.)?[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*)$"
)


def _validate_domain(domain: str) -> None:
    """Raise ValueError if *domain* is not a valid domain pattern."""
    if not _DOMAIN_PATTERN.match(domain):
        raise ValueError(
            f"Invalid domain pattern: '{domain}'. "
            "Must be a domain like 'example.com' "
            "or a wildcard like '*.example.com'."
        )


def load_config(hc_home: Path) -> dict[str, Any]:
    """Load the network config, returning defaults if the file is missing."""
    path = network_config_path(hc_home)
    if not path.exists():
        return _default_config()
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        logger.warning("Corrupt network.yaml — returning defaults")
        return _default_config()
    if "allowedDomains" not in data:
        data["allowedDomains"] = list(DEFAULT_DOMAINS)
    # Migrate legacy wildcard-only configs to the curated default list.
    # Old configs had ``["*"]`` which doesn't actually work with the
    # Claude Code sandbox proxy.
    if data["allowedDomains"] == ["*"]:
        logger.info("Migrating legacy network.yaml wildcard to default domain list")
        data["allowedDomains"] = list(DEFAULT_DOMAINS)
        save_config(hc_home, data)
    return data


def save_config(hc_home: Path, config: dict[str, Any]) -> None:
    """Write the network config to disk."""
    path = network_config_path(hc_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(config, default_flow_style=False))


def get_allowed_domains(hc_home: Path) -> list[str]:
    """Return the current domain allowlist."""
    return load_config(hc_home).get("allowedDomains", list(DEFAULT_DOMAINS))


def allow_domain(hc_home: Path, domain: str) -> list[str]:
    """Add a domain to the allowlist. Returns the updated list."""
    _validate_domain(domain)
    config = load_config(hc_home)
    domains = config.get("allowedDomains", list(DEFAULT_DOMAINS))

    if domain in domains:
        return domains  # Already present

    domains.append(domain)
    config["allowedDomains"] = domains
    save_config(hc_home, config)
    return domains


def disallow_domain(hc_home: Path, domain: str) -> list[str]:
    """Remove a domain from the allowlist. Returns the updated list."""
    _validate_domain(domain)
    config = load_config(hc_home)
    domains = config.get("allowedDomains", list(DEFAULT_DOMAINS))

    if domain not in domains:
        raise ValueError(f"Domain '{domain}' is not in the allowlist.")

    domains.remove(domain)
    config["allowedDomains"] = domains
    save_config(hc_home, config)
    return domains


def reset_config(hc_home: Path) -> list[str]:
    """Reset the allowlist to the curated default domain list."""
    config = _default_config()
    save_config(hc_home, config)
    return config["allowedDomains"]
