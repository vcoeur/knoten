"""Tests for the LLM-ergonomics surface added in this change.

Covers: `schema`, typed frontmatter on `edit` (`--set-frontmatter-json`),
batch `create`, `unresolved`, `--dry-run` on create/edit, `--json` on
`path`/`reset`, the `skill` install/status commands, and the `mcp` facade
(serve guard + the `_do_*` service wrappers).

All write paths run in local mode (no network, no httpx mocks), reusing the
same env shape as `test_cli_local_mode.py`.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from knoten.cli.main import app


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    """Point KNOTEN_* at a per-test tmp dir with no API URL — forces local mode."""
    monkeypatch.setenv("KNOTEN_HOME", str(tmp_path))
    monkeypatch.setenv("KNOTEN_API_URL", "")
    monkeypatch.setenv("KNOTEN_API_TOKEN", "")
    monkeypatch.setenv("KNOTEN_MODE", "local")
    return tmp_path


def _invoke(args: list[str], *, stdin: str | None = None) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(app, args, input=stdin)
    return result.exit_code, result.stdout


# ---- schema -------------------------------------------------------------


def test_schema_json_shape(local_env) -> None:
    code, out = _invoke(["schema", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["tool"] == "knoten"
    assert payload["permissions"] == ["NONE", "LIST", "READ", "APPEND", "WRITE", "ALL"]
    command_names = {c["name"] for c in payload["commands"]}
    # Both pre-existing and newly-added commands are introspected.
    assert {"create", "edit", "search", "schema", "unresolved", "skill", "mcp"} <= command_names
    families = {f["prefix"]: f["family"] for f in payload["families"]}
    assert families["@"] == "person"
    assert families["="] == "reference"
    error_kinds = {e["error"] for e in payload["errors"]}
    assert {"permission_denied", "validation", "not_found"} <= error_kinds
    # `skill` is a group, so it carries subcommands.
    skill_cmd = next(c for c in payload["commands"] if c["name"] == "skill")
    assert {s["name"] for s in skill_cmd["subcommands"]} == {"install", "status"}


# ---- typed frontmatter on edit -----------------------------------------


def test_set_frontmatter_json_preserves_int(local_env) -> None:
    code, out = _invoke(["create", "--filename", "@ Jane Doe", "--body", "x", "--json"])
    assert code == 0, out
    note_id = json.loads(out)["id"]

    code, out = _invoke(
        ["edit", "--set-frontmatter-json", "birth-year=1990", "--json", "--", note_id]
    )
    assert code == 0, out

    code, out = _invoke(["read", "--json", "--", note_id])
    payload = json.loads(out)
    assert payload["frontmatter"]["birth-year"] == 1990
    assert isinstance(payload["frontmatter"]["birth-year"], int)


def test_set_frontmatter_json_rejects_bad_json(local_env) -> None:
    code, out = _invoke(["create", "--filename", "@ Bad", "--body", "x", "--json"])
    note_id = json.loads(out)["id"]
    code, out = _invoke(
        ["edit", "--set-frontmatter-json", "year=not-json", "--json", "--", note_id]
    )
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


# ---- batch create -------------------------------------------------------


def test_create_batch_from_stdin(local_env) -> None:
    drafts = [
        {"filename": "% Alpha", "body": "first", "frontmatter": {"family": "entity"}},
        {"filename": "% Beta", "body": "second", "ai": True},
    ]
    code, out = _invoke(["create", "--batch", "-", "--json"], stdin=json.dumps(drafts))
    assert code == 0, out
    payload = json.loads(out)
    assert payload["operation"] == "create-batch"
    assert payload["created"] == 2
    assert payload["failed"] == 0
    assert all(r["ok"] for r in payload["results"])

    code, out = _invoke(["list", "--json"])
    filenames = {row["filename"] for row in json.loads(out)["notes"]}
    assert {"% Alpha", "% Beta"} <= filenames


def test_create_batch_continues_past_a_bad_item(local_env) -> None:
    drafts = [
        {"filename": "% Good"},
        {"body": "missing filename"},  # invalid
        {"filename": "% AlsoGood"},
    ]
    code, out = _invoke(["create", "--batch", "-", "--json"], stdin=json.dumps(drafts))
    assert code == 0, out
    payload = json.loads(out)
    assert payload["created"] == 2
    assert payload["failed"] == 1
    bad = payload["results"][1]
    assert bad["ok"] is False
    assert bad["error"] == "user"


def test_create_batch_and_filename_are_exclusive(local_env) -> None:
    code, out = _invoke(["create", "--filename", "% X", "--batch", "-", "--json"], stdin="[]")
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


# ---- unresolved ---------------------------------------------------------


def test_unresolved_lists_dangling_targets(local_env) -> None:
    _invoke(["create", "--filename", "- Has link", "--body", "See [[Missing Target]].", "--json"])
    code, out = _invoke(["unresolved", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    targets = {t["target"]: t for t in payload["targets"]}
    assert "Missing Target" in targets
    assert targets["Missing Target"]["reference_count"] == 1
    assert targets["Missing Target"]["referenced_by"][0]["filename"] == "- Has link"


def test_unresolved_empty_when_all_resolve(local_env) -> None:
    _invoke(["create", "--filename", "- Lonely", "--body", "no links here", "--json"])
    code, out = _invoke(["unresolved", "--json"])
    assert code == 0, out
    assert json.loads(out)["total"] == 0


# ---- dry-run ------------------------------------------------------------


def test_dry_run_create_does_not_write(local_env) -> None:
    code, out = _invoke(
        ["create", "--filename", "% Preview", "--body", "links [[Nope]]", "--dry-run", "--json"]
    )
    assert code == 0, out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["family"] == "entity"
    assert payload["unresolved_wikilinks"] == ["Nope"]

    code, out = _invoke(["list", "--json"])
    assert json.loads(out)["total"] == 0  # nothing created


def test_dry_run_edit_does_not_write(local_env) -> None:
    code, out = _invoke(["create", "--filename", "- Real", "--body", "v1", "--json"])
    note_id = json.loads(out)["id"]

    code, out = _invoke(["edit", "--add-tag", "draft", "--dry-run", "--json", "--", note_id])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["changes"]["add_tags"] == ["draft"]

    code, out = _invoke(["read", "--json", "--", note_id])
    assert "draft" not in json.loads(out)["tags"]


# ---- --json on path / reset --------------------------------------------


def test_path_json(local_env) -> None:
    code, out = _invoke(["create", "--filename", "- Locate me", "--body", "x", "--json"])
    note_id = json.loads(out)["id"]
    code, out = _invoke(["path", "--json", "--", note_id])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["filename"] == "- Locate me"
    assert payload["path"].endswith(".md")


def test_reset_json_requires_yes(local_env) -> None:
    code, out = _invoke(["reset", "--json"])
    assert code == 1, out
    assert json.loads(out)["error"] == "user"


def test_reset_json_with_yes(local_env) -> None:
    _invoke(["create", "--filename", "- Doomed", "--body", "x", "--json"])
    code, out = _invoke(["reset", "--json", "--yes"])
    assert code == 0, out
    assert json.loads(out)["reset"] is True


# ---- skill install / status --------------------------------------------


def test_skill_install_and_status(tmp_path) -> None:
    dest = tmp_path / "skills" / "knoten"
    code, out = _invoke(["skill", "install", "--dest", str(dest), "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["overwritten"] is False
    installed = dest / "SKILL.md"
    assert installed.exists()
    assert "knoten" in installed.read_text(encoding="utf-8")

    # Second install without --force is refused.
    code, out = _invoke(["skill", "install", "--dest", str(dest), "--json"])
    assert code == 1, out
    assert json.loads(out)["error"] == "user"

    # With --force it overwrites.
    code, out = _invoke(["skill", "install", "--dest", str(dest), "--force", "--json"])
    assert code == 0, out
    assert json.loads(out)["overwritten"] is True


def test_skill_status_json(tmp_path) -> None:
    code, out = _invoke(["skill", "status", "--json"])
    assert code == 0, out
    payload = json.loads(out)
    assert payload["bundled_bytes"] > 0
    assert {row["scope"] for row in payload["targets"]} == {"user", "project", "claude"}


# ---- mcp ----------------------------------------------------------------


def test_mcp_serve_without_dependency() -> None:
    # `mcp` is an optional extra; without it `serve` exits cleanly with code 4.
    pytest.importorskip  # noqa: B018 — keep import-time side-effect free
    try:
        import mcp  # noqa: F401

        pytest.skip("mcp installed — serve would start a blocking stdio loop")
    except ModuleNotFoundError:
        pass
    code, _out = _invoke(["mcp", "serve"])
    assert code == 4


def test_mcp_do_search_and_create_facade(local_env) -> None:
    from knoten.cli import mcp_server

    created = mcp_server._do_create("- Mcp note", body="hello from mcp")
    assert created["filename"] == "- Mcp note"

    result = mcp_server._do_search("hello")
    titles = {hit["filename"] for hit in result["hits"]}
    assert "- Mcp note" in titles

    read = mcp_server._do_read(created["id"])
    assert "hello from mcp" in read["body"]
