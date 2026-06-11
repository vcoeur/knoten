"""Typer CLI entrypoint for the `knoten` command.

Each subcommand is a thin wrapper: parse flags, resolve Settings, open the
Store (and RemoteBackend when remote access is needed), call into a service,
render the result via `knoten.cli.output`.

Exit codes (mapped from exception types in `app.repositories.errors`):
    0 success
    1 user error
    2 network error, or malformed command line (Click usage error — emitted
      by Click itself, before any command runs, with no JSON envelope)
    3 local store error
    4 config error
    5 lock timeout
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
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
    render_notes,
    render_search_hits,
    render_status,
    render_summary_list,
    render_sync_result,
    render_trash,
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
    RemoteRejectionError,
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
    find_similar,
    hit_to_dict,
    list_summaries_to_dicts,
    list_trash,
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
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


class Fields(StrEnum):
    """Post-write response shape for mutation commands.

    `minimal` returns identity + metadata + tags only (no body, no
    wikilinks, no backlinks). `full` returns the same shape as `knoten
    read` — body, frontmatter, wikilinks, backlinks. Default is `minimal`
    because most callers only need to confirm the note identity.

    Also reused by the read-path `search` / `list` token-budget flag, where
    `minimal` projects each hit / entry down to a small key set at the
    serialization layer (see `_project_hits` / the `list` minimal keys).
    """

    minimal = "minimal"
    full = "full"


class ReadFields(StrEnum):
    """Body-inclusion level for `knoten read`.

    `full` (default) is today's read payload — body included. `meta` omits
    the `body` field entirely while keeping frontmatter, wikilinks, and
    backlinks, for callers that only want a note's metadata + link graph
    without paying for the body text.
    """

    meta = "meta"
    full = "full"


# Per-hit key sets for the `--fields minimal` token-budget projections. The
# projection happens at serialization time (JSON only) so TTY tables, which
# already show a curated subset, keep rendering the full row.
_SEARCH_MINIMAL_KEYS = ("id", "filename", "title", "family", "kind", "score", "snippet")
_LIST_MINIMAL_KEYS = ("id", "filename", "family", "kind", "updated_at")


def _project_hits(hits: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Project each hit / entry dict down to `keys`, dropping the rest."""
    return [{key: hit[key] for key in keys if key in hit} for hit in hits]


def _apply_read_fields(
    payload: dict[str, Any], *, fields: ReadFields, max_body_chars: int | None
) -> dict[str, Any]:
    """Apply the `read` token-budget controls to a full read payload, in place.

    `--fields meta` drops the `body` field outright. `--max-body-chars N`
    truncates `body` to N characters and records the additive
    `body_truncated` / `body_total_chars` fields only when a cut happened —
    so an untruncated payload is byte-for-byte the pre-flag shape.
    """
    if fields is ReadFields.meta:
        payload.pop("body", None)
        return payload
    if max_body_chars is not None:
        body = payload.get("body") or ""
        if len(body) > max_body_chars:
            payload["body"] = body[:max_body_chars]
            payload["body_truncated"] = True
            payload["body_total_chars"] = len(body)
    return payload


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


def _local_stat_walk(settings: Settings, store: Store) -> None:
    """Run the LocalBackend stat walk before a local-mode read query.

    Read commands query the Store directly (no backend), so they must
    trigger the walk themselves to honour the documented contract that
    every invocation picks up external edits. Reuses the command's own
    open Store — a second connection would write concurrently with it.
    No-op in remote mode (the mirror only changes via sync/mutations).
    """
    if settings.effective_mode != "local":
        return
    LocalBackend(settings, store=store).refresh_index()


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
    if isinstance(exc, RemoteRejectionError):
        # Distinct key from the envelope's integer `code` (the exit code) —
        # this carries the server's structured error code string.
        return {"error_code": exc.error_code}
    return {}


