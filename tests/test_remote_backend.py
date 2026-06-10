"""RemoteBackend HTTP smoke tests via pytest-httpx."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from pytest_httpx import HTTPXMock

from knoten.repositories.backend import NoteDraft
from knoten.repositories.errors import (
    AuthError,
    NetworkError,
    RemoteRejectionError,
    ValidationError,
)
from knoten.repositories.errors import (
    PermissionError as LocalPermissionError,
)
from knoten.repositories.remote_backend import RemoteBackend
from knoten.services.sync import iter_all_summaries
from knoten.settings import Settings


def test_client_requires_token(tmp_settings: Settings) -> None:
    bad = replace(tmp_settings, api_token="")
    with pytest.raises(AuthError):
        RemoteBackend(bad)


def test_list_note_summaries_parses_response(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=100&offset=0",
        json={
            "data": [
                {
                    "id": "id-1",
                    "filename": "! One",
                    "title": "One",
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
        },
    )
    with RemoteBackend(tmp_settings) as backend:
        page = backend.list_note_summaries(limit=100, offset=0)
    assert page.total == 1
    assert len(page.data) == 1
    assert page.data[0].id == "id-1"
    assert page.data[0].filename == "! One"
    assert page.data[0].updated_at == "2024-01-02T00:00:00Z"


def test_read_note_401_raises_auth_error(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/abc",
        status_code=401,
        json={"error": "UNAUTHORIZED"},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(AuthError):
        backend.read_note("abc")


def test_network_failure_raises_network_error(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    with RemoteBackend(tmp_settings) as backend, pytest.raises(NetworkError):
        backend.read_note("abc")


def test_delete_note_404_raises_not_found(tmp_settings: Settings, httpx_mock: HTTPXMock) -> None:
    """Deleting an id unknown to the remote is NotFoundError (exit 1), not a
    generic NetworkError (exit 2) — matching read_note and LocalBackend, and
    letting the sync push pass treat a real-server 404 as already-deleted."""
    from knoten.repositories.errors import NotFoundError

    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/missing-id",
        method="DELETE",
        status_code=404,
        json={"error": "NOT_FOUND"},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(NotFoundError):
        backend.delete_note("missing-id")


def test_create_note_400_validation_error_raises_validation_error(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """notes.vcoeur.com v2.9.1+ returns {error:VALIDATION_ERROR, detail:{issues:[…]}}
    on type-mismatched frontmatter. Parse it into a typed ValidationError so
    the CLI can surface the structured issues list instead of a generic 400."""
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=400,
        json={
            "error": "VALIDATION_ERROR",
            "detail": {
                "issues": [
                    {
                        "key": "birth-year",
                        "expected": "number",
                        "actual": "string",
                        "message": "birth-year: expected finite number (got string)",
                    }
                ]
            },
        },
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(ValidationError) as excinfo:
        backend.create_note(
            NoteDraft(
                filename="@ Jane",
                body="x",
                frontmatter={"birth-year": "nineteen-ninety"},
            )
        )
    assert excinfo.value.method == "POST"
    assert excinfo.value.path == "/api/notes"
    assert len(excinfo.value.issues) == 1
    assert excinfo.value.issues[0]["key"] == "birth-year"
    assert excinfo.value.issues[0]["expected"] == "number"
    assert "birth-year" in str(excinfo.value)


def test_create_note_400_invalid_filename_raises_remote_rejection(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """A 400 INVALID_FILENAME is a user-actionable rejection (exit 1), not a
    generic network failure — it carries the server error code on `error_code`."""
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=400,
        json={"error": "INVALID_FILENAME", "detail": {"message": "Unrecognized"}},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(RemoteRejectionError) as exc_info:
        backend.create_note(NoteDraft(filename="nope", body=""))
    assert exc_info.value.error_code == "INVALID_FILENAME"


def test_create_note_409_duplicate_filename_raises_remote_rejection(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=409,
        json={"error": "DUPLICATE_FILENAME"},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(RemoteRejectionError) as exc_info:
        backend.create_note(NoteDraft(filename="! Dup", body=""))
    assert exc_info.value.error_code == "DUPLICATE_FILENAME"


def test_create_note_409_duplicate_reference_raises_remote_rejection(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=409,
        json={"error": "DUPLICATE_REFERENCE", "detail": {"ref": "Voland2024"}},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(RemoteRejectionError) as exc_info:
        backend.create_note(NoteDraft(filename="Voland2024= Ref", body=""))
    assert exc_info.value.error_code == "DUPLICATE_REFERENCE"


def test_read_note_403_forbidden_raises_permission_error(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """A 403 FORBIDDEN with a structured body maps to PermissionError (exit 1,
    permission_denied) carrying the noteId and required level — not AuthError."""
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/abc",
        method="GET",
        status_code=403,
        json={"error": "FORBIDDEN", "detail": {"noteId": "abc", "level": "READ"}},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(LocalPermissionError) as exc_info:
        backend.read_note("abc")
    assert exc_info.value.note_id == "abc"
    assert exc_info.value.required_level == "READ"


def test_403_without_recognisable_body_stays_auth_error(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/abc",
        method="GET",
        status_code=403,
        json={"error": "SOMETHING_ELSE"},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(AuthError):
        backend.read_note("abc")


def test_429_rate_limited_raises_network_error(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes/abc",
        method="GET",
        status_code=429,
        json={"error": "rate_limited", "message": "Too many requests."},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(NetworkError) as exc_info:
        backend.read_note("abc")
    assert "rate limited" in str(exc_info.value).lower()


def test_413_payload_too_large_raises_network_error_with_cap(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=413,
        json={"error": "payload_too_large", "detail": {"maxBytes": 1048576}},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(NetworkError) as exc_info:
        backend.create_note(NoteDraft(filename="! Big", body="x"))
    assert "1048576" in str(exc_info.value)


def test_list_trashed_notes_parses_bare_array_with_deleted_at(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """GET /api/trash/notes returns a bare JSON array of note rows (each with
    `deletedAt`), not the standard envelope — parse it modulo deletedAt."""
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/trash/notes",
        json=[
            {
                "id": "t-1",
                "filename": "! Trashed",
                "title": "Trashed",
                "family": "permanent",
                "kind": "permanent",
                "source": None,
                "permissions": "ALL",
                "deletedAt": "2024-02-01T00:00:00Z",
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-02T00:00:00Z",
            }
        ],
    )
    with RemoteBackend(tmp_settings) as backend:
        rows = backend.list_trashed_notes()
    assert len(rows) == 1
    assert rows[0].id == "t-1"
    assert rows[0].filename == "! Trashed"
    assert rows[0].deleted_at == "2024-02-01T00:00:00Z"


def test_iter_all_summaries_stops_on_short_page(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes?limit=200&offset=0",
        json={
            "data": [
                {"id": "a", "updatedAt": "2024-01-02T00:00:00Z"},
                {"id": "b", "updatedAt": "2024-01-01T00:00:00Z"},
            ],
            "total": 2,
        },
    )
    with RemoteBackend(tmp_settings) as backend:
        ids = [item.id for item in iter_all_summaries(backend)]
    assert ids == ["a", "b"]
