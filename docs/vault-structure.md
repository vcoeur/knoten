---
title: Vault structure · knoten
description: The zettelkasten folder and filename conventions used by the knoten author — note families, prefixes, wiki-links, literature notes, and how quelle feeds sources into the vault.
---

# Vault structure

`knoten` itself does not prescribe a vault structure. It gives you a vault, a search index, wiki-links, rename cascade, and backlinks — how you organise what goes inside is up to you. Filename **prefixes** are the one piece of convention `knoten` knows about, because they drive the `--family` filter on most read commands. Everything else on this page is just one working setup.

This is the convention I actually use day-to-day. Copy it verbatim, adapt it, or ignore it entirely — but if you are starting from zero, copying first and adapting later is faster than inventing.

## The problem this solves

A zettelkasten without conventions turns into a pile of dated snippets that nobody re-reads. The convention below answers three specific questions in under a second each:

- **Where does this note go?** The family prefix answers it.
- **Is this my opinion or someone else's?** Literature notes hold other people's ideas, permanent notes hold mine, entity notes hold context-free references — the family answers it.
- **Can I trust this fact later?** Dated facts (CEO, version, hosting provider, headcount) are written with a year so stale data is visible on sight.

## Note families

Eleven families, all distinguished by a short prefix at the start of the filename. The prefix is mandatory and never changes after creation — `knoten rename` enforces that the family prefix stays the same.

| Family | Prefix | Folder | Purpose | Example filename |
|---|---|---|---|---|
| **Person** | `@` | `entity/` | An individual — scholar, engineer, contact, author | `@ Jane Doe` |
| **Organization** | `$` | `entity/` | Company, institution, team, group | `$ Acme Corp` |
| **Entity** | `%` | `entity/` | Concrete things — tools, products, software, places | `% Prusa i3 MK4` |
| **Topic** | `&` | `entity/` | Concepts, methods, technologies — abstract nouns | `& Neural networks` |
| **Reference** | `CiteKey=` | `literature/` | One anchor per source (paper, book, webpage, experiment) | `Scott2019= Radical Candor` |
| **Literature** | `CiteKey.` | `literature/` | One atomic idea extracted from a source | `Scott2019. Candor requires caring personally` |
| **Day** | `YYYY-MM-DD` | `journal/` | Date anchor for grouping dated content | `2026-04-15` |
| **Journal** | `YYYY-MM-DD Title` | `journal/` | Dated findings, configs, notes-to-self | `2026-04-15 Offline-first is calmer` |
| **File** | `CiteKey+` / `YYYY-MM-DD+` | `files/` | Binary attachment (PDF, image) via `knoten upload` | `Scott2019+ Radical Candor.pdf` |
| **Fleeting** | `-` | `note/` | Quick capture, inbox, may be promoted or discarded | `- maybe try X` |
| **Permanent** | `!` | `note/` | Your own synthesis, linking literature together | `! Candor compounds with proximity` |

### Shell quoting for organization wiki-links

`$` is a shell metacharacter. When you want to `knoten create` or wiki-link an organization from a terminal string, escape it so the shell does not try to expand `$ Acme` as a variable:

```bash
knoten create --filename "\$ Acme Corp" --body 'Founded in 1923.'
# inside a note body:
# [[\$ Acme Corp]] was founded in 1923.
```

## Folder layout

The vault is flat — typed subfolders at the top level, nothing nested deeper:

```
~/.local/share/knoten/kasten/
├── entity/                   # @ $ % & — the knowledge graph's nouns
│   ├── @ Alice Voland.md
│   ├── $ Acme Corp.md
│   ├── % Prusa i3 MK4.md
│   └── & Neural networks.md
├── literature/               # Key= references and Key. literature notes
│   ├── Scott2019= Radical Candor.md
│   ├── Scott2019. Candor requires caring personally.md
│   └── Gleick1987= Chaos.md
├── note/                     # permanent and fleeting notes
│   ├── ! Candor compounds with proximity.md
│   └── - maybe try X.md
├── journal/                  # day anchors + dated journal entries
│   ├── 2026-04-15.md
│   └── 2026-04-15 Offline-first is calmer.md
├── files/                    # file-kind notes pointing at attachments
│   └── Scott2019+ Radical Candor.md
├── .attachments/             # the actual binary blobs (PDFs, images)
│   └── scott2019-radical-candor.pdf
└── .trash/                   # soft-deleted notes (reversible with `knoten restore`)
```

