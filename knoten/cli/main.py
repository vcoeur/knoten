"""Typer CLI entrypoint for the `knoten` command.

Each subcommand is a thin wrapper: parse flags, resolve Settings, open the
Store (and RemoteBackend when remote access is needed), call into a service,
render the result via `knoten.cli.output`.

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
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer

from knoten.cli.config import config_app, init_command
from knoten.cli.output import (
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
from knoten.repositories.backend import Backend
from knoten.repositories.errors import (
    AmbiguousTargetError,
    AuthError,
    ConfigError,
    KnotenError,
    LockTimeoutError,
    NetworkError,
    NotFoundError,
    StoreError,
    UserError,
)
from knoten.repositories.errors import (
    PermissionError as LocalPermissionError,
)
from knoten.repositories.local_backend import LocalBackend
from knoten.repositories.lock import acquire_lock
from knoten.repositories.remote_backend import RemoteBackend
from knoten.repositories.store import Store
from knoten.repositories.sync_state import load_state
from knoten.services.notes import (
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
from knoten.services.reconcile import reconcile_local
from knoten.services.reindex import reindex_from_files
from knoten.services.sync import full_sync, incremental_sync
from knoten.settings import Settings, load_settings

app = typer.Typer(
    help="Local CLI mirror and search for notes.vcoeur.com. Designed for Claude.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(config_app, name="config")


@app.command("init")
def cmd_init() -> None:
    """Create vault + state dirs and seed a default .env if missing."""
    init_command()


class Fields(StrEnum):
    """Post-write response shape for mutation commands.

    `minimal` returns identity + metadata + tags only (no body, no
    wikilinks, no backlinks). `full` returns the same shape as `knoten
    read` — body, frontmatter, wikilinks, backlinks. Default is `minimal`
    because most callers only need to confirm the note identity.
    """

    minimal = "minimal"
    full = "full"


# ---- global state -------------------------------------------------------


def _load() -> Settings:
    return load_settings()


def _require_token(settings: Settings) -> None:
    if settings.effective_mode == "local":
        # Local mode has no server to authenticate against. The token is
        # meaningless and every command works without it.
        return
    if not settings.api_token:
        raise ConfigError(
            "KNOTEN_API_TOKEN is not set. Copy .env.example to .env and add an API token."
        )


def _build_backend(settings: Settings) -> Backend:
    """Construct the backend implementation selected by settings.

    Selection: `effective_mode` resolves `KNOTEN_MODE=auto` to `local` when
    `KNOTEN_API_URL` is empty and `remote` otherwise. Explicit `remote` /
    `local` are honoured as-is. Local mode requires no network and no
    token — any user can run knoten against a plain on-disk vault.
    """
    if settings.effective_mode == "local":
        return LocalBackend(settings)
    if not settings.api_url:
        raise ConfigError(
            "KNOTEN_MODE=remote requires KNOTEN_API_URL to be set "
            "(or unset KNOTEN_MODE to fall back to local mode)."
        )
    return RemoteBackend(settings)


def _classify_error(exc: Exception) -> tuple[int, str]:
    """Map an exception to (exit_code, error_kind).

    Order matters — subclasses are checked before their bases so the most
    specific classification wins. `error_kind` is the machine-parseable
    string that goes into the JSON error envelope's `error` field.
    """
    if isinstance(exc, ConfigError):
        return 4, "config"
    if isinstance(exc, AuthError):
        return 2, "auth"
    if isinstance(exc, NetworkError):
        return 2, "network"
    if isinstance(exc, StoreError):
        return 3, "store"
    if isinstance(exc, LockTimeoutError):
        return 5, "lock_timeout"
    if isinstance(exc, LocalPermissionError):
        return 1, "permission_denied"
    if isinstance(exc, AmbiguousTargetError):
        return 1, "ambiguous_target"
    if isinstance(exc, NotFoundError):
        return 1, "not_found"
    if isinstance(exc, UserError):
        return 1, "user"
    if isinstance(exc, KnotenError):
        return 1, "knoten"
    return 1, "unknown"


def _error_extras(exc: Exception) -> dict[str, Any]:
    """Error-specific fields that go into the JSON error envelope."""
    if isinstance(exc, LocalPermissionError):
        return {
            "note_id": exc.note_id,
            "filename": exc.filename,
            "current_level": exc.current_level,
            "required_level": exc.required_level,
            "operation": exc.operation,
        }
    if isinstance(exc, AmbiguousTargetError):
        return {"candidates": exc.candidates}
    return {}


def _fail(exc: Exception, *, mode: OutputMode | None = None) -> None:
    """Print an error and exit with the appropriate code.

    When `mode.json` is true, emits a structured error envelope to stdout
    so Claude can parse it with jq. Otherwise writes a plain-text line to
    stderr, preserving the existing UX for humans on a TTY. Commands that
    have no `--json` flag (`path`, `reset`) pass `mode=None` and always
    go through the stderr path.
    """
    code, kind = _classify_error(exc)
    if mode is not None and mode.json:
        payload: dict[str, Any] = {
            "error": kind,
            "message": str(exc),
            "code": code,
            **_error_extras(exc),
        }
        emit_json(payload)
    else:
        sys.stderr.write(f"error: {exc}\n")
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
        if settings.effective_mode == "local":
            # Local mode has no server to sync from. `knoten sync` becomes
            # a stat-walk reindex: the backend walks the vault on its
            # first read-path call and catches up external edits.
            progress("→ Local mode: running reindex walk (no network)")
            with _build_backend(settings) as backend:
                page = backend.list_note_summaries(limit=1, offset=0)
            payload = {
                "mode": "local",
                "total": page.total,
                "message": "Local mode — vault reindexed from disk.",
            }
            render_sync_result(payload, mode=mode)
            return
        _require_token(settings)
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                if full:
                    result = full_sync(
                        backend=backend,
                        store=store,
                        settings=settings,
                        verify_hashes=verify,
                        progress=progress,
                    )
                else:
                    result = incremental_sync(
                        backend=backend,
                        store=store,
                        settings=settings,
                        verify_hashes=verify,
                        progress=progress,
                    )
            payload = asdict(result)
            render_sync_result(payload, mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


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

    If the FTS5 cardinality check shows drift, run `knoten reindex` to
    rebuild the derived tables from the on-disk files without a network hit.
    """
    mode = OutputMode.detect(json_output)
    progress = make_progress_callback(mode)
    try:
        settings = _load()
        _require_token(settings)
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
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
            with _build_backend(settings) as backend:
                result = reconcile_local(
                    backend=backend,
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
                    "[yellow]FTS5 drift detected — run `knoten reindex` to rebuild "
                    "the derived tables from on-disk files.[/yellow]"
                )
            if result.missing_ids:
                console.print(f"  re-fetched missing: {', '.join(result.missing_ids[:10])}")
            if result.orphan_paths:
                console.print(f"  orphans removed: {', '.join(result.orphan_paths[:10])}")
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("reindex")
def cmd_reindex(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Rebuild the derived index (FTS5, tags, wikilinks, frontmatter_fields)
    from the `notes` rows and the on-disk mirror files. No network.

    Use this when `knoten verify` reports FTS5 drift, when SQLite's integrity
    check complains about derived tables, or when you want a quick offline
    rebuild without re-fetching every note from the remote.

    Notes whose mirror file is missing are skipped and reported — follow up
    with `knoten verify` (which has network access) to pull them back.
    """
    mode = OutputMode.detect(json_output)
    progress = make_progress_callback(mode)
    try:
        settings = _load()
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
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
                    f"{', '.join(result.missing_file_ids[:10])} — run `knoten verify`"
                )
    except Exception as exc:
        _fail(exc, mode=mode)


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
    fuzzy: bool = typer.Option(
        False,
        "--fuzzy",
        help="Typo-tolerant + substring search (trigram FTS + rapidfuzz on titles)",
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        help="Attach a per-column bm25 breakdown to each hit (title/body/filename). "
        "Local, ranked search only.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Full-text search against the local index."""
    mode = OutputMode.detect(json_output)
    try:
        if explain and fuzzy:
            raise UserError(
                "--explain only applies to ranked unicode61 search; drop --fuzzy to use it"
            )
        settings = _load()
        with Store(settings.paths.index_path) as store:
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
                    vault_dir=settings.paths.vault_dir,
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
                    vault_dir=settings.paths.vault_dir,
                    explain=explain,
                )
                source = "local"
        payload = {
            "query": query,
            "total": total,
            "limit": limit,
            "offset": offset,
            "hits": [hit_to_dict(h) for h in hits],
            "source": source,
        }
        render_search_hits(payload, mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


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
        with Store(settings.paths.index_path) as store:
            payload = read_note_full(
                store,
                settings.paths.vault_dir,
                target,
                include_backlinks=not no_backlinks,
            )
        render_note(payload, mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("path")
def cmd_path(
    target: str = typer.Argument(..., help="Note UUID or filename (or prefix)"),
) -> None:
    """Print the absolute mirror path for a note. One line, no JSON envelope."""
    try:
        settings = _load()
        with Store(settings.paths.index_path) as store:
            row = resolve_target(store, target)
        sys.stdout.write(str((settings.paths.vault_dir / row["path"]).resolve()) + "\n")
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
        with Store(settings.paths.index_path) as store:
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
            vault_dir = settings.paths.vault_dir
            notes = list_summaries_to_dicts(summaries, vault_dir=vault_dir, store=store)
        payload = {"total": total, "limit": limit, "offset": offset, "notes": notes}
        render_summary_list(payload, mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("backlinks")
def cmd_backlinks(
    target: str = typer.Argument(..., help="Note UUID or filename (or prefix)"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List notes that link to the given note."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.paths.index_path) as store:
            row = resolve_target(store, target)
            backlinks = store.backlinks_for_note(row["id"])
            for bl in backlinks:
                bl["absolute_path"] = str((settings.paths.vault_dir / bl["path"]).resolve())
        total = len(backlinks)
        page = backlinks[offset : offset + limit]
        render_backlinks(
            {
                "id": row["id"],
                "total": total,
                "limit": limit,
                "offset": offset,
                "backlinks": page,
            },
            mode=mode,
        )
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("tags")
def cmd_tags(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List all tags with counts, sorted by count DESC."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.paths.index_path) as store:
            rows = store.tag_counts()
        render_counts({"tags": rows}, "tags", mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


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
        with Store(settings.paths.index_path) as store:
            start = resolve_target(store, target)
            nodes, edges, broken = store.graph_neighbourhood(
                start["id"], depth=depth, direction=direction
            )
        for node in nodes.values():
            node["absolute_path"] = str((settings.paths.vault_dir / node["path"]).resolve())
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
        _fail(exc, mode=mode)


@app.command("kinds")
def cmd_kinds(
    family: str | None = typer.Option(None, "--family"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List all kinds with counts, optionally filtered by family."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.paths.index_path) as store:
            rows = store.kind_counts(family=family)
        render_counts({"kinds": rows}, "kinds", mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


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


def _write_response(store: Store, vault_dir: Path, note_id: str, fields: Fields) -> dict[str, Any]:
    """Build the post-write payload for a mutation command.

    `minimal` = identity + metadata + tags (via `summarize_note`).
    `full`    = full read payload with body, frontmatter, wikilinks
                — backlinks are skipped because they're irrelevant to a
                just-written note and cost a DB scan.
    """
    if fields is Fields.full:
        return read_note_full(store, vault_dir, note_id, include_backlinks=False)
    return summarize_note(store, vault_dir, note_id)


@app.command("create")
def cmd_create(
    filename: str = typer.Option(
        ..., "--filename", help="Full Kasten filename (e.g. '! Core idea')"
    ),
    body: str | None = typer.Option(None, "--body"),
    body_file: Path | None = typer.Option(None, "--body-file"),
    kind: str | None = typer.Option(None, "--kind"),
    tag: list[str] = typer.Option([], "--tag"),
    frontmatter_file: Path | None = typer.Option(
        None,
        "--frontmatter-file",
        help="JSON file whose top-level object is merged into the new note's frontmatter.",
    ),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Wrap the body in `#ai begin` / `#ai end` markers (AI-authored content).",
    ),
    fields: Fields = typer.Option(
        Fields.minimal,
        "--fields",
        help="Response shape: `minimal` (id + metadata + tags) or `full` "
        "(body + frontmatter + wikilinks + backlinks).",
        case_sensitive=False,
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
        frontmatter = _load_frontmatter_file(frontmatter_file)
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                note = create_note_remote(
                    backend=backend,
                    store=store,
                    vault_dir=settings.paths.vault_dir,
                    filename=filename,
                    body=body_text,
                    kind=kind,
                    tags=list(tag),
                    frontmatter=frontmatter,
                )
            payload = _write_response(store, settings.paths.vault_dir, note.id, fields)
        render_note(payload, mode=mode, minimal=fields is Fields.minimal)
    except Exception as exc:
        _fail(exc, mode=mode)


def _load_frontmatter_file(path: Path | None) -> dict[str, object] | None:
    """Load a JSON dict from a file, or return None if path is None."""
    if path is None:
        return None
    import json as _json

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserError(f"cannot read --frontmatter-file {path}: {exc}") from exc
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as exc:
        raise UserError(f"--frontmatter-file {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise UserError(f"--frontmatter-file {path} must contain a JSON object at the top level")
    return parsed


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
    fields: Fields = typer.Option(
        Fields.minimal,
        "--fields",
        help="Response shape: `minimal` (id + metadata + tags) or `full` "
        "(body + frontmatter + wikilinks + backlinks).",
        case_sensitive=False,
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
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                note = edit_note_remote(
                    backend=backend,
                    store=store,
                    vault_dir=settings.paths.vault_dir,
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
            payload = _write_response(store, settings.paths.vault_dir, note.id, fields)
        render_note(payload, mode=mode, minimal=fields is Fields.minimal)
    except Exception as exc:
        _fail(exc, mode=mode)


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
    fields: Fields = typer.Option(
        Fields.minimal,
        "--fields",
        help="Response shape: `minimal` (id + metadata + tags) or `full` "
        "(body + frontmatter + wikilinks + backlinks).",
        case_sensitive=False,
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
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                note = append_note_remote(
                    backend=backend,
                    store=store,
                    vault_dir=settings.paths.vault_dir,
                    target=target,
                    content=text,
                    force=force,
                )
            payload = _write_response(store, settings.paths.vault_dir, note.id, fields)
        render_note(payload, mode=mode, minimal=fields is Fields.minimal)
    except Exception as exc:
        _fail(exc, mode=mode)


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
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                note_id = delete_note_remote(
                    backend=backend,
                    store=store,
                    vault_dir=settings.paths.vault_dir,
                    target=target,
                    force=force,
                )
        if mode.json:
            emit_json({"deleted_id": note_id})
        else:
            log(f"deleted {note_id}", mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("restore")
def cmd_restore(
    note_id: str = typer.Argument(...),
    fields: Fields = typer.Option(
        Fields.minimal,
        "--fields",
        help="Response shape: `minimal` (id + metadata + tags) or `full` "
        "(body + frontmatter + wikilinks + backlinks).",
        case_sensitive=False,
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Restore a note from trash."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings)
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            vault_dir = settings.paths.vault_dir
            with _build_backend(settings) as backend:
                note = restore_note_remote(
                    backend=backend, store=store, vault_dir=vault_dir, note_id=note_id
                )
            payload = _write_response(store, vault_dir, note.id, fields)
        render_note(payload, mode=mode, minimal=fields is Fields.minimal)
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("rename")
def cmd_rename(
    target: str = typer.Argument(...),
    new_filename: str = typer.Argument(...),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the local mcp_permissions pre-check (web-scope tokens only)",
    ),
    fields: Fields = typer.Option(
        Fields.minimal,
        "--fields",
        help="Response shape: `minimal` (id + metadata + tags) or `full` "
        "(body + frontmatter + wikilinks + backlinks).",
        case_sensitive=False,
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
        fields=fields,
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
    fields: Fields = typer.Option(
        Fields.minimal,
        "--fields",
        help="Response shape: `minimal` (id + metadata + tags) or `full` "
        "(body + frontmatter + wikilinks + backlinks).",
        case_sensitive=False,
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
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                note, upload = upload_file_remote(
                    backend=backend,
                    store=store,
                    vault_dir=settings.paths.vault_dir,
                    source_path=path,
                    filename=filename,
                    tags=list(tag),
                    source=source,
                    content_type=content_type,
                )
            payload = _write_response(store, settings.paths.vault_dir, note.id, fields)
        payload["upload"] = {
            "storage_key": upload.get("storageKey"),
            "content_type": upload.get("contentType"),
            "size_bytes": upload.get("sizeBytes"),
            "url": upload.get("url"),
        }
        if mode.json:
            emit_json(payload)
        else:
            render_note(payload, mode=mode, minimal=fields is Fields.minimal)
            log(
                f"uploaded {upload.get('storageKey')} ({upload.get('sizeBytes')} bytes)",
                mode=mode,
            )
    except Exception as exc:
        _fail(exc, mode=mode)


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
        with Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                result = download_file_remote(
                    backend=backend,
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
        _fail(exc, mode=mode)


# ---- status / config / reset -------------------------------------------


@app.command("status")
def cmd_status(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show local mirror status — no network."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        state = load_state(settings.paths.state_file)
        with Store(settings.paths.index_path) as store:
            local_total = store.count_notes()
            restricted_total = store.count_restricted()
            cardinality = store.fts_cardinality_check()
            store_schema_version = store.schema_version
        since_sync = _seconds_since(state.last_sync_at)
        payload = {
            "api_url": settings.api_url,
            "vault_path": str(settings.paths.vault_dir),
            "cache_path": str(settings.paths.cache_dir),
            "local_total": local_total,
            "restricted_total": restricted_total,
            "last_sync_at": state.last_sync_at,
            "seconds_since_last_sync": since_sync,
            "last_full_sync_at": state.last_full_sync_at,
            "last_remote_total": state.last_remote_total,
            "fts_consistent": cardinality["consistent"],
            "fts_count": cardinality["fts_count"],
            "schema_version": store_schema_version,
            "db_size_bytes": settings.paths.index_path.stat().st_size
            if settings.paths.index_path.exists()
            else 0,
        }
        render_status(payload, mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


def _seconds_since(iso_timestamp: str | None) -> int | None:
    if not iso_timestamp:
        return None
    from datetime import datetime

    try:
        dt = datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int((datetime.now(tz=UTC) - dt).total_seconds())


@app.command("reset")
def cmd_reset(
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Delete the local mirror. Next sync will be forced full."""
    try:
        settings = _load()
        if not yes:
            confirmed = typer.confirm(
                f"Really delete {settings.paths.cache_dir} and {settings.paths.vault_dir}?",
                default=False,
            )
            if not confirmed:
                raise typer.Exit(0)
        import shutil

        if settings.paths.cache_dir.exists():
            shutil.rmtree(settings.paths.cache_dir)
        if settings.paths.vault_dir.exists():
            shutil.rmtree(settings.paths.vault_dir)
    except Exception as exc:
        _fail(exc)


if __name__ == "__main__":
    app()
