"""LocalBackend rename cascade — Phase 6b.

Mirrors the server-side cascade tests in notes.vcoeur.com's
`notes.test.ts` so that `knoten rename` behaves identically whether the
backend is `LocalBackend` or `RemoteBackend`.
"""

from __future__ import annotations

import pytest

from knoten.repositories.backend import NoteDraft, NotePatch
from knoten.repositories.errors import UserError
from knoten.repositories.local_backend import LocalBackend
from knoten.settings import Settings


def _create(backend: LocalBackend, filename: str, body: str = "") -> str:
    return backend.create_note(NoteDraft(filename=filename, body=body))


def test_rename_rewrites_incoming_wikilinks_in_bodies(tmp_settings: Settings) -> None:
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "target body")
        source_id = _create(
            backend,
            "- Source",
            "See [[! Target]] for context.",
        )

        result = backend.update_note(target_id, NotePatch(filename="! Target Renamed"))
        assert result.note_id == target_id
        assert result.affected_notes == (source_id,)

        refreshed_source = backend.read_note(source_id)
        assert "[[! Target Renamed]]" in refreshed_source.body
        assert "[[! Target]]" not in refreshed_source.body

        refreshed_target = backend.read_note(target_id)
        assert refreshed_target.filename == "! Target Renamed"


def test_rename_with_no_incoming_links_returns_empty_affected(tmp_settings: Settings) -> None:
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Orphan", "no one points here")
        result = backend.update_note(target_id, NotePatch(filename="! Orphan Renamed"))
        assert result.affected_notes == ()

        refreshed = backend.read_note(target_id)
        assert refreshed.filename == "! Orphan Renamed"


def test_rename_handles_alias_wikilink_form(tmp_settings: Settings) -> None:
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "")
        source_id = _create(
            backend,
            "- Source",
            "See [[! Target|friendly label]] for details.",
        )

        backend.update_note(target_id, NotePatch(filename="! Target Renamed"))
        refreshed_source = backend.read_note(source_id)
        assert "[[! Target Renamed|friendly label]]" in refreshed_source.body
        assert "[[! Target|" not in refreshed_source.body


def test_rename_heading_wikilink_is_cascaded(tmp_settings: Settings) -> None:
    """Heading-form wikilinks (`[[target#heading]]`) are now parsed to their
    slug and tracked in the wikilinks table, so the cascade rewrites the slug
    while preserving the `#heading` suffix verbatim — matching the server.
    """
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "")
        source_id = _create(
            backend,
            "- Source",
            "See [[! Target#heading]] for details.",
        )

        backend.update_note(target_id, NotePatch(filename="! Target Renamed"))
        refreshed_source = backend.read_note(source_id)
        assert "[[! Target Renamed#heading]]" in refreshed_source.body
        assert "[[! Target#heading]]" not in refreshed_source.body


def test_rename_rewrites_case_variant_wikilink(tmp_settings: Settings) -> None:
    """A source that wrote a case variant of the filename is still rewritten —
    the wikilinks lookup and the body regex are both case-insensitive.
    """
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "")
        source_id = _create(
            backend,
            "- Source",
            "See [[! target]] for details.",
        )

        backend.update_note(target_id, NotePatch(filename="! Target Renamed"))
        refreshed_source = backend.read_note(source_id)
        assert "[[! Target Renamed]]" in refreshed_source.body
        assert "[[! target]]" not in refreshed_source.body


def test_rename_rewrites_whitespace_padded_wikilink(tmp_settings: Settings) -> None:
    """Whitespace around the slug (`[[  ! Target  ]]`) is tolerated and
    normalised away; the rewrite anchors on the trimmed slug.
    """
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "")
        source_id = _create(
            backend,
            "- Source",
            "See [[  ! Target  ]] and [[ ! Target #heading]] for details.",
        )

        backend.update_note(target_id, NotePatch(filename="! Target Renamed"))
        refreshed_source = backend.read_note(source_id)
        assert "[[! Target Renamed]]" in refreshed_source.body
        assert "[[! Target Renamed#heading]]" in refreshed_source.body
        assert "! Target  ]]" not in refreshed_source.body


def test_rename_does_not_rewrite_other_slug_with_old_in_heading(tmp_settings: Settings) -> None:
    """`[[other#old]]` resolves to slug `other`, not the renamed `old`, so it
    must be left untouched — only the slug position is matched, not the anchor.
    """
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "")
        _create(backend, "! Other", "")
        source_id = _create(
            backend,
            "- Source",
            "See [[! Other#! Target]] for details.",
        )

        backend.update_note(target_id, NotePatch(filename="! Target Renamed"))
        refreshed_source = backend.read_note(source_id)
        assert "[[! Other#! Target]]" in refreshed_source.body
        assert "Renamed" not in refreshed_source.body


