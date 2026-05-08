"""Test the config-drift guard added in v0.4.0.

When the vault has a `last_sync_at` recorded but `KNOTEN_API_URL` is empty,
writes must error rather than land silently in the local-only vault.
Mirrors the data-loss scenario from `projects/2026-05/2026-05-08-knoten-fixes`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from knoten.cli.main import app


@pytest.fixture
def auto_mode_no_api_url(monkeypatch, tmp_path: Path) -> Path:
    """Default `KNOTEN_MODE` (auto) with an empty API URL — the silent-loss config."""
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    monkeypatch.delenv("KNOTEN_MODE", raising=False)
    return tmp_path


def _seed_state_with_prior_sync(monkeypatch, tmp_path: Path) -> None:
    """Drop a `state.json` indicating a prior remote sync into the sandbox cache dir."""
    cache_dir = tmp_path / ".sandbox-paths" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "last_sync_at": "2026-05-08T10:10:57Z",
        "last_sync_max_updated_at": "2026-05-08T09:53:21Z",
        "last_full_sync_at": None,
        "last_remote_total": 100,
    }
    (cache_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _invoke(args: list[str]) -> tuple[int, str, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    # Newer click >=8.2: result.stderr is always separately captured.
    # Older versions need mix_stderr=False; we accept either by trying both.
    try:
        err = result.stderr
    except (AttributeError, ValueError):
        err = ""
    return result.exit_code, result.stdout, err or ""


def test_create_blocked_when_prior_sync_but_url_empty(auto_mode_no_api_url, monkeypatch) -> None:
    _seed_state_with_prior_sync(monkeypatch, auto_mode_no_api_url)
    code, out, _err = _invoke(
        ["create", "--filename", "- Drift test", "--body", "x", "--json"]
    )
    assert code == 4, out
    payload = json.loads(out)
    assert payload["error"] == "config"
    assert "previously synced" in payload["message"]
    assert "create" in payload["message"]


def test_create_allowed_on_fresh_vault_with_no_prior_sync(auto_mode_no_api_url) -> None:
    code, out, _err = _invoke(
        ["create", "--filename", "- Fresh", "--body", "x", "--json"]
    )
    assert code == 0, out
    payload = json.loads(out)
    assert payload["filename"] == "- Fresh"


def test_create_allowed_when_explicit_local_mode(monkeypatch, tmp_path: Path) -> None:
    _seed_state_with_prior_sync(monkeypatch, tmp_path)
    monkeypatch.setenv("KNOTEN_MODE", "local")
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    code, out, _err = _invoke(
        ["create", "--filename", "- Explicit local", "--body", "x", "--json"]
    )
    assert code == 0, out


def test_read_emits_local_mode_banner_when_drift_detected(
    auto_mode_no_api_url, monkeypatch
) -> None:
    _seed_state_with_prior_sync(monkeypatch, auto_mode_no_api_url)
    code, _out, err = _invoke(["list", "--json"])
    assert code == 0
    assert "local-only mode" in err
    assert "previously synced" in err


def test_no_banner_on_fresh_vault(auto_mode_no_api_url) -> None:
    code, _out, err = _invoke(["list", "--json"])
    assert code == 0
    assert "warning" not in err.lower()


def test_no_banner_when_explicit_local_mode(monkeypatch, tmp_path: Path) -> None:
    _seed_state_with_prior_sync(monkeypatch, tmp_path)
    monkeypatch.setenv("KNOTEN_MODE", "local")
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    code, _out, err = _invoke(["list", "--json"])
    assert code == 0
    assert "warning" not in err.lower()


def test_edit_blocked_in_drift_state(auto_mode_no_api_url, monkeypatch) -> None:
    # First create a note in fresh state, then seed drift, then try to edit.
    code, out, _ = _invoke(["create", "--filename", "- Pre-drift", "--body", "v1", "--json"])
    assert code == 0
    note_id = json.loads(out)["id"]
    _seed_state_with_prior_sync(monkeypatch, auto_mode_no_api_url)
    code, out, _ = _invoke(["edit", "--body", "v2", "--json", "--", note_id])
    assert code == 4
    assert json.loads(out)["error"] == "config"


def test_append_blocked_in_drift_state(auto_mode_no_api_url, monkeypatch) -> None:
    code, out, _ = _invoke(["create", "--filename", "- Pre-drift", "--body", "v1", "--json"])
    assert code == 0
    note_id = json.loads(out)["id"]
    _seed_state_with_prior_sync(monkeypatch, auto_mode_no_api_url)
    code, out, _ = _invoke(["append", "--content", "more", "--json", "--", note_id])
    assert code == 4


def test_delete_blocked_in_drift_state(auto_mode_no_api_url, monkeypatch) -> None:
    code, out, _ = _invoke(["create", "--filename", "- Doomed", "--body", "v1", "--json"])
    assert code == 0
    note_id = json.loads(out)["id"]
    _seed_state_with_prior_sync(monkeypatch, auto_mode_no_api_url)
    code, out, _ = _invoke(["delete", "--yes", "--json", "--", note_id])
    assert code == 4
