"""Sync orchestration — incremental and full modes.

Incremental: page through `GET /api/notes` (sorted desc by updatedAt) until
we hit items older than our local cursor, then fetch bodies — in chunks of
50 via `POST /api/notes/batch-read`, falling back to per-note
`GET /api/notes/{id}` on servers that predate the batch route — and upsert
each new/changed note. Delete detection runs when the remote `total`
disagrees with the local count, or when explicitly requested.

Full: same algorithm but with an empty cursor and forced delete detection —
effectively "refetch everything, reconcile both sides". The `/api/export`
endpoint is not used because the export zip is filename-keyed and carries
no UUIDs, so we'd still need the per-note read path to get the canonical IDs.
A dedicated `knoten export` command can still use /api/export when someone
wants an offline archive; that is out of scope for v1.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from knoten.models import Note, NoteSummary
from knoten.repositories.backend import Backend, NoteDraft, NotePatch
from knoten.repositories.errors import (
    BatchReadUnsupportedError,
    FrontmatterValidationError,
    KnotenError,
    NoteForbiddenError,
    NotFoundError,
    RemoteRejectionError,
    ValidationError,
)
from knoten.repositories.store import Store
from knoten.repositories.sync_state import load_state, save_state
from knoten.repositories.vault_files import strip_frontmatter
from knoten.services.notes import (
    delete_ingested,
    ingest_note,
    ingest_placeholder,
)
from knoten.services.reconcile import reconcile_local
from knoten.settings import Settings

ProgressCallback = Callable[[str], None]

# Ids per `POST /api/notes/batch-read` request in the pull pass — half the
# server's 100-id cap, leaving headroom against future cap changes.
BATCH_READ_CHUNK_SIZE = 50


def _noop(_: str) -> None:
    pass


def iter_all_summaries(
    backend: Backend,
    *,
    page_size: int = 200,
    stop_when_older_than: str | None = None,
    page_totals: list[int] | None = None,
) -> Iterator[NoteSummary]:
    """Yield every active note summary, newest-first.

    Caller-side glue over `Backend.list_note_summaries`. Kept out of the
    backend protocol because pagination belongs in the service layer — the
    protocol only needs "give me one page".

    If `stop_when_older_than` is set (ISO-8601), pagination stops once a
    page's newest item has `updated_at <= stop_when_older_than` — the
    caller is using this for incremental sync and no longer cares about
    older items.

    If `page_totals` is provided, the `total` reported by each fetched page is
    appended to it (in walk order). Lets the caller compare the scanned-ID
    count against the *same walk's* own total — a same-scan consistency check
    that a stale total captured in an earlier pass cannot provide.
    """
    offset = 0
    while True:
        page = backend.list_note_summaries(limit=page_size, offset=offset)
        if page_totals is not None:
            page_totals.append(int(page.total or 0))
        if not page.data:
            return
        yield from page.data
        if stop_when_older_than is not None and page.data[-1].updated_at <= stop_when_older_than:
            return
        if len(page.data) < page_size:
            return
        offset += page_size


def _same_scan_consistent(scanned_ids: int, page_totals: list[int]) -> bool:
    """Return True when a reconcile walk's scanned-ID count matches its own total.

    Same-scan consistency requires (a) the walk reported at least one page
    total, (b) that total never shifted across pages (a mid-walk shift signals
    a concurrent create + pagination reorder), and (c) the number of distinct
    ids yielded equals that stable total. Any violation means the scan cannot
    be trusted to drive deletions for this run.
    """
    if not page_totals:
        return False
    distinct_totals = set(page_totals)
    if len(distinct_totals) != 1:
        return False
    return scanned_ids == page_totals[-1]


@dataclass
class SyncResult:
    mode: str
    fetched: int
    deleted: int
    # `remote_total` is the `total` field from the first list response —
    # what the server claims the count is. `scanned_remote_ids` is what
    # `iter_all_summaries` actually yielded. After the 2026-04-12 fix to
    # `notes.vcoeur.com` (incident `2026-04-12-notes-list-permission-leaks`)
    # these two values should always agree. Keep both on the result as a
    # diagnostic tripwire: any future disagreement points at a regression —
    # a stable-sort regression in the list endpoint, an asymmetric filter
    # between the count query and the data query, or a server-side schema
    # change that knoten's pagination does not yet understand.
    remote_total: int | None
    scanned_remote_ids: int
    local_total: int
    last_sync_at: str
    elapsed_seconds: float
    # Placeholders for notes the token is not allowed to READ (GET /api/notes/{id}
    # returns 404 because the server conflates "forbidden" with "not found").
    restricted_placeholders: int = 0
    # Reconciliation (always runs at the end of sync).
    missing_refetched: int = 0
    mismatched_refetched: int = 0
    orphans_removed: int = 0
    verified_hashes: bool = False
    # Bidirectional sync — push pass results.
    pushed_creates: int = 0
    pushed_edits: int = 0
    push_failed: int = 0
    push_preserved_local_only: int = 0
    # Local writes permanently rejected by the remote (bad filename, duplicate,
    # frontmatter type mismatch) — each `{"id", "reason"}`. These rows are
    # skipped by the push pass until a local edit clears the marker; surfaced
    # here so the CLI / JSON consumer can list what is stuck and why.
    push_rejected: list[dict] = field(default_factory=list)
    # Local soft-deletes of remote-known notes propagated to the remote.
    pushed_deletes: int = 0
    # Pull-pass conflicts — each entry is `{"id", "filename", "reason"}`.
    # `local_unsynced_edit`: the pull would have overwritten a `synced=0`
    # row, so the local version was kept. `filename_collision`: a remote
    # note's filename collides with a different local row.
    conflicts: list[dict] = field(default_factory=list)
    # Human-readable warnings (delete-phase skips, invalid filenames, …) —
    # surfaced in the JSON payload, not just the progress stream.
    warnings: list[str] = field(default_factory=list)
    # Remote notes skipped because a server-provided value failed validation
    # at the ingest boundary — a hostile filename (path separators / NUL /
    # empty) or a frontmatter value containing a control character. Each
    # skip also appends a `warnings` entry naming the note.
    skipped_invalid: int = 0


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PushOutcome:
    creates: int = 0
    edits: int = 0
    failed: int = 0
    preserved_local_only: int = 0
    pushed_ids: set[str] = field(default_factory=set)
    # Rows permanently rejected by the remote (now or on a prior run) — each is
    # `{"id", "reason"}`. Surfaced in SyncResult.push_rejected and skipped by
    # subsequent push passes until a local edit clears the marker.
    rejected: list[dict] = field(default_factory=list)
    # Post-push refetches whose server copy failed frontmatter validation —
    # the push succeeded but the mirror was not updated. Rolled up into
    # SyncResult.skipped_invalid.
    skipped_invalid: int = 0


def push_local_writes(
    *,
    backend: Backend,
    store: Store,
    settings: Settings,
    remote_ids_seen: set[str],
    warnings: list[str],
    progress: ProgressCallback | None = None,
) -> PushOutcome:
    """Push local-only changes (notes with `synced=0`) to the remote.

    Drains rows where `synced=0` — notes created or edited locally while
    `KNOTEN_API_URL` was empty, or by `LocalBackend` writes that never had a
    remote round-trip. For each:

    - If the row's `id` is in `remote_ids_seen`, the note exists on the
      server — push as an edit (`PUT /api/notes/{id}`).
    - Otherwise the server doesn't know about it yet — `POST /api/notes`. The
      server returns its own UUID; if it differs from the local one we cascade
      the swap through `Store.reid_note` so the local mirror's tags / wikilinks
      / FTS rows align with the canonical server-side ID.

    After a successful push the note is re-fetched (`read_note`) and re-ingested
    with `synced=1`, so the mirror immediately carries the server's
    normalisation (inserted filename prefix, injected frontmatter, derived
    title/source, new `updatedAt`) instead of lagging a sync. The refetch does
    NOT advance the pull cursor (`max_seen` in `incremental_sync`): the server's
    post-push `updatedAt` is newer than this run's cursor, so leaving the cursor
    where the pull pass left it means the next incremental sync re-ingests this
    one note via the inclusive `>=` boundary (idempotent — see the cursor note
    in `incremental_sync`) while skipping nothing. Advancing the cursor to the
    post-push timestamp would be unsafe: a concurrent server-side edit to a
    *different* note with an `updatedAt` between the old cursor and the push
    time would then be skipped, because reconcile only catches up never-seen
    ids, not already-local ones.

    Permanent rejections (`RemoteRejectionError` / `ValidationError`: bad
    filename, duplicate, frontmatter type mismatch) record a marker on the row
    and are surfaced in `outcome.rejected` so the row is skipped on subsequent
    runs instead of retried forever. Transient failures (network, 5xx, 429)
    leave `synced=0` with no marker so the next sync retries. A row already
    carrying a rejection marker is skipped up-front. Notes never get silently
    dropped.
    """
    log: ProgressCallback = progress or _noop
    outcome = PushOutcome()
    pending = store.unsynced_note_ids()
    if not pending:
        return outcome

    log(f"→ Pushing {len(pending)} local-only change(s) to remote")
    for note_id in pending:
        row = store.find_by_id(note_id)
        if row is None:
            continue
        filename = row["filename"]
        # Skip rows already flagged as permanently rejected — retrying them
        # every sync is exactly the loop this marker exists to break. Surface
        # them so the user knows they are stuck and how to clear it.
        if row.get("push_rejected_at"):
            reason = row.get("push_reject_reason") or "previously rejected by the remote"
            outcome.rejected.append({"id": note_id, "reason": reason})
            log(
                f"  ⏭ '{filename}' was permanently rejected ({reason}) — skipping. "
                "Edit the note to retry."
            )
            continue
        previous_path = row["path"]
        absolute_path = settings.paths.vault_dir / previous_path
        try:
            raw = absolute_path.read_text(encoding="utf-8")
        except OSError as exc:
            log(f"  ✗ '{filename}': mirror file unreadable ({exc}) — preserving locally")
            outcome.failed += 1
            outcome.preserved_local_only += 1
            continue
        body = strip_frontmatter(raw)
        try:
            frontmatter = json.loads(row.get("frontmatter_json") or "{}")
            if not isinstance(frontmatter, dict):
                frontmatter = {}
        except (TypeError, ValueError):
            frontmatter = {}
        tags = list(store.tags_for_note(note_id))

        try:
            if note_id in remote_ids_seen:
                # Edit existing note on the remote.
                backend.update_note(
                    note_id,
                    NotePatch(
                        body=body,
                        frontmatter=frontmatter,
                    ),
                )
                target_id = note_id
                outcome.edits += 1
                log(f"  ↑ edited '{filename}'")
            else:
                # Create on the remote — server may assign a new UUID.
                draft = NoteDraft(
                    filename=filename,
                    body=body,
                    kind=row.get("kind"),
                    frontmatter=frontmatter,
                    tags=tuple(tags),
                )
                new_id = backend.create_note(draft)
                if new_id != note_id:
                    store.reid_note(note_id, new_id)
                target_id = new_id
                outcome.creates += 1
                log(f"  ↑ created '{filename}' ({note_id} -> {new_id})")
        except (RemoteRejectionError, ValidationError) as exc:
            # Permanent, user-actionable rejection — mark the row so we stop
            # retrying it, and surface it. No reid happened (the create/edit
            # failed), so `note_id` is still the local row's id.
            reason = str(exc)
            store.record_push_rejection(note_id, reason=reason, at=_utcnow_iso())
            outcome.failed += 1
            outcome.preserved_local_only += 1
            outcome.rejected.append({"id": note_id, "reason": reason})
            log(
                f"  ✗ '{filename}': permanently rejected ({exc}) — marked; "
                "edit the note to clear the marker and retry."
            )
            continue
        except KnotenError as exc:
            log(f"  ✗ '{filename}': push failed ({exc}) — preserving locally")
            outcome.failed += 1
            outcome.preserved_local_only += 1
            continue
        except Exception as exc:  # noqa: BLE001 — sync should be resilient
            log(f"  ✗ '{filename}': unexpected push error ({exc}) — preserving locally")
            outcome.failed += 1
            outcome.preserved_local_only += 1
            continue

        # Push succeeded. Re-fetch the server-normalised note and ingest it
        # (synced=1) so the mirror carries the server's filename / frontmatter
        # / updatedAt immediately. A refetch failure must never fail the push
        # pass — fall back to the bare `mark_synced` the push used to do.
        outcome.pushed_ids.add(target_id)
        try:
            fresh = backend.read_note(target_id)
            ingest_note(
                fresh,
                store=store,
                vault_dir=settings.paths.vault_dir,
                previous_path=previous_path,
                synced=True,
            )
        except NoteForbiddenError:
            store.mark_synced(target_id)
            warnings.append(
                f"pushed '{filename}' but the token cannot re-read it (forbidden) — "
                "kept it marked synced; the mirror may lag the server by one sync"
            )
            log(f"  ⚠ '{filename}' pushed but not re-readable (forbidden) — mirror may lag")
        except FrontmatterValidationError as exc:
            # The push succeeded but the server's copy now carries a
            # frontmatter value the mirror writer refuses (control character).
            # Same treatment as any other unmirrorable server payload: warn,
            # count it, keep the run going. mark_synced so the row is not
            # re-pushed forever.
            store.mark_synced(target_id)
            outcome.skipped_invalid += 1
            warnings.append(
                f"pushed '{filename}' but its server copy could not be mirrored — "
                f"note {target_id} ('{filename}'): frontmatter value for {exc.key!r} "
                "contains a control character; frontmatter values must be "
                "single-line. Fix the value on the server, then re-sync."
            )
            log(
                f"  ⚠ '{filename}' pushed but its frontmatter value for {exc.key!r} "
                "failed validation — mirror not updated"
            )
        except Exception as exc:  # noqa: BLE001 — refetch is best-effort
            store.mark_synced(target_id)
            warnings.append(
                f"pushed '{filename}' but could not re-fetch it ({exc}) — kept it "
                "marked synced; the mirror catches up on the next sync"
            )
            log(f"  ⚠ '{filename}' pushed but refetch failed ({exc}) — mirror catches up next sync")
    return outcome


def _push_pending_deletes(
    *,
    backend: Backend,
    store: Store,
    warnings: list[str],
    progress: ProgressCallback | None = None,
) -> tuple[int, set[str]]:
    """Propagate local soft-deletes of remote-known notes to the remote.

    Returns `(deletes_issued, still_pending_ids)`. Runs before the pull
    scan so a deleted note is gone from the remote before the catch-up
    pass could re-ingest ("resurrect") it. `NotFoundError` means the note
    is already gone on the remote — the marker is cleared without counting
    a delete. Any other failure keeps the marker so the next sync retries,
    and the id stays excluded from this run's ingest passes.
    """
    log = progress or _noop
    issued = 0
    still_pending: set[str] = set()
    pending = store.pending_remote_delete_rows()
    if pending:
        log(f"→ Propagating {len(pending)} local delete(s) to remote")
    for row in pending:
        note_id = row["id"]
        filename = row["filename"]
        try:
            backend.delete_note(note_id)
        except NotFoundError:
            store.clear_pending_remote_delete(note_id)
            log(f"  ✓ '{filename}' already deleted on remote")
        except Exception as exc:  # noqa: BLE001 — sync should be resilient
            still_pending.add(note_id)
            warnings.append(
                f"could not propagate delete of '{filename}' ({exc}) — will retry next sync"
            )
            log(f"  ✗ '{filename}': remote delete failed ({exc}) — will retry next sync")
        else:
            store.clear_pending_remote_delete(note_id)
            issued += 1
            log(f"  ✗ deleted '{filename}' on remote")
    return issued, still_pending


def incremental_sync(
    *,
    backend: Backend,
    store: Store,
    settings: Settings,
    cursor_override: str | None = None,
    verify_hashes: bool = False,
    force_delete: bool = False,
    progress: ProgressCallback | None = None,
) -> SyncResult:
    """Run an incremental sync. Returns a SyncResult with counts.

    When `cursor_override` is an empty string, every note is refetched —
    used by `full_sync` below.

    `force_delete=True` overrides the mass-delete circuit breaker — the
    guard that refuses to delete more than 20% of the local synced rows
    (and more than 5 notes) in one run.

    Post-pagination, this function *always* runs delete detection (cheap:
    one paginated scan of IDs) and a reconciliation pass (checks that every
    local file the store knows about still exists, cleans up orphans). With
    `verify_hashes=True`, the reconciliation pass also re-hashes every file
    on disk and re-fetches any whose content has drifted from the store's
    recorded body hash.

    The contract: after this function returns, every active note in the
    store has a matching file on disk with the current remote content, and
    no file under `vault/` is unknown to the store.

    `progress` receives one-line human-readable status updates for each
    phase of the sync (pagination, per-note fetches, delete detection,
    reconciliation). Pass `None` for silent operation — JSON mode uses that.
    """
    log: ProgressCallback = progress or _noop
    started = time.monotonic()
    state = load_state(settings.paths.state_file)
    cursor = (
        cursor_override if cursor_override is not None else (state.last_sync_max_updated_at or "")
    )

    log(f"→ Syncing from {settings.api_url}")
    if cursor:
        log(f"  cursor: notes updated after {cursor}")
    else:
        log("  cursor: empty (will re-fetch every note)")

    fetched = 0
    restricted_placeholders = 0
    skipped_invalid = 0
    # Ids skipped this run because a server value failed ingest validation —
    # the catch-up pass must not re-fetch (and re-count) them.
    skipped_invalid_ids: set[str] = set()
    conflicts: list[dict] = []
    warnings: list[str] = []
    max_seen = cursor
    remote_total: int | None = None
    local_ids_before = store.all_ids()

    # Push pass (deletes) — before any pull/scan, so a note soft-deleted
    # locally is gone from the remote before the catch-up pass below could
    # re-ingest ("resurrect") it.
    pushed_deletes, pending_delete_ids = _push_pending_deletes(
        backend=backend,
        store=store,
        warnings=warnings,
        progress=log,
    )

    offset = 0
    page_size = 100
    page_num = 0
    # Summaries needing a body fetch, in walk order. Bodies are fetched
    # after pagination completes, in batch-read chunks (`_pull_note_bodies`)
    # — one request per BATCH_READ_CHUNK_SIZE notes instead of one per note.
    pending_items: list[NoteSummary] = []
    pending_item_ids: set[str] = set()
    while True:
        page_num += 1
        page = backend.list_note_summaries(limit=page_size, offset=offset)
        remote_total = page.total if page.total else remote_total
        items = page.data
        if not items:
            break

        new_on_page = sum(1 for item in items if item.updated_at >= cursor)
        log(
            f"  page {page_num}: {len(items)} items, {new_on_page} newer than cursor"
            + (f" (remote total {remote_total})" if remote_total is not None else "")
        )

        page_has_stale = False
        for item in items:
            item_id = item.id
            updated = item.updated_at
            # `>=` rather than `>`: timestamps are second-precision, so an
            # edit landing in the same second as the recorded cursor would
            # otherwise be skipped forever. Re-ingesting the boundary item
            # is idempotent — one redundant fetch per sync is the cost.
            if updated >= cursor:
                if item_id in pending_delete_ids:
                    continue  # delete push failed — do not resurrect it here
                if item_id not in pending_item_ids:
                    pending_item_ids.add(item_id)
                    pending_items.append(item)
                if updated > max_seen:
                    max_seen = updated
            else:
                page_has_stale = True

        if page_has_stale:
            break
        if len(items) < page_size:
            break
        offset += page_size

    if pending_items:
        log(f"  fetching {len(pending_items)} note bodies")
        fetched_count, restricted_count, invalid_count = _pull_note_bodies(
            pending_items,
            backend=backend,
            store=store,
            settings=settings,
            log=log,
            conflicts=conflicts,
            warnings=warnings,
            skipped_invalid_ids=skipped_invalid_ids,
        )
        fetched += fetched_count
        restricted_placeholders += restricted_count
        skipped_invalid += invalid_count

    # Reconcile the local ID set against the remote ID set. This single pass
    # handles BOTH previously-separate concerns:
    #
    #   (a) Delete detection — any local ID not in the remote is gone on the
    #       server (trashed or hard-deleted). Remove it.
    #   (b) Drift catch-up — any remote ID not in the local store is a note
    #       we have never ingested. This happens when a previous sync was
    #       aborted mid-flight, and also for `permissions = LIST` notes
    #       that live in the vault but return 404 on `GET /api/notes/{id}`
    #       (they come through here as placeholders via `_fetch_or_placeholder`).
    #
    # Merging the two into one iter_all_summaries call halves the HTTP cost
    # relative to running them separately.
    log("→ Reconciling remote ID set (delete detection + drift catch-up)")
    local_ids_after_main = store.all_ids()
    deleted = 0
    remote_ids_seen: set[str] = set()
    catch_up_count = 0
    catch_up_restricted = 0
    already_local = 0
    catch_up_started = False
    # Per-page totals from THIS walk — the basis for the same-scan tripwire.
    reconcile_page_totals: list[int] = []
    for item in iter_all_summaries(backend, page_size=200, page_totals=reconcile_page_totals):
        item_id = item.id
        remote_ids_seen.add(item_id)
        if item_id in local_ids_after_main:
            already_local += 1
            continue
        if item_id in pending_delete_ids:
            continue  # locally trashed, delete push failed — do not resurrect
        if item_id in skipped_invalid_ids:
            continue  # already skipped this run — do not re-fetch or re-count
        if not catch_up_started:
            log("  catching up on never-seen-locally notes")
            catch_up_started = True
        filename = item.filename or item_id
        log(f"    ↓ fetching '{filename}' (never seen locally)")
        fetched_count, restricted_count, invalid_count = _fetch_or_placeholder(
            item,
            backend=backend,
            store=store,
            settings=settings,
            log=log,
            conflicts=conflicts,
            warnings=warnings,
            skipped_invalid_ids=skipped_invalid_ids,
        )
        fetched += fetched_count
        catch_up_count += fetched_count
        restricted_placeholders += restricted_count
        catch_up_restricted += restricted_count
        skipped_invalid += invalid_count
        # Now it is local — avoid double-fetching if iter_all_summaries
        # returns the same id twice (shouldn't happen, defensive).
        local_ids_after_main.add(item_id)

    scanned_remote_ids = len(remote_ids_seen)
    log(
        f"  scanned {scanned_remote_ids} remote id(s), "
        f"already local={already_local}, "
        f"fetched this pass={catch_up_count}, "
        f"restricted placeholders={catch_up_restricted}"
    )
    delete_phase_blocked = False
    # PRIMARY tripwire — same-scan consistency. Compare the reconcile walk's
    # scanned-ID count against the walk's OWN per-page total. If a concurrent
    # create lands mid-walk (the total bumps between pages) or the walk simply
    # missed a row the server still counts, the numbers disagree and the scan
    # cannot be trusted — skip delete detection. This closes the gap where the
    # old cross-scan check (an EARLIER pass's total vs THIS walk's count) could
    # coincidentally agree even though the walk dropped a live row, deleting a
    # mirror row that still exists on the remote.
    reconcile_totals_seen = set(reconcile_page_totals)
    reconcile_walk_total = reconcile_page_totals[-1] if reconcile_page_totals else None
    same_scan_consistent = _same_scan_consistent(scanned_remote_ids, reconcile_page_totals)
    if reconcile_walk_total is not None and not same_scan_consistent:
        detail = (
            f"total shifted mid-walk {sorted(reconcile_totals_seen)}"
            if len(reconcile_totals_seen) > 1
            else f"walk total {reconcile_walk_total}"
        )
        log(
            f"  ⚠ reconcile walk scanned {scanned_remote_ids} id(s) but its own "
            f"{detail} — same-scan inconsistency. Skipping delete detection this run."
        )
        warnings.append(
            f"reconcile walk scanned {scanned_remote_ids} id(s) but its own "
            f"{detail} — delete detection skipped this run"
        )
        delete_phase_blocked = True

    # SECONDARY signal (warn only) — cross-scan. The cursor-pull pass's `total`
    # vs this walk's scanned count. A benign concurrent create between the two
    # passes makes these differ without the walk itself being inconsistent, so
    # this is a diagnostic, not a reason to skip deletes on its own.
    if remote_total is not None and scanned_remote_ids != remote_total:
        warnings.append(
            f"cursor-pull total ({remote_total}) differs from reconcile scan "
            f"({scanned_remote_ids}) — informational; the same-scan check governs deletes"
        )
        log(
            f"  ⚠ cursor-pull total ({remote_total}) differs from reconcile scan "
            f"({scanned_remote_ids}) — informational only"
        )

    # Push pass — drain locally-authored writes (synced=0) before reconciling.
    # Must run *before* delete detection: a synced=0 note that is "local but
    # not on the remote" is a local create awaiting upload, not a remote
    # deletion. Pushing first turns it into either (a) a synced=1 row whose
    # id is now in `remote_ids_seen` (after re-iding) or (b) a still-synced=0
    # row that the delete branch then preserves.
    push_outcome = push_local_writes(
        backend=backend,
        store=store,
        settings=settings,
        remote_ids_seen=remote_ids_seen,
        warnings=warnings,
        progress=log,
    )
    remote_ids_seen.update(push_outcome.pushed_ids)
    skipped_invalid += push_outcome.skipped_invalid
    if push_outcome.creates or push_outcome.edits:
        log(
            f"  pushed {push_outcome.creates} create(s), {push_outcome.edits} edit(s)"
            + (f", {push_outcome.failed} failed" if push_outcome.failed else "")
        )
    elif push_outcome.failed:
        log(f"  ⚠ {push_outcome.failed} push(es) failed — preserved locally for retry")
    if push_outcome.rejected:
        warnings.append(
            f"{len(push_outcome.rejected)} local write(s) permanently rejected by the "
            "remote and skipped — edit each note (or delete it) to clear the marker and "
            "retry: " + ", ".join(sorted({entry["id"] for entry in push_outcome.rejected}))
        )
        log(
            f"  ⚠ {len(push_outcome.rejected)} local write(s) permanently rejected — "
            "skipped; edit the note(s) to retry"
        )

    # Delete detection — local IDs (pre-sync) absent from the remote set.
    # Notes ingested during this run are implicitly in the remote set, so
    # they cannot be flagged for deletion. Rows that are still `synced=0`
    # after the push pass are local-only writes whose push failed; preserve
    # them so the next sync can retry rather than reconciling them away.
    candidate_to_delete = local_ids_before - remote_ids_seen
    to_delete: set[str] = set()
    preserved = 0
    for note_id in candidate_to_delete:
        row = store.find_by_id(note_id)
        if row is None:
            continue
        if int(row.get("synced", 1)) == 0:
            preserved += 1
            log(f"    ⏸ '{row['filename']}' (local-only, push failed) — preserving")
            continue
        to_delete.add(note_id)
    if delete_phase_blocked:
        if to_delete:
            log(
                f"  ⏸ skipping deletion of {len(to_delete)} local row(s) — "
                "remote scan was inconsistent"
            )
    elif to_delete:
        # Mass-delete circuit breaker — a remote that suddenly lists far
        # fewer notes (fresh backend behind KNOTEN_API_URL, server-side
        # regression) must not wipe the local mirror without an explicit
        # opt-in.
        # `to_delete` rows are still in the store here, so this count
        # includes them — the denominator is "synced rows before deletion".
        synced_total = store.count_synced_notes()
        if not force_delete and len(to_delete) > 5 and len(to_delete) > 0.2 * synced_total:
            warnings.append(
                f"delete detection wants to remove {len(to_delete)} of {synced_total} "
                "synced notes (>20%) — skipped; re-run with --force-delete to apply"
            )
            log(
                f"  ⚠ refusing to delete {len(to_delete)} of {synced_total} synced "
                "note(s) (>20%) — re-run with --force-delete to apply"
            )
        else:
            log(f"  {len(to_delete)} local row(s) absent from the remote")
            for note_id in to_delete:
                row = store.find_by_id(note_id)
                label = row["filename"] if row else note_id
                log(f"    ✗ removing '{label}' (trashed or hard-deleted on remote)")
                delete_ingested(store, settings.paths.vault_dir, note_id)
            deleted = len(to_delete)
    else:
        log("  no remote deletions detected")
    if preserved:
        push_outcome.preserved_local_only += preserved

    # Reconciliation — always existence + orphan check, hashes only on opt-in.
    log("→ Reconciling local mirror" + (" (with body-hash verification)" if verify_hashes else ""))
    reconcile = reconcile_local(
        backend=backend,
        store=store,
        settings=settings,
        verify_hashes=verify_hashes,
        progress=log,
    )
    skipped_invalid += reconcile.skipped_invalid
    warnings.extend(reconcile.warnings)
    log(
        f"  missing re-fetched: {reconcile.missing_refetched}, "
        f"mismatched re-fetched: {reconcile.mismatched_refetched}, "
        f"orphans removed: {reconcile.orphans_removed}"
    )

    if remote_total is None:
        remote_total = store.count_notes()

    now = _utcnow_iso()
    state.last_sync_at = now
    if max_seen:
        state.last_sync_max_updated_at = max_seen
    state.last_remote_total = remote_total
    save_state(settings.paths.state_file, state)
    store.set_meta("last_sync_at", now)
    if max_seen:
        store.set_meta("last_sync_max_updated_at", max_seen)

    return SyncResult(
        mode="incremental",
        fetched=fetched,
        deleted=deleted,
        remote_total=remote_total,
        scanned_remote_ids=scanned_remote_ids,
        local_total=store.count_notes(),
        last_sync_at=now,
        elapsed_seconds=round(time.monotonic() - started, 2),
        restricted_placeholders=restricted_placeholders,
        missing_refetched=reconcile.missing_refetched,
        mismatched_refetched=reconcile.mismatched_refetched,
        orphans_removed=reconcile.orphans_removed,
        verified_hashes=reconcile.verified_hashes,
        pushed_creates=push_outcome.creates,
        pushed_edits=push_outcome.edits,
        push_failed=push_outcome.failed,
        push_preserved_local_only=push_outcome.preserved_local_only,
        push_rejected=push_outcome.rejected,
        pushed_deletes=pushed_deletes,
        conflicts=conflicts,
        warnings=warnings,
        skipped_invalid=skipped_invalid,
    )


def full_sync(
    *,
    backend: Backend,
    store: Store,
    settings: Settings,
    verify_hashes: bool = False,
    force_delete: bool = False,
    progress: ProgressCallback | None = None,
) -> SyncResult:
    """Force a full refetch by clearing the cursor and running incremental.

    Delete detection and reconciliation always run (same as incremental).
    Pass `verify_hashes=True` to also re-read every file and compare against
    the recorded body hash — the strongest consistency guarantee the tool
    can offer.
    """
    result = incremental_sync(
        backend=backend,
        store=store,
        settings=settings,
        progress=progress,
        cursor_override="",
        verify_hashes=verify_hashes,
        force_delete=force_delete,
    )
    result.mode = "full"
    now = _utcnow_iso()
    state = load_state(settings.paths.state_file)
    state.last_full_sync_at = now
    save_state(settings.paths.state_file, state)
    return result


def _invalid_filename_reason(filename: str | None) -> str | None:
    """Reason a server-provided filename is unsafe to ingest, or None when fine.

    The filename becomes a path component of the mirror file, so anything
    that could make it escape the vault (path separators) or break the
    filesystem layer (NUL, empty) is rejected at the ingest boundary
    instead of trusted downstream.
    """
    if filename is None or not filename.strip():
        return "empty filename"
    if "\x00" in filename:
        return "filename contains a NUL byte"
    if "/" in filename or "\\" in filename:
        return f"filename contains a path separator: {filename!r}"
    return None


def _frontmatter_skip_warning(
    note_id: str, filename: str | None, exc: FrontmatterValidationError
) -> str:
    """One canonical warning line for a frontmatter-validation skip.

    Names the note (id + filename) and the offending frontmatter key, so the
    user can fix the value on the server — the v0.7.0 ingest-boundary
    contract: server payloads that fail validation are skipped with a
    warning, never aborting the run.
    """
    return (
        f"skipped remote note {note_id} ('{filename}'): frontmatter value for "
        f"{exc.key!r} contains a control character — frontmatter values must be "
        "single-line. Fix the value on the server, then re-sync."
    )


def _prefetch_guard(
    item: NoteSummary,
    *,
    store: Store,
    log: ProgressCallback,
    conflicts: list[dict],
    warnings: list[str],
    skipped_invalid_ids: set[str],
) -> tuple[int, int, int] | None:
    """Run the pre-fetch ingest guards on a list summary.

    Returns the `(fetched, placeholder, skipped_invalid)` counts tuple when
    the item must be skipped without a body fetch, or `None` when the caller
    should go on and fetch the body. Guards, in order:

    - A local row with `synced=0` is a pending local write — never
      overwrite it from the pull pass. Recorded as a conflict; the push
      pass later uploads the local version.
    - A server-provided filename failing validation is skipped with a
      warning — never written to disk, never committed to the store.
    """
    item_id = item.id

    existing = store.find_by_id(item_id)
    if existing is not None and int(existing.get("synced", 1)) == 0:
        conflicts.append(
            {"id": item_id, "filename": existing["filename"], "reason": "local_unsynced_edit"}
        )
        warnings.append(
            f"conflict: '{existing['filename']}' has an unpushed local edit — "
            "kept the local version (the push pass uploads it)"
        )
        log(f"    ⚠ '{existing['filename']}' has an unpushed local edit — keeping local version")
        return (0, 0, 0)

    reason = _invalid_filename_reason(item.filename)
    if reason is not None:
        warnings.append(f"skipped remote note {item_id}: {reason}")
        log(f"    ⚠ skipping remote note {item_id}: {reason}")
        skipped_invalid_ids.add(item_id)
        return (0, 0, 1)
    return None


def _ingest_restricted_placeholder(
    item: NoteSummary,
    *,
    store: Store,
    settings: Settings,
    log: ProgressCallback,
    warnings: list[str],
    skipped_invalid_ids: set[str],
) -> tuple[int, int, int]:
    """Mirror a list-but-not-READ note as a metadata-only placeholder.

    The branch a single read's 404 (`NoteForbiddenError`) and a batch read's
    `failed` entry share — the server conflates "forbidden" and "missing"
    the same way in both. Returns the counts tuple.
    """
    item_id = item.id
    previous = store.get_row(item_id)
    try:
        ingest_placeholder(
            item,
            store=store,
            vault_dir=settings.paths.vault_dir,
            previous_path=previous.path if previous else None,
        )
    except FrontmatterValidationError as exc:
        warning = _frontmatter_skip_warning(item_id, item.filename, exc)
        warnings.append(warning)
        log(f"    ⚠ {warning}")
        skipped_invalid_ids.add(item_id)
        return (0, 0, 1)
    log(f"    ⚠ '{item.filename}' is restricted (LIST but not READ) — stored as placeholder")
    return (0, 1, 0)


def _ingest_full_note(
    note: Note,
    *,
    store: Store,
    settings: Settings,
    log: ProgressCallback,
    conflicts: list[dict],
    warnings: list[str],
    skipped_invalid_ids: set[str],
) -> tuple[int, int, int]:
    """Ingest a fetched full note through the per-note guards.

    Applies identically to single-read and batch-read payloads. Guards:

    - The read response's filename is re-validated — it is also
      server-controlled and may differ from the list summary's.
    - A server-provided frontmatter value failing the writer's validation
      (control character — the mirror writes single-line YAML scalars) is
      skipped with a warning naming the note and the offending key, never
      aborting the run. The render failure happens before the store
      transaction, so nothing is committed.
    - A `UNIQUE(filename)` constraint failure (remote note colliding with
      a different local row's filename) is recorded as a conflict instead
      of aborting the whole sync.
    """
    reason = _invalid_filename_reason(note.filename)
    if reason is not None:
        warnings.append(f"skipped remote note {note.id}: {reason}")
        log(f"    ⚠ skipping remote note {note.id}: {reason}")
        skipped_invalid_ids.add(note.id)
        return (0, 0, 1)

    previous = store.get_row(note.id)
    try:
        ingest_note(
            note,
            store=store,
            vault_dir=settings.paths.vault_dir,
            previous_path=previous.path if previous else None,
        )
    except FrontmatterValidationError as exc:
        warning = _frontmatter_skip_warning(note.id, note.filename, exc)
        warnings.append(warning)
        log(f"    ⚠ {warning}")
        skipped_invalid_ids.add(note.id)
        return (0, 0, 1)
    except sqlite3.IntegrityError:
        conflicts.append({"id": note.id, "filename": note.filename, "reason": "filename_collision"})
        warnings.append(
            f"conflict: remote note '{note.filename}' ({note.id}) collides with an "
            "existing local note's filename — skipped; rename the local note to resolve"
        )
        log(f"    ⚠ '{note.filename}' collides with an existing local filename — skipped")
        return (0, 0, 0)
    return (1, 0, 0)


def _fetch_or_placeholder(
    item: NoteSummary,
    *,
    backend: Backend,
    store: Store,
    settings: Settings,
    log: ProgressCallback,
    conflicts: list[dict],
    warnings: list[str],
    skipped_invalid_ids: set[str],
) -> tuple[int, int, int]:
    """Fetch a full note by ID and ingest it. On 404, create a placeholder.

    Returns `(fetched_full_count, placeholder_count, skipped_invalid_count)`
    — at most one of them is 1. Every skipped note's id is also added to
    `skipped_invalid_ids` so the caller's catch-up pass does not re-fetch
    (and re-count) it within the same run.

    Composes the same three pieces the batch pull path uses —
    `_prefetch_guard`, `_ingest_restricted_placeholder`, `_ingest_full_note`
    — so the per-note guards are byte-identical between the single-read
    path (reconcile catch-up, old-server fallback) and the batch path.
    """
    guarded = _prefetch_guard(
        item,
        store=store,
        log=log,
        conflicts=conflicts,
        warnings=warnings,
        skipped_invalid_ids=skipped_invalid_ids,
    )
    if guarded is not None:
        return guarded

    try:
        note = backend.read_note(item.id)
    except NoteForbiddenError:
        return _ingest_restricted_placeholder(
            item,
            store=store,
            settings=settings,
            log=log,
            warnings=warnings,
            skipped_invalid_ids=skipped_invalid_ids,
        )
    return _ingest_full_note(
        note,
        store=store,
        settings=settings,
        log=log,
        conflicts=conflicts,
        warnings=warnings,
        skipped_invalid_ids=skipped_invalid_ids,
    )


def _pull_note_bodies(
    items: list[NoteSummary],
    *,
    backend: Backend,
    store: Store,
    settings: Settings,
    log: ProgressCallback,
    conflicts: list[dict],
    warnings: list[str],
    skipped_invalid_ids: set[str],
) -> tuple[int, int, int]:
    """Fetch and ingest the bodies for the pull pass's pending summaries.

    Returns the aggregated `(fetched, placeholder, skipped_invalid)` counts.

    Runs `_prefetch_guard` per item first (so `synced=0` conflicts and
    hostile summary filenames never reach the wire), then fetches the
    remainder in `POST /api/notes/batch-read` chunks of
    `BATCH_READ_CHUNK_SIZE`. Each note a batch returns goes through the
    exact same per-note ingest guards as a single read
    (`_ingest_full_note`); each id in the response's `failed` list takes
    the placeholder branch, same as a single read's 404.

    Old-server degradation: when the batch route itself 404s
    (`BatchReadUnsupportedError`), the pass falls back to per-note
    `read_note` calls for the rest of the run — detected once, never
    retried per chunk.
    """
    fetched = 0
    placeholders = 0
    invalid = 0

    to_fetch: list[NoteSummary] = []
    for item in items:
        guarded = _prefetch_guard(
            item,
            store=store,
            log=log,
            conflicts=conflicts,
            warnings=warnings,
            skipped_invalid_ids=skipped_invalid_ids,
        )
        if guarded is not None:
            fetched += guarded[0]
            placeholders += guarded[1]
            invalid += guarded[2]
            continue
        to_fetch.append(item)

    batch_supported = True
    total_chunks = (len(to_fetch) + BATCH_READ_CHUNK_SIZE - 1) // BATCH_READ_CHUNK_SIZE
    for chunk_index, start in enumerate(range(0, len(to_fetch), BATCH_READ_CHUNK_SIZE), start=1):
        chunk = to_fetch[start : start + BATCH_READ_CHUNK_SIZE]
        if batch_supported:
            by_id = {item.id: item for item in chunk}
            try:
                batch = backend.read_notes(tuple(by_id))
            except BatchReadUnsupportedError:
                batch_supported = False
                log(
                    "  server has no batch-read endpoint (pre-batch-read release) — "
                    "falling back to per-note fetches for this run"
                )
            else:
                log(
                    f"    ↓ batch {chunk_index}/{total_chunks}: fetched "
                    f"{len(batch.notes)} note(s), {len(batch.failed)} restricted/missing"
                )
                for note in batch.notes:
                    if note.id not in by_id:
                        continue  # defensive: ignore notes we did not ask for
                    counts = _ingest_full_note(
                        note,
                        store=store,
                        settings=settings,
                        log=log,
                        conflicts=conflicts,
                        warnings=warnings,
                        skipped_invalid_ids=skipped_invalid_ids,
                    )
                    fetched += counts[0]
                    placeholders += counts[1]
                    invalid += counts[2]
                for failed_id in batch.failed:
                    item = by_id.get(failed_id)
                    if item is None:
                        continue
                    counts = _ingest_restricted_placeholder(
                        item,
                        store=store,
                        settings=settings,
                        log=log,
                        warnings=warnings,
                        skipped_invalid_ids=skipped_invalid_ids,
                    )
                    fetched += counts[0]
                    placeholders += counts[1]
                    invalid += counts[2]
                # An id in neither `notes` nor `failed` would violate the
                # endpoint contract; it stays un-ingested this run and the
                # reconcile catch-up pass re-fetches it per-note.
                continue
        # Per-note fallback — the server predates the batch route. The
        # pre-fetch guard re-runs inside `_fetch_or_placeholder`; it is
        # idempotent and the store has not changed for these ids.
        for item in chunk:
            log(f"    ↓ fetching '{item.filename or item.id}'")
            counts = _fetch_or_placeholder(
                item,
                backend=backend,
                store=store,
                settings=settings,
                log=log,
                conflicts=conflicts,
                warnings=warnings,
                skipped_invalid_ids=skipped_invalid_ids,
            )
            fetched += counts[0]
            placeholders += counts[1]
            invalid += counts[2]
    return (fetched, placeholders, invalid)
