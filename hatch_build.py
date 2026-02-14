"""Hatch build hook — builds frontend assets before creating the wheel.

When ``hatch build`` or ``pip install .`` runs, this hook ensures that
``delegate/static/`` contains the latest bundled JS/CSS/HTML from ``frontend/``.

Requires Node.js >= 18 on the build machine.  End users installing from
a pre-built wheel do NOT need Node.js — the assets are already bundled.
"""

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        import os

        root = Path(self.root)
        frontend_dir = root / "frontend"
        build_js = frontend_dir / "build.js"

        if os.environ.get("SKIP_FRONTEND_BUILD", ""):
            self.app.display_info("SKIP_FRONTEND_BUILD set — skipping frontend build")
            return

        if not build_js.is_file():
            # No frontend source (e.g. sdist without frontend/) — skip
            self.app.display_info("No frontend/build.js found — skipping frontend build")
            return

        node = shutil.which("node")
        if node is None:
            raise RuntimeError(
                "Node.js is required to build the frontend assets. "
                "Install Node.js >= 18 and try again."
            )

        # npm install if needed
        if not (frontend_dir / "node_modules").is_dir():
            npm = shutil.which("npm")
            if npm is None:
                raise RuntimeError(
                    "'npm' not found on PATH. Install Node.js >= 18 and try again."
                )
            self.app.display_info("Installing frontend dependencies …")
            subprocess.run(
                [npm, "install"],
                cwd=str(frontend_dir),
                check=True,
            )

        # Production build (minified, no sourcemaps)
        self.app.display_info("Building frontend assets …")
        subprocess.run(
            [node, str(build_js)],
            cwd=str(frontend_dir),
            check=True,
        )
        self.app.display_info("Frontend build complete → delegate/static/")
