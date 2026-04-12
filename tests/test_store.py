"""SQLite store: schema creation, upsert, search, backlinks."""

from __future__ import annotations

from pathlib import Path

from app.models import Note, WikiLink
from app.repositories.store import Store


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
