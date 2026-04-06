"""Adapter registry for technology-specific capabilities.

Delegate's core infrastructure (background processes, artifacts, environment
probing) is technology-neutral.  This module isolates all
technology-specific implementations behind simple registries so that:

- Adding support for new hardware (AMD ROCm, TPU, …) is a single function.
- Network domain groups can be composed per-team instead of globally.
- Artifact categories are co-located with their adapters.

The registries are plain dicts populated at import time — no plugin discovery,
no metaclasses, no configuration UI.  Convention over configuration: if a
library is importable, the adapter is available.

Usage from other modules::

    from delegate.adapters import (
        probe_environment,       # replaces _probe_hardware()
        DOMAIN_GROUPS,           # "ml", etc.
        DEFAULT_ARTIFACT_CATEGORIES,
    )
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Environment Probes
# ═══════════════════════════════════════════════════════════════════════════

# Registry: name → callable returning list[str] of info lines.
# Each probe is independent; failures are silently skipped.
ENVIRONMENT_PROBES: dict[str, Any] = {}


def _register_probe(name: str):
    """Decorator to register an environment probe function."""
    def decorator(fn):
        ENVIRONMENT_PROBES[name] = fn
        return fn
    return decorator


@_register_probe("gpu_nvidia")
def _probe_nvidia_gpu() -> list[str]:
    """Detect NVIDIA GPUs via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,driver_version,compute_cap",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = []
            for i, line in enumerate(result.stdout.strip().splitlines()):
                lines.append(f"GPU {i}: {line.strip()}")
            return lines
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ["GPU: None (CPU only)"]


@_register_probe("cuda_toolkit")
def _probe_cuda_toolkit() -> list[str]:
    """Detect CUDA toolkit version via nvcc."""
    try:
        result = subprocess.run(
            ["nvcc", "--version"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "release" in line.lower():
                    return [f"CUDA: {line.strip()}"]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return []


@_register_probe("cpu")
def _probe_cpu() -> list[str]:
    """Report CPU core count."""
    return [f"CPU cores: {os.cpu_count()}"]


@_register_probe("ram")
def _probe_ram() -> list[str]:
    """Report total RAM from /proc/meminfo (Linux)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    gb = int(line.split()[1]) / (1024 * 1024)
                    return [f"RAM: {gb:.1f} GB"]
    except Exception:
        pass
    return []


@_register_probe("disk")
def _probe_disk() -> list[str]:
    """Report free disk space on /."""
    try:
        import shutil as _shutil
        _total, _used, free = _shutil.disk_usage("/")
        return [f"Disk free: {free / (1024**3):.0f} GB"]
    except Exception:
        pass
    return []


# Cached result — hardware doesn't change during a process's lifetime.
_probe_cache: str | None = None


def probe_environment() -> str:
    """Run all registered probes and return a formatted string.

    Results are cached after the first call.
    """
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache

    info: list[str] = []
    for name, probe_fn in ENVIRONMENT_PROBES.items():
        try:
            info.extend(probe_fn())
        except Exception:
            logger.debug("Probe %s failed", name, exc_info=True)

    _probe_cache = "\n".join(info)
    return _probe_cache


# ═══════════════════════════════════════════════════════════════════════════
# 2. Network Domain Groups
# ═══════════════════════════════════════════════════════════════════════════

# Domains are split into thematic groups.  All groups are included in the
# default allowlist, but the separation makes it possible to compose
# per-team allowlists in the future.

CORE_DOMAINS: list[str] = [
    # ── Python (pip / uv / poetry) ──
    "pypi.org",
    "files.pythonhosted.org",
    # ── Node (npm / yarn / pnpm) ──
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    # ── Rust (cargo) ──
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    # ── Go ──
    "proxy.golang.org",
    "sum.golang.org",
    "storage.googleapis.com",
    # ── Ruby (gem / bundler) ──
    "rubygems.org",
    "index.rubygems.org",
    # ── Java / Kotlin (Maven / Gradle) ──
    "repo1.maven.org",
    "repo.maven.apache.org",
    "plugins.gradle.org",
    "services.gradle.org",
    "jcenter.bintray.com",
    # ── .NET (NuGet) ──
    "api.nuget.org",
    "*.nuget.org",
    # ── Swift / iOS (CocoaPods + SPM uses GitHub) ──
    "cdn.cocoapods.org",
    "trunk.cocoapods.org",
    # ── Dart / Flutter (pub) ──
    "pub.dev",
    "*.pub.dev",
    # ── PHP (Composer / Packagist) ──
    "packagist.org",
    "repo.packagist.org",
    # ── Elixir (Hex) ──
    "hex.pm",
    "repo.hex.pm",
    "builds.hex.pm",
    # ── Haskell (Hackage / Stackage) ──
    "hackage.haskell.org",
    # ── Git forges ──
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
    "gitlab.com",
    "*.gitlab.com",
    "bitbucket.org",
    "*.bitbucket.org",
]

DOMAIN_GROUPS: dict[str, list[str]] = {
    "ml": [
        "download.pytorch.org",
        "huggingface.co",
        "*.huggingface.co",
        "data.pyg.org",
        "conda.anaconda.org",
        "repo.anaconda.com",
    ],
}


def build_default_domains() -> list[str]:
    """Compose the full default domain allowlist from all groups."""
    result = list(CORE_DOMAINS)
    for domains in DOMAIN_GROUPS.values():
        result.extend(domains)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 3. Artifact Categories
# ═══════════════════════════════════════════════════════════════════════════

# Mapping from category slug (used in tool schema enums) to subdirectory name.
# Extensible: add new entries here — setup_artifacts() and artifact_save
# will pick them up automatically.
DEFAULT_ARTIFACT_CATEGORIES: dict[str, str] = {
    "model": "models",
    "log": "logs",
    "report": "reports",
    "data": "data",
    "output": "outputs",
}
