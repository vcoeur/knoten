"""ingest_note writes files on disk and upserts into the store in one pass."""

from __future__ import annotations

from app.models import Note, WikiLink
from app.repositories.store import Store
from app.services.notes import ingest_note
from app.settings import Settings


def _note(filename: str, family: str, body: str) -> Note:
    return Note(
        id="00000000-0000-0000-0000-000000000001",
        filename=filename,
        title=filename.lstrip("!@$%&-=.+ "),
        family=family,
        kind=family,
        source=None,
        body=body,
        frontmatter={"kind": family, "title": filename},
        tags=(),
        wikilinks=(WikiLink(target_title="Other", target_id=None),),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


def test_ingest_writes_markdown_file(tmp_settings: Settings, store: Store) -> None:
    note = _note("! Core idea", "permanent", "Body of the note with [[Other]].")
    relative_path = ingest_note(note, store=store, vault_dir=tmp_settings.vault_dir)

    assert relative_path == "note/! Core idea.md"
    written = tmp_settings.vault_dir / relative_path
    assert written.exists()
    content = written.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "kind: permanent" in content
    assert "Body of the note" in content


def test_rename_removes_old_path(tmp_settings: Settings, store: Store) -> None:
    first = _note("! Core idea", "permanent", "First body.")
    old_path = ingest_note(first, store=store, vault_dir=tmp_settings.vault_dir)
    assert (tmp_settings.vault_dir / old_path).exists()

    # Same ID, new filename -> simulates a rename.
    renamed = Note(
        id=first.id,
        filename="! Core insight",
        title="Core insight",
        family="permanent",
        kind="permanent",
        source=None,
        body="Second body.",
        frontmatter={"kind": "permanent", "title": "Core insight"},
        tags=(),
        wikilinks=(),
        created_at=first.created_at,
        updated_at="2024-01-03T00:00:00Z",
    )
    new_path = ingest_note(
        renamed,
        store=store,
        vault_dir=tmp_settings.vault_dir,
        previous_path=old_path,
    )
    assert new_path == "note/! Core insight.md"
    assert (tmp_settings.vault_dir / new_path).exists()
    assert not (tmp_settings.vault_dir / old_path).exists()
    assert store.count_notes() == 1


def test_journal_path_buckets_by_month(tmp_settings: Settings, store: Store) -> None:
    note = Note(
        id="00000000-0000-0000-0000-000000000002",
        filename="2024-11-10 Weekly review",
        title="Weekly review",
        family="journal",
        kind="journal",
        source="2024-11-10",
        body="Body",
        frontmatter={"kind": "journal"},
        tags=(),
        wikilinks=(),
        created_at="2024-11-10T00:00:00Z",
        updated_at="2024-11-10T00:00:00Z",
    )
    path = ingest_note(note, store=store, vault_dir=tmp_settings.vault_dir)
    assert path == "journal/2024-11/2024-11-10 Weekly review.md"
