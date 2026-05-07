"""End-to-end tests for the `knoten inbox` sub-app.

Drives the Typer app against a `tmp_path` vault in local mode (no network).
URL captures are tested with a stubbed `_fetch_url_title` so the test does
not actually hit the network.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from knoten.cli.main import app


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    """Per-test tmp dir, KNOTEN_API_URL empty → LocalBackend."""
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    monkeypatch.setenv("KNOTEN_MODE", "local")
    return tmp_path


def _invoke(args: list[str]) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout


def test_inbox_add_text_creates_fleeting_with_inbox_tag(local_env) -> None:
    code, out = _invoke(["inbox", "add", "--json", "--", "a thought I want to capture"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["kind"] == "text"
    fleeting = payload["fleeting"]
    assert fleeting["family"] == "fleeting"
    assert fleeting["filename"].startswith("- ")
    assert " inbox " in fleeting["filename"]
    assert "inbox" in fleeting["tags"]


def test_inbox_add_file_creates_fleeting_and_attachment(local_env, tmp_path) -> None:
    photo = tmp_path / "page-47.jpg"
    photo.write_bytes(b"fake image bytes")
    code, out = _invoke(
        [
            "inbox",
            "add",
            "--note",
            "p.47 of book",
            "--json",
            "--",
            str(photo),
        ]
    )
    assert code == 0, out
    payload = json.loads(out)
    assert payload["kind"] == "file"
    assert "[[" in _read_body(payload["fleeting"]["id"])
    attachment = payload["attachment"]
    assert attachment["family"] == "file"
    assert attachment["filename"].startswith(_today_prefix() + "+ inbox ")
    assert attachment["filename"].endswith(".jpg")
    assert attachment["upload"]["storage_key"]


def test_inbox_add_url_uses_stub_title(local_env, monkeypatch) -> None:
    from knoten.cli import inbox

    monkeypatch.setattr(inbox, "_fetch_url_title", lambda url, **_: "Example Page")
    code, out = _invoke(["inbox", "add", "--json", "--", "https://example.com/article"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["kind"] == "url"
    assert payload["title"] == "Example Page"
    assert "example-page" in payload["fleeting"]["filename"]
    body = _read_body(payload["fleeting"]["id"])
    assert "https://example.com/article" in body
    assert "[Example Page]" in body


def test_inbox_append_text_to_existing_fleeting(local_env) -> None:
    code, out = _invoke(["inbox", "add", "--json", "--", "first capture"])
    fleeting_id = json.loads(out)["fleeting"]["id"]

    code, out = _invoke(
        ["inbox", "append", "--note", "added later", "--json", "--", fleeting_id, "second piece"]
    )
    assert code == 0, out
    payload = json.loads(out)
    assert payload["kind"] == "text"
    body = _read_body(fleeting_id)
    assert "first capture" in body
    assert "second piece" in body
    assert "added later" in body


def test_inbox_append_file_creates_attachment_and_links_it(local_env, tmp_path) -> None:
    code, out = _invoke(["inbox", "add", "--json", "--", "first capture"])
    fleeting_id = json.loads(out)["fleeting"]["id"]

    photo = tmp_path / "next-page.jpg"
    photo.write_bytes(b"second photo bytes")
    code, out = _invoke(["inbox", "append", "--json", "--", fleeting_id, str(photo)])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["kind"] == "file"
    body = _read_body(fleeting_id)
    assert "[[" + _today_prefix() + "+ inbox next-page" in body


def test_inbox_list_shows_inbox_and_excludes_promoted(local_env) -> None:
    _invoke(["inbox", "add", "--json", "--", "still in inbox"])
    code, out = _invoke(["inbox", "add", "--json", "--", "to be promoted"])
    promoted_id = json.loads(out)["fleeting"]["id"]
    code, out = _invoke(
        [
            "edit",
            "--add-tag",
            "inbox-promoted",
            "--remove-tag",
            "inbox",
            "--json",
            "--",
            promoted_id,
        ]
    )
    assert code == 0, out

    code, out = _invoke(["inbox", "list", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    titles = {n["filename"] for n in payload["notes"]}
    assert any("still-in-inbox" in t for t in titles)
    assert not any("to-be-promoted" in t for t in titles)
    assert payload["total"] == len(payload["notes"])


def test_inbox_add_text_rejects_empty(local_env) -> None:
    code, out = _invoke(["inbox", "add", "--json", "--", "   "])
    assert code == 1, out
    payload = json.loads(out)
    assert payload["error"] == "user"


def test_inbox_add_force_modes_are_mutually_exclusive(local_env) -> None:
    code, out = _invoke(["inbox", "add", "--as-file", "--as-text", "--json", "--", "anything"])
    assert code == 1, out
    payload = json.loads(out)
    assert payload["error"] == "user"


# ---- helpers -----------------------------------------------------------


def _read_body(note_id: str) -> str:
    code, out = _invoke(["read", "--json", "--", note_id])
    assert code == 0, out
    payload = json.loads(out)
    return payload["body"]


def _today_prefix() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")
