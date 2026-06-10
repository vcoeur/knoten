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
                permissions   TEXT NOT NULL DEFAULT 'ALL',
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


def test_create_resolves_pre_existing_broken_wikilinks(store: Store) -> None:
    """A note's creation backfills `target_id` on every existing broken link to it.

    Reproduces a subtle case during stub-creation flows: writing a reference
    note that links to `[[& Commons]]` before the topic stub exists records
    `target_id=NULL`. When the stub is later created, `knoten read` should
    immediately resolve the link without needing a `sync --full` to rebuild
    the wikilinks table.
    """
    referrer = _make_note(
        note_id="ref",
        filename="Bollier2025= Think Like a Commoner",
        body="Linked to [[& Commons]]",
        wikilinks=(WikiLink(target_title="& Commons", target_id=None),),
        family="reference",
        kind="book",
    )
    store.upsert_note(
        referrer, path="literature/Bollier2025= Think Like a Commoner.md", body_sha256="r"
    )
    pre = store.conn.execute(
        "SELECT target_id FROM wikilinks WHERE source_id = ? AND target_title = ?",
        ("ref", "& Commons"),
    ).fetchone()
    assert pre["target_id"] is None

    store.upsert_note(
        _make_note(
            note_id="commons",
            filename="& Commons",
            body="Definitional body.",
            family="topic",
            kind="topic",
        ),
        path="entity/& Commons.md",
        body_sha256="c",
    )
    post = store.conn.execute(
        "SELECT target_id FROM wikilinks WHERE source_id = ? AND target_title = ?",
        ("ref", "& Commons"),
    ).fetchone()
    assert post["target_id"] == "commons"


def test_search_hit_json_shape_includes_filename(store: Store, tmp_path: Path) -> None:
    """Lock the documented JSON hit shape — every key must be present.

    Reproduces the 2026-05-08 surprise where iterating over hits with
    `python3 -c "...h['filename']..."` raised KeyError because the dataclass
    had not been carrying `filename`. The skill (and the integration scripts
    built on top of it) document `filename` as a stable hit key.
    """
    from knoten.services.notes import hit_to_dict

    store.upsert_note(
        _make_note(
            note_id="n1",
            filename="! Locked shape",
            body="body for shape lock",
        ),
        path="note/! Locked shape.md",
        body_sha256="x",
    )
    hits, _ = store.search("shape", vault_dir=tmp_path)
    assert hits
    payload = hit_to_dict(hits[0])
    expected = {
        "id",
        "filename",
        "title",
        "family",
        "kind",
        "source",
        "path",
        "absolute_path",
        "tags",
        "score",
        "snippet",
        "updated_at",
        "permissions",
    }
    missing = expected - payload.keys()
    assert not missing, f"hit missing documented keys: {missing}"
    assert payload["filename"] == "! Locked shape"


def test_search_handles_citation_key_query_without_fts5_error(store: Store, tmp_path: Path) -> None:
    """`knoten search "Bollier2025="` must not leak FTS5 syntax errors.

    Reproduces the 2026-05-08 bug where `=` in a free-text query crashed the
    underlying MATCH parser. The sanitizer phrase-quotes any token containing
    FTS5-reserved punctuation so the SELECT runs cleanly even when the input
    happens to look like a citation key.
    """
    store.upsert_note(
        _make_note(
            note_id="ref",
            filename="Bollier2025= Think Like a Commoner",
            body="A short introduction.",
            family="reference",
            kind="book",
        ),
        path="literature/Bollier2025= Think Like a Commoner.md",
        body_sha256="abc",
    )
    # Punctuation-only query that previously raised `fts5: syntax error near "="`.
    hits, _total = store.search("Bollier2025=", vault_dir=tmp_path)
    # The query is now phrase-matched; depending on tokenization the trailing
    # `=` may or may not match. The contract being tested is "no exception" —
    # the empty-result case is also acceptable.
    assert isinstance(hits, list)
    # And a normal token search still works.
    hits, total = store.search("commoner", vault_dir=tmp_path)
    assert total == 1
    assert hits[0].id == "ref"