def _fail(exc: Exception, *, mode: OutputMode | None = None) -> None:
    """Print an error and exit with the appropriate code.

    When `mode.json` is true, emits a structured error envelope to stdout
    so Claude can parse it with jq. Otherwise writes a plain-text line to
    stderr, preserving the existing UX for humans on a TTY. `mode=None`
    always goes through the stderr path.
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
    force_delete: bool = typer.Option(
        False,
        "--force-delete",
        help=(
            "Override the mass-delete circuit breaker (delete detection refuses to "
            "remove more than 20% of local synced notes without this flag)"
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON to stdout"),
) -> None:
    """Pull new/changed notes from the configured remote backend into the local mirror.

    Every sync (incremental or `--full`) always:

      1. Pushes local writes and deletes (`synced=0` rows, pending trash deletions).
      2. Fetches new/changed notes via pagination — never overwriting a
         note with an unpushed local edit (surfaced as a conflict instead).
      3. Runs delete detection — any note removed on the remote is purged locally,
         unless the remote scan was inconsistent or the mass-delete circuit
         breaker trips (see `--force-delete`).
      4. Reconciles the local mirror — re-fetches any file that is missing
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
            # first read-path call and catches up external edits. The walk
            # writes to the store, so it runs under the advisory lock like
            # every other mutating path.
            progress("→ Local mode: running reindex walk (no network)")
            with acquire_lock(settings.paths.lock_file), _build_backend(settings) as backend:
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
                        force_delete=force_delete,
                        progress=progress,
                    )
                else:
                    result = incremental_sync(
                        backend=backend,
                        store=store,
                        settings=settings,
                        verify_hashes=verify,
                        force_delete=force_delete,
                        progress=progress,
                    )
            payload = asdict(result)
            render_sync_result(payload, mode=mode)
    except typer.Exit:
        raise
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

    In local mode there is no remote to re-fetch from, so only the
    non-destructive checks run: integrity, cardinality, and the stat-walk
    that catches up external edits. No orphan sweep, no re-fetch — the
    vault on disk is the source of truth, not a mirror to repair.
    """
    mode = OutputMode.detect(json_output)
    progress = make_progress_callback(mode)
    try:
        settings = _load()
        if settings.effective_mode == "local":
            _verify_local(settings, mode=mode, progress=progress)
            return
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
            "skipped_invalid": result.skipped_invalid,
            "warnings": result.warnings,
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
            for warning in result.warnings:
                console.print(f"[yellow]⚠ {warning}[/yellow]")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


def _verify_local(
    settings: Settings,
    *,
    mode: OutputMode,
    progress: Callable[[str], None],
) -> None:
    """Local-mode `knoten verify` — non-destructive checks only.

    Runs the SQLite integrity check, the FTS5 cardinality check, and the
    LocalBackend stat-walk (catches up external edits). Deliberately does
    NOT run `reconcile_local`: its orphan sweep and re-fetch are designed
    for a mirror with a remote authority — against an authoritative local
    vault they would delete `.trash/`/`.attachments/` content and rewrite
    user files.
    """
    with acquire_lock(settings.paths.lock_file):
        progress("→ Local mode: running stat-walk reindex (no orphan sweep, no re-fetch)")
        with _build_backend(settings) as backend:
            backend.list_note_summaries(limit=1, offset=0)
        with Store(settings.paths.index_path) as store:
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
    payload = {
        "mode": "local",
        "integrity": integrity,
        "cardinality": cardinality,
    }
    if mode.json:
        emit_json(payload)
    else:
        from rich.console import Console

        console = Console()
        integrity_colour = "green" if integrity == "ok" else "red"
        consistent_colour = "green" if cardinality["consistent"] else "red"
        console.print(
            f"local mode · integrity=[{integrity_colour}]{integrity}[/{integrity_colour}]  "
            f"fts=[{consistent_colour}]{cardinality['consistent']}[/{consistent_colour}] "
            f"(notes={cardinality['notes_count']}, "
            f"fts={cardinality['fts_count']})"
        )
        if not cardinality["consistent"]:
            console.print(
                "[yellow]FTS5 drift detected — run `knoten reindex` to rebuild "
                "the derived tables from on-disk files.[/yellow]"
            )


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
    except typer.Exit:
        raise
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
    in_columns: list[str] = typer.Option(
        [],
        "--in",
        help="Restrict the match to one or more FTS5 columns (title, body, filename). "
        "Repeatable or comma-separated. Ranked search only.",
    ),
    fields: Fields = typer.Option(
        Fields.full,
        "--fields",
        help="Hit shape: `full` (default) or `minimal` (id, filename, title, "
        "family, kind, score, snippet only). Trims JSON output.",
        case_sensitive=False,
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
        if in_columns and fuzzy:
            raise UserError("--in only applies to ranked unicode61 search; drop --fuzzy to use it")
        columns = _parse_search_columns(in_columns)
        settings = _load()
        with Store(settings.paths.index_path) as store:
            _local_stat_walk(settings, store)
            fuzzy_total: int | None = None
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
                    columns=columns,
                )
                source = "local"
            hint = _family_kind_hint(store, kind=kind, total=total)
            # Zero ranked hits: probe fuzzy under the same filters so we can tell
            # the user --fuzzy would have found something instead of leaving them
            # to conclude the vault is empty. Only on the zero-hit path, so the
            # common case pays nothing.
            if not fuzzy and total == 0:
                fuzzy_total = _zero_hit_fuzzy_probe(
                    store,
                    query,
                    settings=settings,
                    family=family,
                    kind=kind,
                    tag=tag,
                    min_permission=min_permission,
                    max_permission=max_permission,
                )
                if fuzzy_total:
                    fuzzy_hint = f"0 ranked hits; --fuzzy would find {fuzzy_total}"
                    hint = f"{hint} {fuzzy_hint}" if hint else fuzzy_hint
        payload: dict[str, Any] = {
            "query": query,
            "total": total,
            "limit": limit,
            "offset": offset,
            "hits": [hit_to_dict(h) for h in hits],
            "source": source,
        }
        if columns:
            payload["scope"] = columns
        if fuzzy_total:
            payload["fuzzy_total"] = fuzzy_total
        if hint:
            payload["hint"] = hint
        # Minimal projection trims the JSON payload only; the TTY table already
        # renders a curated subset, so it keeps the full hit dicts.
        if fields is Fields.minimal and mode.json:
            payload["hits"] = _project_hits(payload["hits"], _SEARCH_MINIMAL_KEYS)
        render_search_hits(payload, mode=mode)
        if hint and not mode.json:
            sys.stderr.write(f"hint: {hint}\n")
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


_SEARCH_COLUMNS = ("title", "body", "filename")


def _parse_search_columns(raw: list[str]) -> list[str]:
    """Flatten + validate `--in` values into an ordered, de-duplicated column list.

    Accepts repeated flags and comma-separated values (`--in title,body`).
    Each value must name an FTS5 column (title / body / filename); anything
    else raises a UserError so the typo surfaces at the CLI boundary.
    """
    columns: list[str] = []
    for value in raw:
        for part in value.split(","):
            column = part.strip().lower()
            if not column:
                continue
            if column not in _SEARCH_COLUMNS:
                raise UserError(
                    f"--in: '{column}' is not a searchable column "
                    f"(choose from: {', '.join(_SEARCH_COLUMNS)})"
                )
            if column not in columns:
                columns.append(column)
    return columns


def _zero_hit_fuzzy_probe(
    store: Store,
    query: str,
    *,
    settings: Settings,
    family: str | None,
    kind: str | None,
    tag: str | None,
    min_permission: str | None,
    max_permission: str | None,
) -> int:
    """Count fuzzy matches for a query that returned zero ranked hits.

    Reuses `Store.search_fuzzy` with a tiny limit (only the total matters)
    under the same filters. Returns 0 when fuzzy finds nothing too.
    """
    _hits, total = store.search_fuzzy(
        query,
        family=family,
        kind=kind,
        tag=tag,
        min_permission=min_permission,
        max_permission=max_permission,
        limit=1,
        offset=0,
        vault_dir=settings.paths.vault_dir,
    )
    return total


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


@app.command("similar")
def cmd_similar(
    target: str = typer.Argument(..., help="Note UUID or filename (or prefix)"),
    family: str | None = typer.Option(None, "--family"),
    kind: str | None = typer.Option(None, "--kind"),
    tag: str | None = typer.Option(None, "--tag"),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Find notes related to a target — ranked FTS5 search, no embeddings.

    Resolves the target like `read`, derives a query from its title + most
    frequent body terms, runs it through ranked search (matching any term),
    drops the note itself, and returns the top hits. The JSON shape mirrors
    `search` hits, wrapped with `target_id`, `target_filename`, and the
    `derived_query`. No network, no lock.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        with Store(settings.paths.index_path) as store:
            _local_stat_walk(settings, store)
            payload = find_similar(
                store,
                settings.paths.vault_dir,
                target,
                limit=limit,
                family=family,
                kind=kind,
                tag=tag,
            )
        if mode.json:
            render_search_hits(payload, mode=mode)
        else:
            # The TTY/plain renderer keys off `query` for its heading; the JSON
            # payload keeps the documented `derived_query` shape untouched.
            render_search_hits({**payload, "query": payload["derived_query"]}, mode=mode)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("read")
def cmd_read(
    targets: list[str] = typer.Argument(
        ..., help="One or more note UUIDs or filenames (or prefixes)"
    ),
    no_backlinks: bool = typer.Option(False, "--no-backlinks"),
    fields: ReadFields = typer.Option(
        ReadFields.full,
        "--fields",
        help="Body-inclusion level: `full` (default, body included) or `meta` "
        "(omit body; keep frontmatter, wikilinks, backlinks).",
        case_sensitive=False,
    ),
    max_body_chars: int | None = typer.Option(
        None,
        "--max-body-chars",
        min=1,
        help="Truncate the body to N characters; adds body_truncated / "
        "body_total_chars when a cut happens. Excludes --fields meta.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read one or more notes from the local mirror — body + wikilinks + backlinks.

    A single target keeps the exact pre-existing payload shape. With two or
    more targets the payload becomes `{targets, notes, failed}` — each target
    resolves independently, one bad target never aborts the rest, and the
    command exits 0 if at least one note resolved (exit 1 with the usual
    error envelope only when every target failed).
    """
    mode = OutputMode.detect(json_output)
    try:
        if fields is ReadFields.meta and max_body_chars is not None:
            raise UserError(
                "--max-body-chars cannot be combined with --fields meta (meta omits the body)"
            )
        settings = _load()
        with Store(settings.paths.index_path) as store:
            _local_stat_walk(settings, store)
            if len(targets) == 1:
                payload = read_note_full(
                    store,
                    settings.paths.vault_dir,
                    targets[0],
                    include_backlinks=not no_backlinks,
                )
                payload = _apply_read_fields(payload, fields=fields, max_body_chars=max_body_chars)
                render_note(payload, mode=mode, minimal=fields is ReadFields.meta)
                return
            notes: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            first_error: Exception | None = None
            for one in targets:
                try:
                    note_payload = read_note_full(
                        store,
                        settings.paths.vault_dir,
                        one,
                        include_backlinks=not no_backlinks,
                    )
                    notes.append(
                        _apply_read_fields(
                            note_payload, fields=fields, max_body_chars=max_body_chars
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — per-target isolation
                    if first_error is None:
                        first_error = exc
                    _code, kind = _classify_error(exc)
                    failed.append({"target": one, "error": kind, "message": str(exc)})
        if not notes:
            # Every target failed — surface the usual error envelope using the
            # first failure's kind, exactly as a single failed read would.
            assert first_error is not None
            _fail(first_error, mode=mode)
            return
        render_notes(
            {"targets": len(targets), "notes": notes, "failed": failed},
            mode=mode,
            minimal=fields is ReadFields.meta,
        )
    except typer.Exit:
        raise
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
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


def _parse_iso_filter(value: str | None, *, flag: str) -> str | None:
    """Validate an ISO date/datetime CLI bound, returning it normalised for SQL.

    Accepts a bare `YYYY-MM-DD` date or a full ISO-8601 timestamp (a trailing
    `Z` is honoured). Returns the trimmed string unchanged — the stored
    timestamps sort lexicographically, so the validated string is a usable
    `>=` bound directly. An unparseable value raises a UserError (exit 1).
    """
    if value is None:
        return None
    candidate = value.strip()
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UserError(
            f"{flag}: '{value}' is not an ISO date or datetime "
            "(use YYYY-MM-DD or a full ISO-8601 timestamp)"
        ) from exc
    return candidate


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
    updated_after: str | None = typer.Option(
        None,
        "--updated-after",
        help="Only notes updated on or after this ISO date/datetime "
        "(YYYY-MM-DD or a full ISO-8601 timestamp).",
    ),
    created_after: str | None = typer.Option(
        None,
        "--created-after",
        help="Only notes created on or after this ISO date/datetime "
        "(YYYY-MM-DD or a full ISO-8601 timestamp).",
    ),
    sort: str = typer.Option("updated", "--sort"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    fields: Fields = typer.Option(
        Fields.full,
        "--fields",
        help="Entry shape: `full` (default) or `minimal` (id, filename, family, "
        "kind, updated_at only). Trims JSON output.",
        case_sensitive=False,
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List notes from the local index."""
    mode = OutputMode.detect(json_output)
    try:
        updated_after_value = _parse_iso_filter(updated_after, flag="--updated-after")
        created_after_value = _parse_iso_filter(created_after, flag="--created-after")
        settings = _load()
        with Store(settings.paths.index_path) as store:
            _local_stat_walk(settings, store)
            summaries, total = store.list_notes(
                family=family,
                kind=kind,
                tag=tag,
                source=source,
                min_permission=min_permission,
                max_permission=max_permission,
                updated_after=updated_after_value,
                created_after=created_after_value,
                sort=sort,
                limit=limit,
                offset=offset,
            )
            vault_dir = settings.paths.vault_dir
            notes = list_summaries_to_dicts(summaries, vault_dir=vault_dir, store=store)
            hint = _family_kind_hint(store, kind=kind, total=total)
        payload: dict[str, Any] = {
            "total": total,
            "limit": limit,
            "offset": offset,
            "notes": notes,
        }
        if updated_after_value is not None:
            payload["updated_after"] = updated_after_value
        if created_after_value is not None:
            payload["created_after"] = created_after_value
        if hint:
            payload["hint"] = hint
        # Minimal projection trims the JSON payload only; the TTY table keeps
        # its full rows (it already shows a curated subset of columns).
        if fields is Fields.minimal and mode.json:
            payload["notes"] = _project_hits(payload["notes"], _LIST_MINIMAL_KEYS)
        render_summary_list(payload, mode=mode)
        if hint and not mode.json:
            sys.stderr.write(f"hint: {hint}\n")
    except typer.Exit:
        raise
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
            _local_stat_walk(settings, store)
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
    except typer.Exit:
        raise
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
            _local_stat_walk(settings, store)
            rows = store.tag_counts()
        render_counts({"tags": rows}, "tags", mode=mode)
    except typer.Exit:
        raise
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
        # Validate here — Store.graph_neighbourhood raises a bare ValueError,
        # which the classifier would surface as error "unknown".
        if direction not in ("out", "in", "both"):
            raise UserError(f"--direction must be one of: out, in, both (got {direction!r})")
        settings = _load()
        with Store(settings.paths.index_path) as store:
            _local_stat_walk(settings, store)
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
    except typer.Exit:
        raise
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
            _local_stat_walk(settings, store)
            rows = store.kind_counts(family=family)
        render_counts({"kinds": rows}, "kinds", mode=mode)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("unresolved")
def cmd_unresolved(
    target: str | None = typer.Option(
        None,
        "--target",
        help="Restrict to dangling wiki-links referenced FROM this note "
        "(UUID, filename, or prefix — resolved like `read`).",
    ),
    limit: int = typer.Option(0, "--limit", min=0, help="Max distinct targets to show (0 = all)"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List dangling wiki-link targets — links pointing at notes that don't
    exist yet — grouped by target, with the notes that reference each. No
    network. Use after a write to find the stubs you still need to create.

    Pass `--target <note>` to scope the view to one note's outgoing dangling
    links; the resolved note is echoed back in the payload's `target` field.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        target_echo: dict[str, Any] | None = None
        with Store(settings.paths.index_path) as store:
            _local_stat_walk(settings, store)
            source_id: str | None = None
            if target is not None:
                source_row = resolve_target(store, target)
                source_id = source_row["id"]
                target_echo = {"id": source_row["id"], "filename": source_row["filename"]}
            rows = store.unresolved_wikilinks(source_id=source_id)
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
        payload: dict[str, Any] = {"total": len(grouped), "targets": targets}
        if target_echo is not None:
            payload["target"] = target_echo
        render_unresolved(payload, mode=mode)
    except typer.Exit:
        raise
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
            _local_stat_walk(settings, store)
            citekeys = store.distinct_citekeys(prefix=prefix)
        if mode.json:
            emit_json({"citekeys": citekeys, "count": len(citekeys), "prefix": prefix})
        else:
            for citekey in citekeys:
                sys.stdout.write(f"{citekey}\n")
    except typer.Exit:
        raise
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
        body_text = _resolve_body(body, body_file)
        if ai:
            if body_text is None:
                raise UserError("--ai requires --body or --body-file")
            body_text = _wrap_ai(body_text)
        frontmatter = _load_frontmatter_file(frontmatter_file)
        # Dry-run is local-only — no token needed (matches cmd_reference).
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
        _require_token(settings, for_write="create")
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
    except typer.Exit:
        raise
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

    _require_token(settings, for_write="create")
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


def _patch_from_edit_batch_item(index: int, item: Any) -> dict[str, Any]:
    """Validate one edit --batch item and return edit_note_remote()/preview kwargs.

    Mirrors `_draft_from_batch_item` (create): a single bad item raises a
    `UserError` that the batch loop records without aborting the rest. The
    item's `set_frontmatter` carries typed JSON values, so it is routed
    through the `set_frontmatter_json` path to preserve int/list/bool/null.
    """
    if not isinstance(item, dict):
        raise UserError(f"--batch item {index} is not a JSON object")
    target = item.get("target")
    if not isinstance(target, str) or not target:
        raise UserError(f"--batch item {index} is missing a 'target' string")
    for key in ("filename", "title", "body"):
        value = item.get(key)
        if value is not None and not isinstance(value, str):
            raise UserError(f"--batch item {index} '{key}' must be a string")
    body = item.get("body")
    if item.get("ai"):
        if body is None:
            raise UserError(f"--batch item {index} sets 'ai' but has no 'body'")
        body = _wrap_ai(body)
    add_tags = item.get("add_tags") or []
    if not isinstance(add_tags, list) or not all(isinstance(t, str) for t in add_tags):
        raise UserError(f"--batch item {index} 'add_tags' must be an array of strings")
    remove_tags = item.get("remove_tags") or []
    if not isinstance(remove_tags, list) or not all(isinstance(t, str) for t in remove_tags):
        raise UserError(f"--batch item {index} 'remove_tags' must be an array of strings")
    set_frontmatter = item.get("set_frontmatter") or {}
    if not isinstance(set_frontmatter, dict):
        raise UserError(f"--batch item {index} 'set_frontmatter' must be an object")
    unset_frontmatter = item.get("unset_frontmatter") or []
    if not isinstance(unset_frontmatter, list) or not all(
        isinstance(k, str) for k in unset_frontmatter
    ):
        raise UserError(f"--batch item {index} 'unset_frontmatter' must be an array of strings")
    return {
        "target": target,
        "new_filename": item.get("filename"),
        "new_title": item.get("title"),
        "new_body": body,
        "set_frontmatter_json": dict(set_frontmatter),
        "unset_frontmatter": list(unset_frontmatter),
        "add_tags": list(add_tags),
        "remove_tags": list(remove_tags),
    }


def _run_edit_batch(
    settings: Settings, batch_path: Path, *, mode: OutputMode, dry_run: bool, force: bool
) -> None:
    """Edit many notes under one lock pass; never aborts on a single bad patch.

    Always emits a JSON summary on stdout (batch is a machine-oriented path):
    `{operation, count, edited, failed, results: [{index, ok, id|error}]}`.
    Works in both modes — remote does N API calls in one process, local does
    N filesystem writes; either way the win is one lock pass + one invocation.
    """
    items = _read_batch_items(batch_path)
    results: list[dict[str, Any]] = []

    if dry_run:
        with Store(settings.paths.index_path) as store:
            for index, item in enumerate(items):
                try:
                    kwargs = _patch_from_edit_batch_item(index, item)
                    preview = preview_edit(
                        store,
                        settings.paths.vault_dir,
                        target=kwargs["target"],
                        new_filename=kwargs["new_filename"],
                        new_title=kwargs["new_title"],
                        new_body=kwargs["new_body"],
                        set_frontmatter={},
                        set_frontmatter_json=kwargs["set_frontmatter_json"],
                        unset_frontmatter=kwargs["unset_frontmatter"],
                        add_tags=kwargs["add_tags"],
                        remove_tags=kwargs["remove_tags"],
                        force=force,
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
                "operation": "edit-batch",
                "dry_run": True,
                "count": len(results),
                "results": results,
            }
        )
        return

    _require_token(settings, for_write="edit")
    with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
        with _build_backend(settings) as backend:
            for index, item in enumerate(items):
                try:
                    kwargs = _patch_from_edit_batch_item(index, item)
                    result = edit_note_remote(
                        backend=backend,
                        store=store,
                        vault_dir=settings.paths.vault_dir,
                        target=kwargs["target"],
                        new_filename=kwargs["new_filename"],
                        new_title=kwargs["new_title"],
                        new_body=kwargs["new_body"],
                        set_frontmatter={},
                        set_frontmatter_json=kwargs["set_frontmatter_json"],
                        unset_frontmatter=kwargs["unset_frontmatter"],
                        add_tags=kwargs["add_tags"],
                        remove_tags=kwargs["remove_tags"],
                        force=force,
                    )
                    results.append(
                        {
                            "index": index,
                            "ok": True,
                            "id": result.note.id,
                            "filename": result.note.filename,
                            "restricted_affected": len(result.restricted_affected),
                            "warnings": list(result.warnings),
                        }
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
    edited = sum(1 for r in results if r.get("ok"))
    emit_json(
        {
            "operation": "edit-batch",
            "count": len(results),
            "edited": edited,
            "failed": len(results) - edited,
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
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("edit")
def cmd_edit(
    target: str | None = typer.Argument(None),
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
    batch: Path | None = typer.Option(
        None,
        "--batch",
        help="Edit many notes from a JSON array of patches (use '-' for stdin), one lock "
        "pass. Each item: {target, filename?, title?, body?, add_tags?, remove_tags?, "
        "set_frontmatter?, unset_frontmatter?, ai?}.",
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
        if batch is not None:
            conflicting = (
                target is not None
                or filename is not None
                or title is not None
                or body is not None
                or body_file is not None
                or bool(set_frontmatter)
                or bool(set_frontmatter_json)
                or bool(unset_frontmatter)
                or bool(add_tag)
                or bool(remove_tag)
                or ai
            )
            if conflicting:
                raise UserError(
                    "--batch is mutually exclusive with a positional target and per-note edit flags"
                )
            _run_edit_batch(settings, batch, mode=mode, dry_run=dry_run, force=force)
            return
        if target is None:
            raise UserError("pass a target (or --batch <file> for bulk edit)")
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
        _require_token(settings, for_write="edit")
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            with _build_backend(settings) as backend:
                result = edit_note_remote(
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
            payload = _write_response(store, settings.paths.vault_dir, result.note.id, fields)
            # Additive field: how many rename-cascade targets the token could
            # not READ and were mirrored as placeholders. 0 on the common path.
            payload["restricted_affected"] = len(result.restricted_affected)
            # Additive field: cascade targets whose re-mirror was skipped
            # (frontmatter failed the writer's validation). Empty list on
            # the common path.
            payload["warnings"] = list(result.warnings)
        render_note(payload, mode=mode, minimal=fields is Fields.minimal)
    except typer.Exit:
        raise
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
    except typer.Exit:
        raise
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
                # Plain return — `raise typer.Exit(0)` would be caught by the
                # generic handler below and re-classified as an error (exit 1).
                log("aborted", mode=mode)
                return
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
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("restore")
def cmd_restore(
    note_id: str = typer.Argument(...),
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
    """Restore a note from trash.

    Restore requires WRITE on the note; a local pre-check fast-fails when the
    mirror knows the note's level, and `--force` bypasses it (the server is
    the final authority). The restore handle is the `deleted_id` returned by
    `knoten delete`.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        _require_token(settings, for_write="restore")
        with acquire_lock(settings.paths.lock_file), Store(settings.paths.index_path) as store:
            vault_dir = settings.paths.vault_dir
            with _build_backend(settings) as backend:
                note = restore_note_remote(
                    backend=backend,
                    store=store,
                    vault_dir=vault_dir,
                    note_id=note_id,
                    force=force,
                )
            payload = _write_response(store, vault_dir, note.id, fields)
        render_note(payload, mode=mode, minimal=fields is Fields.minimal)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("trash")
def cmd_trash(
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    fields: Fields = typer.Option(
        Fields.full,
        "--fields",
        help="Row shape: `full` (default) or `minimal` (id, filename, deleted_at only).",
        case_sensitive=False,
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List soft-deleted notes (the trash) — read-only.

    Remote mode lists the server's trash (GET /api/trash/notes); local mode
    lists the local `trashed_notes` table. Each row carries `deleted_at`, the
    soft-delete timestamp; the note id is the restore handle for
    `knoten restore <id>`.
    """
    mode = OutputMode.detect(json_output)
    try:
        settings = _load()
        if settings.effective_mode != "local":
            _require_token(settings)
        with _build_backend(settings) as backend:
            payload = list_trash(
                backend, limit=limit, minimal=fields is Fields.minimal and mode.json
            )
        render_trash(payload, mode=mode)
    except typer.Exit:
        raise
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
        batch=None,
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
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


@app.command("download")
def cmd_download(
    target: str = typer.Argument(..., help="File-family note UUID or filename (or prefix)"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Destination path. Defaults to the note filename's basename in the "
        "current directory (the default never escapes the cwd).",
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
    except typer.Exit:
        raise
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
    except typer.Exit:
        raise
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
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc, mode=mode)


if __name__ == "__main__":
    app()
