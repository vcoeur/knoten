---
title: Commands · knoten
description: Full CLI reference for knoten — read, write, sync, graph, and config commands.
---

# Commands

Every command accepts `--json` for machine-readable output. On a TTY without `--json`, output is rendered with rich tables and highlighted snippets. Claude skills should always pass `--json`.

## Read commands

These never hit the network. They resolve against the local Markdown mirror + SQLite FTS5 index.

### `knoten search`

Ranked full-text search on the local index, with snippets, filters, and JSON output.

```bash
knoten search "zettelkasten"
knoten search "query" --fuzzy --tag research --json
knoten search "trigram" --family permanent --limit 5
```

Ranking: **title > filename > body**. Add `--fuzzy` for typo-tolerant + substring match (trigram FTS + rapidfuzz on titles).

### `knoten read`

Full note body, wiki-links, and backlinks, resolved from the local mirror.

```bash
knoten read "- First thought"
knoten read 202604151820-first-thought --json
```

### `knoten list`

Metadata listing — filter by family, kind, or tag.

```bash
knoten list --family permanent --limit 10
knoten list --tag research --json
```

### `knoten backlinks`

Notes that wiki-link to a target.

```bash
knoten backlinks "@ Alice Voland" --json
```

### `knoten graph`

BFS wiki-link neighbourhood for broadened search. Returns nodes with distance + edges. Depth 0–5.

```bash
knoten graph "! Core insight" --depth 2 --direction both
knoten graph "@ Alice Voland" --depth 3 --direction out --json
```

### `knoten tags` / `knoten kinds`

Enumerate the tags and kinds present in the vault.

```bash
knoten tags
knoten kinds --json
```

## Write commands

In remote mode, writes hit the configured backend first (whatever `KNOTEN_API_URL` points at) and refresh the affected note locally. In local mode, writes go straight to the Markdown vault. The local mirror is never authoritative in remote mode.

### `knoten create`

```bash
knoten create --filename "! New idea" --body "First draft."
echo "Draft body" | knoten create --filename "! New idea" --body-file - --json
```

### `knoten edit`

```bash
knoten edit "! New idea" --body "Revised body." --add-tag research
knoten edit "! New idea" --body-file new-body.md --json
```

### `knoten append`

Appends to an existing note without rewriting the head.

```bash
knoten append "! New idea" --body "A later thought."
```

### `knoten rename`

Rewrites `[[old-filename]]` wiki-links in every referencing note. Rolls back on partial failure. Family prefix must stay the same.

```bash
knoten rename "! New idea" "! Core insight" --json
```

### `knoten delete` / `knoten restore`

`delete` moves the file to `<vault>/.trash/` — reversible. `rm foo.md` in a shell is a permanent delete (no trash copy).

```bash
knoten delete "- Scratch" --json
knoten restore "- Scratch"
```

### `knoten upload` / `knoten download`

Attachment operations. Attachments live under `<vault>/.attachments/`.

```bash
knoten upload ./figure.png --for "! Core insight"
knoten download figure.png
```

## Sync commands

### `knoten sync`

Pull new / changed notes from the remote into the local mirror. Always runs delete detection and reconciliation (re-fetch missing files, remove orphans).

```bash
knoten sync                        # incremental
knoten sync --verify               # + full body-hash verification
knoten sync --full                 # clear cursor, rebuild from scratch
```

In TTY mode, `sync` prints phase-by-phase progress to stderr. In `--json` mode, stderr is silent and only the final JSON result is emitted on stdout.

### `knoten verify`

Runs SQLite integrity check, FTS5 / notes cardinality check, file existence, and orphan cleanup.

```bash
knoten verify
knoten verify --hashes             # also compare every file to its recorded body hash
```

### `knoten reindex`

Rebuild derived tables (FTS5, tags, wikilinks, frontmatter fields) from the `notes` table + on-disk files. No network. Use when `verify` reports FTS5 drift or when you are offline.

```bash
knoten reindex
```

## Config and status

### `knoten status`

Inspect the mirror — note count, last sync, lock state, drift warnings.

```bash
knoten status
knoten status --json
```

### `knoten config`

```bash
knoten config show                 # all values, API token redacted
knoten config show --json
knoten config path                 # resolved config / data / cache paths
knoten config path --json
knoten config edit                 # open .env in $EDITOR
```

### `knoten init`

Bootstraps the vault, state, and a commented `.env`. Idempotent — safe to re-run.

```bash
knoten init
```
