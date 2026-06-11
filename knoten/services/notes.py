"""Note read/write orchestration.

Reads are resolved against the local store. Writes go to the remote first,
then the updated note is re-fetched and upserted into the local store so the
mirror is always at least as fresh as the last successful write.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knoten.models import Note, NoteSummary, SearchHit, permission_at_least
from knoten.repositories.backend import Backend, NoteDraft, NotePatch
from knoten.repositories.errors import (
    AmbiguousTargetError,
    FrontmatterValidationError,
    NoteForbiddenError,
    NotFoundError,
    UserError,
)
from knoten.repositories.errors import (
    PermissionError as LocalPermissionError,
)
from knoten.repositories.store import Store
from knoten.repositories.vault_files import (
    path_for_note,
    path_for_summary,
    remove_note_file,
    render_note_markdown,
    render_placeholder_markdown,
    strip_frontmatter,
    write_note_file,
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.lower()))


def _assert_permission(
    row: dict[str, Any],
    *,
    required_level: str,
    operation: str,
    force: bool,
) -> None:
    """Local fast-fail guard against per-note `permissions`.

    Skipped entirely when `force=True` — the caller is responsible for
    using that only with a web-scope token that bypasses enforcement.
    The server is always the final authority, so a false positive here
    just delays the eventual 403; a false negative (passing when the
    server would reject) degrades into a normal NetworkError later.
    """
    if force:
        return
    current = row.get("permissions") or "ALL"
    if permission_at_least(current, required_level):
        return
    raise LocalPermissionError(
        note_id=row["id"],
        filename=row["filename"],
        current_level=current,
        required_level=required_level,
        operation=operation,
    )


def resolve_target(store: Store, target: str) -> dict[str, Any]:
    """Resolve a target string (UUID or filename or prefix) to a note row.

    Raises NotFoundError if nothing matches, AmbiguousTargetError if a
    filename prefix matches more than one note.
    """
    if is_uuid(target):
        row = store.find_by_id(target)
        if row is None:
            raise NotFoundError(f"No local note with id {target}")
        return row

    exact = store.find_by_filename(target)
    if exact is not None:
        return exact

    matches = store.find_by_filename_prefix(target)
    if not matches:
        raise NotFoundError(f"No local note matches '{target}'")
    if len(matches) == 1:
        return matches[0]
    raise AmbiguousTargetError(
        f"'{target}' matches {len(matches)} notes; be more specific or use the UUID",
        candidates=[{"id": m["id"], "filename": m["filename"]} for m in matches[:10]],
    )


def ingest_note(
    note: Note,
    *,
    store: Store,
    vault_dir: Path,
    previous_path: str | None = None,
    synced: bool = True,
) -> str:
    """Upsert the store row + write the mirror file. Returns the relative path.

    Order is deliberate: **transaction first, filesystem second**. If the
    process dies at any point after the store commit, the worst case is a
    missing file at `new_path` (or a stale file at `previous_path`) — both
    recoverable by the next `reconcile_local` pass. Writing the file first
    would leave a window where on-disk content disagrees with the FTS5
    index until the next sync re-fetched the note.

    `synced=True` (default) marks the row as in sync with the remote. The
    sync service uses this when ingesting fetched notes. LocalBackend writes
    pass `synced=False` so the next remote sync's push pass picks them up.
    """
    relative_path = path_for_note(note)
    content = render_note_markdown(note)
    # Hash the body exactly as a re-read of the mirror file will produce it
    # (render normalises trailing newlines), so `verify --hashes` converges
    # to zero mismatches on a clean vault.
    body_sha = hashlib.sha256(strip_frontmatter(content).encode("utf-8")).hexdigest()

    # 1. Commit the store + FTS5 + derived rows in one transaction.
    store.upsert_note(note, path=relative_path, body_sha256=body_sha, synced=synced)

    # 2. Write the mirror file atomically. If this fails, the store already
    #    points at `relative_path`; reconcile will detect the missing file
    #    and re-fetch from the remote.
    destination = write_note_file(vault_dir, relative_path, content)

    # 3. Record the mirror file's (mtime, size) so the LocalBackend drift
    #    walk can tell "this is the content we ingested" from "user edited".
    #    Failure here is self-correcting — the walk sees a mismatch and
    #    re-parses the file.
    try:
        stat = destination.stat()
    except OSError:
        pass
    else:
        store.record_file_stat(
            note.id,
            path_mtime_ns=stat.st_mtime_ns,
            path_size=stat.st_size,
        )

    # 4. Remove any stale file left behind by a rename. If this fails, the
    #    stale file becomes an orphan that reconcile will clean up.
    if previous_path and previous_path != relative_path:
        remove_note_file(vault_dir, previous_path)

    return relative_path


def delete_ingested(store: Store, vault_dir: Path, note_id: str) -> None:
    """Remove a note from both the store and the mirror."""
    row = store.find_by_id(note_id)
    if row is not None:
        remove_note_file(vault_dir, row["path"])
    store.delete_note(note_id)


def ingest_placeholder(
    summary: NoteSummary,
    *,
    store: Store,
    vault_dir: Path,
    previous_path: str | None = None,
) -> str:
    """Create a metadata-only local row + mirror file for a note we can't fetch.

    Used when `GET /api/notes/{id}` returns 404 (restricted or deleted).
    The placeholder file explains that the body is not fetchable; the store
    row is flagged `restricted=1` so other commands can treat it accordingly.
    """
    from knoten.models import NoteSummary as _Summary  # local alias for clarity

    assert isinstance(summary, _Summary)
    relative_path = path_for_summary(summary)
    content = render_placeholder_markdown(summary)

    # 1. Store transaction first (same invariant as ingest_note).
    store.upsert_placeholder(summary, path=relative_path)

    # 2. Atomic file write.
    write_note_file(vault_dir, relative_path, content)

    if previous_path and previous_path != relative_path:
        remove_note_file(vault_dir, previous_path)
    return relative_path


def read_note_full(
    store: Store, vault_dir: Path, target: str, *, include_backlinks: bool = True
) -> dict[str, Any]:
    """Build the `knoten read` payload for a target."""
    row = resolve_target(store, target)
    absolute_path = (vault_dir / row["path"]).resolve()
    try:
        body = absolute_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserError(f"Mirror file missing for {row['id']}: {exc}") from exc

    body_without_frontmatter = strip_frontmatter(body)
    wikilinks = store.wikilinks_for_note(row["id"])
    backlinks = store.backlinks_for_note(row["id"]) if include_backlinks else None
    tags = store.tags_for_note(row["id"])

    frontmatter: dict[str, Any]
    try:
        import json as _json

        frontmatter = _json.loads(row["frontmatter_json"])
    except Exception:
        frontmatter = {}

    payload: dict[str, Any] = {
        "id": row["id"],
        "filename": row["filename"],
        "title": row["title"],
        "family": row["family"],
        "kind": row["kind"],
        "source": row["source"],
        "path": row["path"],
        "absolute_path": str(absolute_path),
        "restricted": bool(row.get("restricted", 0)),
        "permissions": row.get("permissions") or "ALL",
        "tags": list(tags),
        "frontmatter": frontmatter,
        "body": body_without_frontmatter,
        "wikilinks": [
            {
                "title": link["target_title"],
                "id": link["target_id"],
                "broken": link["target_id"] is None,
            }
            for link in wikilinks
        ],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if backlinks is not None:
        payload["backlinks"] = [
            {
                **bl,
                "absolute_path": str((vault_dir / bl["path"]).resolve()),
            }
            for bl in backlinks
        ]
    return payload


def summarize_note(store: Store, vault_dir: Path, target: str) -> dict[str, Any]:
    """Build the minimal post-write payload for a target.

    Unlike `read_note_full`, this skips the body file read, the
    wikilinks/backlinks lookups, and frontmatter parsing. Tags are
    included so a caller can verify `--add-tag` / `--remove-tag` landed
    without paying for `--fields full`. Returned dict size is independent
    of the note's body length.
    """
    row = resolve_target(store, target)
    absolute_path = (vault_dir / row["path"]).resolve()
    return {
        "id": row["id"],
        "filename": row["filename"],
        "title": row["title"],
        "family": row["family"],
        "kind": row["kind"],
        "source": row["source"],
        "path": row["path"],
        "absolute_path": str(absolute_path),
        "restricted": bool(row.get("restricted", 0)),
        "permissions": row.get("permissions") or "ALL",
        "tags": list(store.tags_for_note(row["id"])),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_summaries_to_dicts(
    summaries: list[NoteSummary], *, vault_dir: Path, store: Store
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for summary in summaries:
        row = store.find_by_id(summary.id)
        relative_path = row["path"] if row else ""
        absolute_path = str((vault_dir / relative_path).resolve()) if relative_path else ""
        out.append(
            {
                "id": summary.id,
                "filename": summary.filename,
                "title": summary.title,
                "family": summary.family,
                "kind": summary.kind,
                "source": summary.source,
                "tags": list(summary.tags),
                "path": relative_path,
                "absolute_path": absolute_path,
                "permissions": summary.permissions,
                "created_at": summary.created_at,
                "updated_at": summary.updated_at,
            }
        )
    return out


_TRASH_MINIMAL_KEYS = ("id", "filename", "deleted_at")


def trashed_summary_to_dict(summary: NoteSummary, *, minimal: bool = False) -> dict[str, Any]:
    """Project a trashed-note summary to the `knoten trash` row shape.

    `minimal` keeps only id / filename / deleted_at; otherwise the full
    metadata row (sans body) is returned. `deleted_at` is always present —
    it is the soft-delete timestamp, distinct from `updated_at`.
    """
    full = {
        "id": summary.id,
        "filename": summary.filename,
        "title": summary.title,
        "family": summary.family,
        "kind": summary.kind,
        "source": summary.source,
        "permissions": summary.permissions,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "deleted_at": summary.deleted_at,
    }
    if minimal:
        return {key: full[key] for key in _TRASH_MINIMAL_KEYS}
    return full


def list_trash(
    backend: Backend, *, limit: int | None = None, minimal: bool = False
) -> dict[str, Any]:
    """Build the `knoten trash` payload — `{data: [...], total}`.

    Read-only. Delegates to the backend's `list_trashed_notes` (remote: GET
    /api/trash/notes; local: the `trashed_notes` table), then projects each
    row to the machine output shape.
    """
    summaries = backend.list_trashed_notes(limit=limit)
    data = [trashed_summary_to_dict(summary, minimal=minimal) for summary in summaries]
    return {"data": data, "total": len(data)}


def hit_to_dict(hit: SearchHit) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": hit.id,
        "filename": hit.filename,
        "title": hit.title,
        "family": hit.family,
        "kind": hit.kind,
        "source": hit.source,
        "path": hit.path,
        "absolute_path": hit.absolute_path,
        "tags": list(hit.tags),
        "score": hit.score,
        "snippet": hit.snippet,
        "updated_at": hit.updated_at,
        "permissions": hit.permissions,
    }
    if hit.explain is not None:
        payload["explain"] = dict(hit.explain)
    return payload


# A small English stopword set — enough to keep the derived `similar` query
# focused on content words. Deliberately not exhaustive: the frequency ranking
# already demotes filler, and an over-broad list would strip useful terms.
_SIMILAR_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "any",
        "can",
        "had",
        "her",
        "was",
        "one",
        "our",
        "out",
        "day",
        "get",
        "has",
        "him",
        "his",
        "how",
        "man",
        "new",
        "now",
        "old",
        "see",
        "two",
        "way",
        "who",
        "boy",
        "did",
        "its",
        "let",
        "put",
        "say",
        "she",
        "too",
        "use",
        "that",
        "this",
        "with",
        "from",
        "they",
        "have",
        "were",
        "what",
        "your",
        "when",
        "than",
        "then",
        "them",
        "some",
        "into",
        "more",
        "only",
        "over",
        "such",
        "also",
        "been",
        "both",
        "each",
        "here",
        "just",
        "like",
        "much",
        "must",
        "most",
        "very",
        "will",
        "would",
        "about",
        "there",
        "their",
        "which",
        "these",
        "those",
        "other",
        "could",
        "after",
        "before",
        "between",
    }
)

# Word characters for term extraction — alphanumerics across scripts, no
# underscore. `re.findall` over this naturally drops markdown punctuation
# (``# * _ [ ] ( ) ` >``), wikilink syntax, and stray symbols.
_SIMILAR_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _derive_similarity_terms(title: str, body: str, *, max_terms: int = 10) -> list[str]:
    """Pick up to `max_terms` content words to seed a "more like this" query.

    Title words come first (high signal, always kept), then body words
    ordered by descending frequency (ties broken by first appearance).
    Tokens shorter than three characters and stopwords are dropped; the
    result is de-duplicated while preserving that priority order.
    """

    def tokens(text: str) -> list[str]:
        out: list[str] = []
        for match in _SIMILAR_WORD_RE.findall(text.lower()):
            if len(match) >= 3 and match not in _SIMILAR_STOPWORDS:
                out.append(match)
        return out

    body_tokens = tokens(body)
    frequency: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, token in enumerate(body_tokens):
        frequency[token] = frequency.get(token, 0) + 1
        first_seen.setdefault(token, index)
    ranked_body = sorted(frequency, key=lambda token: (-frequency[token], first_seen[token]))

    ordered: list[str] = []
    seen: set[str] = set()
    for token in [*tokens(title), *ranked_body]:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
        if len(ordered) >= max_terms:
            break
    return ordered


def find_similar(
    store: Store,
    vault_dir: Path,
    target: str,
    *,
    limit: int = 10,
    family: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Build the `knoten similar` payload — related notes, no embeddings.

    Resolves `target` the same way `read` does, derives a disjunctive query
    from its title + most frequent body terms, runs it through the ranked
    FTS5 search (OR semantics), drops the note itself, and returns the top
    `limit` hits. Shares the search-hit serialization so the JSON shape
    matches `knoten search`. Network- and lock-free.
    """
    row = resolve_target(store, target)
    note_id = row["id"]
    derived_query = " ".join(_derive_similarity_terms(row["title"] or "", store.fts_body(note_id)))
    if not derived_query:
        return {
            "target_id": note_id,
            "target_filename": row["filename"],
            "derived_query": "",
            "total": 0,
            "hits": [],
        }
    # Fetch one extra so excluding the note itself still leaves a full page.
    hits, _total = store.search(
        derived_query,
        family=family,
        kind=kind,
        tag=tag,
        limit=limit + 1,
        offset=0,
        vault_dir=vault_dir,
        match_any=True,
    )
    similar = [hit for hit in hits if hit.id != note_id][:limit]
    return {
        "target_id": note_id,
        "target_filename": row["filename"],
        "derived_query": derived_query,
        "total": len(similar),
        "hits": [hit_to_dict(hit) for hit in similar],
    }


