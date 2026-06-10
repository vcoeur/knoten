"""`knoten upload` and `knoten download` — http client + service tests.

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

from knoten.models import Note
from knoten.repositories.errors import NotFoundError, UserError
from knoten.repositories.remote_backend import RemoteBackend
from knoten.repositories.store import Store
from knoten.services.notes import (
    download_file_remote,
    ingest_note,
    upload_file_remote,
)
from knoten.settings import Settings

FILE_NOTE_ID = "22222222-2222-2222-2222-222222222222"
# Server-shaped storage key: 32 lowercase hex chars + the original extension.
STORAGE_KEY = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6.pdf"


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
        permissions="ALL",
    )
    ingest_note(note, store=store, vault_dir=tmp_settings.paths.vault_dir)
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


def test_download_attachment_midstream_failure_leaves_no_partial_file(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    """A connection drop mid-stream must not leave a truncated file at the
    destination — the download streams to a sibling tmp file and only
    `os.replace`s it into place after the stream completes."""
    import httpx
    from pytest_httpx import IteratorStream

    from knoten.repositories.errors import NetworkError

    def _broken_stream():
        yield b"PARTIAL-"
        raise httpx.ReadError("connection dropped mid-stream")

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{STORAGE_KEY}",
        method="GET",
        stream=IteratorStream(_broken_stream()),
        headers={"content-type": "application/pdf"},
    )

    dest = tmp_path / "out.pdf"
    with RemoteBackend(tmp_settings) as backend, pytest.raises(NetworkError):
        backend.download_attachment(STORAGE_KEY, dest)

    assert not dest.exists(), "no partial file may remain at the destination"
    assert not (tmp_path / "out.pdf.tmp").exists(), "tmp file must be cleaned up"


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
            "permissions": "ALL",
            "createdAt": "2024-11-10T00:00:00Z",
            "updatedAt": "2024-11-10T00:00:00Z",
        },
    )

    with Store(tmp_settings.paths.index_path) as store, RemoteBackend(tmp_settings) as backend:
        note, upload = upload_file_remote(
            backend=backend,
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
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
        Store(tmp_settings.paths.index_path) as store,
        RemoteBackend(tmp_settings) as backend,
        pytest.raises(UserError, match="storageKey"),
    ):
        upload_file_remote(
            backend=backend,
            store=store,
            vault_dir=tmp_settings.paths.vault_dir,
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
    with Store(tmp_settings.paths.index_path) as store:
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
    with Store(tmp_settings.paths.index_path) as store:
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
            permissions="ALL",
        )
        ingest_note(non_file, store=store, vault_dir=tmp_settings.paths.vault_dir)
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
    with Store(tmp_settings.paths.index_path) as store:
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


@pytest.mark.parametrize(
    "hostile_key",
    [
        "../x",  # path traversal
        "a/b",  # path separator
        "a?x=1",  # query string — would redirect the authed GET
        "abc#frag",  # fragment
        "DEADBEEFDEADBEEFDEADBEEFDEADBEEF.pdf",  # uppercase hex (server keys are lowercase)
    ],
)
def test_download_file_remote_rejects_hostile_storage_key(
    tmp_settings: Settings, tmp_path: Path, hostile_key: str
) -> None:
    """A storage key carrying `/ ? #` (or non-lowercase-hex) is refused before
    any URL is built — interpolating it would redirect the authenticated GET."""
    with Store(tmp_settings.paths.index_path) as store:
        _seed_file_note(store, tmp_settings, frontmatter={"attachment": hostile_key})
        with RemoteBackend(tmp_settings) as backend:
            with pytest.raises(UserError, match="invalid") as excinfo:
                download_file_remote(
                    backend=backend,
                    store=store,
                    target="2024-11-10+ scan.pdf",
                    destination=tmp_path / "out.bin",
                )
    # The error names both the offending key and the referencing note.
    message = str(excinfo.value)
    assert repr(hostile_key) in message
    assert "2024-11-10+ scan.pdf" in message
    # Nothing was written — the rejection happens before the download.
    assert not (tmp_path / "out.bin").exists()


def test_download_file_remote_accepts_extensionless_hex_key(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    """A bare 32-hex key (upload had no file extension) passes validation."""
    bare_key = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{bare_key}",
        method="GET",
        content=b"BLOB",
        headers={"content-type": "application/octet-stream"},
    )
    dest = tmp_path / "out.bin"
    with Store(tmp_settings.paths.index_path) as store:
        _seed_file_note(store, tmp_settings, frontmatter={"attachment": bare_key})
        with RemoteBackend(tmp_settings) as backend:
            result = download_file_remote(
                backend=backend,
                store=store,
                target="2024-11-10+ scan.pdf",
                destination=dest,
            )
    assert dest.read_bytes() == b"BLOB"
    assert result["storage_key"] == bare_key


# ---- download default-destination confinement (server-controlled filename) --

HOSTILE_NOTE_ID = "44444444-4444-4444-4444-444444444444"


def _seed_raw_file_note(store: Store, *, filename: str) -> Note:
    """Seed a file-family store row WITHOUT writing a mirror file.

    The mirror writer would (rightly) reject hostile filenames, but the
    download path reads only the store row — exactly what a hostile remote
    can populate through sync.
    """
    note = Note(
        id=HOSTILE_NOTE_ID,
        filename=filename,
        title=filename,
        family="file",
        kind="file",
        source=None,
        body="",
        frontmatter={"attachment": STORAGE_KEY},
        tags=(),
        wikilinks=(),
        created_at="2024-11-10T00:00:00Z",
        updated_at="2024-11-10T00:00:00Z",
        permissions="ALL",
    )
    store.upsert_note(note, path="file/hostile.md", body_sha256="0" * 64)
    return note


def test_download_default_confines_absolute_server_filename_to_cwd(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path, monkeypatch
) -> None:
    """A note filename like /home/victim/.bashrc must NOT escape the cwd."""
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{STORAGE_KEY}",
        method="GET",
        content=b"EVIL",
    )
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    with Store(tmp_settings.paths.index_path) as store:
        note = _seed_raw_file_note(store, filename="/home/victim/.bashrc")
        with RemoteBackend(tmp_settings) as backend:
            result = download_file_remote(
                backend=backend, store=store, target=note.id, destination=None
            )
    expected = (workdir / ".bashrc").resolve()
    assert Path(result["path"]).resolve() == expected
    assert expected.read_bytes() == b"EVIL"


def test_download_default_confines_dotdot_server_filename_to_cwd(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path, monkeypatch
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{STORAGE_KEY}",
        method="GET",
        content=b"EVIL",
    )
    workdir = tmp_path / "inner" / "workdir"
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)
    with Store(tmp_settings.paths.index_path) as store:
        note = _seed_raw_file_note(store, filename="../../escape.bin")
        with RemoteBackend(tmp_settings) as backend:
            download_file_remote(backend=backend, store=store, target=note.id, destination=None)
    assert (workdir / "escape.bin").exists()
    assert not (workdir.parent / "escape.bin").exists()
    assert not (workdir.parent.parent / "escape.bin").exists()


@pytest.mark.parametrize("hostile_filename", ["..", "trick\\name.bin", "nul\x00name"])
def test_download_default_rejects_unusable_server_filename(
    tmp_settings: Settings, tmp_path: Path, monkeypatch, hostile_filename: str
) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    with Store(tmp_settings.paths.index_path) as store:
        note = _seed_raw_file_note(store, filename=hostile_filename)
        with (
            RemoteBackend(tmp_settings) as backend,
            pytest.raises(UserError, match="-o/--output"),
        ):
            download_file_remote(backend=backend, store=store, target=note.id, destination=None)


def test_download_default_plain_filename_lands_in_cwd(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path, monkeypatch
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{STORAGE_KEY}",
        method="GET",
        content=b"PDFBLOB",
    )
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    with Store(tmp_settings.paths.index_path) as store:
        _seed_file_note(store, tmp_settings)
        with RemoteBackend(tmp_settings) as backend:
            result = download_file_remote(
                backend=backend, store=store, target="2024-11-10+ scan.pdf", destination=None
            )
    expected = (workdir / "2024-11-10+ scan.pdf").resolve()
    assert Path(result["path"]).resolve() == expected
    assert expected.read_bytes() == b"PDFBLOB"


def test_download_explicit_output_is_honoured_even_with_hostile_filename(
    tmp_settings: Settings, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    """-o/--output is the user's explicit choice — no cwd confinement applied."""
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/attachments/{STORAGE_KEY}",
        method="GET",
        content=b"BYTES",
    )
    dest = tmp_path / "elsewhere" / "explicit.bin"
    with Store(tmp_settings.paths.index_path) as store:
        note = _seed_raw_file_note(store, filename="/home/victim/.bashrc")
        with RemoteBackend(tmp_settings) as backend:
            result = download_file_remote(
                backend=backend, store=store, target=note.id, destination=dest
            )
    assert result["path"] == dest
    assert dest.read_bytes() == b"BYTES"
