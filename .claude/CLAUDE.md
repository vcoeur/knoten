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

- **Reads never touch the network.** Any command that does not hit the server goes straight to the local mirror + SQLite index. The only commands that touch the server are the sync family (`sync`, `verify`) and the mutation family (`create`, `edit`, `append`, `delete`, `rename`, `restore`, `upload`, `download`). Everything else — `search`, `read`, `list`, `backlinks`, `tags`, `kinds`, `graph`, `status`, `config`, `path`, `reindex` — is local-only.
- **Writes always touch the network.** The mutation commands call the REST API first; only after a 2xx do they re-fetch and mirror locally. The local mirror is never authoritative.
- **`search --remote`** is the single opt-in override for "I want the server's view instead of the local index."
- **Sync never runs implicitly.** If the mirror is stale, the user (or Claude) must run `kasten sync`.

## Paths

Default layout — `KASTEN_HOME` anchors a vault + state pair:

- `$KASTEN_HOME/kasten/` — markdown mirror (gitignored)
- `$KASTEN_HOME/.kasten-state/index.sqlite` — metadata + FTS5 (gitignored)
- `$KASTEN_HOME/.kasten-state/state.json` — sync cursor, schema version (gitignored)
- `$KASTEN_HOME/.kasten-state/sync.lock` — fcntl lock held during sync / writes
- `$KASTEN_HOME/.env` — API URL + token (gitignored)

### How `KASTEN_HOME` is resolved

The resolution tries, in order: shell env → any `.env` layer that sets it → source-tree walk → `~/.kasten` fallback. Two runtime contexts matter:

- **Dev** (`uv run kasten …`, `make sync` from the repo): `_default_home()` walks up from `__file__` and finds the repo root via its `pyproject.toml`. The repo's `.env` is read automatically. No user config needed.
- **Installed** (`uv tool install .` → `~/.local/bin/kasten`): `__file__` lives in a uv tools venv's `site-packages/`, so the pyproject walk is skipped. The CLI reads `~/.config/kasten/.env` first — that file typically only carries `KASTEN_HOME=/path/to/repo`, which then triggers a second layer read of `$KASTEN_HOME/.env` for the API URL + token. No secret duplication.

### `.env` layering

`load_settings()` reads multiple `.env` files in priority order with `override=False` — **first value wins**, later files fill in missing keys:

1. `env_file` arg — explicit, tests / advanced callers
2. `~/.config/kasten/.env` — user-level pointer (installed CLI)
3. `$KASTEN_HOME/.env` — once `KASTEN_HOME` is known from layer 2 or the process env
4. `_default_home() / .env` — dev workflow (repo or `~/.kasten`)

Process env vars always win over any file layer (environs `override=False`).

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
4. When changing the store schema: bump `SCHEMA_VERSION` in `app/repositories/store.py` and document the migration in the commit message.

## Key code locations

- CLI entrypoint: `app/cli/main.py` — one Typer function per subcommand, all wiring identical (load → lock → Store → service → render).
- HTTP client: `app/repositories/http_client.py` — all exceptions inherit from `app/repositories/errors.py`.
- Store: `app/repositories/store.py` — schema in `_SCHEMA`, all queries are explicit SQL strings (no ORM).
- FTS5 search: `Store.search()` — weights `(1.0, 10.0, 1.0, 5.0)` map to (note_id, title, body, filename). Keep the UNINDEXED `note_id` weight in the list or bm25 silently mis-weights.
- Fuzzy search: `Store.search_fuzzy()` — combines a second FTS5 virtual table `notes_fts_trigram` (tokenize='trigram', used for substring matching) with a `rapidfuzz.process.extract` pass over titles+filenames. Both FTS tables must stay in sync: `upsert_note`, `upsert_placeholder`, and `delete_note` all write to both, and `fts_cardinality_check` covers both. Combined score = rapidfuzz WRatio (0..100) + 30 if the note is also a trigram substring hit.
- Sync orchestration: `app/services/sync.py` — `incremental_sync` is the real work; `full_sync` delegates to it with a cleared cursor.
- File writing: `app/repositories/vault_files.py` — atomic writes via tmp + rename, path derivation in `path_for_note`. The writer validates that the server-provided relative path stays inside the vault before touching disk.

## Testing conventions

- pytest-httpx to mock the remote. The fixture `_clear_proxy_env` in `tests/conftest.py` strips system proxy env vars so httpx.Client doesn't try to wire up SOCKS during tests.
- SQLite tests run against a real per-test sqlite file in a `tmp_path` — no mocks.
- Ingest tests verify both the file on disk and the store row in one go (`tests/test_ingest_and_files.py`).
