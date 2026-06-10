"""`knoten inbox` — quick-capture sub-app.

Three verbs lower the friction of getting a thought, photo, or URL into the
vault as a fleeting `#inbox` note:

- `knoten inbox add <argument>` — new fleeting `#inbox` note (file, URL, or text).
- `knoten inbox append <fleeting> <argument>` — accumulate into an existing fleeting.
- `knoten inbox list` — show pending `#inbox` fleetings (excludes `#inbox-promoted`).

The argument heuristic is: existing readable file → upload as attachment;
matches `^https?://` → URL (best-effort `<title>` fetch); otherwise → plain
text. `--as-file` / `--as-url` / `--as-text` force a mode.

Filename grammar is part of the cross-repo contract documented in
`docs/inbox-capture.md` (and the conception design.md). Keep
`compose_inbox_fleeting_filename` and `compose_inbox_file_filename` as the
single source of truth on the Python side.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from knoten.cli.output import OutputMode, emit_json, render_note, render_summary_list
from knoten.repositories.errors import UserError

inbox_app = typer.Typer(
    help="Quick-capture flow into a fleeting #inbox note (file/URL/text).",
    no_args_is_help=True,
)

INBOX_TAG = "inbox"
PROMOTED_TAG = "inbox-promoted"

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SLUG_KEEP_RE = re.compile(r"[^a-z0-9]+")
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAX_SLUG_LEN = 40


def _slugify(text: str, *, fallback: str = "note") -> str:
    """Lower-cased ASCII-ish slug, max 40 chars, fallback if empty."""
    if not text:
        return fallback
    lowered = text.strip().lower()
    cleaned = _SLUG_KEEP_RE.sub("-", lowered).strip("-")
    if not cleaned:
        return fallback
    if len(cleaned) > _MAX_SLUG_LEN:
        cleaned = cleaned[:_MAX_SLUG_LEN].rstrip("-") or fallback
    return cleaned


def _now_parts() -> tuple[str, str]:
    """`(YYYY-MM-DD, HHMM)` in local time — naming-convention components."""
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%H%M")


def compose_inbox_fleeting_filename(slug: str, *, now: datetime | None = None) -> str:
    """`- YYYY-MM-DD HHMM inbox <slug>` — the fleeting note filename."""
    when = now or datetime.now()
    return f"- {when.strftime('%Y-%m-%d %H%M')} inbox {slug}"


def compose_inbox_file_filename(slug: str, ext: str, *, now: datetime | None = None) -> str:
    """`YYYY-MM-DD+ inbox <slug> HHMM<ext>` — the file-family attachment filename.

    The `HHMM` lives at the end (right before the extension) so chronological
    sort by date prefix groups all of one day's inbox files together. `ext`
    is the original-file extension *with* leading dot (e.g. `.jpg`); pass
    empty string when there is none.
    """
    when = now or datetime.now()
    return f"{when.strftime('%Y-%m-%d')}+ inbox {slug} {when.strftime('%H%M')}{ext}"


def _classify_argument(
    argument: str,
    *,
    force: str | None,
) -> str:
    """Decide whether an argument is a file path, a URL, or plain text.

    Returns one of `"file"`, `"url"`, `"text"`. Forced modes win; otherwise
    a readable existing file beats URL detection beats text. A path that
    looks file-like but does not exist falls through to text — the caller
    can disambiguate with `--as-file` for clarity.
    """
    if force in {"file", "url", "text"}:
        return force
    candidate = Path(argument).expanduser()
    if candidate.is_file():
        return "file"
    if _URL_RE.match(argument):
        return "url"
    return "text"


def _fetch_url_title(url: str, *, timeout: float = 5.0) -> str | None:
    """Best-effort fetch of a page's `<title>` for slug derivation.

    Returns `None` on any failure — network timeout, non-2xx, missing title.
    Does not raise; URL captures must work even when the page is offline.
    Limits the response size so a hostile / huge page does not stall the CLI.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover — httpx is a hard dep but be defensive
        return None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "knoten/inbox"})
        if response.status_code >= 400:
            return None
        head = response.text[:32_768]
    except Exception:
        return None
    match = _TITLE_TAG_RE.search(head)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or None


def _file_extension(path: Path) -> str:
    """`.jpg`, `.pdf`, etc. — empty string for extensionless files."""
    suffix = path.suffix
    return suffix if suffix else ""


