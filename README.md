# KastenManager

Local CLI mirror and search tool for [notes.vcoeur.com](https://notes.vcoeur.com), designed to be driven by **Claude Code via skills**. Reads are fast and offline against a local SQLite FTS5 index; writes go to the remote first and are then refreshed locally, so the remote is always the source of truth.

> **Not deployed anywhere.** This is a per-laptop tool. Install it on each machine you use, point it at the same `notes.vcoeur.com`, and let each local mirror catch up independently.

## What it does

- **`kasten sync`** — pull new / changed notes from `notes.vcoeur.com` into a local markdown mirror and SQLite FTS5 index. Always runs delete detection and reconciliation (re-fetch missing files, remove orphans). Add `--verify` for full body-hash verification.
- **`kasten verify`** — run SQLite integrity check, FTS5 / notes cardinality check, file existence + orphan cleanup. Add `--hashes` to compare every file against its recorded body hash.
- **`kasten reindex`** — rebuild derived tables (FTS5, tags, wikilinks, frontmatter fields) from the `notes` table + on-disk files. No network. Use when `verify` reports FTS5 drift or when you are offline.
- **`kasten search "query"`** — full-text search on the local index, with snippets, ranking (title > filename > body), filters (`--family`, `--kind`, `--tag`), JSON output.
- **`kasten read <id|filename>`** — full note body + wiki-links + backlinks, resolved from the local mirror (no network hit).
- **`kasten backlinks <target>`**, **`kasten list`**, **`kasten tags`**, **`kasten kinds`** — metadata queries, all offline.
- **`kasten graph <target> --depth N --direction out|in|both`** — BFS wiki-link neighbourhood for broadened search. Returns nodes with their distance from the start, plus edges. Depth 0-5.
- **`kasten create`**, **`kasten edit`**, **`kasten delete`**, **`kasten restore`**, **`kasten rename`** — write operations that hit `notes.vcoeur.com` first, then refresh the affected note locally. The local mirror is never authoritative.
- **`kasten status`** / **`kasten config`** — inspect the mirror and effective config without touching the network.

All commands accept `--json` for machine-parseable output. On a TTY without `--json`, output is rendered with rich (tables, snippet highlighting). Claude skills should always pass `--json`.

### Verbose output by default

In TTY mode, long-running commands (`sync`, `verify`, `reindex`) print phase-by-phase status to stderr so you can see exactly what is happening — every page fetched, every note downloaded, every deletion, every orphan removed. A rich summary table follows on stdout. In `--json` mode, stderr is silent and only the final JSON result is emitted to stdout.

Example:

```
$ kasten sync
→ Syncing from https://notes.vcoeur.com
  cursor: notes updated after 2026-04-12T08:25:54Z
  page 1: 100 items, 3 newer than cursor (remote total 2041)
    ↓ fetching '! New core insight'
    ↓ fetching 'Voland2024. Reading notes'
    ↓ fetching '- Random thought'
→ Detecting remote deletes
  removed 0 local row(s) absent from the remote
→ Reconciling local mirror
  missing re-fetched: 0, mismatched re-fetched: 0, orphans removed: 0
sync incremental complete · 2.1s
  Remote total                2041
  Local total                 2041
  Fetched / updated              3
  Deleted (remote gone)          0
  Re-fetched (missing file)      0
  Re-fetched (hash drift)        0  (not checked — pass --verify)
  Orphans removed                0
  Last sync  2026-04-12T08:52:30Z
```

## Tech stack

Python 3.12+, managed with `uv`. Deliberately small and stdlib-friendly.

| Layer | Choice |
|---|---|
| Packaging | `uv` + `pyproject.toml`, installable globally with `uv tool install .` |
| CLI | [Typer](https://typer.tiangolo.com/) |
| HTTP | [httpx](https://www.python-httpx.org/) (sync) |
| Store + search | stdlib `sqlite3` + FTS5 (unicode61, diacritic-insensitive) |
| Markdown parsing | [markdown-it-py](https://markdown-it-py.readthedocs.io/) |
| Terminal output | [rich](https://rich.readthedocs.io/) |
| Config | [environs](https://pypi.org/project/environs/) + `.env` |
| Tests | pytest + pytest-httpx |

Full rationale in `~/src/vcoeur/conception/projects/2026-04-12-kasten-manager/notes/tech-stack.md`.

## Architecture

Layered, mirrors PaintingManager's conventions:

```
app/
  models/          <- pure dataclasses (Note, NoteSummary, WikiLink, SearchHit)
  repositories/    <- data access: http_client, store (sqlite/FTS5), vault_files, lock, sync_state
  services/        <- business logic: sync, notes (read/write), markdown_parser, note_mapper
  cli/             <- Typer app + rich/JSON output helpers
  settings.py      <- environs-backed configuration
tests/             <- mirror the app layout
```

Read rules:

- **Reads never hit the network.** `search`, `read`, `list`, `backlinks`, `tags`, `kinds`, `status` all resolve against the local mirror + sqlite. If the mirror is stale, Claude sees stale data until the next explicit `sync` — a deliberate choice for predictable latency.
- **Writes always hit the network.** `create`, `edit`, `delete`, `rename`, `restore` call `notes.vcoeur.com` first, then re-fetch the affected note and update the local mirror in the same command. No local-authoritative state.

## Install

```bash
# Clone to your preferred location.
git clone git@github.com:vcoeur/KastenManager.git
cd KastenManager

# Install dev deps into a local .venv for tests.
make dev-install

# Copy the env template and add your API token.
cp .env.example .env
$EDITOR .env    # set KASTEN_API_URL + KASTEN_API_TOKEN

# Verify config.
uv run kasten config --json

# Install globally as the `kasten` command (one-time per laptop).
make tool-install
kasten --help
```

`make tool-install` installs in **editable** mode (`uv tool install --editable .`), so subsequent `git pull`s or local code changes take effect the next time you run `kasten` — **no reinstall needed on updates**. Only run `make tool-install` again if you changed `pyproject.toml` entry points, dependencies, or you see `ImportError` after a refactor.

`make tool-uninstall` removes the global command (does not delete the repo or `.env`).

Getting an API token: open `notes.vcoeur.com`, go to settings → tokens, create a new one with the `api` scope, paste it into `.env` as `KASTEN_API_TOKEN`. The token is shown only once.

## First sync

```bash
kasten sync --full
```

This pages through `GET /api/notes` with the cursor cleared, fetches each note's body via `GET /api/notes/{id}`, writes one markdown file per note under `./kasten/`, and builds the local SQLite index under `./.kasten-state/index.sqlite`.

If `./kasten/` already contains unrelated content, it is preserved — sync writes files by their export-style path (`entity/`, `note/`, `literature/`, `files/`, `journal/YYYY-MM/`) and will not overwrite arbitrary files in parallel directories.

## Usage examples

```bash
# Fresh sync then a few offline queries.
kasten sync
kasten search "trigram blind index" --json | jq '.hits[0]'
kasten read "! Core insight" --json
kasten backlinks "@ Alice Voland" --json
kasten list --family permanent --limit 5 --json

# Create a new note.
echo "Draft body" | kasten create --filename "! New idea" --body-file - --json

# Edit with inline body + add a tag.
kasten edit "! New idea" --body "Revised body." --add-tag research --json

# Rename (family prefix must stay the same).
kasten rename "! New idea" "! Core insight" --json
```

## Local paths

By default everything lives inside the repo:

| Path | Purpose | Gitignored |
|---|---|---|
| `./kasten/` | plaintext markdown mirror | yes |
| `./.kasten-state/index.sqlite` | metadata + FTS5 index | yes |
| `./.kasten-state/state.json` | sync cursor, schema version | yes |
| `./.kasten-state/tmp/` | scratch (atomic writes, zip unpack) | yes |
| `./.kasten-state/sync.lock` | fcntl advisory lock held during sync / writes | yes |
| `./.env` | API URL + token | yes |

Override the root with `KASTEN_HOME=/path/to/somewhere` in `.env`. On a laptop where the repo lives somewhere inconvenient, set `KASTEN_HOME=~/.cache/kasten-manager` to get a more traditional XDG layout.

## Development

```bash
make test       # run pytest
make lint       # ruff check + format --check
make format     # ruff check --fix + format
make sync       # incremental sync
make sync-full  # full rebuild
```

Tests use pytest-httpx to mock `notes.vcoeur.com` — there is no dependency on a running server for the unit test suite.

## Design notes

All design and investigation notes for this project live in the conception repo:

- `~/src/vcoeur/conception/projects/2026-04-12-kasten-manager/README.md` — project overview
- `notes/notes-api-surface.md` — every notes.vcoeur.com endpoint the CLI uses
- `notes/tech-stack.md` — stack decision + rationale
- `notes/local-storage.md` — on-disk layout
- `notes/index-schema.md` — SQLite + FTS5 schema
- `notes/sync-strategy.md` — incremental vs full sync algorithm
- `notes/cli-surface.md` — full CLI reference

## Status

v0.1 — initial CLI and local index. No GUI. No attachment upload. See the design notes and the project README for roadmap and open questions.
