"""Write-command CLI output shape — minimal payload + --with-body escape hatch.

Every write-side subcommand (`create`, `edit`, `append`, `restore`, `rename`,
`upload`) defaults to emitting a small summary dict (id, filename, family,
kind, updated_at, mcp_permissions) instead of the full note body. Passing
`--with-body` restores the old behaviour. These tests lock that contract
down so the `/kasten` Claude skill can stop piping output through `python3`
to strip the body — it is already stripped by default.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from app.cli.main import app
from app.models import Note
from app.repositories.store import Store
from app.services.notes import ingest_note
from app.settings import load_settings

NOTE_ID = "11111111-1111-1111-1111-111111111111"
FILE_NOTE_ID = "22222222-2222-2222-2222-222222222222"
STORAGE_KEY = "att_abc123"
API_URL = "https://notes.test"

MINIMAL_KEYS = {
    "id",
    "filename",
    "title",
    "family",
    "kind",
    "mcp_permissions",
    "updated_at",
}

FORBIDDEN_KEYS = {"body", "frontmatter", "tags", "wikilinks", "backlinks"}


def _assert_minimal(payload: dict) -> None:
    """Minimal payloads carry identity + metadata, never body/tags/links."""
    assert MINIMAL_KEYS.issubset(payload.keys()), f"missing keys: {MINIMAL_KEYS - payload.keys()}"
    present_forbidden = FORBIDDEN_KEYS & payload.keys()
    assert not present_forbidden, f"unexpected keys leaked: {present_forbidden}"


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """Env wiring that lets the CLI find a per-test vault + mock server."""
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_URL", API_URL)
    monkeypatch.setenv("KASTEN_API_TOKEN", "nt_test_token")
    settings = load_settings()
    settings.vault_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _seed_permanent(settings, *, note_id: str = NOTE_ID) -> Note:
    note = Note(
        id=note_id,
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
        mcp_permissions="ALL",
    )
    with Store(settings.index_path) as store:
        ingest_note(note, store=store, vault_dir=settings.vault_dir)
    return note


def _full_note_payload(**overrides) -> dict:
    base = {
        "id": NOTE_ID,
        "filename": "! Seed",
        "title": "Seed",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "body": "first line\n\nsecond line with lots of text " * 50,
        "frontmatter": {"stub": "value"},
        "tags": ["alpha", "beta"],
        "linkMap": {},
        "mcpPermissions": "ALL",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-03T00:00:00Z",
    }
    base.update(overrides)
    return base


# ---- create -------------------------------------------------------------


def test_create_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["create", "--filename", "! Seed", "--body", "first line", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)
    assert payload["id"] == NOTE_ID


def test_create_with_body_flag_returns_full(cli_env, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["create", "--filename", "! Seed", "--body", "first line", "--with-body", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "body" in payload
    assert "first line" in payload["body"]
    assert "frontmatter" in payload
    assert "tags" in payload


# ---- edit ---------------------------------------------------------------


def test_edit_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    _seed_permanent(cli_env)
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="PUT",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["edit", NOTE_ID, "--title", "Seed updated", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)


# ---- append -------------------------------------------------------------


def test_append_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    _seed_permanent(cli_env)
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}/append",
        method="POST",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["append", NOTE_ID, "--content", "second line", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)


# ---- restore ------------------------------------------------------------


def test_restore_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    # A trashed note does not need to be locally seeded — restore starts
    # with a remote-only UUID and ingests into the mirror on refresh.
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}/restore",
        method="POST",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["restore", NOTE_ID, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)


# ---- rename -------------------------------------------------------------


def test_rename_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    _seed_permanent(cli_env)
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="PUT",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(filename="! Seed renamed", title="Seed renamed"),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["rename", NOTE_ID, "! Seed renamed", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)


# ---- upload -------------------------------------------------------------


def test_upload_default_is_minimal_but_keeps_upload_block(
    cli_env, httpx_mock: HTTPXMock, tmp_path
) -> None:
    sample = tmp_path / "scan.pdf"
    sample.write_bytes(b"PDFDATA")

    httpx_mock.add_response(
        url=f"{API_URL}/api/attachments",
        method="POST",
        status_code=201,
        json={
            "storageKey": STORAGE_KEY,
            "sizeBytes": "7",
            "contentType": "application/pdf",
            "url": f"/api/attachments/{STORAGE_KEY}",
        },
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        json={"id": FILE_NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{FILE_NOTE_ID}",
        method="GET",
        json={
            "id": FILE_NOTE_ID,
            "filename": "2024-11-10+ scan.pdf",
            "title": "scan.pdf",
            "family": "file",
            "kind": "file",
            "source": "2024-11-10",
            "body": "body text that should NOT appear in minimal output " * 30,
            "frontmatter": {"attachment": STORAGE_KEY},
            "tags": [],
            "linkMap": {},
            "mcpPermissions": "ALL",
            "createdAt": "2024-11-10T00:00:00Z",
            "updatedAt": "2024-11-10T00:00:00Z",
        },
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "upload",
            str(sample),
            "--filename",
            "2024-11-10+ scan.pdf",
            "--content-type",
            "application/pdf",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)
    # Upload metadata always survives — it is the whole point of the command.
    assert payload["upload"]["storage_key"] == STORAGE_KEY
    assert payload["upload"]["content_type"] == "application/pdf"
    assert payload["upload"]["size_bytes"] == "7"
