"""Incremental sync end-to-end with a mocked remote."""

from __future__ import annotations

import json

from pytest_httpx import HTTPXMock

from knoten.models import Note
from knoten.repositories.remote_backend import RemoteBackend
from knoten.repositories.store import Store
from knoten.repositories.sync_state import load_state
from knoten.services.notes import ingest_note
from knoten.services.sync import incremental_sync
from knoten.settings import Settings


def _list_payload(items: list[dict], total: int) -> dict:
    return {"data": items, "total": total, "limit": 100, "offset": 0}


def test_incremental_sync_fetches_new_notes(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    list_item = {
        "id": "11111111-1111-1111-1111-111111111111",
        "filename": "! First",
        "title": "First",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "tags": [],
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json=_list_payload([list_item], total=1),
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/11111111-1111-1111-1111-111111111111",
        json={
            "id": "11111111-1111-1111-1111-111111111111",
            "filename": "! First",
            "title": "First",
            "family": "permanent",
            "kind": "permanent",
            "source": None,
            "body": "Body of first. [[Second]]",
            "frontmatter": {"kind": "permanent", "title": "First"},
            "tags": [],
            "linkMap": {"Second": None},
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-02T00:00:00Z",
        },
    )
    # Delete detection scans at page_size=200 — always runs now.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [list_item], "total": 1, "limit": 200, "offset": 0},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.fetched == 1
        assert result.deleted == 0
        assert result.local_total == 1
        assert result.missing_refetched == 0
        assert result.orphans_removed == 0

    state = load_state(tmp_settings.paths.state_file)
    assert state.last_sync_max_updated_at == "2024-01-02T00:00:00Z"

    written = tmp_settings.paths.vault_dir / "note" / "! First.md"
    assert written.exists()
    content = written.read_text(encoding="utf-8")
    assert "Body of first" in content


def test_incremental_sync_skips_stale_items(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    # Pre-seed state with a cursor past the only note's updatedAt, and
    # pre-seed the local store + disk with a matching row. The cursor
    # ensures nothing is re-fetched from the list response, and the disk
    # state is clean so reconciliation is a no-op.
    tmp_settings.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_settings.paths.state_file.write_text(
        '{"schema_version": 1, "last_sync_max_updated_at": "2030-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    from knoten.models import Note
    from knoten.services.notes import ingest_note

    with Store(tmp_settings.paths.index_path) as store:
        pre = Note(
            id="22222222-2222-2222-2222-222222222222",
            filename="! Old",
            title="Old",
            family="permanent",
            kind="permanent",
            source=None,
            body="old body",
            frontmatter={"kind": "permanent"},
            tags=(),
            wikilinks=(),
            created_at="2020-01-01T00:00:00Z",
            updated_at="2020-01-02T00:00:00Z",
        )
        ingest_note(pre, store=store, vault_dir=tmp_settings.paths.vault_dir)

    list_item = {
        "id": "22222222-2222-2222-2222-222222222222",
        "filename": "! Old",
        "title": "Old",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "tags": [],
        "createdAt": "2020-01-01T00:00:00Z",
        "updatedAt": "2020-01-02T00:00:00Z",
    }
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json=_list_payload([list_item], total=1),
    )
    # Delete detection always runs now — needs a mock too.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [list_item], "total": 1, "limit": 200, "offset": 0},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.fetched == 0
        assert result.deleted == 0
        assert result.missing_refetched == 0


def test_sync_pushes_local_only_notes_upstream(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """Notes with `synced=0` get POSTed to the remote during sync.

    Reproduces the 2026-05-08 incident: local-mode writes that previously
    got reconciled away on the next sync now ride upstream instead.
    """
    local_id = "33333333-3333-3333-3333-333333333333"
    server_id = "44444444-4444-4444-4444-444444444444"

    # Seed a local-only note marked synced=0 (the state LocalBackend writes
    # leave behind for any note created while api_url was empty).
    with Store(tmp_settings.paths.index_path) as store:
        local_note = Note(
            id=local_id,
            filename="- Local pending",
            title="Local pending",
            family="fleeting",
            kind="fleeting",
            source=None,
            body="written offline",
            frontmatter={"kind": "fleeting"},
            tags=(),
            wikilinks=(),
            created_at="2026-05-08T10:00:00Z",
            updated_at="2026-05-08T10:00:00Z",
        )
        ingest_note(
            local_note,
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
            synced=False,
        )

    # Remote: empty list (no remote notes yet).
    empty_list = {"data": [], "total": 0, "limit": 100, "offset": 0}
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json=empty_list,
    )
    # Iter-all pagination for delete detection.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [], "total": 0, "limit": 200, "offset": 0},
    )
    # The push pass POSTs the note to /api/notes; server returns its own UUID.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        json={
            "id": server_id,
            "filename": "- Local pending",
            "title": "Local pending",
            "family": "fleeting",
            "kind": "fleeting",
            "source": None,
            "tags": [],
            "createdAt": "2026-05-08T10:00:00Z",
            "updatedAt": "2026-05-08T10:00:00Z",
        },
        status_code=201,
    )
    # After the push, knoten re-fetches the server-normalised note and ingests
    # it (synced=1). The server inserts a fleeting filename prefix on create —
    # the refetch is what lands that normalisation in the mirror.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{server_id}",
        method="GET",
        json=_read_payload(
            server_id,
            "- 2026-05-08 1000 Local pending",
            "written offline",
            updated_at="2026-05-08T10:00:05Z",
        ),
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.pushed_creates == 1
        assert result.pushed_edits == 0
        assert result.push_failed == 0
        # The local row's id was swapped to the server-issued one.
        assert store.find_by_id(server_id) is not None
        assert store.find_by_id(local_id) is None
        # And it is now synced, carrying the server-normalised filename.
        row = store.find_by_id(server_id)
        assert int(row["synced"]) == 1
        assert row["filename"] == "- 2026-05-08 1000 Local pending"

    # The mirror file was relocated to the server-normalised name; the old
    # local-only path no longer exists.
    assert (tmp_settings.paths.vault_dir / "note" / "- 2026-05-08 1000 Local pending.md").exists()
    assert not (tmp_settings.paths.vault_dir / "note" / "- Local pending.md").exists()


