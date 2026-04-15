"""Backend abstraction — the contract KastenManager depends on.

Two implementations will live side by side:

- `RemoteBackend` talks to `notes.vcoeur.com` over HTTP (current behaviour).
- `LocalBackend` operates on an on-disk markdown vault + local SQLite index.

The service layer is typed against this `Protocol` and has no knowledge of
which implementation it received. Shared tests parametrise over both where
the behaviour is backend-agnostic.

Every implementation must honour the exception types from
`app.repositories.errors` (`NetworkError`, `AuthError`, `NotFoundError`,
`NoteForbiddenError`) so the service layer stays backend-agnostic. Local
backends that have no permission model simply never raise
`NoteForbiddenError` — the sync placeholder branch is dead code for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.models import Note, NoteSummary


@dataclass(frozen=True)
class NotesPage:
    """One page of note summaries, newest-first."""

    data: tuple[NoteSummary, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class NoteDraft:
    """New-note payload for `create_note`."""

    filename: str
    body: str = ""
    kind: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotePatch:
    """Subset of fields to update on an existing note.

    Every field is optional — `None` means "don't change this field". Tags
    are expressed as add/remove operations rather than a full replacement,
    mirroring the existing CLI flags and avoiding accidental clobbers.
    """

    filename: str | None = None
    title: str | None = None
    body: str | None = None
    frontmatter: dict[str, Any] | None = None
    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class NoteUpdateResult:
    """Return value of `update_note`.

    `affected_notes` lists ids of notes whose bodies were rewritten as a
    side effect of a filename change (rename cascade). Empty for non-rename
    patches. `RemoteBackend` reads it from the server's `affectedNotes`
    envelope; `LocalBackend` computes it locally while walking the vault.
    """

    note_id: str
    affected_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttachmentUploadResult:
    """Return value of `upload_attachment`."""

    storage_key: str
    content_type: str | None = None
    size_bytes: int | None = None
    url: str | None = None


@dataclass(frozen=True)
class AttachmentDownloadResult:
    """Return value of `download_attachment`."""

    path: Path
    bytes_written: int
    content_type: str = ""
    filename: str | None = None


@runtime_checkable
class Backend(Protocol):
    """The full contract KastenManager needs from a notes backend.

    Eight business methods plus `close()` for resource release. Everything
    outside this Protocol is caller-side glue that does not belong on the
    backend contract (e.g. `iter_all_summaries` in the sync service wraps
    `list_note_summaries` with pagination logic).
    """

    def close(self) -> None:
        """Release any resources. Idempotent."""
        ...

    def list_note_summaries(self, *, limit: int, offset: int) -> NotesPage:
        """Return one page of note summaries, newest-first.

        Drives sync's initial fetch and the reconciliation pass that detects
        deletions. `NotesPage.total` lets the caller spot drift between pages.
        """
        ...

    def read_note(self, note_id: str) -> Note:
        """Fetch a full note with body, frontmatter, tags, wikilinks.

        Remote backends raise `NoteForbiddenError` when the caller has
        list-but-not-read permission on a note; local backends never do.
        """
        ...

    def create_note(self, draft: NoteDraft) -> str:
        """Create a note and return its id. Caller re-fetches via read_note()."""
        ...

    def update_note(self, note_id: str, patch: NotePatch) -> NoteUpdateResult:
        """Update any subset of filename/title/body/frontmatter/tags.

        On filename change, triggers a cascade: every other note that
        referenced the old filename via `[[old]]` is rewritten to `[[new]]`
        and returned in `affected_notes`. The caller re-fetches `note_id`
        and every affected id via `read_note()` before ingesting.
        """
        ...

    def append_to_note(self, note_id: str, content: str) -> None:
        """Append content to the body using the weaker APPEND capability.

        Kept distinct from `update_note` because APPEND is a lower privilege
        on the server's permission model — a restricted viewer with APPEND
        can extend a note but not replace it.
        """
        ...

    def delete_note(self, note_id: str) -> None:
        """Soft-delete (move to trash). Reversible via `restore_note`."""
        ...

    def restore_note(self, note_id: str) -> None:
        """Restore a soft-deleted note from trash."""
        ...

    def upload_attachment(
        self,
        path: Path,
        *,
        content_type: str | None = None,
        source: str | None = None,
    ) -> AttachmentUploadResult:
        """Store a file and return at least its storage key.

        The storage key is the opaque handle the caller writes into the
        file-family note's frontmatter. Other fields are best-effort.
        """
        ...

    def download_attachment(self, storage_key: str, destination: Path) -> AttachmentDownloadResult:
        """Stream an attachment to `destination`."""
        ...
