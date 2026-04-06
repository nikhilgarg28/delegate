"""Deterministic generation of .delegate/setup.sh and .delegate/premerge.sh.

Inspects a repo directory to detect tooling (Python, Node, Rust, Go, Ruby,
Nix) and produces first-pass environment scripts.  These are correct for
the common case; agents can modify them during tasks if the repo has unusual
needs.

Handles multi-language repos (e.g. Rust backend + Python server + TypeScript
infra) by scanning both the root and all top-level subdirectories for stacks,
then composing their setup/test steps into unified scripts.

Usage::

    from delegate.env import generate_env_scripts, write_env_scripts

    # Just get the script contents:
    setup, premerge = generate_env_scripts(Path("/path/to/repo"))

    # Or write + git-commit into a worktree:
    write_env_scripts(Path("/path/to/worktree"))
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _has_file(root: Path, name: str) -> bool:
    return (root / name).is_file()


def _pyproject_has_section(root: Path, section: str) -> bool:
    """Check if pyproject.toml contains a TOML section header like [dependency-groups]."""
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return False
    try:
        content = pp.read_text()
        return f"[{section}]" in content
    except Exception:
        return False


def _pyproject_has_dev_deps(root: Path) -> bool:
    """Check if pyproject.toml defines dev dependencies (extras or dependency-groups).

    Returns True if pyproject.toml contains:
    - ``[project.optional-dependencies]`` with a ``dev`` key, OR
    - ``[dependency-groups]`` (PEP 735)

    When False, ``pip install ".[dev]"`` would fail, so callers should
    fall back to ``requirements.txt`` or plain ``"."``.
    """
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return False
    try:
        content = pp.read_text()
        # PEP 735 dependency-groups — any group counts
        if "[dependency-groups]" in content:
            return True
        # [project.optional-dependencies] with a dev = [...] key
        if "[project.optional-dependencies]" in content:
            # Look for a line starting with 'dev' after the section header
            in_section = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    in_section = stripped == "[project.optional-dependencies]"
                    continue
                if in_section and (stripped.startswith("dev ") or stripped.startswith("dev=")):
                    return True
        return False
    except Exception:
        return False


def _package_json_has_script(root: Path, script: str) -> str | None:
    """Return the script command if package.json has a matching script, else None."""
    pj = root / "package.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text())
        return data.get("scripts", {}).get(script)
    except Exception:
        return None


def _node_install_cmd(directory: Path) -> str:
    """Return the correct npm/yarn/pnpm install command for a directory."""
    if _has_file(directory, "pnpm-lock.yaml"):
        return "pnpm install --frozen-lockfile --silent"
    if _has_file(directory, "yarn.lock"):
        return "yarn install --frozen-lockfile --silent"
    if _has_file(directory, "package-lock.json"):
        return "npm ci --silent"
    return "npm install --silent"


# Directories to skip when scanning subdirs
_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "dist", "build", ".delegate", "vendor", "target",
    ".tox", ".mypy_cache", ".pytest_cache", ".next", ".nuxt",
    "htmlcov", "coverage", "egg-info",
})


# ---------------------------------------------------------------------------
# Component — one detected language/tooling stack at a specific path
# ---------------------------------------------------------------------------

class _Component:
    """A detected language stack at a specific directory."""

    def __init__(
        self,
        *,
        name: str,           # "python-uv-lock", "node", "rust", etc.
        rel_path: str,       # "." for root, "frontend" for subdir
        setup_snippet: str,  # bash lines to install deps (self-contained)
        test_cmd: str,       # bash command to run tests
        install_src: str = "",  # Python: pip install target (e.g. '".[dev]"')
    ):
        self.name = name
        self.rel_path = rel_path
        self.setup_snippet = setup_snippet
        self.test_cmd = test_cmd
        self.install_src = install_src

    @property
    def is_root(self) -> bool:
        return self.rel_path == "."

    @property
    def is_python(self) -> bool:
        return self.name.startswith("python")


def _parse_envrc(directory: Path) -> set[str]:
    """Parse .envrc and return a set of detected hints.

    Recognized hints:
        "nix", "flake", "python", "poetry", "node", "ruby"

    Returns an empty set if no .envrc exists or it can't be read.
    """
    envrc = directory / ".envrc"
    if not envrc.is_file():
        return set()
    try:
        content = envrc.read_text()
    except Exception:
        return set()

    hints: set[str] = set()
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        if "use flake" in line or "use_flake" in line:
            hints.add("flake")
            hints.add("nix")
        elif "use nix" in line or "use_nix" in line:
            hints.add("nix")
        if "layout python" in line or "layout_python" in line:
            hints.add("python")
        if "layout poetry" in line or "layout_poetry" in line:
            hints.add("poetry")
        if "layout node" in line or "layout_node" in line:
            hints.add("node")
        if "layout ruby" in line or "layout_ruby" in line:
            hints.add("ruby")
    return hints


def _detect_at(directory: Path, rel_path: str = ".") -> _Component | None:
    """Detect the stack at a single directory.  Returns None if nothing found.

    Detection sources (in priority order):
    1. Lock files and manifest files (most specific)
    2. ``.envrc`` hints from direnv (fills in gaps if no manifest found)

    *rel_path* is the path relative to the repo root ("." for root,
    "frontend" for a subdir, etc.).  Generated bash snippets are
    self-contained and scoped to the correct directory.
    """
    is_root = rel_path == "."

    # Helper to build a path expression for bash
    if is_root:
        dir_expr = '"$WORKTREE_ROOT"'
    else:
        dir_expr = f'"$WORKTREE_ROOT/{rel_path}"'

    # ── Python: Poetry ──
    if _has_file(directory, "poetry.lock"):
        snippet = (
            f'# {rel_path}/ deps (poetry)\n'
            f'export POETRY_VIRTUALENVS_IN_PROJECT=true\n'
            f'(cd {dir_expr} && poetry install --with dev --quiet 2>/dev/null || poetry install --quiet)'
        ) if not is_root else (
            '# Force venv inside the worktree\n'
            'export POETRY_VIRTUALENVS_IN_PROJECT=true\n'
            'cd "$WORKTREE_ROOT"\n'
            'poetry install --with dev --quiet 2>/dev/null || poetry install --quiet'
        )
        return _Component(
            name="python-poetry",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && poetry run python -m pytest tests/ -x -q)' if not is_root else 'python -m pytest tests/ -x -q',
        )

    # ── Python: uv with lockfile ──
    if _has_file(directory, "uv.lock"):
        has_dep_groups = _pyproject_has_section(directory, "dependency-groups")
        has_optional_deps = _pyproject_has_section(directory, "project.optional-dependencies")

        if has_dep_groups:
            install_cmd = "uv sync --group dev --quiet"
        elif has_optional_deps:
            install_cmd = "uv sync --extra dev --quiet"
        else:
            install_cmd = "uv sync --quiet"

        if is_root:
            snippet = f'cd "$WORKTREE_ROOT"\n{install_cmd}'
        else:
            snippet = f'# {rel_path}/ deps (uv)\n(cd {dir_expr} && {install_cmd})'

        return _Component(
            name="python-uv-lock",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && python -m pytest tests/ -x -q)' if not is_root else 'python -m pytest tests/ -x -q',
        )

    # ── Python: Conda / Mamba (environment.yml) ──
    _has_yml = _has_file(directory, "environment.yml")
    _has_yaml = _has_file(directory, "environment.yaml")
    if _has_yml or _has_yaml:
        env_file = "environment.yml" if _has_yml else "environment.yaml"
        if is_root:
            snippet = (
                'cd "$WORKTREE_ROOT"\n'
                '# Conda/mamba environment setup\n'
                '_conda_cmd="conda"\n'
                'if command -v mamba >/dev/null 2>&1; then _conda_cmd="mamba"; fi\n'
                f'if [ -d .conda-env ]; then\n'
                f'  "$_conda_cmd" env update -f {env_file} -p .conda-env --quiet 2>/dev/null || true\n'
                f'else\n'
                f'  "$_conda_cmd" env create -f {env_file} -p .conda-env --quiet 2>/dev/null || true\n'
                f'fi\n'
                'if [ -d .conda-env ]; then\n'
                '  export PATH="$WORKTREE_ROOT/.conda-env/bin:$PATH"\n'
                '  export CONDA_PREFIX="$WORKTREE_ROOT/.conda-env"\n'
                'fi'
            )
        else:
            snippet = (
                f'# {rel_path}/ deps (conda)\n'
                f'_conda_cmd="conda"\n'
                f'if command -v mamba >/dev/null 2>&1; then _conda_cmd="mamba"; fi\n'
                f'if [ -d {dir_expr}/.conda-env ]; then\n'
                f'  (cd {dir_expr} && "$_conda_cmd" env update -f {env_file} -p .conda-env --quiet 2>/dev/null) || true\n'
                f'else\n'
                f'  (cd {dir_expr} && "$_conda_cmd" env create -f {env_file} -p .conda-env --quiet 2>/dev/null) || true\n'
                f'fi'
            )
        return _Component(
            name="python-conda",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && python -m pytest tests/ -x -q)' if not is_root else 'python -m pytest tests/ -x -q',
        )

    # ── Python: pyproject.toml or requirements.txt (no lockfile) ──
    if _has_file(directory, "pyproject.toml") or _has_file(directory, "requirements.txt"):
        has_pyproject = _has_file(directory, "pyproject.toml")
        has_requirements = _has_file(directory, "requirements.txt")

        # Determine what to install:
        #   - If pyproject.toml has dev deps → ".[dev]"
        #   - Elif requirements.txt exists → -r requirements.txt
        #   - Elif pyproject.toml exists (no dev extra) → "."
        if has_pyproject and _pyproject_has_dev_deps(directory):
            install_src = '".[dev]"'
        elif has_requirements:
            install_src = '-r requirements.txt'
        else:
            install_src = '"."'

        if is_root:
            snippet = (
                'cd "$WORKTREE_ROOT"\n'
                'python3 -m venv "$VENV_DIR"\n'
                '_installed=0\n'
                'if command -v uv >/dev/null 2>&1; then\n'
                f'  uv pip install --python "$VENV_DIR/bin/python" {install_src} --quiet 2>/dev/null && _installed=1 || true\n'
                'fi\n'
                'if [ "$_installed" -eq 0 ]; then\n'
                f'  "$VENV_DIR/bin/pip" install {install_src} --quiet 2>/dev/null && _installed=1 || true\n'
                'fi'
            )
        else:
            venv = f'"$WORKTREE_ROOT/{rel_path}/.venv"'
            snippet = (
                f'# {rel_path}/ deps (python)\n'
                f'if [ ! -d {venv} ]; then\n'
                f'  (cd {dir_expr} && python3 -m venv .venv && .venv/bin/pip install {install_src} --quiet)\n'
                f'fi'
            )

        return _Component(
            name="python",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && python -m pytest tests/ -x -q)' if not is_root else 'python -m pytest tests/ -x -q',
            install_src=install_src,
        )

    # ── Node ──
    if _has_file(directory, "package.json"):
        install_cmd = _node_install_cmd(directory)
        # Pick the best available premerge check: test > build > nothing.
        # A build check catches syntax/type/import errors even without tests.
        test_script = _package_json_has_script(directory, "test")
        build_script = _package_json_has_script(directory, "build")
        if test_script:
            test_cmd = f'(cd {dir_expr} && npm test)' if not is_root else "npm test"
        elif build_script:
            test_cmd = f'(cd {dir_expr} && npm run build)' if not is_root else "npm run build"
        else:
            test_cmd = ""

        # Determine offline install command
        if _has_file(directory, "pnpm-lock.yaml"):
            offline_cmd = "pnpm install --frozen-lockfile --offline --silent 2>/dev/null"
        elif _has_file(directory, "yarn.lock"):
            offline_cmd = "yarn install --frozen-lockfile --offline --silent 2>/dev/null"
        else:
            offline_cmd = "npm install --prefer-offline --silent 2>/dev/null"

        if is_root:
            snippet = (
                'MAIN_REPO="$(cd "$(git -C "$WORKTREE_ROOT" rev-parse --git-common-dir 2>&1)/.." 2>/dev/null && pwd)"\n'
                'cd "$WORKTREE_ROOT"\n'
                '# Layer 1: copy node_modules from main repo (fast bootstrap)\n'
                'if [ ! -d node_modules ] && [ -d "$MAIN_REPO/node_modules" ]; then\n'
                '  _cp_tree "$MAIN_REPO/node_modules" node_modules\n'
                'fi\n'
                '# Layer 2: install from cache (offline, catches deltas)\n'
                f'{offline_cmd} || true\n'
                '# Layer 3: install with network (catches anything missing)\n'
                f'{install_cmd} 2>/dev/null || true\n'
                '\n'
                'export PATH="$WORKTREE_ROOT/node_modules/.bin:$PATH"'
            )
        else:
            snippet = (
                f'# {rel_path}/ deps (node)\n'
                f'MAIN_REPO="$(cd "$(git -C "$WORKTREE_ROOT" rev-parse --git-common-dir 2>&1)/.." 2>/dev/null && pwd)"\n'
                f'# Layer 1: copy node_modules from main repo (fast bootstrap)\n'
                f'if [ ! -d "$WORKTREE_ROOT/{rel_path}/node_modules" ] && [ -d "$MAIN_REPO/{rel_path}/node_modules" ]; then\n'
                f'  _cp_tree "$MAIN_REPO/{rel_path}/node_modules" "$WORKTREE_ROOT/{rel_path}/node_modules"\n'
                f'fi\n'
                f'# Layer 2: install from cache (offline, catches deltas)\n'
                f'(cd {dir_expr} && {offline_cmd}) || true\n'
                f'# Layer 3: install with network (catches anything missing)\n'
                f'(cd {dir_expr} && {install_cmd}) 2>/dev/null || true'
            )

        return _Component(
            name="node",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=test_cmd,
        )

    # ── Rust ──
    if _has_file(directory, "Cargo.toml"):
        # Pre-seed CARGO_HOME from the system cache if the team cache is empty.
        # CARGO_HOME is redirected to the team .pkg-cache/ by settings.env —
        # we just need to populate it on first use.
        cache_seed = (
            '# Pre-seed Cargo cache from system cache (read-only, no network)\n'
            '_SYS_CARGO="${HOME}/.cargo"\n'
            'if [ -d "$_SYS_CARGO/registry" ] && [ -n "$CARGO_HOME" ] && [ ! -d "$CARGO_HOME/registry" ]; then\n'
            '  mkdir -p "$CARGO_HOME"\n'
            '  _cp_tree "$_SYS_CARGO/registry" "$CARGO_HOME/registry" 2>/dev/null || true\n'
            '  _cp_tree "$_SYS_CARGO/git" "$CARGO_HOME/git" 2>/dev/null || true\n'
            'fi'
        )
        if is_root:
            snippet = f'{cache_seed}\ncd "$WORKTREE_ROOT"\ncargo build --quiet'
        else:
            snippet = f'# {rel_path}/ deps (rust)\n{cache_seed}\n(cd {dir_expr} && cargo build --quiet)'
        return _Component(
            name="rust",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && cargo test)' if not is_root else "cargo test",
        )

    # ── Go ──
    if _has_file(directory, "go.mod"):
        # Pre-seed GOMODCACHE from the system module cache if the team cache
        # is empty.  GOMODCACHE is redirected by settings.env.
        cache_seed = (
            '# Pre-seed Go module cache from system cache (read-only, no network)\n'
            '_SYS_GOMOD="${HOME}/go/pkg/mod"\n'
            'if [ -d "$_SYS_GOMOD" ] && [ -n "$GOMODCACHE" ] && [ ! -d "$GOMODCACHE/cache" ]; then\n'
            '  mkdir -p "$GOMODCACHE"\n'
            '  _cp_tree "$_SYS_GOMOD/." "$GOMODCACHE/" 2>/dev/null || true\n'
            'fi'
        )
        if is_root:
            snippet = f'{cache_seed}\ncd "$WORKTREE_ROOT"\ngo mod tidy'
        else:
            snippet = f'# {rel_path}/ deps (go)\n{cache_seed}\n(cd {dir_expr} && go mod tidy)'
        return _Component(
            name="go",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && go test ./...)' if not is_root else "go test ./...",
        )

    # ── Ruby ──
    if _has_file(directory, "Gemfile"):
        if is_root:
            snippet = (
                'export BUNDLE_PATH="$WORKTREE_ROOT/vendor/bundle"\n'
                'MAIN_REPO="$(cd "$(git -C "$WORKTREE_ROOT" rev-parse --git-common-dir)/.." && pwd)"\n'
                'cd "$WORKTREE_ROOT"\n'
                'if [ ! -d vendor/bundle ]; then\n'
                '  # Strategy 1: copy from main repo (no network)\n'
                '  if [ -d "$MAIN_REPO/vendor/bundle" ]; then\n'
                '    _cp_tree "$MAIN_REPO/vendor/bundle" vendor/bundle\n'
                '  else\n'
                '    # Strategy 2: install (needs network)\n'
                '    bundle install --path vendor/bundle --quiet 2>/dev/null || true\n'
                '  fi\n'
                'fi'
            )
        else:
            snippet = (
                f'# {rel_path}/ deps (ruby)\n'
                f'MAIN_REPO="$(cd "$(git -C "$WORKTREE_ROOT" rev-parse --git-common-dir)/.." && pwd)"\n'
                f'if [ ! -d "$WORKTREE_ROOT/{rel_path}/vendor/bundle" ]; then\n'
                f'  if [ -d "$MAIN_REPO/{rel_path}/vendor/bundle" ]; then\n'
                f'    _cp_tree "$MAIN_REPO/{rel_path}/vendor/bundle" "$WORKTREE_ROOT/{rel_path}/vendor/bundle"\n'
                f'  else\n'
                f'    (cd {dir_expr} && BUNDLE_PATH=vendor/bundle bundle install --quiet) 2>/dev/null || true\n'
                f'  fi\n'
                f'fi'
            )
        return _Component(
            name="ruby",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && bundle exec rspec)' if not is_root else "bundle exec rspec",
        )

    # ── Fallback: infer from .envrc hints ──
    hints = _parse_envrc(directory)
    if "poetry" in hints:
        snippet = (
            f'export POETRY_VIRTUALENVS_IN_PROJECT=true\n'
            f'cd {dir_expr}\n'
            f'poetry install --quiet'
        ) if is_root else (
            f'# {rel_path}/ deps (poetry, from .envrc)\n'
            f'(cd {dir_expr} && POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --quiet)'
        )
        return _Component(
            name="python-poetry",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && poetry run python -m pytest tests/ -x -q)' if not is_root else 'python -m pytest tests/ -x -q',
        )
    if "python" in hints:
        # .envrc hinted Python — determine install source
        has_pyproject = _has_file(directory, "pyproject.toml")
        has_requirements = _has_file(directory, "requirements.txt")
        if has_pyproject and _pyproject_has_dev_deps(directory):
            envrc_install_src = '".[dev]"'
        elif has_requirements:
            envrc_install_src = '-r requirements.txt'
        elif has_pyproject:
            envrc_install_src = '"."'
        else:
            envrc_install_src = '"."'

        if is_root:
            snippet = (
                'cd "$WORKTREE_ROOT"\n'
                'python3 -m venv "$VENV_DIR"\n'
                '_installed=0\n'
                'if command -v uv >/dev/null 2>&1; then\n'
                f'  uv pip install --python "$VENV_DIR/bin/python" {envrc_install_src} --quiet 2>/dev/null && _installed=1 || true\n'
                'fi\n'
                'if [ "$_installed" -eq 0 ]; then\n'
                f'  "$VENV_DIR/bin/pip" install {envrc_install_src} --quiet 2>/dev/null && _installed=1 || true\n'
                'fi'
            )
        else:
            venv = f'"$WORKTREE_ROOT/{rel_path}/.venv"'
            snippet = (
                f'# {rel_path}/ deps (python, from .envrc)\n'
                f'if [ ! -d {venv} ]; then\n'
                f'  (cd {dir_expr} && python3 -m venv .venv && .venv/bin/pip install {envrc_install_src} --quiet)\n'
                f'fi'
            )
        return _Component(
            name="python",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && python -m pytest tests/ -x -q)' if not is_root else 'python -m pytest tests/ -x -q',
        )
    if "node" in hints:
        install_cmd = _node_install_cmd(directory)
        if is_root:
            snippet = (
                'cd "$WORKTREE_ROOT"\n'
                f'{install_cmd} 2>/dev/null || true\n'
                f'export PATH="$WORKTREE_ROOT/node_modules/.bin:$PATH"'
            )
        else:
            snippet = (
                f'# {rel_path}/ deps (node, from .envrc)\n'
                f'(cd {dir_expr} && {install_cmd}) 2>/dev/null || true'
            )
        return _Component(
            name="node",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && npm test)' if not is_root else "npm test",
        )
    if "ruby" in hints:
        if is_root:
            snippet = (
                'export BUNDLE_PATH="$WORKTREE_ROOT/vendor/bundle"\n'
                'cd "$WORKTREE_ROOT"\n'
                'bundle install --path vendor/bundle --quiet'
            )
        else:
            snippet = (
                f'# {rel_path}/ deps (ruby, from .envrc)\n'
                f'(cd {dir_expr} && BUNDLE_PATH=vendor/bundle bundle install --quiet)'
            )
        return _Component(
            name="ruby",
            rel_path=rel_path,
            setup_snippet=snippet,
            test_cmd=f'(cd {dir_expr} && bundle exec rspec)' if not is_root else "bundle exec rspec",
        )

    return None


def _root_covers_subdirs(root: Path, root_comp: _Component | None) -> set[str]:
    """Return the set of stack names whose subdirs are already covered by a
    root-level workspace config.

    For example, if root has a Cargo workspace, subdirs with ``Cargo.toml``
    are workspace members and don't need separate ``cargo build`` calls.
    """
    covered: set[str] = set()
    if root_comp is None:
        return covered

    # ── Cargo workspace ──
    if root_comp.name == "rust":
        cargo = root / "Cargo.toml"
        try:
            if "[workspace]" in cargo.read_text():
                covered.add("rust")
        except Exception:
            pass

    # ── npm/pnpm/yarn workspaces ──
    if root_comp.name == "node":
        pj = root / "package.json"
        try:
            data = json.loads(pj.read_text())
            if "workspaces" in data:
                covered.add("node")
        except Exception:
            pass

    # ── Go workspace ──
    if root_comp.name == "go":
        if _has_file(root, "go.work"):
            covered.add("go")

    # ── Python (uv workspace) ──
    if root_comp.is_python:
        pyproject = root / "pyproject.toml"
        try:
            if "[tool.uv.workspace]" in pyproject.read_text():
                covered.add("python")
                covered.add("python-uv-lock")
                covered.add("python-poetry")
        except Exception:
            pass

    return covered


def _detect_all(root: Path) -> list[_Component]:
    """Detect all stacks: root-level first, then each top-level subdir.

    Workspace-aware: if the root has a Cargo workspace, npm workspaces,
    go.work, or uv workspace, subdirs of the same stack type are skipped
    (the root install already covers them).

    Returns a list of components ordered: root (if any), then subdirs
    alphabetically.  Each subdir is scanned one level deep.
    """
    components: list[_Component] = []

    # 1. Root
    root_comp = _detect_at(root, ".")
    if root_comp is not None:
        components.append(root_comp)

    # 2. Determine which stack types the root workspace already covers
    covered = _root_covers_subdirs(root, root_comp)

    # 3. Top-level subdirs
    try:
        children = sorted(root.iterdir())
    except OSError:
        children = []

    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        comp = _detect_at(child, child.name)
        if comp is None:
            continue
        # Skip subdirs whose stack is already covered by the root workspace
        if comp.name in covered:
            logger.debug(
                "Skipping %s/%s — covered by root %s workspace",
                child.name, comp.name, root_comp.name if root_comp else "?",
            )
            continue
        components.append(comp)

    # If nothing found at all, return a fallback
    if not components:
        components.append(_Component(
            name="unknown",
            rel_path=".",
            setup_snippet='# TODO: Add setup commands for this project',
            test_cmd='echo "No test command configured — edit .delegate/premerge.sh"',
        ))

    return components


# Keep _detect_stack as a convenience for tests — returns the root component
def _detect_stack(root: Path) -> _Component | None:
    """Detect the primary (root-level) stack.  Convenience wrapper."""
    return _detect_at(root, ".")


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------

_HEADER = """\
#!/usr/bin/env bash
set -e
# Created by delegate. Edit as needed.
"""

_CP_TREE_FN = """\
# Copy directory tree using copy-on-write when available:
#   macOS (APFS) → cp -Rc (clonefile),  Linux (btrfs/XFS) → cp --reflink=auto
# Falls back to regular cp -r on other filesystems.
_cp_tree() { cp -Rc "$@" 2>/dev/null || cp -r --reflink=auto "$@" 2>/dev/null || cp -r "$@"; }
"""

_SELF_REF = """\
# Resolve the path of THIS script, portable across bash, zsh, and others.
# Needed because $0 behaves differently when the script is sourced vs executed.
if [ -n "${BASH_VERSION:-}" ]; then
  _SELF="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
  eval '_SELF="${(%):-%x}"'   # zsh-only syntax, hidden from bash parser via eval
