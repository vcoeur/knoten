"""Sync orchestration — incremental and full modes.

Incremental: page through `GET /api/notes` (sorted desc by updatedAt) until
we hit items older than our local cursor, then fetch bodies and upsert each
new/changed note. Delete detection runs when the remote `total` disagrees
with the local count, or when explicitly requested.

Full: same algorithm but with an empty cursor and forced delete detection —
effectively "refetch everything, reconcile both sides". The `/api/export`
endpoint is not used because the export zip is filename-keyed and carries
no UUIDs, so we'd still need the per-note read path to get the canonical IDs.
A dedicated `kasten export` command can still use /api/export when someone
wants an offline archive; that is out of scope for v1.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.repositories.errors import NoteForbiddenError
from app.repositories.http_client import NotesClient
from app.repositories.store import Store
from app.repositories.sync_state import load_state, save_state
from app.services.note_mapper import note_from_api, summary_from_api
from app.services.notes import delete_ingested, ingest_note, ingest_placeholder
from app.services.reconcile import reconcile_local
from app.settings import Settings

ProgressCallback = Callable[[str], None]


def _noop(_: str) -> None:
    pass


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
    # change that KastenManager's pagination does not yet understand.
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


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def incremental_sync(
    *,
    client: NotesClient,
    store: Store,
    settings: Settings,
    cursor_override: str | None = None,
    verify_hashes: bool = False,
    progress: ProgressCallback | None = None,
) -> SyncResult:
    """Run an incremental sync. Returns a SyncResult with counts.

    When `cursor_override` is an empty string, every note is refetched —
    used by `full_sync` below.

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
    state = load_state(settings.state_file)
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
    max_seen = cursor
    remote_total: int | None = None
    local_ids_before = store.all_ids()

    offset = 0
    page_size = 100
    page_num = 0
    while True:
        page_num += 1
        page = client.list_notes(limit=page_size, offset=offset)
        remote_total = page.get("total", remote_total)
        items = page.get("data", [])
        if not items:
            break

        new_on_page = sum(1 for item in items if _updated(item) > cursor)
        log(
            f"  page {page_num}: {len(items)} items, {new_on_page} newer than cursor"
            + (f" (remote total {remote_total})" if remote_total is not None else "")
        )

        page_has_stale = False
        for item in items:
            item_id = str(item["id"])
            updated = _updated(item)
            if updated > cursor:
                filename = item.get("filename") or item_id
                log(f"    ↓ fetching '{filename}'")
                fetched_count, restricted_count = _fetch_or_placeholder(
                    item,
                    client=client,
                    store=store,
                    settings=settings,
                    log=log,
                )
                fetched += fetched_count
                restricted_placeholders += restricted_count
                if updated > max_seen:
                    max_seen = updated
            else:
                page_has_stale = True

        if page_has_stale:
            break
        if len(items) < page_size:
            break
        offset += page_size

    # Reconcile the local ID set against the remote ID set. This single pass
    # handles BOTH previously-separate concerns:
    #
    #   (a) Delete detection — any local ID not in the remote is gone on the
    #       server (trashed or hard-deleted). Remove it.
    #   (b) Drift catch-up — any remote ID not in the local store is a note
    #       we have never ingested. This happens when a previous sync was
    #       aborted mid-flight, and also for `mcpPermissions = LIST` notes
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
    for item in client.iter_all_summaries(page_size=200):
        item_id = str(item["id"])
        remote_ids_seen.add(item_id)
        if item_id in local_ids_after_main:
            already_local += 1
            continue
        if not catch_up_started:
            log("  catching up on never-seen-locally notes")
            catch_up_started = True
        filename = item.get("filename") or item_id
        log(f"    ↓ fetching '{filename}' (never seen locally)")
        fetched_count, restricted_count = _fetch_or_placeholder(
            item,
            client=client,
            store=store,
            settings=settings,
            log=log,
        )
        fetched += fetched_count
        catch_up_count += fetched_count
        restricted_placeholders += restricted_count
        catch_up_restricted += restricted_count
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
    if remote_total is not None and scanned_remote_ids != remote_total:
        # Tripwire for a regression in the server's list endpoint. After the
        # 2026-04-12 fix (incident `2026-04-12-notes-list-permission-leaks`)
        # this branch should never trigger in a steady state. If it does,
        # the most likely causes are a stable-sort regression in
        # `notes.vcoeur.com`'s `listNotes.orderBy` or a new filter applied
        # to the data query but not the count query. Keep the warning as
        # surface area even if it never fires — silent drift is worse.
        log(
            f"  ⚠ server `total` ({remote_total}) disagrees with scanned count "
            f"({scanned_remote_ids}) — pagination walk saw a different row set "
            f"than the count query. Should not happen post-2026-04-12 fix."
        )

    # Delete detection — local IDs (pre-sync) absent from the remote set.
    # Notes ingested during this run are implicitly in the remote set, so
    # they cannot be flagged for deletion.
    to_delete = local_ids_before - remote_ids_seen
    if to_delete:
        log(f"  {len(to_delete)} local row(s) absent from the remote")
        for note_id in to_delete:
            row = store.find_by_id(note_id)
            label = row["filename"] if row else note_id
            log(f"    ✗ removing '{label}' (trashed or hard-deleted on remote)")
            delete_ingested(store, settings.vault_dir, note_id)
        deleted = len(to_delete)
    else:
        log("  no remote deletions detected")

    # Reconciliation — always existence + orphan check, hashes only on opt-in.
    log("→ Reconciling local mirror" + (" (with body-hash verification)" if verify_hashes else ""))
    reconcile = reconcile_local(
        client=client,
        store=store,
        settings=settings,
        verify_hashes=verify_hashes,
        progress=log,
    )
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
    save_state(settings.state_file, state)
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
    )


