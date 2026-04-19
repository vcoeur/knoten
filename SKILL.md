---
name: knoten
description: Search, read, and optionally write notes in a local knoten zettelkasten vault via the `knoten` CLI. Works in local-only mode (a self-contained markdown + SQLite FTS5 vault, no network) or remote mode (mirror of a compatible remote backend). Use when the user wants to search their notes, browse backlinks, read a note by title, or add new notes. This is a generic example skill — adapt it to your own conventions.
argument-hint: "<natural language query or command>"
allowed-tools: Read, Write, Edit, Bash(knoten sync:*), Bash(knoten status:*), Bash(knoten search:*), Bash(knoten read:*), Bash(knoten list:*), Bash(knoten backlinks:*), Bash(knoten graph:*), Bash(knoten tags:*), Bash(knoten kinds:*), Bash(knoten path:*), Bash(knoten config:*), Bash(knoten verify:*), Bash(knoten reindex:*), Bash(knoten create:*), Bash(knoten edit:*), Bash(knoten append:*), Bash(knoten rename:*), Bash(knoten upload:*), Bash(knoten download:*), Bash(knoten init:*), Bash(knoten version:*), Bash(python3:*), Bash(jq:*), Bash(command -v knoten:*)
---

Thin wrapper around the [`knoten`](https://github.com/vcoeur/knoten) CLI. Given a natural-language request, resolve it against the local vault (search, read, list, backlinks, graph) and, when the user explicitly asks, create or edit notes.

**This is a minimal example skill.** It is deliberately generic and does not encode any particular set of vault conventions. Fork it and add your own rules (note families, filename conventions, tagging scheme, permission expectations, downstream integrations, etc.) as needed.

## Use at your own risk

`knoten` is MIT-licensed software provided **as-is, with no warranty**. This skill drives `knoten` on your behalf — including write commands that mutate your vault and, in remote mode, whichever backend `KNOTEN_API_URL` is pointing at. Review the CLI surface in [`knoten`'s README](https://github.com/vcoeur/knoten) before enabling writes, and keep your vault under version control so any unintended edit is recoverable.

Never run `knoten delete` without explicit user confirmation. Soft-deleted notes land in `<vault>/.trash/` and can be restored with `knoten restore <uuid>`, but a plain `rm` of a vault file is permanent.

## Prerequisites

```bash
command -v knoten
```

If missing, install from PyPI:

```bash
pipx install knoten
# or, fully isolated:
uv tool install knoten
```

Optional one-shot bootstrap — pre-seeds the config, data, and cache dirs and a commented `.env`:

```bash
knoten init
```

For local-only mode, that is all: the vault auto-creates on the first write. For remote mode (syncing with a compatible backend), set `KNOTEN_API_URL` and `KNOTEN_API_TOKEN` in your `.env`:

```bash
knoten config edit
```

Verify the effective configuration:

```bash
knoten config show --json
```

## Request

> $ARGUMENTS

## Always pass `--json`

Every `knoten` subcommand supports `--json`. Always pass it — the TTY rendering is for humans, the JSON envelope is stable and machine-parseable. Parse stdout with `python3 -c "import sys, json; ..."` or `jq`.

On failure, commands emit a structured error envelope on **stdout** (not stderr) and exit non-zero:

```json
{"error": "<kind>", "message": "Human-readable description", "code": 1}
```

Parse the `error` field rather than the free-text message. Typical kinds include `config`, `auth`, `network`, `store`, `lock_timeout`, `permission_denied`, `ambiguous_target`, `not_found`, `user`.

## Sync cadence (remote mode only)

In remote mode the local mirror does not auto-refresh. At the start of a session, run `knoten status --json` (cheap, offline) and check `seconds_since_last_sync`: if `null` or significantly stale (e.g. `> 600`), run `knoten sync --json` before searching or reading. Write commands refresh the affected note synchronously and do not need an explicit sync.

In local mode, every invocation runs a mtime-gated stat walk that picks up external edits automatically — no explicit sync needed.

## Command cheat sheet

Path / id arguments accept either a UUID, an exact filename, or an unambiguous filename prefix.

### Read path (offline, sub-10ms)

| Command | Purpose |
|---|---|
| `knoten status --json` | Local-mirror snapshot (counts, last sync, drift) |
| `knoten search "<query>" --json` | FTS5 search with ranking, snippets, filters (`--family`, `--kind`, `--tag`, `--limit`, `--fuzzy`) |
| `knoten read <target> --json` | Full note body + wikilinks + backlinks |
| `knoten list --json` | Metadata listing, no bodies (`--family`, `--kind`, `--tag`, `--source`, `--sort`, `--limit`) |
| `knoten backlinks <target> --json` | Notes linking to this one |
| `knoten graph <target> --depth N --json` | BFS wikilink neighbourhood (depth 0–5) |
| `knoten tags --json` | Tag counts |
| `knoten kinds --json` | Kind counts (optionally `--family`) |
| `knoten path <target>` | Absolute path to the note file on disk |

### Write path

| Command | Purpose |
|---|---|
| `knoten create --filename "<prefix Title>" --body "..." --json` | Create a new note. Body can also be read from `--body-file PATH` (use `-` for stdin). Optional `--kind`, `--tag`, `--frontmatter-file`. |
| `knoten append <target> --content "..." --json` | Append content to an existing note. |
| `knoten edit <target> --body "..." --json` | Replace body. Other flags: `--filename`, `--title`, `--add-tag`, `--remove-tag`, `--set-frontmatter key=value`, `--unset-frontmatter key`. |
| `knoten rename <target> "<new-filename>" --json` | Thin wrapper over `edit --filename`. The family prefix stays the same. |
| `knoten delete <target> --yes --json` | **Soft** delete (into `.trash/`). Always confirm with the user first. |
| `knoten restore <uuid> --json` | Restore from trash. |
| `knoten upload <path> --filename "<prefix Label>" --json` | Multipart upload of a binary attachment + file-kind note creation. |
| `knoten download <target> [-o PATH]` | Stream an attachment back out. |

### Sync / maintenance

| Command | Purpose |
|---|---|
| `knoten sync --json` | Incremental sync (remote mode only). |
| `knoten sync --full --json` | Clear the cursor and re-fetch every note. |
| `knoten verify --json` | Integrity check (SQLite, FTS5 cardinality, file existence). |
| `knoten reindex --json` | Rebuild derived tables (FTS5, tags, wikilinks) from the `notes` table + on-disk files. No network. |

## Canonical workflows

### Find and read notes on a topic

```bash
knoten search "your query here" --limit 5 --json
```

Parse `.hits[].id`, `.hits[].title`, `.hits[].snippet`, then follow up with `knoten read <id> --json` on the most promising hits.

### Fuzzy search

```bash
knoten search "encrpytion" --fuzzy --json
```

Typo-tolerant substring match over titles + filenames — useful when the user's query does not match any single word exactly. Local-only; incompatible with `--remote`.

### Follow a thread from a starting note

```bash
knoten graph "<target>" --depth 2 --json
```

Returns `nodes` (each with its depth from the start) and `edges`. Sort by `(depth, title)` and read the most promising nodes.

### Create a note

`knoten` expects a filename that encodes the note's family via a short prefix (e.g. an exclamation mark for permanent notes, `@` for person entities, `CiteKey=` for references, `CiteKey.` for literature notes, `YYYY-MM-DD` for day notes, etc.). **Consult `knoten`'s README or your own vault conventions for the exact prefixes** — this generic skill does not prescribe them.

```bash
knoten create \
  --filename "<prefix Title>" \
  --body "Body content." \
  --json
```

Backslash-escape `$` inside shell strings if you use organization-prefixed wikilinks (`[[\$ Acme]]`), so the shell does not expand them.

### Permissions (remote mode)

In remote mode, every note carries an `permissions` level enforced server-side (typical levels: `NONE`, `LIST`, `READ`, `APPEND`, `WRITE`, `ALL`). Before attempting a write, check the `permissions` field returned by `read` / `search` / `list` and skip or tell the user if the required level is not satisfied. `knoten` pre-checks this client-side and exits non-zero with `error: "permission_denied"` when the level is insufficient — parse the envelope rather than retry with `--force`.

## Adapting this skill to your workflow

This skill stops at "expose the CLI". Realistic zettelkasten workflows layer on top:

- **Conventions** — which family prefix for which kind of content, how to pick a citation key, where wikilinks should point, what tags mean.
- **Post-write side-effects** — creating stub notes for every unresolved wikilink, updating an index page, regenerating a static export.
- **Batch operations** — bulk tag rewrites, retroactive frontmatter edits, mass rename.

Those belong in a forked, user-specific SKILL.md — not here. For batch operations specifically, prefer writing a short Python script that calls `knoten` via `subprocess.run(["knoten", ...], check=True)` with argument lists (not shell strings), always with `--json`, and always dry-run destructive batches first.

## Installation

Drop this `SKILL.md` into either:

- `~/.claude/skills/knoten/SKILL.md` — available in every Claude Code session
- `<project>/.claude/skills/knoten/SKILL.md` — project-local (auto-loaded when Claude Code opens that project)

See [Claude Code's skill documentation](https://docs.claude.com/en/docs/claude-code/skills) for details.

$ARGUMENTS
