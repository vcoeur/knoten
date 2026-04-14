"""HTTP client for notes.vcoeur.com.

Thin typed wrapper around httpx. All methods raise the exception types from
`app.repositories.errors` — the CLI layer maps those to exit codes. Response
payloads are returned as dicts; model construction happens in services.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from app.repositories.errors import AuthError, NetworkError, NoteForbiddenError, NotFoundError
from app.settings import Settings


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


class NotesClient:
    """Bearer-token client for the notes.vcoeur.com REST API."""

    def __init__(self, settings: Settings) -> None:
        if not settings.api_token:
            raise AuthError(
                "KASTEN_API_TOKEN is not set. Copy .env.example to .env and add a token.",
            )
        self._settings = settings
        self._client = httpx.Client(
            base_url=settings.api_url,
            headers={
                "Authorization": f"Bearer {settings.api_token}",
                "Accept": "application/json",
                "User-Agent": "kasten-manager/0.1",
            },
            timeout=settings.http_timeout,
        )

    # Context manager so callers can use `with NotesClient(...) as c:`
    def __enter__(self) -> NotesClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---- Notes ------------------------------------------------------------

    def list_notes(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /api/notes — returns {data, total, limit, offset}.

        Items are sorted by updated_at DESC. Body is NOT included — fetch with
        `read_note(id)` to get the full content.
        """
        return self._get_json("/api/notes", params={"limit": limit, "offset": offset})

    def iter_all_summaries(
        self,
        *,
        page_size: int = 200,
        stop_when_older_than: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every active note summary, oldest-new-first stopping aside.

        If `stop_when_older_than` is set (ISO-8601 string), pagination stops
        once a page's newest item has `updated_at <= stop_when_older_than` —
        the caller is using this for incremental sync and no longer cares
        about older items.
        """
        offset = 0
        while True:
            page = self.list_notes(limit=page_size, offset=offset)
            data: list[dict[str, Any]] = page.get("data", [])
            if not data:
                return
            yield from data
            # DESC order: if the last (oldest) item on this page is older
            # than the cursor, the next page is entirely old — stop.
            if (
                stop_when_older_than is not None
                and data[-1].get("updated_at", "") <= stop_when_older_than
            ):
                return
            if len(data) < page_size:
                return
            offset += page_size

    def read_note(self, note_id: str) -> dict[str, Any]:
        """GET /api/notes/{id} — full note with body and linkMap.

        Raises `NoteForbiddenError` on 404. The server deliberately returns
        404 for notes the current token cannot READ (restricted viewer +
        `mcpPermissions = LIST`), conflating "does not exist" and
        "forbidden" to avoid leaking existence. Sync catches this and
        falls back to a metadata-only placeholder.
        """
        return self._request("GET", f"/api/notes/{note_id}", note_id=note_id)

    def create_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/notes — returns the created note (with body)."""
        return self._post_json("/api/notes", json=payload, expected=(200, 201))

    def update_note(self, note_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """PUT /api/notes/{id} — returns the updated note."""
        return self._put_json(f"/api/notes/{note_id}", json=payload)

    def append_note(self, note_id: str, content: str) -> dict[str, Any]:
        """POST /api/notes/{id}/append — append content to the body.

        Uses the dedicated server endpoint that enforces `APPEND` (rather
        than `WRITE`) on the note's `mcpPermissions` level. Returns the
        updated note in the same shape as `read_note`.
        """
        return self._post_json(
            f"/api/notes/{note_id}/append", json={"content": content}, expected=(200, 201)
        )

    def delete_note(self, note_id: str) -> None:
        """DELETE /api/notes/{id} — soft delete (trash)."""
        self._request("DELETE", f"/api/notes/{note_id}")

    def restore_note(self, note_id: str) -> dict[str, Any]:
        """POST /api/notes/{id}/restore."""
        return self._post_json(f"/api/notes/{note_id}/restore", json={}, expected=(200, 201))

    # ---- Attachments -----------------------------------------------------

    def upload_attachment(
        self,
        path: Path,
        *,
        content_type: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/attachments — multipart upload.

        Streams `path` to the server as a `file` form field and returns the
        parsed JSON body. The response always includes `storageKey`, which
        the caller uses to link a file-family note to the uploaded blob.
        """
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
                "check KASTEN_API_TOKEN scope (needs web, api, or mcp)."
            )
        if response.status_code not in (200, 201):
            raise NetworkError(
                f"POST /api/attachments returned {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    def download_attachment(self, storage_key: str, destination: Path) -> dict[str, Any]:
        """GET /api/attachments/{key} — stream to `destination`.

        Returns a dict with the bytes written and the server-provided
        content type / filename (parsed from Content-Disposition when
        available). Caller is responsible for choosing `destination`.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        content_type = ""
        disposition_filename: str | None = None
        try:
            with self._client.stream("GET", f"/api/attachments/{storage_key}") as response:
                if response.status_code in (401, 403):
                    raise AuthError(
                        f"GET /api/attachments/{storage_key} returned {response.status_code} — "
                        "check KASTEN_API_TOKEN scope."
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
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                        written += len(chunk)
        except httpx.HTTPError as exc:
            raise NetworkError(f"Cannot reach {self._settings.api_url}: {exc}") from exc
        return {
            "path": destination,
            "bytes_written": written,
            "content_type": content_type,
            "filename": disposition_filename,
        }

    # ---- Internal helpers -------------------------------------------------

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
    ) -> Any:
        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise NetworkError(f"{method} {path} failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise AuthError(
                f"{method} {path} returned {response.status_code} — check KASTEN_API_TOKEN scope."
            )
        if response.status_code == 503:
            raise NetworkError(f"{method} {path} returned 503 — vault locked on the remote.")
        # Per-note 404 on a read is the server's way of saying "this note
        # does not exist, or you don't have READ permission on it" — the
        # server deliberately conflates the two. We surface this as a
        # dedicated exception so sync can create a placeholder instead of
        # crashing the whole run.
        if response.status_code == 404 and note_id is not None:
            raise NoteForbiddenError(note_id)
        if response.status_code not in expected and response.status_code >= 400:
            raise NetworkError(
                f"{method} {path} returned {response.status_code}: {response.text[:200]}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()
