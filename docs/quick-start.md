---
title: Quick start · knoten
description: Install knoten and its example Claude Code skill, then walk through a first session — create a note, search it, follow a wiki-link, rename with cascade — all through /knoten in Claude Code.
---

# Quick start

The fastest way to understand `knoten` is to drive it from a Claude Code session with the generic example skill shipped in the repo. Five minutes end-to-end: install the CLI, install the skill, say what you want in natural language, watch Claude translate it into `knoten` calls.

This page assumes you already use [Claude Code](https://docs.claude.com/en/docs/claude-code/overview). If you don't, every `knoten` call below also works as a plain shell command — type them yourself.

## 1. Install the CLI

```bash
pipx install knoten
# or: uv tool install knoten
```

Verify the install and inspect the effective configuration:

```bash
knoten --help
knoten config show --json
knoten status
```

The first `knoten` call creates the vault, the SQLite index, and a commented `.env` lazily. You do not need to run `knoten init` unless you want to pre-seed them up front. Default Linux paths:

- Vault: `~/.local/share/knoten/kasten/`
- Config: `~/.config/knoten/.env`
- Cache: `~/.cache/knoten/index.sqlite`

macOS and Windows use their respective platform locations — see [Install](install.md#cross-os-paths) for the full table.

## 2. Install the example skill

`knoten` ships a generic [`SKILL.md`](https://github.com/vcoeur/knoten/blob/main/SKILL.md) at the root of its repo. It wraps every read/write/sync command, always passes `--json`, and is deliberately opinion-free about vault conventions so you can fork it and impose your own.

Drop it into one of these locations:

```bash
# Globally available in every Claude Code session
mkdir -p ~/.claude/skills/knoten
curl -fsSL https://raw.githubusercontent.com/vcoeur/knoten/main/SKILL.md \
  -o ~/.claude/skills/knoten/SKILL.md
```

Or project-local — auto-loaded only inside a specific repo:

```bash
mkdir -p <your-project>/.claude/skills/knoten
curl -fsSL https://raw.githubusercontent.com/vcoeur/knoten/main/SKILL.md \
  -o <your-project>/.claude/skills/knoten/SKILL.md
```

Reload skills in Claude Code (or start a new session) and `/knoten` becomes a slash command that takes a natural-language request as its argument.

## 3. Your first session

The skill takes a free-text request, picks the right subcommand, passes `--json`, and parses the envelope. You never write shell syntax yourself — but the transcripts below show the actual CLI calls Claude makes under the hood so you can see what `/knoten` translates to.

### Create a note

```
You: /knoten create a permanent note "! Offline-first is calmer"
     with body "Offline knowledge bases are less stressful than online ones."

Claude: Creating the note.
→ knoten create --filename "! Offline-first is calmer" \
      --body "Offline knowledge bases are less stressful than online ones." \
      --json
✓ Created note "! Offline-first is calmer" (uuid 7f3a…).
```

The `!` prefix marks it as a **permanent** note — your own synthesis, meant to compound. The [Vault structure](vault-structure.md) page explains the full family prefix convention.

### Search

```
You: /knoten search "offline"

Claude: Searching the local index.
→ knoten search "offline" --json
1. ! Offline-first is calmer                             2026-04-15
   "… **Offline** knowledge bases are less stressful …"
```

Search hits the local SQLite FTS5 index and returns in single-digit milliseconds even across thousands of notes. No network.

### Create a linked note and follow the wiki-link

```
You: /knoten create a literature note "Gleick1987. Telegraph as ancestor
     of the internet" that links to [[! Offline-first is calmer]]

Claude: Creating the note and linking.
→ knoten create --filename "Gleick1987. Telegraph as ancestor of the internet" \
      --body "Every distributed system has ancestors. [[! Offline-first is calmer]] applies here." \
      --json
✓ Created literature note.

You: /knoten show the backlinks of "! Offline-first is calmer"

Claude:
→ knoten backlinks "! Offline-first is calmer" --json
1. Gleick1987. Telegraph as ancestor of the internet
```

The wiki-link was picked up by the mtime-gated stat walk on the next command — no manual reindex, no explicit sync.

### Broaden by graph

```
You: /knoten what's within 2 hops of "! Offline-first is calmer"?

Claude:
→ knoten graph "! Offline-first is calmer" --depth 2 --direction both --json
Nodes: 2
- depth 0: ! Offline-first is calmer
- depth 1: Gleick1987. Telegraph as ancestor of the internet
```

`graph` is the "broadened search" primitive — it returns the BFS neighbourhood of a starting note so you can follow a thread without knowing the exact titles of the connected notes.

### Rename with cascade

```
You: /knoten rename "! Offline-first is calmer" to "! Calm comes from locality"

Claude:
→ knoten rename "! Offline-first is calmer" "! Calm comes from locality" --json
✓ Renamed; 1 referencing note rewritten.
```

Every note that had `[[! Offline-first is calmer]]` now has `[[! Calm comes from locality]]`. Rollback on partial failure is automatic — if the rewrite fails mid-way, the original bytes of every touched file are restored before the error is raised.

## 4. Verify and take stock

```
You: /knoten status

Claude:
→ knoten status --json
Local vault: 2 notes, 1 wiki-link, last walk 2 seconds ago, no drift warnings.
```

That is the entire round trip: install, drop a skill, create, search, link, rename — no server, no browser, no daemon.

## Where to go from here

- Your vault so far has two notes and no structure. The **[Vault structure](vault-structure.md)** page describes the folder and filename convention I actually use day-to-day — eleven note families, filename prefixes, the journal-vs-permanent distinction, entity stubs, and how [`quelle`](https://quelle.vcoeur.com) feeds literature notes from DOIs and arXiv IDs so you do not have to type reference metadata by hand.
- When you outgrow the generic skill, fork it. The comments inside `SKILL.md` call out the sections to replace (conventions, post-write side-effects, batch operations).
- The full CLI surface — every verb, every flag, every envelope — lives in **[Commands](commands.md)**.

## Troubleshooting

- `command not found: knoten` — `pipx install knoten` did not add its bin dir to your `$PATH`. Run `pipx ensurepath` and reopen the shell.
- `/knoten` is not recognised in Claude Code — the skill file is not being picked up. Confirm the path is exactly `~/.claude/skills/knoten/SKILL.md` (global) or `<project>/.claude/skills/knoten/SKILL.md` (project-local), then restart Claude Code.
- Search returns nothing even though you just created a note — run `knoten reindex` to rebuild the FTS tables from the on-disk files. In local mode this is almost never needed, but if you edited the vault directly while `knoten` was mid-walk, the FTS can briefly drift.
