---
title: Commands · knoten
description: Full CLI reference for knoten — read, write, sync, graph, and config commands.
---

# Commands

Every command accepts `--json` for machine-readable output, with three exceptions that have no payload worth shaping: `init`, `config edit`, and `mcp serve`. On a TTY without `--json`, output is rendered with rich tables and highlighted snippets. Agent skills should always pass `--json`.

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
knoten search "alice" --in title --json          # scope the match to a column
knoten search "auth" --in title,filename --json  # repeatable / comma-separated
```

Ranking: **title > filename > body**. Add `--fuzzy` for typo-tolerant + substring match (trigram FTS + rapidfuzz on titles).

`--in <column>` restricts the match to one or more FTS5 columns — `title`, `body`, `filename` — repeatable or comma-separated. Invalid column names are a `user` error, and `--in` cannot be combined with `--fuzzy` (ranked search only). When set, the active scope is echoed back in the JSON payload as `scope`.

When a title or filename matches but the body has no match, the body snippet falls back to the note's first non-empty body line (truncated, no `<<>>` markers) so highly-ranked hits still carry a preview.

When a ranked search returns **0 hits**, knoten runs a cheap fuzzy probe under the same filters; if fuzzy would have found matches it adds `fuzzy_total` to the payload and a `hint` like `0 ranked hits; --fuzzy would find 3`. The mode is never switched automatically.

`--fields minimal` trims each JSON hit to `{id, filename, title, family, kind, score, snippet}` (default `full` keeps the whole hit). The projection is JSON-only — the TTY table already shows a curated subset and ignores the flag.

### `knoten similar`

Related notes without embeddings. Resolves the target like `read` (UUID, exact filename, or unambiguous prefix), derives a query from the note's title + most frequent body terms, runs it through ranked search (matching any term), drops the note itself, and returns the top hits. Network- and lock-free.

```bash
knoten similar "! Core insight" --json
knoten similar 202604151820-core-insight --limit 5 --json
knoten similar "! Core insight" --family permanent --json
```

`--limit` defaults to 10 (max 50). `--family` / `--kind` / `--tag` narrow the candidate pool, same as `search`. JSON is `{target_id, target_filename, derived_query, total, hits}` — `hits` share the `search` hit shape.

### `knoten read`

Full note body, wiki-links, and backlinks, resolved from the local mirror.

```bash
knoten read "- First thought"
knoten read 202604151820-first-thought --json
knoten read "- First thought" "- Second thought" --json   # multiple targets
knoten read "! Core insight" --fields meta --json          # metadata only, no body
knoten read "! Core insight" --max-body-chars 2000 --json  # cap the body
```

**Multiple targets.** Pass two or more targets to read them in one offline, lock-free pass. A single target keeps the exact flat note payload (back-compatible). With two or more, the payload becomes `{targets, notes, failed}` — `notes` is the list of resolved note payloads (each the usual read shape), `failed` lists `{target, error, message}` for any that didn't resolve. Each target resolves independently (one bad target never aborts the rest); the command exits 0 when at least one note resolved, and exits 1 with the usual error envelope (first failure's kind) only when every target failed. `--no-backlinks` and `--fields` / `--max-body-chars` apply to all targets.

**Token-budget controls.** `--fields meta` omits the `body` field entirely (frontmatter, wiki-links, and backlinks stay; composes with `--no-backlinks`). `--max-body-chars N` truncates `body` to N characters and adds `body_truncated: true` + `body_total_chars: <int>` when (and only when) a cut happened. `--max-body-chars` cannot be combined with `--fields meta` (a `user` error).

### `knoten list`

Metadata listing — filter by family, kind, or tag.

```bash
knoten list --family permanent --limit 10
knoten list --tag research --json
knoten list --updated-after 2026-06-01 --json
knoten list --created-after 2026-06-01T09:00:00Z --json
```

`--updated-after` / `--created-after` keep only notes updated / created on or after the given moment. Each accepts a bare `YYYY-MM-DD` date or a full ISO-8601 timestamp (a bare date is inclusive of that whole day); an unparseable value is a `user` error. Active date filters are echoed back in the JSON payload (`updated_after` / `created_after`).

`--fields minimal` trims each JSON entry to `{id, filename, family, kind, updated_at}` (default `full` keeps the whole entry). JSON-only, like `search --fields`.

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
knoten unresolved --target "! Core insight" --json   # only links FROM this note
```