def test_search_handles_url_in_query(store: Store, tmp_path: Path) -> None:
    """A pasted URL must not crash search either."""
    hits, _total = store.search("https://example.com/foo", vault_dir=tmp_path)
    assert isinstance(hits, list)


def test_search_handles_everyday_punctuation(store: Store, tmp_path: Path) -> None:
    """Ordinary punctuation must never surface an fts5 syntax error (M9).

    The old sanitizer blacklisted only `=<>*():"^+-`, so apostrophes, dots,
    question marks, commas, etc. reached the MATCH parser raw and raised.
    The whitelist quotes any token that is not a pure FTS5 bareword.
    """
    store.upsert_note(
        _make_note(
            note_id="n1",
            filename="! Don't panic",
            body="The answer to foo.bar is fine. What? Commoner thinking.",
        ),
        path="note/! Don't panic.md",
        body_sha256="x",
    )
    for query in ("don't", "foo.bar", "what?", "CiteKey.", "a,b", "semi;colon", "don't*"):
        hits, _total = store.search(query, vault_dir=tmp_path)
        assert isinstance(hits, list), f"query {query!r} should not raise"
    # Quoted-phrase matching still finds real content.
    hits, total = store.search("don't", vault_dir=tmp_path)
    assert total == 1
    assert hits[0].id == "n1"
    # A trailing `*` keeps its prefix-search meaning for bareword tokens.
    hits, total = store.search("commo*", vault_dir=tmp_path)
    assert total == 1


def test_find_by_filename_prefix_escapes_like_wildcards(store: Store) -> None:
    """`%` / `_` in a prefix match literally, not as LIKE wildcards."""
    store.upsert_note(
        _make_note(note_id="n1", filename="! Plain name", body=""),
        path="note/! Plain name.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(note_id="n2", filename="! 100% literal", body=""),
        path="note/! 100% literal.md",
        body_sha256="2",
    )
    # A bare `%` prefix must not match every note.
    assert store.find_by_filename_prefix("%") == []
    assert store.find_by_filename_prefix("_") == []
    # But it still matches itself literally.
    matches = store.find_by_filename_prefix("! 100%")
    assert [m["id"] for m in matches] == ["n2"]


def test_delete_note_only_if_path_spares_repointed_row(store: Store) -> None:
    """Conditional hard-delete skips a row whose path changed since the snapshot (M8)."""
    note = _make_note(note_id="n1", filename="! Racer", body="x")
    store.upsert_note(note, path="note/! Racer.md", body_sha256="abc")

    # Snapshot saw a path that a concurrent writer has since re-pointed.
    store.delete_note("n1", only_if_path="note/! Old path.md")
    assert store.find_by_id("n1") is not None

    # Matching path → the delete goes through.
    store.delete_note("n1", only_if_path="note/! Racer.md")
    assert store.find_by_id("n1") is None


def test_wikilinks_target_title_index_exists(store: Store) -> None:
    """v12 adds an index serving the target_title-only hot paths."""
    row = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_wikilinks_target_title'"
    ).fetchone()
    assert row is not None
    assert store.schema_version == SCHEMA_VERSION


def test_open_failure_closes_connection_and_names_cache_dir(tmp_path: Path) -> None:
    """`Store.open` must not leak the sqlite connection when the schema-newer
    guard fires, and its remediation text must point at the v0.2 cache-dir
    layout (`knoten config path`), not the retired `.knoten-state/`."""
    import pytest

    from knoten.repositories.errors import StoreError

    db_path = tmp_path / "future.sqlite"
    with Store(db_path) as seed:
        seed.set_meta("schema_version", str(SCHEMA_VERSION + 1))

    store = Store(db_path)
    with pytest.raises(StoreError) as excinfo:
        store.open()
    assert store._conn is None, "connection must be closed after a failed open"
    assert "knoten config path" in str(excinfo.value)
    assert ".knoten-state" not in str(excinfo.value)


def test_search_empty_query_returns_nothing(store: Store, tmp_path: Path) -> None:
    """Empty string and whitespace-only queries are short-circuited safely."""
    assert store.search("", vault_dir=tmp_path) == ([], 0)
    assert store.search("   ", vault_dir=tmp_path) == ([], 0)


