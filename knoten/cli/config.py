"""`knoten config` sub-app — inspect and edit the user's configuration.

Three subcommands:

- `knoten config show` — dump the effective configuration (API URL, mode,
  token redacted, resolved paths).
- `knoten config path` — print just the resolved config/data/cache paths.
  Grep-friendly output suitable for shell scripts.
- `knoten config edit` — open the `.env` file in `$VISUAL` / `$EDITOR` or
  the OS default editor. On Windows this means users never have to
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
from typing import Any

import typer

from knoten.cli.output import OutputMode, emit_json, render_status
from knoten.settings import Settings, load_settings

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
#   remote — mirror a compatible remote backend (requires URL + token below).
# KNOTEN_MODE=auto

# Remote backend base URL. Leave empty for local mode.
# Example: KNOTEN_API_URL=https://your-backend.example.com
KNOTEN_API_URL=

# API token (Bearer). Only required in remote mode. Must have the `api` scope.
# Shown only once at creation time — store it here or in a password manager.
# KNOTEN_API_TOKEN=nt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# HTTP request timeout in seconds.
# KNOTEN_HTTP_TIMEOUT=30

# Filesystem escape hatches — point config / data / cache somewhere other
# than the platformdirs default. Useful for tests, Docker, custom deploys.
# KNOTEN_CONFIG_DIR=/etc/knoten
# KNOTEN_DATA_DIR=/srv/knoten/data
# KNOTEN_CACHE_DIR=/var/cache/knoten
"""


def _full_config_payload(settings: Settings) -> dict[str, Any]:
    """Build the dict emitted by `config show` (all values + paths)."""
    p = settings.paths
    return {
        "mode": settings.effective_mode,
        "runtime": "dev" if p.is_dev else "installed",
        "api_url": settings.api_url or "(unset)",
        "api_token": settings.token_redacted or "(unset)",
        "http_timeout": settings.http_timeout,
        "config_dir": str(p.config_dir),
        "data_dir": str(p.data_dir),
        "cache_dir": str(p.cache_dir),
        "env_file": str(p.env_file),
        "vault_dir": str(p.vault_dir),
        "index_path": str(p.index_path),
    }


def _paths_payload(settings: Settings) -> dict[str, str]:
    """Build the dict emitted by `config path` (paths only)."""
    p = settings.paths
    return {
        "mode": settings.effective_mode,
        "runtime": "dev" if p.is_dev else "installed",
        "config_dir": str(p.config_dir),
        "data_dir": str(p.data_dir),
        "cache_dir": str(p.cache_dir),
        "env_file": str(p.env_file),
        "vault_dir": str(p.vault_dir),
        "index_path": str(p.index_path),
    }


@config_app.command("show")
def config_show(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the effective configuration (token redacted)."""
    mode = OutputMode.detect(json_output)
    try:
        settings = load_settings()
        render_status(_full_config_payload(settings), mode=mode)
    except Exception as exc:
        _emit_error(exc, mode=mode)


@config_app.command("path")
def config_path(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print the resolved config / data / cache paths."""
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
    try:
        settings = load_settings()
    except Exception as exc:
        sys.stderr.write(f"error: {exc}\n")
        raise typer.Exit(4) from exc
    env_file = settings.paths.env_file
    created = _ensure_env_file(settings)
    editor = _resolve_editor()
    if created:
        typer.echo(f"Created {env_file} from the default template.")
    typer.echo(f"Opening {env_file} in {editor!r}")
    subprocess.run([editor, str(env_file)], check=False)


def init_command() -> None:
    """Implementation of the top-level `knoten init` command."""
    try:
        settings = load_settings()
    except Exception as exc:
        sys.stderr.write(f"error: {exc}\n")
        raise typer.Exit(4) from exc
    created = _ensure_env_file(settings)
    p = settings.paths
    typer.echo(f"mode: {settings.effective_mode}")
    typer.echo(f"runtime: {'dev' if p.is_dev else 'installed'}")
    typer.echo(f"config_dir: {p.config_dir}")
    typer.echo(f"data_dir: {p.data_dir}")
    typer.echo(f"cache_dir: {p.cache_dir}")
    typer.echo(f"vault_dir: {p.vault_dir}")
    suffix = "(created)" if created else "(already present)"
    typer.echo(f"env_file: {p.env_file} {suffix}")
    if created:
        typer.echo("")
        typer.echo(
            "Local mode works out of the box — just run `knoten list` on the empty "
            "vault. For remote mode (sync with a compatible backend), set "
            "KNOTEN_API_URL + KNOTEN_API_TOKEN in the .env."
        )
        typer.echo("Run `knoten config edit` to open it in your editor.")


def _ensure_env_file(settings: Settings) -> bool:
    """Create the .env file from the default template if it does not exist."""
    env_file = settings.paths.env_file
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
