"""`--ai` flag — wraps written content in `#ai begin` / `#ai end` markers.

Covers the helper in isolation plus each of the three write commands
(`create`, `edit`, `append`) end-to-end through the Typer CLI.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from knoten.cli.main import _wrap_ai, app


def test_wrap_ai_basic() -> None:
    assert _wrap_ai("hello") == "#ai begin\nhello\n#ai end"


def test_wrap_ai_trims_surrounding_newlines() -> None:
    assert _wrap_ai("\n\nhello\n\n") == "#ai begin\nhello\n#ai end"


def test_wrap_ai_preserves_interior_blank_lines() -> None:
    assert _wrap_ai("line 1\n\nline 2") == "#ai begin\nline 1\n\nline 2\n#ai end"


def test_wrap_ai_does_not_strip_existing_markers() -> None:
    """Literal wrap: pre-existing markers become nested, not deduplicated."""
    wrapped = _wrap_ai("#ai begin\nx\n#ai end")
    assert wrapped == "#ai begin\n#ai begin\nx\n#ai end\n#ai end"


def _mock_note_response(httpx_mock: HTTPXMock, *, body: str) -> None:
    """Minimal note payload the service layer accepts.

    Registers both the POST (create) and the subsequent GET (service
    re-fetches the note by id to refresh the local mirror).
    """
    payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "filename": "- ai test",
        "title": "ai test",
        "family": "fleeting",
        "kind": "fleeting",
        "source": None,
        "body": body,
        "frontmatter": {},
        "tags": [],
        "linkMap": {},
        "mcpPermissions": "ALL",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-01T00:00:00Z",
    }
    httpx_mock.add_response(method="POST", url="https://notes.test/api/notes", json=payload)
    httpx_mock.add_response(
        method="GET",
        url="https://notes.test/api/notes/11111111-1111-1111-1111-111111111111",
        json=payload,
    )


def test_cli_create_ai_wraps_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    _mock_note_response(httpx_mock, body="#ai begin\nhello world\n#ai end")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["create", "--filename", "- ai test", "--body", "hello world", "--ai", "--json"],
    )
    assert result.exit_code == 0, result.output

    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sent["body"] == "#ai begin\nhello world\n#ai end"


def test_cli_create_ai_without_body_errors(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")

    runner = CliRunner()
    result = runner.invoke(app, ["create", "--filename", "- ai test", "--ai", "--json"])
    assert result.exit_code == 1


def test_cli_append_ai_wraps_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path, httpx_mock: HTTPXMock
) -> None:
    from knoten.models import Note
    from knoten.repositories.store import Store
    from knoten.services.notes import ingest_note

    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")

    vault_dir = tmp_path / "kasten"
    state_dir = tmp_path / ".knoten-state"
    vault_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)

    with Store(state_dir / "index.sqlite") as store:
        ingest_note(
            Note(
                id="11111111-1111-1111-1111-111111111111",
                filename="- seed",
                title="seed",
                family="fleeting",
                kind="fleeting",
                source=None,
                body="existing",
                frontmatter={},
                tags=(),
                wikilinks=(),
                created_at="2024-01-01T00:00:00Z",
                updated_at="2024-01-01T00:00:00Z",
                mcp_permissions="ALL",
            ),
            store=store,
            vault_dir=vault_dir,
        )

    note_payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "filename": "- seed",
        "title": "seed",
        "family": "fleeting",
        "kind": "fleeting",
        "source": None,
        "body": "existing\n\n#ai begin\naddendum\n#ai end",
        "frontmatter": {},
        "tags": [],
        "linkMap": {},
        "mcpPermissions": "ALL",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }
    httpx_mock.add_response(
        method="POST",
        url="https://notes.test/api/notes/11111111-1111-1111-1111-111111111111/append",
        json=note_payload,
    )
    httpx_mock.add_response(
        method="GET",
        url="https://notes.test/api/notes/11111111-1111-1111-1111-111111111111",
        json=note_payload,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "append",
            "11111111-1111-1111-1111-111111111111",
            "--content",
            "addendum",
            "--ai",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    sent = json.loads(post.content)
    assert sent == {"content": "#ai begin\naddendum\n#ai end"}
