"""LocalBackend rename cascade — Phase 6b.

Mirrors the server-side cascade tests in notes.vcoeur.com's
`notes.test.ts` so that `kasten rename` behaves identically whether the
backend is `LocalBackend` or `RemoteBackend`.
"""

from __future__ import annotations

import pytest

from app.repositories.backend import NoteDraft, NotePatch
from app.repositories.errors import UserError
from app.repositories.local_backend import LocalBackend
from app.settings import Settings


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


def test_rename_heading_wikilink_is_not_cascaded(tmp_settings: Settings) -> None:
    """Heading-form wikilinks (`[[target#heading]]`) are not tracked by the
    local markdown parser, so the cascade cannot find them by querying the
    wikilinks table. Documented limitation — the source body retains the
    old filename until the user edits it themselves. If this starts biting,
    fix by teaching `markdown_parser._WIKILINK_RE` to capture the heading
    suffix and storing it under a canonicalised `target_title` that matches
    the plain form.
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
        # Stale — limitation documented above.
        assert "[[! Target#heading]]" in refreshed_source.body


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