else
  _SELF="$0"                  # best-effort fallback
fi"""


def _has_root_python(components: list[_Component]) -> bool:
    return any(c.is_root and c.is_python for c in components)


def _generate_setup(components: list[_Component], *, is_nix: bool, nix_file: str) -> str:
    """Compose setup.sh from all detected components."""
    root_python = next((c for c in components if c.is_root and c.is_python), None)

    # ── Nix wrapper ──
    if is_nix:
        return _generate_nix_setup(components, nix_file)

    # ── Python root (needs venv management) ──
    if root_python:
        return _generate_python_root_setup(components, root_python)

    # ── Generic (no root Python, no Nix) ──
    return _generate_generic_setup(components)


def _generate_nix_setup(components: list[_Component], nix_file: str) -> str:
    """Generate setup.sh wrapped in a Nix shell."""
    if nix_file == "flake.nix":
        nix_run = 'nix develop "$REPO_ROOT" --command bash -c'
    else:
        nix_run = f'nix-shell "$REPO_ROOT/{nix_file}" --run'

    # Collect the last meaningful install line from each root component
    root_cmds = []
    for c in components:
        if c.is_root:
            last_line = c.setup_snippet.splitlines()[-1].strip()
            if last_line and not last_line.startswith("#"):
                root_cmds.append(last_line)

    install_chain = " && ".join(root_cmds) if root_cmds else "true"

    lines = [_HEADER.rstrip(), _CP_TREE_FN.rstrip(), ""]
    lines.append(_SELF_REF)
    lines.append('WORKTREE_ROOT="$(cd "$(dirname "$_SELF")/.." && pwd)"')
    lines.append('GIT_COMMON="$(git -C "$WORKTREE_ROOT" rev-parse --git-common-dir 2>&1)" || true')
    lines.append('REPO_ROOT="$(cd "$GIT_COMMON/.." 2>/dev/null && pwd)"')
    lines.append("")
    lines.append(f'{nix_run} \\')
    lines.append(f'  "bash -c \'cd $WORKTREE_ROOT && {install_chain}\'"')

    # Non-root subdir installs (outside Nix — they may not need it)
    subdir_comps = [c for c in components if not c.is_root]
    if subdir_comps:
        lines.append("")
        for c in subdir_comps:
            lines.append(c.setup_snippet)

    return "\n".join(lines) + "\n"


def _generate_python_root_setup(components: list[_Component], root_python: _Component) -> str:
    """Generate setup.sh with Python venv at root + other components appended.

    All three install layers run unconditionally — each is idempotent and
    a no-op when everything it would install is already present:

      1. **Copy site-packages** from the main repo's venv — instant bulk
         bootstrap (only when Python major.minor matches).
      2. **Install from system cache** (offline) — ``uv pip install --offline``
         or ``pip install --no-index`` against ``~/.cache/uv`` / ``~/.cache/pip``.
         Catches any packages the copy missed.
      3. **Full install** via ``uv pip install`` / ``pip install`` with
         network — catches anything missing from cache (CI, new deps).

    Because layers are additive, changes to requirements.txt are
    picked up on the next source without manual intervention.
    """
    lines = [_HEADER.rstrip(), _CP_TREE_FN.rstrip(), ""]

    lines.append(_SELF_REF)
    lines.append('WORKTREE_ROOT="$(cd "$(dirname "$_SELF")/.." && pwd)"')
    lines.append('VENV_DIR="$WORKTREE_ROOT/.venv"')
    # Guard git command with || true to prevent set -e from killing the script
    lines.append('_GIT_COMMON="$(git -C "$WORKTREE_ROOT" rev-parse --git-common-dir 2>&1)" || true')
    lines.append('MAIN_VENV="$(cd "$_GIT_COMMON/.." 2>/dev/null && pwd)/.venv"')
    lines.append("")

    # ── Ensure venv exists and is healthy ──
    lines.append("# Ensure venv exists and is healthy")
    lines.append('if [ ! -d "$VENV_DIR" ] || ! "$VENV_DIR/bin/python" --version >/dev/null 2>&1; then')
    lines.append('  rm -rf "$VENV_DIR"')
    lines.append('  python3 -m venv "$VENV_DIR"')
    lines.append("fi")
    lines.append('cd "$WORKTREE_ROOT"')
    lines.append("")

    # ── Layer 1: copy site-packages from main repo (instant bootstrap) ──
    lines.append("# ── Layer 1: bootstrap from main repo venv (fast, offline) ──")
    lines.append("# Only copy when Python major.minor versions match (ABI compatibility)")
    lines.append('if [ -d "$MAIN_VENV" ]; then')
    lines.append('  MAIN_SITE="$(ls -d "$MAIN_VENV"/lib/python*/site-packages 2>/dev/null | head -1)"')
    lines.append('  WORKTREE_SITE="$(ls -d "$VENV_DIR"/lib/python*/site-packages 2>/dev/null | head -1)"')
    lines.append('  MAIN_PYVER="$(basename "$(dirname "$MAIN_SITE")" 2>/dev/null)"')
    lines.append('  WORKTREE_PYVER="$(basename "$(dirname "$WORKTREE_SITE")" 2>/dev/null)"')
    lines.append('  if [ -n "$MAIN_SITE" ] && [ -d "$MAIN_SITE" ] && [ -n "$WORKTREE_SITE" ] && [ "$MAIN_PYVER" = "$WORKTREE_PYVER" ]; then')
    lines.append('    _cp_tree "$MAIN_SITE/." "$WORKTREE_SITE/"')
    lines.append("  fi")
    lines.append("fi")
    lines.append("")

    # ── Layers 2 & 3: package-manager install (idempotent, catches deltas) ──
    if root_python.name == "python-uv-lock":
        sync_line = [l.strip() for l in root_python.setup_snippet.splitlines()
                     if l.strip().startswith("uv sync")]
        sync_cmd = sync_line[0] if sync_line else "uv sync --quiet"
        lines.append("# ── Layer 2: uv sync from cache (offline) ──")
        lines.append(f'{sync_cmd.replace("--quiet", "--offline --quiet")} 2>/dev/null || true')
        lines.append("")
        lines.append("# ── Layer 3: uv sync with network (catches new deps) ──")
        lines.append(f'{sync_cmd} 2>/dev/null || true')

    elif root_python.name == "python-poetry":
        lines.append("# ── Layer 2+3: poetry install (manages cache & network internally) ──")
        lines.append("export POETRY_VIRTUALENVS_IN_PROJECT=true")
        lines.append("poetry install --no-interaction --quiet 2>/dev/null || true")

    else:
        # Generic python — uv pip install / pip install
        install_src = root_python.install_src or '"."'
        lines.append("# ── Layer 2: install from system cache (offline, no network) ──")
        lines.append('_SYS_UV_CACHE="${HOME}/.cache/uv"')
        lines.append('_SYS_PIP_CACHE="${HOME}/.cache/pip"')
        lines.append("if command -v uv >/dev/null 2>&1; then")
        lines.append(f'  UV_CACHE_DIR="${{_SYS_UV_CACHE}}" uv pip install --python "$VENV_DIR/bin/python" {install_src} --offline --quiet 2>/dev/null || true')
        lines.append("else")
        lines.append(f'  PIP_CACHE_DIR="${{_SYS_PIP_CACHE}}" "$VENV_DIR/bin/pip" install {install_src} --no-index --quiet 2>/dev/null || true')
        lines.append("fi")
        lines.append("")
        lines.append("# ── Layer 3: install with network (catches any remaining gaps) ──")
        lines.append("if command -v uv >/dev/null 2>&1; then")
        lines.append(f'  uv pip install --python "$VENV_DIR/bin/python" {install_src} --quiet 2>/dev/null || true')
        lines.append("else")
        lines.append(f'  "$VENV_DIR/bin/pip" install {install_src} --quiet 2>/dev/null || true')
        lines.append("fi")

    lines.append("")
    lines.append('source "$VENV_DIR/bin/activate"')
    lines.append('export PYTHONPATH="$WORKTREE_ROOT${PYTHONPATH:+:$PYTHONPATH}"')

    # Other components (subdirs or root non-Python — unlikely but possible)
    others = [c for c in components if c is not root_python]
    if others:
        lines.append("")
        for c in others:
            lines.append(c.setup_snippet)

    return "\n".join(lines) + "\n"


def _generate_generic_setup(components: list[_Component]) -> str:
    """Generate setup.sh for repos without root Python or Nix.

    Each component's snippet is idempotent — safe to re-source.
    """
    lines = [_HEADER.rstrip(), _CP_TREE_FN.rstrip(), ""]
    lines.append(_SELF_REF)
    lines.append('WORKTREE_ROOT="$(cd "$(dirname "$_SELF")/.." && pwd)"')

    for c in components:
        lines.append("")
        lines.append(c.setup_snippet)

    return "\n".join(lines) + "\n"


def _generate_premerge(
    components: list[_Component],
    *,
    is_nix: bool,
    nix_file: str,
) -> str:
    """Compose premerge.sh from all detected components."""
    if is_nix:
        return _generate_nix_premerge(components, nix_file)

    # Collect test commands (skip empty)
    test_lines = [c.test_cmd for c in components if c.test_cmd]
    if not test_lines:
        test_lines = ['echo "No test command configured — edit .delegate/premerge.sh"']

    lines = [_HEADER.rstrip(), ""]
    lines.append(_SELF_REF)
    lines.append('SCRIPT_DIR="$(cd "$(dirname "$_SELF")" && pwd)"')
    lines.append('WORKTREE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"')
    lines.append('source "$SCRIPT_DIR/setup.sh"')
    lines.append('cd "$WORKTREE_ROOT"')
    lines.append("")

    for i, cmd in enumerate(test_lines):
        if i > 0:
            lines.append("")
        lines.append(cmd)

    return "\n".join(lines) + "\n"


def _generate_nix_premerge(components: list[_Component], nix_file: str) -> str:
    """Generate premerge.sh wrapped in a Nix shell (self-contained)."""
    if nix_file == "flake.nix":
        nix_run = 'nix develop "$REPO_ROOT" --command bash -c'
    else:
        nix_run = f'nix-shell "$REPO_ROOT/{nix_file}" --run'

    # Root components: install + test inside nix-shell
    root_comps = [c for c in components if c.is_root]
    install_cmds = []
    for c in root_comps:
        last_line = c.setup_snippet.splitlines()[-1].strip()
        if last_line and not last_line.startswith("#"):
            install_cmds.append(last_line)
    root_test_cmds = [c.test_cmd for c in root_comps if c.test_cmd]

    chain_parts = install_cmds + root_test_cmds
    chain = " && ".join(chain_parts) if chain_parts else "true"

    lines = [_HEADER.rstrip(), ""]
    lines.append(_SELF_REF)
    lines.append('SCRIPT_DIR="$(cd "$(dirname "$_SELF")" && pwd)"')
    lines.append('WORKTREE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"')
    lines.append('GIT_COMMON="$(git -C "$WORKTREE_ROOT" rev-parse --git-common-dir 2>&1)" || true')
    lines.append('REPO_ROOT="$(cd "$GIT_COMMON/.." 2>/dev/null && pwd)"')
    lines.append("")
    lines.append(f'{nix_run} \\')
    lines.append(f'  "bash -c \'cd $WORKTREE_ROOT && {chain}\'"')

    # Non-root test commands (outside nix)
    subdir_tests = [c.test_cmd for c in components if not c.is_root and c.test_cmd]
    if subdir_tests:
        lines.append("")
        for cmd in subdir_tests:
            lines.append(cmd)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_env_scripts(repo_path: Path) -> tuple[str, str]:
    """Detect all tooling in *repo_path* and return (setup_sh, premerge_sh).

    Scans the root directory and all top-level subdirectories for language
    stacks (Python, Node, Rust, Go, Ruby).  Composes a unified setup.sh
    that installs deps for every detected component, and a premerge.sh
    that runs all detected test suites.

    Both scripts are returned as strings, ready to write to disk.
    """
    components = _detect_all(repo_path)

    # Nix is a repo-level concern (shell.nix / flake.nix at root, or .envrc `use nix`)
    envrc_hints = _parse_envrc(repo_path)
    has_flake = _has_file(repo_path, "flake.nix") or "flake" in envrc_hints
    has_nix = _has_file(repo_path, "shell.nix") or has_flake or "nix" in envrc_hints
    is_nix = has_nix
    nix_file = "flake.nix" if has_flake else "shell.nix"

    setup = _generate_setup(components, is_nix=is_nix, nix_file=nix_file)
    premerge = _generate_premerge(components, is_nix=is_nix, nix_file=nix_file)

    return setup.strip() + "\n", premerge.strip() + "\n"


def write_env_scripts(worktree_path: Path, *, commit: bool = True) -> bool:
    """Write .delegate/setup.sh and premerge.sh into a worktree if missing.

    If ``.delegate/setup.sh`` already exists, does nothing (returns False).
    If *commit* is True, stages and commits the new scripts.

    Returns True if scripts were written, False if they already existed.
    """
    delegate_dir = worktree_path / ".delegate"
    setup_path = delegate_dir / "setup.sh"
    premerge_path = delegate_dir / "premerge.sh"

    if setup_path.is_file():
        logger.debug("setup.sh already exists at %s — skipping generation", setup_path)
        return False

    setup_content, premerge_content = generate_env_scripts(worktree_path)

    delegate_dir.mkdir(parents=True, exist_ok=True)
    setup_path.write_text(setup_content)
    setup_path.chmod(0o755)
    premerge_path.write_text(premerge_content)
    premerge_path.chmod(0o755)

    logger.info("Generated env scripts at %s", delegate_dir)

    if commit:
        try:
            subprocess.run(
                ["git", "add", ".delegate/"],
                cwd=str(worktree_path),
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "chore: add delegate env scripts"],
                cwd=str(worktree_path),
                capture_output=True,
                check=True,
            )
            logger.info("Committed env scripts in %s", worktree_path)
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to commit env scripts: %s", exc.stderr.decode() if exc.stderr else exc)

    return True


# ---------------------------------------------------------------------------
# CLI entry point — ``python -m delegate.env [path]``
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI for (re)generating env scripts.

    Usage::

        python -m delegate.env                 # current directory
        python -m delegate.env /path/to/repo   # explicit path
        python -m delegate.env --force /path    # overwrite existing
        python -m delegate.env --no-commit      # don't git-commit
        python -m delegate.env --print          # print to stdout only
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Generate .delegate/setup.sh and premerge.sh for a repository.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to the repo/worktree (default: current directory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scripts (by default, existing scripts are preserved)",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Don't git-commit the generated scripts",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print scripts to stdout instead of writing files",
    )
    args = parser.parse_args()

    repo = Path(args.path).resolve()
    if not repo.is_dir():
        print(f"Error: {repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.print_only:
        setup, premerge = generate_env_scripts(repo)
        print("=== .delegate/setup.sh ===")
        print(setup)
        print("=== .delegate/premerge.sh ===")
        print(premerge)
        return

    if args.force:
        # Remove existing scripts so write_env_scripts doesn't skip
        setup_path = repo / ".delegate" / "setup.sh"
        premerge_path = repo / ".delegate" / "premerge.sh"
        if setup_path.is_file():
            setup_path.unlink()
        if premerge_path.is_file():
            premerge_path.unlink()

    wrote = write_env_scripts(repo, commit=not args.no_commit)
    if wrote:
        print(f"Generated .delegate/setup.sh and premerge.sh in {repo}")
    else:
        print(f"Scripts already exist in {repo}/.delegate/ (use --force to overwrite)")


if __name__ == "__main__":
    main()
