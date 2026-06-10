"""Restore permission pre-check — restore requires WRITE.

Mirrors the edit/delete/append local pre-check: when the trashed row's
permission level is below WRITE, `restore_note_remote` fast-fails with
`PermissionError` unless `--force` is passed (the server stays the final
authority).
"""

from __future__ import annotations

import pytest

from knoten.models import Note
from knoten.repositories.errors import PermissionError as LocalPermissionError
from knoten.repositories.local_backend import LocalBackend
from knoten.repositories.store import Store
from knoten.services.notes import ingest_note, restore_note_remote
from knoten.settings import Settings

NOTE_ID = "55555555-6666-7777-8888-999999999999"


def _seed_and_trash(settings: Settings, permissions: str) -> None:
    note = Note(
        id=NOTE_ID,
        filename="! Restricted",
        title="Restricted",
        family="permanent",
        kind="permanent",
        source=None,
        body="body",
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        permissions=permissions,
    )
    with Store(settings.paths.index_path) as store:
        ingest_note(note, store=store, vault_dir=settings.paths.vault_dir)
    with LocalBackend(settings) as backend:
        backend.delete_note(NOTE_ID)


def test_restore_blocked_when_below_write(tmp_settings: Settings) -> None:
    _seed_and_trash(tmp_settings, "READ")
    with Store(tmp_settings.paths.index_path) as store, LocalBackend(tmp_settings) as backend:
        with pytest.raises(LocalPermissionError) as exc_info:
            restore_note_remote(
                backend=backend,
                store=store,
                vault_dir=tmp_settings.paths.vault_dir,
                note_id=NOTE_ID,
            )
    assert exc_info.value.required_level == "WRITE"
    assert exc_info.value.operation == "restore"


def test_restore_force_bypasses_precheck(tmp_settings: Settings) -> None:
    _seed_and_trash(tmp_settings, "READ")
    with Store(tmp_settings.paths.index_path) as store, LocalBackend(tmp_settings) as backend:
        note = restore_note_remote(
            backend=backend,
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
            note_id=NOTE_ID,
            force=True,
        )
    assert note.id == NOTE_ID


def test_restore_allowed_when_write_or_above(tmp_settings: Settings) -> None:
    _seed_and_trash(tmp_settings, "ALL")
    with Store(tmp_settings.paths.index_path) as store, LocalBackend(tmp_settings) as backend:
        note = restore_note_remote(
            backend=backend,
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
            note_id=NOTE_ID,
        )
    assert note.id == NOTE_ID
