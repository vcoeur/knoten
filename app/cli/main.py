"""Typer CLI entrypoint for the `kasten` command.

Each subcommand is a thin wrapper: parse flags, resolve Settings, open the
Store (and NotesClient when remote access is needed), call into a service,
render the result via `app.cli.output`.

Exit codes (mapped from exception types in `app.repositories.errors`):
    0 success
    1 user error
    2 network error
    3 local store error
    4 config error
    5 lock timeout
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from datetime import UTC
from pathlib import Path

import typer

from app.cli.output import (
    OutputMode,
    emit_json,
    log,
    make_progress_callback,
    render_backlinks,
    render_counts,
    render_note,
    render_search_hits,
    render_status,
    render_summary_list,
    render_sync_result,
)
from app.repositories.errors import (
    AmbiguousTargetError,
    AuthError,
    ConfigError,
    KastenError,
    LockTimeoutError,
    NetworkError,
    NotFoundError,
    StoreError,
    UserError,
)
from app.repositories.errors import (
    PermissionError as LocalPermissionError,
)
from app.repositories.http_client import NotesClient
from app.repositories.lock import acquire_lock
from app.repositories.store import Store
from app.repositories.sync_state import load_state
from app.services.notes import (
    append_note_remote,
    create_note_remote,
    delete_note_remote,
    download_file_remote,
    edit_note_remote,
    hit_to_dict,
    list_summaries_to_dicts,
    read_note_full,
    resolve_target,
    restore_note_remote,
    summarize_note,
    upload_file_remote,
)
from app.services.reconcile import reconcile_local
from app.services.reindex import reindex_from_files
from app.services.sync import full_sync, incremental_sync
from app.settings import Settings, ensure_dirs, load_settings

app = typer.Typer(
    help="Local CLI mirror and search for notes.vcoeur.com. Designed for Claude.",
    no_args_is_help=True,
    add_completion=False,
)


# ---- global state -------------------------------------------------------


def _load() -> Settings:
    settings = load_settings()
    ensure_dirs(settings)
    return settings


def _require_token(settings: Settings) -> None:
    if not settings.api_token:
        raise ConfigError(
            "KASTEN_API_TOKEN is not set. Copy .env.example to .env and add an API token."
        )


def _fail(exc: Exception) -> None:
    """Print an error and exit with the appropriate code."""
    message = str(exc)
    if isinstance(exc, ConfigError):
        code = 4
    elif isinstance(exc, (AuthError, NetworkError)):
        code = 2
    elif isinstance(exc, StoreError):
        code = 3
    elif isinstance(exc, LockTimeoutError):
        code = 5
    elif isinstance(exc, LocalPermissionError):
        code = 1
        payload = {
            "error": "permission_denied",
            "message": message,
            "note_id": exc.note_id,
            "filename": exc.filename,
            "current_level": exc.current_level,
            "required_level": exc.required_level,
            "operation": exc.operation,
        }
        emit_json(payload)
        raise typer.Exit(code)
    elif isinstance(exc, AmbiguousTargetError):
        code = 1
        payload = {"error": "ambiguous_target", "message": message, "candidates": exc.candidates}
        emit_json(payload)
        raise typer.Exit(code)
    elif isinstance(exc, (UserError, NotFoundError, KastenError)):
        code = 1
    else:
        code = 1
    sys.stderr.write(f"error: {message}\n")
    raise typer.Exit(code)


# ---- sync ---------------------------------------------------------------


@app.command("sync")
def cmd_sync(
    full: bool = typer.Option(False, "--full", help="Force a full refetch of every note"),
    verify: bool = typer.Option(
        False,
        "--verify",
        help="Re-hash every local file and re-fetch any that have drifted from the recorded hash",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON to stdout"),
) -> None:
    """Pull new/changed notes from notes.vcoeur.com into the local mirror.

    Every sync (incremental or `--full`) always:

      1. Fetches new/changed notes via pagination.
      2. Runs delete detection — any note removed on the remote is purged locally.
      3. Reconciles the local mirror — re-fetches any file that is missing
         on disk, removes orphan files that the store does not know about.

    With `--verify`, the reconciliation pass also re-hashes every file and
    re-fetches any whose content has drifted from the recorded body hash.
    Slower (O(N) disk reads), but gives a strong consistency guarantee.
    """
    mode = OutputMode.detect(json_output)
    progress = make_progress_callback(mode)
    try:
        settings = _load()
        _require_token(settings)
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            with NotesClient(settings) as client:
                if full:
                    result = full_sync(
                        client=client,
                        store=store,
                        settings=settings,
                        verify_hashes=verify,
                        progress=progress,
                    )
                else:
                    result = incremental_sync(
                        client=client,
                        store=store,
                        settings=settings,
                        verify_hashes=verify,
                        progress=progress,
                    )
            payload = asdict(result)
            render_sync_result(payload, mode=mode)
    except Exception as exc:
        _fail(exc)


@app.command("verify")
def cmd_verify(
    hashes: bool = typer.Option(
        False,
        "--hashes",
        help="Also re-hash every file and re-fetch mismatched ones (slower, O(N) disk reads)",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reconcile local mirror + index against the store, without a full sync.

    Three checks run unconditionally:

      1. **SQLite integrity** — `PRAGMA integrity_check`. Fast; catches
         page-level corruption.
      2. **FTS5 / notes cardinality** — every row in `notes` should have a
         matching row in `notes_fts`, and vice versa.
      3. **File existence + orphan cleanup** — re-fetches missing mirror
         files from the remote, deletes orphans.

    With `--hashes`, additionally re-reads every file and compares its body
    hash against the recorded `body_sha256`. Mismatches are re-fetched.

    If the FTS5 cardinality check shows drift, run `kasten reindex` to
    rebuild the derived tables from the on-disk files without a network hit.
    """
    mode = OutputMode.detect(json_output)
    progress = make_progress_callback(mode)
    try:
        settings = _load()
        _require_token(settings)
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            progress("→ SQLite integrity check")
            integrity = store.integrity_check()
            progress(f"  {integrity}")
            progress("→ FTS5 / notes cardinality")
            cardinality = store.fts_cardinality_check()
            progress(
                f"  notes={cardinality['notes_count']} "
                f"fts={cardinality['fts_count']} "
                f"consistent={cardinality['consistent']}"
            )
            progress(
                "→ Reconciling local mirror" + (" (with body-hash verification)" if hashes else "")
            )
            with NotesClient(settings) as client:
                result = reconcile_local(
                    client=client,
                    store=store,
                    settings=settings,
                    verify_hashes=hashes,
                    progress=progress,
                )
        payload = {
            "integrity": integrity,
            "cardinality": cardinality,
            "checked": result.checked,
            "missing_refetched": result.missing_refetched,
            "mismatched_refetched": result.mismatched_refetched,
            "orphans_removed": result.orphans_removed,
            "verified_hashes": result.verified_hashes,
            "missing_ids": result.missing_ids,
            "mismatched_ids": result.mismatched_ids,
            "orphan_paths": result.orphan_paths,
        }
        if mode.json:
            emit_json(payload)
        else:
            from rich.console import Console

            console = Console()
            integrity_colour = "green" if integrity == "ok" else "red"
            consistent_colour = "green" if cardinality["consistent"] else "red"
            console.print(
                f"integrity=[{integrity_colour}]{integrity}[/{integrity_colour}]  "
                f"fts=[{consistent_colour}]{cardinality['consistent']}[/{consistent_colour}] "
                f"(notes={cardinality['notes_count']}, "
                f"fts={cardinality['fts_count']})"
            )
            console.print(
                f"checked={result.checked} "
                f"[green]missing_refetched={result.missing_refetched}[/green] "
                f"[yellow]mismatched_refetched={result.mismatched_refetched}[/yellow] "
                f"[red]orphans_removed={result.orphans_removed}[/red] "
                f"(hashes={'yes' if result.verified_hashes else 'no'})"
            )
            if not cardinality["consistent"]:
                console.print(
                    "[yellow]FTS5 drift detected — run `kasten reindex` to rebuild "
                    "the derived tables from on-disk files.[/yellow]"
                )
            if result.missing_ids:
                console.print(f"  re-fetched missing: {', '.join(result.missing_ids[:10])}")
            if result.orphan_paths:
                console.print(f"  orphans removed: {', '.join(result.orphan_paths[:10])}")
    except Exception as exc:
        _fail(exc)


