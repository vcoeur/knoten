"""Offline rebuild of the derived index from `notes` rows + on-disk files.

When SQLite itself is healthy but the derived tables (`notes_fts`, `tags`,
`wikilinks`, `frontmatter_fields`) are suspect, `reindex` walks every note,
re-reads its body from the mirror file, re-parses tags and wiki-links, and
rewrites the derived rows. No network required.

Composition with other commands:

- `knoten verify` — catches drift between disk, store, and remote. Needs
  the network to re-fetch missing/drifted files.
- `knoten reindex` — catches drift between the derived index and the
  `notes` table. No network. Trusts on-disk content.
- `knoten sync --verify` — the nuclear option: re-fetch every note from
  the remote and re-ingest, which rebuilds everything from scratch.

`reindex` and `verify` are complementary: run `reindex` first (offline, fast),
then `verify` (with network) if anything is still missing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from knoten.models import Note, WikiLink
from knoten.repositories.store import Store
from knoten.services.markdown_parser import parse_body
from knoten.settings import Settings

ProgressCallback = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass
class ReindexResult:
    checked: int = 0
    reindexed: int = 0
    skipped_missing_file: int = 0
    integrity: str = "ok"
    cardinality_before: dict = field(default_factory=dict)
    cardinality_after: dict = field(default_factory=dict)
    missing_file_ids: list[str] = field(default_factory=list)


def reindex_from_files(
    *,
    store: Store,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ReindexResult:
    """Walk every active note, re-read its body from disk, and rewrite the
    derived tables (FTS5, tags, wikilinks, frontmatter_fields).

    Notes whose mirror file is missing are skipped and reported — run
    `knoten verify` after to pull them back from the remote.
    """
    log = progress or _noop
    result = ReindexResult()
    log("→ SQLite integrity check")
    result.integrity = store.integrity_check()
    log(f"  {result.integrity}")
    log("→ FTS5 / notes cardinality (before)")
    result.cardinality_before = store.fts_cardinality_check()
    log(
        f"  notes={result.cardinality_before['notes_count']} "
        f"fts={result.cardinality_before['fts_count']} "
        f"consistent={result.cardinality_before['consistent']}"
    )

    rows = store.all_rows()
    result.checked = len(rows)
    log(f"→ Rebuilding derived tables from {result.checked} on-disk file(s)")

    for row in rows:
        absolute = settings.vault_dir / row.path
        if not absolute.exists():
            result.skipped_missing_file += 1
            result.missing_file_ids.append(row.id)
            continue

        try:
            text = absolute.read_text(encoding="utf-8")
        except OSError:
            result.skipped_missing_file += 1
            result.missing_file_ids.append(row.id)
            continue

        body = _strip_frontmatter(text)
        parsed = parse_body(body)

        # Pull the rest of the metadata from the stored notes row.
        full = store.full_row(row.id)
        if full is None:
            # Race with a concurrent delete — skip silently.
            continue
        frontmatter = _load_frontmatter(full.get("frontmatter_json") or "{}")

        # Reconstruct a Note from the store + parsed body. The `wikilinks`
        # we get here are title-only (target_id unknown), because the remote
        # is the only one who can resolve titles → UUIDs. Preserve existing
        # resolved target_ids from the store so we don't lose them.
        existing_wikilinks = {
            row["target_title"]: row["target_id"] for row in store.wikilinks_for_note(row.id)
        }
        wikilinks = tuple(
            WikiLink(
                target_title=title,
                target_id=existing_wikilinks.get(title),
            )
            for title in parsed.wikilink_titles
        )

        note = Note(
            id=row.id,
            filename=full["filename"],
            title=full["title"],
            family=full["family"],
            kind=full["kind"],
            source=full["source"],
            body=body,
            frontmatter=frontmatter,
            tags=parsed.tags,
            wikilinks=wikilinks,
            created_at=full["created_at"],
            updated_at=full["updated_at"],
            mcp_permissions=full.get("mcp_permissions") or "ALL",
        )

        body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        store.upsert_note(note, path=row.path, body_sha256=body_sha)
        result.reindexed += 1
        if result.reindexed % 200 == 0:
            log(f"  reindexed {result.reindexed}/{result.checked}")

    log(f"  reindexed {result.reindexed}, skipped {result.skipped_missing_file}")
    log("→ FTS5 / notes cardinality (after)")
    result.cardinality_after = store.fts_cardinality_check()
    log(
        f"  notes={result.cardinality_after['notes_count']} "
        f"fts={result.cardinality_after['fts_count']} "
        f"consistent={result.cardinality_after['consistent']}"
    )
    return result


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + 5 :]


def _load_frontmatter(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