`--target <note>` (UUID, filename, or prefix — resolved like `read`) scopes the output to dangling links referenced **from** that one note. The JSON shape is unchanged, just filtered; the resolved note is echoed back as `target: {id, filename}`.

### `knoten path`

Absolute path of a note's mirror file. Plain one-line output by default (grep-friendly); `{id, filename, path}` with `--json`.

```bash
knoten path "! Core insight"
knoten path "! Core insight" --json
```

### `knoten citekeys`

The vault's in-use CiteKeys — the distinct, non-empty `source` frontmatter values across all notes, sorted ascending. A reference note stores its CiteKey in its `source` field, so this is the set of CiteKeys already taken. Plain output is one CiteKey per line (pipes cleanly into a collision-aware minting tool); JSON is `{citekeys, count, prefix}`. `--prefix STR` keeps only CiteKeys starting with `STR` (case-sensitive).

```bash
knoten citekeys
knoten citekeys --prefix Alice2026 --json
knoten citekeys | quelle resolve "<x>" --taken-file -   # avoid minting a collision
```

## Write commands

In remote mode, writes hit the configured backend first (whatever `KNOTEN_API_URL` points at) and refresh the affected note locally. In local mode, writes go straight to the Markdown vault. The local mirror is never authoritative in remote mode.

Every write response (`create`, `edit`, `append`, `restore`, `reference`, `upload`) accepts `--fields minimal|full` like the read commands. With `--fields full` the response carries the written note's `wikilinks: [{title, id, broken}]` — `broken: true` flags a target that does not resolve to an existing note. After a single write you can read dangling links straight off the response (`jq '.wikilinks[] | select(.broken)'`) instead of running a vault-wide [`unresolved`](#knoten-unresolved).

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

### `knoten reference`

Create a CiteKey-anchored reference note from a quelle Source JSON object (`--from-source FILE`, or `-` for stdin). Maps a quelle `Publication` dict (snake_case) to a knoten reference note: the filename is `<CiteKey>= <Title>`, the quelle `kind` maps to a knoten reference kind (`article`/`preprint`→`article`, `book`/`book-chapter`→`book`, `web`→`web`, `media`→`media`, anything else→`document`), and the frontmatter is rebuilt in knoten's hyphen-key convention (`family`, `kind`, `source`, `title`, `authors` as `[[@ Name]]` wikilinks, `year`, `publisher`, `edition`, `isbn-13`, `isbn-10`, `page-count`, `subjects`, `url` — each set only when the Source carries it). The CiteKey is `x_vcoeur.citekey` when present, else the top-level `citation_key`. Wikilink-special characters (`|`, `#`, `[`, `]`) and path separators (`/`, `\`) are stripped from the title used in the filename so the note stays linkable as `[[CiteKey= Title]]` and stays a single flat file (a raw web title like `Working on projects | uv` would otherwise break at the `|`, and `astral-sh/uv` would split the note into a stray sub-directory); the original title is kept verbatim in the `title` frontmatter field.

```bash
quelle fetch --book 9781250103505 --json | knoten reference --from-source - --json
knoten reference --from-source source.json --body-file summary.md --ai --json
knoten reference --from-source source.json --dry-run --json   # preview, no write
```

`--body`/`--body-file` add a summary blurb; `--ai` wraps it in `#ai begin`/`#ai end` markers (and the note picks up the `ai` tag). `--dry-run` previews the filename, mapped kind, frontmatter, and unresolved links without writing (and needs no token). `--fields minimal|full` selects the response shape.

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