# ---- Write-path (remote-first) ------------------------------------------


def upload_file_remote(
    *,
    backend: Backend,
    store: Store,
    vault_dir: Path,
    source_path: Path,
    filename: str,
    tags: list[str],
    source: str | None,
    content_type: str | None,
) -> tuple[Note, dict[str, Any]]:
    """Upload `source_path` to the remote and create a linked file-family note.

    Two HTTP calls in order:

      1. `POST /api/attachments` — multipart upload; response contains a
         `storageKey` string that identifies the stored blob.
      2. `POST /api/notes` — creates a file-family note whose frontmatter
         sets `attachment: <storage_key>`. The server rejects non-file
         filenames for this shape, so the caller is responsible for using
         a `CiteKey+` or `YYYY-MM-DD+` prefix.

    The created note is then re-fetched and ingested into the local mirror.
    Returns `(note, upload_metadata)` — the metadata dict is the raw upload
    response, useful for the CLI's JSON output.
    """
    if not source_path.is_file():
        raise UserError(f"Not a file: {source_path}")

    upload = backend.upload_attachment(
        source_path,
        content_type=content_type,
        source=source,
    )
    if not upload.storage_key:
        raise UserError("Upload response missing storageKey")

    composed_body = _compose_body("", add_tags=tags, remove_tags=[])
    draft = NoteDraft(
        filename=filename,
        body=composed_body,
        kind="file",
        frontmatter={"attachment": upload.storage_key},
    )

    created_id = backend.create_note(draft)
    fresh = backend.read_note(created_id)
    ingest_note(fresh, store=store, vault_dir=vault_dir)
    upload_meta: dict[str, Any] = {
        "storageKey": upload.storage_key,
        "contentType": upload.content_type,
        "sizeBytes": upload.size_bytes,
        "url": upload.url,
    }
    return fresh, upload_meta


