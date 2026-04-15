"""LocalBackend read path — `list_note_summaries` + `read_note`.

Populates a vault and its Store via `ingest_note` (the same pipeline the
real CLI uses), then drives `LocalBackend` against it. No network, no
mocks — purely filesystem and SQLite.
"""

from __future__ import annotations

import pytest

from app.models import Note
from app.repositories.backend import NotesPage
from app.repositories.errors import NotFoundError
from app.repositories.local_backend import LocalBackend
from app.repositories.store import Store
from app.services.notes import ingest_note
from app.settings import Settings


def _make_note(
    note_id: str,
    filename: str,
    family: str,
    body: str,
    updated_at: str = "2024-01-02T00:00:00Z",
) -> Note:
    return Note(
        id=note_id,
        filename=filename,
        title=filename.lstrip("- @=+!").strip(),
        family=family,
        kind=family,
        source=None,
        body=body,
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at=updated_at,
        mcp_permissions="ALL",
    )


def _seed_vault(settings: Settings) -> list[Note]:
    notes = [
        _make_note(
            "11111111-1111-1111-1111-111111111111",
            "- First",
            "fleeting",
            "First body.",
            updated_at="2024-01-05T00:00:00Z",
        ),
        _make_note(
            "22222222-2222-2222-2222-222222222222",
            "! Second permanent",
            "permanent",
            "Second body with a [[- First]] wiki-link.",
            updated_at="2024-01-03T00:00:00Z",
        ),
        _make_note(
            "33333333-3333-3333-3333-333333333333",
            "@ Third person",
            "person",
            "",
            updated_at="2024-01-04T00:00:00Z",
        ),
    ]
    with Store(settings.index_path) as store:
        for note in notes:
            ingest_note(note, store=store, vault_dir=settings.vault_dir)
    return notes


def test_local_backend_rejects_missing_vault_dir(tmp_settings: Settings) -> None:
    tmp_settings.vault_dir.rmdir()
    with pytest.raises(Exception, match="Vault directory does not exist"):
        LocalBackend(tmp_settings)


def test_list_note_summaries_returns_paginated_page(tmp_settings: Settings) -> None:
    seeded = _seed_vault(tmp_settings)

    with LocalBackend(tmp_settings) as backend:
        page = backend.list_note_summaries(limit=10, offset=0)

    assert isinstance(page, NotesPage)
    assert page.total == len(seeded)
    assert page.limit == 10
    assert page.offset == 0
    assert len(page.data) == len(seeded)

    returned_ids = {s.id for s in page.data}
    expected_ids = {note.id for note in seeded}
    assert returned_ids == expected_ids

    # Ordering is newest-first by updated_at (First has the latest timestamp).
    assert page.data[0].id == seeded[0].id
    assert page.data[0].filename == "- First"


def test_list_note_summaries_respects_limit_and_offset(tmp_settings: Settings) -> None:
    _seed_vault(tmp_settings)
    with LocalBackend(tmp_settings) as backend:
        first_page = backend.list_note_summaries(limit=2, offset=0)
        second_page = backend.list_note_summaries(limit=2, offset=2)

    assert first_page.total == 3
    assert first_page.limit == 2
    assert len(first_page.data) == 2
    assert second_page.total == 3
    assert second_page.offset == 2
    assert len(second_page.data) == 1

    first_ids = {s.id for s in first_page.data}
    second_ids = {s.id for s in second_page.data}
    assert first_ids.isdisjoint(second_ids)


def test_read_note_returns_full_note(tmp_settings: Settings) -> None:
    _seed_vault(tmp_settings)
    with LocalBackend(tmp_settings) as backend:
        note = backend.read_note("22222222-2222-2222-2222-222222222222")

    assert isinstance(note, Note)
    assert note.id == "22222222-2222-2222-2222-222222222222"
    assert note.filename == "! Second permanent"
    assert note.family == "permanent"
    assert "Second body" in note.body
    assert "[[- First]]" in note.body
    assert note.mcp_permissions == "ALL"


def test_read_note_missing_id_raises_not_found(tmp_settings: Settings) -> None:
    _seed_vault(tmp_settings)
    with LocalBackend(tmp_settings) as backend, pytest.raises(NotFoundError):
        backend.read_note("00000000-0000-0000-0000-000000000000")


def test_attachments_round_trip(tmp_settings: Settings, tmp_path) -> None:
    """Phase 7: attachments live under `<vault>/.attachments/<storage_key>`."""
    _seed_vault(tmp_settings)
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"PDFDATA")

    with LocalBackend(tmp_settings) as backend:
        upload = backend.upload_attachment(sample, content_type="application/pdf")
        assert upload.storage_key
        assert upload.size_bytes == 7

        dest = tmp_path / "roundtrip.pdf"
        download = backend.download_attachment(upload.storage_key, dest)

    assert dest.read_bytes() == b"PDFDATA"
    assert download.bytes_written == 7
    assert download.content_type == "application/pdf"
    assert download.filename == "sample.pdf"
