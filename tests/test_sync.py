"""Incremental sync end-to-end with a mocked remote."""

from __future__ import annotations

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

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.pushed_creates == 1
        assert result.pushed_edits == 0
        assert result.push_failed == 0
        # The local row's id was swapped to the server-issued one.
        assert store.find_by_id(server_id) is not None
        assert store.find_by_id(local_id) is None
        # And it is now synced.
        row = store.find_by_id(server_id)
        assert int(row["synced"]) == 1


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
