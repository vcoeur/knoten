"""CLI-level mapping of the server's structured 4xx error bodies.

Asserts the exit code and the `--json` error envelope for each structured
shape the remote emits, end-to-end through the Typer app with a mocked HTTP
backend. Complements the unit-level checks in `test_remote_backend.py`.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from knoten.cli.main import app
from knoten.models import Note
from knoten.repositories.store import Store
from knoten.services.notes import ingest_note
from knoten.settings import load_settings

API_URL = "https://notes.test"
NOTE_ID = "11111111-1111-1111-1111-111111111111"

runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch):
    """Point the CLI at a mocked remote backend (paths are sandboxed already)."""
    monkeypatch.setenv("KNOTEN_API_URL", API_URL)
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test_token")
    return load_settings()


def _seed(settings) -> None:
    note = Note(
        id=NOTE_ID,
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
        permissions="ALL",
    )
    with Store(settings.paths.index_path) as store:
        ingest_note(note, store=store, vault_dir=settings.paths.vault_dir)


def _envelope(result) -> dict:
    return json.loads(result.stdout)


def test_cli_create_duplicate_filename(cli_env, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        status_code=409,
        json={"error": "DUPLICATE_FILENAME"},
    )
    result = runner.invoke(app, ["create", "--filename", "! Dup", "--json"])
    assert result.exit_code == 1
    env = _envelope(result)
    assert env["error"] == "user"
    assert env["code"] == 1
    assert env["error_code"] == "DUPLICATE_FILENAME"


def test_cli_create_invalid_filename(cli_env, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        status_code=400,
        json={"error": "INVALID_FILENAME", "detail": {"message": "bad"}},
    )
    result = runner.invoke(app, ["create", "--filename", "nope", "--json"])
    assert result.exit_code == 1
    env = _envelope(result)
    assert env["error"] == "user"
    assert env["error_code"] == "INVALID_FILENAME"


def test_cli_edit_forbidden_maps_to_permission_denied(cli_env, httpx_mock: HTTPXMock) -> None:
    _seed(cli_env)
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="PUT",
        status_code=403,
        json={"error": "FORBIDDEN", "detail": {"noteId": NOTE_ID, "level": "WRITE"}},
    )
    result = runner.invoke(app, ["edit", NOTE_ID, "--title", "New", "--force", "--json"])
    assert result.exit_code == 1
    env = _envelope(result)
    assert env["error"] == "permission_denied"
    assert env["code"] == 1
    assert env["note_id"] == NOTE_ID
    assert env["required_level"] == "WRITE"


def test_cli_create_rate_limited(cli_env, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        status_code=429,
        json={"error": "rate_limited"},
    )
    result = runner.invoke(app, ["create", "--filename", "! RL", "--json"])
    assert result.exit_code == 2
    env = _envelope(result)
    assert env["error"] == "network"
    assert env["code"] == 2


def test_cli_create_payload_too_large(cli_env, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        status_code=413,
        json={"error": "payload_too_large", "detail": {"maxBytes": 1048576}},
    )
    result = runner.invoke(app, ["create", "--filename", "! Big", "--body", "x", "--json"])
    assert result.exit_code == 2
    env = _envelope(result)
    assert env["error"] == "network"
    assert "1048576" in env["message"]