def test_sync_preserves_local_only_when_push_fails(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """A push-pass failure leaves the local note intact (synced=0) for retry."""
    local_id = "55555555-5555-5555-5555-555555555555"

    with Store(tmp_settings.paths.index_path) as store:
        ingest_note(
            Note(
                id=local_id,
                filename="- Stuck pending",
                title="Stuck",
                family="fleeting",
                kind="fleeting",
                source=None,
                body="cannot be pushed",
                frontmatter={"kind": "fleeting"},
                tags=(),
                wikilinks=(),
                created_at="2026-05-08T10:00:00Z",
                updated_at="2026-05-08T10:00:00Z",
            ),
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
            synced=False,
        )

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [], "total": 0, "limit": 200, "offset": 0},
    )
    # The POST fails — server returns 500. The push pass should mark the
    # failure on the result and leave the local row alone.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=500,
        json={"error": "boom"},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.pushed_creates == 0
        assert result.push_failed == 1
        assert result.deleted == 0  # crucially, the local row was NOT reconciled away
        row = store.find_by_id(local_id)
        assert row is not None
        assert int(row["synced"]) == 0


def _seed_synced(
    store: Store,
    settings: Settings,
    note_id: str,
    filename: str,
    *,
    body: str = "synced body",
    synced: bool = True,
) -> None:
    ingest_note(
        Note(
            id=note_id,
            filename=filename,
            title=filename.lstrip("!- ").strip(),
            family="permanent",
            kind="permanent",
            source=None,
            body=body,
            frontmatter={"kind": "permanent"},
            tags=(),
            wikilinks=(),
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-02T00:00:00Z",
        ),
        store=store,
        vault_dir=settings.paths.vault_dir,
        synced=synced,
    )


def _summary_item(note_id: str, filename: str, updated_at: str = "2024-01-02T00:00:00Z") -> dict:
    return {
        "id": note_id,
        "filename": filename,
        "title": filename.lstrip("!- ").strip(),
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "tags": [],
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": updated_at,
    }


def _read_payload(
    note_id: str, filename: str, body: str, updated_at: str = "2024-01-02T00:00:00Z"
) -> dict:
    return {
        "id": note_id,
        "filename": filename,
        "title": filename.lstrip("!- ").strip(),
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "body": body,
        "frontmatter": {"kind": "permanent"},
        "tags": [],
        "linkMap": {},
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": updated_at,
    }


