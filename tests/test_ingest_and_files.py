"""ingest_note writes files on disk and upserts into the store in one pass."""

from __future__ import annotations

from knoten.models import Note, WikiLink
from knoten.repositories.store import Store
from knoten.services.notes import ingest_note
from knoten.settings import Settings


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
    relative_path = ingest_note(note, store=store, vault_dir=tmp_settings.paths.vault_dir)

    assert relative_path == "note/! Core idea.md"
    written = tmp_settings.paths.vault_dir / relative_path
    assert written.exists()
    content = written.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "kind: permanent" in content
    assert "Body of the note" in content


def test_rename_removes_old_path(tmp_settings: Settings, store: Store) -> None:
    first = _note("! Core idea", "permanent", "First body.")
    old_path = ingest_note(first, store=store, vault_dir=tmp_settings.paths.vault_dir)
    assert (tmp_settings.paths.vault_dir / old_path).exists()

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
        vault_dir=tmp_settings.paths.vault_dir,
        previous_path=old_path,
    )
    assert new_path == "note/! Core insight.md"
    assert (tmp_settings.paths.vault_dir / new_path).exists()
    assert not (tmp_settings.paths.vault_dir / old_path).exists()
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
    path = ingest_note(note, store=store, vault_dir=tmp_settings.paths.vault_dir)
    assert path == "journal/2024-11/2024-11-10 Weekly review.md"


# ── strip_frontmatter round-trip (M1 regression) ─────────────────────────


def test_strip_frontmatter_consumes_separator_blank_line() -> None:
    """render → strip round-trips the body without a leading blank line.

    The old per-module copies returned everything after the closing `---`
    line including the separator blank line render adds, so every
    read-modify-write cycle grew the body by one leading newline.
    """
    from knoten.repositories.vault_files import render_note_markdown, strip_frontmatter

    note = _note("! Round trip", "permanent", "First line.\n\nSecond paragraph.")
    rendered = render_note_markdown(note)
    assert strip_frontmatter(rendered) == "First line.\n\nSecond paragraph.\n"
    # Idempotent under repeated render/strip cycles — no newline accumulation.
    body = strip_frontmatter(rendered)
    for _ in range(3):
        cycled = render_note_markdown(
            Note(
                id=note.id,
                filename=note.filename,
                title=note.title,
                family=note.family,
                kind=note.kind,
                source=None,
                body=body,
                frontmatter=note.frontmatter,
                tags=(),
                wikilinks=(),
                created_at=note.created_at,
                updated_at=note.updated_at,
            )
        )
        body = strip_frontmatter(cycled)
    assert body == "First line.\n\nSecond paragraph.\n"


def test_strip_frontmatter_preserves_body_leading_blank_line() -> None:
    from knoten.repositories.vault_files import strip_frontmatter

    text = "---\ntitle: x\n---\n\n\nBody starting after a blank line.\n"
    assert strip_frontmatter(text) == "\nBody starting after a blank line.\n"


def test_strip_frontmatter_tolerates_crlf() -> None:
    from knoten.repositories.vault_files import strip_frontmatter

    text = "---\r\ntitle: x\r\n---\r\n\r\nCRLF body.\r\n"
    assert strip_frontmatter(text) == "CRLF body.\r\n"


def test_strip_frontmatter_passthrough_without_block() -> None:
    from knoten.repositories.vault_files import strip_frontmatter

    assert strip_frontmatter("no frontmatter here\n") == "no frontmatter here\n"
    assert strip_frontmatter("---\nunclosed block") == "---\nunclosed block"


def test_render_rejects_control_characters_in_frontmatter_values() -> None:
    """A frontmatter value with a newline must be refused, not emitted raw.

    A raw newline breaks the single-line YAML scalar, and a crafted value
    containing `\\n---\\n` would shift the fence `strip_frontmatter` cuts at,
    leaking frontmatter into the body.
    """
    import pytest

    from knoten.repositories.errors import FrontmatterValidationError, UserError
    from knoten.repositories.vault_files import render_note_markdown

    for hostile in ("multi\nline", "carriage\rreturn", "fence\n---\nbreak", "\x00nul"):
        note = _note("! Hostile fm", "permanent", "body")
        bad = Note(
            id=note.id,
            filename=note.filename,
            title=note.title,
            family=note.family,
            kind=note.kind,
            source=None,
            body=note.body,
            frontmatter={"crafted": hostile},
            tags=(),
            wikilinks=(),
            created_at=note.created_at,
            updated_at=note.updated_at,
        )
        with pytest.raises(UserError, match="control character") as excinfo:
            render_note_markdown(bad)
        # The writer's error names the note (id + filename) and carries the
        # offending key, so interactive writes fail loudly with context and
        # sync can skip per-note.
        assert isinstance(excinfo.value, FrontmatterValidationError)
        assert excinfo.value.key == "crafted"
        assert excinfo.value.note_id == bad.id
        assert excinfo.value.filename == "! Hostile fm"
        assert bad.id in str(excinfo.value)
        assert "! Hostile fm" in str(excinfo.value)

    # List items are checked too.
    listed = _note("! Hostile list", "permanent", "body")
    bad_list = Note(
        id=listed.id,
        filename=listed.filename,
        title=listed.title,
        family=listed.family,
        kind=listed.kind,
        source=None,
        body=listed.body,
        frontmatter={"items": ["fine", "not\nfine"]},
        tags=(),
        wikilinks=(),
        created_at=listed.created_at,
        updated_at=listed.updated_at,
    )
    with pytest.raises(UserError, match="control character"):
        render_note_markdown(bad_list)


def test_ingest_hash_matches_reread_of_mirror_file(tmp_settings: Settings, store: Store) -> None:
    """body_sha256 is recorded over the round-trip body, so a clean re-read
    of the mirror file hashes to the stored value (verify --hashes converges).
    """
    import hashlib

    from knoten.repositories.vault_files import strip_frontmatter

    note = _note("! Hash check", "permanent", "Body without trailing newline")
    relative_path = ingest_note(note, store=store, vault_dir=tmp_settings.paths.vault_dir)
    on_disk = (tmp_settings.paths.vault_dir / relative_path).read_text(encoding="utf-8")
    disk_sha = hashlib.sha256(strip_frontmatter(on_disk).encode("utf-8")).hexdigest()
    row = store.get_row(note.id)
    assert row is not None
    assert disk_sha == row.body_sha256
