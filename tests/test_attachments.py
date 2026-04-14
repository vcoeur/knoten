"""`kasten upload` and `kasten download` — http client + service tests.

Covers:
  * `NotesClient.upload_attachment` posts multipart form data to
    `/api/attachments` and parses the response.
  * `NotesClient.download_attachment` streams the body to disk and returns
    the bytes written plus content-type / disposition metadata.
  * `upload_file_remote` performs the two-step flow (upload → create file
    note) and ingests the fresh note into the local mirror.
  * `download_file_remote` refuses non-file-family targets and targets whose
    frontmatter lacks an `attachment` key.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from app.models import Note
from app.repositories.errors import NotFoundError, UserError
from app.repositories.remote_backend import RemoteBackend
from app.repositories.store import Store
from app.services.notes import (
    download_file_remote,
    ingest_note,
    upload_file_remote,
)
from app.settings import Settings

FILE_NOTE_ID = "22222222-2222-2222-2222-222222222222"
STORAGE_KEY = "att_abc123"


def _seed_file_note(
    store: Store,
    tmp_settings: Settings,
    *,
    note_id: str = FILE_NOTE_ID,
    filename: str = "2024-11-10+ scan.pdf",
    frontmatter: dict | None = None,
) -> Note:
    note = Note(
        id=note_id,
        filename=filename,
        title=filename,
        family="file",
        kind="file",
        source="2024-11-10",
        body="",
        frontmatter=frontmatter if frontmatter is not None else {"attachment": STORAGE_KEY},
        tags=(),
        wikilinks=(),
        created_at="2024-11-10T00:00:00Z",
        updated_at="2024-11-10T00:00:00Z",
        mcp_permissions="ALL",
    )
    ingest_note(note, store=store, vault_dir=tmp_settings.vault_dir)
    return note


# ---- NotesClient.upload_attachment --------------------------------------


def test_upload_attachment_posts_multipart(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"PDF-BYTES")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments",
        method="POST",
        status_code=201,
        json={
            "id": "att-row-1",
            "filename": "sample.pdf",
            "contentType": "application/pdf",
            "sizeBytes": "9",
            "storageKey": STORAGE_KEY,
            "source": None,
            "url": f"/api/attachments/{STORAGE_KEY}",
        },
    )

    with RemoteBackend(tmp_settings) as backend:
        result = backend.upload_attachment(sample, content_type="application/pdf")

    assert result.storage_key == STORAGE_KEY
    assert result.content_type == "application/pdf"
    assert result.size_bytes == 9
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = requests[0].content
    assert b"PDF-BYTES" in body
    assert b'name="file"' in body
    assert b"sample.pdf" in body
    assert b"application/pdf" in body


def test_upload_attachment_includes_source_field(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"x")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments",
        method="POST",
        status_code=201,
        json={"storageKey": STORAGE_KEY, "sizeBytes": "1"},
    )

    with RemoteBackend(tmp_settings) as backend:
        backend.upload_attachment(sample, content_type="application/pdf", source="Scott2019")

    requests = httpx_mock.get_requests()
    assert b'name="source"' in requests[0].content
    assert b"Scott2019" in requests[0].content


# ---- NotesClient.download_attachment ------------------------------------


def test_download_attachment_streams_to_disk(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{STORAGE_KEY}",
        method="GET",
        content=b"PDFBLOB",
        headers={
            "content-type": "application/pdf",
            "content-disposition": 'inline; filename="scan.pdf"',
        },
    )

    dest = tmp_path / "out.pdf"
    with RemoteBackend(tmp_settings) as backend:
        result = backend.download_attachment(STORAGE_KEY, dest)

    assert dest.read_bytes() == b"PDFBLOB"
    assert result.bytes_written == 7
    assert result.content_type == "application/pdf"
    assert result.filename == "scan.pdf"


def test_download_attachment_404_raises_not_found(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{STORAGE_KEY}",
        method="GET",
        status_code=404,
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(NotFoundError):
        backend.download_attachment(STORAGE_KEY, tmp_path / "out.bin")


# ---- upload_file_remote -------------------------------------------------


def test_upload_file_remote_two_step_flow(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    sample = tmp_path / "scan.pdf"
    sample.write_bytes(b"DATA")

    # 1. Upload.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments",
        method="POST",
        status_code=201,
        json={"storageKey": STORAGE_KEY, "sizeBytes": "4", "contentType": "application/pdf"},
    )
    # 2. Create note.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        json={"id": FILE_NOTE_ID},
    )
    # 3. Refresh — service re-reads the created note.
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/{FILE_NOTE_ID}",
        method="GET",
        json={
            "id": FILE_NOTE_ID,
            "filename": "2024-11-10+ scan.pdf",
            "title": "scan.pdf",
            "family": "file",
            "kind": "file",
            "source": "2024-11-10",
            "body": "",
            "frontmatter": {"attachment": STORAGE_KEY},
            "tags": [],
            "linkMap": {},
            "mcpPermissions": "ALL",
            "createdAt": "2024-11-10T00:00:00Z",
            "updatedAt": "2024-11-10T00:00:00Z",
        },
    )

    with Store(tmp_settings.index_path) as store, RemoteBackend(tmp_settings) as backend:
        note, upload = upload_file_remote(
            backend=backend,
            store=store,
            vault_dir=tmp_settings.vault_dir,
            source_path=sample,
            filename="2024-11-10+ scan.pdf",
            tags=[],
            source=None,
            content_type="application/pdf",
        )

    assert note.id == FILE_NOTE_ID
    assert upload["storageKey"] == STORAGE_KEY

    # Second HTTP call should be a note-create carrying the storage key in frontmatter.
    requests = httpx_mock.get_requests()
    create_req = next(r for r in requests if r.method == "POST" and r.url.path == "/api/notes")
    sent = _json.loads(create_req.content)
    assert sent["filename"] == "2024-11-10+ scan.pdf"
    assert sent["kind"] == "file"
    assert sent["frontmatter"] == {"attachment": STORAGE_KEY}


def test_upload_file_remote_rejects_missing_storage_key(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    sample = tmp_path / "scan.pdf"
    sample.write_bytes(b"DATA")
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments",
        method="POST",
        status_code=201,
        json={"sizeBytes": "4"},  # no storageKey
    )
    with (
        Store(tmp_settings.index_path) as store,
        RemoteBackend(tmp_settings) as backend,
        pytest.raises(UserError, match="storageKey"),
    ):
        upload_file_remote(
            backend=backend,
            store=store,
            vault_dir=tmp_settings.vault_dir,
            source_path=sample,
            filename="2024-11-10+ scan.pdf",
            tags=[],
            source=None,
            content_type=None,
        )


# ---- download_file_remote -----------------------------------------------


def test_download_file_remote_happy_path(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{STORAGE_KEY}",
        method="GET",
        content=b"PDFBLOB",
        headers={"content-type": "application/pdf"},
    )
    dest = tmp_path / "out.pdf"
    with Store(tmp_settings.index_path) as store:
        _seed_file_note(store, tmp_settings)
        with RemoteBackend(tmp_settings) as backend:
            result = download_file_remote(
                backend=backend,
                store=store,
                target="2024-11-10+ scan.pdf",
                destination=dest,
            )

    assert dest.read_bytes() == b"PDFBLOB"
    assert result["note_id"] == FILE_NOTE_ID
    assert result["storage_key"] == STORAGE_KEY
    assert result["bytes_written"] == 7


def test_download_file_remote_rejects_non_file_family(
    tmp_settings: Settings, tmp_path: Path
) -> None:
    with Store(tmp_settings.index_path) as store:
        non_file = Note(
            id="33333333-3333-3333-3333-333333333333",
            filename="! Permanent",
            title="Permanent",
            family="permanent",
            kind="permanent",
            source=None,
            body="",
            frontmatter={},
            tags=(),
            wikilinks=(),
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            mcp_permissions="ALL",
        )
        ingest_note(non_file, store=store, vault_dir=tmp_settings.vault_dir)
        with (
            RemoteBackend(tmp_settings) as backend,
            pytest.raises(UserError, match="not a file-family note"),
        ):
            download_file_remote(
                backend=backend,
                store=store,
                target="! Permanent",
                destination=tmp_path / "should-not-exist.bin",
            )


def test_download_file_remote_rejects_missing_attachment_key(
    tmp_settings: Settings, tmp_path: Path
) -> None:
    with Store(tmp_settings.index_path) as store:
        _seed_file_note(store, tmp_settings, frontmatter={})
        with (
            RemoteBackend(tmp_settings) as backend,
            pytest.raises(UserError, match="no `attachment` key"),
        ):
            download_file_remote(
                backend=backend,
                store=store,
                target="2024-11-10+ scan.pdf",
                destination=tmp_path / "out.bin",
            )
