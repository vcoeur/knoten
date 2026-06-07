"""`knoten skill` — install the bundled agent skill (SKILL.md).

The skill is shipped as package data at `knoten/skill/SKILL.md`, so it
updates in lockstep with the CLI. `install` copies it into a skills
directory; `status` reports where it lives and whether the installed copy
matches the bundled one.

This module must not import `knoten.cli.main` — `main` registers this
sub-app at import time, so the dependency only goes one way.
"""

from __future__ import annotations

import sys
from importlib import resources
from pathlib import Path
from typing import Any

import typer

from knoten.cli.output import OutputMode, emit_json

skill_app = typer.Typer(
    help="Install the bundled knoten agent skill (SKILL.md).",
    no_args_is_help=True,
)

# Default install targets, by scope.
USER_SKILL_DIR = Path.home() / ".config" / "agents" / "skills" / "knoten"
CLAUDE_SKILL_DIR = Path.home() / ".claude" / "skills" / "knoten"
PROJECT_SKILL_DIR = Path(".agents") / "skills" / "knoten"


def _bundled_skill_text() -> str:
    """Read the SKILL.md shipped inside the package."""
    return (resources.files("knoten") / "skill" / "SKILL.md").read_text(encoding="utf-8")


def _emit_error(message: str, *, mode: OutputMode, code: int = 1, kind: str = "user") -> None:
    if mode.json:
        emit_json({"error": kind, "message": message, "code": code})
    else:
        sys.stderr.write(f"error: {message}\n")
    raise typer.Exit(code)


def _resolve_target(*, user: bool, project: bool, claude: bool, dest: Path | None) -> Path:
    """Pick the install directory from the (mutually exclusive) scope flags."""
    chosen = [
        name
        for name, flag in (("--user", user), ("--project", project), ("--claude", claude))
        if flag
    ]
    if dest is not None and chosen:
        raise ValueError(f"--dest is mutually exclusive with {chosen[0]}")
    if len(chosen) > 1:
        raise ValueError(f"pass only one of {', '.join(chosen)}")
    if dest is not None:
        return dest
    if project:
        return Path.cwd() / PROJECT_SKILL_DIR
    if claude:
        return CLAUDE_SKILL_DIR
    return USER_SKILL_DIR  # --user is the default


@skill_app.command("install")
def skill_install(
    user: bool = typer.Option(
        False, "--user", help="Install to ~/.config/agents/skills/knoten/ (default)."
    ),
    project: bool = typer.Option(
        False, "--project", help="Install to ./.agents/skills/knoten/ (project-local)."
    ),
    claude: bool = typer.Option(
        False, "--claude", help="Install to ~/.claude/skills/knoten/ (vanilla Claude Code)."
    ),
    dest: Path | None = typer.Option(None, "--dest", help="Install to an explicit directory."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing SKILL.md."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Copy the bundled SKILL.md into a skills directory."""
    mode = OutputMode.detect(json_output)
    try:
        target_dir = _resolve_target(user=user, project=project, claude=claude, dest=dest)
    except ValueError as exc:
        _emit_error(str(exc), mode=mode)
        return
    target = target_dir / "SKILL.md"
    existed = target.exists()
    if existed and not force:
        _emit_error(f"{target} already exists — pass --force to overwrite", mode=mode)
        return
    text = _bundled_skill_text()
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    payload = {
        "installed": str(target),
        "bytes": len(text.encode("utf-8")),
        "overwritten": existed,
    }
    if mode.json:
        emit_json(payload)
    else:
        verb = "Updated" if existed else "Installed"
        typer.echo(f"{verb} {target}")


@skill_app.command("status")
def skill_status(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show where the skill is installed and whether it matches the bundled copy."""
    mode = OutputMode.detect(json_output)
    bundled = _bundled_skill_text()
    targets = {
        "user": USER_SKILL_DIR / "SKILL.md",
        "project": Path.cwd() / PROJECT_SKILL_DIR / "SKILL.md",
        "claude": CLAUDE_SKILL_DIR / "SKILL.md",
    }
    rows: list[dict[str, Any]] = []
    for scope, path in targets.items():
        installed = path.exists()
        current = installed and path.read_text(encoding="utf-8") == bundled
        rows.append({"scope": scope, "path": str(path), "installed": installed, "current": current})
    payload = {"bundled_bytes": len(bundled.encode("utf-8")), "targets": rows}
    if mode.json:
        emit_json(payload)
    else:
        for row in rows:
            if not row["installed"]:
                state = "not installed"
            elif row["current"]:
                state = "up to date"
            else:
                state = "STALE (run `knoten skill install --force`)"
            typer.echo(f"{row['scope']:<8} {state:<12} {row['path']}")
