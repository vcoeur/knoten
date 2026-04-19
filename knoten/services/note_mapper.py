"""Translate remote-backend API payloads into domain models.

The remote returns raw dicts from its HTTP routes. This module is the single
place where that shape is interpreted — every other layer consumes the typed
models from `app.models`.
"""

from __future__ import annotations

from typing import Any

from knoten.models import Note, NoteSummary, WikiLink


def summary_from_api(payload: dict[str, Any]) -> NoteSummary:
    """Convert a list-endpoint row into a NoteSummary."""
    return NoteSummary(
        id=str(payload["id"]),
        filename=_as_str(payload.get("filename", "")),
        title=_as_str(payload.get("title", "")),
        family=_as_str(payload.get("family", "")),
        kind=_as_str(payload.get("kind", "")),
        source=_as_str(payload.get("source")) or None,
        tags=tuple(_as_str(t) for t in (payload.get("tags") or ())),
        created_at=_as_str(payload.get("createdAt") or payload.get("created_at", "")),
        updated_at=_as_str(payload.get("updatedAt") or payload.get("updated_at", "")),
        permissions=_as_str(payload.get("permissions")) or "ALL",
    )


def note_from_api(payload: dict[str, Any]) -> Note:
    """Convert a single-note read response into a Note.

    Uses the server-provided linkMap (title -> UUID, null for broken) to build
    the wiki-links tuple. Tags come straight from the payload.
    """
    link_map = payload.get("linkMap") or {}
    wikilinks = tuple(
        WikiLink(target_title=str(title), target_id=(str(target) if target else None))
        for title, target in link_map.items()
    )
    frontmatter = payload.get("frontmatter") or {}
    return Note(
        id=str(payload["id"]),
        filename=_as_str(payload.get("filename", "")),
        title=_as_str(payload.get("title", "")),
        family=_as_str(payload.get("family", "")),
        kind=_as_str(payload.get("kind", "")),
        source=_as_str(payload.get("source")) or None,
        body=_as_str(payload.get("body", "")),
        frontmatter=dict(frontmatter) if isinstance(frontmatter, dict) else {},
        tags=tuple(_as_str(t) for t in (payload.get("tags") or ())),
        wikilinks=wikilinks,
        created_at=_as_str(payload.get("createdAt") or payload.get("created_at", "")),
        updated_at=_as_str(payload.get("updatedAt") or payload.get("updated_at", "")),
        permissions=_as_str(payload.get("permissions")) or "ALL",
    )


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)
