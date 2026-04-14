"""Filesystem + SQLite `Backend` implementation.

Operates on a markdown vault under `settings.vault_dir` with a local
`Store` (SQLite + FTS5) as a derived index. No network, no permission
model — a standalone zettelkasten CLI for users who do not want to run
their own `notes.vcoeur.com` instance.

Phase 4: read path (`list_note_summaries`, `read_note`). Every mutation
method raises `NotImplementedError` until Phase 6. The stat-walk drift
detector `_refresh_index_if_stale` is a no-op until Phase 5.
"""

from __future__ import annotations

import json
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
from app.settings import Settings


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
        """Walk the vault and refresh drifted rows in the store.

        Phase 4 stub — the real mtime-gated walk lands in Phase 5. Kept
        here now so the read-path methods already call it, and the only
        change in Phase 5 is the body of this method.
        """
        if self._reindex_done:
            return
        self._reindex_done = True

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
