"""Write-command CLI output shape — minimal payload + --fields full escape hatch.

Every write-side subcommand (`create`, `edit`, `append`, `restore`, `rename`,
`upload`) defaults to `--fields minimal`, which emits a small summary dict
(id, filename, family, kind, tags, updated_at, permissions) instead of
the full note body. Tags are included because they're cheap and callers
frequently want to confirm `--add-tag` / `--remove-tag` landed without
paying for `--fields full`. Passing `--fields full` restores the full-read
behaviour.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from knoten.cli.main import app
from knoten.models import Note
from knoten.repositories.store import Store
from knoten.services.notes import ingest_note
from knoten.settings import load_settings

NOTE_ID = "11111111-1111-1111-1111-111111111111"
FILE_NOTE_ID = "22222222-2222-2222-2222-222222222222"
STORAGE_KEY = "att_abc123"
API_URL = "https://notes.test"

MINIMAL_KEYS = {
    "id",
    "filename",
    "title",
    "family",
    "kind",
    "tags",
    "permissions",
    "updated_at",
}

FORBIDDEN_KEYS = {"body", "frontmatter", "wikilinks", "backlinks"}


def _assert_minimal(payload: dict) -> None:
    """Minimal payloads carry identity + metadata + tags, never body/links."""
    assert MINIMAL_KEYS.issubset(payload.keys()), f"missing keys: {MINIMAL_KEYS - payload.keys()}"
    present_forbidden = FORBIDDEN_KEYS & payload.keys()
    assert not present_forbidden, f"unexpected keys leaked: {present_forbidden}"
    assert isinstance(payload["tags"], list)


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """Env wiring that lets the CLI find a per-test vault + mock server.

    The autouse `_isolate_paths` fixture already pins config / data / cache
    dirs under a per-test sandbox, so we only need to inject the API URL
    and token for the mocked remote backend.
    """
    monkeypatch.setenv("KNOTEN_API_URL", API_URL)
    monkeypatch.setenv("KNOTEN_API_TOKEN", "nt_test_token")
    return load_settings()


def _seed_permanent(settings, *, note_id: str = NOTE_ID) -> Note:
    note = Note(
        id=note_id,
        filename="! Seed",
        title="Seed",
        family="permanent",
        kind="permanent",
        source=None,
        body="first line",
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        permissions="ALL",
    )
    with Store(settings.paths.index_path) as store:
        ingest_note(note, store=store, vault_dir=settings.paths.vault_dir)
    return note


def _full_note_payload(**overrides) -> dict:
    base = {
        "id": NOTE_ID,
        "filename": "! Seed",
        "title": "Seed",
        "family": "permanent",
        "kind": "permanent",
        "source": None,
        "body": "first line\n\nsecond line with lots of text " * 50,
        "frontmatter": {"stub": "value"},
        "tags": ["alpha", "beta"],
        "linkMap": {},
        "permissions": "ALL",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-03T00:00:00Z",
    }
    base.update(overrides)
    return base


# ---- create -------------------------------------------------------------


def test_create_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["create", "--filename", "! Seed", "--body", "first line", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)
    assert payload["id"] == NOTE_ID


def test_create_fields_full_returns_full(cli_env, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "create",
            "--filename",
            "! Seed",
            "--body",
            "first line",
            "--fields",
            "full",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "body" in payload
    assert "first line" in payload["body"]
    assert "frontmatter" in payload
    assert "tags" in payload


# ---- edit ---------------------------------------------------------------


def test_edit_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    _seed_permanent(cli_env)
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="PUT",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["edit", NOTE_ID, "--title", "Seed updated", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)


def test_edit_batch_remote_mode(cli_env, httpx_mock: HTTPXMock) -> None:
    """`edit --batch` runs N PUT+GET round-trips under one process in remote mode."""
    import json as _json

    second_id = "44444444-4444-4444-4444-444444444444"
    _seed_permanent(cli_env, note_id=NOTE_ID)
    second = Note(
        id=second_id,
        filename="! Second",
        title="Second",
        family="permanent",
        kind="permanent",
        source=None,
        body="second line",
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        permissions="ALL",
    )
    with Store(cli_env.paths.index_path) as store:
        ingest_note(second, store=store, vault_dir=cli_env.paths.vault_dir)
    for note_id, filename in ((NOTE_ID, "! Seed"), (second_id, "! Second")):
        httpx_mock.add_response(
            url=f"{API_URL}/api/notes/{note_id}", method="PUT", json={"id": note_id}
        )
        httpx_mock.add_response(
            url=f"{API_URL}/api/notes/{note_id}",
            method="GET",
            json=_full_note_payload(id=note_id, filename=filename),
        )
    patches = [
        {"target": NOTE_ID, "title": "First updated"},
        {"target": second_id, "add_tags": ["batch"]},
    ]
    runner = CliRunner()
    result = runner.invoke(app, ["edit", "--batch", "-", "--json"], input=_json.dumps(patches))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["operation"] == "edit-batch"
    assert payload["edited"] == 2
    assert payload["failed"] == 0


# ---- append -------------------------------------------------------------


def test_append_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    _seed_permanent(cli_env)
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}/append",
        method="POST",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["append", NOTE_ID, "--content", "second line", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)


# ---- restore ------------------------------------------------------------


def test_restore_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    # A trashed note does not need to be locally seeded — restore starts
    # with a remote-only UUID and ingests into the mirror on refresh.
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}/restore",
        method="POST",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["restore", NOTE_ID, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)


# ---- rename -------------------------------------------------------------


def test_rename_default_is_minimal(cli_env, httpx_mock: HTTPXMock) -> None:
    _seed_permanent(cli_env)
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="PUT",
        json={"id": NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(filename="! Seed renamed", title="Seed renamed"),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["rename", NOTE_ID, "! Seed renamed", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)


def test_rename_refreshes_affected_notes(cli_env, httpx_mock: HTTPXMock) -> None:
    """When the server returns `affectedNotes`, the client re-fetches each
    and re-ingests it so the local mirror converges without a full sync.
    """
    _seed_permanent(cli_env)
    source_id = "33333333-3333-3333-3333-333333333333"
    source_note = Note(
        id=source_id,
        filename="- Source",
        title="Source",
        family="fleeting",
        kind="fleeting",
        source=None,
        body="See [[! Seed]] for context.",
        frontmatter={},
        tags=(),
        wikilinks=(),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        permissions="ALL",
    )
    with Store(cli_env.paths.index_path) as store:
        source_path = ingest_note(source_note, store=store, vault_dir=cli_env.paths.vault_dir)

    # Sanity: the source file on disk currently points at [[! Seed]].
    assert "[[! Seed]]" in (cli_env.paths.vault_dir / source_path).read_text()

    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="PUT",
        json={
            "id": NOTE_ID,
            "affectedNotes": [
                {
                    "id": source_id,
                    "filename": "- Source",
                    "updatedAt": "2024-01-03T00:00:00Z",
                }
            ],
        },
    )
    # Refresh the renamed note.
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(filename="! Seed renamed", title="Seed renamed"),
    )
    # Refresh the affected source note with its rewritten body.
    rewritten_source_payload = {
        "id": source_id,
        "filename": "- Source",
        "title": "Source",
        "family": "fleeting",
        "kind": "fleeting",
        "source": None,
        "body": "See [[! Seed renamed]] for context.",
        "frontmatter": {},
        "tags": [],
        "linkMap": {},
        "permissions": "ALL",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-03T00:00:00Z",
    }
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{source_id}",
        method="GET",
        json=rewritten_source_payload,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["rename", NOTE_ID, "! Seed renamed", "--json"])
    assert result.exit_code == 0, result.output

    # The local mirror of the source note must now contain the new wikilink.
    refreshed_body = (cli_env.paths.vault_dir / source_path).read_text()
    assert "[[! Seed renamed]]" in refreshed_body
    assert "[[! Seed]]" not in refreshed_body


def test_rename_with_forbidden_affected_note_succeeds_and_placeholders_it(
    cli_env, httpx_mock: HTTPXMock
) -> None:
    """A rename whose `affectedNotes` names an id the token cannot READ must
    still succeed: the readable affected note is mirrored, the forbidden one
    is downgraded to a placeholder (not a hard failure), the loop continues,
    and the operation reports the restricted count.

    Regression: the server's cascade rewrites every linking note regardless
    of this token's per-note permissions, so a per-note 404 on the refresh
    must not turn a successful rename into an exit-1 failure.
    """
    _seed_permanent(cli_env)
    readable_id = "33333333-3333-3333-3333-333333333333"
    forbidden_id = "55555555-5555-5555-5555-555555555555"

    def _link_note(note_id: str, filename: str, title: str) -> Note:
        return Note(
            id=note_id,
            filename=filename,
            title=title,
            family="fleeting",
            kind="fleeting",
            source=None,
            body="See [[! Seed]] for context.",
            frontmatter={},
            tags=(),
            wikilinks=(),
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-02T00:00:00Z",
            permissions="ALL",
        )

    # Both affected notes start as full mirror rows (a prior sync ingested
    # them); the rename later cascades into both.
    with Store(cli_env.paths.index_path) as store:
        readable_path = ingest_note(
            _link_note(readable_id, "- Readable", "Readable"),
            store=store,
            vault_dir=cli_env.paths.vault_dir,
        )
        forbidden_path = ingest_note(
            _link_note(forbidden_id, "- Forbidden", "Forbidden"),
            store=store,
            vault_dir=cli_env.paths.vault_dir,
        )

    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="PUT",
        json={
            "id": NOTE_ID,
            "affectedNotes": [
                {"id": readable_id, "filename": "- Readable"},
                {"id": forbidden_id, "filename": "- Forbidden"},
            ],
        },
    )
    # Refresh the renamed note.
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{NOTE_ID}",
        method="GET",
        json=_full_note_payload(filename="! Seed renamed", title="Seed renamed"),
    )
    # The readable affected note refreshes with its rewritten body.
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{readable_id}",
        method="GET",
        json={
            "id": readable_id,
            "filename": "- Readable",
            "title": "Readable",
            "family": "fleeting",
            "kind": "fleeting",
            "source": None,
            "body": "See [[! Seed renamed]] for context.",
            "frontmatter": {},
            "tags": [],
            "linkMap": {},
            "permissions": "ALL",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-03T00:00:00Z",
        },
    )
    # The forbidden affected note returns a per-note 404 → NoteForbiddenError.
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{forbidden_id}",
        method="GET",
        status_code=404,
        json={"error": "NOT_FOUND"},
    )

    runner = CliRunner()
    result = runner.invoke(app, ["rename", NOTE_ID, "! Seed renamed", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["restricted_affected"] == 1

    # Readable affected note: mirrored with the rewritten wikilink.
    readable_body = (cli_env.paths.vault_dir / readable_path).read_text()
    assert "[[! Seed renamed]]" in readable_body

    # Forbidden affected note: downgraded to a placeholder row + file.
    with Store(cli_env.paths.index_path) as store:
        row = store.find_by_id(forbidden_id)
    assert row is not None
    assert row["restricted"] == 1
    placeholder_body = (cli_env.paths.vault_dir / forbidden_path).read_text()
    assert "Body not fetchable" in placeholder_body


# ---- upload -------------------------------------------------------------


def test_upload_default_is_minimal_but_keeps_upload_block(
    cli_env, httpx_mock: HTTPXMock, tmp_path
) -> None:
    sample = tmp_path / "scan.pdf"
    sample.write_bytes(b"PDFDATA")

    httpx_mock.add_response(
        url=f"{API_URL}/api/attachments",
        method="POST",
        status_code=201,
        json={
            "storageKey": STORAGE_KEY,
            "sizeBytes": "7",
            "contentType": "application/pdf",
            "url": f"/api/attachments/{STORAGE_KEY}",
        },
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes",
        method="POST",
        json={"id": FILE_NOTE_ID},
    )
    httpx_mock.add_response(
        url=f"{API_URL}/api/notes/{FILE_NOTE_ID}",
        method="GET",
        json={
            "id": FILE_NOTE_ID,
            "filename": "2024-11-10+ scan.pdf",
            "title": "scan.pdf",
            "family": "file",
            "kind": "file",
            "source": "2024-11-10",
            "body": "body text that should NOT appear in minimal output " * 30,
            "frontmatter": {"attachment": STORAGE_KEY},
            "tags": [],
            "linkMap": {},
            "permissions": "ALL",
            "createdAt": "2024-11-10T00:00:00Z",
            "updatedAt": "2024-11-10T00:00:00Z",
        },
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "upload",
            str(sample),
            "--filename",
            "2024-11-10+ scan.pdf",
            "--content-type",
            "application/pdf",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_minimal(payload)
    # Upload metadata always survives — it is the whole point of the command.
    assert payload["upload"]["storage_key"] == STORAGE_KEY
    assert payload["upload"]["content_type"] == "application/pdf"
    assert payload["upload"]["size_bytes"] == 7
