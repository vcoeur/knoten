"""One-shot migration from the legacy KNOTEN_HOME-anchored layout.

On first upgrade from knoten v0.1.x — where vault + state + config lived
under `~/.knoten/` (or wherever `KNOTEN_HOME` pointed) — move the user's
files into the new platformdirs layout so nothing is lost. Each move is
guarded on target absence so running the migration twice does nothing.

Called from `load_settings()` before `ensure_dirs()`. Never raises; any
filesystem error is logged as a warning and the CLI continues.

Legacy layout (v0.1.x):

    $KNOTEN_HOME/kasten/                        — markdown vault
    $KNOTEN_HOME/.knoten-state/index.sqlite     — derived SQLite index
    $KNOTEN_HOME/.knoten-state/state.json       — sync cursor
    ~/.config/knoten/.env                       — config

If `KNOTEN_HOME` is unset, legacy default is `~/.knoten/`.

New layout (v0.2+):

    config_dir / .env                           — config
    data_dir / kasten/                          — markdown vault
    cache_dir / index.sqlite                    — derived SQLite index
    cache_dir / state.json                      — sync cursor

Sync lock, tmp scratch, and other ephemeral `.knoten-state/` files are
not migrated — they are rebuilt on demand.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from knoten.paths import Paths


def _legacy_home() -> Path:
    """Return the legacy KNOTEN_HOME root to scan for v0.1 state."""
    env_home = os.environ.get("KNOTEN_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".knoten"


def _legacy_env_file() -> Path:
    return Path.home() / ".config" / "knoten" / ".env"


def migrate_legacy_layout(paths_obj: Paths) -> list[str]:
    """Move legacy artifacts into the new layout. Idempotent.

    Returns short human-readable descriptions of each move performed. On
    per-file failure, logs a warning and continues. Silent when nothing
    needs moving. No-op in dev mode — the repo keeps its own state under
    `.dev-state/`.
    """
    if paths_obj.is_dev:
        return []

    moved: list[str] = []

    legacy_root = _legacy_home()
    legacy_vault = legacy_root / "kasten"
    legacy_state_dir = legacy_root / ".knoten-state"
    legacy_index = legacy_state_dir / "index.sqlite"
    legacy_state_file = legacy_state_dir / "state.json"
    legacy_env = _legacy_env_file()

    # 1. Vault — single whole-directory move. Only when the target dir
    # either doesn't exist yet, or exists but is empty (ensure_dirs may
    # have pre-created it).
    if (
        legacy_vault.exists()
        and legacy_vault.is_dir()
        and _target_dir_is_absent_or_empty(paths_obj.vault_dir)
    ):
        try:
            if paths_obj.vault_dir.exists():
                paths_obj.vault_dir.rmdir()
            paths_obj.data_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_vault), str(paths_obj.vault_dir))
            moved.append(f"{legacy_vault} -> {paths_obj.vault_dir}")
        except OSError as exc:
            _warn(f"could not migrate {legacy_vault}: {exc}")

    # 2. SQLite index — rebuildable, but migrating saves a full sync.
    if legacy_index.exists() and not paths_obj.index_path.exists():
        try:
            paths_obj.cache_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_index), str(paths_obj.index_path))
            moved.append(f"{legacy_index} -> {paths_obj.index_path}")
        except OSError as exc:
            _warn(f"could not migrate {legacy_index}: {exc}")

    # 3. Sync cursor — small JSON, worth preserving so the next sync
    # stays incremental.
    if legacy_state_file.exists() and not paths_obj.state_file.exists():
        try:
            paths_obj.cache_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_state_file), str(paths_obj.state_file))
            moved.append(f"{legacy_state_file} -> {paths_obj.state_file}")
        except OSError as exc:
            _warn(f"could not migrate {legacy_state_file}: {exc}")

    # 4. .env — only moves when source and target differ (on Linux they
    # are often the same file under ~/.config/knoten/.env and there is
    # nothing to do).
    if (
        legacy_env.exists()
        and legacy_env.resolve() != paths_obj.env_file.resolve()
        and not paths_obj.env_file.exists()
    ):
        try:
            paths_obj.config_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_env), str(paths_obj.env_file))
            moved.append(f"{legacy_env} -> {paths_obj.env_file}")
            _rmdir_if_empty(legacy_env.parent)
        except OSError as exc:
            _warn(f"could not migrate {legacy_env}: {exc}")

    _rmdir_if_empty(legacy_state_dir)
    _rmdir_if_empty(legacy_root)

    if moved and os.environ.get("KNOTEN_HOME"):
        _warn(
            "KNOTEN_HOME is set but obsolete as of v0.2 — knoten ignores it. "
            "Use KNOTEN_CONFIG_DIR / KNOTEN_DATA_DIR / KNOTEN_CACHE_DIR to "
            "pin directories instead."
        )

    for description in moved:
        _info(f"migrated: {description}")

    return moved


def _target_dir_is_absent_or_empty(path: Path) -> bool:
    """True if `path` does not exist or exists as an empty directory."""
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    try:
        next(iter(path.iterdir()))
    except StopIteration:
        return True
    except OSError:
        return False
    return False


def _rmdir_if_empty(path: Path) -> None:
    """Remove `path` only if it exists and is an empty directory."""
    if not path.exists() or not path.is_dir():
        return
    try:
        next(iter(path.iterdir()))
    except StopIteration:
        try:
            path.rmdir()
        except OSError:
            pass
    except OSError:
        pass


def _info(message: str) -> None:
    print(f"knoten: {message}", file=sys.stderr)


def _warn(message: str) -> None:
    print(f"knoten: warning: {message}", file=sys.stderr)