def test_full_sync_preserves_offline_edit_and_pushes_it(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """`sync --full` must not clobber a `synced=0` row with the remote body (C3).

    The pull pass runs before the push pass; before the fix it re-ingested
    the unchanged remote body over the offline edit and flipped `synced`
    to 1, so the push pass never saw the edit. Now the pull skips the row
    (surfacing a conflict) and the push uploads the local version.
    """
    from knoten.services.sync import full_sync

    note_id = "11111111-2222-3333-4444-555555555555"
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(
            store,
            tmp_settings,
            note_id,
            "! Conflicted",
            body="OFFLINE EDIT body",
            synced=False,
        )

    item = _summary_item(note_id, "! Conflicted")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [item], "total": 1, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [item], "total": 1, "limit": 200, "offset": 0},
    )
    # The pull pass must NOT GET this note (it is a synced=0 conflict). The
    # only GET is the push pass's post-PUT refetch, which echoes the body the
    # server now stores (what we just PUT).
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="PUT",
        json={"id": note_id},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="GET",
        json=_read_payload(note_id, "! Conflicted", "OFFLINE EDIT body"),
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = full_sync(backend=backend, store=store, settings=tmp_settings)

        assert result.pushed_edits == 1
        assert result.fetched == 0
        assert [c["reason"] for c in result.conflicts] == ["local_unsynced_edit"]
        row = store.find_by_id(note_id)
        assert row is not None
        assert int(row["synced"]) == 1  # pushed, not clobbered

    mirror = tmp_settings.paths.vault_dir / "note" / "! Conflicted.md"
    assert "OFFLINE EDIT body" in mirror.read_text(encoding="utf-8")
    put_requests = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    assert len(put_requests) == 1
    assert b"OFFLINE EDIT body" in put_requests[0].content


def test_true_conflict_keeps_local_version_and_surfaces_it(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """Remote AND local both edited: local wins, conflict lands in the result (C3)."""
    note_id = "22222222-3333-4444-5555-666666666666"
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(
            store,
            tmp_settings,
            note_id,
            "! Diverged",
            body="LOCAL version",
            synced=False,
        )

    item = _summary_item(note_id, "! Diverged", updated_at="2026-06-09T12:00:00Z")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [item], "total": 1, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [item], "total": 1, "limit": 200, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="PUT",
        json={"id": note_id},
    )
    # Post-push refetch: the server now stores the local version we PUT.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="GET",
        json=_read_payload(note_id, "! Diverged", "LOCAL version"),
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.conflicts == [
            {"id": note_id, "filename": "! Diverged", "reason": "local_unsynced_edit"}
        ]
        assert result.warnings  # surfaced in the JSON payload, not just logs

    mirror = tmp_settings.paths.vault_dir / "note" / "! Diverged.md"
    assert "LOCAL version" in mirror.read_text(encoding="utf-8")


