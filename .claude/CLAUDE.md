# CLAUDE.md — knoten

Standalone zettelkasten CLI. Primary consumer is Claude itself (via a `knoten` skill); humans are a secondary audience.

## Project type

- **Not deployed.** Per-laptop tool, installed globally via `uv tool install .`.
- **No daemon / server.** Every invocation is a short-lived CLI process.
- **Two modes** (see `## Backends` below): `local` operates on an on-disk vault as the source of truth; `remote` mirrors a `notes.vcoeur.com` instance with the server as authority. `KNOTEN_MODE=auto` (default) picks local when `KNOTEN_API_URL` is empty and remote otherwise.

## Stack

- Python 3.12+, `uv`-managed
- Typer (CLI) + httpx (sync HTTP) + stdlib `sqlite3` (+ FTS5) + markdown-it-py + rich + environs + pytest + pytest-httpx
- No GUI. No ORM. No async.

## Architecture

Strict layers — imports only go downward.

```
knoten/
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
- **CLI** is the wiring layer: Typer command → load Settings → open Store → build a `Backend` via `_build_backend(settings)` → call service → render via `knoten/cli/output.py`. Commands never reach into `RemoteBackend` / `LocalBackend` directly.

## Backends

Every CLI command talks to a single `Backend` protocol defined in `knoten/repositories/backend.py`. Two implementations live side by side:

| Backend | File | Source of truth | Network | Auth |
|---|---|---|---|---|
| `RemoteBackend` | `knoten/repositories/remote_backend.py` | `notes.vcoeur.com` server | every mutation + sync | `KNOTEN_API_TOKEN` (Bearer) |
| `LocalBackend` | `knoten/repositories/local_backend.py` | the on-disk markdown vault | never | none |

The protocol has 8 business methods: `list_note_summaries`, `read_note`, `create_note`, `update_note` (returns `NoteUpdateResult` with the rename cascade's `affected_notes` list), `append_to_note`, `delete_note`, `restore_note`, `upload_attachment`, `download_attachment`, plus `close`. Shapes live in `knoten/repositories/backend.py` as frozen dataclasses — `NotesPage`, `NoteDraft`, `NotePatch`, `NoteUpdateResult`, `AttachmentUploadResult`, `AttachmentDownloadResult`. Services depend on the protocol, not on either implementation, so the same `edit_note_remote` / `create_note_remote` helpers drive both backends.

Selection: `knoten/cli/main.py:_build_backend` reads `settings.effective_mode` — `auto` resolves to `local` when `KNOTEN_API_URL` is empty and `remote` otherwise; explicit `KNOTEN_MODE=local` / `remote` overrides the URL-based inference.

**LocalBackend specifics** (the ones Claude most often needs to know when debugging):

- **Stat-walk reindex.** `LocalBackend._refresh_index_if_stale` runs at most once per CLI invocation at the top of every read method. It walks `vault_dir/*.md` (skipping `.trash/` and `.attachments/`), compares each file's `(mtime_ns, size)` against the `notes.path_mtime_ns / path_size` columns, re-parses drifted files via `parse_body`, and hard-deletes store rows whose path is missing on disk. External `rm foo.md` is a permanent delete in local mode — there is no trash copy to restore from, only `knoten delete` moves to `.trash/`.
- **Soft delete.** `knoten delete` moves the file from `<vault>/<path>` to `<vault>/.trash/<path>` and shifts the store row from `notes` into the separate `trashed_notes` table (schema v6). `knoten restore` is the inverse, with a collision check — if a new note was created under the same filename while this one was in the trash, restore raises `UserError` asking the user to rename one of them first.
- **Attachments.** `knoten upload` writes the blob to `<vault>/.attachments/<uuid><ext>` and records a row in the `attachments` table (schema v7). `knoten download` streams it back. Storage keys are plain UUIDs with the original extension preserved — no content hashing, no dedup (v0.1 simplicity).
- **Rename cascade.** `LocalBackend._rename_with_cascade` queries `wikilinks WHERE target_title = <old_filename>` for every source note referencing the renamed one, regex-rewrites `[[old]]` and `[[old|alias]]` forms in each source body, and re-ingests both the target and the sources. Rollback on partial failure restores the original bytes of every file it touched before re-raising. Heading-form wikilinks (`[[target#heading]]`) are a documented limitation — the local markdown parser regex does not capture the heading suffix, so they are invisible to the cascade (see `tests/test_local_backend_rename.py::test_rename_heading_wikilink_is_not_cascaded`).
- **No permission model.** `NoteForbiddenError` cannot happen in local mode — the placeholder branch in the sync path is dead code for local-only users and is kept only for symmetry with `RemoteBackend`.

**Tests**: `tests/test_cli_local_mode.py` is the integration anchor — drives every CLI command end-to-end against a `tmp_path` vault with `KNOTEN_MODE=local` and zero network. Read-path and write-path unit tests live in `tests/test_local_backend_reads.py`, `tests/test_local_backend_reindex.py`, `tests/test_local_backend_writes.py`, and `tests/test_local_backend_rename.py`.

## Read vs write

The single most important rule for anyone (especially Claude) using this CLI:

- **Reads never touch the network** in either mode. Reads resolve against the local mirror + SQLite index. The only commands that might touch the server are the sync family (`sync`, `verify`) and the mutation family (`create`, `edit`, `append`, `delete`, `rename`, `restore`, `upload`, `download`) — and even those go through the `Backend` protocol so they become filesystem ops in local mode.
- **In remote mode, writes always touch the network first.** Mutations call the REST API first; only after a 2xx do they re-fetch and mirror locally. The local mirror is never authoritative in remote mode.
- **In local mode, writes go straight to disk.** The vault is authoritative; SQLite is derived. `_refresh_index_if_stale` catches up to external edits at the top of every read method.
- **Sync never runs implicitly.** If the remote-mode mirror is stale, the user (or Claude) must run `knoten sync`. In local mode `knoten sync` is a shortcut for the stat-walk reindex.

## Paths

As of v0.2 the layout is cross-OS via `platformdirs` (see `knoten/paths.py`). Three roles, three directories:

- **config_dir** — holds `.env` only. Linux: `~/.config/knoten/`. macOS: `~/Library/Application Support/knoten/`. Windows: `%APPDATA%\knoten\`.
- **data_dir** — holds the markdown vault under `kasten/` (user content). Linux: `~/.local/share/knoten/`. macOS: `~/Library/Application Support/knoten/`. Windows: `%LOCALAPPDATA%\knoten\`.
- **cache_dir** — holds the SQLite index (`index.sqlite`), sync cursor (`state.json`), advisory lock (`sync.lock`), and tmp scratch. Linux: `~/.cache/knoten/`. macOS: `~/Library/Caches/knoten/`. Windows: `%LOCALAPPDATA%\knoten\Cache\`.

Each dir can be overridden via `KNOTEN_CONFIG_DIR` / `KNOTEN_DATA_DIR` / `KNOTEN_CACHE_DIR` — process env wins over the file, tests / Docker / custom deployments set these directly. `knoten config path` prints the resolved paths.

### Dev mode vs installed mode

`paths._repo_root()` walks up from `__file__` looking for `pyproject.toml`; if it finds one (and `__file__` is not inside a site-packages / uv tools venv), that's dev mode.

- **Dev** (`uv run knoten …` from the repo): `config_dir = data_dir = <repo>`, `cache_dir = <repo>/.dev-state/cache/`. The markdown vault stays at `<repo>/kasten/` — historic location, unchanged from pre-v0.2, so the maintainer's dev notes don't move. Migration is skipped in dev mode for the same reason.
- **Installed** (`pipx install knoten` / `uv tool install knoten`): platformdirs defaults, resolved from `APP_NAME="knoten"` with `appauthor=False`.

### Legacy migration (v0.1.x → v0.2)

`migrate_legacy_layout` runs inside `load_settings` right after `paths.resolve()`, before `ensure_dirs`. Idempotent, survives OS errors, silent on clean state. Source is `$KNOTEN_HOME/` (or `~/.knoten/` if unset), target is the new platformdirs layout. Ephemeral files (sync lock, tmp) are not migrated — they rebuild on demand. See `knoten/migrate.py` for the exact rules and `tests/test_migrate.py` for the full contract.

`KNOTEN_HOME` is still consulted by the migration so legacy users who pointed it at a non-default directory don't lose their vault. If it's still set after migration, the CLI prints a one-time deprecation warning telling the user to use `KNOTEN_*_DIR` instead.

### One `.env` per invocation

`load_settings()` reads **exactly one** `.env` file: `settings.paths.env_file`, which lives at `config_dir / .env`. In dev mode that's `<repo>/.env`; in installed mode it's `~/.config/knoten/.env` (or the OS equivalent). Process env vars always win over the file (`environs.read_env(override=False)`).

## Commands

```bash
make dev-install   # install all deps incl. dev
make test          # pytest
make lint          # ruff check + format --check
make format        # ruff check --fix + format
make sync          # incremental sync
make sync-full     # full refetch
make tool-install  # install `knoten` globally via `uv tool install`
```

## Workflow

1. After any code change: `make format` — enforced by ruff.
2. Before committing: `make lint && make test`.
3. When adding a new CLI command: add a test in `tests/test_cli_smoke.py` that at minimum runs it with `--json` on an empty store to catch wiring regressions.
4. When changing the store schema: bump `SCHEMA_VERSION` in `knoten/repositories/store.py` and document the migration in the commit message.

## Key code locations

- CLI entrypoint: `knoten/cli/main.py` — one Typer function per subcommand, all wiring identical (load → lock → Store → `_build_backend` → service → render).
- Backend protocol + data types: `knoten/repositories/backend.py`.
- Remote implementation: `knoten/repositories/remote_backend.py` — bearer-token httpx wrapper. All exceptions inherit from `knoten/repositories/errors.py`.
- Local implementation: `knoten/repositories/local_backend.py` — filesystem + Store. See the `## Backends` section for the LocalBackend-specific gotchas (stat walk, trash, rename cascade).
- Filename parser: `knoten/services/knoten_filename.py` — Python port of `notes.vcoeur.com/packages/shared/src/kasten.ts`. LocalBackend uses it to derive family + source from a `NoteDraft.filename` on create and rename.
- Store: `knoten/repositories/store.py` — schema in `_SCHEMA`, all queries are explicit SQL strings (no ORM).
- FTS5 search: `Store.search()` — weights `(1.0, 10.0, 1.0, 5.0)` map to (note_id, title, body, filename). Keep the UNINDEXED `note_id` weight in the list or bm25 silently mis-weights.
- Fuzzy search: `Store.search_fuzzy()` — combines a second FTS5 virtual table `notes_fts_trigram` (tokenize='trigram', used for substring matching) with a `rapidfuzz.process.extract` pass over titles+filenames. Both FTS tables must stay in sync: `upsert_note`, `upsert_placeholder`, and `delete_note` all write to both, and `fts_cardinality_check` covers both. Combined score = rapidfuzz WRatio (0..100) + 30 if the note is also a trigram substring hit.
- Sync orchestration: `knoten/services/sync.py` — `incremental_sync` is the real work; `full_sync` delegates to it with a cleared cursor.
- File writing: `knoten/repositories/vault_files.py` — atomic writes via tmp + rename, path derivation in `path_for_note`. The writer validates that the server-provided relative path stays inside the vault before touching disk.

## Testing conventions

- pytest-httpx to mock the remote. The fixture `_clear_proxy_env` in `tests/conftest.py` strips system proxy env vars so httpx.Client doesn't try to wire up SOCKS during tests.
- SQLite tests run against a real per-test sqlite file in a `tmp_path` — no mocks.
- Ingest tests verify both the file on disk and the store row in one go (`tests/test_ingest_and_files.py`).
