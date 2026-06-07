"""Machine-readable contract dump for `knoten schema`.

One read-only command emits the whole CLI contract so an LLM (or any
client) can self-orient without reading prose docs: every command + its
flags, the family→prefix→kind→directory table, the permission ladder, and
the error kinds + exit codes.

Commands and flags are introspected from the live Typer/Click app, so the
listing never drifts from the real surface. The static tables (families,
permissions, error kinds) are read from the modules that already own them.
"""

from __future__ import annotations

from typing import Any

import typer

from knoten import __version__
from knoten.models import PERMISSIONS
from knoten.services.knoten_filename import FAMILY_TO_DIRECTORY, PREFIX_TO_FAMILY

# Error kinds + exit codes — mirrors `_classify_error` in `knoten.cli.main`.
# Duplicated here intentionally: the schema is a stable data contract and
# should not change shape just because the classifier's internal order does.
ERROR_KINDS: tuple[dict[str, Any], ...] = (
    {
        "error": "config",
        "code": 4,
        "meaning": "Missing/unreadable config (e.g. KNOTEN_API_TOKEN unset)",
    },
    {"error": "auth", "code": 2, "meaning": "Token invalid or lacks the required scope"},
    {"error": "network", "code": 2, "meaning": "Remote unreachable or returned 5xx"},
    {"error": "store", "code": 3, "meaning": "Local SQLite / filesystem failure"},
    {"error": "lock_timeout", "code": 5, "meaning": "Another knoten process holds the sync lock"},
    {
        "error": "permission_denied",
        "code": 1,
        "meaning": "Write blocked by the per-note permission pre-check",
    },
    {"error": "ambiguous_target", "code": 1, "meaning": "A filename prefix matched multiple notes"},
    {
        "error": "not_found",
        "code": 1,
        "meaning": "No note matches the target (UUID, filename, or prefix)",
    },
    {"error": "validation", "code": 1, "meaning": "Backend rejected a typed frontmatter value"},
    {"error": "user", "code": 1, "meaning": "Bad arguments or validation failure"},
    {"error": "knoten", "code": 1, "meaning": "Generic KnotenError (rare)"},
    {"error": "unknown", "code": 1, "meaning": "Uncategorised exception (very rare)"},
)


def _param_info(param: Any) -> dict[str, Any] | None:
    """Describe a single Click parameter, or None for things we don't surface.

    Duck-typed on `param.param_type_name` ("argument" / "option") instead of
    `isinstance` against `click`: typer >= 0.25 vendors its own copy of click,
    so an introspected param is not an instance of a separately-imported
    `click`'s classes — an isinstance check silently returns False there and
    the whole surface drops out of the schema.
    """
    kind = getattr(param, "param_type_name", None)
    if kind == "argument":
        return {"name": param.name, "kind": "argument", "required": param.required}
    if kind == "option":
        return {
            "name": param.name,
            "kind": "option",
            "flags": list(param.opts),
            "required": param.required,
            "is_flag": getattr(param, "is_flag", False),
            "multiple": getattr(param, "multiple", False),
            "help": (getattr(param, "help", "") or "").strip(),
        }
    return None


def _first_line(text: str | None) -> str:
    return (text or "").strip().split("\n", 1)[0].strip()


def _command_info(name: str, cmd: Any) -> dict[str, Any]:
    """Describe a command (and one level of subcommands for groups).

    A group is detected by a populated `.commands` dict rather than an
    `isinstance(cmd, click.Group)` check (see `_param_info` for why).
    """
    info: dict[str, Any] = {
        "name": name,
        "help": _first_line(cmd.help or cmd.short_help),
        "params": [p for p in (_param_info(pp) for pp in cmd.params) if p],
    }
    subcommands = getattr(cmd, "commands", None)
    if isinstance(subcommands, dict) and subcommands:
        info["subcommands"] = [_command_info(sub, subcommands[sub]) for sub in sorted(subcommands)]
    return info


def _families() -> list[dict[str, Any]]:
    """The family→prefix→directory table, derived from the filename parser."""
    out = [
        {"prefix": prefix, "family": family, "directory": FAMILY_TO_DIRECTORY.get(family, "")}
        for prefix, family in PREFIX_TO_FAMILY.items()
    ]
    # day / journal have no single-symbol prefix — they're date-derived.
    out.append({"prefix": "YYYY-MM-DD", "family": "day", "directory": FAMILY_TO_DIRECTORY["day"]})
    out.append(
        {
            "prefix": "YYYY-MM-DD Title",
            "family": "journal",
            "directory": FAMILY_TO_DIRECTORY["journal"],
        }
    )
    return out


def build_schema() -> dict[str, Any]:
    """Build the full machine-readable contract dict for `knoten schema`."""
    from knoten.cli.main import app  # local import to avoid an import cycle

    cli = typer.main.get_command(app)
    cli_commands = getattr(cli, "commands", {})
    commands = [_command_info(name, cli_commands[name]) for name in sorted(cli_commands)]

    return {
        "tool": "knoten",
        "version": __version__,
        "conventions": {
            "json": "Every command accepts --json; the JSON envelope is the stable contract.",
            "errors": (
                "On failure, --json commands print {error, message, code, ...extras} to stdout."
            ),
            "targets": (
                "Path/id arguments accept a UUID, an exact filename, "
                "or an unambiguous filename prefix."
            ),
            "wikilinks": (
                "Use plain [[target]]; piped [[a|b]] and heading-form [[a#h]] are not resolved."
            ),
        },
        "permissions": list(PERMISSIONS),
        "families": _families(),
        "errors": list(ERROR_KINDS),
        "commands": commands,
    }