def _emit_error(exc: Exception, *, mode: OutputMode) -> "typer.Exit":  # noqa: UP037
    """Re-use main's error envelope so JSON shape stays consistent."""
    from knoten.cli.main import _classify_error, _error_extras

    code, kind = _classify_error(exc)
    if mode.json:
        payload: dict[str, Any] = {
            "error": kind,
            "message": str(exc),
            "code": code,
            **_error_extras(exc),
        }
        emit_json(payload)
    else:
        import sys

        sys.stderr.write(f"error: {exc}\n")
    return typer.Exit(code)


def _load_runtime():
    """Lazy-import the main-module wiring helpers (avoids a circular import).

    Returns `(load_settings, build_backend, require_token)` — each command
    calls these to set up its execution context, exactly as `cli/main.py`
    does. Centralising in one helper keeps the per-command bodies tight.
    """
    from knoten.cli.main import _build_backend, _load, _require_token

    return _load, _build_backend, _require_token


# ---- add ---------------------------------------------------------------


@inbox_app.command("add")
def cmd_add(
    argument: str = typer.Argument(
        ...,
        help="A local file path, an http(s) URL, or plain text to capture.",
    ),
    note: str | None = typer.Option(
        None,
        "--note",
        help="Optional context line appended to the new fleeting's body.",
    ),
    tag: list[str] = typer.Option(
        [],
        "--tag",
        help="Extra tag(s) to add to the fleeting on top of `inbox` (repeatable).",
    ),
    as_file: bool = typer.Option(False, "--as-file", help="Force file-upload mode."),
    as_url: bool = typer.Option(False, "--as-url", help="Force URL mode."),
    as_text: bool = typer.Option(False, "--as-text", help="Force plain-text mode."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture an argument as a new fleeting `#inbox` note.

    File arguments are uploaded as a `YYYY-MM-DD+ inbox <slug> HHMM<ext>`
    file-family note and the new fleeting wikilinks to it. URL arguments are
    fetched only enough to derive a slug from `<title>`; the URL itself goes
    into the body. Bare text becomes the fleeting body verbatim.

    The fleeting always carries the `inbox` tag (composed into the body
    automatically), plus any extra `--tag` values.
    """
    mode = OutputMode.detect(json_output)
    try:
        force = _resolve_force(as_file, as_url, as_text)
        kind = _classify_argument(argument, force=force)
        load_settings, build_backend, require_token = _load_runtime()
        settings = load_settings()
        require_token(settings, for_write="inbox add")
        from knoten.repositories.lock import acquire_lock
        from knoten.repositories.store import Store
        from knoten.services.notes import create_note_remote, upload_file_remote

        # Network fetch happens before the vault lock — a slow page must not
        # block every concurrent mutation for up to the fetch timeout.
        url_title = _fetch_url_title(argument) if kind == "url" else None
        all_tags = [INBOX_TAG, *tag]
        with (
            acquire_lock(settings.paths.lock_file),
            Store(settings.paths.index_path) as store,
            build_backend(settings) as backend,
        ):
            if kind == "file":
                payload = _add_file(
                    argument,
                    note=note,
                    tags=all_tags,
                    backend=backend,
                    store=store,
                    settings=settings,
                    upload_file_remote=upload_file_remote,
                    create_note_remote=create_note_remote,
                )
            elif kind == "url":
                payload = _add_url(
                    argument,
                    title=url_title,
                    note=note,
                    tags=all_tags,
                    backend=backend,
                    store=store,
                    settings=settings,
                    create_note_remote=create_note_remote,
                )
            else:
                payload = _add_text(
                    argument,
                    note=note,
                    tags=all_tags,
                    backend=backend,
                    store=store,
                    settings=settings,
                    create_note_remote=create_note_remote,
                )
        if mode.json:
            emit_json(payload)
        else:
            render_note(payload["fleeting"], mode=mode, minimal=True)
    except typer.Exit:
        raise
    except Exception as exc:
        raise _emit_error(exc, mode=mode) from exc


def _resolve_force(as_file: bool, as_url: bool, as_text: bool) -> str | None:
    flags = (("file", as_file), ("url", as_url), ("text", as_text))
    chosen = [name for name, flag in flags if flag]
    if len(chosen) > 1:
        raise UserError("--as-file, --as-url, --as-text are mutually exclusive")
    return chosen[0] if chosen else None


def _add_file(
    argument: str,
    *,
    note: str | None,
    tags: list[str],
    backend: Any,
    store: Any,
    settings: Any,
    upload_file_remote: Any,
    create_note_remote: Any,
) -> dict[str, Any]:
    """Upload the file as a file-family note, then create a fleeting linking to it."""
    path = Path(argument).expanduser()
    if not path.is_file():
        raise UserError(f"--as-file requires a readable file (not found: {path})")
    slug = _slugify(path.stem, fallback="photo")
    now = datetime.now()
    file_filename = compose_inbox_file_filename(slug, _file_extension(path), now=now)
    fleeting_filename = compose_inbox_fleeting_filename(slug, now=now)

    file_note, upload_meta = upload_file_remote(
        backend=backend,
        store=store,
        vault_dir=settings.paths.vault_dir,
        source_path=path,
        filename=file_filename,
        tags=[],
        source=None,
        content_type=None,
    )
    body = _fleeting_body(
        primary=f"[[{file_filename}]]",
        note=note,
    )
    fleeting = create_note_remote(
        backend=backend,
        store=store,
        vault_dir=settings.paths.vault_dir,
        filename=fleeting_filename,
        body=body,
        kind=None,
        tags=tags,
    )
    return {
        "kind": "file",
        "fleeting": _summary(fleeting),
        "attachment": _summary(file_note, extra={"upload": _upload_meta(upload_meta)}),
    }


def _add_url(
    argument: str,
    *,
    title: str | None,
    note: str | None,
    tags: list[str],
    backend: Any,
    store: Any,
    settings: Any,
    create_note_remote: Any,
) -> dict[str, Any]:
    slug = _slugify(title or "", fallback="link")
    fleeting_filename = compose_inbox_fleeting_filename(slug)
    primary = f"[{title}]({argument})" if title else argument
    body = _fleeting_body(primary=primary, note=note)
    fleeting = create_note_remote(
        backend=backend,
        store=store,
        vault_dir=settings.paths.vault_dir,
        filename=fleeting_filename,
        body=body,
        kind=None,
        tags=tags,
    )
    return {
        "kind": "url",
        "url": argument,
        "title": title,
        "fleeting": _summary(fleeting),
    }


def _add_text(
    argument: str,
    *,
    note: str | None,
    tags: list[str],
    backend: Any,
    store: Any,
    settings: Any,
    create_note_remote: Any,
) -> dict[str, Any]:
    text = argument.strip()
    if not text:
        raise UserError("Cannot capture an empty text argument")
    slug = _slugify(text, fallback="note")
    fleeting_filename = compose_inbox_fleeting_filename(slug)
    body = _fleeting_body(primary=text, note=note)
    fleeting = create_note_remote(
        backend=backend,
        store=store,
        vault_dir=settings.paths.vault_dir,
        filename=fleeting_filename,
        body=body,
        kind=None,
        tags=tags,
    )
    return {
        "kind": "text",
        "fleeting": _summary(fleeting),
    }


def _fleeting_body(*, primary: str, note: str | None) -> str:
    """Body for a freshly-created inbox fleeting.

    `primary` is the captured payload — the wikilink to the file note, the
    URL, or the typed text. `note` is the optional context blurb. The
    `#inbox` tag is added by the service layer's `_compose_body`, so this
    body deliberately does not embed the hashtag itself.
    """
    parts = [primary]
    if note:
        parts.append(note.strip())
    return "\n\n".join(p for p in parts if p)


# ---- append ------------------------------------------------------------


@inbox_app.command("append")
def cmd_append(
    fleeting: str = typer.Argument(
        ...,
        help="Existing fleeting note (UUID, exact filename, or unambiguous prefix).",
    ),
    argument: str = typer.Argument(
        ...,
        help="A local file path, an http(s) URL, or plain text to append.",
    ),
    note: str | None = typer.Option(
        None,
        "--note",
        help="Optional context line appended after the new payload.",
    ),
    as_file: bool = typer.Option(False, "--as-file"),
    as_url: bool = typer.Option(False, "--as-url"),
    as_text: bool = typer.Option(False, "--as-text"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Append a captured argument to an existing fleeting note.

    Same heuristic as `add`: file → upload as a separate `YYYY-MM-DD+ inbox …`
    file note, append a wikilink to it; URL → append `[title](url)`; text →
    append the text directly. The fleeting's tags are not modified — if it
    already had `#inbox`, it keeps it; if it had something else, that is
    preserved too. `--note` adds a context line after the primary payload.
    """
    mode = OutputMode.detect(json_output)
    try:
        force = _resolve_force(as_file, as_url, as_text)
        kind = _classify_argument(argument, force=force)
        load_settings, build_backend, require_token = _load_runtime()
        settings = load_settings()
        require_token(settings, for_write="inbox append")
        from knoten.repositories.lock import acquire_lock
        from knoten.repositories.store import Store
        from knoten.services.notes import (
            append_note_remote,
            resolve_target,
            upload_file_remote,
        )

        # Network fetch happens before the vault lock (see cmd_add).
        url_title = _fetch_url_title(argument) if kind == "url" else None
        with (
            acquire_lock(settings.paths.lock_file),
            Store(settings.paths.index_path) as store,
            build_backend(settings) as backend,
        ):
            target_row = resolve_target(store, fleeting)
            payload = _do_append(
                argument,
                kind=kind,
                url_title=url_title,
                note=note,
                target_row=target_row,
                backend=backend,
                store=store,
                settings=settings,
                append_note_remote=append_note_remote,
                upload_file_remote=upload_file_remote,
            )
        if mode.json:
            emit_json(payload)
        else:
            render_note(payload["fleeting"], mode=mode, minimal=True)
    except typer.Exit:
        raise
    except Exception as exc:
        raise _emit_error(exc, mode=mode) from exc


def _do_append(
    argument: str,
    *,
    kind: str,
    url_title: str | None,
    note: str | None,
    target_row: dict[str, Any],
    backend: Any,
    store: Any,
    settings: Any,
    append_note_remote: Any,
    upload_file_remote: Any,
) -> dict[str, Any]:
    fleeting_id = target_row["id"]
    if kind == "file":
        path = Path(argument).expanduser()
        if not path.is_file():
            raise UserError(f"--as-file requires a readable file (not found: {path})")
        slug = _slugify(path.stem, fallback="photo")
        file_filename = compose_inbox_file_filename(slug, _file_extension(path))
        file_note, upload_meta = upload_file_remote(
            backend=backend,
            store=store,
            vault_dir=settings.paths.vault_dir,
            source_path=path,
            filename=file_filename,
            tags=[],
            source=None,
            content_type=None,
        )
        primary = f"[[{file_filename}]]"
    elif kind == "url":
        primary = f"[{url_title}]({argument})" if url_title else argument
        file_note = None
        upload_meta = None
    else:
        text = argument.strip()
        if not text:
            raise UserError("Cannot append an empty text argument")
        primary = text
        file_note = None
        upload_meta = None

    content = _fleeting_body(primary=primary, note=note)
    fleeting = append_note_remote(
        backend=backend,
        store=store,
        vault_dir=settings.paths.vault_dir,
        target=fleeting_id,
        content=content,
    )
    out: dict[str, Any] = {
        "kind": kind,
        "fleeting": _summary(fleeting),
    }
    if file_note is not None:
        out["attachment"] = _summary(file_note, extra={"upload": _upload_meta(upload_meta)})
    return out


# ---- list --------------------------------------------------------------


@inbox_app.command("list")
def cmd_list(
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List pending `#inbox` fleetings (excluding ones already `#inbox-promoted`).

    Sorted by creation time, oldest first — the inbox is FIFO. The total
    count returned in the payload is the count *after* filtering out
    promoted notes, so a caller does not need to do a second pass.
    """
    mode = OutputMode.detect(json_output)
    try:
        load_settings, _build_backend, _require_token = _load_runtime()
        settings = load_settings()
        from knoten.repositories.store import Store
        from knoten.services.notes import list_summaries_to_dicts

        with Store(settings.paths.index_path) as store:
            # Promoted notes are excluded in SQL so they never consume page
            # slots and `total` is the global pending count, not page-local.
            summaries, total = store.list_notes(
                tag=INBOX_TAG,
                exclude_tag=PROMOTED_TAG,
                sort="created",
                limit=limit,
                offset=offset,
            )
            notes = list_summaries_to_dicts(
                summaries,
                vault_dir=settings.paths.vault_dir,
                store=store,
            )
        payload = {
            "total": total,
            "limit": limit,
            "offset": offset,
            "notes": notes,
        }
        render_summary_list(payload, mode=mode)
    except typer.Exit:
        raise
    except Exception as exc:
        raise _emit_error(exc, mode=mode) from exc


# ---- output helpers ----------------------------------------------------


def _summary(note: Any, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compact dict for the JSON payload — covers both write returns and Note objects."""
    base = {
        "id": getattr(note, "id", None),
        "filename": getattr(note, "filename", None),
        "title": getattr(note, "title", None),
        "family": getattr(note, "family", None),
        "kind": getattr(note, "kind", None),
        "tags": list(getattr(note, "tags", []) or []),
    }
    if extra:
        base.update(extra)
    return base


def _upload_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not meta:
        return None
    return {
        "storage_key": meta.get("storageKey"),
        "content_type": meta.get("contentType"),
        "size_bytes": meta.get("sizeBytes"),
        "url": meta.get("url"),
    }