You never manually create these subfolders — `knoten create` places files by family automatically, based on the prefix.

## Linking conventions

Inside a knoten vault, use **wiki-links** for internal references and standard Markdown links only for things outside the vault.

```markdown
[[@ Jane Doe]] runs the frontend team at [[\$ Acme Corp]].
The paper argues for [[& Diffuse attention]] over hard focus ([[Scott2019. Candor requires caring personally]]).
External data from [Our World In Data](https://ourworldindata.org/).
```

Why split them:

- Wiki-links drive the `wikilinks` index, which powers `knoten backlinks`, `knoten graph`, and the rename cascade. Markdown links are opaque to those features.
- Keeping external references in Markdown syntax means they do not clutter the graph — you never want `https://ourworldindata.org/` as a node.

Every wiki-link should resolve to a real note. If you mention a new concept, stop and create a stub for it before finishing the current note — see below.

## Entity stubs

An entity stub is a minimal, definitional note for a concept, person, tool, or organization referenced elsewhere. The point is to make wiki-links resolve and the graph connect — not to write an encyclopaedia.

```markdown
# @ Josef Průša

Czech engineer, founder of [[\$ Prusa Research]] (2012–).

---
source: https://en.wikipedia.org/wiki/Josef_Pr%C5%AF%C5%A1a
```

```markdown
# % PrusaSlicer

Open-source 3D printing slicer by [[\$ Prusa Research]].
```

```markdown
# & CoreXY

Motion system for 3D printers using two motors on crossed belts,
allowing higher print speeds than bed-slinger kinematics.
```

Rules I follow:

- **Short.** One to three sentences. If you have more to say, write a permanent note and link to it from the stub.
- **Definitional, not narrative.** "Czech engineer, founder of …" — not "Josef Průša is known for …".
- **Dated facts are dated.** "CEO of [[\$ Acme]] (2019–)", "based in Paris (2022–)". When the fact changes, add a new year; never silently overwrite history.
- **Link back.** A person links to their organization; an entity links to its maker; a topic links to its related entities.

The stub habit is what turns the vault into a graph. Every time you mention a new concept in a literature note or a journal entry, create a stub for it before finishing the note.

## Journal vs permanent — the hardest distinction

Journal notes and permanent notes both contain your own writing. The difference is what they're *for*.

- **Journal notes** (`YYYY-MM-DD Title`, in `journal/`) capture **findings anchored in time** — configs, snippets, architectural decisions, debug sessions, research dumps, project notes, anything that came out of a specific moment of work. They decay in relevance as the codebase they describe changes. Most of what you write ends up here.
- **Permanent notes** (`! Title`, in `note/`) capture **your own synthesis** of what you read in literature notes — atomic, self-contained ideas, usually three to ten sentences, with wiki-links to the literature they were distilled from. They compound: a good permanent note from three years ago is still useful today.

A concrete test: **if the note would become wrong when an external codebase changes, it is a journal note. If the note would become wrong when *you* change your mind, it is a permanent note.**

Most developer captures are journal notes. Do not feel bad about that — the journal is where real work happens, permanent notes are the occasional distilled output.

!!! tip "From working-directory artifacts to journal notes"
    When capturing something out of a project, incident, or research session, name the journal note after the item (`2026-04-15 P1S firmware investigation`) and mention the short name in the body (not the repo path — those rot). Also create stubs for every tool, repository, organization, or person mentioned. That is how the capture becomes reachable six months later when you have forgotten which project it came from.

## Literature notes — reference + atomic idea

A **reference** note is a single anchor per source. A **literature** note is a single atomic idea extracted from that source. They share the same citation key, so `knoten backlinks "Scott2019= Radical Candor"` surfaces every literature note, file note, and permanent note that traces back to the source.

```markdown
# Scott2019= Radical Candor

---
family: reference
authors:
  - "[[@ Kim Scott]]"
year: 2019
url: https://www.radicalcandor.com/the-book/
pdf: "[[Scott2019+ Radical Candor.pdf]]"
source: Scott2019
---

*Radical Candor* is Kim Scott's management book on giving feedback by
combining challenging directly with caring personally.
```

