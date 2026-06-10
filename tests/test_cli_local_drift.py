"""Test the bidirectional-sync behaviour added in v0.4.0.

When the vault has a `last_sync_at` recorded but `KNOTEN_API_URL` is empty
(config drift), writes still succeed locally — but each new row is marked
`synced=0`. The next remote sync's push pass drains the queue.
A stderr banner surfaces the degraded mode for every non-config command.
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
    try:
        err = result.stderr
    except (AttributeError, ValueError):
        err = ""
    return result.exit_code, result.stdout, err or ""


def test_create_succeeds_in_drift_state(auto_mode_no_api_url, monkeypatch) -> None:
    """Writes are not blocked — local-only writes are legitimate."""
    _seed_state_with_prior_sync(monkeypatch, auto_mode_no_api_url)
    code, out, _err = _invoke(["create", "--filename", "- Drift test", "--body", "x", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["filename"] == "- Drift test"


def _read_synced(note_id: str) -> int:
    """Open the live SQLite mirror via paths.resolve() and return synced for note_id."""
    from knoten.paths import resolve

    paths = resolve()
    import sqlite3

    with sqlite3.connect(paths.index_path) as conn:
        row = conn.execute("SELECT synced FROM notes WHERE id = ?", (note_id,)).fetchone()
    return int(row[0]) if row else -1


def test_create_marks_row_synced_zero_in_drift_state(auto_mode_no_api_url, monkeypatch) -> None:
    """Notes created in drifted local mode get `synced=0` for later push."""
    _seed_state_with_prior_sync(monkeypatch, auto_mode_no_api_url)
    code, out, _ = _invoke(["create", "--filename", "- Drift sync test", "--body", "x", "--json"])
    assert code == 0, out
    note_id = json.loads(out)["id"]
    assert _read_synced(note_id) == 0


def test_create_on_fresh_vault_marks_synced_zero(auto_mode_no_api_url) -> None:
    """Even on a fresh vault with no prior sync, local writes track unsynced state."""
    code, out, _ = _invoke(["create", "--filename", "- Fresh", "--body", "x", "--json"])
    assert code == 0, out
    note_id = json.loads(out)["id"]
    assert _read_synced(note_id) == 0


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


# ---- local-mode read commands run the stat walk (M10) -------------------


def _local_mode(monkeypatch) -> None:
    monkeypatch.setenv("KNOTEN_MODE", "local")
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")


def test_search_sees_external_edit_without_sync(monkeypatch) -> None:
    """`search` in local mode must run the stat walk before querying."""
    _local_mode(monkeypatch)
    code, out, _ = _invoke(
        ["create", "--filename", "- Walk target", "--body", "original", "--json"]
    )
    assert code == 0, out
    payload = json.loads(out)
    note_file = Path(payload["absolute_path"])

    # External edit — no knoten write in between.
    note_file.write_text(
        note_file.read_text(encoding="utf-8") + "\nzebraxylo\n",
        encoding="utf-8",
    )

    code, out, _ = _invoke(["search", "zebraxylo", "--json"])
    assert code == 0, out
    hits = json.loads(out)["hits"]
    assert [h["id"] for h in hits] == [payload["id"]]


def test_list_sees_external_delete_without_sync(monkeypatch) -> None:
    """`list` in local mode must drop rows whose file was externally removed."""
    _local_mode(monkeypatch)
    code, out, _ = _invoke(["create", "--filename", "- Doomed note", "--body", "x", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    Path(payload["absolute_path"]).unlink()

    code, out, _ = _invoke(["list", "--json"])
    assert code == 0, out
    listing = json.loads(out)
    assert payload["id"] not in {n["id"] for n in listing["notes"]}
    assert listing["total"] == 0
