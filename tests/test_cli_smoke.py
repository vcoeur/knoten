"""Smoke tests for the Typer CLI — exercises `status` and `config`.

These commands do not touch the network, so they are safe to run without a
live remote. They confirm that the wiring from CLI → Settings → Store works
end-to-end and that JSON output mode is parseable.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from knoten.cli.main import app


def test_status_json(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["api_url"] == "https://notes.test"
    assert payload["local_total"] == 0


def test_config_json_redacts_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_secret_xyz")
    runner = CliRunner()
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["api_token"].startswith("nt_")
    assert "secret" not in payload["api_token"]


def test_list_empty_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["total"] == 0
    assert payload["notes"] == []


def test_citekeys_empty_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["citekeys", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"citekeys": [], "count": 0, "prefix": None}


def test_reference_missing_from_source_is_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    # --from-source is required; omitting it is a usage error (exit code 2).
    result = runner.invoke(app, ["reference", "--json"])
    assert result.exit_code != 0


def test_upload_smoke_missing_file_is_user_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["upload", str(tmp_path / "missing.pdf"), "--filename", "2024-11-10+ x.pdf", "--json"],
    )
    # Typer's built-in exists=True check fires first → exit code 2.
    assert result.exit_code != 0


def test_download_smoke_no_such_note(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["download", "nonexistent", "--json"])
    assert result.exit_code == 1


def test_search_fuzzy_empty_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["search", "anything", "--fuzzy", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source"] == "local-fuzzy"
    assert payload["hits"] == []


def test_similar_no_such_note(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["similar", "nonexistent", "--json"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"] == "not_found"


def test_missing_token_is_config_error(monkeypatch, tmp_path) -> None:
    # Force remote mode so the missing-token path is exercised; without an
    # api_url the CLI would resolve to local mode and skip the token check.
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 4, result.output
    assert "KNOTEN_API_TOKEN" in result.output or "KNOTEN_API_TOKEN" in (result.stderr or "")


def test_error_envelope_config_error_json(monkeypatch, tmp_path) -> None:
    """ConfigError with --json must emit a parseable error envelope on stdout."""
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--json"])
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "config"
    assert payload["code"] == 4
    assert "KNOTEN_API_TOKEN" in payload["message"]


def test_error_envelope_not_found_json(monkeypatch, tmp_path) -> None:
    """A NotFoundError from `read` with --json emits a structured envelope."""
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["read", "definitely-not-a-real-note", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_found"
    assert payload["code"] == 1
    assert "message" in payload


def test_kind_filter_hint_when_family_has_matches(monkeypatch, tmp_path) -> None:
    """`--kind reference` with no hits hints at `--family reference` when the family has rows."""
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    runner.invoke(
        app,
        ["create", "--filename", "Bollier2025= Foo", "--body", "x", "--kind", "book", "--json"],
    )
    result = runner.invoke(app, ["list", "--kind", "reference", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["total"] == 0
    assert "hint" in payload
    assert "--family reference" in payload["hint"]


def test_error_envelope_plaintext_without_json(monkeypatch, tmp_path) -> None:
    """Without --json, errors emit plain text and no JSON envelope."""
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 4
    # The error message is present, but not wrapped in a JSON envelope.
    assert "KNOTEN_API_TOKEN" in result.output
    assert '"error":' not in result.output


# ---- contract fixes ------------------------------------------------------


def test_delete_abort_exits_zero(monkeypatch) -> None:
    """Declining the delete confirmation is a successful no-op, not an error."""
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["delete", "--", "- Anything"], input="n\n")
    assert result.exit_code == 0, result.output
    assert '"error"' not in result.stdout


def test_create_dry_run_needs_no_token(monkeypatch) -> None:
    """Dry-run is local-only — it must run before the token gate (like reference)."""
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    runner = CliRunner()
    result = runner.invoke(
        app, ["create", "--filename", "- Dry", "--body", "x", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["operation"] == "create"


def test_create_batch_dry_run_needs_no_token(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["create", "--batch", "-", "--dry-run", "--json"],
        input='[{"filename": "- Batch dry"}]',
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["operation"] == "create-batch"
    assert payload["dry_run"] is True
    assert payload["count"] == 1


def test_edit_dry_run_needs_no_token(monkeypatch) -> None:
    runner = CliRunner()
    # Create the note in local mode first…
    monkeypatch.setenv("KNOTEN_MODE", "local")
    result = runner.invoke(app, ["create", "--filename", "- Editable", "--body", "x", "--json"])
    assert result.exit_code == 0, result.output
    note_id = json.loads(result.stdout)["id"]
    # …then flip to remote mode with no token: dry-run must still succeed.
    monkeypatch.setenv("KNOTEN_MODE", "remote")
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    result = runner.invoke(app, ["edit", note_id, "--body", "y", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["operation"] == "edit"


def test_graph_invalid_direction_is_user_error(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["graph", "anything", "--direction", "sideways", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "user"
    assert "--direction" in payload["message"]


def test_sync_local_mode_json_shape_and_plain_rendering(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["create", "--filename", "- Synced local", "--body", "x", "--json"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["sync", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "local"
    assert payload["total"] == 1
    assert "message" in payload

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "local" in result.output
    # The remote-shaped tables must not render for a local stat-walk.
    assert "Remote" not in result.output


def test_init_creates_private_env_file(monkeypatch) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "env_file" in result.output

    from knoten.paths import resolve

    paths = resolve()
    assert (paths.env_file.stat().st_mode & 0o777) == 0o600
    assert (paths.config_dir.stat().st_mode & 0o777) == 0o700


# ---- --json-on-empty-store smoke (M16) -----------------------------------


def test_verify_json_local_mode_empty_store(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["verify", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "local"
    assert payload["integrity"] == "ok"
    assert payload["cardinality"]["consistent"] is True


def test_reindex_json_empty_store(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["reindex", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["integrity"] == "ok"
    assert payload["checked"] == 0
    assert payload["reindexed"] == 0


def test_graph_json_smoke(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["create", "--filename", "- Graph node", "--body", "x", "--json"])
    assert result.exit_code == 0, result.output
    note_id = json.loads(result.stdout)["id"]
    result = runner.invoke(app, ["graph", note_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["start"] == note_id
    assert "nodes" in payload
    assert "edges" in payload
    assert "broken_targets" in payload


def test_backlinks_json_smoke(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["create", "--filename", "- Lonely", "--body", "x", "--json"])
    assert result.exit_code == 0, result.output
    note_id = json.loads(result.stdout)["id"]
    result = runner.invoke(app, ["backlinks", note_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["id"] == note_id
    assert payload["total"] == 0
    assert payload["backlinks"] == []


def test_trash_json_empty_store(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["trash", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"data": [], "total": 0}


def test_trash_lists_deleted_note_and_restore_roundtrips(monkeypatch) -> None:
    """Local mode: delete a note, see it in `trash`, restore it by its id."""
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["create", "--filename", "- Trashable", "--body", "x", "--json"])
    assert result.exit_code == 0, result.output
    note_id = json.loads(result.stdout)["id"]

    result = runner.invoke(app, ["delete", "--yes", "--json", "--", note_id])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["deleted_id"] == note_id

    result = runner.invoke(app, ["trash", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["total"] == 1
    row = payload["data"][0]
    assert row["id"] == note_id
    assert row["filename"] == "- Trashable"
    assert row["deleted_at"]  # soft-delete timestamp present

    result = runner.invoke(app, ["restore", note_id, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == note_id

    # Trash is empty again; the note is back in the active list.
    result = runner.invoke(app, ["trash", "--json"])
    assert json.loads(result.stdout)["total"] == 0


def test_trash_minimal_fields(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["create", "--filename", "- Min", "--body", "x", "--json"])
    note_id = json.loads(result.stdout)["id"]
    runner.invoke(app, ["delete", "--yes", "--json", "--", note_id])
    result = runner.invoke(app, ["trash", "--fields", "minimal", "--json"])
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)["data"][0]
    assert set(row.keys()) == {"id", "filename", "deleted_at"}


def test_tags_json_empty_store(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["tags", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"tags": []}


def test_kinds_json_empty_store(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    runner = CliRunner()
    result = runner.invoke(app, ["kinds", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"kinds": []}


# ---- exit-code / error-envelope contract ----------------------------------


@pytest.mark.parametrize(
    ("scenario", "args", "expected_error", "expected_code"),
    [
        ("ambiguous", ["read", "--json", "--", "- alpha"], "ambiguous_target", 1),
        ("not_found", ["read", "definitely-missing", "--json"], "not_found", 1),
        ("config", ["sync", "--json"], "config", 4),
    ],
)
def test_error_envelope_kinds_and_exit_codes(
    monkeypatch, scenario, args, expected_error, expected_code
) -> None:
    runner = CliRunner()
    if scenario == "ambiguous":
        monkeypatch.setenv("KNOTEN_MODE", "local")
        for name in ("- alpha one", "- alpha two"):
            result = runner.invoke(app, ["create", "--filename", name, "--body", "x", "--json"])
            assert result.exit_code == 0, result.output
    elif scenario == "not_found":
        monkeypatch.setenv("KNOTEN_MODE", "local")
    else:  # config — remote mode without a token
        monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
        monkeypatch.setenv("KNOTEN_API_TOKEN", "")

    result = runner.invoke(app, args)
    assert result.exit_code == expected_code, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == expected_error
    assert payload["code"] == expected_code
    if scenario == "ambiguous":
        assert len(payload["candidates"]) == 2
