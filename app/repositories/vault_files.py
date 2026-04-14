"""Filesystem mirror: writes note markdown files with YAML frontmatter.

Mirrors the format of notes.vcoeur.com's export endpoint so that the local
vault is a superset of what `kasten sync --full` can ingest directly.

One file per note. Atomic writes (tmp + rename). Old files at a stale path
are removed when a note is renamed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.models import Note, NoteSummary

# Mirrors notes.vcoeur.com's export layout — see
# `packages/shared/src/kasten.ts:FAMILY_TO_DIRECTORY` and
# `packages/server/src/services/export.ts`. The `docs/api.md` file shows
# underscore-prefixed names, but that is stale — the code uses bare names.
_FAMILY_DIRS = {
    "person": "entity",
    "organization": "entity",
    "entity": "entity",
    "topic": "entity",
    "fleeting": "note",
    "permanent": "note",
    "reference": "literature",
    "literature": "literature",
    "file": "files",
    "day": "journal",
    "journal": "journal",
}


def path_for_note(note: Note) -> str:
    """Return the path (relative to the vault root) where this note should live.

    Journal families are bucketed by YYYY-MM, derived from the note's source
    (expected to be a YYYY-MM-DD date) or from the first 10 chars of the
    filename. Other families go flat into the family directory.
    """
    return _path_for(note.family, note.source, note.filename)


def path_for_summary(summary: NoteSummary) -> str:
    """Same rule as `path_for_note` but for a list-endpoint summary row."""
    return _path_for(summary.family, summary.source, summary.filename)


def _path_for(family: str, source: str | None, filename: str) -> str:
    base = _FAMILY_DIRS.get(family, ".")
    if family in ("day", "journal"):
        prefix = _month_prefix(source) or _month_prefix(filename[:10])
        if prefix is None:
            return f"{base}/{filename}.md"
        return f"{base}/{prefix}/{filename}.md"
    if base == ".":
        return f"{filename}.md"
    return f"{base}/{filename}.md"


def _month_prefix(value: str | None) -> str | None:
    if not value or len(value) < 7:
        return None
    candidate = value[:7]
    if len(candidate) == 7 and candidate[4] == "-" and candidate[:4].isdigit():
        return candidate
    return None


def render_note_markdown(note: Note) -> str:
    """Produce the YAML-frontmatter markdown representation of a note."""
    frontmatter = _sanitise_frontmatter(note)
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(_yaml_line(key, value))
    lines.append("---")
    lines.append("")
    lines.append(note.body.rstrip("\n"))
    lines.append("")
    return "\n".join(lines)


def render_placeholder_markdown(summary: NoteSummary) -> str:
    """Produce a marker file for a note whose body we cannot fetch.

    The file has full YAML frontmatter so that it looks legitimate when
    read by hand or ingested by `reindex`. A `restricted: true` key in
    the frontmatter makes the state explicit; the body is a one-line
    explanation so Claude / the user can tell what is going on.
    """
    lines = ["---"]
    lines.append(_yaml_line("kind", summary.kind))
    lines.append(_yaml_line("family", summary.family))
    lines.append(_yaml_line("title", summary.title))
    if summary.source:
        lines.append(_yaml_line("source", summary.source))
    lines.append(_yaml_line("created", summary.created_at))
    lines.append(_yaml_line("updated", summary.updated_at))
    lines.append(_yaml_line("restricted", True))
    lines.append("---")
    lines.append("")
    lines.append(
        "_Body not fetchable — the current API token does not have READ "
        "permission for this note. Open it on notes.vcoeur.com (as the web "
        "user) to see its content._"
    )
    lines.append("")
    return "\n".join(lines)


def _sanitise_frontmatter(note: Note) -> dict[str, Any]:
    """Build the frontmatter block we actually write to disk.

    Always includes the server-authoritative fields (kind, family, created,
    updated) on top of the user-visible frontmatter, so a file read back in
    isolation still has enough context to be ingested.
    """
    out: dict[str, Any] = {
        "kind": note.kind,
        "family": note.family,
        "title": note.title,
    }
    if note.source:
        out["source"] = note.source
    # Copy user-visible frontmatter except keys we already set authoritatively.
    for key, value in note.frontmatter.items():
        if key in out:
            continue
        out[key] = value
    out["created"] = note.created_at
    out["updated"] = note.updated_at
    return out


def _yaml_line(key: str, value: Any) -> str:
    """Emit a single YAML key: value line.

    Kept intentionally simple — the export format uses only scalars and flat
    lists, and we match that. Anything more exotic is JSON-encoded.
    """
    if value is None:
        return f"{key}: "
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key}: {value}"
    if isinstance(value, list):
        if not value:
            return f"{key}: []"
        rendered_items = ", ".join(_yaml_inline(item) for item in value)
        return f"{key}: [{rendered_items}]"
    return f"{key}: {_yaml_inline(value)}"


def _yaml_inline(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # Quote if contains any char that would break a plain YAML scalar.
    if any(ch in text for ch in ":#[]{},&*!|>'\"%@`") or text.startswith(" ") or text.endswith(" "):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _safe_destination(vault_dir: Path, relative_path: str) -> Path:
    """Return `vault_dir / relative_path`, refusing any path that escapes the vault.

    The server is the source of truth for note paths, but we defend against the
    case where an untrusted or broken server returns a filename containing `..`
    or an absolute path: resolving the target and asserting it stays under the
    resolved vault dir catches both.
    """
    destination = vault_dir / relative_path
    vault_resolved = vault_dir.resolve()
    destination_resolved = destination.resolve()
    if destination_resolved != vault_resolved and not destination_resolved.is_relative_to(
        vault_resolved
    ):
        raise ValueError(f"refusing to touch path outside vault: {relative_path!r}")
    return destination


def write_note_file(vault_dir: Path, relative_path: str, content: str) -> Path:
    """Atomically write `content` to `vault_dir/relative_path`."""
    destination = _safe_destination(vault_dir, relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, destination)
    return destination


def remove_note_file(vault_dir: Path, relative_path: str) -> None:
    """Remove a note file, ignoring "already gone" errors."""
    destination = _safe_destination(vault_dir, relative_path)
    try:
        destination.unlink()
    except FileNotFoundError:
        pass
    # Clean up empty parent dirs up to vault_dir (best-effort).
    parent = destination.parent
    while parent != vault_dir and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
