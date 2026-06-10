"""Post-sync reconciliation: make sure the local mirror matches what the
store claims, and remove files the store does not know about.

Three independent checks, composed by `reconcile_local`:

1. **Missing files** — every row in `notes` has a `path`. If the file at that
   path is gone (user `rm`-ed it, OS hiccup, filesystem restore), re-fetch the
   note from the remote and re-ingest.
2. **Orphan files** — scan `vault/**/*.md` and delete any file whose relative
   path is not in the set of known paths from the `notes` table. Extends to
   `*.pdf`, `*.jpg`, `*.png` for file-family attachments.
3. **Body hash mismatch** (opt-in, `verify_hashes=True`) — re-hash every file
   and compare against the `body_sha256` recorded in the store. Mismatches are
   re-fetched. This is `O(N)` disk reads, so it is not run by default.

After reconciliation, every row in `notes` has a matching file on disk with
bytes identical to what the remote returned at the last ingest, and no file
under `vault/` is unknown to the store.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from knoten.repositories.backend import Backend
from knoten.repositories.errors import NoteForbiddenError, NotFoundError
from knoten.repositories.store import Store, StoreNoteRow
from knoten.repositories.vault_files import strip_frontmatter
from knoten.services.notes import (
    delete_ingested,
    ingest_note,
    ingest_placeholder,
    summary_from_row,
)
from knoten.settings import Settings

ProgressCallback = Callable[[str], None]

_BINARY_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}


def _noop(_: str) -> None:
    pass


@dataclass
class ReconcileResult:
    missing_refetched: int = 0
    mismatched_refetched: int = 0
    orphans_removed: int = 0
    verified_hashes: bool = False
    checked: int = 0
    missing_ids: list[str] = field(default_factory=list)
    mismatched_ids: list[str] = field(default_factory=list)
    orphan_paths: list[str] = field(default_factory=list)


def reconcile_local(
    *,
    backend: Backend,
    store: Store,
    settings: Settings,
    verify_hashes: bool = False,
    progress: ProgressCallback | None = None,
) -> ReconcileResult:
    """Run all three reconciliation checks and return a report.

    Performs network calls only for notes that need re-fetching. File system
    scans stay within `settings.paths.vault_dir`. `progress` receives one-line
    status updates during each phase.
    """
    log = progress or _noop
    result = ReconcileResult(verified_hashes=verify_hashes)

    rows = store.all_rows()
    result.checked = len(rows)

    # --- 1. Missing files ---------------------------------------------------
    missing: list[StoreNoteRow] = []
    existing: list[StoreNoteRow] = []
    for row in rows:
        if (settings.paths.vault_dir / row.path).exists():
            existing.append(row)
        else:
            missing.append(row)
    result.missing_ids = [row.id for row in missing]
    if missing:
        log(f"  {len(missing)} file(s) missing on disk, will re-fetch")
        for row in missing[:5]:
            log(f"    ↓ re-fetching '{row.filename}'")
        if len(missing) > 5:
            log(f"    … and {len(missing) - 5} more")

    # --- 2. Hash verification (opt-in) --------------------------------------
    mismatched: list[StoreNoteRow] = []
    if verify_hashes:
        # Placeholders for restricted notes have no body on disk to check —
        # skip them. Their file is the marker we wrote at ingest time.
        verifiable = [row for row in existing if not row.restricted]
        log(f"  hashing {len(verifiable)} file(s) to check body drift")
        for row in verifiable:
            absolute = settings.paths.vault_dir / row.path
            try:
                text = absolute.read_text(encoding="utf-8")
            except OSError:
                # File disappeared between the existence check and now.
                missing.append(row)
                result.missing_ids.append(row.id)
                continue
            body = strip_frontmatter(text)
            disk_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if disk_sha != row.body_sha256:
                mismatched.append(row)
                log(f"    ≠ '{row.filename}' hash drifted, will re-fetch")
        result.mismatched_ids = [row.id for row in mismatched]
        if not mismatched:
            log("  all hashes match recorded body_sha256")

    # --- Re-fetch missing + mismatched --------------------------------------
    for row in missing:
        _refetch(row, backend=backend, store=store, settings=settings)
        result.missing_refetched += 1
    for row in mismatched:
        _refetch(row, backend=backend, store=store, settings=settings)
        result.mismatched_refetched += 1

    # --- 3. Orphan cleanup --------------------------------------------------
    # Rebuild known_paths after re-fetch in case any note's path changed.
    known_paths = {row.path for row in store.all_rows()}
    orphans = _find_orphans(settings.paths.vault_dir, known_paths)
    if orphans:
        log(f"  {len(orphans)} orphan file(s) to remove")
        for orphan in orphans[:5]:
            relative = orphan.relative_to(settings.paths.vault_dir)
            log(f"    ✗ removing '{relative}'")
        if len(orphans) > 5:
            log(f"    … and {len(orphans) - 5} more")
    for orphan in orphans:
        orphan.unlink()
        _prune_empty_parents(orphan.parent, settings.paths.vault_dir)
    result.orphans_removed = len(orphans)
    result.orphan_paths = [str(o.relative_to(settings.paths.vault_dir)) for o in orphans]

    return result


def _refetch(
    row: StoreNoteRow,
    *,
    backend: Backend,
    store: Store,
    settings: Settings,
) -> None:
    """Pull a fresh copy of a single note and re-ingest it locally.

    If the note is restricted (server returns 404 because the token has
    LIST but not READ), recreate the placeholder using the fields from the
    store's existing row. A row that is *already* a placeholder is rebuilt
    as a placeholder without asking the backend — a LocalBackend read
    would happily return the marker body as a "real" note and clear the
    `restricted` flag.

    The row's current `synced` value is preserved through the re-ingest:
    a `synced=0` row is a pending local write, and flipping it to 1 here
    would drop it from the push queue and arm the next sync's delete
    detection against it.
    """
    if row.restricted:
        _reingest_placeholder_from_row(row, store=store, settings=settings)
        return
    try:
        note = backend.read_note(row.id)
    except NoteForbiddenError:
        _reingest_placeholder_from_row(row, store=store, settings=settings)
        return
    except NotFoundError:
        # The remote no longer has this note (deleted server-side, or this
        # row is left over from a partial local delete). Drop the local row
        # + file so the next sync starts clean instead of crashing the whole
        # reconcile pass over a single phantom id.
        delete_ingested(store, settings.paths.vault_dir, row.id)
        return
    current = store.find_by_id(note.id)
    synced = bool(int(current.get("synced", 1))) if current is not None else True
    previous = store.get_row(note.id)
    ingest_note(
        note,
        store=store,
        vault_dir=settings.paths.vault_dir,
        previous_path=previous.path if previous else None,
        synced=synced,
    )


def _reingest_placeholder_from_row(
    row: StoreNoteRow,
    *,
    store: Store,
    settings: Settings,
) -> None:
    """Rebuild a restricted note's placeholder file from its stored metadata."""
    current = store.find_by_id(row.id)
    if current is None:
        return
    ingest_placeholder(
        summary_from_row(current),
        store=store,
        vault_dir=settings.paths.vault_dir,
        previous_path=row.path,
    )


def _find_orphans(vault_dir: Path, known_paths: set[str]) -> list[Path]:
    """Walk vault/ and return paths the store has no row for.

    Includes markdown files and common attachment extensions. Any path with
    a dot-prefixed component is skipped — dot-files (`.DS_Store`,
    atomic-write `*.tmp`) and, crucially, dot-directories: `.trash/` holds
    soft-deleted notes and `.attachments/` holds uploaded blobs, neither of
    which has a row in `notes`, so without this guard the orphan sweep
    would irreversibly delete them.
    """
    orphans: list[Path] = []
    if not vault_dir.exists():
        return orphans
    for path in vault_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(vault_dir)
        if any(part.startswith(".") for part in relative.parts):
            continue
        suffix = path.suffix.lower()
        if suffix != ".md" and suffix not in _BINARY_EXTENSIONS:
            continue
        if str(relative) not in known_paths:
            orphans.append(path)
    return orphans


def _prune_empty_parents(start: Path, root: Path) -> None:
    """Best-effort removal of now-empty directories up to (but not including) root."""
    current = start
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
