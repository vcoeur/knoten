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
    """A write was blocked because the note's `permissions` level is below
    what the operation needs.

    Raised in two situations, both exit 1 / kind `permission_denied`:

    - The **local** client-side pre-check (full context: `filename`,
      `current_level`, `operation`). A fast-fail guard for tokens that enforce
      per-note permissions (`api` scope); `web`-scope tokens bypass it with
      `--force`, the server staying the final authority.
    - A **server** 403 `{"error": "FORBIDDEN", "detail": {noteId, level}}` that
      slipped past the local check (stale mirror, or `--force`). Only `note_id`
      and `required_level` are known there — the other fields stay `None`.
    """

    def __init__(
        self,
        *,
        note_id: str,
        required_level: str,
        filename: str | None = None,
        current_level: str | None = None,
        operation: str | None = None,
    ) -> None:
        if filename is not None and current_level is not None and operation is not None:
            message = (
                f"{operation} requires {required_level} on '{filename}' "
                f"(note {note_id} is {current_level}). "
                f"Use --force to bypass the local check — the server may still reject."
            )
        else:
            # Server-origin 403 FORBIDDEN — we only learn the note id and the
            # level the server demanded.
            message = (
                f"permission denied by the remote: requires {required_level} on note {note_id}"
            )
        super().__init__(message)
        self.note_id = note_id
        self.filename = filename
        self.current_level = current_level
        self.required_level = required_level
        self.operation = operation


class RemoteRejectionError(UserError):
    """The remote rejected a mutation with a structured, user-actionable 4xx.

    Covers 409 `DUPLICATE_FILENAME` / `DUPLICATE_REFERENCE` and 400
    `INVALID_FILENAME`. Exit 1 / kind `user`. The server's error code is
    preserved on `error_code` and surfaced in the JSON envelope under
    `error_code` (the envelope's own `code` field is the integer exit code).
    """

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ValidationError(UserError):
    """Remote backend rejected a note payload because a frontmatter field
    value did not match its declared type.

    Raised on HTTP 400 responses whose body is
    `{"error": "VALIDATION_ERROR", "detail": {"issues": [...]}}` — one issue
    per offending field, each a dict of `{key, expected, actual, message}`.
    The backend began enforcing this with `notes.vcoeur.com v2.9.1`;
    earlier releases silently accepted type-mismatched values.

    The full issues list is preserved on the exception so the CLI can
    surface it in the structured error envelope under `issues`.
    """

    def __init__(self, issues: list[dict], *, method: str, path: str) -> None:
        summary = (
            "; ".join(
                f"{issue.get('key', '?')}: {issue.get('message') or issue.get('expected', '?')}"
                for issue in issues
            )
            or "frontmatter type mismatch"
        )
        super().__init__(f"{method} {path} rejected by backend validation — {summary}")
        self.issues = issues
        self.method = method
        self.path = path


class FrontmatterValidationError(UserError):
    """A frontmatter value cannot be written as a single-line YAML scalar.

    Raised by the vault-file writer (`render_note_markdown` /
    `render_placeholder_markdown`) when a value contains a control character
    (newline, carriage return, any C0 except tab). Carries the offending
    `key` plus — once the writer has enriched it — the note's `note_id` and
    `filename`, so callers can either name the note in a loud failure
    (interactive writes) or skip it with a warning (sync ingest, where a
    hostile server value must never abort the run).
    """

    def __init__(
        self,
        message: str,
        *,
        key: str,
        note_id: str | None = None,
        filename: str | None = None,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.note_id = note_id
        self.filename = filename


class NetworkError(KnotenError):
    """Remote API unreachable, authentication failed, or returned 5xx."""


class AuthError(NetworkError):
    """Token missing, invalid, or lacks required scope."""


class BatchReadUnsupportedError(NetworkError):
    """The remote lacks `POST /api/notes/batch-read` (the route itself 404s).

    Raised by `RemoteBackend.read_notes` when the server predates the
    batch-read endpoint. The sync pull pass catches it once per run and
    falls back to per-note `GET /api/notes/{id}` for the remainder — one
    release of backward compatibility with older servers.
    """


class StoreError(KnotenError):
    """Local SQLite or filesystem failure."""


class ConfigError(KnotenError):
    """Missing or unreadable config (e.g. KNOTEN_API_TOKEN not set)."""


class LockTimeoutError(KnotenError):
    """Another knoten process is holding the sync lock."""
