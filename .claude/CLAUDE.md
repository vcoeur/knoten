# CLAUDE.md — KastenManager

Local CLI mirror and search tool for `notes.vcoeur.com`. Primary consumer is Claude itself (via a `kasten` skill); humans are a secondary audience.

## Project type

- **Not deployed.** Per-laptop tool, installed globally via `uv tool install .`.
- **No daemon / server.** Every invocation is a short-lived CLI process.
- **Remote is the source of truth.** The local mirror is never authoritative. Writes go to `notes.vcoeur.com` first, then the affected note is re-fetched and upserted locally.

## Stack

- Python 3.12+, `uv`-managed
- Typer (CLI) + httpx (sync HTTP) + stdlib `sqlite3` (+ FTS5) + markdown-it-py + rich + environs + pytest + pytest-httpx
- No GUI. No ORM. No async.

## Architecture

Strict layers — imports only go downward.

```
app/
  models/        <- pure dataclasses, no I/O
  repositories/  <- http_client, store, vault_files, lock, sync_state, errors
  services/      <- sync, notes (read/write), markdown_parser, note_mapper
  cli/           <- Typer app + rich/JSON output helpers
  settings.py    <- environs config
```

Layer rules:

- **Models** import nothing from this project.
- **Repositories** import from models.
- **Services** import from models + repositories. No I/O of their own beyond calling a repository.
- **CLI** is the wiring layer: Typer command → load Settings → open Store (+ NotesClient if remote is needed) → call service → render via `app/cli/output.py`.

## Read vs write

The single most important rule for anyone (especially Claude) using this CLI:

- **Reads never touch the network.** `search`, `read`, `list`, `backlinks`, `tags`, `kinds`, `status`, `path` all resolve against the local mirror + SQLite index.
- **Writes always touch the network.** `create`, `edit`, `delete`, `rename`, `restore` call the REST API first; only after a 2xx do they re-fetch and mirror locally.
- **Sync never runs implicitly.** If the mirror is stale, the user (or Claude) must run `kasten sync`.

## Paths

Default layout — all inside the repo:

- `./kasten/` — markdown mirror (gitignored)
- `./.kasten-state/index.sqlite` — metadata + FTS5 (gitignored)
- `./.kasten-state/state.json` — sync cursor, schema version (gitignored)
- `./.kasten-state/sync.lock` — fcntl lock held during sync / writes
- `./.env` — API URL + token (gitignored)

Override with `KASTEN_HOME` in `.env`.

## Commands

```bash
make dev-install   # install all deps incl. dev
make test          # pytest
make lint          # ruff check + format --check
make format        # ruff check --fix + format
make sync          # incremental sync
make sync-full     # full refetch
make tool-install  # install `kasten` globally via `uv tool install`
```

## Workflow

1. After any code change: `make format` — enforced by ruff.
2. Before committing: `make lint && make test`.
3. When adding a new CLI command: add a test in `tests/test_cli_smoke.py` that at minimum runs it with `--json` on an empty store to catch wiring regressions.
4. When changing the store schema: bump `SCHEMA_VERSION` in `app/repositories/store.py` and update the design note at `~/src/vcoeur/conception/projects/2026-04-12-kasten-manager/notes/index-schema.md`.

## Key code locations

- CLI entrypoint: `app/cli/main.py` — one Typer function per subcommand, all wiring identical (load → lock → Store → service → render).
- HTTP client: `app/repositories/http_client.py` — all exceptions inherit from `app/repositories/errors.py`.
- Store: `app/repositories/store.py` — schema in `_SCHEMA`, all queries are explicit SQL strings (no ORM).
- FTS5 search: `Store.search()` — weights `(1.0, 10.0, 1.0, 5.0)` map to (note_id, title, body, filename). Keep the UNINDEXED `note_id` weight in the list or bm25 silently mis-weights.
- Sync orchestration: `app/services/sync.py` — `incremental_sync` is the real work; `full_sync` delegates to it with a cleared cursor.
- File writing: `app/repositories/vault_files.py` — atomic writes via tmp + rename, path derivation in `path_for_note`.

## Testing conventions

- pytest-httpx to mock the remote. The fixture `_clear_proxy_env` in `tests/conftest.py` strips system proxy env vars so httpx.Client doesn't try to wire up SOCKS during tests.
- SQLite tests run against a real per-test sqlite file in a `tmp_path` — no mocks.
- Ingest tests verify both the file on disk and the store row in one go (`tests/test_ingest_and_files.py`).

## Related projects

- `~/src/vcoeur/notes.vcoeur.com/` — the remote source of truth (TypeScript, Hono, Drizzle, Postgres). Docs under `docs/api.md`, `docs/auth.md`, `docs/datamodel.md`.
- `~/src/vcoeur/PaintingManager/` — Python CLI with the same architectural conventions (layered app, uv, atomic writes). Copy patterns from there first before inventing new ones.
- `~/src/vcoeur/conception/projects/2026-04-12-kasten-manager/` — conception project with all design notes.
