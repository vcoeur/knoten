"""Parse markdown bodies for tags and wiki-links.

This is used when ingesting notes from disk (e.g. during reindex or full
import from an export zip) where the server's linkMap is not available.

When the server-provided linkMap is available, prefer that — it already
resolves titles to UUIDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WIKILINK_RE = re.compile(r"\[\[([^\[\]#|]+?)(?:\|[^\[\]]+)?\]\]")
_TAG_RE = re.compile(r"(?<![\w#])#([\w\-]+)")
_DRAWING_BLOCK_RE = re.compile(r"```drawing\n.*?\n```", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


@dataclass(frozen=True)
class ParsedBody:
    tags: tuple[str, ...]
    wikilink_titles: tuple[str, ...]


def parse_body(body: str) -> ParsedBody:
    """Extract tags and wiki-link titles from a markdown body.

    Code fences and inline code are stripped before scanning so that
    example snippets containing `#foo` or `[[x]]` don't pollute the index.
    Drawing blocks are also stripped (Excalidraw JSON).
    """
    stripped = _DRAWING_BLOCK_RE.sub("", body)
    stripped = _CODE_FENCE_RE.sub("", stripped)
    stripped = _INLINE_CODE_RE.sub("", stripped)

    tags: list[str] = []
    seen_tags: set[str] = set()
    for match in _TAG_RE.finditer(stripped):
        tag = match.group(1)
        if tag not in seen_tags:
            seen_tags.add(tag)
            tags.append(tag)

    titles: list[str] = []
    seen_titles: set[str] = set()
    for match in _WIKILINK_RE.finditer(stripped):
        title = match.group(1).strip()
        if title and title not in seen_titles:
            seen_titles.add(title)
            titles.append(title)

    return ParsedBody(tags=tuple(tags), wikilink_titles=tuple(titles))
