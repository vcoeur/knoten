"""Tests for the config / data / cache path resolution layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from knoten import paths


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any KNOTEN_* overrides so each test starts from a clean slate.

    The conftest `_isolate_paths` autouse fixture sets these dirs under a
    per-test sandbox; clear them here and re-set explicitly in each test
    so we observe the resolver's real behaviour (dev-mode detection, env
    overrides winning, platformdirs fallback).
    """
    for var in (paths.ENV_CONFIG_DIR, paths.ENV_DATA_DIR, paths.ENV_CACHE_DIR):
        monkeypatch.delenv(var, raising=False)


def test_env_overrides_win(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All three env vars take precedence over both dev-mode and platformdirs."""
    config = tmp_path / "cfg"
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    monkeypatch.setenv(paths.ENV_CONFIG_DIR, str(config))
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(data))
    monkeypatch.setenv(paths.ENV_CACHE_DIR, str(cache))

    resolved = paths.resolve()

    assert resolved.config_dir == config
    assert resolved.data_dir == data
    assert resolved.cache_dir == cache
    assert resolved.env_file == config / ".env"
    assert resolved.vault_dir == data / paths.VAULT_SUBDIR
    assert resolved.index_path == cache / "index.sqlite"
    assert resolved.state_file == cache / "state.json"
    assert resolved.lock_file == cache / "sync.lock"
    assert resolved.tmp_dir == cache / "tmp"


def test_env_overrides_expand_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Env-var values with a leading ~ are expanded relative to $HOME."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv(paths.ENV_CONFIG_DIR, "~/myconfig")

    resolved = paths.resolve()

    assert resolved.config_dir == fake_home / "myconfig"


def test_dev_mode_uses_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Dev mode: vault stays at repo/kasten, cache goes to repo/.dev-state/cache."""
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / "pyproject.toml").write_text("[project]\nname='fake'\n")
    monkeypatch.setattr(paths, "_repo_root", lambda: fake_repo)

    resolved = paths.resolve()

    assert resolved.is_dev is True
    assert resolved.config_dir == fake_repo
    assert resolved.data_dir == fake_repo
    assert resolved.cache_dir == fake_repo / ".dev-state" / "cache"
    assert resolved.env_file == fake_repo / ".env"
    assert resolved.vault_dir == fake_repo / paths.VAULT_SUBDIR
    assert resolved.index_path == fake_repo / ".dev-state" / "cache" / "index.sqlite"


def test_installed_mode_uses_platformdirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When `_repo_root` returns None, all three paths come from platformdirs."""
    fake_cfg = tmp_path / "platform_config"
    fake_data = tmp_path / "platform_data"
    fake_cache = tmp_path / "platform_cache"

    monkeypatch.setattr(paths, "_repo_root", lambda: None)
    monkeypatch.setattr(paths, "user_config_dir", lambda *a, **k: str(fake_cfg))
    monkeypatch.setattr(paths, "user_data_dir", lambda *a, **k: str(fake_data))
    monkeypatch.setattr(paths, "user_cache_dir", lambda *a, **k: str(fake_cache))

    resolved = paths.resolve()

    assert resolved.is_dev is False
    assert resolved.config_dir == fake_cfg
    assert resolved.data_dir == fake_data
    assert resolved.cache_dir == fake_cache


def test_env_override_beats_dev_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A set env var overrides even when `_repo_root` thinks we're in dev mode."""
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    monkeypatch.setattr(paths, "_repo_root", lambda: fake_repo)

    explicit = tmp_path / "explicit_data"
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(explicit))

    resolved = paths.resolve()

    assert resolved.data_dir == explicit
    assert resolved.config_dir == fake_repo  # unaffected — config override not set


def test_ensure_dirs_creates_all_four(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`ensure_dirs` creates config, vault, cache, and tmp directories."""
    monkeypatch.setenv(paths.ENV_CONFIG_DIR, str(tmp_path / "c"))
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "d"))
    monkeypatch.setenv(paths.ENV_CACHE_DIR, str(tmp_path / "k"))

    resolved = paths.resolve()
    paths.ensure_dirs(resolved)

    assert resolved.config_dir.is_dir()
    assert resolved.vault_dir.is_dir()
    assert resolved.cache_dir.is_dir()
    assert resolved.tmp_dir.is_dir()


def test_ensure_dirs_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Calling `ensure_dirs` twice is a no-op."""
    monkeypatch.setenv(paths.ENV_CONFIG_DIR, str(tmp_path / "c"))
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "d"))
    monkeypatch.setenv(paths.ENV_CACHE_DIR, str(tmp_path / "k"))

    resolved = paths.resolve()
    paths.ensure_dirs(resolved)
    paths.ensure_dirs(resolved)  # second call must not raise


def test_installed_location_detector() -> None:
    """The heuristic flags site-packages and uv tools venvs as installed."""
    assert paths._looks_like_installed_location(Path("/foo/site-packages/knoten/paths.py"))
    assert paths._looks_like_installed_location(
        Path("/home/a/.local/share/uv/tools/knoten/lib/knoten/paths.py")
    )
    assert not paths._looks_like_installed_location(
        Path("/home/a/src/vcoeur/knoten/knoten/paths.py")
    )
