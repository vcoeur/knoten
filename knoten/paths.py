"""Resolve config, data, and cache directories for the CLI.

Uses `platformdirs` so the layout follows OS conventions: XDG on Linux,
`~/Library/Application Support` + `~/Library/Caches` on macOS, `%APPDATA%` +
`%LOCALAPPDATA%` on Windows. Env-var overrides win over everything so tests,
Docker, and power users can point the three locations anywhere.

Dev mode is detected by walking up from `__file__` looking for `pyproject.toml`:
when running from a source checkout the `.env` at the repo root is still the
config source (convenient during development), but the vault and cache go
into a repo-local `.dev-state/` subdirectory so dev state never mixes with
the user's real installed data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir

APP_NAME = "knoten"

ENV_CONFIG_DIR = "KNOTEN_CONFIG_DIR"
ENV_DATA_DIR = "KNOTEN_DATA_DIR"
ENV_CACHE_DIR = "KNOTEN_CACHE_DIR"

VAULT_SUBDIR = "kasten"


@dataclass(frozen=True)
class Paths:
    """Resolved filesystem locations for a single CLI invocation."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path
    env_file: Path
    vault_dir: Path
    index_path: Path
    state_file: Path
    lock_file: Path
    tmp_dir: Path
    is_dev: bool


def _looks_like_installed_location(path: Path) -> bool:
    """True when `path` is inside a uv tools venv or any site-packages tree."""
    parts = path.parts
    return "site-packages" in parts or ("uv" in parts and "tools" in parts)


def _repo_root() -> Path | None:
    """Return the repo root when running from a source checkout, else None."""
    here = Path(__file__).resolve()
    if _looks_like_installed_location(here):
        return None
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def resolve() -> Paths:
    """Compute the effective config/data/cache layout for this process.

    Dev-mode rationale: the vault under `<repo>/kasten/` is the historic
    location before v0.2 and typically holds the maintainer's real notes,
    so `data_dir = repo_root` keeps `vault_dir = repo_root / "kasten"` —
    no forced migration. Only the SQLite cache, locks, and tmp scratch
    move to `<repo>/.dev-state/cache/` to keep derived state out of the
    main tree.
    """
    repo = _repo_root()

    if repo is not None:
        dev_config = repo
        dev_data = repo
        dev_cache = repo / ".dev-state" / "cache"
    else:
        dev_config = dev_data = dev_cache = None

    installed_config = Path(user_config_dir(APP_NAME, appauthor=False))
    installed_data = Path(user_data_dir(APP_NAME, appauthor=False))
    installed_cache = Path(user_cache_dir(APP_NAME, appauthor=False))

    def pick(env_var: str, dev: Path | None, installed: Path) -> Path:
        override = os.environ.get(env_var)
        if override:
            return Path(override).expanduser()
        return dev if dev is not None else installed

    config_dir = pick(ENV_CONFIG_DIR, dev_config, installed_config)
    data_dir = pick(ENV_DATA_DIR, dev_data, installed_data)
    cache_dir = pick(ENV_CACHE_DIR, dev_cache, installed_cache)

    return Paths(
        config_dir=config_dir,
        data_dir=data_dir,
        cache_dir=cache_dir,
        env_file=config_dir / ".env",
        vault_dir=data_dir / VAULT_SUBDIR,
        index_path=cache_dir / "index.sqlite",
        state_file=cache_dir / "state.json",
        lock_file=cache_dir / "sync.lock",
        tmp_dir=cache_dir / "tmp",
        is_dev=repo is not None,
    )


def ensure_dirs(paths: Paths) -> None:
    """Create config, vault, and cache directories if missing. Idempotent."""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.vault_dir.mkdir(parents=True, exist_ok=True)
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