def test_rename_to_colliding_filename_raises(tmp_settings: Settings) -> None:
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! First", "")
        _create(backend, "! Second", "")

        with pytest.raises(UserError, match="already uses"):
            backend.update_note(target_id, NotePatch(filename="! Second"))

        refreshed = backend.read_note(target_id)
        assert refreshed.filename == "! First"


def test_rename_refuses_family_prefix_change(tmp_settings: Settings) -> None:
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Fleeting-style", "")
        with pytest.raises(UserError, match="Family prefix"):
            backend.update_note(target_id, NotePatch(filename="@ Not Allowed"))


def test_rename_does_not_match_substring_filenames(tmp_settings: Settings) -> None:
    with LocalBackend(tmp_settings) as backend:
        short_id = _create(backend, "! A", "")
        long_id = _create(backend, "! A More", "")
        source_id = _create(
            backend,
            "- Refs",
            "Link to [[! A]] and separately to [[! A More]].",
        )

        backend.update_note(short_id, NotePatch(filename="! A Renamed"))
        source = backend.read_note(source_id)
        assert "[[! A Renamed]]" in source.body
        assert "[[! A More]]" in source.body, (
            "Substring rename should not touch the longer filename"
        )

        long_note = backend.read_note(long_id)
        assert long_note.filename == "! A More"


@pytest.mark.parametrize("failing_call", [1, 2])
def test_rename_failure_restores_files_and_store(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, failing_call: int
) -> None:
    """A mid-cascade failure rolls back BOTH file bytes and store rows (M6).

    The old `_rollback` restored file bytes only; the store kept the new
    path, and the next stat walk hard-deleted the row — the note's file
    survived on disk but became permanently invisible to the index.
    Call 1 is the renamed target's mirror write, call 2 the first source
    re-ingest's mirror write.
    """
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "target body")
        source_id = _create(backend, "- Source", "See [[! Target]] for context.")

    vault = tmp_settings.paths.vault_dir
    target_path = vault / "note" / "! Target.md"
    source_path = vault / "note" / "- Source.md"
    original_target_bytes = target_path.read_bytes()
    original_source_bytes = source_path.read_bytes()

    import knoten.services.notes as notes_module

    real_write = notes_module.write_note_file
    calls = {"count": 0}

    def failing_write(vault_dir, relative_path, content):
        calls["count"] += 1
        if calls["count"] == failing_call:
            raise OSError("disk full (simulated)")
        return real_write(vault_dir, relative_path, content)

    monkeypatch.setattr(notes_module, "write_note_file", failing_write)

    with LocalBackend(tmp_settings) as backend:
        with pytest.raises(OSError, match="disk full"):
            backend.update_note(target_id, NotePatch(filename="! Target Renamed"))

    monkeypatch.setattr(notes_module, "write_note_file", real_write)

    # Files: byte-identical to the originals, nothing new left behind.
    assert target_path.read_bytes() == original_target_bytes
    assert source_path.read_bytes() == original_source_bytes
    assert not (vault / "note" / "! Target Renamed.md").exists()

    # Store: rows unchanged and the note still readable through a fresh backend.
    with LocalBackend(tmp_settings) as backend:
        target = backend.read_note(target_id)
        assert target.filename == "! Target"
        assert "target body" in target.body
        source = backend.read_note(source_id)
        assert "[[! Target]]" in source.body
        page = backend.list_note_summaries(limit=10, offset=0)
        assert {summary.id for summary in page.data} == {target_id, source_id}


def test_rename_refuses_unindexed_file_at_destination(tmp_settings: Settings) -> None:
    """Rename must not clobber an on-disk file the store does not know (M7)."""
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "body")

    stray = tmp_settings.paths.vault_dir / "note" / "! Occupied.md"
    stray.write_text("HAND-DROPPED BYTES", encoding="utf-8")

    with LocalBackend(tmp_settings) as backend:
        with pytest.raises(UserError, match="not in the index"):
            backend.update_note(target_id, NotePatch(filename="! Occupied"))
        assert backend.read_note(target_id).filename == "! Target"

    assert stray.read_text(encoding="utf-8") == "HAND-DROPPED BYTES"


def test_rename_to_filename_with_regex_template_chars(tmp_settings: Settings) -> None:
    r"""`\1` / `\g` in the new filename are inserted literally, not expanded.

    The old code interpolated the new filename into the `re.subn` replacement
    template, so `\1` duplicated the matched group and `\g` raised re.error
    mid-cascade.
    """
    with LocalBackend(tmp_settings) as backend:
        target_id = _create(backend, "! Target", "")
        source_id = _create(backend, "- Source", "See [[! Target]] here.")

        backend.update_note(target_id, NotePatch(filename=r"! New \1 name"))
        source = backend.read_note(source_id)
        assert r"[[! New \1 name]]" in source.body
        assert backend.read_note(target_id).filename == r"! New \1 name"
