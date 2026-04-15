"""LocalBackend write path — create, append, update, delete, restore.

Rename-cascade tests live in `test_local_backend_rename.py` (Phase 6b).
These tests focus on the non-cascade behaviours of every mutation method.
"""

from __future__ import annotations

import pytest

from app.models import Note
from app.repositories.backend import NoteDraft, NotePatch, NoteUpdateResult
from app.repositories.errors import NotFoundError, UserError
from app.repositories.local_backend import LocalBackend
from app.repositories.store import Store
from app.services.notes import ingest_note
from app.settings import Settings


def _seed_permanent(settings: Settings, note_id: str, filename: str, body: str) -> Note:
    note = Note(
        id=note_id,
        filename=filename,
        title=filename.lstrip("- @=+!").strip(),
        family="permanent",
        kind="permanent",
        source=None,
        body=body,
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        mcp_permissions="ALL",
    )
    with Store(settings.index_path) as store:
        ingest_note(note, store=store, vault_dir=settings.vault_dir)
    return note


def test_create_note_writes_file_and_persists_row(tmp_settings: Settings) -> None:
    draft = NoteDraft(
        filename="- Fresh thought",
        body="A new fleeting note with a #tag and a [[- Target]] link.",
    )
    with LocalBackend(tmp_settings) as backend:
        new_id = backend.create_note(draft)

    assert new_id
    absolute = tmp_settings.vault_dir / "note" / "- Fresh thought.md"
    assert absolute.exists()
    content = absolute.read_text(encoding="utf-8")
    assert "family: fleeting" in content
    assert "A new fleeting note" in content

    with LocalBackend(tmp_settings) as backend:
        read = backend.read_note(new_id)
    assert read.filename == "- Fresh thought"
    assert read.family == "fleeting"
    assert "tag" in read.tags
    assert any(link.target_title == "- Target" for link in read.wikilinks)


def test_create_note_rejects_duplicate_filename(tmp_settings: Settings) -> None:
    _seed_permanent(
        tmp_settings,
        "11111111-1111-1111-1111-111111111111",
        "! Seed",
        "Seed body.",
    )
    with LocalBackend(tmp_settings) as backend, pytest.raises(UserError, match="already exists"):
        backend.create_note(NoteDraft(filename="! Seed", body="clash"))


def test_append_to_note_extends_body(tmp_settings: Settings) -> None:
    seed = _seed_permanent(
        tmp_settings,
        "22222222-2222-2222-2222-222222222222",
        "! Seed",
        "first line",
    )
    with LocalBackend(tmp_settings) as backend:
        backend.append_to_note(seed.id, "second line")
        refreshed = backend.read_note(seed.id)

    assert "first line" in refreshed.body
    assert "second line" in refreshed.body
    assert refreshed.body.index("first line") < refreshed.body.index("second line")


def test_update_note_body_only(tmp_settings: Settings) -> None:
    seed = _seed_permanent(
        tmp_settings,
        "33333333-3333-3333-3333-333333333333",
        "! Seed",
        "old body",
    )
    with LocalBackend(tmp_settings) as backend:
        result = backend.update_note(seed.id, NotePatch(body="new body"))
        refreshed = backend.read_note(seed.id)

    assert isinstance(result, NoteUpdateResult)
    assert result.affected_notes == ()
    assert "new body" in refreshed.body
    assert "old body" not in refreshed.body


def test_update_note_rename_raises_until_phase_6b(tmp_settings: Settings) -> None:
    seed = _seed_permanent(
        tmp_settings,
        "44444444-4444-4444-4444-444444444444",
        "! Seed",
        "body",
    )
    with LocalBackend(tmp_settings) as backend, pytest.raises(NotImplementedError):
        backend.update_note(seed.id, NotePatch(filename="! Renamed"))


def test_delete_note_moves_file_to_trash(tmp_settings: Settings) -> None:
    seed = _seed_permanent(
        tmp_settings,
        "55555555-5555-5555-5555-555555555555",
        "! Doomed",
        "going away soon",
    )
    mirror = tmp_settings.vault_dir / "note" / "! Doomed.md"
    assert mirror.exists()

    with LocalBackend(tmp_settings) as backend:
        backend.delete_note(seed.id)

    assert not mirror.exists()
    trash_file = tmp_settings.vault_dir / ".trash" / "note" / "! Doomed.md"
    assert trash_file.exists()

    with LocalBackend(tmp_settings) as backend, pytest.raises(NotFoundError):
        backend.read_note(seed.id)


def test_delete_then_restore_round_trip(tmp_settings: Settings) -> None:
    seed = _seed_permanent(
        tmp_settings,
        "66666666-6666-6666-6666-666666666666",
        "! Recoverable",
        "keepsake",
    )
    with LocalBackend(tmp_settings) as backend:
        backend.delete_note(seed.id)
        backend.restore_note(seed.id)
        refreshed = backend.read_note(seed.id)

    assert refreshed.id == seed.id
    assert "keepsake" in refreshed.body
    mirror = tmp_settings.vault_dir / "note" / "! Recoverable.md"
    assert mirror.exists()
    assert not (tmp_settings.vault_dir / ".trash" / "note" / "! Recoverable.md").exists()


def test_restore_raises_when_filename_collides(tmp_settings: Settings) -> None:
    seed = _seed_permanent(
        tmp_settings,
        "77777777-7777-7777-7777-777777777777",
        "! Colliding",
        "first",
    )
    with LocalBackend(tmp_settings) as backend:
        backend.delete_note(seed.id)
        # Create a second note under the same name while the first is in trash.
        backend.create_note(NoteDraft(filename="! Colliding", body="second"))
        with pytest.raises(UserError, match="already exists"):
            backend.restore_note(seed.id)


def test_trash_files_do_not_surface_in_list(tmp_settings: Settings) -> None:
    seed = _seed_permanent(
        tmp_settings,
        "88888888-8888-8888-8888-888888888888",
        "! Soon gone",
        "x",
    )
    with LocalBackend(tmp_settings) as backend:
        backend.delete_note(seed.id)
        page = backend.list_note_summaries(limit=50, offset=0)

    assert all(summary.id != seed.id for summary in page.data)