def full_sync(
    *,
    client: NotesClient,
    store: Store,
    settings: Settings,
    verify_hashes: bool = False,
    progress: ProgressCallback | None = None,
) -> SyncResult:
    """Force a full refetch by clearing the cursor and running incremental.

    Delete detection and reconciliation always run (same as incremental).
    Pass `verify_hashes=True` to also re-read every file and compare against
    the recorded body hash — the strongest consistency guarantee the tool
    can offer.
    """
    result = incremental_sync(
        client=client,
        store=store,
        settings=settings,
        progress=progress,
        cursor_override="",
        verify_hashes=verify_hashes,
    )
    result.mode = "full"
    now = _utcnow_iso()
    state = load_state(settings.state_file)
    state.last_full_sync_at = now
    save_state(settings.state_file, state)
    return result


def _fetch_or_placeholder(
    item: dict[str, Any],
    *,
    client: NotesClient,
    store: Store,
    settings: Settings,
    log: ProgressCallback,
) -> tuple[int, int]:
    """Fetch a full note by ID and ingest it. On 404, create a placeholder.

    Returns `(fetched_full_count, placeholder_count)` — one of them is 0 and
    the other is 1 depending on whether the body was readable.
    """
    item_id = str(item["id"])
    try:
        note_payload = client.read_note(item_id)
    except NoteForbiddenError:
        summary = summary_from_api(item)
        previous = store.get_row(item_id)
        ingest_placeholder(
            summary,
            store=store,
            vault_dir=settings.vault_dir,
            previous_path=previous.path if previous else None,
        )
        log(f"    ⚠ '{summary.filename}' is restricted (LIST but not READ) — stored as placeholder")
        return (0, 1)

    note = note_from_api(note_payload)
    previous = store.get_row(note.id)
    ingest_note(
        note,
        store=store,
        vault_dir=settings.vault_dir,
        previous_path=previous.path if previous else None,
    )
    return (1, 0)


def _updated(item: dict[str, Any]) -> str:
    return str(item.get("updatedAt") or item.get("updated_at", ""))
