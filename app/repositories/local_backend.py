"""Filesystem + SQLite `Backend` implementation.

Operates on a markdown vault under `settings.vault_dir` with a local
`Store` (SQLite + FTS5) as a derived index. No network, no permission
model — a standalone zettelkasten CLI for users who do not want to run
their own `notes.vcoeur.com` instance.

Phase 5: read path (`list_note_summaries`, `read_note`) plus a
mtime-gated stat walk in `_refresh_index_if_stale` that detects external
edits — files written by the user's editor, deleted via `rm`, etc. The
walk runs at most once per CLI invocation. Mutation methods still raise
`NotImplementedError` until Phase 6 lands writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from app.models import Note, WikiLink
from app.repositories.backend import (
    AttachmentDownloadResult,
    AttachmentUploadResult,
    Backend,
    NoteDraft,
    NotePatch,
    NotesPage,
    NoteUpdateResult,
)
from app.repositories.errors import NotFoundError, UserError
from app.repositories.store import Store
from app.services.markdown_parser import parse_body
from app.settings import Settings

_LOG = logging.getLogger(__name__)

# Top-level directories under the vault that the drift walk skips. These
# hold machine-managed files (soft-deleted notes, attachment blobs) that
# should not surface as note drift.
_SKIP_TOP_LEVEL = frozenset({".trash", ".attachments"})


class LocalBackend(Backend):
    """`Backend` backed by a markdown vault + local SQLite index."""

    def __init__(self, settings: Settings) -> None:
        if not settings.vault_dir.exists():
            raise UserError(
                f"Vault directory does not exist: {settings.vault_dir} — "
                "create it or set KASTEN_HOME to a directory with a `kasten/` subdir."
            )
        self._settings = settings
        self._vault_dir = settings.vault_dir
        self._store = Store(settings.index_path)
        self._store.open()
        self._reindex_done: bool = False

    def __enter__(self) -> LocalBackend:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._store.close()

    def _refresh_index_if_stale(self) -> None:
        """Stat-walk the vault and refresh drifted rows in the store.

        Runs at most once per `LocalBackend` instance — one walk per CLI
        invocation. For each `.md` file under `vault_dir`:

        - `(mtime_ns, size)` matches the store's recorded tuple → no-op.
        - Tuple mismatches → re-parse the body and call
          `Store.apply_drifted_body` to refresh FTS5, tags, wikilinks,
          and the recorded stat values.
        - File not known to the store (new file on disk) → logged and
          skipped. Creating notes from unknown markdown files requires a
          filename-to-family parser that lands with `create_note` in
          Phase 6; until then, users who drop files into the vault by
          hand need to re-run `kasten sync` after Phase 6.

        Missing files — entries in the store whose path is no longer on
        disk — are hard-deleted. External `rm foo.md` is a permanent
        delete; only `kasten delete` moves files to `.trash/`.
        """
        if self._reindex_done:
            return
        self._reindex_done = True

        try:
            path_index = self._store.path_index()
        except Exception as exc:
            _LOG.debug("Skipping stat walk (store not ready): %s", exc)
            return

        seen_paths: set[str] = set()
        vault_root = self._vault_dir.resolve()

        for file_path in sorted(self._vault_dir.rglob("*.md")):
            try:
                relative = file_path.resolve().relative_to(vault_root)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] in _SKIP_TOP_LEVEL:
                continue

            relative_str = str(relative)
            seen_paths.add(relative_str)

            try:
                stat = file_path.stat()
            except OSError:
                continue

            indexed = path_index.get(relative_str)
            if indexed is None:
                _LOG.debug("Unknown vault file %s — Phase 5 skip; land in Phase 6", relative_str)
                continue

            recorded_mtime, recorded_size, note_id = indexed
            if recorded_mtime == stat.st_mtime_ns and recorded_size == stat.st_size:
                continue

            try:
                raw = file_path.read_text(encoding="utf-8")
            except OSError:
                continue
            body = _strip_frontmatter(raw)
            parsed = parse_body(body)
            body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            self._store.apply_drifted_body(
                note_id,
                body=body,
                body_sha256=body_sha,
                tags=parsed.tags,
                wikilink_titles=parsed.wikilink_titles,
                path_mtime_ns=stat.st_mtime_ns,
                path_size=stat.st_size,
            )

        for missing_path in set(path_index) - seen_paths:
            _, _, missing_id = path_index[missing_path]
            self._store.delete_note(missing_id)

    def list_note_summaries(self, *, limit: int, offset: int) -> NotesPage:
        self._refresh_index_if_stale()
        summaries, total = self._store.list_notes(limit=limit, offset=offset)
        return NotesPage(
            data=tuple(summaries),
            total=total,
            limit=limit,
            offset=offset,
        )

    def read_note(self, note_id: str) -> Note:
        self._refresh_index_if_stale()
        row = self._store.find_by_id(note_id)
        if row is None:
            raise NotFoundError(f"No note with id {note_id}")

        absolute = self._vault_dir / row["path"]
        try:
            raw = absolute.read_text(encoding="utf-8")
        except OSError as exc:
            raise NotFoundError(f"Mirror file missing for {note_id}: {exc}") from exc

        body = _strip_frontmatter(raw)

        frontmatter_raw = row.get("frontmatter_json") or "{}"
        try:
            frontmatter = json.loads(frontmatter_raw)
        except (TypeError, ValueError):
            frontmatter = {}
        if not isinstance(frontmatter, dict):
            frontmatter = {}

        wikilinks = tuple(
            WikiLink(
                target_title=str(link["target_title"]),
                target_id=(str(link["target_id"]) if link["target_id"] else None),
            )
            for link in self._store.wikilinks_for_note(note_id)
        )
        tags = self._store.tags_for_note(note_id)

        return Note(
            id=row["id"],
            filename=row["filename"],
            title=row["title"] or "",
            family=row["family"] or "",
            kind=row["kind"] or "",
            source=row.get("source"),
            body=body,
            frontmatter=frontmatter,
            tags=tuple(tags),
            wikilinks=wikilinks,
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
            mcp_permissions=row.get("mcp_permissions") or "ALL",
        )

    def create_note(self, draft: NoteDraft) -> str:
        raise NotImplementedError("LocalBackend writes land in Phase 6")

    def update_note(self, note_id: str, patch: NotePatch) -> NoteUpdateResult:
        raise NotImplementedError("LocalBackend writes land in Phase 6")

    def append_to_note(self, note_id: str, content: str) -> None:
        raise NotImplementedError("LocalBackend writes land in Phase 6")

    def delete_note(self, note_id: str) -> None:
        raise NotImplementedError("LocalBackend writes land in Phase 6")

    def restore_note(self, note_id: str) -> None:
        raise NotImplementedError("LocalBackend writes land in Phase 6")

    def upload_attachment(
        self,
        path: Path,
        *,
        content_type: str | None = None,
        source: str | None = None,
    ) -> AttachmentUploadResult:
        raise NotImplementedError("LocalBackend attachments land in Phase 7")

    def download_attachment(self, storage_key: str, destination: Path) -> AttachmentDownloadResult:
        raise NotImplementedError("LocalBackend attachments land in Phase 7")


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block, if any."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + 5 :]
