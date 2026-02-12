"""Runtime dependency verification (e.g., git, python, API keys).

Used by ``delegate doctor`` CLI command.
"""

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str = ""


def check_git() -> CheckResult:
    """Check if git is installed and accessible."""
    if shutil.which("git"):
        return CheckResult("Git", True, "Git is installed.")
    return CheckResult("Git", False, "Git is not installed or not in PATH.")


def check_python_version() -> CheckResult:
    """Check if Python version is 3.12 or higher."""
    if sys.version_info >= (3, 12):
        return CheckResult("Python Version", True, f"Python {sys.version.split()[0]} is installed.")
    return CheckResult(
        "Python Version",
        False,
        f"Python 3.12 or higher is required. Found {sys.version.split()[0]}.",
    )


def check_uv() -> CheckResult:
    """Check if uv is available (optional but recommended)."""
    if shutil.which("uv"):
        return CheckResult("uv", True, "uv is installed (fast package manager).")
    return CheckResult("uv", True, "uv not found — pip will be used as fallback.")


def check_claude_cli() -> CheckResult:
    """Check if the claude CLI is installed."""
    if shutil.which("claude"):
        return CheckResult("Claude CLI", True, "claude CLI is installed.")
    return CheckResult(
        "Claude CLI",
        False,
        "claude CLI not found in PATH. Install from https://docs.anthropic.com/en/docs/claude-code",
    )


def check_api_key() -> CheckResult:
    """Check if Anthropic API key is available.

    Looks for:
    1. ANTHROPIC_API_KEY environment variable
    2. Claude CLI credentials (~/.claude.json or ~/.claude/credentials.json)
    """
    # 1. Environment variable
    if os.environ.get("ANTHROPIC_API_KEY"):
        return CheckResult("API Key", True, "ANTHROPIC_API_KEY is set in environment.")

    # 2. Claude CLI credential files
    home = Path.home()
    credential_paths = [
        home / ".claude.json",
        home / ".claude" / "credentials.json",
    ]
    for cred_path in credential_paths:
        if cred_path.exists():
            try:
                data = json.loads(cred_path.read_text())
                # .claude.json stores oauthAccount or similar
                # credentials.json might have apiKey or token
                if data:
                    return CheckResult(
                        "API Key",
                        True,
                        f"Claude credentials found at {cred_path}.",
                    )
            except (json.JSONDecodeError, OSError):
                continue

    return CheckResult(
        "API Key",
        False,
        "No Anthropic API key found. Set ANTHROPIC_API_KEY or authenticate with `claude login`.",
    )


def run_all_checks() -> list[CheckResult]:
    """Run all dependency checks."""
    return [
        check_git(),
        check_python_version(),
        check_uv(),
        check_claude_cli(),
        check_api_key(),
    ]


# Aliases used by the CLI
run_doctor = run_all_checks


def print_doctor_report(checks: list[CheckResult]) -> bool:
    """Print a formatted report of check results.

    Returns True if all checks passed.
    """
    import click

    click.echo("Running Delegate doctor checks...")
    all_passed = True
    for result in checks:
        if result.passed:
            status = click.style("[PASS]", fg="green")
        else:
            status = click.style("[FAIL]", fg="red")
        click.echo(f"  {status} {result.name}: {result.message}")
        if not result.passed:
            all_passed = False

    click.echo()  # blank line
    if all_passed:
        click.echo(click.style("All essential checks passed. Delegate is ready!", fg="green"))
    else:
        click.echo(click.style("Some checks failed. Please address the issues above.", fg="red"))
    return all_passed


def main():
    checks = run_all_checks()
    ok = print_doctor_report(checks)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