**Batch.** Edit many notes from a JSON array of patches under one lock pass — one permission prompt, no per-call shell-escaping. Works in both local and remote mode (remote does N API round-trips inside one process). Each item is `{target, filename?, title?, body?, add_tags?, remove_tags?, set_frontmatter?, unset_frontmatter?, ai?}`; `set_frontmatter` values are typed JSON (ints/lists/bool/null round-trip). A single bad patch doesn't abort the rest. `--batch` is mutually exclusive with a positional target and the per-note edit flags.

```bash
knoten edit --batch patches.json --json   # or '-' to read the array from stdin
```

The result is `{operation: "edit-batch", count, edited, failed, results: [{index, ok, id|error}]}`. Add `--dry-run` to validate/preview every patch (permissions, prefix, changes, unresolved links) without writing.

### `knoten append`

Appends to an existing note without rewriting the head. Content comes from `--content <text>` or `--content-file <path>` (`-` for stdin) — exactly one of the two.

```bash
knoten append "! New idea" --content "A later thought."
echo "A later thought." | knoten append "! New idea" --content-file - --json
```

`--ai` wraps the appended content in `#ai begin` / `#ai end` markers.

### `knoten rename`

Rewrites `[[old-filename]]` wiki-links in every referencing note. Rolls back on partial failure. Family prefix must stay the same. `--dry-run` validates the rename without writing.

```bash
knoten rename "! New idea" "! Core insight" --json
knoten rename "! New idea" "! Core insight" --dry-run --json
```

In remote mode the server's cascade rewrites every referencing note regardless of the API token's per-note permissions, so a referencing note the token cannot READ is mirrored as a metadata-only placeholder (same as a restricted note during sync) instead of failing the rename. The JSON output carries `restricted_affected`, the count of such notes (0 on the common path).

### `knoten delete` / `knoten restore`

`delete` moves the file to `<vault>/.trash/` — reversible. `rm foo.md` in a shell is a permanent delete (no trash copy). Declining the interactive confirmation prompt exits 0 (a successful no-op, not an error). The `--json` response is `{"deleted_id": "<uuid>"}` — **that id is the restore handle**: pass it to `knoten restore` (or find it again via `knoten trash`). `restore` accepts only a note UUID — trash lookups are by id, not filename. Restore requires WRITE on the note; a local pre-check fast-fails when the mirror knows the level, and `--force` bypasses it (the server stays the final authority).

```bash
knoten delete "- Scratch" --json          # → {"deleted_id": "0d4f…"}
knoten restore 0d4f6c0e-93f7-4f4e-9a44-1c8b1a2f3d4e
```

### `knoten trash`

List soft-deleted notes (read-only), most-recently-deleted first. Remote mode lists the server's trash (`GET /api/trash/notes`); local mode lists the local `trashed_notes` table — both work offline-consistent with their own source of truth. The `--json` payload is `{"data": [{"id", "filename", "title", "family", "kind", "source", "permissions", "created_at", "updated_at", "deleted_at"}], "total"}`; `--fields minimal` trims each JSON row to `id`, `filename`, `deleted_at`. `--limit` caps the rows. Each `id` is the restore handle for `knoten restore`.

```bash
knoten trash --json
knoten trash --limit 10 --fields minimal --json
```

### `knoten upload` / `knoten download`

Attachment operations. Attachments live under `<vault>/.attachments/`. `upload` stores the blob and creates a linked **file-family note** whose filename you pass with `--filename` (required; must use a `CiteKey+` or `YYYY-MM-DD+` prefix). `download` targets that file-family **note** — by UUID, exact filename, or unambiguous prefix — and streams its attachment back out.

```bash
knoten upload ./figure.png --filename "2026-06-09+ figure.png"
knoten download "2026-06-09+ figure.png"
knoten download 0d4f6c0e-93f7-4f4e-9a44-1c8b1a2f3d4e -o out/figure.png
```

