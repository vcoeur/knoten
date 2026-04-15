# knoten

Standalone CLI zettelkasten with a local markdown vault + SQLite FTS5 index. Runs in two modes:

- **Local mode** (default): a self-contained zettelkasten. No server, no network, no token. The vault is the source of truth; SQLite is a derived index that catches up to external edits automatically on each CLI invocation. This is all you need if you just want a fast, text-editor-friendly notes system.
- **Remote mode**: mirrors a [notes.vcoeur.com](https://notes.vcoeur.com) instance. Reads stay offline against the local index; writes go to the remote first and refresh the mirror.

Both modes share the same CLI surface — the only difference is where data lives. Set `KNOTEN_API_URL` in your environment to switch to remote mode; leave it empty (the default) for local mode.

## What it does

- **`knoten sync`** — pull new / changed notes from `notes.vcoeur.com` into a local markdown mirror and SQLite FTS5 index. Always runs delete detection and reconciliation (re-fetch missing files, remove orphans). Add `--verify` for full body-hash verification.
- **`knoten verify`** — run SQLite integrity check, FTS5 / notes cardinality check, file existence + orphan cleanup. Add `--hashes` to compare every file against its recorded body hash.
- **`knoten reindex`** — rebuild derived tables (FTS5, tags, wikilinks, frontmatter fields) from the `notes` table + on-disk files. No network. Use when `verify` reports FTS5 drift or when you are offline.
- **`knoten search "query"`** — full-text search on the local index, with snippets, ranking (title > filename > body), filters (`--family`, `--kind`, `--tag`), JSON output. Pass `--fuzzy` for typo-tolerant + substring match (trigram FTS + rapidfuzz on titles).
- **`knoten read <id|filename>`** — full note body + wiki-links + backlinks, resolved from the local mirror (no network hit).
- **`knoten backlinks <target>`**, **`knoten list`**, **`knoten tags`**, **`knoten kinds`** — metadata queries, all offline.
- **`knoten graph <target> --depth N --direction out|in|both`** — BFS wiki-link neighbourhood for broadened search. Returns nodes with their distance from the start, plus edges. Depth 0-5.
- **`knoten create`**, **`knoten edit`**, **`knoten append`**, **`knoten delete`**, **`knoten restore`**, **`knoten rename`**, **`knoten upload`**, **`knoten download`** — write / attachment operations that hit `notes.vcoeur.com` first, then refresh the affected note locally. The local mirror is never authoritative.
- **`knoten status`** / **`knoten config show`** / **`knoten config path`** / **`knoten config edit`** / **`knoten init`** — inspect the mirror, see the effective configuration, open the `.env` in your editor, or bootstrap the vault + state dirs. All offline.

All commands accept `--json` for machine-parseable output. On a TTY without `--json`, output is rendered with rich (tables, snippet highlighting). Claude skills should always pass `--json`.

### Verbose output by default

In TTY mode, long-running commands (`sync`, `verify`, `reindex`) print phase-by-phase status to stderr so you can see exactly what is happening — every page fetched, every note downloaded, every deletion, every orphan removed. A rich summary table follows on stdout. In `--json` mode, stderr is silent and only the final JSON result is emitted to stdout.

Example:

```
$ knoten sync
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
| Packaging | `uv` + `pyproject.toml`, published to PyPI as `knoten` |
| CLI | [Typer](https://typer.tiangolo.com/) |
| HTTP | [httpx](https://www.python-httpx.org/) (sync) |
| Store + search | stdlib `sqlite3` + FTS5 (`unicode61` for ranked search, `trigram` mirror for `search --fuzzy`) + [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) |
| Markdown parsing | [markdown-it-py](https://markdown-it-py.readthedocs.io/) |
| Terminal output | [rich](https://rich.readthedocs.io/) |
| Config | [environs](https://pypi.org/project/environs/) + `.env` |
| Tests | pytest + pytest-httpx |

## Architecture

Layered — models / repositories / services / CLI, the usual Python CLI layout.

```
knoten/
  models/          <- pure dataclasses (Note, NoteSummary, WikiLink, SearchHit)
  repositories/    <- data access: http_client, store (sqlite/FTS5), vault_files, lock, sync_state
  services/        <- business logic: sync, notes (read/write), markdown_parser, note_mapper
  cli/             <- Typer app + rich/JSON output helpers
  settings.py      <- environs-backed configuration
tests/             <- mirror the knoten layout
```

Read rules:

- **Reads never hit the network.** Every command except `sync`, `verify`, and the write / attachment operations below resolves against the local mirror + sqlite. If the mirror is stale, Claude sees stale data until the next explicit `sync` — a deliberate choice for predictable latency.
- **Writes always hit the network in remote mode.** `create`, `edit`, `append`, `delete`, `rename`, `restore`, `upload`, `download` call `notes.vcoeur.com` first, then re-fetch the affected note and update the local mirror in the same command. No local-authoritative state.
- **Local mode is filesystem-authoritative.** The vault is the source of truth; SQLite is derived. Every CLI invocation first runs a mtime-gated stat walk that picks up external edits (e.g. files you edited in your text editor), new files dropped into the vault, and external deletes. `knoten delete` moves files to `<vault>/.trash/` (reversible via `knoten restore`); `rm foo.md` in a shell is a permanent delete (the walk drops the store row and there is no trash copy to restore from).
- **Rename cascades across referencing notes** in both modes. Rename rewrites `[[old-filename]]` to `[[new-filename]]` in every other note whose body referenced the renamed one. In remote mode the server does the rewrite and returns an `affectedNotes` envelope (notes.vcoeur.com v2.9.0+); in local mode knoten does the same rewrite by walking the `wikilinks` index. Rollback on partial failure restores the original bytes of every file it touched before re-raising.

## Local-only mode — quickstart

```bash
# 1. Install.
git clone https://github.com/vcoeur/knoten.git
cd knoten
make dev-install

# 2. Point it at a fresh vault directory. No token, no URL.
export KNOTEN_HOME=~/my-knoten
mkdir -p ~/my-knoten/kasten

# 3. Create your first note.
uv run knoten create --filename "- First thought" --body "Hello from my new vault."

# 4. Read, list, search — all offline.
uv run knoten list
uv run knoten search "hello"
```

Vault layout after a few writes:

```
~/my-knoten/
├── kasten/                   ← the markdown vault, version-control this
│   ├── note/
│   │   ├── - First thought.md
│   │   └── ! Permanent insight.md
│   ├── entity/
│   │   └── @ Alice Voland.md
│   ├── literature/
│   │   └── Smith2024. Reading notes.md
│   ├── .trash/               ← soft-deleted notes (reversible)
│   └── .attachments/         ← blobs for `knoten upload`
└── .knoten-state/
    └── index.sqlite          ← derived FTS5 + wikilink index
```

You can edit `.md` files directly in any editor — the next `knoten` invocation picks up the changes via a stat walk. Git-managing the `kasten/` directory is the expected sync story across machines; `.knoten-state/` should be gitignored because it is a derived cache.

The `kasten/` subdirectory name is historical (the vault is a Zettelkasten) and can be changed via `KNOTEN_VAULT_DIR`.

## Install

Install from PyPI:

```bash
pipx install knoten
# or: uv tool install knoten
```

Both install `knoten` into its own isolated venv and put it on your `$PATH`.

Verify:

```bash
knoten --help
knoten config show --json   # see the effective configuration
```

For local mode (the default), that's all you need — the vault at `~/.knoten/kasten/` and the SQLite index are created lazily on your first command (`knoten list`, `knoten create`, …).

Optional bootstrap — pre-seed a commented `.env` and create the vault dirs up front instead of lazily:

```bash
knoten init
```

For remote mode (mirroring a `notes.vcoeur.com` instance), edit your `.env` and add `KNOTEN_API_URL` + `KNOTEN_API_TOKEN`:

```bash
knoten config edit          # opens your .env in $EDITOR
```

Getting an API token: open your `notes.vcoeur.com` instance, go to settings → tokens, create a new one with the `api` scope, paste it into the `.env` as `KNOTEN_API_TOKEN`. The token is shown only once.

### Development from a source checkout

```bash
git clone https://github.com/vcoeur/knoten.git
cd knoten
make dev-install            # uv sync --all-groups
cp .env.example .env        # optional — only for remote mode
uv run knoten --help        # run the CLI straight from the repo
uv run knoten config show --json
make test                   # pytest
```

When run from the repo, `knoten` picks up the `.env` at the repo root automatically and stores its vault + SQLite index under the repo. No global install needed.

## First sync

```bash
knoten sync --full
```

This pages through `GET /api/notes` with the cursor cleared, fetches each note's body via `GET /api/notes/{id}`, writes one markdown file per note under `./kasten/`, and builds the local SQLite index under `./.knoten-state/index.sqlite`.

If `./kasten/` already contains unrelated content, it is preserved — sync writes files by their export-style path (`entity/`, `note/`, `literature/`, `files/`, `journal/YYYY-MM/`) and will not overwrite arbitrary files in parallel directories.

## Usage examples

```bash
# Fresh sync then a few offline queries.
knoten sync
knoten search "trigram blind index" --json | jq '.hits[0]'
knoten read "! Core insight" --json
knoten backlinks "@ Alice Voland" --json
knoten list --family permanent --limit 5 --json

# Create a new note.
echo "Draft body" | knoten create --filename "! New idea" --body-file - --json

# Edit with inline body + add a tag.
knoten edit "! New idea" --body "Revised body." --add-tag research --json

# Rename (family prefix must stay the same).
knoten rename "! New idea" "! Core insight" --json
```

## Local paths

`KNOTEN_HOME` anchors the vault and state directories:

| Path | Purpose | Gitignored |
|---|---|---|
| `$KNOTEN_HOME/kasten/` | plaintext markdown mirror | yes |
| `$KNOTEN_HOME/.knoten-state/index.sqlite` | metadata + FTS5 index | yes |
| `$KNOTEN_HOME/.knoten-state/state.json` | sync cursor, schema version | yes |
| `$KNOTEN_HOME/.knoten-state/tmp/` | scratch (atomic writes, zip unpack) | yes |
| `$KNOTEN_HOME/.knoten-state/sync.lock` | fcntl advisory lock during sync / writes | yes |
| `$KNOTEN_HOME/.env` | API URL + token | yes |

### Two runtime contexts

**Dev from the repo** (`uv run knoten …`, `make sync`): `KNOTEN_HOME` defaults to the directory containing `pyproject.toml`, and the repo's own `.env` is read automatically. Clone, `cp .env.example .env`, fill in the token, done.

**Installed from PyPI** (`pipx install knoten` → `knoten` on `$PATH`): the installed copy can't find a source tree, so it reads **`~/.config/knoten/.env`** first. That file is typically a two-line pointer:

```ini
KNOTEN_HOME=~/src/vcoeur/knoten
```

Once `KNOTEN_HOME` is known, the CLI layers on `$KNOTEN_HOME/.env` automatically to pick up the API URL + token — no secret duplication. Fallback if nothing is configured: `~/.knoten/` (state) + `~/.knoten/.env` (config).

`.env` files are layered with "first value wins" semantics (`environs.read_env(override=False)`), so a process env var always beats any file, and an earlier layer always beats a later one.

## Development

```bash
make test       # run pytest
make lint       # ruff check + format --check
make format     # ruff check --fix + format
make sync       # incremental sync
make sync-full  # full rebuild
```

Tests use pytest-httpx to mock `notes.vcoeur.com` — there is no dependency on a running server for the unit test suite.

## Status

v0.1 — initial CLI, local SQLite/FTS5 index, attachment upload/download, and fuzzy search. No GUI.

## Licence

MIT — see [`LICENSE`](LICENSE).

## Questions or feedback

This is a personal tool — I'm happy to hear from you, but there is no formal support. The best way to reach me is the contact form on [vcoeur.com](https://vcoeur.com).
