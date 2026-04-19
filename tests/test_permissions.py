"""`permissions` end-to-end: mapper, store, schema migration, pre-check.

Covers:
  * `summary_from_api` / `note_from_api` pick up camelCase `permissions`.
  * `Store.upsert_note` / `upsert_placeholder` persist the field.
  * Schema migration from v1 and v2 to v3 keeps existing rows and defaults
    their level to `ALL`.
  * `_append_permission_filter` min/max rejects unknown levels and filters
    correctly.
  * `_assert_permission` (the client-side pre-check) fast-fails with a
    `PermissionError` below the required level and passes with `--force`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from knoten.models import PERMISSIONS, Note, NoteSummary, permission_at_least
from knoten.repositories.errors import PermissionError as LocalPermissionError
from knoten.repositories.errors import UserError
from knoten.repositories.store import SCHEMA_VERSION, Store
from knoten.services.note_mapper import note_from_api, summary_from_api
from knoten.services.notes import _assert_permission


def _make_note(note_id: str, filename: str, level: str = "ALL") -> Note:
    return Note(
        id=note_id,
        filename=filename,
        title=filename.lstrip("!@$%&-=.+ "),
        family="permanent",
        kind="permanent",
        source=None,
        body="body",
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        permissions=level,
    )


# ── mapper ──────────────────────────────────────────────────────────────


def test_summary_from_api_picks_up_permissions() -> None:
    payload = {
        "id": "a",
        "filename": "! Locked",
        "title": "Locked",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "tags": [],
        "permissions": "READ",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }
    summary = summary_from_api(payload)
    assert summary.permissions == "READ"


def test_summary_from_api_defaults_to_all_when_missing() -> None:
    payload = {
        "id": "a",
        "filename": "! Open",
        "title": "Open",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "tags": [],
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }
    summary = summary_from_api(payload)
    assert summary.permissions == "ALL"


def test_note_from_api_picks_up_permissions() -> None:
    payload = {
        "id": "b",
        "filename": "! Append only",
        "title": "Append only",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "body": "x",
        "frontmatter": {},
        "tags": [],
        "linkMap": {},
        "permissions": "APPEND",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
    }
    note = note_from_api(payload)
    assert note.permissions == "APPEND"


# ── rank helper ─────────────────────────────────────────────────────────


def test_permission_at_least_is_monotonic() -> None:
    for required in PERMISSIONS:
        for level in PERMISSIONS:
            got = permission_at_least(level, required)
            expected = PERMISSIONS.index(level) >= PERMISSIONS.index(required)
            assert got is expected, f"{level} >= {required} expected {expected}, got {got}"


def test_permission_at_least_defaults_unknown_to_all() -> None:
    # Defensive default: an unknown level is treated as permissive so the
    # server remains the final gate.
    assert permission_at_least("CUSTOM", "WRITE") is True


# ── store persistence ──────────────────────────────────────────────────


def test_upsert_note_persists_permissions(store: Store) -> None:
    store.upsert_note(
        _make_note("n1", "! Locked", level="READ"),
        path="note/! Locked.md",
        body_sha256="abc",
    )
    row = store.find_by_id("n1")
    assert row is not None
    assert row["permissions"] == "READ"

    # Re-upserting with a different level updates the column.
    store.upsert_note(
        _make_note("n1", "! Locked", level="APPEND"),
        path="note/! Locked.md",
        body_sha256="abc",
    )
    row = store.find_by_id("n1")
    assert row["permissions"] == "APPEND"


def test_upsert_placeholder_persists_permissions(store: Store) -> None:
    summary = NoteSummary(
        id="p1",
        filename="! Hidden",
        title="Hidden",
        family="permanent",
        kind="permanent",
        source=None,
        tags=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        permissions="LIST",
    )
    store.upsert_placeholder(summary, path="note/! Hidden.md")
    row = store.find_by_id("p1")
    assert row is not None
    assert row["permissions"] == "LIST"
    assert row["restricted"] == 1


def test_list_and_search_return_permissions(store: Store, tmp_path: Path) -> None:
    store.upsert_note(
        _make_note("n1", "! Full", level="ALL"),
        path="note/! Full.md",
        body_sha256="1",
    )
    store.upsert_note(
        _make_note("n2", "! Append", level="APPEND"),
        path="note/! Append.md",
        body_sha256="2",
    )
    summaries, _ = store.list_notes()
    levels = {s.id: s.permissions for s in summaries}
    assert levels == {"n1": "ALL", "n2": "APPEND"}

    hits, _ = store.search("Full OR Append", vault_dir=tmp_path)
    hit_levels = {hit.id: hit.permissions for hit in hits}
    assert hit_levels == {"n1": "ALL", "n2": "APPEND"}


def test_list_notes_min_permission_filter(store: Store) -> None:
    for note_id, filename, level in [
        ("a", "! A", "LIST"),
        ("b", "! B", "READ"),
        ("c", "! C", "APPEND"),
        ("d", "! D", "ALL"),
    ]:
        store.upsert_note(
            _make_note(note_id, filename, level=level),
            path=f"note/{filename}.md",
            body_sha256=note_id,
        )
    summaries, total = store.list_notes(min_permission="APPEND")
    ids = {s.id for s in summaries}
    assert ids == {"c", "d"}
    assert total == 2


def test_list_notes_max_permission_filter(store: Store) -> None:
    for note_id, filename, level in [
        ("a", "! A", "LIST"),
        ("b", "! B", "READ"),
        ("c", "! C", "APPEND"),
        ("d", "! D", "ALL"),
    ]:
        store.upsert_note(
            _make_note(note_id, filename, level=level),
            path=f"note/{filename}.md",
            body_sha256=note_id,
        )
    summaries, total = store.list_notes(max_permission="READ")
    ids = {s.id for s in summaries}
    assert ids == {"a", "b"}
    assert total == 2


def test_permission_filter_rejects_unknown_level(store: Store) -> None:
    with pytest.raises(UserError, match="--min-permission"):
        store.list_notes(min_permission="BANANA")
    with pytest.raises(UserError, match="--max-permission"):
        store.list_notes(max_permission="BANANA")


# ── schema migration v1/v2 → v3 ────────────────────────────────────────


def test_schema_version_is_v8() -> None:
    assert SCHEMA_VERSION == 8


def _seed_v1_database(path: Path) -> None:
    """Build a SQLite file at the v1 schema (no `restricted`, no `permissions`)."""
    conn = sqlite3.connect(str(path))
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
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        CREATE TABLE sync_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO sync_meta (key, value) VALUES ('schema_version', '1');
        INSERT INTO notes (
            id, filename, title, family, kind, source, path,
            frontmatter_json, body_sha256, created_at, updated_at
        ) VALUES (
            'legacy-1', '! Legacy', 'Legacy', 'permanent', 'permanent', NULL,
            'note/! Legacy.md', '{}', 'abc', '2020-01-01T00:00:00Z',
            '2020-01-02T00:00:00Z'
        );
        """
    )
    conn.commit()
    conn.close()


