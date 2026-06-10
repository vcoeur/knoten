"""Tag-edit case handling — `--remove-tag` strips any case variant.

Covers both body-rewrite helpers that back `knoten edit --remove-tag`:
`notes._compose_body` (remote path) and `local_backend._apply_tag_edits`
(local path). Tags are case-folded everywhere else (parser + store), so the
body rewrite must match every case variant of the tag literal.
"""

from __future__ import annotations

from knoten.repositories.local_backend import _apply_tag_edits
from knoten.services.notes import _compose_body


def test_compose_body_remove_tag_is_case_insensitive() -> None:
    body = "Note text #Foo and #FOO and #foo here."
    result = _compose_body(body, add_tags=[], remove_tags=["foo"])
    assert "#Foo" not in result
    assert "#FOO" not in result
    assert "#foo" not in result


def test_compose_body_add_tag_skips_existing_case_variant() -> None:
    body = "Existing #Foo tag."
    result = _compose_body(body, add_tags=["foo"], remove_tags=[])
    # Already present (case-insensitively) — no duplicate appended.
    assert result.count("#") == 1


def test_apply_tag_edits_remove_tag_is_case_insensitive() -> None:
    body = "Local body #Bar #BAR #bar end."
    result = _apply_tag_edits(body, add_tags=(), remove_tags=("bar",))
    assert "#Bar" not in result
    assert "#BAR" not in result
    assert "#bar" not in result
