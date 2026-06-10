"""Tests for the MCP facade's `_do_*` helpers (no `mcp` dependency needed)."""

from __future__ import annotations

import pytest

from knoten.cli.mcp_server import _do_append, _do_create, _do_edit
from knoten.repositories.errors import ConfigError


@pytest.mark.parametrize(
    "call",
    [
        lambda: _do_create("- X", body="x"),
        lambda: _do_append("- X", "more"),
        lambda: _do_edit("- X", body="y"),
    ],
    ids=["create", "append", "edit"],
)
def test_mcp_write_helpers_require_token(monkeypatch, call) -> None:
    """MCP write tools run the same fail-fast token gate as the CLI mutations."""
    monkeypatch.setenv("KNOTEN_API_URL", "https://notes.test")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    with pytest.raises(ConfigError, match="KNOTEN_API_TOKEN"):
        call()