```markdown
# Scott2019. Candor requires caring personally

---
family: literature
source: Scott2019
---

Scott's central claim: you cannot challenge someone directly in a way that
improves their work unless they feel you care about them as a person. The
two axes are independent — high care and low challenge is "ruinous empathy",
low care and high challenge is "obnoxious aggression", low + low is
"manipulative insincerity", high + high is radical candor.

Distilled from: [[Scott2019= Radical Candor]]
```

Citation key rules I follow — all lifted from BibTeX convention:

- One author: `AuthorYYYY` (e.g. `Scott2019`)
- Two authors: `AuthorOtherYYYY` (e.g. `ScottDoe2019`)
- Three or more: `AuthorAlYYYY` (e.g. `ScottAl2019`)
- No spaces, no underscores, no special characters.

The `=`, `.`, `+` suffixes after the citation key are the family discriminator and are part of the filename on disk.

**Decision rule for new sources:** use the reference + literature split **only if** the source yields multiple atomic ideas. A one-shot blog post with a single takeaway is a journal note, not a reference/literature pair. The cost of two extra files per idea is not worth it for a single quote.

## Source ingest with `quelle`

Manually typing reference metadata for academic papers — authors, year, DOI, journal, abstract — is slow and error-prone. [`quelle`](https://quelle.vcoeur.com) resolves a paper by DOI, arXiv id, or free-text title and returns normalised JSON: authors, year, DOI, journal, open-access PDF URL if one exists. The natural pipeline is:

```bash
# 1. Resolve the paper.
quelle fetch 10.1109/83.902291 --json > /tmp/paper.json

# 2. Derive a citation key and title from the JSON.
cite=$(jq -r '.authors[0].family + (.year | tostring)' /tmp/paper.json)
title=$(jq -r '.title' /tmp/paper.json)

# 3. Create the reference note.
knoten create \
  --filename "${cite}= ${title}" \
  --frontmatter-file /tmp/paper.json \
  --body "$(jq -r '.abstract // ""' /tmp/paper.json)" \
  --json

# 4. If an open-access PDF is available, download and attach it.
quelle fetch 10.1109/83.902291 --download-pdf
knoten upload "$(quelle config path data)/pdfs/10.1109_83.902291.pdf" \
  --filename "${cite}+ ${title}.pdf" \
  --json
```

The file note and the reference note share the citation key, so `knoten backlinks "Scott2019= Radical Candor"` surfaces the attachment alongside the literature notes. Wire this into a small script or a Claude Code skill — do not type it by hand every time.

For stubs: after creating the reference note, also create author stubs (`@ Kim Scott`) for any author not already in the vault, and link the reference note's `authors:` frontmatter at them. Topic and organization stubs should stay hand-curated — auto-generating them from abstracts is too noisy to be worth it.

## What I deliberately do not use

- **Tags.** I filter by `--family` and navigate by wiki-link graph. Tags are optional — use them if they help you, but the structural signal is in the families and the links, not in tags.
- **Deep nesting.** The five top-level folders are flat on purpose. Finding a note happens via search, not by walking directories.
- **Long frontmatter.** A handful of keys (`year`, `authors`, `url`, `source`, `family`) — the rest is noise that rots.
- **Filename timestamps.** No Zettelkasten-style `202604151820-my-note.md` IDs. The date prefix on journal notes is enough; everything else is addressed by title.

## Minimum starting vault

If you are starting from zero, seed the vault with these notes to exercise every family at least once. After that, every new piece of content will have an obvious home:

```bash
knoten create --filename "@ $(git config user.name)" --body "This is me."
knoten create --filename "\$ V-Coeur" --body "My personal projects workspace."
knoten create --filename "% knoten" --body "This CLI. https://knoten.vcoeur.com"
knoten create --filename "& Zettelkasten" --body "Atomic, linked, dated notes."
knoten create --filename "$(date +%Y-%m-%d)" --body "Day anchor."
knoten create --filename "$(date +%Y-%m-%d) Starting my vault" \
  --body "First journal entry, seeded the family stubs."
```

Five minutes from now you will want to write your first real permanent note. Write it.

## Related reading

- [Commands](commands.md) — the CLI verbs that drive reads, writes, graph queries, and the rename cascade.
- [Install](install.md) — cross-OS paths, remote-mode setup, upgrade from v0.1.
- [`quelle`](https://quelle.vcoeur.com) — the companion CLI that resolves papers into JSON ready for a reference note.
