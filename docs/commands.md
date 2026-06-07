---
title: Commands · knoten
description: Full CLI reference for knoten — read, write, sync, graph, and config commands.
---

# Commands

Every command accepts `--json` for machine-readable output. On a TTY without `--json`, output is rendered with rich tables and highlighted snippets. Agent skills should always pass `--json`.

### `knoten schema`

Dump the whole machine-readable contract — every command and its flags, the family/prefix/kind table, the permission ladder, and the error kinds with their exit codes. Introspected from the live app, so it never drifts. One call lets an agent self-orient without reading these docs.

```bash
knoten schema --json
```

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

### `knoten unresolved`

Dangling wiki-link targets — links pointing at notes that don't exist yet — grouped by target, with the notes that reference each. Run it after a write to find the stubs you still need to create.

```bash
knoten unresolved --json
```

### `knoten path`

Absolute path of a note's mirror file. Plain one-line output by default (grep-friendly); `{id, filename, path}` with `--json`.

```bash
knoten path "! Core insight"
knoten path "! Core insight" --json
```

## Write commands

In remote mode, writes hit the configured backend first (whatever `KNOTEN_API_URL` points at) and refresh the affected note locally. In local mode, writes go straight to the Markdown vault. The local mirror is never authoritative in remote mode.

### `knoten create`

```bash
knoten create --filename "! New idea" --body "First draft."
echo "Draft body" | knoten create --filename "! New idea" --body-file - --json
knoten create --filename "% Foo" --dry-run --json    # resolve family/kind + unresolved links, no write
```

Seed typed frontmatter with `--frontmatter-file PATH.json` (ints/lists/null round-trip).

**Batch.** Create many notes from a JSON array of drafts under one lock pass — one permission prompt, no per-call shell-escaping. Each item is `{filename, body?, kind?, tags?, frontmatter?, ai?}`; a single bad draft doesn't abort the rest.

```bash
knoten create --batch drafts.json --json      # or '-' to read the array from stdin
```

The result is `{operation, count, created, failed, results: [{index, ok, id|error}]}`. Add `--dry-run` to preview every draft without writing.

### `knoten edit`

```bash
knoten edit "! New idea" --body "Revised body." --add-tag research
knoten edit "! New idea" --body-file new-body.md --json
knoten edit "! New idea" --dry-run --json     # validate (permissions, prefix, changes), no write
```

**Typed frontmatter.** `--set-frontmatter key=value` sends the value as a *string*. To change a numeric/list/bool/null field on an existing note, use `--set-frontmatter-json key=<json-literal>` so the type round-trips:

```bash
knoten edit "@ Jane Doe" --set-frontmatter-json birth-year=1990 --json
knoten edit "Scott2019= …" --set-frontmatter-json 'authors=["[[@ Kim Scott]]"]' --json
```

### `knoten append`

Appends to an existing note without rewriting the head.

```bash
knoten append "! New idea" --body "A later thought."
```

### `knoten rename`

Rewrites `[[old-filename]]` wiki-links in every referencing note. Rolls back on partial failure. Family prefix must stay the same. `--dry-run` validates the rename without writing.

```bash
knoten rename "! New idea" "! Core insight" --json
knoten rename "! New idea" "! Core insight" --dry-run --json
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

### `knoten reset`

Delete the local mirror (cache + vault). The next sync is forced full. Prompts unless `--yes`; in `--json` mode `--yes` is required.

```bash
knoten reset --yes
knoten reset --yes --json
```

## Agent integration

### `knoten skill`

knoten ships a convention-free [agent skill](https://docs.claude.com/en/docs/claude-code/skills) (`SKILL.md`) that teaches an LLM to drive the CLI safely. Install it into a skills directory:

```bash
knoten skill install --user        # ~/.config/agents/skills/knoten/SKILL.md (default)
knoten skill install --project     # ./.agents/skills/knoten/SKILL.md
knoten skill install --claude      # ~/.claude/skills/knoten/SKILL.md
knoten skill status                # where it's installed and whether it matches the bundled copy
```

The bundled skill is deliberately generic — layer your own vault conventions in a separate skill that references it.

### `knoten mcp serve`

Optional [Model Context Protocol](https://modelcontextprotocol.io/) server over the vault, for agents that prefer MCP tools to a shell. It is a thin facade over the same service layer the CLI uses — the CLI remains the primary integration. Needs the optional `mcp` dependency:

```bash
uv tool install 'knoten[mcp]'      # or: pipx inject knoten 'mcp>=1.0'
knoten mcp serve                   # stdio transport
```
