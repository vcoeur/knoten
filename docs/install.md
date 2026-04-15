---
title: Install · knoten
description: How to install knoten, bootstrap the vault, and configure optional remote-mode sync with a compatible backend.
---

# Install

## From PyPI

```bash
pipx install knoten
# or: uv tool install knoten
```

Both install `knoten` into its own isolated venv and put it on your `$PATH`.

## First run

For **local mode** (the default), that's all you need. The vault and the SQLite index are created lazily on your first command:

```bash
knoten list                      # empty vault — prints an empty table
knoten create --filename "- First thought" --body "Hello from my new vault."
knoten search "hello"
```

Optional bootstrap — pre-seed a commented `.env` and create the vault dirs up front instead of lazily:

```bash
knoten init
```

Verify the configuration at any time:

```bash
knoten --help
knoten config show --json
knoten status
```

## Remote mode

Remote mode turns the local vault into a mirror of a **compatible backend** — any HTTP service that implements the knoten sync protocol. Local mode is fully featured on its own; remote mode only adds multi-device sync.

!!! note "About the backend"
    knoten does not bundle or host a public backend. The protocol is designed so anyone can run their own. The author is currently running an experimental instance to validate the sync contract; it is not a hosted product, not advertised, and not open to the public.

To enable remote mode, edit your `.env`:

```bash
knoten config edit                # opens $KNOTEN_CONFIG_DIR/.env in $EDITOR
```

Set the URL of your backend and an API token:

```env
KNOTEN_API_URL=https://your-backend.example.com
KNOTEN_API_TOKEN=<paste your token here>
```

**Getting an API token** depends on the backend. Whichever one you point `KNOTEN_API_URL` at needs to expose a way to issue a scoped token — typically a `settings → tokens` screen with an `api` scope, shown once at creation. Paste it into `.env`.

First sync:

```bash
knoten sync                       # incremental from empty — fetches everything
knoten sync --verify              # add full body-hash verification
```

Reads stay offline against the local mirror after the initial sync. Writes (`create`, `edit`, `append`, `delete`, `rename`, `restore`, `upload`, `download`) hit the remote first, then refresh the affected note locally.

## Cross-OS paths

`knoten` follows each OS's standard "config dir + data dir + cache dir" layout via [`platformdirs`](https://platformdirs.readthedocs.io/):

| Role | Linux (XDG) | macOS | Windows |
|---|---|---|---|
| Config (`.env`) | `~/.config/knoten/` | `~/Library/Application Support/knoten/` | `%APPDATA%\knoten\` |
| Data (markdown vault) | `~/.local/share/knoten/kasten/` | `~/Library/Application Support/knoten/kasten/` | `%LOCALAPPDATA%\knoten\kasten\` |
| Cache (SQLite + sync state) | `~/.cache/knoten/` | `~/Library/Caches/knoten/` | `%LOCALAPPDATA%\knoten\Cache\` |

Any of the three can be overridden via env vars — useful for tests, Docker, or custom deployments:

```bash
export KNOTEN_CONFIG_DIR=/etc/knoten
export KNOTEN_DATA_DIR=/srv/knoten/data
export KNOTEN_CACHE_DIR=/var/cache/knoten
```

Inspect the resolved paths:

```bash
knoten config path                 # plain output, one path per line
knoten config path --json          # JSON, scriptable
knoten config show                 # all values including API token (redacted)
```

## Development from a source checkout

```bash
git clone https://github.com/vcoeur/knoten.git
cd knoten
make dev-install                   # uv sync --all-groups
cp .env.example .env               # optional — only for remote mode
uv run knoten --help               # run the CLI straight from the repo
uv run knoten config show --json
make test                          # pytest
```

When run from the repo, `knoten` picks up the `.env` at the repo root, keeps the vault at `<repo>/kasten/`, and puts the SQLite cache under `<repo>/.dev-state/cache/` so derived state stays out of the main tree.

## Upgrading from v0.1.x

The first run of v0.2+ from an installed copy automatically moves:

| Legacy path (v0.1.x) | New path (v0.2+) |
|---|---|
| `~/.knoten/kasten/` | `$KNOTEN_DATA_DIR/kasten/` |
| `~/.knoten/.knoten-state/index.sqlite` | `$KNOTEN_CACHE_DIR/index.sqlite` |
| `~/.knoten/.knoten-state/state.json` | `$KNOTEN_CACHE_DIR/state.json` |
| `~/.config/knoten/.env` | `$KNOTEN_CONFIG_DIR/.env` (same file on Linux — no-op) |

No data loss, no manual steps. `KNOTEN_HOME` still works as a one-release deprecation shim — knoten prints a warning and tells you to use `KNOTEN_CONFIG_DIR` / `KNOTEN_DATA_DIR` / `KNOTEN_CACHE_DIR` instead.