def test_sync_skips_delete_phase_when_scan_disagrees_with_total(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """The remote-total tripwire must gate the delete phase, not just log (M3)."""
    kept_id = "33333333-4444-5555-6666-777777777777"
    missed_id = "44444444-5555-6666-7777-888888888888"
    tmp_settings.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_settings.paths.state_file.write_text(
        '{"schema_version": 1, "last_sync_max_updated_at": "2030-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(store, tmp_settings, kept_id, "! Kept")
        _seed_synced(store, tmp_settings, missed_id, "! Missed by unstable scan")

    # The server claims total=2 but the scan only ever yields one row —
    # an unstable offset-paginated walk. `missed_id` must NOT be deleted.
    item = _summary_item(kept_id, "! Kept")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [item], "total": 2, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [item], "total": 2, "limit": 200, "offset": 0},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.deleted == 0
        assert store.find_by_id(missed_id) is not None
        assert any("delete detection skipped" in warning for warning in result.warnings)


def test_sync_mass_delete_circuit_breaker_blocks_wipe(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """An empty remote must not wipe the local mirror without --force-delete (M3)."""
    ids = [f"aaaaaaa{i}-0000-0000-0000-00000000000{i}" for i in range(8)]
    with Store(tmp_settings.paths.index_path) as store:
        for i, note_id in enumerate(ids):
            _seed_synced(store, tmp_settings, note_id, f"! Note {i}")

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [], "total": 0, "limit": 200, "offset": 0},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.deleted == 0
        assert store.count_notes() == 8
        assert any("--force-delete" in warning for warning in result.warnings)


def test_sync_mass_delete_applies_with_force_delete(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """--force-delete overrides the circuit breaker (M3)."""
    ids = [f"bbbbbbb{i}-0000-0000-0000-00000000000{i}" for i in range(8)]
    with Store(tmp_settings.paths.index_path) as store:
        for i, note_id in enumerate(ids):
            _seed_synced(store, tmp_settings, note_id, f"! Note {i}")

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [], "total": 0, "limit": 200, "offset": 0},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(
            backend=backend, store=store, settings=tmp_settings, force_delete=True
        )
        assert result.deleted == 8
        assert store.count_notes() == 0


def test_sync_filename_collision_surfaces_conflict_and_continues(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """A remote note colliding with a local filename must not wedge the sync (M4)."""
    local_id = "55555555-6666-7777-8888-999999999999"
    remote_id = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    tmp_settings.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_settings.paths.state_file.write_text(
        '{"schema_version": 1, "last_sync_max_updated_at": "2030-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(store, tmp_settings, local_id, "! Meeting notes")

    local_item = _summary_item(local_id, "! Meeting notes")
    remote_item = _summary_item(remote_id, "! Meeting notes")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [local_item], "total": 2, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [local_item, remote_item], "total": 2, "limit": 200, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{remote_id}",
        json=_read_payload(remote_id, "! Meeting notes", "remote twin body"),
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.conflicts == [
            {"id": remote_id, "filename": "! Meeting notes", "reason": "filename_collision"}
        ]
        # The sync completed: no store row committed for the twin, the
        # original local note is untouched, nothing got deleted.
        assert store.find_by_id(remote_id) is None
        assert store.find_by_id(local_id) is not None
        assert result.deleted == 0


def test_local_soft_delete_propagates_to_remote(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """Deleting a synced note in local mode issues DELETE on the next sync (M5).

    Before the fix `trashed_notes` was never consulted by sync, so the
    remote copy survived and the catch-up scan resurrected the note.
    """
    from knoten.repositories.local_backend import LocalBackend

    note_id = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(store, tmp_settings, note_id, "! Old idea")

    with LocalBackend(tmp_settings) as backend:
        backend.delete_note(note_id)

    with Store(tmp_settings.paths.index_path) as store:
        trashed = store.find_trashed(note_id)
        assert trashed is not None
        assert int(trashed["pending_remote_delete"]) == 1

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="DELETE",
        status_code=204,
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [], "total": 0, "limit": 200, "offset": 0},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.pushed_deletes == 1
        # The note did not resurrect: no active row, no mirror file.
        assert store.find_by_id(note_id) is None
        trashed = store.find_trashed(note_id)
        assert trashed is not None
        assert int(trashed["pending_remote_delete"]) == 0

    assert not (tmp_settings.paths.vault_dir / "note" / "! Old idea.md").exists()
    delete_requests = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
    assert len(delete_requests) == 1
    assert delete_requests[0].url.path == f"/api/notes/{note_id}"


def test_sync_refetches_boundary_same_second_edit(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """An item whose updatedAt equals the cursor is re-fetched (minor fix).

    Timestamps are second-precision; the old strict `>` skipped a note
    edited in the same second as the recorded cursor forever.
    """
    note_id = "88888888-9999-aaaa-bbbb-cccccccccccc"
    tmp_settings.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_settings.paths.state_file.write_text(
        '{"schema_version": 1, "last_sync_max_updated_at": "2024-01-02T00:00:00Z"}',
        encoding="utf-8",
    )
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(store, tmp_settings, note_id, "! Same second", body="stale body")

    item = _summary_item(note_id, "! Same second", updated_at="2024-01-02T00:00:00Z")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [item], "total": 1, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [item], "total": 1, "limit": 200, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        json=_read_payload(note_id, "! Same second", "same-second edit body"),
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.fetched == 1

    mirror = tmp_settings.paths.vault_dir / "note" / "! Same second.md"
    assert "same-second edit body" in mirror.read_text(encoding="utf-8")


def test_sync_skips_hostile_server_filenames(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    """A server filename with path separators is skipped, never ingested (minor fix).

    The old code passed it down to the file writer, which raised and
    aborted the whole sync — after the store row was already committed.
    """
    hostile_id = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    item = _summary_item(hostile_id, "../../escape attempt")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [item], "total": 1, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [item], "total": 1, "limit": 200, "offset": 0},
    )
    # No GET /api/notes/{id} registered — validation must skip before the read.

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.skipped_invalid >= 1
        assert any("path separator" in warning for warning in result.warnings)
        assert store.find_by_id(hostile_id) is None
        assert store.count_notes() == 0


def test_same_scan_consistent_helper() -> None:
    """The same-scan decision: agree, single-page disagree, mid-walk shift."""
    from knoten.services.sync import _same_scan_consistent

    # Agree — stable total equal to the scanned count.
    assert _same_scan_consistent(3, [3, 3]) is True
    assert _same_scan_consistent(0, [0]) is True
    # Single-page disagree — the walk's own total ≠ scanned count.
    assert _same_scan_consistent(1, [2]) is False
    # Mid-walk total change — a concurrent create bumped the total between pages.
    assert _same_scan_consistent(200, [200, 201]) is False
    # No pages reported — cannot judge, treat as inconsistent (no deletes).
    assert _same_scan_consistent(0, []) is False


def test_sync_runs_delete_phase_when_same_scan_agrees(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """When the reconcile walk's own total matches its scanned count, the
    delete phase runs and a note truly absent from the remote is purged."""
    kept_id = "33333333-4444-5555-6666-777777777777"
    gone_id = "44444444-5555-6666-7777-888888888888"
    tmp_settings.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_settings.paths.state_file.write_text(
        '{"schema_version": 1, "last_sync_max_updated_at": "2030-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(store, tmp_settings, kept_id, "! Kept")
        _seed_synced(store, tmp_settings, gone_id, "! Gone on remote")

    # Walk yields one row and its OWN total agrees (1 == 1) — consistent scan.
    item = _summary_item(kept_id, "! Kept")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [item], "total": 1, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [item], "total": 1, "limit": 200, "offset": 0},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.deleted == 1
        assert store.find_by_id(gone_id) is None
        assert store.find_by_id(kept_id) is not None


# ---- item 2: post-push refetch lands server normalisation ----------------


def test_pushed_edit_mirror_gets_server_normalised_frontmatter(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """After pushing an offline edit, the post-push refetch lands the server's
    injected frontmatter + bumped updatedAt in the mirror (item 2)."""
    note_id = "abababab-cdcd-efef-0101-232345456767"
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(store, tmp_settings, note_id, "! Edited", body="local edit", synced=False)

    item = _summary_item(note_id, "! Edited")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [item], "total": 1, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [item], "total": 1, "limit": 200, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="PUT",
        json={"id": note_id},
    )
    # Server injects a frontmatter field + bumps updatedAt on the write.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="GET",
        json={
            "id": note_id,
            "filename": "! Edited",
            "title": "Edited",
            "family": "permanent",
            "kind": "permanent",
            "source": None,
            "body": "local edit",
            "frontmatter": {"kind": "permanent", "server-field": "injected"},
            "tags": [],
            "linkMap": {},
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-02T09:00:00Z",
        },
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.pushed_edits == 1
        row = store.find_by_id(note_id)
        assert int(row["synced"]) == 1
        assert json.loads(row["frontmatter_json"]).get("server-field") == "injected"
        assert row["updated_at"] == "2024-01-02T09:00:00Z"

    mirror = tmp_settings.paths.vault_dir / "note" / "! Edited.md"
    assert "injected" in mirror.read_text(encoding="utf-8")


def test_pushed_edit_forbidden_refetch_falls_back_to_mark_synced(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """A post-push refetch that 404s (token lost READ) degrades to the bare
    mark_synced with a warning — it never fails the push pass (item 2)."""
    note_id = "fefefefe-1111-2222-3333-444455556666"
    with Store(tmp_settings.paths.index_path) as store:
        _seed_synced(store, tmp_settings, note_id, "! Locked", body="local edit", synced=False)

    item = _summary_item(note_id, "! Locked")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [item], "total": 1, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [item], "total": 1, "limit": 200, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="PUT",
        json={"id": note_id},
    )
    # The refetch is forbidden (404 → NoteForbiddenError).
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        method="GET",
        status_code=404,
        json={"error": "NOT_FOUND"},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.pushed_edits == 1
        assert result.push_failed == 0
        row = store.find_by_id(note_id)
        assert row is not None
        assert int(row["synced"]) == 1  # marked synced despite the refetch failure
        assert any("forbidden" in warning for warning in result.warnings)


# ---- item 3: poison-row escape hatch for permanently rejected pushes ------


def test_push_rejection_is_marked_surfaced_and_preserved(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """A create rejected with a permanent 4xx (duplicate filename) records a
    marker, surfaces in push_rejected + warnings, and is NOT reconciled away."""
    local_id = "12121212-3434-5656-7878-909090909090"
    with Store(tmp_settings.paths.index_path) as store:
        ingest_note(
            Note(
                id=local_id,
                filename="- Dupe",
                title="Dupe",
                family="fleeting",
                kind="fleeting",
                source=None,
                body="written offline",
                frontmatter={"kind": "fleeting"},
                tags=(),
                wikilinks=(),
                created_at="2026-05-08T10:00:00Z",
                updated_at="2026-05-08T10:00:00Z",
            ),
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
            synced=False,
        )

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [], "total": 0, "limit": 200, "offset": 0},
    )
    # POST permanently rejected — 409 DUPLICATE_FILENAME maps to RemoteRejectionError.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=409,
        json={"error": "DUPLICATE_FILENAME"},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.pushed_creates == 0
        assert result.push_failed == 1
        assert [entry["id"] for entry in result.push_rejected] == [local_id]
        assert "DUPLICATE_FILENAME" in result.push_rejected[0]["reason"]
        assert any("permanently rejected" in warning for warning in result.warnings)
        row = store.find_by_id(local_id)
        assert row is not None  # NOT reconciled away
        assert row["push_rejected_at"] is not None
        assert int(row["synced"]) == 0


def test_push_skips_row_with_existing_rejection_marker(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """A row already carrying a rejection marker is skipped up-front — no POST
    is attempted — but is still surfaced so the user knows it is stuck."""
    local_id = "34343434-5656-7878-9090-121212121212"
    with Store(tmp_settings.paths.index_path) as store:
        ingest_note(
            Note(
                id=local_id,
                filename="- Stuck",
                title="Stuck",
                family="fleeting",
                kind="fleeting",
                source=None,
                body="written offline",
                frontmatter={"kind": "fleeting"},
                tags=(),
                wikilinks=(),
                created_at="2026-05-08T10:00:00Z",
                updated_at="2026-05-08T10:00:00Z",
            ),
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
            synced=False,
        )
        store.record_push_rejection(
            local_id, reason="INVALID_FILENAME — bad grammar", at="2026-06-10T00:00:00Z"
        )

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [], "total": 0, "limit": 200, "offset": 0},
    )
    # NO POST mock — a push attempt would raise (pytest-httpx has no match).

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.pushed_creates == 0
        assert result.push_failed == 0  # never attempted, so not counted as a failure
        assert [entry["id"] for entry in result.push_rejected] == [local_id]
        # The row survived (synced=0 preserved), not deleted by reconcile.
        row = store.find_by_id(local_id)
        assert row is not None
        assert int(row["synced"]) == 0
    # The push pass never issued a POST.
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


def test_transient_push_failure_is_not_marked_rejected(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """A 5xx push failure is transient: no rejection marker, retried next sync."""
    local_id = "56565656-7878-9090-1212-343434343434"
    with Store(tmp_settings.paths.index_path) as store:
        ingest_note(
            Note(
                id=local_id,
                filename="- Flaky",
                title="Flaky",
                family="fleeting",
                kind="fleeting",
                source=None,
                body="written offline",
                frontmatter={"kind": "fleeting"},
                tags=(),
                wikilinks=(),
                created_at="2026-05-08T10:00:00Z",
                updated_at="2026-05-08T10:00:00Z",
            ),
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
            synced=False,
        )

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": [], "total": 0, "limit": 200, "offset": 0},
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=503,
        json={"error": "vault locked"},
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.push_failed == 1
        assert result.push_rejected == []
        row = store.find_by_id(local_id)
        assert row is not None
        assert row["push_rejected_at"] is None  # transient — retried next sync
        assert int(row["synced"]) == 0
        assert not any("delete detection skipped" in warning for warning in result.warnings)
