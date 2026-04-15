"""Exception hierarchy used across repositories and services.

These are mapped to CLI exit codes in `knoten.cli.main`:

    UserError       -> 1
    NetworkError    -> 2
    StoreError      -> 3
    ConfigError     -> 4
    LockTimeout     -> 5
"""

from __future__ import annotations


class KnotenError(Exception):
    """Base class for all knoten errors."""


class UserError(KnotenError):
    """Invalid arguments, missing target, validation failure."""


class NotFoundError(UserError):
    """The requested target does not exist locally or remotely."""


class NoteForbiddenError(KnotenError):
    """Per-note 404 from the remote during a read.

    The server deliberately conflates "note does not exist" and "viewer
    cannot read this note" to avoid leaking existence to restricted tokens.
    Sync treats this as a recoverable per-note error and creates a
    metadata-only placeholder for the note instead of failing the run.
    """

    def __init__(self, note_id: str) -> None:
        super().__init__(f"note {note_id}: forbidden or deleted on remote")
        self.note_id = note_id


class AmbiguousTargetError(UserError):
    """A filename prefix matched more than one note; includes candidates."""

    def __init__(self, message: str, candidates: list[dict]) -> None:
        super().__init__(message)
        self.candidates = candidates


class PermissionError(UserError):
    """Local client-side pre-check blocked a write because the note's
    `mcpPermissions` level is below what the operation needs.

    This is a fast-fail guard for tokens that enforce per-note permissions
    (`api` scope). `web`-scope tokens can bypass it with `--force`, which
    skips the check entirely — the server is still the final authority.
    """

    def __init__(
        self,
        *,
        note_id: str,
        filename: str,
        current_level: str,
        required_level: str,
        operation: str,
    ) -> None:
        super().__init__(
            f"{operation} requires {required_level} on '{filename}' "
            f"(note {note_id} is {current_level}). "
            f"Use --force to bypass the local check — the server may still reject."
        )
        self.note_id = note_id
        self.filename = filename
        self.current_level = current_level
        self.required_level = required_level
        self.operation = operation


class NetworkError(KnotenError):
    """Remote API unreachable, authentication failed, or returned 5xx."""


class AuthError(NetworkError):
    """Token missing, invalid, or lacks required scope."""


class StoreError(KnotenError):
    """Local SQLite or filesystem failure."""


class ConfigError(KnotenError):
    """Missing or unreadable config (e.g. KNOTEN_API_TOKEN not set)."""


class LockTimeoutError(KnotenError):
    """Another knoten process is holding the sync lock."""
