"""JSON state file holding the sync cursor and schema version.

Mirrors a subset of `sync_meta` in SQLite, but exists on disk as its own
human-readable file — makes it easy for a user to inspect state without
opening the sqlite DB.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SyncState:
    schema_version: int = 1
    last_sync_at: str | None = None
    last_sync_max_updated_at: str | None = None
    last_full_sync_at: str | None = None
    last_remote_total: int | None = None


def load_state(path: Path) -> SyncState:
    if not path.exists():
        return SyncState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SyncState()
    return SyncState(
        schema_version=int(data.get("schema_version", 1)),
        last_sync_at=data.get("last_sync_at"),
        last_sync_max_updated_at=data.get("last_sync_max_updated_at"),
        last_full_sync_at=data.get("last_full_sync_at"),
        last_remote_total=data.get("last_remote_total"),
    )


def save_state(path: Path, state: SyncState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
