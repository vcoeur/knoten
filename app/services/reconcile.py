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

from app.repositories.errors import NoteForbiddenError
from app.repositories.remote_backend import RemoteBackend
from app.repositories.store import Store, StoreNoteRow
from app.services.notes import ingest_note, ingest_placeholder
from app.settings import Settings

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
    backend: RemoteBackend,
    store: Store,
    settings: Settings,
    verify_hashes: bool = False,
    progress: ProgressCallback | None = None,
) -> ReconcileResult:
    """Run all three reconciliation checks and return a report.

    Performs network calls only for notes that need re-fetching. File system
    scans stay within `settings.vault_dir`. `progress` receives one-line
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
        if (settings.vault_dir / row.path).exists():
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
            absolute = settings.vault_dir / row.path
            try:
                text = absolute.read_text(encoding="utf-8")
            except OSError:
                # File disappeared between the existence check and now.
                missing.append(row)
                result.missing_ids.append(row.id)
                continue
            body = _strip_frontmatter(text)
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
    orphans = _find_orphans(settings.vault_dir, known_paths)
    if orphans:
        log(f"  {len(orphans)} orphan file(s) to remove")
        for orphan in orphans[:5]:
            relative = orphan.relative_to(settings.vault_dir)
            log(f"    ✗ removing '{relative}'")
        if len(orphans) > 5:
            log(f"    … and {len(orphans) - 5} more")
    for orphan in orphans:
        orphan.unlink()
        _prune_empty_parents(orphan.parent, settings.vault_dir)
    result.orphans_removed = len(orphans)
    result.orphan_paths = [str(o.relative_to(settings.vault_dir)) for o in orphans]

    return result


def _refetch(
    row: StoreNoteRow,
    *,
    backend: RemoteBackend,
    store: Store,
    settings: Settings,
) -> None:
    """Pull a fresh copy of a single note and re-ingest it locally.

    If the note is restricted (server returns 404 because the token has
    LIST but not READ), recreate the placeholder using the fields from the
    store's existing row.
    """
    try:
        note = backend.read_note(row.id)
    except NoteForbiddenError:
        from app.models import NoteSummary

        current = store.find_by_id(row.id)
        if current is None:
            return
        summary = NoteSummary(
            id=current["id"],
            filename=current["filename"],
            title=current["title"],
            family=current["family"],
            kind=current["kind"],
            source=current["source"],
            tags=(),
            created_at=current["created_at"],
            updated_at=current["updated_at"],
        )
        ingest_placeholder(
            summary,
            store=store,
            vault_dir=settings.vault_dir,
            previous_path=row.path,
        )
        return
    previous = store.get_row(note.id)
    ingest_note(
        note,
        store=store,
        vault_dir=settings.vault_dir,
        previous_path=previous.path if previous else None,
    )


def _find_orphans(vault_dir: Path, known_paths: set[str]) -> list[Path]:
    """Walk vault/ and return paths the store has no row for.

    Includes markdown files and common attachment extensions. Dotfiles are
    skipped (e.g. `.DS_Store`, atomic-write `*.tmp`).
    """
    orphans: list[Path] = []
    if not vault_dir.exists():
        return orphans
    for path in vault_dir.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("."):
            continue
        suffix = path.suffix.lower()
        if suffix != ".md" and suffix not in _BINARY_EXTENSIONS:
            continue
        relative = str(path.relative_to(vault_dir))
        if relative not in known_paths:
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


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block, if any — same rule as ingest."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + 5 :]
