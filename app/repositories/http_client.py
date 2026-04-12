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

from app.repositories.errors import AuthError, NetworkError, NoteForbiddenError
from app.settings import Settings


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
        kind: str | None = None,
        family: str | None = None,
        tag: str | None = None,
        ref: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/notes — returns {data, total, limit, offset}.

        Items are sorted by updated_at DESC. Body is NOT included — fetch with
        `read_note(id)` to get the full content.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if kind:
            params["kind"] = kind
        if family:
            params["family"] = family
        if tag:
            params["tag"] = tag
        if ref:
            params["ref"] = ref
        return self._get_json("/api/notes", params=params)

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
        """GET /api/notes/{id} — full note with body, linkMap, imageMap.

        Raises `NoteForbiddenError` on 404. The server deliberately returns
        404 for notes the current token cannot READ (restricted viewer +
        `mcpPermissions = LIST`), conflating "does not exist" and
        "forbidden" to avoid leaking existence. Sync catches this and
        falls back to a metadata-only placeholder.
        """
        try:
            return self._request("GET", f"/api/notes/{note_id}", note_id=note_id)
        except NoteForbiddenError:
            raise

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

    # ---- Backlinks / graph / metadata ------------------------------------

    def backlinks(self, note_id: str) -> list[dict[str, Any]]:
        data = self._get_json(f"/api/backlinks/{note_id}")
        return data if isinstance(data, list) else data.get("data", [])

    def tags(self) -> list[dict[str, Any]]:
        data = self._get_json("/api/tags")
        return data if isinstance(data, list) else data.get("data", [])

    def kinds(self, family: str | None = None) -> list[dict[str, Any]] | list[str]:
        path = f"/api/kinds/{family}" if family else "/api/kinds"
        data = self._get_json(path)
        return data if isinstance(data, list) else data.get("data", [])

    def remote_search(
        self,
        query: str,
        *,
        kind: str | None = None,
        family: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if kind:
            params["kind"] = kind
        if family:
            params["family"] = family
        if tag:
            params["tag"] = tag
        data = self._get_json("/api/search", params=params)
        return data if isinstance(data, list) else data.get("data", [])

    # ---- Export / bulk ---------------------------------------------------

    def download_export(self, destination: Path) -> Path:
        """GET /api/export — stream the full vault zip to `destination`."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream("GET", "/api/export") as response:
                if response.status_code == 401 or response.status_code == 403:
                    raise AuthError(f"Auth failed on /api/export: {response.status_code}")
                if response.status_code >= 400:
                    raise NetworkError(f"GET /api/export returned {response.status_code}")
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            raise NetworkError(f"Cannot reach {self._settings.api_url}: {exc}") from exc
        return destination

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