Without `-o/--output`, `download` writes to the current directory using only the **basename** of the note's filename — the filename is server-controlled, so the default destination is confined to the cwd (unusable basenames are rejected with a `user` error). Pass `-o <path>` to choose any destination explicitly.

## Inbox

Quick-capture flow — `knoten inbox` lowers the friction of getting a thought, photo, or URL into the vault as a fleeting `#inbox` note.

The capture argument is classified by a heuristic: an existing readable file → uploaded as an attachment; something matching `^https?://` → URL (with a best-effort `<title>` fetch to derive a slug); anything else → plain text. Force a mode with `--as-file` / `--as-url` / `--as-text` (mutually exclusive).

**Filename grammar.** Captures follow a fixed naming convention (local time):

- Fleeting note: `- YYYY-MM-DD HHMM inbox <slug>`
- Uploaded file note: `YYYY-MM-DD+ inbox <slug> HHMM<ext>` — `HHMM` sits right before the extension so a chronological sort by date prefix groups one day's inbox files together.
- `<slug>` is a lower-cased slug of the text / file stem / page title (non-alphanumeric runs become `-`, max 40 chars; falls back to `note` / `photo` / `link` when empty).

### `knoten inbox add`

Capture an argument as a new fleeting `#inbox` note. File arguments are uploaded as a file-family note and the fleeting wikilinks to it; URL arguments put `[title](url)` (or the bare URL) in the body; text becomes the body verbatim. The fleeting always carries the `inbox` tag, plus any extra `--tag` values (repeatable). `--note` adds an optional context line to the body.

```bash
knoten inbox add "an idea I had on the train" --json
knoten inbox add ./photo.jpg --note "whiteboard from the design session"
knoten inbox add https://example.com/article --tag research --json
```

### `knoten inbox append`

Append a captured argument to an existing fleeting (UUID, exact filename, or unambiguous prefix). Same heuristic as `add`; the fleeting's tags are not modified. `--note` adds a context line after the payload.

```bash
knoten inbox append "- 2026-06-09 0915 inbox train-idea" "follow-up thought"
knoten inbox append "- 2026-06-09 0915 inbox train-idea" ./sketch.png --json
```

### `knoten inbox list`

List pending `#inbox` fleetings, excluding ones already tagged `#inbox-promoted` (filtered in SQL, so promoted notes never consume page slots and `total` is the global pending count). Sorted by creation time, oldest first — the inbox is FIFO. `--limit` (default 50, max 500) and `--offset` paginate.

```bash
knoten inbox list --json
knoten inbox list --limit 10 --offset 10
```

## Sync commands

### `knoten sync`

Pull new / changed notes from the remote into the local mirror. Always pushes local writes and pending deletes first, then runs delete detection and reconciliation (re-fetch missing files, remove orphans).

```bash
knoten sync                        # incremental
knoten sync --verify               # + full body-hash verification
knoten sync --full                 # clear cursor, rebuild from scratch
knoten sync --force-delete         # override the mass-delete circuit breaker
```

A note with an unpushed local edit (`synced=0`) is never overwritten by the pull pass — it is kept, reported in the result's `conflicts` list, and uploaded by the push pass. Delete detection is skipped when the remote scan is inconsistent (server `total` disagrees with the scanned count), and refuses to remove more than 20% of the local synced notes (and more than 5) unless `--force-delete` is passed; both guards surface in the result's `warnings` list.

In TTY mode, `sync` prints phase-by-phase progress to stderr. In `--json` mode, stderr is silent and only the final JSON result is emitted on stdout.

### `knoten verify`

Runs SQLite integrity check, FTS5 / notes cardinality check, file existence, and orphan cleanup.

In local mode only the non-destructive checks run — integrity, cardinality, and the stat-walk that catches up external edits. No orphan sweep, no re-fetch: the vault is the source of truth, not a mirror to repair. The JSON payload carries `"mode": "local"`.

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
