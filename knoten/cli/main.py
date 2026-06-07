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

import json
import sys
from dataclasses import asdict
from datetime import UTC
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer

from knoten import __version__
from knoten.cli.config import config_app, init_command
from knoten.cli.inbox import inbox_app
from knoten.cli.mcp_server import mcp_app
from knoten.cli.output import (
    OutputMode,
    emit_json,
    log,
    make_progress_callback,
    render_backlinks,
    render_counts,
    render_dry_run,
    render_note,
    render_search_hits,
    render_status,
    render_summary_list,
    render_sync_result,
    render_unresolved,
)
from knoten.cli.skill import skill_app
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
    ValidationError,
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
    preview_create,
    preview_edit,
    preview_reference,
    read_note_full,
    resolve_target,
    restore_note_remote,
    source_to_reference_inputs,
    summarize_note,
    upload_file_remote,
)
from knoten.services.reconcile import reconcile_local
from knoten.services.reindex import reindex_from_files
from knoten.services.sync import full_sync, incremental_sync
from knoten.settings import MODE_LOCAL, Settings, load_settings

app = typer.Typer(
    help=(
        "Standalone CLI zettelkasten — local Markdown vault + SQLite FTS5 "
        "search, with optional remote-backend sync."
    ),
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(config_app, name="config")
app.add_typer(inbox_app, name="inbox")
app.add_typer(skill_app, name="skill")
app.add_typer(mcp_app, name="mcp")


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Print version and exit.",
        is_eager=True,
    ),
) -> None:
    """Root callback — handles the global `--version` flag.

    The `invoke_without_command=True` + `no_args_is_help=True` combo lets
    `knoten --version` short-circuit without triggering the usage text; a
    bare `knoten` with no subcommand still falls through to the help view.

    Also prints a stderr warning when knoten is in local-only mode after a
    previous remote sync — i.e. a config drift that would otherwise silently
    reroute writes into the local vault and lose them on next sync.
    """
    if version:
        typer.echo(f"knoten {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand and ctx.invoked_subcommand != "config":
        _maybe_emit_local_mode_banner()


def _maybe_emit_local_mode_banner() -> None:
    """Warn on stderr when knoten is in degraded local mode after remote sync.

    Quiet for fresh installs (no prior sync) and for users who explicitly opted
    into local-only operation via `KNOTEN_MODE=local`. Loud when the vault has
    a `last_sync_at` recorded — that means the user was previously talking to
    a remote backend and something has gone missing.
    """
    try:
        settings = load_settings()
    except Exception:
        return  # settings load itself failed — let the actual command surface it
    if settings.effective_mode != "local" or settings.mode == MODE_LOCAL:
        return
    state = load_state(settings.paths.state_file)
    if state.last_sync_at is None:
        return
    sys.stderr.write(
        "warning: knoten is in local-only mode (KNOTEN_API_URL is empty), but this vault was "
        f"previously synced from a remote backend (last_sync_at={state.last_sync_at!r}). "
        "Writes will not propagate to the remote and may be reconciled away on the next sync.\n"
        f"  Restore your remote config at {settings.paths.env_file}, or set KNOTEN_MODE=local "
        "to silence this warning.\n"
    )


@app.command("init")
def cmd_init() -> None:
    """Create vault + state dirs and seed a default .env if missing."""
    init_command()


@app.command("schema")
def cmd_schema(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Dump the machine-readable CLI contract — commands, flags, families,
    permission ladder, and error kinds. No network, no vault access.

    Lets a client (or an LLM) self-orient from one call instead of relying
    on prose docs: every command and its flags are introspected from the
    live app, and the family/permission/error tables are read from the
    modules that own them, so the output never drifts from reality.
    """
    mode = OutputMode.detect(json_output)
    try:
        from knoten.services.schema import build_schema

        payload = build_schema()
        if mode.json:
            emit_json(payload)
        else:
            from rich.console import Console

            console = Console()
            console.print(
                f"[bold]knoten {payload['version']}[/bold] — {len(payload['commands'])} commands"
            )
            console.print(
                "[bold]families:[/bold] "
                + ", ".join(f"{f['prefix']}→{f['family']}" for f in payload["families"])
            )
            console.print("[bold]permissions:[/bold] " + " < ".join(payload["permissions"]))
            console.print(
                "[bold]errors:[/bold] "
                + ", ".join(f"{e['error']}({e['code']})" for e in payload["errors"])
            )
            console.print("[dim]pass --json for the full contract[/dim]")
    except Exception as exc:
        _fail(exc, mode=mode)


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


def _require_token(settings: Settings, *, for_write: str | None = None) -> None:
    """Verify the CLI is configured to talk to a remote backend (when needed).

    `for_write` is accepted but only used as a no-op marker for now — local
    writes are intentionally allowed in any mode (offline-first). The local
    mirror tracks them via `notes.synced` and the next remote `sync` pushes
    them upstream rather than reconciling them away. The argument is kept on
    the signature so callsites stay self-documenting.
    """
    del for_write  # currently unused — kept for callsite documentation
    if settings.effective_mode == "remote" and not settings.api_token:
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
    if isinstance(exc, ValidationError):
        return 1, "validation"
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
    if isinstance(exc, ValidationError):
        return {"issues": exc.issues}
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
    """Pull new/changed notes from the configured remote backend into the local mirror.

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
        help="Only include notes at this permission level or higher "
        "(NONE/LIST/READ/APPEND/WRITE/ALL)",
    ),
    max_permission: str | None = typer.Option(
        None,
        "--max-permission",
        help="Only include notes at this permission level or lower",
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
            hint = _family_kind_hint(store, kind=kind, total=total)
        payload = {
            "query": query,
            "total": total,
            "limit": limit,
            "offset": offset,
            "hits": [hit_to_dict(h) for h in hits],
            "source": source,
        }
        if hint:
            payload["hint"] = hint
        render_search_hits(payload, mode=mode)
        if hint and not mode.json:
            sys.stderr.write(f"hint: {hint}\n")
    except Exception as exc:
        _fail(exc, mode=mode)


def _family_kind_hint(store: Store, *, kind: str | None, total: int) -> str | None:
    """Suggest `--family <kind>` when `--kind <kind>` returned no hits.

    Several kind names are also family names (e.g. `reference`), and a user
    typing `--kind reference` to filter for references would silently miss
    every book / article / web / media row because those have their own
    literal `kind` value. When the literal-kind query returns 0 but the
    family-by-the-same-name has matches, we surface that as a hint instead
    of letting the user assume their data is missing.
    """
    if total > 0 or kind is None:
        return None
    rows = store.conn.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE family = ?", (kind,)
    ).fetchone()
    if rows is None:
        return None
    family_total = int(rows["c"])
    if family_total <= 0:
        return None
    return (
        f"--kind {kind!r} matched 0 rows, but {family_total} note(s) live in the "
        f"{kind!r} family under other kinds. Did you mean --family {kind}?"
    )


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
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print the absolute mirror path for a note.

    Plain one-line path by default (grep-friendly); `{"id", "filename",
    "path"}` with `--json`.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.paths.index_path) as store:
            row = resolve_target(store, target)
        absolute = str((settings.paths.vault_dir / row["path"]).resolve())
        if mode.json:
            emit_json({"id": row["id"], "filename": row["filename"], "path": absolute})
        else:
            sys.stdout.write(absolute + "\n")
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("list")
def cmd_list(
    family: str | None = typer.Option(None, "--family"),
    kind: str | None = typer.Option(None, "--kind"),
    tag: str | None = typer.Option(None, "--tag"),
    source: str | None = typer.Option(None, "--source"),
    min_permission: str | None = typer.Option(
        None,
        "--min-permission",
        help="Only include notes at this permission level or higher",
    ),
    max_permission: str | None = typer.Option(
        None,
        "--max-permission",
        help="Only include notes at this permission level or lower",
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
            hint = _family_kind_hint(store, kind=kind, total=total)
        payload = {"total": total, "limit": limit, "offset": offset, "notes": notes}
        if hint:
            payload["hint"] = hint
        render_summary_list(payload, mode=mode)
        if hint and not mode.json:
            sys.stderr.write(f"hint: {hint}\n")
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


@app.command("unresolved")
def cmd_unresolved(
    limit: int = typer.Option(0, "--limit", min=0, help="Max distinct targets to show (0 = all)"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List dangling wiki-link targets — links pointing at notes that don't
    exist yet — grouped by target, with the notes that reference each. No
    network. Use after a write to find the stubs you still need to create.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.paths.index_path) as store:
            rows = store.unresolved_wikilinks()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["target_title"], []).append(
                {
                    "id": row["source_id"],
                    "filename": row["source_filename"],
                    "title": row["source_title"],
                }
            )
        targets = [
            {"target": title, "reference_count": len(sources), "referenced_by": sources}
            for title, sources in grouped.items()
        ]
        if limit:
            targets = targets[:limit]
        render_unresolved({"total": len(grouped), "targets": targets}, mode=mode)
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("citekeys")
def cmd_citekeys(
    prefix: str | None = typer.Option(
        None,
        "--prefix",
        help="Only CiteKeys starting with this string (case-sensitive prefix match).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List the vault's in-use CiteKeys — the distinct, non-empty `source`
    frontmatter values across all notes, sorted ascending. No network.

    These are the CiteKeys already taken in the vault. The plain output is
    one CiteKey per line so it pipes straight into a collision-aware minting
    tool (`knoten citekeys | quelle resolve <x> --taken-file -`); `--prefix`
    narrows it to a single author/site family.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.paths.index_path) as store:
            citekeys = store.distinct_citekeys(prefix=prefix)
        if mode.json:
            emit_json({"citekeys": citekeys, "count": len(citekeys), "prefix": prefix})
        else:
            for citekey in citekeys:
                sys.stdout.write(f"{citekey}\n")
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
    filename: str | None = typer.Option(
        None,
        "--filename",
        help="Full Kasten filename (e.g. '! Core idea'). Omit when using --batch.",
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
    batch: Path | None = typer.Option(
        None,
        "--batch",
        help="Create many notes from a JSON array of drafts (use '-' for stdin), one lock "
        "pass. Each item: {filename, body?, kind?, tags?, frontmatter?, ai?}.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Resolve and validate without creating; report family/kind/source + unresolved links.",
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
    """Create a new note on the configured remote backend and mirror it locally."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        if batch is not None:
            if filename is not None:
                raise UserError("--filename and --batch are mutually exclusive")
            _run_create_batch(settings, batch, mode=mode, dry_run=dry_run)
            return
        if filename is None:
            raise UserError("pass --filename <name> (or --batch <file> for bulk create)")
        _require_token(settings, for_write="create")
        body_text = _resolve_body(body, body_file)
        if ai:
            if body_text is None:
                raise UserError("--ai requires --body or --body-file")
            body_text = _wrap_ai(body_text)
        frontmatter = _load_frontmatter_file(frontmatter_file)
        if dry_run:
            with Store(settings.paths.index_path) as store:
                preview = preview_create(
                    store,
                    filename=filename,
                    body=body_text,
                    kind=kind,
                    tags=list(tag),
                    frontmatter=frontmatter,
                )
            render_dry_run(preview, mode=mode)
            return
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


def _draft_from_batch_item(index: int, item: Any) -> dict[str, Any]:
    """Validate one --batch item and return create_note_remote() kwargs."""
    if not isinstance(item, dict):
        raise UserError(f"--batch item {index} is not a JSON object")
    filename = item.get("filename")
    if not isinstance(filename, str) or not filename:
        raise UserError(f"--batch item {index} is missing a 'filename' string")
    body = item.get("body")
    if body is not None and not isinstance(body, str):
        raise UserError(f"--batch item {index} 'body' must be a string")
    if item.get("ai"):
        if body is None:
            raise UserError(f"--batch item {index} sets 'ai' but has no 'body'")
        body = _wrap_ai(body)
    tags = item.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise UserError(f"--batch item {index} 'tags' must be an array of strings")
    kind = item.get("kind")
    if kind is not None and not isinstance(kind, str):
        raise UserError(f"--batch item {index} 'kind' must be a string")
    frontmatter = item.get("frontmatter")
    if frontmatter is not None and not isinstance(frontmatter, dict):
        raise UserError(f"--batch item {index} 'frontmatter' must be an object")
    return {
        "filename": filename,
        "body": body,
        "kind": kind,
        "tags": list(tags),
        "frontmatter": frontmatter,
    }


def _read_batch_items(batch_path: Path) -> list[Any]:
    raw = sys.stdin.read() if str(batch_path) == "-" else batch_path.read_text(encoding="utf-8")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UserError(f"--batch input is not valid JSON: {exc}") from exc
    if not isinstance(items, list):
        raise UserError("--batch input must be a JSON array of draft objects")
    return items


def _run_create_batch(
    settings: Settings, batch_path: Path, *, mode: OutputMode, dry_run: bool
) -> None:
    """Create many notes under one lock pass; never aborts on a single bad draft.

    Always emits a JSON summary on stdout (batch is a machine-oriented path):
    `{operation, count, created, failed, results: [{index, ok, id|error}]}`.
    A failed item carries `error`/`code`/`message`; the rest still run.
    """
    items = _read_batch_items(batch_path)
    _require_token(settings, for_write="create")
    results: list[dict[str, Any]] = []

    if dry_run:
        with Store(settings.paths.index_path) as store:
            for index, item in enumerate(items):
                try:
                    draft = _draft_from_batch_item(index, item)
                    preview = preview_create(
                        store,
                        filename=draft["filename"],
                        body=draft["body"],
                        kind=draft["kind"],
                        tags=draft["tags"],
                        frontmatter=draft["frontmatter"],
                    )
                    results.append({"index": index, "ok": True, **preview})
                except Exception as exc:
                    code, kind = _classify_error(exc)
                    results.append(
                        {
                            "index": index,
                            "ok": False,
                            "error": kind,
                            "code": code,
                            "message": str(exc),
                        }
                    )
        emit_json(
            {
                "operation": "create-batch",
                "dry_run": True,
                "count": len(results),
                "results": results,
            }
        )
        return

    with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
        with _build_backend(settings) as backend:
            for index, item in enumerate(items):
                try:
                    draft = _draft_from_batch_item(index, item)
                    note = create_note_remote(
                        backend=backend,
                        store=store,
                        vault_dir=settings.paths.vault_dir,
                        **draft,
                    )
                    results.append(
                        {"index": index, "ok": True, "id": note.id, "filename": note.filename}
                    )
                except Exception as exc:
                    code, kind = _classify_error(exc)
                    results.append(
                        {
                            "index": index,
                            "ok": False,
                            "error": kind,
                            "code": code,
                            "message": str(exc),
                            **_error_extras(exc),
                        }
                    )
    created = sum(1 for r in results if r.get("ok"))
    emit_json(
        {
            "operation": "create-batch",
            "count": len(results),
            "created": created,
            "failed": len(results) - created,
            "results": results,
        }
    )


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


def _load_source_json(path: Path) -> dict[str, Any]:
    """Read a quelle Source JSON object from a file (or '-' for stdin)."""
    raw = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UserError(f"--from-source input is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise UserError("--from-source input must be a JSON object (a quelle Source)")
    return parsed


@app.command("reference")
def cmd_reference(
    from_source: Path = typer.Option(
        ...,
        "--from-source",
        help="quelle Source JSON object — a file path, or '-' to read stdin.",
    ),
    body: str | None = typer.Option(None, "--body"),
    body_file: Path | None = typer.Option(None, "--body-file"),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Wrap the body in `#ai begin` / `#ai end` markers (AI-authored content).",
    ),
    tag: list[str] = typer.Option([], "--tag"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Resolve and validate without creating; report the mapped kind, "
        "frontmatter, and unresolved links.",
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
    """Create a CiteKey-anchored reference note from a quelle Source JSON.

    Maps a quelle `Publication` dict (snake_case) to a knoten reference note:
    the filename is `<CiteKey>= <Title>`, the quelle `kind` maps to a knoten
    reference kind, and the frontmatter is rebuilt in knoten's hyphen-key
    convention. The CiteKey is `x_vcoeur.citekey` when present, else the
    top-level `citation_key`. Pass `--body`/`--body-file` for a summary blurb.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        source = _load_source_json(from_source)
        body_text = _resolve_body(body, body_file)
        if ai:
            if body_text is None:
                raise UserError("--ai requires --body or --body-file")
            body_text = _wrap_ai(body_text)
        inputs = source_to_reference_inputs(source, ai=ai)
        # Helper-derived tags (e.g. `ai`) come first; caller `--tag`s extend
        # them, de-duplicated while preserving order.
        tags = list(dict.fromkeys([*inputs.tags, *tag]))
        if dry_run:
            with Store(settings.paths.index_path) as store:
                preview = preview_reference(
                    store,
                    filename=inputs.filename,
                    kind=inputs.kind,
                    body=body_text,
                    tags=tags,
                    frontmatter=inputs.frontmatter,
                )
            render_dry_run(preview, mode=mode)
            return
        _require_token(settings, for_write="reference")
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                note = create_note_remote(
                    backend=backend,
                    store=store,
                    vault_dir=settings.paths.vault_dir,
                    filename=inputs.filename,
                    body=body_text,
                    kind=inputs.kind,
                    tags=tags,
                    frontmatter=inputs.frontmatter,
                )
            payload = _write_response(store, settings.paths.vault_dir, note.id, fields)
        render_note(payload, mode=mode, minimal=fields is Fields.minimal)
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("edit")
def cmd_edit(
    target: str = typer.Argument(...),
    filename: str | None = typer.Option(None, "--filename"),
    title: str | None = typer.Option(None, "--title"),
    body: str | None = typer.Option(None, "--body"),
    body_file: Path | None = typer.Option(None, "--body-file"),
    set_frontmatter: list[str] = typer.Option([], "--set-frontmatter"),
    set_frontmatter_json: list[str] = typer.Option(
        [],
        "--set-frontmatter-json",
        help="key=<json-literal> — set a TYPED frontmatter value (int/list/bool/null round-trip). "
        "Use this for numbers/lists; --set-frontmatter sends strings only.",
    ),
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
        help="Bypass the local permissions pre-check (web-scope tokens only)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate (permissions, prefix, changes) without writing; report unresolved links.",
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
    """Edit a note on the configured remote backend and refresh the local mirror."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings, for_write="edit")
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
        fm_json_sets: dict[str, Any] = {}
        for pair in set_frontmatter_json:
            if "=" not in pair:
                raise UserError(f"--set-frontmatter-json expects key=<json>, got '{pair}'")
            key, _, raw = pair.partition("=")
            try:
                fm_json_sets[key] = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise UserError(
                    f"--set-frontmatter-json {key}=… value is not valid JSON: {exc}"
                ) from exc
        if dry_run:
            with Store(settings.paths.index_path) as store:
                preview = preview_edit(
                    store,
                    settings.paths.vault_dir,
                    target=target,
                    new_filename=filename,
                    new_title=title,
                    new_body=body_text,
                    set_frontmatter=fm_sets,
                    set_frontmatter_json=fm_json_sets,
                    unset_frontmatter=list(unset_frontmatter),
                    add_tags=list(add_tag),
                    remove_tags=list(remove_tag),
                    force=force,
                )
            render_dry_run(preview, mode=mode)
            return
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
                    set_frontmatter_json=fm_json_sets,
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
        help="Bypass the local permissions pre-check (web-scope tokens only)",
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
        _require_token(settings, for_write="append")
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
        help="Bypass the local permissions pre-check (web-scope tokens only)",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Soft-delete a note (move to trash on the configured remote backend)."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings, for_write="delete")
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
        _require_token(settings, for_write="restore")
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
        help="Bypass the local permissions pre-check (web-scope tokens only)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the rename (permissions, immutable prefix) without writing.",
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
        set_frontmatter_json=[],
        unset_frontmatter=[],
        add_tag=[],
        remove_tag=[],
        ai=False,
        force=force,
        dry_run=dry_run,
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
        _require_token(settings, for_write="upload")
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
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Delete the local mirror. Next sync will be forced full."""
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        if mode.json and not yes:
            raise UserError("In --json mode you must pass --yes to confirm reset")
        if not yes:
            confirmed = typer.confirm(
                f"Really delete {settings.paths.cache_dir} and {settings.paths.vault_dir}?",
                default=False,
            )
            if not confirmed:
                log("aborted", mode=mode)
                return
        import shutil

        cache_removed = settings.paths.cache_dir.exists()
        vault_removed = settings.paths.vault_dir.exists()
        if cache_removed:
            shutil.rmtree(settings.paths.cache_dir)
        if vault_removed:
            shutil.rmtree(settings.paths.vault_dir)
        if mode.json:
            emit_json(
                {
                    "reset": True,
                    "cache_dir": str(settings.paths.cache_dir),
                    "vault_dir": str(settings.paths.vault_dir),
                    "cache_removed": cache_removed,
                    "vault_removed": vault_removed,
                }
            )
    except Exception as exc:
        _fail(exc, mode=mode)


if __name__ == "__main__":
    app()
