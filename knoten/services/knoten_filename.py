"""Parse and generate Kasten-style filenames.

Python port of `notes.vcoeur.com/packages/shared/src/kasten.ts`. Kept
behaviourally identical so a vault produced by either side parses the
same way — the server is still authoritative for any remote note, but
`LocalBackend` needs the same rules when a file is created or renamed
against a local-only vault.

Three-level hierarchy: *directory → family → kind*. Family is derived
from the filename prefix symbol and is immutable. Kind is usually equal
to family (only `reference` has multiple kinds).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Exact-match symbols — prefix is a single char followed by a space.
_EXACT_SYMBOLS: dict[str, str] = {
    "@": "person",
    "$": "organization",
    "%": "entity",
    "&": "topic",
    "-": "fleeting",
    "!": "permanent",
}

# Suffix-match symbols — source key sits before the symbol: `CiteKey= Title`.
_SUFFIX_SYMBOLS: dict[str, str] = {
    "=": "reference",
    ".": "literature",
    "+": "file",
}

PREFIX_TO_FAMILY: dict[str, str] = {**_EXACT_SYMBOLS, **_SUFFIX_SYMBOLS}

FAMILY_TO_PREFIX: dict[str, str] = {family: prefix for prefix, family in PREFIX_TO_FAMILY.items()}

# Canonical directory per family — used when building a note's vault path.
FAMILY_TO_DIRECTORY: dict[str, str] = {
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

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_JOURNAL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$")


@dataclass(frozen=True)
class ParsedFilename:
    """Result of parsing a Kasten filename."""

    family: str
    title: str
    source: str | None
    date: str | None


def parse_knoten_filename(filename: str) -> ParsedFilename:
    """Parse a Kasten filename into `(family, title, source, date)`.

    Mirrors `parseKastenFilename` in `notes.vcoeur.com/packages/shared/src/kasten.ts`.
    Falls back to the `fleeting` family for unprefixed filenames.
    """
    trimmed = filename.strip()
    if not trimmed:
        return ParsedFilename(family="fleeting", title="", source=None, date=None)

    first_char = trimmed[0]
    if first_char in _EXACT_SYMBOLS:
        title = trimmed[1:].strip()
        return ParsedFilename(
            family=_EXACT_SYMBOLS[first_char],
            title=title,
            source=None,
            date=None,
        )

    for symbol, family in _SUFFIX_SYMBOLS.items():
        needle = f"{symbol} "
        idx = trimmed.find(needle)
        if idx > 0:
            source = trimmed[:idx].strip() or None
            title = trimmed[idx + len(needle) :].strip()
            return ParsedFilename(family=family, title=title, source=source, date=None)
        if trimmed.endswith(symbol) and len(trimmed) > 1:
            source = trimmed[:-1].strip() or None
            return ParsedFilename(family=family, title="", source=source, date=None)

    if _DATE_ONLY_RE.match(trimmed):
        return ParsedFilename(
            family="day",
            title=trimmed,
            source=trimmed,
            date=trimmed,
        )

    journal_match = _JOURNAL_RE.match(trimmed)
    if journal_match:
        return ParsedFilename(
            family="journal",
            title=journal_match.group(2),
            source=journal_match.group(1),
            date=journal_match.group(1),
        )

    return ParsedFilename(family="fleeting", title=trimmed, source=None, date=None)


def has_valid_prefix(filename: str) -> bool:
    """True if `filename` starts with a recognised Kasten prefix.

    Unprefixed filenames are *accepted* by `parse_knoten_filename` (they
    fall back to `fleeting`), but they do not have a valid prefix. Use
    this when you want to warn the user before defaulting silently.
    """
    trimmed = filename.strip()
    if not trimmed:
        return False
    if trimmed[0] in _EXACT_SYMBOLS:
        return True
    for symbol in _SUFFIX_SYMBOLS:
        if trimmed.find(f"{symbol} ") > 0:
            return True
        if trimmed.endswith(symbol) and len(trimmed) > 1:
            return True
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}(\s|$)", trimmed))