# Server attachment storage keys are `crypto.randomBytes(16).toString("hex")`
# (32 lowercase hex chars) plus the original file extension (`path.extname`),
# which may be absent. Mirrors the server's own ref-parser shape — see
# ATTACHMENT_URL_RE in the backend's `services/refs.ts`.
_ATTACHMENT_STORAGE_KEY_RE = re.compile(r"[a-f0-9]+(\.\w+)?")


def download_file_remote(
    *,
    backend: Backend,
    store: Store,
    target: str,
    destination: Path | None,
) -> dict[str, Any]:
    """Resolve a file-family note and stream its attachment to disk.

    The storage key lives in the note's `frontmatter.attachment` field. The
    function refuses to download non-file-family notes — there is nothing
    to download — and refuses targets whose frontmatter has no attachment
    key (malformed file note or pending upload).
    """
    row = resolve_target(store, target)
    if row.get("family") != "file":
        raise UserError(
            f"Note '{row['filename']}' is not a file-family note "
            f"(family={row.get('family')}) — nothing to download"
        )

    import json as _json

    try:
        frontmatter = _json.loads(row.get("frontmatter_json") or "{}")
    except _json.JSONDecodeError:
        frontmatter = {}
    storage_key = frontmatter.get("attachment")
    if not isinstance(storage_key, str) or not storage_key:
        raise UserError(
            f"Note '{row['filename']}' has no `attachment` key in its frontmatter — "
            "the link to the uploaded blob is missing"
        )
    # The key is interpolated straight into `GET /api/attachments/{storage_key}`,
    # so a key containing `/`, `?`, or `#` would redirect the authenticated
    # request to an unintended path. Server-issued keys are strictly hex16 with
    # an optional file extension (the server's own ref-parser keys on
    # `[a-f0-9]+\.\w+` — see ATTACHMENT_URL_RE in refs.ts); reject anything else
    # before a URL is built. Local mode is already guarded: LocalBackend looks
    # the key up in the `attachments` table and never builds a URL from it.
    if not _ATTACHMENT_STORAGE_KEY_RE.fullmatch(storage_key):
        raise UserError(
            f"Note '{row['filename']}' references an attachment with an invalid "
            f"storage key {storage_key!r} — keys must be hexadecimal with an "
            "optional file extension. Refusing to build a download URL from it."
        )

    # -o/--output is the user's explicit choice and is honoured as-is; the
    # DEFAULT destination is derived from the server-controlled filename and
    # must be confined (a hostile remote could otherwise name a note
    # "/home/user/.bashrc" and overwrite arbitrary files on download).
    if destination is not None:
        chosen = destination
    else:
        chosen = _default_download_destination(row["filename"])
    download = backend.download_attachment(storage_key, chosen)
    return {
        "path": download.path,
        "bytes_written": download.bytes_written,
        "content_type": download.content_type,
        "note_id": row["id"],
        "filename": row["filename"],
        "storage_key": storage_key,
    }


