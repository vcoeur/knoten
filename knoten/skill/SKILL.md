---
name: knoten
description: Drive a local knoten zettelkasten vault via the `knoten` CLI — search, read, browse backlinks/graph, and (on request) create/edit/append notes. Works local-only (self-contained markdown + SQLite FTS5, no network) or in remote mirror mode. Use whenever the user wants to query, explore, or write to their knoten notes. This is the CLI contract; layer your own vault conventions in a separate skill on top.
argument-hint: "<natural-language request, or a knoten subcommand>"
allowed-tools: Read, Write, Edit, Bash(knoten:*), Bash(python3:*), Bash(jq:*), Bash(command -v knoten:*)
---

Thin, **convention-free** wrapper around the [`knoten`](https://github.com/vcoeur/knoten) CLI. It encodes how to drive the tool *correctly and safely* — JSON parsing, error handling, permissions, batch writes, shell-quoting. It deliberately encodes **no vault conventions** (note families, filename grammar, tagging scheme). Those belong in a separate, user-specific skill that references this one. Discover the conventions a given vault enforces with `knoten schema --json`.

## Use at your own risk

`knoten` is MIT software, **as-is, no warranty**. This skill runs write commands that mutate your vault (and, in remote mode, whatever `KNOTEN_API_URL` points at). Keep your vault under version control. **Never run `knoten delete` without explicit user confirmation** — soft-deletes land in `<vault>/.trash/` (restore with `knoten restore <uuid>`), but a plain `rm` of a vault file is permanent.

## Request

> $ARGUMENTS

---

## 1 — Prerequisites

```bash
command -v knoten || pipx install knoten   # or: uv tool install knoten
```

Local-only mode needs no further setup — the vault auto-creates on first write. For remote mirror mode, set `KNOTEN_API_URL` + `KNOTEN_API_TOKEN` (`knoten config edit`), then `knoten config show --json` to verify.

If `knoten` is not on PATH (webapp sessions, fresh host) the vault is unreachable — say so and stop. **Never `curl` the REST API or hand-edit vault files directly** — both bypass the index/sync and corrupt search, backlinks, and wikilinks.

## 2 — Always pass `--json`

Every command supports `--json` except `init`, `config edit`, and `mcp serve` (no payload worth shaping — passing `--json` to those is a usage error). Pass it everywhere else — the TTY rendering is for humans; the JSON envelope is the stable, parseable contract. Parse with `jq` or `python3 -c "import sys,json; ..."`.

## 3 — Errors are structured (parse, don't string-match)

On failure, `--json` commands print an envelope to **stdout** (not stderr) and exit non-zero:

```json
{"error": "<kind>", "message": "human text", "code": <int>}
```

Branch on `error`, never on the message. Kinds: `config`, `auth`, `network`, `store`, `lock_timeout`, `permission_denied`, `ambiguous_target`, `not_found`, `user`, `validation`, `knoten`, `unknown`. Some carry extras — `ambiguous_target` → `candidates: [{id, filename}]`; `permission_denied` → `note_id, current_level, required_level, operation`; `validation` → `issues: [{key, expected, actual}]`.

Caveat: a malformed command line (Click usage error) also exits 2 — the same code as `network`/`auth` — but prints usage text to stderr with **no JSON envelope** on stdout. On exit 2, check stdout parses as JSON before treating it as a network failure.

## 4 — Discover the contract: `knoten schema`

Before writing into an unfamiliar vault, dump the machine-readable contract once:

```bash
knoten schema --json
```

It returns: every command + its flags, the family→prefix→kind→directory table, the permission ladder, and the error kinds + exit codes — the source of truth for *this vault's* note families and filename prefixes. This skill does not hard-code them.

## 5 — Sync cadence (remote mode only)

The local mirror does not auto-refresh in remote mode. At session start run `knoten status --json` (cheap, offline) and check `seconds_since_last_sync`: if `null` or large (e.g. `> 600`), run `knoten sync --json` before searching. Write commands refresh the affected note synchronously — no explicit sync needed after a write. In **local mode**, every invocation runs an mtime-gated stat-walk that picks up external edits automatically.

## 6 — Permissions (remote mode)

Each note carries a permission level (`NONE < LIST < READ < APPEND < WRITE < ALL`), enforced server-side and pre-checked client-side. It appears on every `read`/`search`/`list` payload. **Check it before attempting a write**; on insufficient level the command exits non-zero with `error: "permission_denied"` — surface it, don't retry with `--force`. On `APPEND`-level notes only `knoten append` works (not `edit`). Local-only vaults have no permission model — every note is fully writable.

## 7 — Command cheat sheet

Path/id arguments accept a UUID, an exact filename, or an unambiguous filename prefix.

### Read path (offline, sub-10ms)
| Command | Purpose |
|---|---|
| `knoten status --json` | Mirror snapshot (counts, last sync, drift) |
| `knoten search "<q>" --json` | FTS5 search (`--family --kind --tag --limit --offset --fuzzy --explain`). `--in title\|body\|filename` scopes columns (not with `--fuzzy`). `--fields minimal\|full` trims each hit. 0 ranked hits → payload `hint` + `fuzzy_total` when `--fuzzy` would match. |
| `knoten similar <target> --json` | Related notes, no embeddings — derives a term query from the note + ranks it (`--limit` 10/max 50, `--family --kind --tag`). Payload `{target_id, target_filename, derived_query, total, hits}`. |
| `knoten read <t> [<t2> …] --json` | Full note(s): body + wikilinks + backlinks (`--no-backlinks`). Multi-target → `{targets, notes, failed}` (exit 0 if ≥1 resolved). `--fields meta\|full` (meta drops body), `--max-body-chars N` (adds `body_truncated` + `body_total_chars` when cut; not with `meta`). |
| `knoten list --json` | Metadata, no bodies (`--family --kind --tag --source --sort --limit`). `--updated-after`/`--created-after` ISO date or datetime. `--fields minimal\|full`. |
| `knoten backlinks <target> --json` | Notes linking here |
| `knoten graph <target> --json` | BFS wikilink neighbourhood (`--depth 0..5`, `--direction out\|in\|both`) |
| `knoten tags --json` / `knoten kinds --json` | Counts |
| `knoten unresolved --json` | Dangling wikilink targets + their referencing notes. `--target <note>` scopes to links from one note. |
| `knoten path <target> --json` | Absolute file path on disk |
| `knoten citekeys --json` | The vault's in-use CiteKeys (distinct non-empty `source` values, sorted). `--prefix STR`. Plain output is one-per-line, pipe-friendly. |
| `knoten inbox list --json` | Pending `#inbox` fleetings, oldest first (`--limit --offset`; notes tagged `inbox-promoted` are excluded and `total` is the global pending count). |

### Write path
| Command | Purpose |
|---|---|
| `knoten create --filename "<prefix Title>" --body-file PATH --frontmatter-file PATH.json --json` | New note. `--body -`/`--body-file -` read stdin. `--kind`, `--tag` (repeatable), `--ai`. `--dry-run` previews family/kind/source + unresolved links without writing. |
| `knoten create --batch FILE --json` | Bulk create from a JSON array of drafts (`-` for stdin) under one lock pass. See §9. |
| `knoten reference --from-source FILE --json` | Create a CiteKey-anchored reference note from a quelle Source JSON object (`-` for stdin). Maps quelle `kind`→reference kind, builds hyphen-key frontmatter, filename `<CiteKey>= <Title>`. `--body`/`--body-file`, `--ai`, `--tag`, `--dry-run`, `--fields`. |
| `knoten append <target> --content-file PATH --json` | Append (works at `APPEND` level). |
| `knoten edit <target> --body-file PATH --json` | Replace body/filename/frontmatter/tags. `--add-tag --remove-tag --set-frontmatter k=v --unset-frontmatter k`. Family prefix immutable. `--dry-run` supported. Bulk: `--batch FILE\|-` (see §9). |
| `knoten edit <target> --set-frontmatter-json k=<json> --json` | Set a **typed** frontmatter value (int/list/bool/null round-trip). Use this for numbers/lists; `--set-frontmatter` only sends strings. See §8. |
| `knoten rename <target> "<new>" --json` | Thin wrapper over `edit --filename`. `--dry-run` supported. |
| `knoten delete <target> --yes --json` | **Soft** delete. Confirm with the user first. |
| `knoten restore <uuid> --json` | Restore from trash. |
| `knoten upload <path> --filename "<prefix Label>" --json` | Attachment + file-note. |
| `knoten download <target> [-o PATH]` | Stream an attachment back out (target is the file-family **note**, not the blob). |
| `knoten inbox add <file\|url\|text> --json` | Quick capture into a new fleeting `#inbox` note. Heuristic: readable file → upload + wikilink; `https?://` → URL (title fetched for the slug); else text. Force with `--as-file\|--as-url\|--as-text`; `--note` adds context; `--tag` repeatable. |
| `knoten inbox append <fleeting> <file\|url\|text> --json` | Append a capture to an existing fleeting (same heuristic; tags untouched). |

### Sync / maintenance
`knoten sync --json` (incremental; `--full`, `--verify`, `--force-delete` to override the mass-delete guard) · `knoten verify --json` (local mode: integrity + index checks only, non-destructive) · `knoten reindex --json` (rebuild derived tables, no network).

## 8 — Typed frontmatter

- **At create time**, seed typed frontmatter with `--frontmatter-file PATH.json` — JSON types round-trip (ints stay ints, lists stay lists, `null` stays null).
- **On an existing note**, use `--set-frontmatter-json key=<json-literal>` to change a typed field, e.g. `--set-frontmatter-json year=1990` or `--set-frontmatter-json 'authors=["[[@ Jane Doe]]"]'`. Plain `--set-frontmatter key=value` sends the value as a **string** — fine for text fields, rejected by a type-checking backend for numbers/lists.

## 9 — Batch writes: one process, not N calls

For **3+ creates** (the common post-write stub case), do not chain individual `knoten create` calls — that's N permission prompts, N lock passes, and N rounds of escaping. Pipe a JSON array of drafts instead:

```bash
python3 - <<'PY' | knoten create --batch - --json
import json
print(json.dumps([
  {"filename": "% PrusaSlicer", "body": "Open-source slicer.[^1]\n\n[^1]: ...", "ai": True,
   "frontmatter": {"family": "entity", "kind": "entity", "url": "https://example.com"}},
  {"filename": "% OrcaSlicer", "body": "Fork of PrusaSlicer.", "ai": True},
]))
PY
```

Each item: `{filename, body?, kind?, tags?, frontmatter?, ai?}`. The result is `{"results": [{"index", "ok", "id"|"error"}], "created", "failed"}` — one bad draft does not abort the rest. Add `--dry-run` to preview every draft without writing.

`edit --batch FILE|-` mirrors this for edits (one lock pass, partial success, both modes). Each item: `{target, filename?, title?, body?, add_tags?, remove_tags?, set_frontmatter? (typed JSON — see §8), unset_frontmatter?, ai?}`; envelope `{operation: "edit-batch", count, edited, failed, results}`. `--batch` is mutually exclusive with a positional target and the per-note edit flags, and supports `--dry-run`.

## 10 — After a write: resolve dangling wikilinks

Every `[[wikilink]]` in a note body should resolve to a real note. After a write, find the dangling targets and stub them (per your conventions skill).

**After a single write, skip the vault-wide scan.** A write response (`create`, `edit`, `append`, `restore`, `reference`, `upload`) requested with `--fields full` carries `wikilinks: [{title, id, broken}]` for that one note — `broken: true` is a dangling target. Read it straight off the write you just did, no second command:

```bash
knoten create … --fields full --json | jq '.wikilinks[] | select(.broken) | .title'
```

For a vault-wide sweep (or after a `--batch`), list every dangling target instead:

```bash
knoten unresolved --json    # vault-wide: {target, reference_count, referenced_by:[{id,filename}]}
```

Note: piped wikilinks (`[[target|alias]]`) and heading-form links (`[[target#heading]]`) are not resolved/cascaded — use plain `[[target]]`.

## 11 — Avoid shell-quoting hazards (critical for writes)

Note bodies and wikilinks routinely contain `$`, `&`, `[`, `]`, `#`. Two hard rules:

1. **Never pass bodies inline via `--body "..."`.** Write the body to a temp file and use `--body-file`/`--content-file` (or pipe via `-`). This sidesteps all shell expansion.
2. **Never use `sed`/regex to rewrite body text containing wikilinks.** Use Python string ops + `knoten edit --body-file`.

---

## Installation

```bash
knoten skill install --user        # -> ~/.config/agents/skills/knoten/SKILL.md
knoten skill install --project     # -> <cwd>/.agents/skills/knoten/SKILL.md
knoten skill install --claude      # -> ~/.claude/skills/knoten/SKILL.md
knoten skill status                # show where it's installed and whether it matches the bundled copy
```

To add vault conventions (families, tagging, ingest flows), fork into a **separate** skill that references this one for mechanics — keep this file convention-free so it updates cleanly with the tool.

$ARGUMENTS
