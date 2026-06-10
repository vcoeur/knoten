"""End-to-end CLI tests in local mode — no network, no httpx mocks.

Drives `knoten` commands against a disposable vault under `tmp_path`
with `KNOTEN_API_URL=""` so the CLI picks `LocalBackend`. Covers every
mutation + read path so a regression in `_build_backend` wiring or the
LocalBackend implementation surfaces immediately.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from knoten.cli.main import app


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    """Point KNOTEN_* at a per-test tmp dir with no API URL — forces local mode."""
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    monkeypatch.setenv("KNOTEN_MODE", "local")
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


def test_similar_finds_related_note(local_env) -> None:
    _invoke(
        [
            "create",
            "--filename",
            "- Encryption basics",
            "--body",
            "Symmetric encryption uses shared keys and ciphers for confidentiality.",
            "--json",
        ]
    )
    _invoke(
        [
            "create",
            "--filename",
            "- Cipher design",
            "--body",
            "Block ciphers and stream ciphers rely on keys for encryption.",
            "--json",
        ]
    )
    _invoke(["create", "--filename", "- Garden log", "--body", "Tomatoes need sunlight.", "--json"])

    code, out = _invoke(["similar", "--json", "--", "- Encryption basics"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["target_filename"] == "- Encryption basics"
    assert payload["derived_query"]
    filenames = {hit["filename"] for hit in payload["hits"]}
    assert "- Encryption basics" not in filenames  # self excluded
    assert "- Cipher design" in filenames
    assert payload["total"] == len(payload["hits"])


def test_similar_respects_limit(local_env) -> None:
    for index in range(4):
        _invoke(
            [
                "create",
                "--filename",
                f"- Note {index}",
                "--body",
                "shared keyword routing networking protocol packets",
                "--json",
            ]
        )
    code, out = _invoke(["similar", "--limit", "2", "--json", "--", "- Note 0"])
    assert code == 0, out
    payload = json.loads(out)
    assert len(payload["hits"]) <= 2


def test_search_in_column_scope(local_env) -> None:
    _invoke(["create", "--filename", "- Zephyr title", "--body", "nothing here", "--json"])
    _invoke(["create", "--filename", "- Plain", "--body", "mentions zephyr in body", "--json"])

    # Scope to title: only the note with zephyr in the title/filename matches.
    code, out = _invoke(["search", "zephyr", "--in", "title", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["scope"] == ["title"]
    assert {hit["filename"] for hit in payload["hits"]} == {"- Zephyr title"}

    # Comma-separated form is accepted and de-duplicated.
    code, out = _invoke(["search", "zephyr", "--in", "title,body", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["scope"] == ["title", "body"]
    assert payload["total"] == 2


def test_search_in_invalid_column_is_user_error(local_env) -> None:
    code, out = _invoke(["search", "anything", "--in", "nope", "--json"])
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


def test_search_in_with_fuzzy_is_user_error(local_env) -> None:
    code, out = _invoke(["search", "anything", "--in", "title", "--fuzzy", "--json"])
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


def test_search_zero_hit_fuzzy_hint(local_env) -> None:
    _invoke(["create", "--filename", "- Encryption handbook", "--body", "ciphers", "--json"])
    # A typo gets 0 ranked hits but the fuzzy probe finds the note.
    code, out = _invoke(["search", "encrpytion", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["total"] == 0
    assert payload["fuzzy_total"] >= 1
    assert "--fuzzy" in payload["hint"]


def test_list_updated_after_filter_and_echo(local_env) -> None:
    _invoke(["create", "--filename", "- One", "--body", "a", "--json"])
    # A future bound excludes everything; the filter is echoed in the payload.
    code, out = _invoke(["list", "--updated-after", "2999-01-01", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["total"] == 0
    assert payload["updated_after"] == "2999-01-01"

    # A past bound keeps the note.
    code, out = _invoke(["list", "--updated-after", "2000-01-01", "--json"])
    assert json.loads(out)["total"] == 1


def test_list_created_after_invalid_is_user_error(local_env) -> None:
    code, out = _invoke(["list", "--created-after", "not-a-date", "--json"])
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


def test_verify_local_mode_is_non_destructive(local_env) -> None:
    """`knoten verify` in local mode must not sweep `.trash/` / `.attachments/` (C1).

    Before the fix, verify ran `reconcile_local` in either mode; in local
    mode the orphan sweep unlinked every trashed note and attachment blob,
    making subsequent `restore` fail permanently.
    """
    from pathlib import Path

    code, out = _invoke(["create", "--filename", "- Keeper", "--body", "kept", "--json"])
    assert code == 0, out
    keeper_id = json.loads(out)["id"]
    code, out = _invoke(["create", "--filename", "- Doomed", "--body", "gone", "--json"])
    assert code == 0, out
    doomed_id = json.loads(out)["id"]
    code, out = _invoke(["delete", "--yes", "--json", "--", doomed_id])
    assert code == 0, out

    code, out = _invoke(["read", "--json", "--", keeper_id])
    assert code == 0, out
    vault_dir = Path(json.loads(out)["absolute_path"]).parent.parent
    # Trash names embed the note id (C2 fix), so glob for the single copy.
    trash_file = vault_dir / ".trash" / "note" / f"- Doomed.{doomed_id}.md"
    assert trash_file.exists()
    blob = vault_dir / ".attachments" / "fakeblob.pdf"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"%PDF-1.4 fake blob")

    code, out = _invoke(["verify", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["mode"] == "local"
    assert payload["integrity"] == "ok"
    assert payload["cardinality"]["consistent"] is True
    # No reconcile ran — the remote-mode repair keys are absent.
    assert "orphans_removed" not in payload

    assert trash_file.exists()
    assert blob.exists()
    # The trashed note is still restorable after verify.
    code, out = _invoke(["restore", "--json", doomed_id])
    assert code == 0, out
