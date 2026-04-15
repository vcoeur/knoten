---
title: knoten — CLI zettelkasten
description: Standalone CLI zettelkasten with a local markdown vault and SQLite FTS5 index. Offline-first, with an optional pluggable remote backend for multi-device sync.
---

# knoten

<p class="tagline">Notes, knotted together.</p>

Standalone CLI zettelkasten with a local Markdown vault and SQLite FTS5 index. Runs offline as a self-contained notes system, and can optionally plug into a remote backend for multi-device sync.

## Install

```bash
pipx install knoten
# or
uv tool install knoten
```

Both install `knoten` into its own isolated venv and put it on your `$PATH`. See [Install](install.md) for the long-form guide and cross-OS paths.

## 60-second quickstart

```bash
# Create your first note — vault + SQLite index auto-create on demand.
knoten create --filename "- First thought" --body "Hello from my new vault."

# Read, list, search — all offline.
knoten list
knoten search "hello"
knoten read "- First thought"
```

Example output:

```
$ knoten search "hello"
1. - First thought                         2026-04-15  [ ]
   "… **Hello** from my new vault. …"
```

All commands accept `--json` for machine-readable output. See [Commands](commands.md) for the full reference.

## What it does

- **Offline-first.** The local Markdown vault is the source of truth; SQLite is a derived FTS5 index that catches up to external edits on every invocation. Edit `.md` files in any editor — the next `knoten` invocation picks up the changes via a mtime-gated stat walk.
- **Fast search.** Ranked full-text search (title > filename > body), with `--fuzzy` for typo-tolerant queries (trigram FTS + rapidfuzz on titles) and `--tag` / `--family` / `--kind` filters.
- **Wiki-link graph.** `knoten graph <target> --depth 2 --direction both` returns the BFS neighbourhood of a note — nodes with their distance from the start, plus edges — for broadened search.
- **Soft delete and rename cascade.** `knoten delete` moves files to `<vault>/.trash/` (reversible via `knoten restore`); `knoten rename` rewrites `[[old]]` wiki-links in every referencing note and rolls back on partial failure.
- **Pluggable remote backend (optional).** Set `KNOTEN_API_URL` to a compatible backend and knoten becomes a multi-device mirror: reads stay offline, writes hit the remote first and refresh the local copy. The author runs an experimental backend instance used to validate the sync protocol — no public backend is bundled, and the CLI is fully usable without one.

## Why knoten

A zettelkasten you can run without a server, without a browser, and without losing the plot: the vault is just Markdown files, the index rebuilds itself, and the CLI is scriptable (every command takes `--json`). Good for personal research notes, Claude skills that need to query a knowledge base, and anyone who wants wiki-linked Markdown without a heavyweight app.

## Learn more

- [Install guide](install.md) — prerequisites, first-run config, cross-OS paths, remote-mode setup
- [Commands](commands.md) — full CLI reference
- [Source on GitHub](https://github.com/vcoeur/knoten)
- [`knoten` on PyPI](https://pypi.org/project/knoten/)
