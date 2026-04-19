"""Note read/write orchestration.

Reads are resolved against the local store. Writes go to the remote first,
then the updated note is re-fetched and upserted into the local store so the
mirror is always at least as fresh as the last successful write.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from knoten.models import Note, NoteSummary, SearchHit, permission_at_least
from knoten.repositories.backend import Backend, NoteDraft, NotePatch
from knoten.repositories.errors import (
    AmbiguousTargetError,
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
) -> str:
    """Upsert the store row + write the mirror file. Returns the relative path.

    Order is deliberate: **transaction first, filesystem second**. If the
    process dies at any point after the store commit, the worst case is a
    missing file at `new_path` (or a stale file at `previous_path`) — both
    recoverable by the next `reconcile_local` pass. Writing the file first
    would leave a window where on-disk content disagrees with the FTS5
    index until the next sync re-fetched the note.
    """
    relative_path = path_for_note(note)
    body_sha = hashlib.sha256(note.body.encode("utf-8")).hexdigest()
    content = render_note_markdown(note)

    # 1. Commit the store + FTS5 + derived rows in one transaction.
    store.upsert_note(note, path=relative_path, body_sha256=body_sha)

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

    body_without_frontmatter = _strip_frontmatter(body)
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


def _strip_frontmatter(body: str) -> str:
    """Remove a leading YAML frontmatter block, if any."""
    if not body.startswith("---\n"):
        return body
    end = body.find("\n---\n", 4)
    if end == -1:
        return body
    return body[end + 5 :]


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


def hit_to_dict(hit: SearchHit) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": hit.id,
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

    chosen = destination if destination is not None else Path.cwd() / row["filename"]
    download = backend.download_attachment(storage_key, chosen)
    return {
        "path": download.path,
        "bytes_written": download.bytes_written,
        "content_type": download.content_type,
        "note_id": row["id"],
        "filename": row["filename"],
        "storage_key": storage_key,
    }


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
    ingest_note(fresh, store=store, vault_dir=vault_dir)
    return fresh


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
    force: bool = False,
) -> Note:
    row = resolve_target(store, target)
    _assert_permission(row, required_level="WRITE", operation="edit", force=force)
    note_id = row["id"]
    previous_path = row["path"]

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
    if set_frontmatter or unset_frontmatter:
        patch_frontmatter = _apply_frontmatter_changes(
            row["frontmatter_json"], set_frontmatter, unset_frontmatter
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
    ingest_note(fresh, store=store, vault_dir=vault_dir, previous_path=previous_path)

    # Rename cascade: when the server rewrites [[old]] → [[new]] in other
    # notes' bodies, it returns them in `affected_notes`. Re-fetch each and
    # re-ingest so the local mirror converges without a full sync.
    for affected_id in update_result.affected_notes:
        if affected_id == note_id:
            continue
        affected_note = backend.read_note(affected_id)
        ingest_note(affected_note, store=store, vault_dir=vault_dir)

    return fresh


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
    ingest_note(fresh, store=store, vault_dir=vault_dir, previous_path=previous_path)
    return fresh


def restore_note_remote(*, backend: Backend, store: Store, vault_dir: Path, note_id: str) -> Note:
    if not is_uuid(note_id):
        raise UserError("restore only accepts UUIDs (trash lookups are by id)")
    backend.restore_note(note_id)
    fresh = backend.read_note(note_id)
    ingest_note(fresh, store=store, vault_dir=vault_dir)
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
        pattern = re.compile(rf"(?<![\w#])#{re.escape(tag)}\b")
        new_body = pattern.sub("", new_body)
    new_body = re.sub(r"[ \t]+\n", "\n", new_body).rstrip()

    missing = [
        tag for tag in add_tags if not re.search(rf"(?<![\w#])#{re.escape(tag)}\b", new_body)
    ]
    if missing:
        suffix = " ".join(f"#{tag}" for tag in missing)
        new_body = f"{new_body.rstrip()}\n\n{suffix}\n" if new_body else f"{suffix}\n"
    return new_body


def _read_stripped_body(vault_dir: Path, relative_path: str) -> str:
    absolute = vault_dir / relative_path
    text = absolute.read_text(encoding="utf-8")
    return _strip_frontmatter(text)


def _apply_frontmatter_changes(
    current_json: str, sets: dict[str, str], unsets: list[str]
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
