"""Smoke tests for the Typer CLI — exercises `status` and `config`.

These commands do not touch the network, so they are safe to run without a
live remote. They confirm that the wiring from CLI → Settings → Store works
end-to-end and that JSON output mode is parseable.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from app.cli.main import app


def test_status_json(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KASTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["api_url"] == "https://notes.test"
    assert payload["local_total"] == 0


def test_config_json_redacts_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_TOKEN", "nt_secret_xyz")
    runner = CliRunner()
    result = runner.invoke(app, ["config", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["api_token"].startswith("nt_")
    assert "secret" not in payload["api_token"]


def test_list_empty_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["total"] == 0
    assert payload["notes"] == []


def test_upload_smoke_missing_file_is_user_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KASTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["upload", str(tmp_path / "missing.pdf"), "--filename", "2024-11-10+ x.pdf", "--json"],
    )
    # Typer's built-in exists=True check fires first → exit code 2.
    assert result.exit_code != 0


def test_download_smoke_no_such_note(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KASTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["download", "nonexistent", "--json"])
    assert result.exit_code == 1


def test_search_fuzzy_empty_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["search", "anything", "--fuzzy", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source"] == "local-fuzzy"
    assert payload["hits"] == []


def test_missing_token_is_config_error(monkeypatch, tmp_path) -> None:
    # Explicitly set to "" so environs does not fall back to the repo-level
    # .env (which may or may not exist depending on the user's setup).
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_TOKEN", "")
    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 4, result.output
    assert "KASTEN_API_TOKEN" in result.output or "KASTEN_API_TOKEN" in (result.stderr or "")


def test_error_envelope_config_error_json(monkeypatch, tmp_path) -> None:
    """ConfigError with --json must emit a parseable error envelope on stdout."""
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_TOKEN", "")
    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--json"])
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "config"
    assert payload["code"] == 4
    assert "KASTEN_API_TOKEN" in payload["message"]


def test_error_envelope_not_found_json(monkeypatch, tmp_path) -> None:
    """A NotFoundError from `read` with --json emits a structured envelope."""
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_TOKEN", "nt_test")
    runner = CliRunner()
    result = runner.invoke(app, ["read", "definitely-not-a-real-note", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_found"
    assert payload["code"] == 1
    assert "message" in payload


def test_error_envelope_plaintext_without_json(monkeypatch, tmp_path) -> None:
    """Without --json, errors emit plain text and no JSON envelope."""
    monkeypatch.setenv("KASTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KASTEN_API_TOKEN", "")
    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 4
    # The error message is present, but not wrapped in a JSON envelope.
    assert "KASTEN_API_TOKEN" in result.output
    assert '"error":' not in result.output