def _default_download_destination(server_filename: str) -> Path:
    """Derive a cwd-confined default download path from a note's filename.

    The note `filename` field is server-controlled, so only its basename is
    used. Rejects basenames that are empty, contain NUL or path separators,
    or are `.`/`..`, and requires the resolved destination to stay under the
    current working directory.
    """
    name = Path(server_filename).name
    if not name or "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
        raise UserError(
            f"Refusing to derive a download destination from note filename "
            f"{server_filename!r} — pass -o/--output to choose one explicitly"
        )
    cwd = Path.cwd().resolve()
    chosen = (cwd / name).resolve()
    if not chosen.is_relative_to(cwd):
        raise UserError(
            f"Default download destination for note filename {server_filename!r} "
            f"escapes the current directory — pass -o/--output to choose one explicitly"
        )
    return chosen


def _is_local_backend(backend: Backend) -> bool:
    """True if the backend writes to the local vault only (no remote round-trip).

    Lazy import keeps this module free of the LocalBackend dependency at
    parse time — services/notes is imported very early in the CLI startup.
    """
    from knoten.repositories.local_backend import LocalBackend

    return isinstance(backend, LocalBackend)


def create_note_remote(
    *,
    backend: Backend,
    store: Store,
    vault_dir: Path,
    filename: str,
    body: str | None,
    kind: str | None,
    tags: list[str],
    frontmatter: dict[str, Any] | None = None,
) -> Note:
    """POST to the remote, then fetch and mirror the created note locally."""
    composed_body = _compose_body(body or "", add_tags=tags, remove_tags=[])
    draft = NoteDraft(
        filename=filename,
        body=composed_body,
        kind=kind,
        frontmatter=dict(frontmatter) if frontmatter else {},
    )
    created_id = backend.create_note(draft)
    fresh = backend.read_note(created_id)
    # Local-backend writes never round-trip through a remote; the new row is
    # `synced=0` until a remote sync's push pass uploads it.
    ingest_note(
        fresh,
        store=store,
        vault_dir=vault_dir,
        synced=not _is_local_backend(backend),
    )
    return fresh