@app.command("reindex")
def cmd_reindex(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Rebuild the derived index (FTS5, tags, wikilinks, frontmatter_fields)
    from the `notes` rows and the on-disk mirror files. No network.

    Use this when `kasten verify` reports FTS5 drift, when SQLite's integrity
    check complains about derived tables, or when you want a quick offline
    rebuild without re-fetching every note from the remote.

    Notes whose mirror file is missing are skipped and reported — follow up
    with `kasten verify` (which has network access) to pull them back.
    """
    mode = OutputMode.detect(json_output)
    progress = make_progress_callback(mode)
    try:
        settings = _load()
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            result = reindex_from_files(store=store, settings=settings, progress=progress)
        payload = {
            "integrity": result.integrity,
            "checked": result.checked,
            "reindexed": result.reindexed,
            "skipped_missing_file": result.skipped_missing_file,
            "missing_file_ids": result.missing_file_ids[:50],
            "cardinality_before": result.cardinality_before,
            "cardinality_after": result.cardinality_after,
        }
        if mode.json:
            emit_json(payload)
        else:
            from rich.console import Console

            console = Console()
            console.print(
                f"reindex: checked={result.checked} "
                f"reindexed={result.reindexed} "
                f"skipped_missing_file={result.skipped_missing_file}"
            )
            console.print(
                f"  fts before: notes={result.cardinality_before.get('notes_count')} "
                f"fts={result.cardinality_before.get('fts_count')} "
                f"consistent={result.cardinality_before.get('consistent')}"
            )
            console.print(
                f"  fts after:  notes={result.cardinality_after.get('notes_count')} "
                f"fts={result.cardinality_after.get('fts_count')} "
                f"consistent={result.cardinality_after.get('consistent')}"
            )
            if result.missing_file_ids:
                console.print(
                    f"[yellow]skipped (missing file):[/yellow] "
                    f"{', '.join(result.missing_file_ids[:10])} — run `kasten verify`"
                )
    except Exception as exc:
        _fail(exc)


# ---- read-path ----------------------------------------------------------


@app.command("search")
def cmd_search(
    query: str = typer.Argument(..., help="FTS5 query string (or free text with --fuzzy)"),
    family: str | None = typer.Option(None, "--family"),
    kind: str | None = typer.Option(None, "--kind"),
    tag: str | None = typer.Option(None, "--tag"),
    min_permission: str | None = typer.Option(
        None,
        "--min-permission",
        help="Only include notes at this mcp permission level or higher "
        "(NONE/LIST/READ/APPEND/WRITE/ALL)",
    ),
    max_permission: str | None = typer.Option(
        None,
        "--max-permission",
        help="Only include notes at this mcp permission level or lower",
    ),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    offset: int = typer.Option(0, "--offset", min=0),
    remote: bool = typer.Option(False, "--remote", help="Bypass local index, hit the server"),
    fuzzy: bool = typer.Option(
        False,
        "--fuzzy",
        help="Typo-tolerant + substring search (trigram FTS + rapidfuzz on titles)",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Full-text search against the local index (or --remote for server search)."""
    mode = OutputMode.detect(json_output)
    try:
        if fuzzy and remote:
            raise UserError("--fuzzy is a local-only mode; drop --remote to use it")
        settings = _load()
        if remote:
            _require_token(settings)
            with NotesClient(settings) as client:
                raw_hits = client.remote_search(
                    query, kind=kind, family=family, tag=tag, limit=limit
                )
            payload = {"query": query, "total": len(raw_hits), "hits": raw_hits, "source": "remote"}
            render_search_hits(payload, mode=mode)
            return
        with Store(settings.index_path) as store:
            if fuzzy:
                hits, total = store.search_fuzzy(
                    query,
                    family=family,
                    kind=kind,
                    tag=tag,
                    min_permission=min_permission,
                    max_permission=max_permission,
                    limit=limit,
                    offset=offset,
                    vault_dir=settings.vault_dir,
                )
                source = "local-fuzzy"
            else:
                hits, total = store.search(
                    query,
                    family=family,
                    kind=kind,
                    tag=tag,
                    min_permission=min_permission,
                    max_permission=max_permission,
                    limit=limit,
                    offset=offset,
                    vault_dir=settings.vault_dir,
                )
                source = "local"
        payload = {
            "query": query,
            "total": total,
            "hits": [hit_to_dict(h) for h in hits],
            "source": source,
        }
        render_search_hits(payload, mode=mode)
    except Exception as exc:
        _fail(exc)


@app.command("read")
def cmd_read(
    target: str = typer.Argument(..., help="Note UUID or filename (or prefix)"),
    no_backlinks: bool = typer.Option(False, "--no-backlinks"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read a note from the local mirror — body + wikilinks + backlinks."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.index_path) as store:
            payload = read_note_full(
                store,
                settings.vault_dir,
                target,
                include_backlinks=not no_backlinks,
            )
        render_note(payload, mode=mode)
    except Exception as exc:
        _fail(exc)


@app.command("path")
def cmd_path(
    target: str = typer.Argument(..., help="Note UUID or filename (or prefix)"),
) -> None:
    """Print the absolute mirror path for a note. One line, no JSON envelope."""
    try:
        settings = _load()
        with Store(settings.index_path) as store:
            row = resolve_target(store, target)
        sys.stdout.write(str((settings.vault_dir / row["path"]).resolve()) + "\n")
    except Exception as exc:
        _fail(exc)


@app.command("list")
def cmd_list(
    family: str | None = typer.Option(None, "--family"),
    kind: str | None = typer.Option(None, "--kind"),
    tag: str | None = typer.Option(None, "--tag"),
    source: str | None = typer.Option(None, "--source"),
    min_permission: str | None = typer.Option(
        None,
        "--min-permission",
        help="Only include notes at this mcp permission level or higher",
    ),
    max_permission: str | None = typer.Option(
        None,
        "--max-permission",
        help="Only include notes at this mcp permission level or lower",
    ),
    sort: str = typer.Option("updated", "--sort"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List notes from the local index."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.index_path) as store:
            summaries, total = store.list_notes(
                family=family,
                kind=kind,
                tag=tag,
                source=source,
                min_permission=min_permission,
                max_permission=max_permission,
                sort=sort,
                limit=limit,
                offset=offset,
            )
            notes = list_summaries_to_dicts(summaries, vault_dir=settings.vault_dir, store=store)
        payload = {"total": total, "limit": limit, "offset": offset, "notes": notes}
        render_summary_list(payload, mode=mode)
    except Exception as exc:
        _fail(exc)


@app.command("backlinks")
def cmd_backlinks(
    target: str = typer.Argument(..., help="Note UUID or filename (or prefix)"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List notes that link to the given note."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.index_path) as store:
            row = resolve_target(store, target)
            backlinks = store.backlinks_for_note(row["id"])
            for bl in backlinks:
                bl["absolute_path"] = str((settings.vault_dir / bl["path"]).resolve())
        render_backlinks({"id": row["id"], "backlinks": backlinks}, mode=mode)
    except Exception as exc:
        _fail(exc)


@app.command("tags")
def cmd_tags(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List all tags with counts, sorted by count DESC."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.index_path) as store:
            rows = store.tag_counts()
        render_counts({"tags": rows}, "tags", mode=mode)
    except Exception as exc:
        _fail(exc)


@app.command("graph")
def cmd_graph(
    target: str = typer.Argument(
        ..., help="Note UUID or filename (or prefix) to centre the graph on"
    ),
    depth: int = typer.Option(2, "--depth", min=0, max=5, help="Traversal depth (BFS hops, max 5)"),
    direction: str = typer.Option(
        "both",
        "--direction",
        help="Follow outgoing wiki-links ('out'), backlinks ('in'), or both",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """BFS wiki-link neighbourhood around a note — useful for broadening a search.

    Returns nodes and edges within `--depth` hops of the starting note. Each
    node carries its depth (0 for the starting note) so Claude can sort by
    distance. Broken wiki-links (titles that resolved to no note) are returned
    in a separate list, not as nodes.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.index_path) as store:
            start = resolve_target(store, target)
            nodes, edges, broken = store.graph_neighbourhood(
                start["id"], depth=depth, direction=direction
            )
        for node in nodes.values():
            node["absolute_path"] = str((settings.vault_dir / node["path"]).resolve())
        payload = {
            "start": start["id"],
            "depth": depth,
            "direction": direction,
            "nodes": sorted(nodes.values(), key=lambda n: (n["depth"], n["title"])),
            "edges": [{"source": s, "target": t} for s, t in edges],
            "broken_targets": broken,
        }
        if mode.json:
            emit_json(payload)
        else:
            from rich.console import Console

            console = Console()
            console.print(
                f"[bold]{start['title']}[/bold]  "
                f"[dim]({len(nodes)} nodes, {len(edges)} edges, depth {depth}, {direction})[/dim]"
            )
            for node in payload["nodes"]:
                prefix = "  " * node["depth"] + ("•" if node["depth"] else "★")
                console.print(
                    f"{prefix} [cyan]{node['family']}/{node['kind']}[/cyan] {node['title']}"
                )
            if broken:
                console.print(f"[yellow]broken:[/yellow] {', '.join(broken)}")
    except Exception as exc:
        _fail(exc)


@app.command("kinds")
def cmd_kinds(
    family: str | None = typer.Option(None, "--family"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List all kinds with counts, optionally filtered by family."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.index_path) as store:
            rows = store.kind_counts(family=family)
        render_counts({"kinds": rows}, "kinds", mode=mode)
    except Exception as exc:
        _fail(exc)


# ---- write-path ---------------------------------------------------------


def _resolve_body(body: str | None, body_file: Path | None) -> str | None:
    if body is not None and body_file is not None:
        raise UserError("--body and --body-file are mutually exclusive")
    if body is not None:
        return body
    if body_file is not None:
        if str(body_file) == "-":
            return sys.stdin.read()
        return body_file.read_text(encoding="utf-8")
    return None


def _wrap_ai(content: str) -> str:
    """Wrap AI-authored content with `#ai begin` / `#ai end` markers.

    Literal wrap — pre-existing markers in the input are not stripped or
    de-duplicated. Leading and trailing blank lines are trimmed so the
    markers sit flush against the content.
    """
    return f"#ai begin\n{content.strip(chr(10))}\n#ai end"


@app.command("create")
def cmd_create(
    filename: str = typer.Option(
        ..., "--filename", help="Full Kasten filename (e.g. '! Core idea')"
    ),
    body: str | None = typer.Option(None, "--body"),
    body_file: Path | None = typer.Option(None, "--body-file"),
    kind: str | None = typer.Option(None, "--kind"),
    tag: list[str] = typer.Option([], "--tag"),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Wrap the body in `#ai begin` / `#ai end` markers (AI-authored content).",
    ),
    with_body: bool = typer.Option(
        False,
        "--with-body",
        help="Echo the full note body, frontmatter, tags, wikilinks, backlinks (off by default).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create a new note on notes.vcoeur.com and mirror it locally."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings)
        body_text = _resolve_body(body, body_file)
        if ai:
            if body_text is None:
                raise UserError("--ai requires --body or --body-file")
            body_text = _wrap_ai(body_text)
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            with NotesClient(settings) as client:
                note = create_note_remote(
                    client=client,
                    store=store,
                    vault_dir=settings.vault_dir,
                    filename=filename,
                    body=body_text,
                    kind=kind,
                    tags=list(tag),
                )
            if with_body:
                payload = read_note_full(
                    store, settings.vault_dir, note.id, include_backlinks=False
                )
            else:
                payload = summarize_note(store, settings.vault_dir, note.id)
        render_note(payload, mode=mode, minimal=not with_body)
    except Exception as exc:
        _fail(exc)


@app.command("edit")
def cmd_edit(
    target: str = typer.Argument(...),
    filename: str | None = typer.Option(None, "--filename"),
    title: str | None = typer.Option(None, "--title"),
    body: str | None = typer.Option(None, "--body"),
    body_file: Path | None = typer.Option(None, "--body-file"),
    set_frontmatter: list[str] = typer.Option([], "--set-frontmatter"),
    unset_frontmatter: list[str] = typer.Option([], "--unset-frontmatter"),
    add_tag: list[str] = typer.Option([], "--add-tag"),
    remove_tag: list[str] = typer.Option([], "--remove-tag"),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Wrap the replacement body in `#ai begin` / `#ai end` markers.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the local mcp_permissions pre-check (web-scope tokens only)",
    ),
    with_body: bool = typer.Option(
        False,
        "--with-body",
        help="Echo the full note body, frontmatter, tags, wikilinks, backlinks (off by default).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Edit a note on notes.vcoeur.com and refresh the local mirror."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings)
        body_text = _resolve_body(body, body_file)
        if ai:
            if body_text is None:
                raise UserError("--ai requires --body or --body-file")
            body_text = _wrap_ai(body_text)
        fm_sets: dict[str, str] = {}
        for pair in set_frontmatter:
            if "=" not in pair:
                raise UserError(f"--set-frontmatter expects key=value, got '{pair}'")
            key, _, value = pair.partition("=")
            fm_sets[key] = value
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            with NotesClient(settings) as client:
                note = edit_note_remote(
                    client=client,
                    store=store,
                    vault_dir=settings.vault_dir,
                    target=target,
                    new_filename=filename,
                    new_title=title,
                    new_body=body_text,
                    set_frontmatter=fm_sets,
                    unset_frontmatter=list(unset_frontmatter),
                    add_tags=list(add_tag),
                    remove_tags=list(remove_tag),
                    force=force,
                )
            if with_body:
                payload = read_note_full(
                    store, settings.vault_dir, note.id, include_backlinks=False
                )
            else:
                payload = summarize_note(store, settings.vault_dir, note.id)
        render_note(payload, mode=mode, minimal=not with_body)
    except Exception as exc:
        _fail(exc)


@app.command("append")
def cmd_append(
    target: str = typer.Argument(..., help="Note UUID or filename (or prefix)"),
    content: str | None = typer.Option(
        None,
        "--content",
        help="Text to append. Mutually exclusive with --content-file.",
    ),
    content_file: Path | None = typer.Option(
        None,
        "--content-file",
        help="Read the content from a file (use '-' for stdin).",
    ),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Wrap the appended content in `#ai begin` / `#ai end` markers.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the local mcp_permissions pre-check (web-scope tokens only)",
    ),
    with_body: bool = typer.Option(
        False,
        "--with-body",
        help="Echo the full note body, frontmatter, tags, wikilinks, backlinks (off by default).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Append content to a note via POST /api/notes/{id}/append.

    Uses the server's dedicated append endpoint — so a token with only
    APPEND permission on the target note can extend it without needing
    WRITE. The server joins the new content with a blank-line separator.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings)
        if content is not None and content_file is not None:
            raise UserError("--content and --content-file are mutually exclusive")
        if content is None and content_file is None:
            raise UserError("Pass --content <text> or --content-file <path>")
        if content_file is not None:
            text = (
                sys.stdin.read()
                if str(content_file) == "-"
                else content_file.read_text(encoding="utf-8")
            )
        else:
            text = content or ""
        if not text:
            raise UserError("Content is empty — nothing to append")
        if ai:
            text = _wrap_ai(text)
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            with NotesClient(settings) as client:
                note = append_note_remote(
                    client=client,
                    store=store,
                    vault_dir=settings.vault_dir,
                    target=target,
                    content=text,
                    force=force,
                )
            if with_body:
                payload = read_note_full(
                    store, settings.vault_dir, note.id, include_backlinks=False
                )
            else:
                payload = summarize_note(store, settings.vault_dir, note.id)
        render_note(payload, mode=mode, minimal=not with_body)
    except Exception as exc:
        _fail(exc)


@app.command("delete")
def cmd_delete(
    target: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the local mcp_permissions pre-check (web-scope tokens only)",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Soft-delete a note (move to trash on notes.vcoeur.com)."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings)
        if mode.json and not yes:
            raise UserError("In --json mode you must pass --yes to confirm deletion")
        if not mode.json and not yes:
            confirmed = typer.confirm(f"Really delete '{target}'?", default=False)
            if not confirmed:
                log("aborted", mode=mode)
                raise typer.Exit(0)
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            with NotesClient(settings) as client:
                note_id = delete_note_remote(
                    client=client,
                    store=store,
                    vault_dir=settings.vault_dir,
                    target=target,
                    force=force,
                )
        if mode.json:
            emit_json({"deleted_id": note_id})
        else:
            log(f"deleted {note_id}", mode=mode)
    except Exception as exc:
        _fail(exc)


@app.command("restore")
def cmd_restore(
    note_id: str = typer.Argument(...),
    with_body: bool = typer.Option(
        False,
        "--with-body",
        help="Echo the full note body, frontmatter, tags, wikilinks, backlinks (off by default).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Restore a note from trash."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings)
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            with NotesClient(settings) as client:
                note = restore_note_remote(
                    client=client, store=store, vault_dir=settings.vault_dir, note_id=note_id
                )
            if with_body:
                payload = read_note_full(
                    store, settings.vault_dir, note.id, include_backlinks=False
                )
            else:
                payload = summarize_note(store, settings.vault_dir, note.id)
        render_note(payload, mode=mode, minimal=not with_body)
    except Exception as exc:
        _fail(exc)


@app.command("rename")
def cmd_rename(
    target: str = typer.Argument(...),
    new_filename: str = typer.Argument(...),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the local mcp_permissions pre-check (web-scope tokens only)",
    ),
    with_body: bool = typer.Option(
        False,
        "--with-body",
        help="Echo the full note body, frontmatter, tags, wikilinks, backlinks (off by default).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Rename a note — thin wrapper over `edit --filename`.

    The family prefix (symbol, or source+symbol) is immutable; this command
    refuses to change it client-side for a clean error.
    """
    cmd_edit(
        target=target,
        filename=new_filename,
        title=None,
        body=None,
        body_file=None,
        set_frontmatter=[],
        unset_frontmatter=[],
        add_tag=[],
        remove_tag=[],
        ai=False,
        force=force,
        with_body=with_body,
        json_output=json_output,
    )


# ---- attachments --------------------------------------------------------


@app.command("upload")
def cmd_upload(
    path: Path = typer.Argument(
        ...,
        help="Local file to upload",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    filename: str = typer.Option(
        ...,
        "--filename",
        help="Kasten file-note filename (e.g. 'Scott2019+ Summary.pdf' or '2024-11-10+ scan.pdf')",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Override the attachment's source label; defaults to the server's own inference",
    ),
    content_type: str | None = typer.Option(
        None,
        "--content-type",
        help="Override the content type sent with the upload "
        "(defaults to application/octet-stream)",
    ),
    tag: list[str] = typer.Option([], "--tag", help="Tag to add to the created note (repeatable)"),
    with_body: bool = typer.Option(
        False,
        "--with-body",
        help="Echo the full note body, frontmatter, tags, wikilinks, backlinks (off by default).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Upload a file and create a linked file-family note.

    Two steps, atomic from the caller's perspective:

      1. POST the file bytes to `/api/attachments` (multipart) — the server
         returns a short `storageKey`.
      2. POST a file-family note whose frontmatter `attachment` field points
         at that key.

    The created note is then refreshed into the local mirror. `--filename`
    must use a `CiteKey+` or `YYYY-MM-DD+` prefix — the server's file-family
    shape requires it.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings)
        with acquire_lock(settings.lock_file), Store(settings.index_path) as store:
            with NotesClient(settings) as client:
                note, upload = upload_file_remote(
                    client=client,
                    store=store,
                    vault_dir=settings.vault_dir,
                    source_path=path,
                    filename=filename,
                    tags=list(tag),
                    source=source,
                    content_type=content_type,
                )
            if with_body:
                payload = read_note_full(
                    store, settings.vault_dir, note.id, include_backlinks=False
                )
            else:
                payload = summarize_note(store, settings.vault_dir, note.id)
        payload["upload"] = {
            "storage_key": upload.get("storageKey"),
            "content_type": upload.get("contentType"),
            "size_bytes": upload.get("sizeBytes"),
            "url": upload.get("url"),
        }
        if mode.json:
            emit_json(payload)
        else:
            render_note(payload, mode=mode, minimal=not with_body)
            log(
                f"uploaded {upload.get('storageKey')} ({upload.get('sizeBytes')} bytes)",
                mode=mode,
            )
    except Exception as exc:
        _fail(exc)


@app.command("download")
def cmd_download(
    target: str = typer.Argument(..., help="File-family note UUID or filename (or prefix)"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Destination path. Defaults to ./<note filename> in the current directory.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Download the attachment linked to a file-family note.

    Resolves the target locally, reads the `attachment` storage key from the
    note's frontmatter, and streams `GET /api/attachments/{key}` to disk.
    Refuses targets that are not file-family or that have no attachment key.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings)
        with Store(settings.index_path) as store:
            with NotesClient(settings) as client:
                result = download_file_remote(
                    client=client,
                    store=store,
                    target=target,
                    destination=output,
                )
        payload = {
            "note_id": result["note_id"],
            "filename": result["filename"],
            "storage_key": result["storage_key"],
            "path": str(result["path"].resolve()),
            "bytes_written": result["bytes_written"],
            "content_type": result["content_type"],
        }
        if mode.json:
            emit_json(payload)
        else:
            log(
                f"downloaded {result['bytes_written']} bytes → {payload['path']}",
                mode=mode,
            )
    except Exception as exc:
        _fail(exc)


# ---- status / config / reset -------------------------------------------


@app.command("status")
def cmd_status(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show local mirror status — no network."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        state = load_state(settings.state_file)
        with Store(settings.index_path) as store:
            local_total = store.count_notes()
            restricted_total = store.count_restricted()
            cardinality = store.fts_cardinality_check()
            # Read schema_version from the store (sync_meta) — the
            # state.json value is a human-readable copy and can drift if a
            # migration happened without updating state.json.
            store_schema_version = int(store.get_meta("schema_version") or 0)
        since_sync = _seconds_since(state.last_sync_at)
        payload = {
            "api_url": settings.api_url,
            "vault_path": str(settings.vault_dir),
            "state_path": str(settings.state_dir),
            "local_total": local_total,
            "restricted_total": restricted_total,
            "last_sync_at": state.last_sync_at,
            "seconds_since_last_sync": since_sync,
            "last_full_sync_at": state.last_full_sync_at,
            "last_remote_total": state.last_remote_total,
            "fts_consistent": cardinality["consistent"],
            "fts_count": cardinality["fts_count"],
            "schema_version": store_schema_version,
            "db_size_bytes": settings.index_path.stat().st_size
            if settings.index_path.exists()
            else 0,
        }
        render_status(payload, mode=mode)
    except Exception as exc:
        _fail(exc)


def _seconds_since(iso_timestamp: str | None) -> int | None:
    if not iso_timestamp:
        return None
    from datetime import datetime

    try:
        dt = datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int((datetime.now(tz=UTC) - dt).total_seconds())


@app.command("config")
def cmd_config(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show effective config (token redacted)."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        payload = {
            "api_url": settings.api_url,
            "api_token": settings.token_redacted,
            "http_timeout": settings.http_timeout,
            "home": str(settings.home),
            "vault_dir": str(settings.vault_dir),
            "state_dir": str(settings.state_dir),
        }
        render_status(payload, mode=mode)
    except Exception as exc:
        _fail(exc)


@app.command("reset")
def cmd_reset(
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Delete the local mirror. Next sync will be forced full."""
    try:
        settings = _load()
        if not yes:
            confirmed = typer.confirm(
                f"Really delete {settings.state_dir} and {settings.vault_dir}?",
                default=False,
            )
            if not confirmed:
                raise typer.Exit(0)
        import shutil

        if settings.state_dir.exists():
            shutil.rmtree(settings.state_dir)
        if settings.vault_dir.exists():
            shutil.rmtree(settings.vault_dir)
    except Exception as exc:
        _fail(exc)


if __name__ == "__main__":
    app()