def test_schema_migration_from_v1_adds_permissions_with_all_default(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite"
    _seed_v1_database(db_path)

    with Store(db_path) as store:
        # Opening should auto-migrate from v1 up to the current schema.
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
        row = store.find_by_id("legacy-1")
        assert row is not None
        assert row["permissions"] == "ALL"
        assert row["restricted"] == 0  # added by v2 migration

        # Subsequent upserts overwrite the default.
        store.upsert_note(
            _make_note("legacy-1", "! Legacy", level="READ"),
            path="note/! Legacy.md",
            body_sha256="new",
        )
        row = store.find_by_id("legacy-1")
        assert row["permissions"] == "READ"


def _seed_v7_database(path: Path) -> None:
    """Build a SQLite file at the v7 schema shape (historical `mcp_permissions` column)."""
    conn = sqlite3.connect(str(path))
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
            updated_at        TEXT NOT NULL,
            path_mtime_ns     INTEGER NOT NULL DEFAULT 0,
            path_size         INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE sync_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO sync_meta (key, value) VALUES ('schema_version', '7');
        INSERT INTO notes (
            id, filename, title, family, kind, source, path,
            frontmatter_json, body_sha256, restricted, mcp_permissions,
            created_at, updated_at, path_mtime_ns, path_size
        ) VALUES (
            'legacy-7', '! Legacy', 'Legacy', 'permanent', 'permanent', NULL,
            'note/! Legacy.md', '{}', 'abc', 0, 'READ',
            '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z', 0, 0
        );
        """
    )
    conn.commit()
    conn.close()


def test_schema_migration_from_v7_renames_mcp_permissions_to_permissions(tmp_path: Path) -> None:
    """v7 -> v8 renames `notes.mcp_permissions` to `notes.permissions`; data survives."""
    db_path = tmp_path / "index.sqlite"
    _seed_v7_database(db_path)

    with Store(db_path) as store:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(notes)").fetchall()}
        assert "mcp_permissions" not in columns
        assert "permissions" in columns

        row = store.find_by_id("legacy-7")
        assert row is not None
        assert row["permissions"] == "READ"


# ── client-side pre-check ──────────────────────────────────────────────


def test_assert_permission_passes_when_level_is_sufficient() -> None:
    row = {"id": "n", "filename": "! X", "permissions": "WRITE"}
    # Should not raise.
    _assert_permission(row, required_level="WRITE", operation="edit", force=False)
    _assert_permission(row, required_level="APPEND", operation="append", force=False)


def test_assert_permission_fails_when_level_is_insufficient() -> None:
    row = {"id": "n", "filename": "! X", "permissions": "READ"}
    with pytest.raises(LocalPermissionError) as exc_info:
        _assert_permission(row, required_level="WRITE", operation="edit", force=False)
    assert exc_info.value.current_level == "READ"
    assert exc_info.value.required_level == "WRITE"
    assert exc_info.value.operation == "edit"


def test_assert_permission_force_bypasses_check() -> None:
    row = {"id": "n", "filename": "! X", "permissions": "READ"}
    # Should not raise despite the low level.
    _assert_permission(row, required_level="ALL", operation="delete", force=True)


def test_assert_permission_defaults_missing_column_to_all() -> None:
    row = {"id": "n", "filename": "! X"}
    _assert_permission(row, required_level="ALL", operation="delete", force=False)
