"""RemoteBackend HTTP smoke tests via pytest-httpx."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from app.repositories.errors import AuthError, NetworkError
from app.repositories.remote_backend import RemoteBackend
from app.services.sync import iter_all_summaries
from app.settings import Settings


def test_client_requires_token(tmp_settings: Settings) -> None:
    bad = Settings(
        api_url=tmp_settings.api_url,
        api_token="",
        http_timeout=tmp_settings.http_timeout,
        home=tmp_settings.home,
        vault_dir=tmp_settings.vault_dir,
        state_dir=tmp_settings.state_dir,
        index_path=tmp_settings.index_path,
        state_file=tmp_settings.state_file,
        lock_file=tmp_settings.lock_file,
        tmp_dir=tmp_settings.tmp_dir,
    )
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
