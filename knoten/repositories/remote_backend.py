"""HTTP-backed `Backend` implementation — talks to a compatible remote backend.

Thin typed wrapper around httpx. All methods raise the exception types from
`app.repositories.errors` so the service layer stays backend-agnostic. The
class used to be `NotesClient` in `http_client.py`; this file is the same
behaviour adapted to the `Backend` protocol — method names and return types
match the protocol exactly, payload dicts are translated to/from the shared
dataclasses at the boundary.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from knoten.models import Note, NoteSummary
from knoten.repositories.backend import (
    AttachmentDownloadResult,
    AttachmentUploadResult,
    Backend,
    NoteDraft,
    NotePatch,
    NotesPage,
    NoteUpdateResult,
)
from knoten.repositories.errors import (
    AuthError,
    NetworkError,
    NoteForbiddenError,
    NotFoundError,
    RemoteRejectionError,
    ValidationError,
)
from knoten.repositories.errors import (
    PermissionError as LocalPermissionError,
)
from knoten.services.note_mapper import note_from_api, summary_from_api
from knoten.settings import Settings


def _parse_disposition_filename(header: str) -> str | None:
    """Best-effort extraction of `filename="..."` from a Content-Disposition header."""
    if not header:
        return None
    marker = 'filename="'
    start = header.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = header.find('"', start)
    if end == -1:
        return None
    return header[start:end] or None


def _safe_json(response: httpx.Response) -> Any:
    """Return the parsed JSON body, or None if the body is empty / not JSON.

    Used by `_request` to peek at error-response envelopes without ever
    raising while inside the error-handling branch.
    """
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


class RemoteBackend(Backend):
    """Bearer-token client for a knoten-compatible remote backend's REST API.

    Honours the `Backend` protocol. Mutations return the post-mutation
    envelope the service layer actually consumes — a plain id string from
    `create_note`, a `NoteUpdateResult` with `affected_notes` from
    `update_note`, and `None` from `append_to_note / delete_note /
    restore_note` because the caller already knows the id and re-fetches
    via `read_note` before ingesting.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.api_token:
            raise AuthError(
                "KNOTEN_API_TOKEN is not set. Copy .env.example to .env and add a token.",
            )
        self._settings = settings
        self._client = httpx.Client(
            base_url=settings.api_url,
            headers={
                "Authorization": f"Bearer {settings.api_token}",
                "Accept": "application/json",
                "User-Agent": "knoten/0.1",
            },
            timeout=settings.http_timeout,
        )

    def __enter__(self) -> RemoteBackend:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def list_note_summaries(self, *, limit: int, offset: int) -> NotesPage:
        raw = self._get_json("/api/notes", params={"limit": limit, "offset": offset})
        items = raw.get("data") or []
        return NotesPage(
            data=tuple(summary_from_api(item) for item in items),
            total=int(raw.get("total") or 0),
            limit=int(raw.get("limit") or limit),
            offset=int(raw.get("offset") or offset),
        )

    def list_trashed_notes(self, *, limit: int | None = None) -> tuple[NoteSummary, ...]:
        # GET /api/trash/notes returns a bare JSON array of note rows (each
        # carrying `deletedAt`), not the standard `{data,total}` envelope.
        raw = self._get_json("/api/trash/notes")
        items = raw if isinstance(raw, list) else (raw.get("data") if isinstance(raw, dict) else [])
        summaries = [summary_from_api(item) for item in (items or [])]
        if limit is not None:
            summaries = summaries[:limit]
        return tuple(summaries)

    def read_note(self, note_id: str) -> Note:
        payload = self._request("GET", f"/api/notes/{note_id}", note_id=note_id)
        return note_from_api(payload)

    def create_note(self, draft: NoteDraft) -> str:
        # `tags` is deliberately NOT sent: the server's createNoteSchema is
        # non-strict and silently strips an unknown `tags` field, re-deriving
        # tags from the body's `#hashtags` instead. The client already composes
        # tags into the body (`_compose_body`), so the body is the only tag
        # surface on create — sending `tags` would be a misleading no-op.
        payload: dict[str, Any] = {"filename": draft.filename}
        if draft.body:
            payload["body"] = draft.body
        if draft.kind is not None:
            payload["kind"] = draft.kind
        if draft.frontmatter:
            payload["frontmatter"] = dict(draft.frontmatter)
        raw = self._post_json("/api/notes", json=payload, expected=(200, 201))
        return str(raw.get("id"))

    def update_note(self, note_id: str, patch: NotePatch) -> NoteUpdateResult:
        payload: dict[str, Any] = {}
        if patch.filename is not None:
            payload["filename"] = patch.filename
        if patch.title is not None:
            payload["title"] = patch.title
        if patch.body is not None:
            payload["body"] = patch.body
        if patch.frontmatter is not None:
            payload["frontmatter"] = dict(patch.frontmatter)
        raw = self._put_json(f"/api/notes/{note_id}", json=payload)
        affected_raw = raw.get("affectedNotes") if isinstance(raw, dict) else None
        affected_ids: list[str] = []
        if isinstance(affected_raw, list):
            for entry in affected_raw:
                if isinstance(entry, dict) and "id" in entry:
                    affected_ids.append(str(entry["id"]))
                elif isinstance(entry, str):
                    affected_ids.append(entry)
        return NoteUpdateResult(note_id=note_id, affected_notes=tuple(affected_ids))

    def append_to_note(self, note_id: str, content: str) -> None:
        self._post_json(
            f"/api/notes/{note_id}/append",
            json={"content": content},
            expected=(200, 201),
        )

    def delete_note(self, note_id: str) -> None:
        # 404 maps to NotFoundError (exit 1), matching read_note and
        # LocalBackend.delete_note — and letting the sync push pass treat
        # "already gone on remote" as success instead of a network failure.
        self._request(
            "DELETE",
            f"/api/notes/{note_id}",
            not_found_message=f"No note with id {note_id} on remote",
        )

    def restore_note(self, note_id: str) -> None:
        self._post_json(
            f"/api/notes/{note_id}/restore",
            json={},
            expected=(200, 201),
        )

    def upload_attachment(
        self,
        path: Path,
        *,
        content_type: str | None = None,
        source: str | None = None,
    ) -> AttachmentUploadResult:
        try:
            with path.open("rb") as handle:
                files = {"file": (path.name, handle, content_type or "application/octet-stream")}
                data: dict[str, str] = {}
                if source is not None:
                    data["source"] = source
                response = self._client.post("/api/attachments", files=files, data=data)
        except OSError as exc:
            raise NetworkError(f"Cannot read {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"POST /api/attachments failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise AuthError(
                f"POST /api/attachments returned {response.status_code} — "
                "check KNOTEN_API_TOKEN scope (needs web or api)."
            )
        if response.status_code not in (200, 201):
            raise NetworkError(
                f"POST /api/attachments returned {response.status_code}: {response.text[:200]}"
            )
        body = response.json()
        size_raw = body.get("sizeBytes")
        try:
            size_bytes = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        return AttachmentUploadResult(
            storage_key=str(body.get("storageKey") or ""),
            content_type=body.get("contentType"),
            size_bytes=size_bytes,
            url=body.get("url"),
        )

    def download_attachment(self, storage_key: str, destination: Path) -> AttachmentDownloadResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        content_type = ""
        disposition_filename: str | None = None
        # Stream into a sibling tmp file and os.replace on success, so a
        # mid-stream failure never leaves a truncated file at the destination.
        tmp = destination.with_name(destination.name + ".tmp")
        try:
            with self._client.stream("GET", f"/api/attachments/{storage_key}") as response:
                if response.status_code in (401, 403):
                    raise AuthError(
                        f"GET /api/attachments/{storage_key} returned {response.status_code} — "
                        "check KNOTEN_API_TOKEN scope."
                    )
                if response.status_code == 404:
                    raise NotFoundError(f"Attachment {storage_key} not found or deleted on remote")
                if response.status_code >= 400:
                    raise NetworkError(
                        f"GET /api/attachments/{storage_key} returned {response.status_code}"
                    )
                content_type = response.headers.get("content-type", "")
                disposition_filename = _parse_disposition_filename(
                    response.headers.get("content-disposition", "")
                )
                with tmp.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                        written += len(chunk)
            os.replace(tmp, destination)
        except httpx.HTTPError as exc:
            raise NetworkError(f"Cannot reach {self._settings.api_url}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)
        return AttachmentDownloadResult(
            path=destination,
            bytes_written=written,
            content_type=content_type,
            filename=disposition_filename,
        )

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post_json(
        self, path: str, *, json: dict[str, Any], expected: tuple[int, ...] = (200,)
    ) -> Any:
        return self._request("POST", path, json=json, expected=expected)

    def _put_json(self, path: str, *, json: dict[str, Any]) -> Any:
        return self._request("PUT", path, json=json)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200, 201, 204),
        note_id: str | None = None,
        not_found_message: str | None = None,
    ) -> Any:
        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise NetworkError(f"{method} {path} failed: {exc}") from exc
        status = response.status_code
        if status >= 400:
            # Parse the structured error envelope once. The server emits
            # `{"error": "<CODE>", "detail": {...}}` for the cases below; we map
            # each to the typed exception that carries the right exit code and
            # structured fields, falling back to a generic NetworkError.
            parsed = _safe_json(response)
            error_code = parsed.get("error") if isinstance(parsed, dict) else None
            detail = parsed.get("detail") if isinstance(parsed, dict) else None

            # 403 FORBIDDEN — a per-note permission denial. Map to the same
            # PermissionError the local pre-check raises (exit 1,
            # permission_denied), carrying noteId/level as structured extras.
            if status == 403 and error_code == "FORBIDDEN":
                forbidden = detail if isinstance(detail, dict) else {}
                raise LocalPermissionError(
                    note_id=str(forbidden.get("noteId") or note_id or ""),
                    required_level=str(forbidden.get("level") or ""),
                )
            # 401/403 without a recognisable body — token missing/invalid/scope.
            if status in (401, 403):
                raise AuthError(
                    f"{method} {path} returned {status} — check KNOTEN_API_TOKEN scope."
                )
            # 429 — rate limited (transient; retry later). Exit 2 (network).
            if status == 429:
                raise NetworkError(
                    f"{method} {path} returned 429 — rate limited by the remote. "
                    "Too many requests; wait and retry."
                )
            # 413 — payload too large. Exit 2 (network), consistent with the
            # upload path's other size/transport failures; message carries the
            # server's byte cap when present.
            if status == 413:
                max_bytes = detail.get("maxBytes") if isinstance(detail, dict) else None
                limit = f" (max {max_bytes} bytes)" if max_bytes is not None else ""
                raise NetworkError(
                    f"{method} {path} returned 413 — payload too large{limit}. "
                    "Reduce the upload size."
                )
            if status == 503:
                raise NetworkError(f"{method} {path} returned 503 — vault locked on the remote.")
            if status == 404 and note_id is not None:
                raise NoteForbiddenError(note_id)
            if status == 404 and not_found_message is not None:
                raise NotFoundError(not_found_message)
            # 400 VALIDATION_ERROR — frontmatter type mismatch (v2.9.1+).
            # Shape: {"error": "VALIDATION_ERROR", "detail": {"issues": [...]}}.
            if status == 400 and error_code == "VALIDATION_ERROR":
                issues = detail.get("issues") if isinstance(detail, dict) else None
                if not isinstance(issues, list):
                    issues = []
                raise ValidationError(issues, method=method, path=path)
            # 400 INVALID_FILENAME — the server rejected the filename grammar.
            if status == 400 and error_code == "INVALID_FILENAME":
                raise RemoteRejectionError(
                    f"{method} {path} rejected: INVALID_FILENAME — the filename "
                    "does not satisfy the server's grammar.",
                    error_code="INVALID_FILENAME",
                )
            # 409 DUPLICATE_FILENAME / DUPLICATE_REFERENCE — uniqueness conflict.
            if status == 409 and error_code in ("DUPLICATE_FILENAME", "DUPLICATE_REFERENCE"):
                raise RemoteRejectionError(
                    f"{method} {path} rejected: {error_code} — "
                    "a note with that filename/reference already exists.",
                    error_code=str(error_code),
                )
            if status not in expected:
                raise NetworkError(f"{method} {path} returned {status}: {response.text[:200]}")
        if status == 204 or not response.content:
            return None
        return response.json()
