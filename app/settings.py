"""Runtime configuration loaded from environment and .env files.

The layout rule is: all local state lives under `KASTEN_HOME` (the directory
containing `pyproject.toml` by default). Vault content and index are siblings
under that root, so Claude and humans can find them with predictable paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from environs import Env


def _default_home() -> Path:
    """Return the KastenManager repo root: the directory that contains pyproject.toml."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent.parent


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

    @property
    def token_redacted(self) -> str:
        """Token with everything but the prefix masked, for display."""
        if not self.api_token:
            return ""
        prefix, _, _ = self.api_token.partition("_")
        return f"{prefix}_******" if prefix else "******"


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from process env and optional .env file.

    Missing KASTEN_API_TOKEN is tolerated here — commands that need it raise
    later with a clear exit-4 config error. This keeps `kasten config --show`
    usable even when the token is not yet set.
    """
    env = Env()
    if env_file is not None and env_file.exists():
        env.read_env(str(env_file), override=False)
    else:
        default_env = _default_home() / ".env"
        if default_env.exists():
            env.read_env(str(default_env), override=False)

    home = Path(env.str("KASTEN_HOME", str(_default_home()))).expanduser().resolve()
    vault_dir = home / env.str("KASTEN_VAULT_DIR", "kasten")
    state_dir = home / env.str("KASTEN_STATE_DIR", ".kasten-state")

    return Settings(
        api_url=env.str("KASTEN_API_URL", "https://notes.vcoeur.com").rstrip("/"),
        api_token=env.str("KASTEN_API_TOKEN", ""),
        http_timeout=env.float("KASTEN_HTTP_TIMEOUT", 30.0),
        home=home,
        vault_dir=vault_dir,
        state_dir=state_dir,
        index_path=state_dir / "index.sqlite",
        state_file=state_dir / "state.json",
        lock_file=state_dir / "sync.lock",
        tmp_dir=state_dir / "tmp",
    )


def ensure_dirs(settings: Settings) -> None:
    """Create vault + state directories if missing. Idempotent."""
    settings.vault_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
