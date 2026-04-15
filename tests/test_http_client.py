"""RemoteBackend HTTP smoke tests via pytest-httpx."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from pytest_httpx import HTTPXMock

from knoten.repositories.backend import NoteDraft
from knoten.repositories.errors import AuthError, NetworkError, ValidationError
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


def test_create_note_400_non_validation_falls_through_to_network_error(
    tmp_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    """Non-VALIDATION_ERROR 400s (e.g. INVALID_FILENAME) still go down the
    generic NetworkError path. The VALIDATION_ERROR parsing is narrowly
    scoped so other structured envelopes keep their existing handling."""
    httpx_mock.add_response(
        url=f"{tmp_settings.api_url}/api/notes",
        method="POST",
        status_code=400,
        json={"error": "INVALID_FILENAME", "detail": {"message": "Unrecognized"}},
    )
    with RemoteBackend(tmp_settings) as backend, pytest.raises(NetworkError):
        backend.create_note(NoteDraft(filename="nope", body=""))


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
