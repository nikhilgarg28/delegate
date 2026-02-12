"""CLI output formatting helpers using click.style."""

import json
import os
from pathlib import Path

import click


def success(msg: str) -> None:
    """Green checkmark prefix."""
    click.echo(click.style(" [*] ", fg="green") + msg)


def warn(msg: str) -> None:
    """Yellow warning prefix."""
    click.echo(click.style(" [!] ", fg="yellow") + msg)


def error(msg: str) -> None:
    """Red error prefix, writes to stderr."""
    click.echo(click.style(" [x] ", fg="red") + msg, err=True)


def info(msg: str) -> None:
    """Blue info prefix."""
    click.echo(click.style(" [-] ", fg="blue") + msg)


def header(msg: str) -> None:
    """Bold header text."""
    click.echo(click.style(msg, bold=True))


def dim(msg: str) -> None:
    """Dimmed text."""
    click.echo(click.style(msg, dim=True))


def get_auth_display() -> str:
    """Return human-readable auth method for CLI display.

    Returns a string like:
    - "API key (sk-a...xK4f)" if ANTHROPIC_API_KEY is set
    - "Claude login (email@example.com)" if ~/.claude.json contains an email
    - "Claude login" if ~/.claude/credentials.json exists
    - "not configured" if no auth method found
    """
    # Check for API key first
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "****"
        return f"API key ({masked})"

    # Check Claude CLI credential files
    home = Path.home()
    claude_json = home / ".claude.json"
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text())
            if data:
                acct = data.get("oauthAccount", {})
                email = acct.get("emailAddress", "")
                if email:
                    return f"Claude login ({email})"
                return "Claude login"
        except (json.JSONDecodeError, OSError):
            pass

    creds = home / ".claude" / "credentials.json"
    if creds.exists():
        try:
            data = json.loads(creds.read_text())
            if data:
                return "Claude login"
        except (json.JSONDecodeError, OSError):
            pass

    return "not configured"


def get_version() -> str:
    """Get the delegate version from package metadata."""
    try:
        from importlib.metadata import version
        return version("delegate-ai")
    except Exception:
        return "dev"
