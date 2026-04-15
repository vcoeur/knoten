"""Core note models — pure dataclasses mirrored from the remote-backend API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MCP_PERMISSIONS: tuple[str, ...] = ("NONE", "LIST", "READ", "APPEND", "WRITE", "ALL")
"""Ordered (cumulative) MCP permission levels — mirrors the remote backend.

Each level grants all capabilities of the lower levels. `ALL` is the default
for every note the server creates. A note at `APPEND` cannot be freely
rewritten; a note at `READ` cannot be mutated at all.
"""

_MCP_PERMISSION_RANK: dict[str, int] = {name: index for index, name in enumerate(MCP_PERMISSIONS)}


def permission_rank(level: str) -> int:
    """Numeric rank for a permission level. Unknown levels rank as permissive (ALL).

    Defensive default so that a future server-side level we don't know about
    does not accidentally block writes — the server itself is the final gate.
    """
    return _MCP_PERMISSION_RANK.get(level, len(MCP_PERMISSIONS) - 1)


def permission_at_least(level: str, minimum: str) -> bool:
    """Return True if `level` grants at least the capability named by `minimum`."""
    return permission_rank(level) >= permission_rank(minimum)


@dataclass(frozen=True)
class WikiLink:
    """A wiki-link parsed from a note body.

    `target_id` is None when the link is broken (target does not exist).
    """

    target_title: str
    target_id: str | None

    @property
    def broken(self) -> bool:
        return self.target_id is None


@dataclass(frozen=True)
class NoteSummary:
    """Lightweight note row — no body. Matches GET /api/notes list rows."""

    id: str
    filename: str
    title: str
    family: str
    kind: str
    source: str | None
    tags: tuple[str, ...]
    created_at: str
    updated_at: str
    mcp_permissions: str = "ALL"


@dataclass(frozen=True)
class Note:
    """Full note with body and parsed links. Matches GET /api/notes/{id}."""

    id: str
    filename: str
    title: str
    family: str
    kind: str
    source: str | None
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    wikilinks: tuple[WikiLink, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    mcp_permissions: str = "ALL"

    def to_summary(self) -> NoteSummary:
        return NoteSummary(
            id=self.id,
            filename=self.filename,
            title=self.title,
            family=self.family,
            kind=self.kind,
            source=self.source,
            tags=self.tags,
            created_at=self.created_at,
            updated_at=self.updated_at,
            mcp_permissions=self.mcp_permissions,
        )


@dataclass(frozen=True)
class SearchHit:
    """A single FTS5 result row."""

    id: str
    title: str
    family: str
    kind: str
    source: str | None
    path: str
    absolute_path: str
    tags: tuple[str, ...]
    score: float
    snippet: str
    updated_at: str
    mcp_permissions: str = "ALL"
    explain: tuple[tuple[str, float], ...] | None = None
    """Per-column bm25 contributions when `search --explain` is set.

    Tuple of (column_name, score) pairs — tuple (not dict) because
    SearchHit is frozen and dicts are not hashable. None when the caller
    did not ask for an explanation.
    """
