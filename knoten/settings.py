"""Runtime configuration loaded from environment and a single .env file.

Filesystem locations (config / data / cache dirs) are resolved in `paths.py`
via `platformdirs`, so the layout follows each OS's conventions out of the
box and the three dirs can be pointed anywhere via `KNOTEN_*_DIR` env
overrides.

This module loads non-path values — API URL, token, HTTP timeout, mode —
from the resolved config dir's `.env` file layered over the process
environment (process env wins).
"""

from __future__ import annotations

from dataclasses import dataclass

from environs import Env

from knoten import paths
from knoten.migrate import migrate_legacy_layout
from knoten.paths import Paths

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
    paths: Paths
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


def load_settings() -> Settings:
    """Resolve paths, migrate legacy layout, and return an immutable Settings.

    Missing KNOTEN_API_TOKEN is tolerated here — commands that need it raise
    later with a clear exit-4 config error. This keeps `knoten config show`
    usable even when the token is not yet set.
    """
    resolved = paths.resolve()
    migrate_legacy_layout(resolved)
    paths.ensure_dirs(resolved)

    env = Env()
    if resolved.env_file.exists():
        env.read_env(str(resolved.env_file), override=False)

    mode = env.str("KNOTEN_MODE", MODE_AUTO).strip().lower() or MODE_AUTO
    if mode not in _VALID_MODES:
        from knoten.repositories.errors import ConfigError

        raise ConfigError(f"KNOTEN_MODE must be one of {sorted(_VALID_MODES)}, got {mode!r}")

    return Settings(
        api_url=env.str("KNOTEN_API_URL", "").rstrip("/"),
        api_token=env.str("KNOTEN_API_TOKEN", ""),
        http_timeout=env.float("KNOTEN_HTTP_TIMEOUT", 30.0),
        paths=resolved,
        mode=mode,
    )