@dataclass(frozen=True)
class EditNoteResult:
    """Outcome of `edit_note_remote`.

    `note` is the refreshed primary note. `restricted_affected` holds the ids
    of rename-cascade targets the server rewrote but this token cannot READ
    (per-note 404 → `NoteForbiddenError`); each is mirrored as a metadata-only
    placeholder instead of a full note. `warnings` carries human-readable
    notices for cascade targets whose re-mirror was skipped (frontmatter
    failed the writer's validation) — the edit itself succeeded server-side.
    Both are empty on the common path.
    """

    note: Note
    restricted_affected: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def summary_from_row(row: dict[str, Any]) -> NoteSummary:
    """Reconstruct a `NoteSummary` from a stored `notes` row (a `find_by_id` dict).

    Lets a placeholder ingest reuse a note's already-mirrored metadata instead
    of refetching a summary the server would refuse — the rename-cascade guard
    here and reconcile's restricted-placeholder rebuild share this mapping.
    """
    return NoteSummary(
        id=row["id"],
        filename=row["filename"],
        title=row["title"],
        family=row["family"],
        kind=row["kind"],
        source=row["source"],
        tags=(),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        permissions=row.get("permissions") or "ALL",
    )


def edit_note_remote(
    *,
    backend: Backend,
    store: Store,
    vault_dir: Path,
    target: str,
    new_filename: str | None,
    new_title: str | None,
    new_body: str | None,
    set_frontmatter: dict[str, str],
    unset_frontmatter: list[str],
    add_tags: list[str],
    remove_tags: list[str],
    set_frontmatter_json: dict[str, Any] | None = None,
    force: bool = False,
) -> EditNoteResult:
    row = resolve_target(store, target)
    _assert_permission(row, required_level="WRITE", operation="edit", force=force)
    note_id = row["id"]
    previous_path = row["path"]
    json_sets = set_frontmatter_json or {}

    # Compute body if any tag change is requested — need current body from disk.
    body_to_send: str | None = None
    if new_body is not None or add_tags or remove_tags:
        current_body = new_body
        if current_body is None:
            current_body = _read_stripped_body(vault_dir, previous_path)
        body_to_send = _compose_body(current_body, add_tags=add_tags, remove_tags=remove_tags)

    # Prefix immutability check on rename.
    if new_filename is not None:
        _assert_same_family_prefix(row["filename"], new_filename)

    patch_frontmatter: dict[str, Any] | None = None
    if set_frontmatter or unset_frontmatter or json_sets:
        patch_frontmatter = _apply_frontmatter_changes(
            row["frontmatter_json"], set_frontmatter, unset_frontmatter, json_sets=json_sets
        )

    patch = NotePatch(
        filename=new_filename,
        title=new_title,
        body=body_to_send,
        frontmatter=patch_frontmatter,
    )
    if (
        patch.filename is None
        and patch.title is None
        and patch.body is None
        and patch.frontmatter is None
    ):
        raise UserError("Nothing to update — pass at least one --filename/--body/--tag/--set flag")

    update_result = backend.update_note(note_id, patch)
    fresh = backend.read_note(note_id)
    synced = not _is_local_backend(backend)
    ingest_note(fresh, store=store, vault_dir=vault_dir, previous_path=previous_path, synced=synced)

    # Rename cascade: when the server rewrites [[old]] → [[new]] in other
    # notes' bodies, it returns them in `affected_notes`. Re-fetch each and
    # re-ingest so the local mirror converges without a full sync.
    restricted_affected: list[str] = []
    cascade_warnings: list[str] = []
    for affected_id in update_result.affected_notes:
        if affected_id == note_id:
            continue
        try:
            affected_note = backend.read_note(affected_id)
        except NoteForbiddenError:
            # The server's cascade rewrites every linking note regardless of
            # this token's per-note permissions, so `affected_notes` can name
            # ids the token cannot READ (permission NONE/LIST) — the server
            # returns a per-note 404. The rename already succeeded server-side,
            # so degrade to a metadata-only placeholder (the same branch the
            # sync pull pass uses) and keep going instead of failing the whole
            # operation. The placeholder needs the note's metadata, which we
            # only have when the note is already mirrored locally; otherwise we
            # cannot write a file, so just record the id and move on.
            existing = store.find_by_id(affected_id)
            if existing is not None:
                ingest_placeholder(
                    summary_from_row(existing),
                    store=store,
                    vault_dir=vault_dir,
                    previous_path=existing["path"],
                )
            restricted_affected.append(affected_id)
            continue
        try:
            ingest_note(affected_note, store=store, vault_dir=vault_dir, synced=synced)
        except FrontmatterValidationError as exc:
            # The cascade target's server copy carries a frontmatter value the
            # mirror writer refuses (control character). The rename already
            # succeeded server-side, so skip re-mirroring this one note with a
            # warning naming it instead of failing the whole edit — same
            # ingest-boundary contract as the sync pull pass.
            cascade_warnings.append(
                f"skipped re-mirroring affected note {affected_note.id} "
                f"('{affected_note.filename}'): frontmatter value for {exc.key!r} "
                "contains a control character — its local mirror is stale until "
                "the value is fixed on the server"
            )
            continue

    return EditNoteResult(
        note=fresh,
        restricted_affected=tuple(restricted_affected),
        warnings=tuple(cascade_warnings),
    )


