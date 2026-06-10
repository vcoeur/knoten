"""End-to-end exercise of `knoten mcp serve` against the real MCP client SDK.

The `_do_*` helpers are unit-tested in `test_mcp_server.py` without the SDK.
This module is the proof that the *wiring* holds: it spawns the actual
`knoten mcp serve` process over stdio, drives it with `mcp.client`, and
asserts the FastMCP `.tool()` / `.run()` assumptions (tool registration,
stdio loop, structured results) are correct.

Skipped cleanly when the optional `mcp` extra is absent — the gate uses
`importlib.util.find_spec` (no import), so collection never fails, and the
`mcp` imports live inside the driver so they only run when the test runs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_MCP_MISSING = importlib.util.find_spec("mcp") is None

pytestmark = pytest.mark.skipif(_MCP_MISSING, reason="requires the optional 'mcp' extra")

EXPECTED_TOOLS = {
    "knoten_search",
    "knoten_read",
    "knoten_list",
    "knoten_unresolved",
    "knoten_create",
    "knoten_append",
    "knoten_edit",
}


def _serve_env(root: Path) -> dict[str, str]:
    """Build a local-mode env pointing every KNOTEN dir under a temp root."""
    env = dict(os.environ)
    env["KNOTEN_CONFIG_DIR"] = str(root / "cfg")
    env["KNOTEN_DATA_DIR"] = str(root / "data")
    env["KNOTEN_CACHE_DIR"] = str(root / "cache")
    env["KNOTEN_API_URL"] = ""
    env["KNOTEN_API_TOKEN"] = ""
    env["KNOTEN_MODE"] = "local"
    return env


def _seed_note(env: dict[str, str], filename: str, body: str) -> None:
    """Create a note via the CLI so the temp vault + index has content to read."""
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "knoten.cli.main",
            "create",
            "--filename",
            filename,
            "--body",
            body,
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def _drive(env: dict[str, str]) -> dict[str, Any]:
    """Spawn the server over stdio and collect init / tools / search / read."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "knoten.cli.main", "mcp", "serve"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            search = await session.call_tool("knoten_search", {"query": "unicorn"})
            read_result = await session.call_tool("knoten_read", {"target": "- MCP probe"})
    return {
        "server_name": init.serverInfo.name,
        "tool_names": {tool.name for tool in tools.tools},
        "search": search,
        "read": read_result,
    }


def test_mcp_serve_stdio_end_to_end(tmp_path) -> None:
    """The shipped FastMCP wiring serves the read tools correctly over stdio."""
    env = _serve_env(tmp_path)
    _seed_note(env, "- MCP probe", "searchable mcp content unicorn")

    collected = asyncio.run(asyncio.wait_for(_drive(env), timeout=60))

    # Server identity + the exact tool surface registered by `serve`.
    assert collected["server_name"] == "knoten"
    assert collected["tool_names"] == EXPECTED_TOOLS

    # knoten_search returns the seeded note as a structured hit.
    search = collected["search"]
    assert search.isError is False
    search_payload = search.structuredContent
    assert search_payload["total"] >= 1
    assert "- MCP probe" in {hit["filename"] for hit in search_payload["hits"]}

    # knoten_read returns the full note body as a structured result.
    read_result = collected["read"]
    assert read_result.isError is False
    read_payload = read_result.structuredContent
    assert read_payload["filename"] == "- MCP probe"
    assert "unicorn" in read_payload["body"]
