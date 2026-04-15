"""Runtime configuration loaded from environment and a single .env file.

Exactly one `.env` file is read per invocation. The file picked depends
on whether the CLI is running from an installed copy or a source checkout:

  1. **Dev from the repo** (`uv run knoten …`, `make sync`): a pyproject
     walk up from `__file__` finds the repo root, and the repo's own
     `.env` is read.
  2. **Installed** (`pipx install knoten` / `uv tool install knoten`):
     `__file__` lives inside a site-packages or uv tools venv, so the
     pyproject walk is skipped. The CLI reads `~/.config/knoten/.env`.
     If a user wants the vault somewhere other than `~/.knoten`, they
     set `KNOTEN_HOME=/path/to/vault` inside that same file — all
     configuration lives in one place.
  3. **Tests**: the `tmp_settings` fixture constructs `Settings`
     explicitly; the discovery helpers are bypassed entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from environs import Env

# Stable user-level config path. The single `.env` file read when knoten
# is running from an installed copy (`pipx install knoten` /
# `uv tool install knoten`). Users who want the vault outside `~/.knoten`
# set `KNOTEN_HOME=…` inside this same file, alongside URL + token.
USER_CONFIG_ENV = Path.home() / ".config" / "knoten" / ".env"

# Final fallback home when no other anchor is available. Matches the layout
# expected by `ensure_dirs` — `kasten/` + `.knoten-state/` as siblings.
# The vault subdir keeps its historical `kasten/` name (configurable via
# KNOTEN_VAULT_DIR) because that's what the dev mirror is called.
FALLBACK_HOME = Path.home() / ".knoten"


def _looks_like_installed_location(path: Path) -> bool:
    """True when `path` is inside a uv tools venv (or any site-packages tree).

    Used to reject the source-tree walk when the code is running from an
    installed copy — otherwise the walk falls off the end and returns a
    nonsense directory under `site-packages/` as the home.
    """
    parts = path.parts
    return "site-packages" in parts or ("uv" in parts and "tools" in parts)


def _default_home() -> Path:
    """Return the best guess for `KNOTEN_HOME` when nothing overrides it.

    Walks up from this file looking for a `pyproject.toml` — that path
    matches the dev workflow (`uv run` / `make` from the repo). If the
    walk finishes without finding one, or if `__file__` is inside a uv
    tools install, returns `~/.knoten` so the installed CLI has a real,
    writable place to put its state.
    """
    here = Path(__file__).resolve()
    if not _looks_like_installed_location(here):
        for parent in here.parents:
            if (parent / "pyproject.toml").exists():
                return parent
    return FALLBACK_HOME


MODE_REMOTE = "remote"
MODE_LOCAL = "local"
MODE_AUTO = "auto"
_VALID_MODES = frozenset({MODE_REMOTE, MODE_LOCAL, MODE_AUTO})


@dataclass(frozen=True)
class Settings:
    """Effective configuration for a single CLI invocation."""

    api_url: str
    api_token: str
    http_timeout: float
    home: Path
    vault_dir: Path
    state_dir: Path
    index_path: Path
    state_file: Path
    lock_file: Path
    tmp_dir: Path
    mode: str = MODE_AUTO

    @property
    def token_redacted(self) -> str:
        """Token with everything but the prefix masked, for display."""
        if not self.api_token:
            return ""
        prefix, _, _ = self.api_token.partition("_")
        return f"{prefix}_******" if prefix else "******"

    @property
    def effective_mode(self) -> str:
        """Return 'local' or 'remote' after resolving `auto`.

        `auto` → `local` if `api_url` is empty, else `remote`. Explicit
        `local` / `remote` are honoured as-is.
        """
        if self.mode == MODE_LOCAL:
            return MODE_LOCAL
        if self.mode == MODE_REMOTE:
            return MODE_REMOTE
        return MODE_LOCAL if not self.api_url else MODE_REMOTE


def primary_env_file() -> Path:
    """Return the single `.env` file knoten reads for this invocation.

    - In an installed copy (`pipx install knoten` / `uv tool install knoten`):
      `~/.config/knoten/.env`.
    - In a dev checkout: the repo-root `.env` found by walking up from
      this file to the first `pyproject.toml`.
    - As a final fallback (neither pattern matches): `~/.knoten/.env`.
    """
    here = Path(__file__).resolve()
    if _looks_like_installed_location(here):
        return USER_CONFIG_ENV
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent / ".env"
    return FALLBACK_HOME / ".env"


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from process env and a single .env file.

    Missing KNOTEN_API_TOKEN is tolerated here — commands that need it raise
    later with a clear exit-4 config error. This keeps `knoten config show`
    usable even when the token is not yet set.
    """
    env = Env()
    target = (env_file or primary_env_file()).expanduser()
    if target.exists():
        env.read_env(str(target), override=False)

    home = Path(env.str("KNOTEN_HOME", str(_default_home()))).expanduser().resolve()
    vault_dir = home / env.str("KNOTEN_VAULT_DIR", "kasten")
    state_dir = home / env.str("KNOTEN_STATE_DIR", ".knoten-state")

    mode = env.str("KNOTEN_MODE", MODE_AUTO).strip().lower() or MODE_AUTO
    if mode not in _VALID_MODES:
        from knoten.repositories.errors import ConfigError

        raise ConfigError(f"KNOTEN_MODE must be one of {sorted(_VALID_MODES)}, got {mode!r}")

    return Settings(
        api_url=env.str("KNOTEN_API_URL", "").rstrip("/"),
        api_token=env.str("KNOTEN_API_TOKEN", ""),
        http_timeout=env.float("KNOTEN_HTTP_TIMEOUT", 30.0),
        home=home,
        vault_dir=vault_dir,
        state_dir=state_dir,
        index_path=state_dir / "index.sqlite",
        state_file=state_dir / "state.json",
        lock_file=state_dir / "sync.lock",
        tmp_dir=state_dir / "tmp",
        mode=mode,
    )


def ensure_dirs(settings: Settings) -> None:
    """Create vault + state directories if missing. Idempotent."""
    settings.vault_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
