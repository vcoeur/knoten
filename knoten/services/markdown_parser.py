"""Parse markdown bodies for tags and wiki-links.

This is used when ingesting notes from disk (e.g. during reindex or full
import from an export zip) where the server's linkMap is not available.

When the server-provided linkMap is available, prefer that — it already
resolves titles to UUIDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Wikilink grammar — mirrors notes.vcoeur.com's shared/src/wikilinks.ts. The
# inner text is everything between `[[` and the first `]]` (any char but `]`);
# `_split_wikilink` then peels off the `#heading` and `|alias` suffixes so all
# four forms — [[slug]], [[slug#h]], [[slug|a]], [[slug#h|a]] — resolve to the
# same `slug` target.
_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")
# Tag grammar — mirrors the server's `extractTags` regex
# /(?<!\w)#([a-zA-Z][a-zA-Z0-9_-]*)/g: an ASCII-letter start, then ASCII
# letters / digits / `-` / `_`. `re.ASCII` keeps the `(?<!\w)` lookbehind
# ASCII-only so it matches the server's JS `\w` exactly (e.g. a tag glued to a
# preceding accented letter is still a tag, as it is on the server). Matches are
# lowercased on extraction. Note the continuation set stops at the first
# non-ASCII-word char, so `#café` yields `caf` (matching the server) — odd but
# intentional parity.
_TAG_RE = re.compile(r"(?<!\w)#([a-zA-Z][a-zA-Z0-9_-]*)", re.ASCII)
_DRAWING_BLOCK_RE = re.compile(r"```drawing\n.*?\n```", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _split_wikilink(inner: str) -> str:
    """Return the resolution slug from a wikilink's inner text.

    The slug is the text before the first `#heading` or `|alias`, trimmed.
    Mirrors `splitWikilink` in the server's shared/src/wikilinks.ts: the alias
    starts at the first `|`, and the `#` that precedes the alias delimits the
    heading (a `#` after the `|` belongs to the alias text, not the anchor).
    Returns an empty string for anchor-only / alias-only links (e.g. `[[#h]]`).
    """
    pipe_index = inner.find("|")
    before_alias = inner[:pipe_index] if pipe_index >= 0 else inner
    hash_index = before_alias.find("#")
    slug = before_alias[:hash_index] if hash_index >= 0 else before_alias
    return slug.strip()


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
        tag = match.group(1).lower()
        if tag not in seen_tags:
            seen_tags.add(tag)
            tags.append(tag)

    titles: list[str] = []
    seen_titles: set[str] = set()
    for match in _WIKILINK_RE.finditer(stripped):
        title = _split_wikilink(match.group(1))
        if title and title not in seen_titles:
            seen_titles.add(title)
            titles.append(title)

    return ParsedBody(tags=tuple(tags), wikilink_titles=tuple(titles))
