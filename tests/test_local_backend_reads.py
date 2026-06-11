"""LocalBackend read path — `list_note_summaries` + `read_note`.

Populates a vault and its Store via `ingest_note` (the same pipeline the
real CLI uses), then drives `LocalBackend` against it. No network, no
mocks — purely filesystem and SQLite.
"""

from __future__ import annotations

import pytest

from knoten.models import Note
from knoten.repositories.backend import NotesPage
from knoten.repositories.errors import NotFoundError
from knoten.repositories.local_backend import LocalBackend
from knoten.repositories.store import Store
from knoten.services.notes import ingest_note
from knoten.settings import Settings


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
        permissions="ALL",
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
    with Store(settings.paths.index_path) as store:
        for note in notes:
            ingest_note(note, store=store, vault_dir=settings.paths.vault_dir)
    return notes


def test_local_backend_rejects_missing_vault_dir(tmp_settings: Settings) -> None:
    tmp_settings.paths.vault_dir.rmdir()
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
    assert note.permissions == "ALL"


def test_read_note_missing_id_raises_not_found(tmp_settings: Settings) -> None:
    _seed_vault(tmp_settings)
    with LocalBackend(tmp_settings) as backend, pytest.raises(NotFoundError):
        backend.read_note("00000000-0000-0000-0000-000000000000")


def test_read_notes_returns_batch_with_missing_ids_in_failed(tmp_settings: Settings) -> None:
    """`read_notes` loops `read_note`; missing ids land in `failed` instead
    of raising — contract symmetry with the remote batch-read endpoint."""
    _seed_vault(tmp_settings)
    with LocalBackend(tmp_settings) as backend:
        result = backend.read_notes(
            [
                "11111111-1111-1111-1111-111111111111",
                "00000000-0000-0000-0000-000000000000",
                "22222222-2222-2222-2222-222222222222",
            ]
        )

    assert [note.id for note in result.notes] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert result.failed == ("00000000-0000-0000-0000-000000000000",)
    assert "First body" in result.notes[0].body


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


def test_download_unknown_storage_key_raises_terse_not_found(
    tmp_settings: Settings, tmp_path
) -> None:
    """No referencing note → keep the terse `No attachment with storage_key …` message."""
    _seed_vault(tmp_settings)
    with LocalBackend(tmp_settings) as backend:
        with pytest.raises(NotFoundError, match=r"No attachment with storage_key"):
            backend.download_attachment("missing.jpg", tmp_path / "out.jpg")


def test_download_referenced_storage_key_hints_at_remote_mode(
    tmp_settings: Settings, tmp_path
) -> None:
    """A file-family note pointing at a missing key → enriched error pointing at remote mode."""
    file_note = Note(
        id="44444444-4444-4444-4444-444444444444",
        filename="2024-05-08+ inbox photo.jpg",
        title="inbox photo.jpg",
        family="file",
        kind="file",
        source="2024-05-08",
        body="",
        frontmatter={"attachment": "ghostkey.jpg"},
        tags=(),
        wikilinks=(),
        created_at="2024-05-08T00:00:00Z",
        updated_at="2024-05-08T00:00:00Z",
        permissions="ALL",
    )
    with Store(tmp_settings.paths.index_path) as store:
        ingest_note(file_note, store=store, vault_dir=tmp_settings.paths.vault_dir)

    with LocalBackend(tmp_settings) as backend:
        with pytest.raises(NotFoundError) as excinfo:
            backend.download_attachment("ghostkey.jpg", tmp_path / "out.jpg")

    message = str(excinfo.value)
    assert "ghostkey.jpg" in message
    assert "2024-05-08+ inbox photo.jpg" in message
    assert "KNOTEN_API_URL" in message