def delete_note_remote(
    *,
    backend: Backend,
    store: Store,
    vault_dir: Path,
    target: str,
    force: bool = False,
) -> str:
    row = resolve_target(store, target)
    _assert_permission(row, required_level="ALL", operation="delete", force=force)
    note_id = row["id"]
    backend.delete_note(note_id)
    delete_ingested(store, vault_dir, note_id)
    return note_id


def append_note_remote(
    *,
    backend: Backend,
    store: Store,
    vault_dir: Path,
    target: str,
    content: str,
    force: bool = False,
) -> Note:
    """POST to `/api/notes/{id}/append` and refresh the local mirror.

    `knoten append` is distinct from `knoten edit --body` because the
    server endpoint accepts the weaker `APPEND` permission (body extended,
    never truncated). The content is joined with a blank-line separator
    on the server side.
    """
    row = resolve_target(store, target)
    _assert_permission(row, required_level="APPEND", operation="append", force=force)
    note_id = row["id"]
    previous_path = row["path"]
    backend.append_to_note(note_id, content)
    fresh = backend.read_note(note_id)
    ingest_note(
        fresh,
        store=store,
        vault_dir=vault_dir,
        previous_path=previous_path,
        synced=not _is_local_backend(backend),
    )
    return fresh


def restore_note_remote(
    *, backend: Backend, store: Store, vault_dir: Path, note_id: str, force: bool = False
) -> Note:
    if not is_uuid(note_id):
        raise UserError("restore only accepts UUIDs (trash lookups are by id)")
    # Local permission pre-check (restore needs WRITE on the server). The
    # trashed row carries the note's permission level when one is mirrored
    # locally; in remote mode after a remote-only delete there is no local
    # trash row, so the check is best-effort and the server stays the final
    # authority (its 403 FORBIDDEN now maps to permission_denied). `--force`
    # skips it, consistent with edit/delete/append.
    trashed = store.find_trashed(note_id)
    if trashed is not None:
        _assert_permission(trashed, required_level="WRITE", operation="restore", force=force)
    backend.restore_note(note_id)
    fresh = backend.read_note(note_id)
    ingest_note(fresh, store=store, vault_dir=vault_dir, synced=not _is_local_backend(backend))
    return fresh


# ---- helpers -----------------------------------------------------------


def _compose_body(body: str, *, add_tags: list[str], remove_tags: list[str]) -> str:
    """Rewrite a body to add/remove trailing `#tag` markers.

    Tags live in `#hashtags` in the body (server-side convention). add_tags
    appends missing ones at the end; remove_tags strips them as standalone
    words anywhere in the body.
    """
    new_body = body
    for tag in remove_tags:
        # Case-insensitive so `--remove-tag foo` strips `#Foo` / `#FOO` too —
        # tags are case-folded everywhere else (parser + store), so the body
        # rewrite must match every case variant of the tag literal.
        pattern = re.compile(rf"(?<![\w#])#{re.escape(tag)}\b", re.IGNORECASE)
        new_body = pattern.sub("", new_body)
    new_body = re.sub(r"[ \t]+\n", "\n", new_body).rstrip()

    missing = [
        tag
        for tag in add_tags
        if not re.search(rf"(?<![\w#])#{re.escape(tag)}\b", new_body, re.IGNORECASE)
    ]
    if missing:
        suffix = " ".join(f"#{tag}" for tag in missing)
        new_body = f"{new_body.rstrip()}\n\n{suffix}\n" if new_body else f"{suffix}\n"
    return new_body


def _read_stripped_body(vault_dir: Path, relative_path: str) -> str:
    absolute = vault_dir / relative_path
    text = absolute.read_text(encoding="utf-8")
    return strip_frontmatter(text)


