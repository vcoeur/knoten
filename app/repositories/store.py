"""SQLite + FTS5 local store.

Holds the metadata index that backs every read command. The mirror files on
disk remain the canonical body — this DB is the query accelerator. All writes
go through an explicit transaction, all queries return plain dicts or model
instances from `app.models`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from app.models import MCP_PERMISSIONS, Note, NoteSummary, SearchHit, permission_rank
from app.repositories.errors import NotFoundError, StoreError, UserError

SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_filename ON notes(filename);
CREATE INDEX IF NOT EXISTS idx_notes_family  ON notes(family);
CREATE INDEX IF NOT EXISTS idx_notes_kind    ON notes(kind);
CREATE INDEX IF NOT EXISTS idx_notes_source  ON notes(source);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC);

CREATE TABLE IF NOT EXISTS tags (
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS wikilinks (
    source_id    TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    target_title TEXT NOT NULL,
    target_id    TEXT,
    PRIMARY KEY (source_id, target_title)
);
CREATE INDEX IF NOT EXISTS idx_wikilinks_target ON wikilinks(target_id) WHERE target_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS frontmatter_fields (
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (note_id, name, value)
);
CREATE INDEX IF NOT EXISTS idx_fm_name_value ON frontmatter_fields(name, value);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    note_id UNINDEXED,
    title,
    body,
    filename,
    tokenize='unicode61 remove_diacritics 2'
);

-- Mirror of notes_fts with the trigram tokenizer, used by `search --fuzzy`
-- for substring/partial-word matching. Column order must match notes_fts
-- so bm25() weights stay consistent.
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts_trigram USING fts5(
    note_id UNINDEXED,
    title,
    body,
    filename,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _trigram_query(query: str) -> str:
    """Build an FTS5 query for the trigram tokenizer from free-form input.

    Splits on whitespace, drops tokens shorter than 3 characters (the trigram
    tokenizer cannot match them), and wraps each surviving token as a quoted
    phrase so FTS5 treats it literally. The resulting AND-of-phrases finds
    notes whose columns contain every surviving token as a substring.
    Returns "" if nothing survives — the caller should then skip the pass.
    """
    tokens = [token for token in query.split() if len(token) >= 3]
    if not tokens:
        return ""
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def _append_permission_filter(
    where_clauses: list[str],
    params: list[Any],
    min_permission: str | None,
    max_permission: str | None,
) -> None:
    """Add cumulative min/max permission filters to a WHERE clause list.

    Permissions are an ordered enum (NONE < LIST < READ < APPEND < WRITE < ALL).
    `min_permission='READ'` keeps notes whose level is READ or higher;
    `max_permission='APPEND'` keeps notes whose level is APPEND or lower.
    Unknown levels are rejected with a UserError so the caller sees the
    typo at the CLI boundary rather than getting a silent empty result.
    """
    if min_permission is not None:
        if min_permission not in MCP_PERMISSIONS:
            raise UserError(
                f"--min-permission: '{min_permission}' is not one of {', '.join(MCP_PERMISSIONS)}"
            )
        min_rank = permission_rank(min_permission)
        allowed = [level for level in MCP_PERMISSIONS if permission_rank(level) >= min_rank]
        placeholders = ",".join("?" * len(allowed))
        where_clauses.append(f"n.mcp_permissions IN ({placeholders})")
        params.extend(allowed)
    if max_permission is not None:
        if max_permission not in MCP_PERMISSIONS:
            raise UserError(
                f"--max-permission: '{max_permission}' is not one of {', '.join(MCP_PERMISSIONS)}"
            )
        max_rank = permission_rank(max_permission)
        allowed = [level for level in MCP_PERMISSIONS if permission_rank(level) <= max_rank]
        placeholders = ",".join("?" * len(allowed))
        where_clauses.append(f"n.mcp_permissions IN ({placeholders})")
        params.extend(allowed)


@dataclass
class StoreNoteRow:
    """Raw row as it lives in the `notes` table — used for staleness comparisons."""

    id: str
    filename: str
    path: str
    updated_at: str
    body_sha256: str
    restricted: bool = False
    mcp_permissions: str = "ALL"


class Store:
    """Connection wrapper + repository for the SQLite index.

    Open one per CLI invocation; short-lived. Always use as a context manager
    so the connection is closed even on error.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> Store:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(self._db_path))
        except sqlite3.Error as exc:
            raise StoreError(f"Cannot open {self._db_path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("Store connection is not open")
        return self._conn

    # ---- schema ----------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """Current schema version recorded in sync_meta.

        The authoritative copy lives in the `sync_meta` table (written by
        `_ensure_schema` on migration). The value in `state.json` is a
        human-readable mirror and can drift after an offline migration —
        callers that need the real version should read this property, not
        state.json.
        """
        raw = self._read_meta("schema_version")
        return int(raw) if raw is not None else 0

    def _ensure_schema(self) -> None:
        try:
            self.conn.executescript(_SCHEMA)
            current_raw = self._read_meta("schema_version")
            current = int(current_raw) if current_raw is not None else None
            if current is None:
                self._write_meta("schema_version", str(SCHEMA_VERSION))
            elif current > SCHEMA_VERSION:
                raise StoreError(
                    f"Index schema_version={current} is newer than this KastenManager "
                    f"(expects {SCHEMA_VERSION}). Upgrade the tool or delete .kasten-state/."
                )
            elif current < SCHEMA_VERSION:
                self._migrate_from(current)
                self._write_meta("schema_version", str(SCHEMA_VERSION))
            self.conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"Schema initialisation failed: {exc}") from exc

    def _migrate_from(self, from_version: int) -> None:
        """Apply linear forward migrations. Each step bumps schema_version by 1."""
        if from_version < 2:
            # v1 -> v2: add the `restricted` column for metadata-only placeholders
            # created when the server returns 404 on a note read.
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(notes)").fetchall()}
            if "restricted" not in columns:
                self.conn.execute(
                    "ALTER TABLE notes ADD COLUMN restricted INTEGER NOT NULL DEFAULT 0"
                )
        if from_version < 3:
            # v2 -> v3: add `mcp_permissions` mirroring notes.vcoeur.com's per-note level.
            # Existing rows default to 'ALL'; the next `sync` populates real values.
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(notes)").fetchall()}
            if "mcp_permissions" not in columns:
                self.conn.execute(
                    "ALTER TABLE notes ADD COLUMN mcp_permissions TEXT NOT NULL DEFAULT 'ALL'"
                )
        if from_version < 4:
            # v3 -> v4: backfill the trigram FTS table from the existing
            # unicode61 FTS table. The CREATE VIRTUAL TABLE above already
            # created the (empty) trigram table; populate it row-for-row
            # so `search --fuzzy` works without a full re-sync.
            self.conn.execute("DELETE FROM notes_fts_trigram")
            self.conn.execute(
                """
                INSERT INTO notes_fts_trigram(note_id, title, body, filename)
                SELECT note_id, title, body, filename FROM notes_fts
                """
            )
        if from_version < 5:
            # v4 -> v5: add path_mtime_ns + path_size so LocalBackend can
            # stat-walk the vault and detect external edits. Existing rows
            # get 0, which the walk treats as "never seen" — it re-stats and
            # backfills on the first invocation.
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(notes)").fetchall()}
            if "path_mtime_ns" not in columns:
                self.conn.execute(
                    "ALTER TABLE notes ADD COLUMN path_mtime_ns INTEGER NOT NULL DEFAULT 0"
                )
            if "path_size" not in columns:
                self.conn.execute(
                    "ALTER TABLE notes ADD COLUMN path_size INTEGER NOT NULL DEFAULT 0"
                )

    def _read_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _write_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---- ingest ----------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def upsert_note(
        self,
        note: Note,
        *,
        path: str,
        body_sha256: str,
        path_mtime_ns: int = 0,
        path_size: int = 0,
    ) -> None:
        """Insert or replace a note and its derived rows (tags, wikilinks, FTS).

        Clears the `restricted` flag — this method is only used for notes
        whose body we actually fetched from the remote. For notes we can
        only see in list responses, use `upsert_placeholder`.

        `path_mtime_ns` / `path_size` record the stat of the mirror file
        after it was written, so `LocalBackend._refresh_index_if_stale`
        can detect external edits without re-hashing every file.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO notes (
                    id, filename, title, family, kind, source, path,
                    frontmatter_json, body_sha256, restricted, mcp_permissions,
                    created_at, updated_at, path_mtime_ns, path_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    filename = excluded.filename,
                    title = excluded.title,
                    family = excluded.family,
                    kind = excluded.kind,
                    source = excluded.source,
                    path = excluded.path,
                    frontmatter_json = excluded.frontmatter_json,
                    body_sha256 = excluded.body_sha256,
                    restricted = 0,
                    mcp_permissions = excluded.mcp_permissions,
                    updated_at = excluded.updated_at,
                    path_mtime_ns = excluded.path_mtime_ns,
                    path_size = excluded.path_size
                """,
                (
                    note.id,
                    note.filename,
                    note.title,
                    note.family,
                    note.kind,
                    note.source,
                    path,
                    json.dumps(note.frontmatter, ensure_ascii=False),
                    body_sha256,
                    note.mcp_permissions,
                    note.created_at,
                    note.updated_at,
                    path_mtime_ns,
                    path_size,
                ),
            )

            conn.execute("DELETE FROM tags WHERE note_id = ?", (note.id,))
            if note.tags:
                conn.executemany(
                    "INSERT OR IGNORE INTO tags(note_id, tag) VALUES(?, ?)",
                    [(note.id, tag) for tag in note.tags],
                )

            conn.execute("DELETE FROM wikilinks WHERE source_id = ?", (note.id,))
            if note.wikilinks:
                conn.executemany(
                    "INSERT OR IGNORE INTO wikilinks(source_id, target_title, target_id) "
                    "VALUES(?, ?, ?)",
                    [(note.id, link.target_title, link.target_id) for link in note.wikilinks],
                )

            conn.execute("DELETE FROM frontmatter_fields WHERE note_id = ?", (note.id,))
            scalar_rows: list[tuple[str, str, str]] = []
            for key, value in note.frontmatter.items():
                if isinstance(value, (str, int, float)):
                    scalar_rows.append((note.id, key, str(value)))
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, (str, int, float)):
                            scalar_rows.append((note.id, key, str(item)))
            if scalar_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO frontmatter_fields(note_id, name, value) "
                    "VALUES(?, ?, ?)",
                    scalar_rows,
                )

            conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (note.id,))
            conn.execute(
                "INSERT INTO notes_fts(note_id, title, body, filename) VALUES(?, ?, ?, ?)",
                (note.id, note.title, note.body, note.filename),
            )
            conn.execute("DELETE FROM notes_fts_trigram WHERE note_id = ?", (note.id,))
            conn.execute(
                "INSERT INTO notes_fts_trigram(note_id, title, body, filename) VALUES(?, ?, ?, ?)",
                (note.id, note.title, note.body, note.filename),
            )

    def upsert_placeholder(self, summary: NoteSummary, *, path: str) -> None:
        """Insert a metadata-only row for a note whose body we cannot fetch.

        Used when `GET /api/notes/{id}` returns 404 — either the note is
        restricted (`mcpPermissions = LIST` for a non-web token) or it was
        deleted between the list and the read. Either way, we have the
        summary fields from the list response and want to preserve them
        locally so title search still works and the user sees the note is
        known to exist.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO notes (
                    id, filename, title, family, kind, source, path,
                    frontmatter_json, body_sha256, restricted, mcp_permissions,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '', 1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    filename = excluded.filename,
                    title = excluded.title,
                    family = excluded.family,
                    kind = excluded.kind,
                    source = excluded.source,
                    path = excluded.path,
                    frontmatter_json = '{}',
                    body_sha256 = '',
                    restricted = 1,
                    mcp_permissions = excluded.mcp_permissions,
                    updated_at = excluded.updated_at
                """,
                (
                    summary.id,
                    summary.filename,
                    summary.title,
                    summary.family,
                    summary.kind,
                    summary.source,
                    path,
                    summary.mcp_permissions,
                    summary.created_at,
                    summary.updated_at,
                ),
            )
            # A placeholder has no body-derived tags, wiki-links, or
            # frontmatter fields — drop any stale rows left over from a
            # previous full ingest of this note.
            conn.execute("DELETE FROM tags WHERE note_id = ?", (summary.id,))
            conn.execute("DELETE FROM wikilinks WHERE source_id = ?", (summary.id,))
            conn.execute("DELETE FROM frontmatter_fields WHERE note_id = ?", (summary.id,))
            # Title + filename still go into FTS5 so title search works.
            conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (summary.id,))
            conn.execute(
                "INSERT INTO notes_fts(note_id, title, body, filename) VALUES(?, ?, '', ?)",
                (summary.id, summary.title, summary.filename),
            )
            conn.execute("DELETE FROM notes_fts_trigram WHERE note_id = ?", (summary.id,))
            conn.execute(
                "INSERT INTO notes_fts_trigram(note_id, title, body, filename) VALUES(?, ?, '', ?)",
                (summary.id, summary.title, summary.filename),
            )

    def count_restricted(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM notes WHERE restricted = 1").fetchone()
        return int(row["c"]) if row else 0

    def delete_note(self, note_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
            conn.execute("DELETE FROM notes_fts_trigram WHERE note_id = ?", (note_id,))
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))

    def get_row(self, note_id: str) -> StoreNoteRow | None:
        row = self.conn.execute(
            "SELECT id, filename, path, updated_at, body_sha256, restricted, mcp_permissions "
            "FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if row is None:
            return None
        return StoreNoteRow(
            id=row["id"],
            filename=row["filename"],
            path=row["path"],
            updated_at=row["updated_at"],
            body_sha256=row["body_sha256"],
            restricted=bool(row["restricted"]),
            mcp_permissions=row["mcp_permissions"] or "ALL",
        )

    def all_ids(self) -> set[str]:
        return {row["id"] for row in self.conn.execute("SELECT id FROM notes").fetchall()}

    def record_file_stat(self, note_id: str, *, path_mtime_ns: int, path_size: int) -> None:
        """Record the `(mtime_ns, size)` tuple for a note's mirror file.

        Called by `ingest_note` after the mirror file has been written
        atomically, so the drift detector can tell "this is the content
        we ingested" from "the user edited the file by hand". Idempotent
        — a no-op if the note row does not exist.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE notes SET path_mtime_ns = ?, path_size = ? WHERE id = ?",
                (path_mtime_ns, path_size, note_id),
            )

    def path_index(self) -> dict[str, tuple[int, int, str]]:
        """Return `{relative_path: (mtime_ns, size, note_id)}` for active notes.

        Fed to `LocalBackend._refresh_index_if_stale` so the stat walk can
        detect drifted files in O(n) SQL + O(files) stat calls, without a
        hash pass. Placeholders are skipped — their mirror file is a stub
        and should not participate in drift detection.
        """
        rows = self.conn.execute(
            "SELECT path, path_mtime_ns, path_size, id FROM notes WHERE restricted = 0"
        ).fetchall()
        return {row["path"]: (row["path_mtime_ns"], row["path_size"], row["id"]) for row in rows}

    def apply_drifted_body(
        self,
        note_id: str,
        *,
        body: str,
        body_sha256: str,
        tags: tuple[str, ...],
        wikilink_titles: tuple[str, ...],
        path_mtime_ns: int,
        path_size: int,
    ) -> None:
        """Refresh a note's body + derived rows after an external edit.

        Called by the `LocalBackend` stat walk when a mirror file's
        (mtime, size) has drifted from the recorded values. Updates FTS5,
        tags, and wikilinks (with `target_id = None` — the walk does not
        resolve titles to ids; the next full reindex or explicit write
        does that). Leaves `filename / family / kind / source /
        frontmatter_json` unchanged — only the body changed on disk, not
        the metadata.
        """
        with self.transaction() as conn:
            updated_rows = conn.execute(
                """
                UPDATE notes
                SET body_sha256   = ?,
                    path_mtime_ns = ?,
                    path_size     = ?
                WHERE id = ? AND restricted = 0
                """,
                (body_sha256, path_mtime_ns, path_size, note_id),
            ).rowcount
            if updated_rows == 0:
                return

            title_row = conn.execute(
                "SELECT title, filename FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            if title_row is None:
                return
            title = title_row["title"]
            filename = title_row["filename"]

            conn.execute("DELETE FROM tags WHERE note_id = ?", (note_id,))
            if tags:
                conn.executemany(
                    "INSERT OR IGNORE INTO tags(note_id, tag) VALUES(?, ?)",
                    [(note_id, tag) for tag in tags],
                )

            conn.execute("DELETE FROM wikilinks WHERE source_id = ?", (note_id,))
            if wikilink_titles:
                conn.executemany(
                    "INSERT OR IGNORE INTO wikilinks(source_id, target_title, target_id) "
                    "VALUES(?, ?, NULL)",
                    [(note_id, title_) for title_ in wikilink_titles],
                )

            conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
            conn.execute(
                "INSERT INTO notes_fts(note_id, title, body, filename) VALUES(?, ?, ?, ?)",
                (note_id, title, body, filename),
            )
            conn.execute("DELETE FROM notes_fts_trigram WHERE note_id = ?", (note_id,))
            conn.execute(
                "INSERT INTO notes_fts_trigram(note_id, title, body, filename) VALUES(?, ?, ?, ?)",
                (note_id, title, body, filename),
            )

    def all_rows(self) -> list[StoreNoteRow]:
        """Every active note row, used by sync reconciliation."""
        rows = self.conn.execute(
            "SELECT id, filename, path, updated_at, body_sha256, restricted, mcp_permissions "
            "FROM notes"
        ).fetchall()
        return [
            StoreNoteRow(
                id=row["id"],
                filename=row["filename"],
                path=row["path"],
                updated_at=row["updated_at"],
                body_sha256=row["body_sha256"],
                restricted=bool(row["restricted"]),
                mcp_permissions=row["mcp_permissions"] or "ALL",
            )
            for row in rows
        ]

    def full_row(self, note_id: str) -> dict[str, Any] | None:
        """Raw notes row including `frontmatter_json`.

        Used by `reindex` to rebuild derived tables from stored metadata
        without re-parsing the on-disk file. Callers that only need the
        StoreNoteRow subset should use `get_row()` instead.
        """
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return dict(row) if row else None

    def integrity_check(self) -> str:
        """Run SQLite's `PRAGMA integrity_check`. Returns "ok" or a problem report."""
        rows = self.conn.execute("PRAGMA integrity_check").fetchall()
        lines = [row[0] for row in rows]
        if lines == ["ok"]:
            return "ok"
        return "; ".join(lines)

    def fts_cardinality_check(self) -> dict[str, Any]:
        """Detect `notes` rows missing from `notes_fts` and vice versa.

        Returns a dict with counts and the first-N mismatched note IDs in
        each direction. In a healthy store, both counts and both lists are
        empty. This is the core "is the index consistent with the metadata"
        question. Covers both the primary notes_fts (unicode61) and the
        trigram mirror used by `search --fuzzy`.
        """
        missing_in_fts = [
            row["id"]
            for row in self.conn.execute(
                """
                SELECT n.id FROM notes n
                LEFT JOIN notes_fts f ON f.note_id = n.id
                WHERE f.note_id IS NULL
                LIMIT 100
                """
            ).fetchall()
        ]
        orphan_fts = [
            row["note_id"]
            for row in self.conn.execute(
                """
                SELECT f.note_id FROM notes_fts f
                LEFT JOIN notes n ON n.id = f.note_id
                WHERE n.id IS NULL
                LIMIT 100
                """
            ).fetchall()
        ]
        missing_in_trigram = [
            row["id"]
            for row in self.conn.execute(
                """
                SELECT n.id FROM notes n
                LEFT JOIN notes_fts_trigram t ON t.note_id = n.id
                WHERE t.note_id IS NULL
                LIMIT 100
                """
            ).fetchall()
        ]
        orphan_trigram = [
            row["note_id"]
            for row in self.conn.execute(
                """
                SELECT t.note_id FROM notes_fts_trigram t
                LEFT JOIN notes n ON n.id = t.note_id
                WHERE n.id IS NULL
                LIMIT 100
                """
            ).fetchall()
        ]
        notes_count = self.count_notes()
        fts_count = int(self.conn.execute("SELECT COUNT(*) AS c FROM notes_fts").fetchone()["c"])
        trigram_count = int(
            self.conn.execute("SELECT COUNT(*) AS c FROM notes_fts_trigram").fetchone()["c"]
        )
        return {
            "notes_count": notes_count,
            "fts_count": fts_count,
            "trigram_count": trigram_count,
            "missing_in_fts": missing_in_fts,
            "orphan_fts": orphan_fts,
            "missing_in_trigram": missing_in_trigram,
            "orphan_trigram": orphan_trigram,
            "consistent": (
                notes_count == fts_count
                and notes_count == trigram_count
                and not missing_in_fts
                and not orphan_fts
                and not missing_in_trigram
                and not orphan_trigram
            ),
        }

    def count_notes(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM notes").fetchone()
        return int(row["c"]) if row else 0

    # ---- lookups ---------------------------------------------------------

    def find_by_id(self, note_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return dict(row) if row else None

    def find_by_filename(self, filename: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM notes WHERE filename = ?", (filename,)).fetchone()
        return dict(row) if row else None

    def find_by_filename_prefix(self, prefix: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notes WHERE filename LIKE ? LIMIT ?",
            (f"{prefix}%", limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def tags_for_note(self, note_id: str) -> tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (note_id,)
        ).fetchall()
        return tuple(row["tag"] for row in rows)

    def wikilinks_for_note(self, note_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT target_title, target_id FROM wikilinks WHERE source_id = ? "
            "ORDER BY target_title",
            (note_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def backlinks_for_note(self, note_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT n.id, n.title, n.family, n.kind, n.path
            FROM wikilinks w
            JOIN notes n ON n.id = w.source_id
            WHERE w.target_id = ?
            ORDER BY n.title
            """,
            (note_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def graph_neighbourhood(
        self,
        start_id: str,
        *,
        depth: int,
        direction: str = "both",
    ) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]], list[str]]:
        """Breadth-first wiki-link traversal from `start_id`.

        Returns:
          * `nodes`: dict id -> {id, title, family, kind, path, depth}
          * `edges`: list of (source_id, target_id) tuples, each seen once
          * `broken_targets`: list of target titles encountered that have no
            resolved note id (forward-link only)

        `direction`:
          * `out` — only follow forward wiki-links
          * `in` — only follow backlinks
          * `both` — follow both (default)
        """
        if direction not in ("out", "in", "both"):
            raise ValueError(f"graph direction must be out/in/both, got {direction}")
        if depth < 0:
            raise ValueError("graph depth must be >= 0")

        visited_depth: dict[str, int] = {start_id: 0}
        edges: set[tuple[str, str]] = set()
        broken: set[str] = set()
        frontier: set[str] = {start_id}

        for current_depth in range(1, depth + 1):
            next_frontier: set[str] = set()
            if not frontier:
                break

            placeholders = ",".join("?" * len(frontier))
            frontier_params = tuple(frontier)

            if direction in ("out", "both"):
                for row in self.conn.execute(
                    f"""
                    SELECT source_id, target_id, target_title
                    FROM wikilinks
                    WHERE source_id IN ({placeholders})
                    """,
                    frontier_params,
                ).fetchall():
                    source_id = row["source_id"]
                    target_id = row["target_id"]
                    if target_id is None:
                        broken.add(row["target_title"])
                        continue
                    edges.add((source_id, target_id))
                    if target_id not in visited_depth:
                        visited_depth[target_id] = current_depth
                        next_frontier.add(target_id)

            if direction in ("in", "both"):
                for row in self.conn.execute(
                    f"""
                    SELECT source_id, target_id
                    FROM wikilinks
                    WHERE target_id IN ({placeholders})
                    """,
                    frontier_params,
                ).fetchall():
                    source_id = row["source_id"]
                    target_id = row["target_id"]
                    edges.add((source_id, target_id))
                    if source_id not in visited_depth:
                        visited_depth[source_id] = current_depth
                        next_frontier.add(source_id)

            frontier = next_frontier

        if not visited_depth:
            return {}, [], sorted(broken)

        node_ids = list(visited_depth.keys())
        node_placeholders = ",".join("?" * len(node_ids))
        rows = self.conn.execute(
            f"""
            SELECT id, title, family, kind, path
            FROM notes
            WHERE id IN ({node_placeholders})
            """,
            tuple(node_ids),
        ).fetchall()
        nodes: dict[str, dict[str, Any]] = {}
        for row in rows:
            nid = row["id"]
            nodes[nid] = {
                "id": nid,
                "title": row["title"],
                "family": row["family"],
                "kind": row["kind"],
                "path": row["path"],
                "depth": visited_depth[nid],
            }

        # Drop edges whose endpoints fell outside the visited set — shouldn't
        # happen in practice, but keeps the graph self-consistent.
        clean_edges = sorted((s, t) for (s, t) in edges if s in nodes and t in nodes)
        return nodes, clean_edges, sorted(broken)

    # ---- list / search --------------------------------------------------

    def list_notes(
        self,
        *,
        family: str | None = None,
        kind: str | None = None,
        tag: str | None = None,
        source: str | None = None,
        min_permission: str | None = None,
        max_permission: str | None = None,
        sort: str = "updated",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[NoteSummary], int]:
        order_by = {
            "updated": "n.updated_at DESC",
            "created": "n.created_at DESC",
            "title": "n.title ASC",
        }.get(sort, "n.updated_at DESC")

        where_clauses: list[str] = []
        params: list[Any] = []
        if family:
            where_clauses.append("n.family = ?")
            params.append(family)
        if kind:
            where_clauses.append("n.kind = ?")
            params.append(kind)
        if source:
            where_clauses.append("n.source = ?")
            params.append(source)
        if tag:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM tags t WHERE t.note_id = n.id AND t.tag = ?)"
            )
            params.append(tag)
        _append_permission_filter(where_clauses, params, min_permission, max_permission)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        total_row = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM notes n {where_sql}", tuple(params)
        ).fetchone()
        total = int(total_row["c"]) if total_row else 0

        rows = self.conn.execute(
            f"""
            SELECT n.id, n.filename, n.title, n.family, n.kind, n.source,
                   n.mcp_permissions, n.created_at, n.updated_at
            FROM notes n
            {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (limit, offset),
        ).fetchall()

        summaries: list[NoteSummary] = []
        for row in rows:
            summaries.append(
                NoteSummary(
                    id=row["id"],
                    filename=row["filename"],
                    title=row["title"],
                    family=row["family"],
                    kind=row["kind"],
                    source=row["source"],
                    tags=self.tags_for_note(row["id"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    mcp_permissions=row["mcp_permissions"] or "ALL",
                )
            )
        return summaries, total

    def search(
        self,
        query: str,
        *,
        family: str | None = None,
        kind: str | None = None,
        tag: str | None = None,
        min_permission: str | None = None,
        max_permission: str | None = None,
        limit: int = 20,
        offset: int = 0,
        vault_dir: Path,
        explain: bool = False,
    ) -> tuple[list[SearchHit], int]:
        where_clauses: list[str] = ["notes_fts MATCH ?"]
        params: list[Any] = [query]
        if family:
            where_clauses.append("n.family = ?")
            params.append(family)
        if kind:
            where_clauses.append("n.kind = ?")
            params.append(kind)
        if tag:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM tags t WHERE t.note_id = n.id AND t.tag = ?)"
            )
            params.append(tag)
        _append_permission_filter(where_clauses, params, min_permission, max_permission)

        where_sql = " AND ".join(where_clauses)
        total_row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM notes_fts
            JOIN notes n ON n.id = notes_fts.note_id
            WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()
        total = int(total_row["c"]) if total_row else 0

        # bm25() expects one weight per FTS5 column *including* UNINDEXED,
        # so the order matches the virtual table columns:
        #   (note_id UNINDEXED, title, body, filename).
        # note_id's weight is irrelevant (never matches), but must be present.
        #
        # For `--explain`, we additionally compute isolated per-column
        # scores by zeroing out the other columns' weights. Cost: 3 extra
        # bm25 evaluations per row — negligible next to the FTS match.
        explain_columns = (
            """,
                bm25(notes_fts, 0.0, 10.0, 0.0, 0.0) AS score_title,
                bm25(notes_fts, 0.0, 0.0, 1.0, 0.0) AS score_body,
                bm25(notes_fts, 0.0, 0.0, 0.0, 5.0) AS score_filename"""
            if explain
            else ""
        )
        rows = self.conn.execute(
            f"""
            SELECT
                n.id, n.title, n.family, n.kind, n.source, n.path, n.mcp_permissions,
                n.updated_at,
                bm25(notes_fts, 1.0, 10.0, 1.0, 5.0) AS score,
                snippet(notes_fts, 2, '<<', '>>', '...', 16) AS snippet{explain_columns}
            FROM notes_fts
            JOIN notes n ON n.id = notes_fts.note_id
            WHERE {where_sql}
            ORDER BY score
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (limit, offset),
        ).fetchall()

        hits: list[SearchHit] = []
        for row in rows:
            absolute = str((vault_dir / row["path"]).resolve())
            explain_tuple: tuple[tuple[str, float], ...] | None = None
            if explain:
                explain_tuple = (
                    ("title", float(row["score_title"] or 0.0)),
                    ("body", float(row["score_body"] or 0.0)),
                    ("filename", float(row["score_filename"] or 0.0)),
                )
            hits.append(
                SearchHit(
                    id=row["id"],
                    title=row["title"],
                    family=row["family"],
                    kind=row["kind"],
                    source=row["source"],
                    path=row["path"],
                    absolute_path=absolute,
                    tags=self.tags_for_note(row["id"]),
                    score=float(row["score"]) if row["score"] is not None else 0.0,
                    snippet=row["snippet"] or "",
                    updated_at=row["updated_at"],
                    mcp_permissions=row["mcp_permissions"] or "ALL",
                    explain=explain_tuple,
                )
            )
        return hits, total

    def search_fuzzy(
        self,
        query: str,
        *,
        family: str | None = None,
        kind: str | None = None,
        tag: str | None = None,
        min_permission: str | None = None,
        max_permission: str | None = None,
        limit: int = 20,
        offset: int = 0,
        vault_dir: Path,
    ) -> tuple[list[SearchHit], int]:
        """Typo-tolerant + substring search.

        Combines two passes and merges candidates by note_id:
          1. FTS5 trigram: substring match on title / body / filename,
             catching partial words and mid-word hits that the unicode61
             tokenizer misses (`auth` inside `authentication`).
          2. rapidfuzz over titles + filenames: Levenshtein-style scoring,
             catches typos and reorderings the user types.

        Filters (family/kind/tag/permission) apply to both passes at SQL
        level so we never fuzz-rank notes the user has excluded.
        """
        query = query.strip()
        if not query:
            return [], 0

        where_clauses: list[str] = []
        params: list[Any] = []
        if family:
            where_clauses.append("n.family = ?")
            params.append(family)
        if kind:
            where_clauses.append("n.kind = ?")
            params.append(kind)
        if tag:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM tags t WHERE t.note_id = n.id AND t.tag = ?)"
            )
            params.append(tag)
        _append_permission_filter(where_clauses, params, min_permission, max_permission)
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        trigram_hits: dict[str, tuple[float, str]] = {}
        trigram_query = _trigram_query(query)
        if trigram_query:
            trigram_rows = self.conn.execute(
                f"""
                SELECT
                    notes_fts_trigram.note_id AS id,
                    bm25(notes_fts_trigram, 1.0, 10.0, 1.0, 5.0) AS score,
                    snippet(notes_fts_trigram, 2, '<<', '>>', '...', 16) AS snippet
                FROM notes_fts_trigram
                JOIN notes n ON n.id = notes_fts_trigram.note_id
                WHERE notes_fts_trigram MATCH ? AND {where_sql}
                ORDER BY score
                LIMIT 200
                """,
                (trigram_query, *params),
            ).fetchall()
            for row in trigram_rows:
                trigram_hits[row["id"]] = (float(row["score"] or 0.0), row["snippet"] or "")

        filtered_rows = self.conn.execute(
            f"""
            SELECT n.id, n.filename, n.title, n.family, n.kind, n.source, n.path,
                   n.mcp_permissions, n.updated_at
            FROM notes n
            WHERE {where_sql}
            """,
            tuple(params),
        ).fetchall()
        row_by_id = {row["id"]: row for row in filtered_rows}

        haystack = {row["id"]: f"{row['title']} {row['filename']}" for row in filtered_rows}
        fuzz_hits: dict[str, float] = {}
        if haystack:
            matches = process.extract(
                query,
                haystack,
                scorer=fuzz.WRatio,
                limit=200,
                score_cutoff=55,
            )
            for _, score, key in matches:
                fuzz_hits[key] = float(score)

        candidate_ids = set(trigram_hits) | set(fuzz_hits)
        scored: list[tuple[float, str, str]] = []
        for note_id in candidate_ids:
            if note_id not in row_by_id:
                continue
            fuzz_score = fuzz_hits.get(note_id, 0.0)
            trigram_entry = trigram_hits.get(note_id)
            has_trigram = trigram_entry is not None
            snippet = trigram_entry[1] if trigram_entry else ""
            # rapidfuzz is 0..100. A trigram substring hit alone is worth
            # a fixed 30-point floor so the user still sees it above pure
            # noise. A note that matches both gets the sum, capped at 130.
            combined = fuzz_score + (30.0 if has_trigram else 0.0)
            scored.append((combined, note_id, snippet))

        scored.sort(key=lambda item: item[0], reverse=True)
        total = len(scored)
        page = scored[offset : offset + limit]

        hits: list[SearchHit] = []
        for score, note_id, snippet in page:
            row = row_by_id[note_id]
            absolute = str((vault_dir / row["path"]).resolve())
            hits.append(
                SearchHit(
                    id=row["id"],
                    title=row["title"],
                    family=row["family"],
                    kind=row["kind"],
                    source=row["source"],
                    path=row["path"],
                    absolute_path=absolute,
                    tags=self.tags_for_note(row["id"]),
                    score=score,
                    snippet=snippet,
                    updated_at=row["updated_at"],
                    mcp_permissions=row["mcp_permissions"] or "ALL",
                )
            )
        return hits, total

    def tag_counts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT tag, COUNT(*) AS count FROM tags GROUP BY tag ORDER BY count DESC, tag"
        ).fetchall()
        return [dict(row) for row in rows]

    def kind_counts(self, family: str | None = None) -> list[dict[str, Any]]:
        if family:
            rows = self.conn.execute(
                "SELECT kind, COUNT(*) AS count FROM notes WHERE family = ? "
                "GROUP BY kind ORDER BY count DESC, kind",
                (family,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT kind, COUNT(*) AS count FROM notes GROUP BY kind ORDER BY count DESC, kind"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- sync metadata --------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as _conn:
            self._write_meta(key, value)

    def get_meta(self, key: str) -> str | None:
        return self._read_meta(key)


def require_row(row: StoreNoteRow | None, note_id: str) -> StoreNoteRow:
    if row is None:
        raise NotFoundError(f"Note {note_id} is not in the local index")
    return row
