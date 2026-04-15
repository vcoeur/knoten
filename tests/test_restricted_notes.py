"""Restricted-note handling: LIST-permission notes become local placeholders.

The server returns 404 on `GET /api/notes/{id}` for notes the token cannot
READ (conflating "forbidden" with "not found" to hide existence). Sync must
catch this and create a metadata-only placeholder so (a) title search still
finds the note and (b) local_total == remote_total despite restrictions.
"""

from __future__ import annotations

import json

from pytest_httpx import HTTPXMock

from knoten.repositories.errors import NoteForbiddenError
from knoten.repositories.remote_backend import RemoteBackend
from knoten.repositories.store import Store
from knoten.services.sync import incremental_sync
from knoten.settings import Settings


def _list_item(note_id: str, filename: str, updated_at: str) -> dict:
    return {
        "id": note_id,
        "filename": filename,
        "title": filename.lstrip("!@$%&-=.+ "),
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "tags": [],
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": updated_at,
    }


def _note_payload(note_id: str, filename: str) -> dict:
    return {
        "id": note_id,
        "filename": filename,
        "title": filename.lstrip("!@$%&-=.+ "),
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "body": f"Body of {filename}.",
        "frontmatter": {"kind": "permanent", "title": filename},
        "tags": [],
        "linkMap": {},
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }


def test_http_client_raises_note_forbidden_on_404(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    note_id = "11111111-1111-1111-1111-111111111111"
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{note_id}",
        status_code=404,
        json={"error": "not_found"},
    )
    with RemoteBackend(tmp_settings) as backend:
        try:
            backend.read_note(note_id)
            raise AssertionError("expected NoteForbiddenError")
        except NoteForbiddenError as exc:
            assert exc.note_id == note_id


def test_sync_creates_placeholder_for_404_note(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    readable_id = "22222222-2222-2222-2222-222222222222"
    restricted_id = "33333333-3333-3333-3333-333333333333"

    list_payload = {
        "data": [
            _list_item(readable_id, "! Readable", "2024-01-02T00:00:00Z"),
            _list_item(restricted_id, "! Restricted", "2024-01-02T00:00:00Z"),
        ],
        "total": 2,
        "limit": 100,
        "offset": 0,
    }
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json=list_payload,
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{readable_id}",
        json=_note_payload(readable_id, "! Readable"),
    )
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{restricted_id}",
        status_code=404,
        json={"error": "not_found"},
    )
    # Delete detection ID scan (always runs on every sync).
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={
            "data": list_payload["data"],
            "total": 2,
            "limit": 200,
            "offset": 0,
        },
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.fetched == 1
        assert result.restricted_placeholders == 1
        assert result.local_total == 2  # placeholder counts toward local
        assert result.remote_total == 2

        # Placeholder row exists with restricted=1.
        row = store.find_by_id(restricted_id)
        assert row is not None
        assert row["restricted"] == 1
        assert row["body_sha256"] == ""
        assert json.loads(row["frontmatter_json"]) == {}

        # Placeholder is in FTS5 via title, so search by title still works.
        hits, total = store.search("Restricted", vault_dir=tmp_settings.paths.vault_dir)
        assert total == 1
        assert hits[0].id == restricted_id

        # Placeholder is NOT in FTS5 via body (body is empty).
        hits, total = store.search("Body of", vault_dir=tmp_settings.paths.vault_dir)
        assert total == 1  # only the readable one
        assert hits[0].id == readable_id

    # The placeholder file exists on disk with the restricted marker.
    placeholder_file = tmp_settings.paths.vault_dir / "note" / "! Restricted.md"
    assert placeholder_file.exists()
    content = placeholder_file.read_text(encoding="utf-8")
    assert "restricted: true" in content
    assert "Body not fetchable" in content


def test_sync_drift_catch_up_pass_finds_never_seen_ids(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """Regression test for the ~90-note drift.

    Pre-seed the store + the cursor so the list response's items are all
    older than the cursor. The first-page pagination sees a stale item and
    stops early. The drift-catch-up pass must then walk iter_all_summaries,
    notice an ID that is in the remote but not in the local store, and
    fetch it.
    """
    known_id = "44444444-4444-4444-4444-444444444444"
    never_seen_id = "55555555-5555-5555-5555-555555555555"

    # Pre-seed state with a cursor and a local row for known_id.
    tmp_settings.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_settings.paths.state_file.write_text(
        '{"schema_version": 2, "last_sync_max_updated_at": "2030-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    from knoten.models import Note

    with Store(tmp_settings.paths.index_path) as store:
        pre = Note(
            id=known_id,
            filename="! Known",
            title="Known",
            family="permanent",
            kind="permanent",
            source=None,
            body="known body",
            frontmatter={"kind": "permanent"},
            tags=(),
            wikilinks=(),
            created_at="2020-01-01T00:00:00Z",
            updated_at="2020-01-02T00:00:00Z",
        )
        from knoten.services.notes import ingest_note

        ingest_note(pre, store=store, vault_dir=tmp_settings.paths.vault_dir)

    # List response: both notes present, both older than the cursor (so the
    # main pagination loop bails on the stale page immediately).
    list_items = [
        _list_item(known_id, "! Known", "2020-01-02T00:00:00Z"),
        _list_item(never_seen_id, "! NeverSeen", "2020-01-02T00:00:00Z"),
    ]
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={"data": list_items, "total": 2, "limit": 100, "offset": 0},
    )
    # Merged reconcile pass (delete detection + drift catch-up) walks
    # iter_all_summaries exactly once at page_size=200.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={"data": list_items, "total": 2, "limit": 200, "offset": 0},
    )
    # It then fetches the never-seen note in full.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{never_seen_id}",
        json=_note_payload(never_seen_id, "! NeverSeen"),
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        result = incremental_sync(backend=backend, store=store, settings=tmp_settings)
        assert result.fetched == 1  # the never-seen note was pulled
        assert store.find_by_id(never_seen_id) is not None
        assert store.count_notes() == 2
