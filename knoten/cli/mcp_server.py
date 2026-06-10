"""`knoten mcp serve` — optional MCP stdio facade over the service layer.

This is **opt-in** and never the primary integration — the CLI is the
contract. Each MCP tool is a thin wrapper that calls the SAME service
functions the Typer commands use, against the configured backend, so there
is no duplicated note logic. Install the optional dependency to use it:

    uv tool install 'knoten[mcp]'      # or: pipx inject knoten 'mcp>=1.0'

The `_do_*` helpers below carry all the logic and have no `mcp` dependency,
so they are unit-testable without the SDK installed. `mcp serve` only wires
them onto a FastMCP server and runs the stdio loop.
"""

from __future__ import annotations

import sys
from typing import Any

import typer

from knoten.repositories.store import Store
from knoten.services.notes import (
    append_note_remote,
    create_note_remote,
    edit_note_remote,
    hit_to_dict,
    list_summaries_to_dicts,
    read_note_full,
)
from knoten.settings import load_settings

mcp_app = typer.Typer(
    help="Optional Model Context Protocol (MCP) server over the knoten vault.",
    no_args_is_help=True,
)


# ---- tool implementations (no mcp dependency) ---------------------------


def _do_search(
    query: str, *, limit: int = 20, family: str | None = None, kind: str | None = None
) -> dict[str, Any]:
    """Full-text search the local index (read-only, no network)."""
    settings = load_settings()
    with Store(settings.paths.index_path) as store:
        hits, total = store.search(
            query,
            family=family,
            kind=kind,
            tag=None,
            min_permission=None,
            max_permission=None,
            limit=limit,
            offset=0,
            vault_dir=settings.paths.vault_dir,
        )
    return {"query": query, "total": total, "hits": [hit_to_dict(h) for h in hits]}


def _do_read(target: str, *, include_backlinks: bool = True) -> dict[str, Any]:
    """Read a full note (body + wikilinks + backlinks) from the local mirror."""
    settings = load_settings()
    with Store(settings.paths.index_path) as store:
        return read_note_full(
            store, settings.paths.vault_dir, target, include_backlinks=include_backlinks
        )


def _do_list(
    *,
    family: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List note metadata (no bodies)."""
    settings = load_settings()
    with Store(settings.paths.index_path) as store:
        summaries, total = store.list_notes(
            family=family,
            kind=kind,
            tag=tag,
            source=None,
            min_permission=None,
            max_permission=None,
            sort="updated",
            limit=limit,
            offset=0,
        )
        notes = list_summaries_to_dicts(summaries, vault_dir=settings.paths.vault_dir, store=store)
    return {"total": total, "notes": notes}


def _do_unresolved() -> dict[str, Any]:
    """List dangling wikilink targets and the notes that reference them."""
    settings = load_settings()
    with Store(settings.paths.index_path) as store:
        rows = store.unresolved_wikilinks()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["target_title"], []).append(
            {"id": row["source_id"], "filename": row["source_filename"]}
        )
    targets = [
        {"target": title, "reference_count": len(sources), "referenced_by": sources}
        for title, sources in grouped.items()
    ]
    return {"total": len(grouped), "targets": targets}


def _do_create(
    filename: str,
    *,
    body: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a note through the configured backend, mirror it locally."""
    from knoten.repositories.lock import acquire_lock

    settings = load_settings()
    _require_token(settings, for_write="create")
    with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
        with _build_backend(settings) as backend:
            note = create_note_remote(
                backend=backend,
                store=store,
                vault_dir=settings.paths.vault_dir,
                filename=filename,
                body=body,
                kind=kind,
                tags=list(tags or []),
                frontmatter=frontmatter,
            )
        return read_note_full(store, settings.paths.vault_dir, note.id, include_backlinks=False)


def _do_append(target: str, content: str) -> dict[str, Any]:
    """Append content to a note."""
    from knoten.repositories.lock import acquire_lock

    settings = load_settings()
    _require_token(settings, for_write="append")
    with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
        with _build_backend(settings) as backend:
            note = append_note_remote(
                backend=backend,
                store=store,
                vault_dir=settings.paths.vault_dir,
                target=target,
                content=content,
            )
        return read_note_full(store, settings.paths.vault_dir, note.id, include_backlinks=False)


def _do_edit(
    target: str,
    *,
    body: str | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    set_frontmatter_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a note's body / tags / typed frontmatter."""
    from knoten.repositories.lock import acquire_lock

    settings = load_settings()
    _require_token(settings, for_write="edit")
    with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
        with _build_backend(settings) as backend:
            note = edit_note_remote(
                backend=backend,
                store=store,
                vault_dir=settings.paths.vault_dir,
                target=target,
                new_filename=None,
                new_title=None,
                new_body=body,
                set_frontmatter={},
                set_frontmatter_json=set_frontmatter_json or {},
                unset_frontmatter=[],
                add_tags=list(add_tags or []),
                remove_tags=list(remove_tags or []),
            )
        return read_note_full(store, settings.paths.vault_dir, note.id, include_backlinks=False)


def _build_backend(settings: Any) -> Any:
    """Local re-export of the CLI backend factory (imported lazily to dodge a cycle)."""
    from knoten.cli.main import _build_backend as build

    return build(settings)


def _require_token(settings: Any, *, for_write: str) -> None:
    """Same fail-fast token gate the CLI mutations run (lazy import, same cycle dodge)."""
    from knoten.cli.main import _require_token as gate

    gate(settings, for_write=for_write)


# ---- serve --------------------------------------------------------------


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the MCP server over stdio (needs the optional `mcp` dependency)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        sys.stderr.write(
            "error: the MCP server needs the optional 'mcp' dependency.\n"
            "  install with: uv tool install 'knoten[mcp]'  (or: pipx inject knoten 'mcp>=1.0')\n"
        )
        raise typer.Exit(4) from exc

    server = FastMCP("knoten")

    # Read tools.
    server.tool(name="knoten_search")(_do_search)
    server.tool(name="knoten_read")(_do_read)
    server.tool(name="knoten_list")(_do_list)
    server.tool(name="knoten_unresolved")(_do_unresolved)
    # Write tools.
    server.tool(name="knoten_create")(_do_create)
    server.tool(name="knoten_append")(_do_append)
    server.tool(name="knoten_edit")(_do_edit)

    server.run()
