"""`knoten append` — http client, service, and write-path integration.

Covers:
  * `NotesClient.append_note` hits `POST /api/notes/{id}/append` with
    `{"content": ...}` and returns the server's updated note.
  * `append_note_remote` refuses when the local `mcp_permissions` is below
    `APPEND` unless `--force` is passed.
  * `append_note_remote` round-trips through the local store: the note row
    and the mirror file get the new body.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from knoten.models import Note
from knoten.repositories.errors import PermissionError as LocalPermissionError
from knoten.repositories.remote_backend import RemoteBackend
from knoten.repositories.store import Store
from knoten.services.notes import append_note_remote, ingest_note
from knoten.settings import Settings


def _seed_local(store: Store, tmp_settings: Settings, *, mcp_permissions: str = "ALL") -> Note:
    note = Note(
        id="11111111-1111-1111-1111-111111111111",
        filename="! Seed",
        title="Seed",
        family="permanent",
        kind="permanent",
        source=None,
        body="first line",
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        mcp_permissions=mcp_permissions,
    )
    ingest_note(note, store=store, vault_dir=tmp_settings.paths.vault_dir)
    return note


def test_append_to_note_backend_posts_to_append_endpoint(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/11111111-1111-1111-1111-111111111111/append",
        method="POST",
        json={
            "id": "11111111-1111-1111-1111-111111111111",
            "filename": "! Seed",
            "title": "Seed",
            "family": "permanent",
            "kind": "permanent",
            "source": None,
            "body": "first line\n\nsecond line",
            "frontmatter": {},
            "tags": [],
            "linkMap": {},
            "mcpPermissions": "ALL",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-03T00:00:00Z",
        },
    )
    with RemoteBackend(tmp_settings) as backend:
        result = backend.append_to_note("11111111-1111-1111-1111-111111111111", "second line")
    assert result is None

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url).endswith("/api/notes/11111111-1111-1111-1111-111111111111/append")
    import json as _json

    sent = _json.loads(requests[0].content)
    assert sent == {"content": "second line"}


def test_append_note_remote_round_trips_through_store(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    with Store(tmp_settings.paths.index_path) as store:
        _seed_local(store, tmp_settings)

        httpx_mock.add_response(
            url=f"{tmp_settings.api_url}/api/notes/11111111-1111-1111-1111-111111111111/append",
            method="POST",
            json={
                "id": "11111111-1111-1111-1111-111111111111",
                "body": "(ignored by service; refetch is authoritative)",
            },
        )
        # Service re-fetches after append.
        httpx_mock.add_response(
            url=f"{tmp_settings.api_url}/api/notes/11111111-1111-1111-1111-111111111111",
            json={
                "id": "11111111-1111-1111-1111-111111111111",
                "filename": "! Seed",
                "title": "Seed",
                "family": "permanent",
                "kind": "permanent",
                "source": None,
                "body": "first line\n\nsecond line",
                "frontmatter": {},
                "tags": [],
                "linkMap": {},
                "mcpPermissions": "ALL",
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-03T00:00:00Z",
            },
        )

        with RemoteBackend(tmp_settings) as backend:
            note = append_note_remote(
                backend=backend,
                store=store,
                vault_dir=tmp_settings.paths.vault_dir,
                target="11111111-1111-1111-1111-111111111111",
                content="second line",
            )

        assert note.body == "first line\n\nsecond line"

        # Mirror file has the new body.
        path = tmp_settings.paths.vault_dir / "note" / "! Seed.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "first line" in text
        assert "second line" in text


def test_append_note_remote_refuses_below_append_without_force(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    with Store(tmp_settings.paths.index_path) as store:
        _seed_local(store, tmp_settings, mcp_permissions="READ")

        with RemoteBackend(tmp_settings) as backend:
            with pytest.raises(LocalPermissionError) as exc_info:
                append_note_remote(
                    backend=backend,
                    store=store,
                    vault_dir=tmp_settings.paths.vault_dir,
                    target="11111111-1111-1111-1111-111111111111",
                    content="blocked",
                )

    assert exc_info.value.current_level == "READ"
    assert exc_info.value.required_level == "APPEND"
    assert exc_info.value.operation == "append"
    # Pre-check fired before any HTTP round-trip.
    assert len(httpx_mock.get_requests()) == 0


def test_append_note_remote_force_bypasses_precheck(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    with Store(tmp_settings.paths.index_path) as store:
        _seed_local(store, tmp_settings, mcp_permissions="READ")

        # Server would accept it for a web-scope token; mock accordingly.
        httpx_mock.add_response(
            url=f"{tmp_settings.api_url}/api/notes/11111111-1111-1111-1111-111111111111/append",
            method="POST",
            json={"id": "11111111-1111-1111-1111-111111111111"},
        )
        httpx_mock.add_response(
            url=f"{tmp_settings.api_url}/api/notes/11111111-1111-1111-1111-111111111111",
            json={
                "id": "11111111-1111-1111-1111-111111111111",
                "filename": "! Seed",
                "title": "Seed",
                "family": "permanent",
                "kind": "permanent",
                "source": None,
                "body": "first line\n\nforced",
                "frontmatter": {},
                "tags": [],
                "linkMap": {},
                "mcpPermissions": "READ",
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-03T00:00:00Z",
            },
        )
        with RemoteBackend(tmp_settings) as backend:
            note = append_note_remote(
                backend=backend,
                store=store,
                vault_dir=tmp_settings.paths.vault_dir,
                target="11111111-1111-1111-1111-111111111111",
                content="forced",
                force=True,
            )

    assert note.body.endswith("forced")
