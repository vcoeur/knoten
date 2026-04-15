"""End-to-end CLI tests in local mode — no network, no httpx mocks.

Drives `kasten` commands against a disposable vault under `tmp_path`
with `KASTEN_API_URL=""` so the CLI picks `LocalBackend`. Covers every
mutation + read path so a regression in `_build_backend` wiring or the
LocalBackend implementation surfaces immediately.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from app.cli.main import app


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    """Point KASTEN_* at a per-test tmp dir with no API URL — forces local mode."""
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_URL", "")
    monkeypatch.setenv("KASTEN_API_TOKEN", "")
    monkeypatch.setenv("KASTEN_MODE", "local")
    return tmp_path


def _invoke(args: list[str]) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout


def test_create_read_cycle_without_network(local_env) -> None:
    code, out = _invoke(
        ["create", "--filename", "- Local test", "--body", "hello from local", "--json"]
    )
    assert code == 0, out
    payload = json.loads(out)
    note_id = payload["id"]
    assert payload["filename"] == "- Local test"

    code, out = _invoke(["read", "--json", "--", note_id])
    assert code == 0, out
    payload = json.loads(out)
    assert "hello from local" in payload["body"]


def test_list_shows_created_note(local_env) -> None:
    _invoke(["create", "--filename", "- First", "--body", "a", "--json"])
    _invoke(["create", "--filename", "- Second", "--body", "b", "--json"])

    code, out = _invoke(["list", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    filenames = {row["filename"] for row in payload["notes"]}
    assert "- First" in filenames
    assert "- Second" in filenames


def test_edit_body_in_place(local_env) -> None:
    code, out = _invoke(["create", "--filename", "- Editable", "--body", "v1", "--json"])
    payload = json.loads(out)
    note_id = payload["id"]

    code, out = _invoke(["edit", "--body", "v2", "--json", "--", note_id])
    assert code == 0, out

    code, out = _invoke(["read", "--json", "--", note_id])
    payload = json.loads(out)
    assert "v2" in payload["body"]
    assert "v1" not in payload["body"]


def test_rename_cascades_to_referencing_note(local_env) -> None:
    code, out = _invoke(["create", "--filename", "- Target", "--body", "target", "--json"])
    target_id = json.loads(out)["id"]
    code, out = _invoke(
        [
            "create",
            "--filename",
            "- Referrer",
            "--body",
            "See [[- Target]] for details.",
            "--json",
        ]
    )
    referrer_id = json.loads(out)["id"]

    code, out = _invoke(["rename", "--json", "--", target_id, "- Target Renamed"])
    assert code == 0, out

    code, out = _invoke(["read", "--json", "--", referrer_id])
    payload = json.loads(out)
    assert "[[- Target Renamed]]" in payload["body"]
    assert "[[- Target]]" not in payload["body"]


def test_delete_then_restore_round_trip(local_env) -> None:
    code, out = _invoke(["create", "--filename", "- Ephemeral", "--body", "will vanish", "--json"])
    note_id = json.loads(out)["id"]

    code, out = _invoke(["delete", "--yes", "--json", "--", note_id])
    assert code == 0, out

    # After delete, read raises not-found.
    code, _ = _invoke(["read", "--json", "--", note_id])
    assert code != 0

    code, out = _invoke(["restore", "--json", note_id])
    assert code == 0, out

    code, out = _invoke(["read", "--json", "--", note_id])
    assert code == 0, out
    payload = json.loads(out)
    assert "will vanish" in payload["body"]


def test_sync_in_local_mode_is_a_reindex_walk(local_env) -> None:
    _invoke(["create", "--filename", "- One", "--body", "x", "--json"])
    code, out = _invoke(["sync", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload.get("mode") == "local"
    assert payload.get("total", 0) >= 1


def test_append_adds_to_existing_body(local_env) -> None:
    code, out = _invoke(["create", "--filename", "- Log", "--body", "line 1", "--json"])
    note_id = json.loads(out)["id"]

    code, out = _invoke(["append", "--content", "line 2", "--json", "--", note_id])
    assert code == 0, out

    code, out = _invoke(["read", "--json", "--", note_id])
    payload = json.loads(out)
    assert "line 1" in payload["body"]
    assert "line 2" in payload["body"]
