---
title: knoten — CLI zettelkasten
description: Standalone CLI zettelkasten with a local Markdown vault and SQLite FTS5 index. Offline-first, optional pluggable remote backend, built for Claude Code skills and day-to-day research.
---

# knoten

<p class="tagline">Notes, knotted together.</p>

`knoten` is a CLI zettelkasten for people who already live in a terminal and a text editor, and who want their notes to behave like code: plain Markdown on disk, fast full-text search, wiki-links, and a clean scriptable interface for Claude Code and shell tooling.

## What it is

- A **local Markdown vault** — one `.md` file per note, editable in any editor you already use, trackable in git.
- A **SQLite FTS5 index** built from the vault, so search across thousands of notes returns in milliseconds.
- A **CLI** — every operation is a `knoten <verb>` command, every command returns JSON when asked, every command is safe to call from a script or an agent.
- An **optional remote backend** for multi-device sync. Never required — local mode is fully featured on its own.

## What it does

- **Offline-first.** The Markdown vault is the source of truth; SQLite is a derived index that catches up to external edits on every invocation. Edit `.md` files in Neovim, VS Code, or Obsidian and the next `knoten` call picks up the changes via an mtime-gated stat walk.
- **Ranked full-text search.** Title > filename > body, with snippets, tag/family/kind filters, and an opt-in `--fuzzy` mode for typo-tolerant and substring queries (trigram FTS + rapidfuzz on titles).
- **Wiki-link graph.** `knoten graph <note> --depth 2 --direction both` returns the BFS neighbourhood of any note — nodes with distances, plus edges — for broadened search without guessing exact titles.
- **Soft delete and rename cascade.** `knoten delete` moves files to `<vault>/.trash/` (reversible via `knoten restore`). `knoten rename` rewrites `[[old]]` wiki-links in every referencing note and rolls back on partial failure.
- **Pluggable remote backend (optional).** Point `KNOTEN_API_URL` at a compatible HTTP backend and the vault becomes a multi-device mirror. Reads stay local; writes hit the remote first, then refresh the local copy. No public backend is bundled — local mode is fully featured without one.

## Who it's for

- A solo researcher or developer who wants **wiki-linked Markdown without a heavyweight app** — no Electron, no browser, no daemon.
- Anyone building **Claude Code skills** that need a queryable knowledge base — every command takes `--json`, the envelopes are stable, and a generic example skill ships in the repo at [`SKILL.md`](https://github.com/vcoeur/knoten/blob/main/SKILL.md).
- Users who **already version their notes in git** and want the index to catch up to whatever they edit outside the CLI.

## Install

```bash
pipx install knoten
# or: uv tool install knoten
```

That is enough to start. The vault, the SQLite index, and a commented `.env` are all created lazily on the first command — no `init` step required.

## Learn more, in order

The pages are written to be read top-to-bottom the first time:

1. **[Quick start](quick-start.md)** — install the CLI, drop the example Claude Code skill into place, and run your first session: create a note, search it, follow a wiki-link, rename with cascade — all through natural-language requests to `/knoten`.
2. **[Vault structure](vault-structure.md)** — how I organise my own vault. Note families with prefixes (`@` person, `$` organization, `%` entity, `&` topic, `!` permanent, `-` fleeting, `YYYY-MM-DD` day/journal, `Key=` reference, `Key.` literature), filename conventions, wiki-links, entity stubs, the journal-vs-permanent distinction, and how [`quelle`](https://quelle.vcoeur.com) feeds literature notes from DOIs and arXiv IDs.
3. **Reference** — the long form: [Install](install.md) (cross-OS paths, remote mode, upgrade notes) and [Commands](commands.md) (every verb, every flag, every envelope).

## Links

- [Source on GitHub](https://github.com/vcoeur/knoten)
- [`knoten` on PyPI](https://pypi.org/project/knoten/)
- [Author](https://vcoeur.com)
