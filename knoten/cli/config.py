"""`knoten config` sub-app — inspect and edit the user's configuration.

Three subcommands:

- `knoten config show` — dump the effective configuration (same payload
  the old flat `knoten config` used to produce; the token is redacted).
- `knoten config path` — print just the resolved paths. Grep-friendly
  output suitable for shell scripts.
- `knoten config edit` — open the `.env` file in `$VISUAL` / `$EDITOR`
  or the OS default editor. On Windows this means users never have to
  navigate `%APPDATA%` manually.

The `init_command` helper is invoked by the top-level `knoten init`
command in `main.py`; it ensures directories exist and seeds a default
`.env` if none is present.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

from knoten.cli.output import OutputMode, emit_json, render_status
from knoten.settings import Settings, ensure_dirs, load_settings, primary_env_file

config_app = typer.Typer(
    help="Inspect and edit the knoten configuration.",
    no_args_is_help=True,
)


ENV_EXAMPLE_TEMPLATE = """\
# knoten configuration.
# Optional — the CLI works without any config in local-only mode.

# Mode selection.
#   auto   — local if KNOTEN_API_URL is empty, else remote (default)
#   local  — operate on an on-disk vault only. No network, no token required.
#   remote — mirror a notes.vcoeur.com instance (requires URL + token below).
# KNOTEN_MODE=auto

# notes.vcoeur.com API base URL. Leave empty for local mode.
# Example: KNOTEN_API_URL=https://notes.vcoeur.com
KNOTEN_API_URL=

# API token (Bearer). Only required in remote mode. Must have the `api` scope.
# Shown only once at creation time — store it here or in a password manager.
# KNOTEN_API_TOKEN=nt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional: override the root path for vault + state.
# Defaults to the repo root in dev, ~/.knoten when installed.
# KNOTEN_HOME=~/src/vcoeur/knoten
"""


def _full_config_payload(settings: Settings) -> dict[str, Any]:
    """Build the dict emitted by `config show` (all values + paths)."""
    return {
        "mode": settings.effective_mode,
        "api_url": settings.api_url,
        "api_token": settings.token_redacted,
        "http_timeout": settings.http_timeout,
        "home": str(settings.home),
        "vault_dir": str(settings.vault_dir),
        "state_dir": str(settings.state_dir),
        "env_file": str(primary_env_file()),
    }


def _paths_payload(settings: Settings) -> dict[str, str]:
    """Build the dict emitted by `config path` (paths only)."""
    return {
        "mode": settings.effective_mode,
        "home": str(settings.home),
        "vault_dir": str(settings.vault_dir),
        "state_dir": str(settings.state_dir),
        "index_path": str(settings.index_path),
        "env_file": str(primary_env_file()),
    }


@config_app.command("show")
def config_show(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the effective configuration (token redacted)."""
    mode = OutputMode.detect(json_output)
    try:
        settings = load_settings()
        ensure_dirs(settings)
        render_status(_full_config_payload(settings), mode=mode)
    except Exception as exc:
        _emit_error(exc, mode=mode)


@config_app.command("path")
def config_path(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print the resolved config, vault, and state paths."""
    mode = OutputMode.detect(json_output)
    try:
        settings = load_settings()
        payload = _paths_payload(settings)
        if mode.json:
            emit_json(payload)
        else:
            for key, value in payload.items():
                typer.echo(f"{key}: {value}")
    except Exception as exc:
        _emit_error(exc, mode=mode)


@config_app.command("edit")
def config_edit() -> None:
    """Open the knoten .env file in $VISUAL / $EDITOR or the OS default editor."""
    env_file = primary_env_file()
    created = _ensure_env_file(env_file)
    editor = _resolve_editor()
    if created:
        typer.echo(f"Created {env_file} from the default template.")
    typer.echo(f"Opening {env_file} in {editor!r}")
    subprocess.run([editor, str(env_file)], check=False)


def init_command() -> None:
    """Implementation of the top-level `knoten init` command."""
    try:
        settings = load_settings()
        ensure_dirs(settings)
    except Exception as exc:
        sys.stderr.write(f"error: {exc}\n")
        raise typer.Exit(4) from exc
    env_file = primary_env_file()
    created = _ensure_env_file(env_file)
    typer.echo(f"mode: {settings.effective_mode}")
    typer.echo(f"home: {settings.home}")
    typer.echo(f"vault_dir: {settings.vault_dir}")
    typer.echo(f"state_dir: {settings.state_dir}")
    suffix = "(created)" if created else "(already present)"
    typer.echo(f"env_file: {env_file} {suffix}")
    if created:
        typer.echo("")
        typer.echo(
            "Local mode works out of the box — just run `knoten list` on the empty "
            "vault. For remote mode (mirror a notes.vcoeur.com instance), set "
            "KNOTEN_API_URL + KNOTEN_API_TOKEN in the .env."
        )
        typer.echo("Run `knoten config edit` to open it in your editor.")


def _ensure_env_file(env_file: Path) -> bool:
    """Create the .env file from the default template if it does not exist."""
    if env_file.exists():
        return False
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(ENV_EXAMPLE_TEMPLATE)
    return True


def _resolve_editor() -> str:
    """Return the editor command to open text files with."""
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var)
        if value:
            return value
    system = platform.system()
    if system == "Windows":
        return "notepad"
    if system == "Darwin":
        return "open"
    return "xdg-open"


def _emit_error(exc: Exception, *, mode: OutputMode) -> None:
    """Emit a structured error and exit with code 4 (config error)."""
    if mode.json:
        emit_json({"error": "config", "message": str(exc), "code": 4})
    else:
        sys.stderr.write(f"error: {exc}\n")
    raise typer.Exit(4) from exc
