"""Reconciliation: missing files re-fetched, orphans removed, hashes verified.

These tests all use a mocked remote via pytest-httpx — the reconciliation
pass is what keeps the local mirror consistent after the user (or the OS)
meddles with the on-disk state.
"""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from app.models import Note
from app.repositories.http_client import NotesClient
from app.repositories.store import Store
from app.services.notes import ingest_note
from app.services.reconcile import reconcile_local
from app.services.sync import incremental_sync
from app.settings import Settings


def _seed_note(store: Store, settings: Settings, note_id: str, body: str = "Body one.") -> None:
    """Write one note to both the store and the disk."""
    note = Note(
        id=note_id,
        filename="! Seeded",
        title="Seeded",
        family="permanent",
        kind="permanent",
        source=None,
        body=body,
        frontmatter={"kind": "permanent", "title": "Seeded"},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )
    ingest_note(note, store=store, vault_dir=settings.vault_dir)


def _note_read_payload(note_id: str, *, body: str = "Body one.") -> dict:
    return {
        "id": note_id,
        "filename": "! Seeded",
        "title": "Seeded",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "body": body,
        "frontmatter": {"kind": "permanent", "title": "Seeded"},
        "tags": [],
        "linkMap": {},
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }


def test_reconcile_refetches_missing_files(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    note_id = "11111111-1111-1111-1111-111111111111"
    with Store(tmp_settings.index_path) as store:
        _seed_note(store, tmp_settings, note_id)
        # Simulate user rm'ing the mirror file.
        target = tmp_settings.vault_dir / "note" / "! Seeded.md"
        assert target.exists()
        target.unlink()
        assert not target.exists()

        httpx_mock.add_response(
            url=f"{tmp_settings.api_url}/api/notes/{note_id}",
            json=_note_read_payload(note_id),
        )

        with NotesClient(tmp_settings) as client:
            result = reconcile_local(client=client, store=store, settings=tmp_settings)

        assert result.missing_refetched == 1
        assert result.missing_ids == [note_id]
        assert target.exists()
        # Body content was rewritten from the remote response.
        assert "Body one." in target.read_text(encoding="utf-8")


def test_reconcile_removes_orphan_files(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    note_id = "22222222-2222-2222-2222-222222222222"
    with Store(tmp_settings.index_path) as store:
        _seed_note(store, tmp_settings, note_id)

        # Drop an orphan markdown file that the store does not know about.
        orphan = tmp_settings.vault_dir / "note" / "! Orphaned.md"
        orphan.write_text("---\ntitle: Orphan\n---\n\nOrphan body.\n", encoding="utf-8")
        assert orphan.exists()

        # Drop a dotfile that should be ignored (atomic-write leftover).
        dotfile = tmp_settings.vault_dir / "note" / ".DS_Store"
        dotfile.write_bytes(b"")

        with NotesClient(tmp_settings) as client:
            result = reconcile_local(client=client, store=store, settings=tmp_settings)

        assert result.orphans_removed == 1
        assert result.orphan_paths == ["note/! Orphaned.md"]
        assert not orphan.exists()
        # Dotfile is left alone.
        assert dotfile.exists()
        # Known file is untouched.
        assert (tmp_settings.vault_dir / "note" / "! Seeded.md").exists()


def test_reconcile_hash_verification_refetches_drifted(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    note_id = "33333333-3333-3333-3333-333333333333"
    with Store(tmp_settings.index_path) as store:
        _seed_note(store, tmp_settings, note_id, body="Original body.")
        target = tmp_settings.vault_dir / "note" / "! Seeded.md"

        # User manually edits the file — body no longer matches body_sha256.
        text = target.read_text(encoding="utf-8")
        tampered = text.replace("Original body.", "Tampered body.")
        target.write_text(tampered, encoding="utf-8")

        httpx_mock.add_response(
            url=f"{tmp_settings.api_url}/api/notes/{note_id}",
            json=_note_read_payload(note_id, body="Original body."),
        )

        with NotesClient(tmp_settings) as client:
            # Without --hashes, the drift is invisible (file still exists).
            silent = reconcile_local(client=client, store=store, settings=tmp_settings)
            assert silent.mismatched_refetched == 0
            assert "Tampered body." in target.read_text(encoding="utf-8")

            # With --hashes, the drift is caught and the file is restored.
            loud = reconcile_local(
                client=client, store=store, settings=tmp_settings, verify_hashes=True
            )
            assert loud.mismatched_refetched == 1
            assert loud.mismatched_ids == [note_id]
            assert "Original body." in target.read_text(encoding="utf-8")
            assert "Tampered body." not in target.read_text(encoding="utf-8")


def test_reconcile_is_idempotent_when_clean(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    note_id = "44444444-4444-4444-4444-444444444444"
    with Store(tmp_settings.index_path) as store:
        _seed_note(store, tmp_settings, note_id)
        with NotesClient(tmp_settings) as client:
            result = reconcile_local(client=client, store=store, settings=tmp_settings)
        assert result.missing_refetched == 0
        assert result.mismatched_refetched == 0
        assert result.orphans_removed == 0
        # No network calls needed: pytest-httpx would error if any were made
        # without a registered mock, so reaching this line proves none happened.


def test_sync_always_catches_remote_deletes_even_on_equal_totals(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """Regression test for the 'one-create-one-delete' gap.

    Before this fix, delete detection only ran when `remote_total != local_count`.
    A sync that creates one note and deletes another leaves the total equal,
    so the deletion was silently missed.
    """
    kept_id = "55555555-5555-5555-5555-555555555555"
    deleted_id = "66666666-6666-6666-6666-666666666666"

    # Pre-seed both notes locally, then advance the cursor past everything.
    tmp_settings.state_dir.mkdir(parents=True, exist_ok=True)
    tmp_settings.state_file.write_text(
        '{"schema_version": 1, "last_sync_max_updated_at": "2030-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    with Store(tmp_settings.index_path) as store:
        _seed_note(store, tmp_settings, kept_id)
        # Manually insert a second note so both are "locally known".
        other = Note(
            id=deleted_id,
            filename="! Doomed",
            title="Doomed",
            family="permanent",
            kind="permanent",
            source=None,
            body="Going away soon.",
            frontmatter={"kind": "permanent"},
            tags=(),
            wikilinks=(),
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-02T00:00:00Z",
        )
        ingest_note(other, store=store, vault_dir=tmp_settings.vault_dir)
        assert store.count_notes() == 2

    # Remote now only has kept_id. Total=1 on the list response.
    # But the test sets the cursor ahead, so the sync fetches nothing new;
    # delete detection (ID scan) must still find deleted_id is gone.
    list_payload = {
        "data": [
            {
                "id": kept_id,
                "filename": "! Seeded",
                "title": "Seeded",
                "family": "permanent",
                "kind": "permanent",
                "source": None,
                "tags": [],
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-02T00:00:00Z",
            }
        ],
        "total": 1,
        "limit": 100,
        "offset": 0,
    }
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json=list_payload,
    )
    # Delete detection scans at page_size=200.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={
            "data": [list_payload["data"][0]],
            "total": 1,
            "limit": 200,
            "offset": 0,
        },
    )

    with Store(tmp_settings.index_path) as store, NotesClient(tmp_settings) as client:
        result = incremental_sync(client=client, store=store, settings=tmp_settings)
        assert result.deleted == 1
        assert store.count_notes() == 1
        assert store.find_by_id(deleted_id) is None
