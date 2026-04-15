"""Pytest fixtures shared across knoten tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """A Settings instance rooted under a per-test temp directory."""
    vault_dir = tmp_path / "kasten"
    state_dir = tmp_path / ".knoten-state"
    vault_dir.mkdir()
    state_dir.mkdir()
    return Settings(
        api_url="https://notes.test",
        api_token="nt_test_token",
        http_timeout=5.0,
        home=tmp_path,
        vault_dir=vault_dir,
        state_dir=state_dir,
        index_path=state_dir / "index.sqlite",
        state_file=state_dir / "state.json",
        lock_file=state_dir / "sync.lock",
        tmp_dir=state_dir / "tmp",
    )


@pytest.fixture
def store(tmp_settings: Settings) -> Store:
    """An open Store backed by a per-test sqlite file."""
    store = Store(tmp_settings.index_path)
    store.open()
    try:
        yield store
    finally:
        store.close()