def test_v8_to_v9_renames_trashed_notes_permissions(tmp_path: Path) -> None:
    """A v8 DB with `mcp_permissions` on `trashed_notes` migrates cleanly.

    Mirrors the schema state real users had after upgrading from v7 to v8: the
    `notes` rename happened, but `trashed_notes` was missed because its
    `CREATE TABLE IF NOT EXISTS` clause is a no-op for an existing table.
    Without this migration step, `knoten delete` crashes with
    `table trashed_notes has no column named permissions`.
    """
    db_path = tmp_path / "index.sqlite"
    with sqlite3.connect(db_path) as conn:
        # Re-seed a v8-shaped trashed_notes with the OLD column name. Other
        # tables are seeded with the post-rename name; we only care about the
        # column on trashed_notes for this regression.
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
                permissions       TEXT NOT NULL DEFAULT 'ALL',
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            );
            CREATE TABLE trashed_notes (
                id                TEXT PRIMARY KEY,
                filename          TEXT NOT NULL,
                title             TEXT NOT NULL,
                family            TEXT NOT NULL,
                kind              TEXT NOT NULL,
                source            TEXT,
                original_path     TEXT NOT NULL,
                trash_path        TEXT NOT NULL,
                frontmatter_json  TEXT NOT NULL DEFAULT '{}',
                body_sha256       TEXT NOT NULL,
                mcp_permissions   TEXT NOT NULL DEFAULT 'ALL',
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                deleted_at        TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE notes_fts USING fts5(
                note_id UNINDEXED, title, body, filename,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute("INSERT INTO sync_meta(key, value) VALUES('schema_version', '8')")
        conn.commit()

    with Store(db_path) as migrated:
        assert migrated.get_meta("schema_version") == str(SCHEMA_VERSION)
        columns = {
            row[1] for row in migrated.conn.execute("PRAGMA table_info(trashed_notes)").fetchall()
        }
        assert "permissions" in columns
        assert "mcp_permissions" not in columns


def test_v8_without_permissions_column_gets_added(tmp_path: Path) -> None:
    """An older fixture lacking *both* names gets `permissions` added defensively."""
    db_path = tmp_path / "index.sqlite"
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
                permissions       TEXT NOT NULL DEFAULT 'ALL',
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            );
            CREATE TABLE trashed_notes (
                id                TEXT PRIMARY KEY,
                filename          TEXT NOT NULL,
                title             TEXT NOT NULL,
                family            TEXT NOT NULL,
                kind              TEXT NOT NULL,
                source            TEXT,
                original_path     TEXT NOT NULL,
                trash_path        TEXT NOT NULL,
                frontmatter_json  TEXT NOT NULL DEFAULT '{}',
                body_sha256       TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                deleted_at        TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE notes_fts USING fts5(
                note_id UNINDEXED, title, body, filename,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute("INSERT INTO sync_meta(key, value) VALUES('schema_version', '8')")
        conn.commit()

    with Store(db_path) as migrated:
        columns = {
            row[1] for row in migrated.conn.execute("PRAGMA table_info(trashed_notes)").fetchall()
        }
        assert "permissions" in columns


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


def _make_reference(*, note_id: str, citekey: str, title: str) -> Note:
    """A reference note whose `source` column holds its CiteKey."""
    filename = f"{citekey}= {title}"
    return Note(
        id=note_id,
        filename=filename,
        title=title,
        family="reference",
        kind="book",
        source=citekey,
        body="",
        frontmatter={"family": "reference", "kind": "book", "source": citekey},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


def test_distinct_citekeys_sorted_and_deduplicated(store: Store) -> None:
    # Two notes share a CiteKey (a reference + its file note, say) — distinct.
    store.upsert_note(
        _make_reference(note_id="r1", citekey="Scott2019", title="Radical Candor"),
        path="literature/Scott2019= Radical Candor.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_reference(note_id="r2", citekey="Alice2026", title="One"),
        path="literature/Alice2026= One.md",
        body_sha256="2",
    )
    store.upsert_note(
        _make_reference(note_id="r3", citekey="Alice2026a", title="Two"),
        path="literature/Alice2026a= Two.md",
        body_sha256="3",
    )
    # A note with no source (an empty-string source) is excluded.
    store.upsert_note(
        _make_note(note_id="n4", filename="! Plain", body=""),
        path="note/! Plain.md",
        body_sha256="4",
    )
    assert store.distinct_citekeys() == ["Alice2026", "Alice2026a", "Scott2019"]


def test_distinct_citekeys_prefix_filter(store: Store) -> None:
    for note_id, citekey in (("a", "Alice2026"), ("b", "Alice2026a"), ("c", "Scott2019")):
        store.upsert_note(
            _make_reference(note_id=note_id, citekey=citekey, title="t"),
            path=f"literature/{citekey}= t.md",
            body_sha256=note_id,
        )
    assert store.distinct_citekeys(prefix="Alice2026") == ["Alice2026", "Alice2026a"]
    # Case-sensitive: a lowercased prefix matches nothing.
    assert store.distinct_citekeys(prefix="alice2026") == []


def test_distinct_citekeys_empty_vault(store: Store) -> None:
    assert store.distinct_citekeys() == []


# ---- snippet fallback (finding 1) ---------------------------------------


def test_snippet_fallback_recovers_first_nonempty_line() -> None:
    from knoten.repositories.store import _snippet_fallback

    assert _snippet_fallback("") == ""
    assert _snippet_fallback("\n\n") == ""
    assert _snippet_fallback("   \n \nFirst real line.\nSecond.") == "First real line."
    # Trimmed and truncated to ~120 chars, no highlight markers.
    long_line = "word " * 60
    out = _snippet_fallback(f"\n\n{long_line}")
    assert len(out) <= 120
    assert "<<" not in out and ">>" not in out


def test_ranked_search_empty_body_snippet_is_clean(store: Store, tmp_path: Path) -> None:
    """A title-only match on a body-less note yields "" (not "\\n\\n")."""
    store.upsert_note(
        _make_note(note_id="n1", filename="! Zephyr protocol", body=""),
        path="note/! Zephyr protocol.md",
        body_sha256="1",
    )
    hits, total = store.search("zephyr", vault_dir=tmp_path)
    assert total == 1
    # Whitespace-only natural snippet is normalised away by the fallback.
    assert hits[0].snippet == ""


def test_fuzzy_snippet_falls_back_to_body_line(store: Store, tmp_path: Path) -> None:
    """A rapidfuzz-only title hit carries no snippet; the fallback recovers one."""
    store.upsert_note(
        _make_note(
            note_id="n1",
            filename="! Encryption handbook",
            body="\n\nSymmetric and asymmetric ciphers explained in depth.",
        ),
        path="note/! Encryption handbook.md",
        body_sha256="1",
    )
    # Typo on the title → rapidfuzz match, no trigram snippet.
    hits, total = store.search_fuzzy("encrpytion", vault_dir=tmp_path)
    assert total >= 1
    assert hits[0].id == "n1"
    assert hits[0].snippet == "Symmetric and asymmetric ciphers explained in depth."


# ---- column-scoped search (finding 5) -----------------------------------


def test_search_in_columns_scopes_to_named_columns(store: Store, tmp_path: Path) -> None:
    # n1: term only in the body. n2: term only in the title.
    store.upsert_note(
        _make_note(note_id="n1", filename="! Alpha note", body="mentions zephyr in the body"),
        path="note/! Alpha note.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(note_id="n2", filename="! Zephyr title", body="nothing relevant here"),
        path="note/! Zephyr title.md",
        body_sha256="2",
    )
    # Unscoped: both match.
    _hits, total = store.search("zephyr", vault_dir=tmp_path)
    assert total == 2
    # Scoped to title: only the title hit.
    hits, total = store.search("zephyr", vault_dir=tmp_path, columns=["title"])
    assert total == 1
    assert hits[0].id == "n2"
    # Scoped to body: only the body hit.
    hits, total = store.search("zephyr", vault_dir=tmp_path, columns=["body"])
    assert total == 1
    assert hits[0].id == "n1"


def test_search_match_any_uses_or_semantics(store: Store, tmp_path: Path) -> None:
    store.upsert_note(
        _make_note(note_id="n1", filename="! Ciphers", body="symmetric ciphers and keys"),
        path="note/! Ciphers.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(note_id="n2", filename="! Routing", body="packets and routers only"),
        path="note/! Routing.md",
        body_sha256="2",
    )
    # Implicit AND of two disjoint terms matches nothing.
    _hits, total_and = store.search("ciphers routers", vault_dir=tmp_path)
    assert total_and == 0
    # OR semantics matches both notes.
    _hits, total_or = store.search("ciphers routers", vault_dir=tmp_path, match_any=True)
    assert total_or == 2


# ---- list date filters (finding 3) --------------------------------------


def _make_dated_note(note_id: str, filename: str, *, created: str, updated: str) -> Note:
    return Note(
        id=note_id,
        filename=filename,
        title=filename.lstrip("! "),
        family="permanent",
        kind="permanent",
        source=None,
        body="",
        frontmatter={},
        created_at=created,
        updated_at=updated,
    )


def test_list_filters_by_updated_after(store: Store) -> None:
    store.upsert_note(
        _make_dated_note(
            "old", "! Old", created="2024-01-01T00:00:00Z", updated="2024-01-05T00:00:00Z"
        ),
        path="note/! Old.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_dated_note(
            "new", "! New", created="2024-01-01T00:00:00Z", updated="2024-03-10T12:00:00Z"
        ),
        path="note/! New.md",
        body_sha256="2",
    )
    notes, total = store.list_notes(updated_after="2024-02-01")
    assert total == 1
    assert notes[0].id == "new"


def test_list_filters_by_created_after_is_inclusive(store: Store) -> None:
    store.upsert_note(
        _make_dated_note(
            "a", "! A", created="2024-01-02T00:00:00Z", updated="2024-01-02T00:00:00Z"
        ),
        path="note/! A.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_dated_note(
            "b", "! B", created="2024-01-01T00:00:00Z", updated="2024-01-01T00:00:00Z"
        ),
        path="note/! B.md",
        body_sha256="2",
    )
    # A bare date bound includes timestamps on that day (lexicographic >=).
    notes, total = store.list_notes(created_after="2024-01-02")
    assert {n.id for n in notes} == {"a"}
    assert total == 1


# ---- similar (finding 4) ------------------------------------------------


def test_find_similar_returns_related_notes_excluding_self(store: Store, tmp_path: Path) -> None:
    from knoten.services.notes import find_similar

    store.upsert_note(
        _make_note(
            note_id="seed",
            filename="! Encryption basics",
            body="Symmetric encryption uses shared keys and ciphers for confidentiality.",
        ),
        path="note/! Encryption basics.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note(
            note_id="related",
            filename="! Cipher design",
            body="Block ciphers and stream ciphers both rely on keys for encryption.",
        ),
        path="note/! Cipher design.md",
        body_sha256="2",
    )
    store.upsert_note(
        _make_note(
            note_id="unrelated",
            filename="! Gardening log",
            body="Tomatoes need sunlight and water in the summer months.",
        ),
        path="note/! Gardening log.md",
        body_sha256="3",
    )
    payload = find_similar(store, tmp_path, "! Encryption basics", limit=10)
    assert payload["target_id"] == "seed"
    assert payload["target_filename"] == "! Encryption basics"
    assert payload["derived_query"]
    ids = {hit["id"] for hit in payload["hits"]}
    assert "seed" not in ids  # the note itself is excluded
    assert "related" in ids
    assert payload["total"] == len(payload["hits"])


def test_find_similar_empty_body_no_terms(store: Store, tmp_path: Path) -> None:
    from knoten.services.notes import find_similar

    # A note whose title tokens are all too short / stopwords and body empty.
    store.upsert_note(
        _make_note(note_id="bare", filename="! a", body="", title="a"),
        path="note/! a.md",
        body_sha256="1",
    )
    payload = find_similar(store, tmp_path, "! a", limit=10)
    assert payload["derived_query"] == ""
    assert payload["total"] == 0
    assert payload["hits"] == []