def _apply_frontmatter_changes(
    current_json: str,
    sets: dict[str, str],
    unsets: list[str],
    json_sets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    import json as _json

    try:
        current = _json.loads(current_json) if current_json else {}
    except _json.JSONDecodeError:
        current = {}
    for key in unsets:
        current.pop(key, None)
    for key, value in sets.items():
        current[key] = value
    # Typed sets win over string sets for the same key, and preserve JSON
    # types (ints, lists, null) rather than coercing to a string.
    for key, value in (json_sets or {}).items():
        current[key] = value
    return current


def _assert_same_family_prefix(old_filename: str, new_filename: str) -> None:
    """The family prefix (symbol or source+symbol) is immutable.

    We enforce this client-side so the error is clean. Server would reject too.
    """
    old_prefix = _family_prefix(old_filename)
    new_prefix = _family_prefix(new_filename)
    if old_prefix != new_prefix:
        raise UserError(f"Family prefix is immutable. Old '{old_prefix}' vs new '{new_prefix}'.")


def _family_prefix(filename: str) -> str:
    """Return the immutable family-prefix portion of a filename.

    Exact-match families use a single-symbol prefix (e.g. '@ '). Suffix-match
    families use '<source><symbol> ' (e.g. 'Voland2024= '). Date-prefixed
    families use YYYY-MM-DD or similar.
    """
    # Simple heuristic: prefix up to and including the first space.
    space = filename.find(" ")
    if space == -1:
        return filename
    return filename[: space + 1]


# ---- quelle Source → knoten reference -----------------------------------

# Maps a quelle Publication `kind` to a knoten reference `kind`. quelle's
# kind vocabulary is broader than knoten's reference kinds, so several quelle
# kinds collapse onto one knoten kind; anything missing or unrecognised falls
# back to `document`.
QUELLE_KIND_TO_REFERENCE_KIND: dict[str, str] = {
    "article": "article",
    "preprint": "article",
    "book": "book",
    "book-chapter": "book",
    "web": "web",
    "media": "media",
}


@dataclass(frozen=True)
class ReferenceInputs:
    """knoten-side inputs derived from a quelle Source.

    `filename` is `<CiteKey>= <Title>`; `kind` is the mapped reference kind;
    `frontmatter` uses knoten's hyphen-key convention; `tags` carries `ai`
    when the caller asked for AI-authored framing.
    """

    filename: str
    kind: str
    frontmatter: dict[str, Any]
    tags: list[str]


def _citekey_from_source(source: dict[str, Any]) -> str:
    """Resolve the CiteKey: `x_vcoeur.citekey` wins over top-level `citation_key`."""
    x_vcoeur = source.get("x_vcoeur")
    if isinstance(x_vcoeur, dict):
        citekey = x_vcoeur.get("citekey")
        if isinstance(citekey, str) and citekey:
            return citekey
    citation_key = source.get("citation_key")
    if isinstance(citation_key, str) and citation_key:
        return citation_key
    raise UserError("source has no citation_key / x_vcoeur.citekey")


def _present(value: Any) -> bool:
    """True when a Source value is worth carrying into frontmatter.

    Omits None, empty strings, and empty collections; keeps 0 / False and
    any non-empty scalar or collection.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


_WIKILINK_SPECIAL_RE = re.compile(r"[|#\[\]/\\]")


def _sanitize_filename_title(title: str) -> str:
    """Strip filename-breaking chars from a title bound for a `CiteKey= Title` filename.

    The vault cites a reference note as `[[CiteKey= Title]]`, and knoten resolves
    wikilinks by the full filename. Two classes of character break that filename and
    are replaced with spaces (collapsed):

    - **Wikilink-special** — `|` (the alias separator), `#` (heading anchor), and
      `[`/`]` — make the wikilink unresolvable (common for web titles like
      "Working on projects | uv").
    - **Path separators** — `/` and `\\` — would split the filename into spurious
      sub-directories (e.g. a web title "astral-sh/uv" nesting the note under a stray
      folder) and break filename-based storage on the remote backend.

    The original title is kept verbatim in the `title` frontmatter field.
    """
    cleaned = _WIKILINK_SPECIAL_RE.sub(" ", title)
    return re.sub(r"\s+", " ", cleaned).strip()


def source_to_reference_inputs(source: dict[str, Any], *, ai: bool) -> ReferenceInputs:
    """Map a quelle Source dict to knoten reference-note inputs.

    `source` is a quelle `Publication` object (snake_case keys), optionally
    carrying an `x_vcoeur` block. Returns the `<CiteKey>= <Title>` filename,
    the mapped reference `kind`, the hyphen-keyed frontmatter (any
    missing/empty Source field is omitted), and the tag list (`ai` when
    requested). Raises `UserError` when no CiteKey can be resolved.
    """
    citekey = _citekey_from_source(source)
    kind = QUELLE_KIND_TO_REFERENCE_KIND.get(source.get("kind"), "document")
    title = source.get("title") or ""
    filename_title = _sanitize_filename_title(title)
    filename = f"{citekey}= {filename_title}" if filename_title else f"{citekey}="

    # knoten's hyphen-key convention (not quelle's snake_case). family/kind/
    # source are always present; everything else is set only when the Source
    # carries a non-empty value.
    frontmatter: dict[str, Any] = {
        "family": "reference",
        "kind": kind,
        "source": citekey,
    }
    if _present(title):
        frontmatter["title"] = title
    authors = [
        f"[[@ {author['name']}]]"
        for author in (source.get("authors") or [])
        if isinstance(author, dict) and _present(author.get("name"))
    ]
    if authors:
        frontmatter["authors"] = authors
    # (knoten frontmatter key, quelle Source key) for the flat scalar fields.
    scalar_map = (
        ("year", "year"),
        ("publisher", "publisher"),
        ("edition", "edition"),
        ("isbn-13", "isbn_13"),
        ("isbn-10", "isbn_10"),
        ("page-count", "page_count"),
        ("url", "source_url"),
    )
    for knoten_key, source_key in scalar_map:
        value = source.get(source_key)
        if _present(value):
            frontmatter[knoten_key] = value
    subjects = source.get("subjects")
    if _present(subjects):
        frontmatter["subjects"] = list(subjects)

    tags = ["ai"] if ai else []
    return ReferenceInputs(filename=filename, kind=kind, frontmatter=frontmatter, tags=tags)


# ---- dry-run previews ---------------------------------------------------


def _unresolved_titles(store: Store, body: str) -> list[str]:
    """Wikilink target titles in `body` that resolve to no existing note."""
    from knoten.services.markdown_parser import parse_body

    titles = parse_body(body).wikilink_titles
    return [title for title in titles if store.find_by_filename(title) is None]


def preview_create(
    store: Store,
    *,
    filename: str,
    body: str | None,
    kind: str | None,
    tags: list[str],
    frontmatter: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve a would-be `create` without writing anything.

    Reports the family/kind/source the filename parses to, whether the
    prefix is recognised, whether the filename already exists, and which
    `[[wikilinks]]` in the body do not yet resolve to a note.
    """
    from knoten.services.knoten_filename import (
        FAMILY_TO_DIRECTORY,
        has_valid_prefix,
        parse_knoten_filename,
    )

    parsed = parse_knoten_filename(filename)
    body_for_links = _compose_body(body or "", add_tags=tags, remove_tags=[])
    return {
        "dry_run": True,
        "operation": "create",
        "filename": filename,
        "family": parsed.family,
        "kind": kind or parsed.family,
        "title": parsed.title,
        "source": parsed.source,
        "directory": FAMILY_TO_DIRECTORY.get(parsed.family),
        "has_valid_prefix": has_valid_prefix(filename),
        "filename_exists": store.find_by_filename(filename) is not None,
        "frontmatter": frontmatter or {},
        "tags": list(tags),
        "unresolved_wikilinks": _unresolved_titles(store, body_for_links),
    }


def preview_reference(
    store: Store,
    *,
    filename: str,
    kind: str,
    body: str | None,
    tags: list[str],
    frontmatter: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve a would-be `reference --from-source` without writing anything.

    Same shape as `preview_create`, but the operation label is `reference`
    so the caller can tell the two dry-run paths apart. The mapped `kind` and
    built frontmatter come from the quelle Source.
    """
    preview = preview_create(
        store,
        filename=filename,
        body=body,
        kind=kind,
        tags=tags,
        frontmatter=frontmatter,
    )
    preview["operation"] = "reference"
    return preview


def preview_edit(
    store: Store,
    vault_dir: Path,
    *,
    target: str,
    new_filename: str | None,
    new_title: str | None,
    new_body: str | None,
    set_frontmatter: dict[str, str],
    set_frontmatter_json: dict[str, Any],
    unset_frontmatter: list[str],
    add_tags: list[str],
    remove_tags: list[str],
    force: bool = False,
) -> dict[str, Any]:
    """Validate a would-be `edit` / `rename` without writing.

    Runs the same permission and immutable-prefix checks the real edit
    runs, lists the fields that would change, and reports any unresolved
    wikilinks in the resulting body. Raises on a no-op or a blocked write,
    exactly as the real command would — so a dry-run is a safe pre-flight.
    """
    row = resolve_target(store, target)
    _assert_permission(row, required_level="WRITE", operation="edit", force=force)
    if new_filename is not None:
        _assert_same_family_prefix(row["filename"], new_filename)

    body_for_links: str | None = None
    if new_body is not None or add_tags or remove_tags:
        current_body = new_body
        if current_body is None:
            current_body = _read_stripped_body(vault_dir, row["path"])
        body_for_links = _compose_body(current_body, add_tags=add_tags, remove_tags=remove_tags)

    changes: dict[str, Any] = {}
    if new_filename is not None:
        changes["filename"] = new_filename
    if new_title is not None:
        changes["title"] = new_title
    if new_body is not None:
        changes["body"] = "<replaced>"
    if add_tags:
        changes["add_tags"] = list(add_tags)
    if remove_tags:
        changes["remove_tags"] = list(remove_tags)
    merged_sets = {**set_frontmatter, **(set_frontmatter_json or {})}
    if merged_sets:
        changes["set_frontmatter"] = merged_sets
    if unset_frontmatter:
        changes["unset_frontmatter"] = list(unset_frontmatter)
    if not changes:
        raise UserError("Nothing to update — pass at least one change flag")

    return {
        "dry_run": True,
        "operation": "edit",
        "id": row["id"],
        "filename": row["filename"],
        "current_permission": row.get("permissions") or "ALL",
        "changes": changes,
        "unresolved_wikilinks": (
            _unresolved_titles(store, body_for_links) if body_for_links is not None else []
        ),
    }
