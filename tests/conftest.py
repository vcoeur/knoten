"""Pytest fixtures shared across knoten tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from knoten import paths as paths_module
from knoten.paths import VAULT_SUBDIR, Paths
from knoten.repositories.store import Store
from knoten.settings import Settings


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove system proxy env vars so httpx.Client does not try to wire up
    a SOCKS or HTTP proxy during unit tests. pytest-httpx patches the
    transport, but httpx reads proxy env vars at Client-construction time.
    """
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    os.environ.setdefault("NO_PROXY", "*")


@pytest.fixture(autouse=True)
def _isolate_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Sandbox every path-resolving code path under a per-test temp dir.

    Tests must never touch the user's real `~/.config/knoten/`,
    `~/.local/share/knoten/`, or `~/.cache/knoten/`. This fixture forces
    `paths.resolve()` onto a throwaway directory via the env-var escape
    hatches and also stubs `_repo_root` so dev-mode detection can't leak
    into tests running from a source checkout.
    """
    sandbox = tmp_path / ".sandbox-paths"
    monkeypatch.setenv(paths_module.ENV_CONFIG_DIR, str(sandbox / "cfg"))
    monkeypatch.setenv(paths_module.ENV_DATA_DIR, str(sandbox / "data"))
    monkeypatch.setenv(paths_module.ENV_CACHE_DIR, str(sandbox / "cache"))
    monkeypatch.delenv("KNOTEN_HOME", raising=False)
    monkeypatch.setattr(paths_module, "_repo_root", lambda: None)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """A Settings with paths rooted under a fresh tmp_path directory."""
    config_dir = tmp_path / "cfg"
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    vault_dir = data_dir / VAULT_SUBDIR
    config_dir.mkdir(parents=True, exist_ok=True)
    vault_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "tmp").mkdir(parents=True, exist_ok=True)

    test_paths = Paths(
        config_dir=config_dir,
        data_dir=data_dir,
        cache_dir=cache_dir,
        env_file=config_dir / ".env",
        vault_dir=vault_dir,
        index_path=cache_dir / "index.sqlite",
        state_file=cache_dir / "state.json",
        lock_file=cache_dir / "sync.lock",
        tmp_dir=cache_dir / "tmp",
        is_dev=False,
    )
    return Settings(
        api_url="https://notes.test",
        api_token="nt_test_token",
        http_timeout=5.0,
        paths=test_paths,
    )


@pytest.fixture
def store(tmp_settings: Settings) -> Store:
    """An open Store backed by a per-test sqlite file."""
    store = Store(tmp_settings.paths.index_path)
    store.open()
    try:
        yield store
    finally:
        store.close()
