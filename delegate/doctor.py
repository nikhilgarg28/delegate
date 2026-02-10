"""Runtime dependency verification (e.g., git, python).

Used by ``delegate doctor`` CLI command.
"""

import shutil
import sys
from dataclasses import dataclass


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


def run_all_checks() -> list[CheckResult]:
    """Run all dependency checks."""
    return [
        check_git(),
        check_python_version(),
        check_uv(),
    ]


# Aliases used by the CLI
run_doctor = run_all_checks


def print_doctor_report(checks: list[CheckResult]) -> bool:
    """Print a formatted report of check results.

    Returns True if all checks passed.
    """
    print("Running Delegate doctor checks...")
    all_passed = True
    for result in checks:
        status = "PASSED" if result.passed else "FAILED"
        print(f"  [{status}] {result.name}: {result.message}")
        if not result.passed:
            all_passed = False
    if all_passed:
        print("\nAll essential checks passed. Delegate is ready!")
    else:
        print("\nSome checks failed. Please address the issues above.")
    return all_passed


def main():
    checks = run_all_checks()
    ok = print_doctor_report(checks)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
