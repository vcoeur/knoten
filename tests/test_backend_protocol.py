"""Sanity check for `app.repositories.backend`.

Asserts the `Backend` Protocol is runtime-checkable and that a minimal
fake implementation passes the structural check. Catches broken imports
and signature drift early — no implementation code is exercised here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models import Note
from app.repositories.backend import (
    AttachmentDownloadResult,
    AttachmentUploadResult,
    Backend,
    NoteDraft,
    NotePatch,
    NotesPage,
    NoteUpdateResult,
)


class _FakeBackend:
    """Satisfies the `Backend` Protocol with no real behaviour."""

    def close(self) -> None:
        return None

    def list_note_summaries(self, *, limit: int, offset: int) -> NotesPage:
        return NotesPage(data=(), total=0, limit=limit, offset=offset)

    def read_note(self, note_id: str) -> Note:
        return Note(
            id=note_id,
            filename="- stub",
            title="stub",
            family="fleeting",
            kind="fleeting",
            source=None,
            body="",
        )

    def create_note(self, draft: NoteDraft) -> str:
        return "00000000-0000-0000-0000-000000000000"

    def update_note(self, note_id: str, patch: NotePatch) -> NoteUpdateResult:
        return NoteUpdateResult(note_id=note_id, affected_notes=())

    def append_to_note(self, note_id: str, content: str) -> None:
        return None

    def delete_note(self, note_id: str) -> None:
        return None

    def restore_note(self, note_id: str) -> None:
        return None

    def upload_attachment(
        self,
        path: Path,
        *,
        content_type: str | None = None,
        source: str | None = None,
    ) -> AttachmentUploadResult:
        return AttachmentUploadResult(storage_key="stub")

    def download_attachment(self, storage_key: str, destination: Path) -> AttachmentDownloadResult:
        return AttachmentDownloadResult(path=destination, bytes_written=0)


def test_backend_is_runtime_checkable() -> None:
    fake = _FakeBackend()
    assert isinstance(fake, Backend)


def test_dataclasses_are_frozen() -> None:
    page = NotesPage(data=(), total=0, limit=50, offset=0)
    try:
        page.total = 1  # type: ignore[misc]
    except Exception as exc:
        assert "frozen" in str(exc).lower() or "cannot assign" in str(exc).lower()
    else:
        raise AssertionError("NotesPage is not frozen")


def test_notepatch_defaults_are_empty() -> None:
    patch = NotePatch()
    assert patch.filename is None
    assert patch.title is None
    assert patch.body is None
    assert patch.frontmatter is None
    assert patch.add_tags == ()
    assert patch.remove_tags == ()


def test_notedraft_frontmatter_is_independent() -> None:
    a = NoteDraft(filename="- a")
    b = NoteDraft(filename="- b")
    a.frontmatter["shared"] = "state"
    assert "shared" not in b.frontmatter


def _static_type_check(backend: Backend) -> tuple[str, ...]:
    """Compile-time witness: the methods exist on the Protocol.

    Called from `test_backend_protocol_static_surface` — the body never
    runs against a real backend, but importing and referencing the methods
    forces the type checker / interpreter to notice signature drift.
    """
    _: Any = backend.list_note_summaries
    _ = backend.read_note
    _ = backend.create_note
    _ = backend.update_note
    _ = backend.append_to_note
    _ = backend.delete_note
    _ = backend.restore_note
    _ = backend.upload_attachment
    _ = backend.download_attachment
    _ = backend.close
    return (
        "list_note_summaries",
        "read_note",
        "create_note",
        "update_note",
        "append_to_note",
        "delete_note",
        "restore_note",
        "upload_attachment",
        "download_attachment",
        "close",
    )


def test_backend_protocol_static_surface() -> None:
    names = _static_type_check(_FakeBackend())
    assert len(names) == 10
