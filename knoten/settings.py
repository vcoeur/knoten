"""Runtime configuration loaded from environment and .env files.

All local state lives under `KNOTEN_HOME`. Discovery order for the root
and the `.env` file is designed so the same codebase works in three
different runtime contexts:

  1. **Dev from the repo** (`uv run knoten …`, `make sync`): `_default_home()`
     walks up from `__file__` and finds the repo root via `pyproject.toml`.
     The repo's `.env` is picked up automatically.
  2. **Installed globally** (`uv tool install .` → `~/.local/bin/knoten`):
     `__file__` is inside a uv tools venv's `site-packages/`, so the
     pyproject walk would match nothing useful. `_default_home()` detects
     that case and falls back to `~/.knoten`, and `.env` is picked up from
     `~/.config/knoten/.env` so the installed CLI can find the user's real
     vault without per-invocation env vars.
  3. **Tests**: `tmp_settings` fixture constructs `Settings` explicitly;
     the discovery helpers are bypassed entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from environs import Env

# Stable user-level config path, read before the source-tree fallback so an
# installed CLI can find a repo vault via `KNOTEN_HOME=…` inside this file.
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


def _env_file_candidates(explicit: Path | None) -> list[Path]:
    """Return every `.env` that should be layered, in priority order.

    `environs.read_env(override=False)` implements "first value wins", so
    files later in the list only fill in keys the earlier files did not
    set. This means an installed CLI can keep a tiny `~/.config/knoten/.env`
    that just points `KNOTEN_HOME` at a repo, and still pick up the
    token from that repo's own `.env` — no secret duplication.

    Order:
      1. `explicit` — caller-supplied, for tests or advanced callers.
      2. `~/.config/knoten/.env` — user-level config. The canonical
         location for an installed CLI's `KNOTEN_HOME` pointer.
      3. `$KNOTEN_HOME/.env` — if `KNOTEN_HOME` was set by the shell
         (or by an earlier layer), its sibling `.env` is layered next.
         This is what lets the user-level pointer cascade into the repo's
         own `.env`.
      4. `_default_home() / .env` — dev workflow (repo via pyproject walk
         or `~/.knoten`). Only added when it's not already in the list.
    """
    candidates: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        resolved = path.expanduser()
        if resolved.exists() and resolved not in candidates:
            candidates.append(resolved)

    add(explicit)
    add(USER_CONFIG_ENV)

    # Second pass: re-read the environment so a layer-1/2 hit that set
    # KNOTEN_HOME is visible for candidate 3.
    import os

    env_home_value = os.environ.get("KNOTEN_HOME")
    if candidates:
        # Pre-parse candidates already collected so KNOTEN_HOME that lives
        # only inside those files still activates candidate 3.
        for candidate in candidates:
            env_home_value = _peek_env_var(candidate, "KNOTEN_HOME", env_home_value)
    if env_home_value:
        add(Path(env_home_value) / ".env")

    add(_default_home() / ".env")
    return candidates


def _peek_env_var(env_file: Path, key: str, current: str | None) -> str | None:
    """Return the first definition of `key` in `env_file`, or `current` if absent.

    A minimal parser — we only care about simple `KEY=value` lines at the
    top of a `.env`, which is enough to look up `KNOTEN_HOME` before fully
    loading the layered config via environs.
    """
    if current:
        return current
    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            lhs, _, rhs = line.partition("=")
            if lhs.strip() == key:
                return rhs.strip().strip('"').strip("'") or None
    except OSError:
        return current
    return current


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from process env and optional .env file.

    Missing KNOTEN_API_TOKEN is tolerated here — commands that need it raise
    later with a clear exit-4 config error. This keeps `knoten config --show`
    usable even when the token is not yet set.
    """
    env = Env()
    for candidate in _env_file_candidates(env_file):
        env.read_env(str(candidate), override=False)

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
