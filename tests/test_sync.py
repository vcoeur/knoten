"""Incremental sync end-to-end with a mocked remote."""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from app.repositories.http_client import NotesClient
from app.repositories.store import Store
from app.repositories.sync_state import load_state
from app.services.sync import incremental_sync
from app.settings import Settings


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

    with Store(tmp_settings.index_path) as store, NotesClient(tmp_settings) as client:
        result = incremental_sync(client=client, store=store, settings=tmp_settings)
        assert result.fetched == 1
        assert result.deleted == 0
        assert result.local_total == 1
        assert result.missing_refetched == 0
        assert result.orphans_removed == 0

    state = load_state(tmp_settings.state_file)
    assert state.last_sync_max_updated_at == "2024-01-02T00:00:00Z"

    written = tmp_settings.vault_dir / "note" / "! First.md"
    assert written.exists()
    content = written.read_text(encoding="utf-8")
    assert "Body of first" in content


def test_incremental_sync_skips_stale_items(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    # Pre-seed state with a cursor past the only note's updatedAt, and
    # pre-seed the local store + disk with a matching row. The cursor
    # ensures nothing is re-fetched from the list response, and the disk
    # state is clean so reconciliation is a no-op.
    tmp_settings.state_dir.mkdir(parents=True, exist_ok=True)
    tmp_settings.state_file.write_text(
        '{"schema_version": 1, "last_sync_max_updated_at": "2030-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    from app.models import Note
    from app.services.notes import ingest_note

    with Store(tmp_settings.index_path) as store:
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
        ingest_note(pre, store=store, vault_dir=tmp_settings.vault_dir)

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

    with Store(tmp_settings.index_path) as store, NotesClient(tmp_settings) as client:
        result = incremental_sync(client=client, store=store, settings=tmp_settings)
        assert result.fetched == 0
        assert result.deleted == 0
        assert result.missing_refetched == 0
