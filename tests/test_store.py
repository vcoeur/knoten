"""SQLite store: schema creation, upsert, search, backlinks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from knoten.models import Note, WikiLink
from knoten.repositories.store import SCHEMA_VERSION, Store


def _make_note(
    *,
    note_id: str,
    filename: str,
    body: str,
    family: str = "permanent",
    kind: str = "permanent",
    wikilinks: tuple[WikiLink, ...] = (),
    tags: tuple[str, ...] = (),
    title: str | None = None,
) -> Note:
    return Note(
        id=note_id,
        filename=filename,
        title=title or filename.lstrip("!@$%&-=.+ "),
        family=family,
        kind=kind,
        source=None,
        body=body,
        frontmatter={"kind": kind, "title": title or filename},
        tags=tags,
        wikilinks=wikilinks,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


def test_upsert_and_count(store: Store) -> None:
    note = _make_note(note_id="n1", filename="! Hello world", body="hello body")
    store.upsert_note(note, path="note/! Hello world.md", body_sha256="abc")
    assert store.count_notes() == 1

    # Second upsert with the same id replaces in place.
    changed = _make_note(note_id="n1", filename="! Hello world", body="hello body updated")
    store.upsert_note(changed, path="note/! Hello world.md", body_sha256="def")
    assert store.count_notes() == 1


def test_find_by_filename_prefix(store: Store) -> None:
    store.upsert_note(
        _make_note(note_id="n1", filename="Voland2024= Book One", body=""),
        path="literature/Voland2024= Book One.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(note_id="n2", filename="Voland2024= Book Two", body=""),
        path="literature/Voland2024= Book Two.md",
        body_sha256="2",
    )
    matches = store.find_by_filename_prefix("Voland2024")
    assert len(matches) == 2


def test_fts_search_ranks_title_above_body(store: Store, tmp_path: Path) -> None:
    store.upsert_note(
        _make_note(
            note_id="n1",
            filename="! Trigram blind index",
            body="A note about search strategies.",
        ),
        path="note/! Trigram blind index.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(
            note_id="n2",
            filename="! Some other thing",
            body="This note mentions trigram once.",
        ),
        path="note/! Some other thing.md",
        body_sha256="2",
    )
    hits, total = store.search("trigram", vault_dir=tmp_path)
    assert total == 2
    # Title match (n1) should come before body-only match (n2).
    assert hits[0].id == "n1"
    assert hits[1].id == "n2"
    # Snippets contain the match markers.
    assert "<<" in hits[0].snippet or hits[0].snippet != ""


def test_backlinks_returns_linking_notes(store: Store) -> None:
    target_id = "target"
    store.upsert_note(
        _make_note(note_id=target_id, filename="! Target", body=""),
        path="note/! Target.md",
        body_sha256="t",
    )
    store.upsert_note(
        _make_note(
            note_id="src1",
            filename="! Source one",
            body="Linked to [[Target]]",
            wikilinks=(WikiLink(target_title="Target", target_id=target_id),),
        ),
        path="note/! Source one.md",
        body_sha256="s1",
    )
    store.upsert_note(
        _make_note(
            note_id="src2",
            filename="! Source two",
            body="Also [[Target]]",
            wikilinks=(WikiLink(target_title="Target", target_id=target_id),),
        ),
        path="note/! Source two.md",
        body_sha256="s2",
    )
    backlinks = store.backlinks_for_note(target_id)
    ids = {bl["id"] for bl in backlinks}
    assert ids == {"src1", "src2"}


def test_list_filters_by_family(store: Store) -> None:
    store.upsert_note(
        _make_note(note_id="a", filename="@ Alice", body="", family="person", kind="person"),
        path="entity/@ Alice.md",
        body_sha256="a",
    )
    store.upsert_note(
        _make_note(note_id="b", filename="! Idea", body="", family="permanent", kind="permanent"),
        path="note/! Idea.md",
        body_sha256="b",
    )
    notes, total = store.list_notes(family="person")
    assert total == 1
    assert notes[0].id == "a"


def test_search_explain_returns_per_column_scores(store: Store, tmp_path: Path) -> None:
    """`search(..., explain=True)` attaches per-column bm25 breakdowns.

    A title-heavy hit should have a larger (more negative) `title`
    contribution than its `body` contribution under bm25. We only assert
    the structure + ordering, not exact numbers — bm25 is tokenizer- and
    corpus-dependent, so hardcoded values rot.
    """
    store.upsert_note(
        _make_note(
            note_id="n1",
            filename="! Trigram blind index",
            body="A note about search strategies.",
        ),
        path="note/! Trigram blind index.md",
        body_sha256="1",
    )
    hits, total = store.search("trigram", vault_dir=tmp_path, explain=True)
    assert total == 1
    hit = hits[0]
    assert hit.explain is not None
    columns = dict(hit.explain)
    assert set(columns) == {"title", "body", "filename"}
    # bm25 is negative for matches; more negative = better.
    # The title hit should beat the body-only hit in magnitude.
    assert columns["title"] < columns["body"]


def test_fuzzy_search_matches_substring_in_body(store: Store, tmp_path: Path) -> None:
    store.upsert_note(
        _make_note(
            note_id="n1",
            filename="! Auth middleware notes",
            body="Refactoring the authentication middleware to use JWTs.",
        ),
        path="note/! Auth middleware notes.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(
            note_id="n2",
            filename="! Unrelated",
            body="Nothing about the topic here.",
        ),
        path="note/! Unrelated.md",
        body_sha256="2",
    )

    # Substring "auth" (inside "authentication") — unicode61 FTS misses this;
    # trigram FTS picks it up.
    hits, total = store.search_fuzzy("auth", vault_dir=tmp_path)
    assert total == 1
    assert hits[0].id == "n1"
    assert hits[0].score > 0.0


def test_fuzzy_search_typo_tolerant_on_title(store: Store, tmp_path: Path) -> None:
    store.upsert_note(
        _make_note(
            note_id="n1",
            filename="! Encryption handbook",
            body="",
        ),
        path="note/! Encryption handbook.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(
            note_id="n2",
            filename="! Something else entirely",
            body="",
        ),
        path="note/! Something else entirely.md",
        body_sha256="2",
    )

    # Typo "encrpytion" should still find the encryption note via rapidfuzz.
    hits, total = store.search_fuzzy("encrpytion handbok", vault_dir=tmp_path)
    assert total >= 1
    assert hits[0].id == "n1"


def test_fuzzy_search_respects_family_filter(store: Store, tmp_path: Path) -> None:
    store.upsert_note(
        _make_note(
            note_id="n1",
            filename="@ Encryption person",
            body="encryption body",
            family="person",
            kind="person",
        ),
        path="entity/@ Encryption person.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(
            note_id="n2",
            filename="! Encryption handbook",
            body="encryption body",
            family="permanent",
            kind="permanent",
        ),
        path="note/! Encryption handbook.md",
        body_sha256="2",
    )

    hits, _ = store.search_fuzzy("encryption", family="permanent", vault_dir=tmp_path)
    assert {hit.id for hit in hits} == {"n2"}


def test_fuzzy_search_empty_query_returns_nothing(store: Store, tmp_path: Path) -> None:
    store.upsert_note(
        _make_note(note_id="n1", filename="! Anything", body="some body"),
        path="note/! Anything.md",
        body_sha256="1",
    )
    hits, total = store.search_fuzzy("   ", vault_dir=tmp_path)
    assert hits == []
    assert total == 0


def test_v3_to_v4_migration_populates_trigram(tmp_path: Path) -> None:
    """Opening a v3 store triggers the trigram backfill from notes_fts."""
    db_path = tmp_path / "index.sqlite"
    # Seed a minimal v3 store: notes row + notes_fts row, without the trigram
    # table. `_ensure_schema` will then add the trigram table and backfill it.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE notes (
                id                TEXT PRIMARY KEY,
                filename          TEXT NOT NULL,
                title             TEXT NOT NULL,
                family            TEXT NOT NULL,
                kind              TEXT NOT NULL,
                source            TEXT,
                path              TEXT NOT NULL,
                frontmatter_json  TEXT NOT NULL DEFAULT '{}',
                body_sha256       TEXT NOT NULL,
                restricted        INTEGER NOT NULL DEFAULT 0,
                mcp_permissions   TEXT NOT NULL DEFAULT 'ALL',
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE notes_fts USING fts5(
                note_id UNINDEXED, title, body, filename,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute(
            """
            INSERT INTO notes VALUES (
                'n1', '! Legacy note', 'Legacy note', 'permanent', 'permanent',
                NULL, 'note/! Legacy note.md', '{}', 'abc', 0, 'ALL',
                '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z'
            )
            """
        )
        conn.execute(
            "INSERT INTO notes_fts(note_id, title, body, filename) VALUES(?, ?, ?, ?)",
            ("n1", "Legacy note", "authentication middleware body", "! Legacy note"),
        )
        conn.execute("INSERT INTO sync_meta(key, value) VALUES('schema_version', '3')")
        conn.commit()

    with Store(db_path) as migrated:
        # Schema version bumped
        assert migrated.get_meta("schema_version") == str(SCHEMA_VERSION)
        # Trigram table populated
        trigram_count = migrated.conn.execute("SELECT COUNT(*) FROM notes_fts_trigram").fetchone()[
            0
        ]
        assert trigram_count == 1
        # Fuzzy search against the migrated store finds the body substring
        hits, total = migrated.search_fuzzy("auth", vault_dir=tmp_path)
        assert total == 1
        assert hits[0].id == "n1"


def test_tag_and_kind_counts(store: Store) -> None:
    store.upsert_note(
        _make_note(note_id="a", filename="! One", body="", tags=("search", "encryption")),
        path="note/! One.md",
        body_sha256="a",
    )
    store.upsert_note(
        _make_note(note_id="b", filename="! Two", body="", tags=("search",)),
        path="note/! Two.md",
        body_sha256="b",
    )
    tag_counts = {row["tag"]: row["count"] for row in store.tag_counts()}
    assert tag_counts == {"search": 2, "encryption": 1}

    kind_counts = {row["kind"]: row["count"] for row in store.kind_counts()}
    assert kind_counts == {"permanent": 2}
