"""Tests for the one-shot legacy-layout migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from knoten.migrate import migrate_legacy_layout
from knoten.paths import Paths


def _paths_under(root: Path, *, is_dev: bool = False) -> Paths:
    return Paths(
        config_dir=root / "new_cfg",
        data_dir=root / "new_data",
        cache_dir=root / "new_cache",
        env_file=root / "new_cfg" / ".env",
        vault_dir=root / "new_data" / "kasten",
        index_path=root / "new_cache" / "index.sqlite",
        state_file=root / "new_cache" / "state.json",
        lock_file=root / "new_cache" / "sync.lock",
        tmp_dir=root / "new_cache" / "tmp",
        is_dev=is_dev,
    )


def _seed_legacy(
    home: Path,
    *,
    vault: bool = True,
    index: bool = True,
    state: bool = True,
    env: bool = True,
) -> None:
    """Seed whichever legacy artifacts the caller asks for under `home/.knoten`."""
    legacy_root = home / ".knoten"
    if vault:
        vault_dir = legacy_root / "kasten" / "note"
        vault_dir.mkdir(parents=True)
        (vault_dir / "! First.md").write_text("legacy body\n", encoding="utf-8")
    if index:
        state_dir = legacy_root / ".knoten-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "index.sqlite").write_bytes(b"legacy-sqlite-bytes")
    if state:
        state_dir = legacy_root / ".knoten-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text('{"cursor": "2024-01-01"}', encoding="utf-8")
    if env:
        legacy_cfg = home / ".config" / "knoten"
        legacy_cfg.mkdir(parents=True, exist_ok=True)
        (legacy_cfg / ".env").write_text(
            "KNOTEN_API_URL=https://notes.example.com\n", encoding="utf-8"
        )


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point `Path.home()` at a throwaway directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KNOTEN_HOME", raising=False)
    return home


def test_migrates_vault(fake_home: Path, tmp_path: Path) -> None:
    _seed_legacy(fake_home, index=False, state=False, env=False)
    paths = _paths_under(tmp_path / "new")

    moved = migrate_legacy_layout(paths)

    assert len(moved) == 1
    assert (paths.vault_dir / "note" / "! First.md").read_text() == "legacy body\n"
    assert not (fake_home / ".knoten" / "kasten").exists()


def test_migrates_index(fake_home: Path, tmp_path: Path) -> None:
    _seed_legacy(fake_home, vault=False, state=False, env=False)
    paths = _paths_under(tmp_path / "new")

    moved = migrate_legacy_layout(paths)

    assert len(moved) == 1
    assert paths.index_path.read_bytes() == b"legacy-sqlite-bytes"
    assert not (fake_home / ".knoten" / ".knoten-state" / "index.sqlite").exists()


def test_migrates_state_json(fake_home: Path, tmp_path: Path) -> None:
    _seed_legacy(fake_home, vault=False, index=False, env=False)
    paths = _paths_under(tmp_path / "new")

    moved = migrate_legacy_layout(paths)

    assert len(moved) == 1
    assert paths.state_file.read_text() == '{"cursor": "2024-01-01"}'


def test_migrates_env_when_target_differs(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_legacy(fake_home, vault=False, index=False, state=False)
    # Config dir outside ~/.config/knoten so the source and target are distinct.
    paths = _paths_under(tmp_path / "new")

    moved = migrate_legacy_layout(paths)

    assert len(moved) == 1
    assert "KNOTEN_API_URL=https://notes.example.com" in paths.env_file.read_text()
    assert not (fake_home / ".config" / "knoten" / ".env").exists()


def test_migrates_everything(fake_home: Path, tmp_path: Path) -> None:
    _seed_legacy(fake_home)
    paths = _paths_under(tmp_path / "new")

    moved = migrate_legacy_layout(paths)

    assert len(moved) == 4
    assert (paths.vault_dir / "note" / "! First.md").exists()
    assert paths.index_path.exists()
    assert paths.state_file.exists()
    assert paths.env_file.exists()
    # Legacy dirs cleaned up where empty.
    assert not (fake_home / ".knoten" / ".knoten-state").exists()
    assert not (fake_home / ".knoten").exists()


def test_second_call_is_noop(fake_home: Path, tmp_path: Path) -> None:
    _seed_legacy(fake_home)
    paths = _paths_under(tmp_path / "new")

    first = migrate_legacy_layout(paths)
    second = migrate_legacy_layout(paths)

    assert len(first) == 4
    assert second == []


def test_skips_when_target_vault_nonempty(fake_home: Path, tmp_path: Path) -> None:
    _seed_legacy(fake_home, index=False, state=False, env=False)
    paths = _paths_under(tmp_path / "new")
    paths.vault_dir.mkdir(parents=True)
    (paths.vault_dir / "existing.md").write_text("existing content\n", encoding="utf-8")

    moved = migrate_legacy_layout(paths)

    assert moved == []
    # Target preserved.
    assert (paths.vault_dir / "existing.md").read_text() == "existing content\n"
    # Legacy still there.
    assert (fake_home / ".knoten" / "kasten" / "note" / "! First.md").exists()


def test_skips_when_target_index_exists(fake_home: Path, tmp_path: Path) -> None:
    _seed_legacy(fake_home, vault=False, state=False, env=False)
    paths = _paths_under(tmp_path / "new")
    paths.cache_dir.mkdir(parents=True)
    paths.index_path.write_bytes(b"existing-sqlite")

    moved = migrate_legacy_layout(paths)

    assert moved == []
    assert paths.index_path.read_bytes() == b"existing-sqlite"


def test_skips_when_nothing_legacy(fake_home: Path, tmp_path: Path) -> None:
    paths = _paths_under(tmp_path / "new")
    moved = migrate_legacy_layout(paths)
    assert moved == []


def test_dev_mode_skips_migration(fake_home: Path, tmp_path: Path) -> None:
    """Dev mode must never migrate — the repo keeps its own state."""
    _seed_legacy(fake_home)
    paths = _paths_under(tmp_path / "new", is_dev=True)

    moved = migrate_legacy_layout(paths)

    assert moved == []
    # Legacy still where it was.
    assert (fake_home / ".knoten" / "kasten" / "note" / "! First.md").exists()


def test_knoten_home_env_points_at_custom_location(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy user with KNOTEN_HOME=/elsewhere gets their vault migrated from there."""
    custom_root = tmp_path / "custom_root"
    custom_vault = custom_root / "kasten" / "note"
    custom_vault.mkdir(parents=True)
    (custom_vault / "! Custom.md").write_text("custom body\n", encoding="utf-8")
    monkeypatch.setenv("KNOTEN_HOME", str(custom_root))

    paths = _paths_under(tmp_path / "new")
    moved = migrate_legacy_layout(paths)

    assert (paths.vault_dir / "note" / "! Custom.md").read_text() == "custom body\n"
    # Default ~/.knoten was empty, so no other moves.
    assert len(moved) == 1


def test_no_crash_when_move_fails(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If shutil.move raises OSError, the CLI must not crash."""
    _seed_legacy(fake_home, index=False, state=False, env=False)
    paths = _paths_under(tmp_path / "new")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated")

    import knoten.migrate as mig

    monkeypatch.setattr(mig.shutil, "move", _boom)

    moved = migrate_legacy_layout(paths)

    assert moved == []
    # Legacy still there.
    assert (fake_home / ".knoten" / "kasten" / "note" / "! First.md").exists()
