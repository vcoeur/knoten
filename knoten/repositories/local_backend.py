"""Filesystem + SQLite `Backend` implementation.

Operates on a markdown vault under `settings.paths.vault_dir` with a local
`Store` (SQLite + FTS5) as a derived index. No network, no permission
model — a standalone zettelkasten CLI for users who do not want to run
their own remote backend.

Phase 5: read path (`list_note_summaries`, `read_note`) plus a
mtime-gated stat walk in `_refresh_index_if_stale` that detects external
edits — files written by the user's editor, deleted via `rm`, etc. The
walk runs at most once per CLI invocation. Mutation methods still raise
`NotImplementedError` until Phase 6 lands writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from knoten.models import Note, WikiLink
from knoten.repositories.backend import (
    AttachmentDownloadResult,
    AttachmentUploadResult,
    Backend,
    NoteDraft,
    NotePatch,
    NotesPage,
    NoteUpdateResult,
)
from knoten.repositories.errors import NotFoundError, UserError
from knoten.repositories.store import Store
from knoten.repositories.vault_files import path_for_note, strip_frontmatter
from knoten.services.knoten_filename import parse_knoten_filename
from knoten.services.markdown_parser import parse_body
from knoten.services.notes import _assert_same_family_prefix, ingest_note
from knoten.settings import Settings

_LOG = logging.getLogger(__name__)

# Top-level directories under the vault that the drift walk skips. These
# hold machine-managed files (soft-deleted notes, attachment blobs) that
# should not surface as note drift.
_SKIP_TOP_LEVEL = frozenset({".trash", ".attachments"})


class LocalBackend(Backend):
    """`Backend` backed by a markdown vault + local SQLite index."""

    def __init__(self, settings: Settings, *, store: Store | None = None) -> None:
        if not settings.paths.vault_dir.exists():
            raise UserError(
                f"Vault directory does not exist: {settings.paths.vault_dir} — "
                "run `knoten init` or set KNOTEN_DATA_DIR to a directory with a "
                "`kasten/` subdir."
            )
        self._settings = settings
        self._vault_dir = settings.paths.vault_dir
        # An injected store lets a caller that already holds an open Store
        # (e.g. a CLI read command) run the stat walk on the same SQLite
        # connection instead of opening a second, concurrently-writing one.
        if store is not None:
            self._store = store
            self._owns_store = False
        else:
            self._store = Store(settings.paths.index_path)
            self._store.open()
            self._owns_store = True
        self._reindex_done: bool = False

    def __enter__(self) -> LocalBackend:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_store:
            self._store.close()

    def refresh_index(self) -> None:
        """Run the mtime-gated stat walk now (public entry for read commands)."""
        self._refresh_index_if_stale()

    def _refresh_index_if_stale(self) -> None:
        """Stat-walk the vault and refresh drifted rows in the store.

        Runs at most once per `LocalBackend` instance — one walk per CLI
        invocation. For each `.md` file under `vault_dir`:

        - `(mtime_ns, size)` matches the store's recorded tuple → no-op.
        - Tuple mismatches → re-parse the body and call
          `Store.apply_drifted_body` to refresh FTS5, tags, wikilinks,
          and the recorded stat values.
        - File not known to the store (new file on disk) → logged and
          skipped. Creating notes from unknown markdown files requires a
          filename-to-family parser that lands with `create_note` in
          Phase 6; until then, users who drop files into the vault by
          hand need to re-run `knoten sync` after Phase 6.

        Missing files — entries in the store whose path is no longer on
        disk — are hard-deleted. External `rm foo.md` is a permanent
        delete; only `knoten delete` moves files to `.trash/`.
        """
        if self._reindex_done:
            return
        self._reindex_done = True

        try:
            path_index = self._store.path_index()
        except Exception as exc:
            _LOG.debug("Skipping stat walk (store not ready): %s", exc)
            return

        seen_paths: set[str] = set()
        vault_root = self._vault_dir.resolve()

        for file_path in sorted(self._vault_dir.rglob("*.md")):
            try:
                relative = file_path.resolve().relative_to(vault_root)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] in _SKIP_TOP_LEVEL:
                continue

            relative_str = str(relative)
            seen_paths.add(relative_str)

            try:
                stat = file_path.stat()
            except OSError:
                continue

            indexed = path_index.get(relative_str)
            if indexed is None:
                _LOG.debug("Unknown vault file %s — Phase 5 skip; land in Phase 6", relative_str)
                continue

            recorded_mtime, recorded_size, note_id = indexed
            if recorded_mtime == stat.st_mtime_ns and recorded_size == stat.st_size:
                continue

            try:
                raw = file_path.read_text(encoding="utf-8")
            except OSError:
                continue
            body = strip_frontmatter(raw)
            parsed = parse_body(body)
            body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            self._store.apply_drifted_body(
                note_id,
                body=body,
                body_sha256=body_sha,
                tags=parsed.tags,
                wikilink_titles=parsed.wikilink_titles,
                path_mtime_ns=stat.st_mtime_ns,
                path_size=stat.st_size,
            )

        for missing_path in set(path_index) - seen_paths:
            _, _, missing_id = path_index[missing_path]
            # Conditional delete: reads run the walk without the advisory
            # lock, so the row may have been re-pointed (e.g. by a concurrent
            # rename) between the snapshot and now — only drop it if it still
            # has the path this walk saw missing.
            self._store.delete_note(missing_id, only_if_path=missing_path)

    def list_note_summaries(self, *, limit: int, offset: int) -> NotesPage:
        self._refresh_index_if_stale()
        summaries, total = self._store.list_notes(limit=limit, offset=offset)
        return NotesPage(
            data=tuple(summaries),
            total=total,
            limit=limit,
            offset=offset,
        )

    def read_note(self, note_id: str) -> Note:
        self._refresh_index_if_stale()
        row = self._store.find_by_id(note_id)
        if row is None:
            raise NotFoundError(f"No note with id {note_id}")

        absolute = self._vault_dir / row["path"]
        try:
            raw = absolute.read_text(encoding="utf-8")
        except OSError as exc:
            raise NotFoundError(f"Mirror file missing for {note_id}: {exc}") from exc

        body = strip_frontmatter(raw)

        frontmatter_raw = row.get("frontmatter_json") or "{}"
        try:
            frontmatter = json.loads(frontmatter_raw)
        except (TypeError, ValueError):
            frontmatter = {}
        if not isinstance(frontmatter, dict):
            frontmatter = {}

        wikilinks = tuple(
            WikiLink(
                target_title=str(link["target_title"]),
                target_id=(str(link["target_id"]) if link["target_id"] else None),
            )
            for link in self._store.wikilinks_for_note(note_id)
        )
        tags = self._store.tags_for_note(note_id)

        return Note(
            id=row["id"],
            filename=row["filename"],
            title=row["title"] or "",
            family=row["family"] or "",
            kind=row["kind"] or "",
            source=row.get("source"),
            body=body,
            frontmatter=frontmatter,
            tags=tuple(tags),
            wikilinks=wikilinks,
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
            permissions=row.get("permissions") or "ALL",
        )

    def create_note(self, draft: NoteDraft) -> str:
        self._refresh_index_if_stale()

        parsed = parse_knoten_filename(draft.filename)
        if not parsed.family:
            raise UserError(f"Could not derive family from filename {draft.filename!r}")
        if self._store.find_by_filename(draft.filename) is not None:
            raise UserError(
                f"A note with filename {draft.filename!r} already exists — "
                "pick a different name or edit the existing note."
            )

        note_id = str(uuid.uuid4())
        now = _utcnow_iso()
        kind = draft.kind or parsed.family
        parsed_body = parse_body(draft.body)

        note = Note(
            id=note_id,
            filename=draft.filename,
            title=parsed.title or draft.filename,
            family=parsed.family,
            kind=kind,
            source=parsed.source,
            body=draft.body,
            frontmatter=dict(draft.frontmatter),
            tags=_merge_tags(draft.tags, parsed_body.tags),
            wikilinks=tuple(
                WikiLink(target_title=title, target_id=None)
                for title in parsed_body.wikilink_titles
            ),
            created_at=now,
            updated_at=now,
            permissions="ALL",
        )
        self._refuse_unindexed_destination(path_for_note(note))
        ingest_note(note, store=self._store, vault_dir=self._vault_dir, synced=False)
        return note_id

    def _refuse_unindexed_destination(self, relative_path: str) -> None:
        """Refuse to write over an on-disk file the store does not know.

        Collision checks consult the store only, so a file dropped into the
        vault by hand (never indexed — the stat walk skips unknown files)
        would be silently overwritten by `os.replace`. In local mode the
        vault is authoritative, so clobbering those bytes is data loss.
        """
        destination = self._vault_dir / relative_path
        if destination.exists():
            raise UserError(
                f"A file already exists at {relative_path!r} but is not in the "
                "index — run `knoten sync` to import it, or rename the file "
                "(or pick a different note name) first."
            )

    def update_note(self, note_id: str, patch: NotePatch) -> NoteUpdateResult:
        self._refresh_index_if_stale()

        row = self._store.find_by_id(note_id)
        if row is None:
            raise NotFoundError(f"No note with id {note_id}")

        if patch.filename is not None and patch.filename != row["filename"]:
            return self._rename_with_cascade(row, patch)

        previous_path = row["path"]
        absolute = self._vault_dir / previous_path
        try:
            current_raw = absolute.read_text(encoding="utf-8")
        except OSError as exc:
            raise NotFoundError(f"Mirror file missing for {note_id}: {exc}") from exc
        current_body = strip_frontmatter(current_raw)

        new_body = patch.body if patch.body is not None else current_body
        if patch.add_tags or patch.remove_tags:
            new_body = _apply_tag_edits(
                new_body, add_tags=patch.add_tags, remove_tags=patch.remove_tags
            )

        try:
            current_fm = json.loads(row.get("frontmatter_json") or "{}")
            if not isinstance(current_fm, dict):
                current_fm = {}
        except (TypeError, ValueError):
            current_fm = {}
        new_fm = dict(patch.frontmatter) if patch.frontmatter is not None else current_fm

        new_title = patch.title if patch.title is not None else row["title"]

        parsed_body = parse_body(new_body)
        tags_tuple = tuple(sorted(set(parsed_body.tags)))

        note = Note(
            id=note_id,
            filename=row["filename"],
            title=new_title,
            family=row["family"],
            kind=row["kind"],
            source=row.get("source"),
            body=new_body,
            frontmatter=new_fm,
            tags=tags_tuple,
            wikilinks=tuple(
                WikiLink(target_title=title, target_id=None)
                for title in parsed_body.wikilink_titles
            ),
            created_at=row.get("created_at") or _utcnow_iso(),
            updated_at=_utcnow_iso(),
            permissions=row.get("permissions") or "ALL",
        )
        ingest_note(
            note,
            store=self._store,
            vault_dir=self._vault_dir,
            previous_path=previous_path,
            synced=False,
        )
        return NoteUpdateResult(note_id=note_id, affected_notes=())

    def _rename_with_cascade(
        self,
        row: dict,
        patch: NotePatch,
    ) -> NoteUpdateResult:
        """Rename a note and rewrite every incoming `[[old]]` wikilink.

        Mirrors the server-side `cascadeRename` in notes.vcoeur.com so a
        local vault stays consistent after a filename change. Rollback on
        partial failure restores both layers: every touched file goes back
        to its original bytes AND every touched store row is re-upserted to
        its pre-rename state — a file-only rollback left the store pointing
        at the new path, which the next stat walk hard-deleted.
        """
        note_id = row["id"]
        old_filename = row["filename"]
        new_filename = patch.filename
        assert new_filename is not None

        _assert_same_family_prefix(old_filename, new_filename)

        if new_filename == old_filename:
            return NoteUpdateResult(note_id=note_id, affected_notes=())

        collision = self._store.find_by_filename(new_filename)
        if collision is not None and collision["id"] != note_id:
            raise UserError(
                f"Cannot rename to {new_filename!r}: another note already uses that name."
            )

        source_rows = self._store.conn.execute(
            "SELECT DISTINCT source_id FROM wikilinks WHERE target_title = ?",
            (old_filename,),
        ).fetchall()
        source_ids = [str(r["source_id"]) for r in source_rows if str(r["source_id"]) != note_id]

        rewrite_re = re.compile(rf"\[\[{re.escape(old_filename)}(\]\]|#|\|)")

        def _replacement(match: re.Match[str]) -> str:
            # Callable replacement so `\1` / `\g` sequences in the new
            # filename are inserted literally instead of being expanded as
            # regex template escapes (which would corrupt links or raise).
            return f"[[{new_filename}{match.group(1)}"

        backups: list[tuple[Path, bytes]] = []
        created_paths: list[Path] = []
        # (note, path, body_sha256, synced) per touched store row, captured
        # before the cascade mutates it, so rollback can restore the store
        # alongside the file bytes.
        row_snapshots: list[tuple[Note, str, str, bool]] = []

        def _save_backup(absolute: Path) -> None:
            try:
                backups.append((absolute, absolute.read_bytes()))
            except OSError:
                pass

        def _snapshot_row(note_row: dict, raw_text: str) -> None:
            body = strip_frontmatter(raw_text)
            parsed = parse_body(body)
            try:
                fm = json.loads(note_row.get("frontmatter_json") or "{}")
                if not isinstance(fm, dict):
                    fm = {}
            except (TypeError, ValueError):
                fm = {}
            original = Note(
                id=note_row["id"],
                filename=note_row["filename"],
                title=note_row["title"],
                family=note_row["family"],
                kind=note_row["kind"],
                source=note_row.get("source"),
                body=body,
                frontmatter=fm,
                tags=tuple(sorted(set(parsed.tags))),
                wikilinks=tuple(
                    WikiLink(target_title=title, target_id=None) for title in parsed.wikilink_titles
                ),
                created_at=note_row.get("created_at") or _utcnow_iso(),
                updated_at=note_row.get("updated_at") or _utcnow_iso(),
                permissions=note_row.get("permissions") or "ALL",
            )
            row_snapshots.append(
                (
                    original,
                    note_row["path"],
                    note_row["body_sha256"],
                    bool(note_row.get("synced", 0)),
                )
            )

        def _rollback() -> None:
            # Files first: remove anything the cascade created, then restore
            # the original bytes of everything it touched.
            for created in created_paths:
                try:
                    created.unlink(missing_ok=True)
                except OSError:
                    _LOG.exception("Rollback could not remove %s", created)
            for absolute, original in reversed(backups):
                try:
                    absolute.parent.mkdir(parents=True, exist_ok=True)
                    absolute.write_bytes(original)
                except OSError:
                    _LOG.exception("Rollback failed for %s", absolute)
            # Store second: re-upsert every touched row to its pre-rename
            # state so disk and index stay consistent (the stat walk would
            # otherwise hard-delete a row whose path no longer exists).
            for original_note, original_path, original_sha, original_synced in reversed(
                row_snapshots
            ):
                try:
                    stat = (self._vault_dir / original_path).stat()
                    mtime_ns, size = stat.st_mtime_ns, stat.st_size
                except OSError:
                    mtime_ns, size = 0, 0
                try:
                    self._store.upsert_note(
                        original_note,
                        path=original_path,
                        body_sha256=original_sha,
                        path_mtime_ns=mtime_ns,
                        path_size=size,
                        synced=original_synced,
                    )
                except Exception:
                    _LOG.exception("Store rollback failed for note %s", original_note.id)

        try:
            source_updates: list[tuple[dict, str]] = []
            for source_id in source_ids:
                source_row = self._store.find_by_id(source_id)
                if source_row is None:
                    continue
                source_abs = self._vault_dir / source_row["path"]
                try:
                    raw = source_abs.read_text(encoding="utf-8")
                except OSError:
                    continue
                new_raw, count = rewrite_re.subn(_replacement, raw)
                if count == 0:
                    continue
                _save_backup(source_abs)
                _snapshot_row(source_row, raw)
                source_abs.write_text(new_raw, encoding="utf-8")
                source_updates.append((source_row, new_raw))

            target_abs = self._vault_dir / row["path"]
            _save_backup(target_abs)

            body_raw = target_abs.read_text(encoding="utf-8")
            _snapshot_row(row, body_raw)
            body_only = strip_frontmatter(body_raw)
            if patch.body is not None:
                body_only = patch.body
            if patch.add_tags or patch.remove_tags:
                body_only = _apply_tag_edits(
                    body_only,
                    add_tags=patch.add_tags,
                    remove_tags=patch.remove_tags,
                )

            try:
                current_fm = json.loads(row.get("frontmatter_json") or "{}")
                if not isinstance(current_fm, dict):
                    current_fm = {}
            except (TypeError, ValueError):
                current_fm = {}
            new_fm = dict(patch.frontmatter) if patch.frontmatter is not None else current_fm

            parsed_new = parse_knoten_filename(new_filename)
            parsed_body = parse_body(body_only)
            renamed_note = Note(
                id=note_id,
                filename=new_filename,
                title=patch.title or parsed_new.title or new_filename,
                family=row["family"],
                kind=row["kind"],
                source=parsed_new.source or row.get("source"),
                body=body_only,
                frontmatter=new_fm,
                tags=tuple(sorted(set(parsed_body.tags))),
                wikilinks=tuple(
                    WikiLink(target_title=title, target_id=None)
                    for title in parsed_body.wikilink_titles
                ),
                created_at=row.get("created_at") or _utcnow_iso(),
                updated_at=_utcnow_iso(),
                permissions=row.get("permissions") or "ALL",
            )
            new_relative = path_for_note(renamed_note)
            if new_relative != row["path"]:
                self._refuse_unindexed_destination(new_relative)
                created_paths.append(self._vault_dir / new_relative)
            ingest_note(
                renamed_note,
                store=self._store,
                vault_dir=self._vault_dir,
                previous_path=row["path"],
                synced=False,
            )

            affected_ids: list[str] = []
            for source_row, new_source_raw in source_updates:
                source_body = strip_frontmatter(new_source_raw)
                parsed_source_body = parse_body(source_body)
                try:
                    source_fm = json.loads(source_row.get("frontmatter_json") or "{}")
                    if not isinstance(source_fm, dict):
                        source_fm = {}
                except (TypeError, ValueError):
                    source_fm = {}
                source_note = Note(
                    id=source_row["id"],
                    filename=source_row["filename"],
                    title=source_row["title"],
                    family=source_row["family"],
                    kind=source_row["kind"],
                    source=source_row.get("source"),
                    body=source_body,
                    frontmatter=source_fm,
                    tags=tuple(sorted(set(parsed_source_body.tags))),
                    wikilinks=tuple(
                        WikiLink(target_title=title, target_id=None)
                        for title in parsed_source_body.wikilink_titles
                    ),
                    created_at=source_row.get("created_at") or _utcnow_iso(),
                    updated_at=_utcnow_iso(),
                    permissions=source_row.get("permissions") or "ALL",
                )
                ingest_note(source_note, store=self._store, vault_dir=self._vault_dir, synced=False)
                affected_ids.append(source_row["id"])

        except Exception:
            _rollback()
            raise

        return NoteUpdateResult(note_id=note_id, affected_notes=tuple(affected_ids))

    def append_to_note(self, note_id: str, content: str) -> None:
        self._refresh_index_if_stale()
        row = self._store.find_by_id(note_id)
        if row is None:
            raise NotFoundError(f"No note with id {note_id}")

        absolute = self._vault_dir / row["path"]
        try:
            current_raw = absolute.read_text(encoding="utf-8")
        except OSError as exc:
            raise NotFoundError(f"Mirror file missing for {note_id}: {exc}") from exc
        # Normalise the trailing newline before joining so repeated appends
        # insert exactly one blank-line separator (no newline accumulation).
        current_body = strip_frontmatter(current_raw).rstrip("\n")
        new_body = f"{current_body}\n\n{content}" if current_body else content

        self.update_note(note_id, NotePatch(body=new_body))

    def delete_note(self, note_id: str) -> None:
        self._refresh_index_if_stale()
        row = self._store.find_by_id(note_id)
        if row is None:
            raise NotFoundError(f"No note with id {note_id}")

        relative_path = row["path"]
        trash_relative, trash_abs = self._unique_trash_path(relative_path, note_id)
        source_abs = self._vault_dir / relative_path

        if not source_abs.exists():
            raise NotFoundError(f"Mirror file missing for {note_id}: {relative_path}")

        trash_abs.parent.mkdir(parents=True, exist_ok=True)
        source_abs.rename(trash_abs)

        try:
            moved = self._store.soft_delete_to_trash(
                note_id,
                trash_path=trash_relative,
                deleted_at=_utcnow_iso(),
            )
        except Exception:
            # Roll back the filesystem move if the SQL transaction failed —
            # otherwise the next reconcile sees a notes row whose mirror file
            # is gone (orphan), and the user is stuck unable to delete OR
            # restore until they hand-edit the index.
            try:
                trash_abs.rename(source_abs)
            except OSError:
                pass
            raise
        if not moved:
            trash_abs.rename(source_abs)
            raise NotFoundError(f"No note with id {note_id}")

    def _unique_trash_path(self, relative_path: str, note_id: str) -> tuple[str, Path]:
        """Derive a trash path that is unique per deletion.

        The note id goes into the name (before the extension) so two
        soft-deleted notes that ever shared a filename get distinct trash
        files — deriving from the vault path alone made re-deleting a
        same-filename note destroy the first note's only surviving body.
        An existing occupant is never unlinked; a numeric suffix is added
        until the path is free (e.g. a leftover from a crashed delete).
        """
        source_rel = Path(relative_path)
        candidate = source_rel.with_name(f"{source_rel.stem}.{note_id}{source_rel.suffix}")
        counter = 0
        while (self._vault_dir / ".trash" / candidate).exists():
            counter += 1
            candidate = source_rel.with_name(
                f"{source_rel.stem}.{note_id}-{counter}{source_rel.suffix}"
            )
        trash_relative = f".trash/{candidate}"
        return trash_relative, self._vault_dir / trash_relative

    def restore_note(self, note_id: str) -> None:
        self._refresh_index_if_stale()
        trashed = self._store.find_trashed(note_id)
        if trashed is None:
            raise NotFoundError(f"No trashed note with id {note_id}")

        original_path = trashed["original_path"]
        if self._store.find_by_filename(trashed["filename"]) is not None:
            raise UserError(
                f"Cannot restore {note_id}: a note with filename "
                f"{trashed['filename']!r} already exists. Rename one of them first."
            )

        trash_abs = self._vault_dir / trashed["trash_path"]
        restore_abs = self._vault_dir / original_path
        if not trash_abs.exists():
            raise NotFoundError(f"Trash file missing for {note_id}: {trashed['trash_path']}")
        if restore_abs.exists():
            raise UserError(f"Cannot restore {note_id}: {original_path!r} already exists on disk.")

        restore_abs.parent.mkdir(parents=True, exist_ok=True)
        trash_abs.rename(restore_abs)

        try:
            raw = restore_abs.read_text(encoding="utf-8")
        except OSError as exc:
            restore_abs.rename(trash_abs)
            raise NotFoundError(f"Cannot read restored file: {exc}") from exc

        body = strip_frontmatter(raw)
        parsed_body = parse_body(body)
        try:
            fm = json.loads(trashed.get("frontmatter_json") or "{}")
            if not isinstance(fm, dict):
                fm = {}
        except (TypeError, ValueError):
            fm = {}

        note = Note(
            id=note_id,
            filename=trashed["filename"],
            title=trashed["title"],
            family=trashed["family"],
            kind=trashed["kind"],
            source=trashed.get("source"),
            body=body,
            frontmatter=fm,
            tags=tuple(sorted(set(parsed_body.tags))),
            wikilinks=tuple(
                WikiLink(target_title=title, target_id=None)
                for title in parsed_body.wikilink_titles
            ),
            created_at=trashed["created_at"],
            updated_at=_utcnow_iso(),
            permissions=trashed.get("permissions") or "ALL",
        )
        ingest_note(note, store=self._store, vault_dir=self._vault_dir, synced=False)
        self._store.discard_trashed(note_id)

    def upload_attachment(
        self,
        path: Path,
        *,
        content_type: str | None = None,
        source: str | None = None,
    ) -> AttachmentUploadResult:
        self._refresh_index_if_stale()
        if not path.is_file():
            raise UserError(f"Not a file: {path}")

        suffix = path.suffix
        storage_key = f"{uuid.uuid4().hex}{suffix}"
        attachments_dir = self._vault_dir / ".attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        destination = attachments_dir / storage_key
        destination.write_bytes(path.read_bytes())

        size_bytes = destination.stat().st_size
        self._store.record_attachment(
            storage_key=storage_key,
            original_name=path.name,
            content_type=content_type,
            size_bytes=size_bytes,
            source=source,
            created_at=_utcnow_iso(),
        )
        return AttachmentUploadResult(
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=size_bytes,
            url=None,
        )

    def download_attachment(self, storage_key: str, destination: Path) -> AttachmentDownloadResult:
        self._refresh_index_if_stale()
        row = self._store.find_attachment(storage_key)
        if row is None:
            referencing = self._store.find_notes_referencing_attachment(storage_key)
            if referencing:
                example = referencing[0]["filename"]
                raise NotFoundError(
                    f"No local blob for storage_key {storage_key} — but file-family "
                    f"note {example!r} references it. The vault metadata was "
                    "synced from a remote backend, but this CLI is running in "
                    "local mode (KNOTEN_API_URL is empty), so the blob was never "
                    "streamed down. Set KNOTEN_API_URL and KNOTEN_API_TOKEN "
                    "(e.g. in ~/.config/knoten/.env) so knoten resolves to "
                    "remote mode and downloads attachments on demand."
                )
            raise NotFoundError(f"No attachment with storage_key {storage_key}")

        source_abs = self._vault_dir / ".attachments" / storage_key
        if not source_abs.exists():
            raise NotFoundError(f"Attachment blob missing on disk: {source_abs}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source_abs.read_bytes())
        return AttachmentDownloadResult(
            path=destination,
            bytes_written=destination.stat().st_size,
            content_type=row.get("content_type") or "",
            filename=row.get("original_name"),
        )


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _merge_tags(explicit: tuple[str, ...], parsed: tuple[str, ...]) -> tuple[str, ...]:
    """Combine caller-supplied tags with ones extracted from the body."""
    return tuple(sorted({*explicit, *parsed}))


def _apply_tag_edits(
    body: str,
    *,
    add_tags: tuple[str, ...],
    remove_tags: tuple[str, ...],
) -> str:
    """Port of the remote-path tag-edit routine used by `knoten edit --add-tag`."""
    import re

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
