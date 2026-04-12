"""Indexing-consistency guarantees: ingest ordering, integrity, reindex."""

from __future__ import annotations

from app.models import Note
from app.repositories.store import Store
from app.services.notes import ingest_note
from app.services.reindex import reindex_from_files
from app.settings import Settings


def _seed(store: Store, settings: Settings, note_id: str, body: str) -> None:
    note = Note(
        id=note_id,
        filename="! Seeded",
        title="Seeded",
        family="permanent",
        kind="permanent",
        source=None,
        body=body,
        frontmatter={"kind": "permanent", "title": "Seeded"},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )
    ingest_note(note, store=store, vault_dir=settings.vault_dir)


def test_sqlite_integrity_check_passes_on_fresh_store(tmp_settings: Settings) -> None:
    with Store(tmp_settings.index_path) as store:
        _seed(store, tmp_settings, "11111111-1111-1111-1111-111111111111", "Body.")
        assert store.integrity_check() == "ok"


def test_fts_cardinality_detects_manual_deletion(tmp_settings: Settings) -> None:
    with Store(tmp_settings.index_path) as store:
        _seed(store, tmp_settings, "22222222-2222-2222-2222-222222222222", "Body.")
        # Simulate index drift: yank the FTS5 row out without touching notes.
        store.conn.execute("DELETE FROM notes_fts")
        store.conn.commit()

        report = store.fts_cardinality_check()
        assert report["consistent"] is False
        assert report["notes_count"] == 1
        assert report["fts_count"] == 0
        assert report["missing_in_fts"] == ["22222222-2222-2222-2222-222222222222"]


def test_reindex_rebuilds_fts_from_files(tmp_settings: Settings) -> None:
    with Store(tmp_settings.index_path) as store:
        _seed(
            store,
            tmp_settings,
            "33333333-3333-3333-3333-333333333333",
            "Body about #encryption and [[HMAC]].",
        )

        # Corrupt the derived tables: drop FTS5, tags, and wikilinks for this note.
        store.conn.execute("DELETE FROM notes_fts")
        store.conn.execute("DELETE FROM tags")
        store.conn.execute("DELETE FROM wikilinks")
        store.conn.commit()
        assert store.fts_cardinality_check()["consistent"] is False

        result = reindex_from_files(store=store, settings=tmp_settings)
        assert result.reindexed == 1
        assert result.skipped_missing_file == 0
        assert result.cardinality_before["consistent"] is False
        assert result.cardinality_after["consistent"] is True

        # Derived tables are fully rebuilt from the on-disk body.
        hits, total = store.search("encryption", vault_dir=tmp_settings.vault_dir)
        assert total == 1
        assert hits[0].id == "33333333-3333-3333-3333-333333333333"

        tags = {row["tag"] for row in store.tag_counts()}
        assert "encryption" in tags

        wikilinks = store.wikilinks_for_note("33333333-3333-3333-3333-333333333333")
        assert any(w["target_title"] == "HMAC" for w in wikilinks)


def test_reindex_skips_notes_with_missing_files(tmp_settings: Settings) -> None:
    with Store(tmp_settings.index_path) as store:
        _seed(store, tmp_settings, "44444444-4444-4444-4444-444444444444", "Body.")
        target = tmp_settings.vault_dir / "note" / "! Seeded.md"
        target.unlink()

        result = reindex_from_files(store=store, settings=tmp_settings)
        assert result.skipped_missing_file == 1
        assert result.missing_file_ids == ["44444444-4444-4444-4444-444444444444"]


def test_ingest_order_store_first_then_file(tmp_settings: Settings) -> None:
    """ingest_note must commit the store transaction BEFORE writing the file.

    We assert this by checking that when ingest returns, the store row is
    present AND the file exists. Previously the file was written first —
    a crash between the two steps would leave a drifted index. This test
    locks in the new order so a refactor can't silently regress it.
    """
    with Store(tmp_settings.index_path) as store:
        note = Note(
            id="55555555-5555-5555-5555-555555555555",
            filename="! Ordered",
            title="Ordered",
            family="permanent",
            kind="permanent",
            source=None,
            body="Body.",
            frontmatter={"kind": "permanent", "title": "Ordered"},
            tags=(),
            wikilinks=(),
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-02T00:00:00Z",
        )
        ingest_note(note, store=store, vault_dir=tmp_settings.vault_dir)
        assert store.find_by_id("55555555-5555-5555-5555-555555555555") is not None
        assert (tmp_settings.vault_dir / "note" / "! Ordered.md").exists()
        # FTS5 row was written inside the transaction — a search hits it.
        hits, total = store.search("Body", vault_dir=tmp_settings.vault_dir)
        assert total == 1
