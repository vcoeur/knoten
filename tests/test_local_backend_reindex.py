"""LocalBackend stat-walk drift detection.

Populates a vault + store via `ingest_note`, mutates the filesystem
out-of-band, and asserts the LocalBackend walk notices the drift on the
next read-path call.
"""

from __future__ import annotations

import os

import pytest

from app.models import Note
from app.repositories.errors import NotFoundError
from app.repositories.local_backend import LocalBackend
from app.repositories.store import Store
from app.services.notes import ingest_note
from app.settings import Settings


def _make_note(
    note_id: str,
    filename: str,
    body: str,
    updated_at: str = "2024-01-02T00:00:00Z",
) -> Note:
    return Note(
        id=note_id,
        filename=filename,
        title=filename.lstrip("- @=+!").strip(),
        family="fleeting",
        kind="fleeting",
        source=None,
        body=body,
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at=updated_at,
        mcp_permissions="ALL",
    )


def _seed(settings: Settings, count: int = 2) -> list[Note]:
    notes = [
        _make_note(
            f"{i:08d}-0000-0000-0000-000000000000",
            f"- Note {i}",
            f"Body number {i}.",
        )
        for i in range(1, count + 1)
    ]
    with Store(settings.index_path) as store:
        for note in notes:
            ingest_note(note, store=store, vault_dir=settings.vault_dir)
    return notes


def _bump_mtime(file_path) -> None:
    """Advance a file's mtime by 2 seconds so stat comparisons see drift."""
    stat = file_path.stat()
    new_time = stat.st_mtime + 2
    os.utime(file_path, (new_time, new_time))


def test_external_write_body_surfaces_on_next_read(tmp_settings: Settings) -> None:
    notes = _seed(tmp_settings, count=1)
    target = notes[0]

    path = tmp_settings.vault_dir / "note" / f"{target.filename}.md"
    assert path.exists()

    new_markdown = (
        "---\n"
        f"kind: fleeting\nfamily: fleeting\ntitle: {target.title}\n"
        "created: 2024-01-01T00:00:00Z\nupdated: 2024-01-02T00:00:00Z\n"
        "---\n\n"
        "Edited externally by the user's text editor.\n"
        "Plus a [[- Something Else]] wiki-link and a #freshtag.\n"
    )
    path.write_text(new_markdown, encoding="utf-8")
    _bump_mtime(path)

    with LocalBackend(tmp_settings) as backend:
        refreshed = backend.read_note(target.id)

    assert "Edited externally" in refreshed.body
    assert "#freshtag" in refreshed.body


def test_external_write_updates_stored_stats(tmp_settings: Settings) -> None:
    notes = _seed(tmp_settings, count=1)
    target = notes[0]
    path = tmp_settings.vault_dir / "note" / f"{target.filename}.md"

    path.write_text(
        "---\nkind: fleeting\nfamily: fleeting\ntitle: drifted\n"
        "created: 2024-01-01T00:00:00Z\nupdated: 2024-01-02T00:00:00Z\n---\n\nnew body\n",
        encoding="utf-8",
    )
    _bump_mtime(path)
    new_stat = path.stat()

    with LocalBackend(tmp_settings) as backend:
        backend.list_note_summaries(limit=10, offset=0)

    with Store(tmp_settings.index_path) as store:
        index = store.path_index()
    recorded_mtime, recorded_size, _ = index[f"note/{target.filename}.md"]
    assert recorded_mtime == new_stat.st_mtime_ns
    assert recorded_size == new_stat.st_size


def test_external_delete_drops_store_row(tmp_settings: Settings) -> None:
    notes = _seed(tmp_settings, count=2)
    victim = notes[0]
    survivor = notes[1]

    victim_path = tmp_settings.vault_dir / "note" / f"{victim.filename}.md"
    victim_path.unlink()

    with LocalBackend(tmp_settings) as backend:
        page = backend.list_note_summaries(limit=10, offset=0)
        surfaced_ids = {summary.id for summary in page.data}
        with pytest.raises(NotFoundError):
            backend.read_note(victim.id)

    assert victim.id not in surfaced_ids
    assert survivor.id in surfaced_ids


def test_walk_runs_once_per_instance(tmp_settings: Settings, monkeypatch) -> None:
    _seed(tmp_settings, count=2)

    call_counter = {"count": 0}
    original_path_index = Store.path_index

    def counting_path_index(self):
        call_counter["count"] += 1
        return original_path_index(self)

    monkeypatch.setattr(Store, "path_index", counting_path_index)

    with LocalBackend(tmp_settings) as backend:
        backend.list_note_summaries(limit=10, offset=0)
        backend.list_note_summaries(limit=10, offset=0)
        backend.list_note_summaries(limit=10, offset=0)

    assert call_counter["count"] == 1, (
        f"stat walk ran {call_counter['count']} times; should be once per instance"
    )


def test_trash_directory_is_skipped(tmp_settings: Settings) -> None:
    _seed(tmp_settings, count=1)

    trash_dir = tmp_settings.vault_dir / ".trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    stray = trash_dir / "should-be-ignored.md"
    stray.write_text(
        "---\nkind: fleeting\nfamily: fleeting\ntitle: stray\n"
        "created: 2024-01-01T00:00:00Z\nupdated: 2024-01-01T00:00:00Z\n---\n\nfrom the trash\n",
        encoding="utf-8",
    )

    with LocalBackend(tmp_settings) as backend:
        page = backend.list_note_summaries(limit=10, offset=0)

    assert page.total == 1
    assert all(".trash" not in (summary.filename or "") for summary in page.data)
