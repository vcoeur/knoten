"""Output helpers — JSON vs rich TTY rendering.

Every command calls one of these helpers with a plain dict. In `--json`
mode we emit JSON to stdout. In TTY mode we render with rich. In neither
(non-TTY plain mode) we emit a compact plain-text rendering.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class OutputMode:
    json: bool
    tty: bool

    @classmethod
    def detect(cls, json_flag: bool) -> OutputMode:
        return cls(json=json_flag, tty=sys.stdout.isatty() and not json_flag)


_console = Console()
_stderr_console = Console(stderr=True)


def log(message: str, *, mode: OutputMode) -> None:
    """Write a status line to stderr. Hidden in --json mode."""
    if mode.json:
        return
    _stderr_console.print(message)


def make_progress_callback(mode: OutputMode):
    """Return a progress callback that writes to stderr, silent in --json mode.

    Services (sync, reconcile, reindex) take a `progress` callable so the
    CLI can stream phase-by-phase status to the user. JSON mode keeps
    stdout clean for pipes; TTY mode gets coloured, dim-styled updates
    so the actual work is visible.
    """

    def _progress(message: str) -> None:
        if mode.json:
            return
        # Lines starting with "→" are phase headers; dim everything else.
        if message.startswith("→"):
            _stderr_console.print(f"[bold cyan]{message}[/bold cyan]")
        else:
            _stderr_console.print(f"[dim]{message}[/dim]")

    return _progress


def emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    sys.stdout.write("\n")


_PERMISSION_BADGE: dict[str, str] = {
    "NONE": "[red]N[/red]",
    "LIST": "[yellow]L[/yellow]",
    "READ": "[cyan]R[/cyan]",
    "APPEND": "[green]A[/green]",
    "WRITE": "[green]W[/green]",
    "ALL": "[dim]·[/dim]",
}


def _permission_badge(level: str | None) -> str:
    """Single-character TTY marker for an `mcp_permissions` level."""
    return _PERMISSION_BADGE.get(level or "ALL", "[dim]?[/dim]")


def render_search_hits(payload: dict[str, Any], *, mode: OutputMode) -> None:
    if mode.json:
        emit_json(payload)
        return
    hits = payload.get("hits", [])
    total = payload.get("total", len(hits))
    query = payload.get("query", "")
    if not hits:
        _console.print(f"No matches for [bold]{query}[/bold].")
        return
    if mode.tty:
        table = Table(title=f'{total} match(es) for "{query}"', show_lines=False)
        table.add_column("Score", justify="right", style="dim")
        table.add_column("P", justify="center")
        table.add_column("Family/Kind", style="cyan")
        table.add_column("Title", style="bold")
        table.add_column("Snippet", overflow="fold")
        for hit in hits:
            snippet = _colourise_snippet(hit.get("snippet", ""))
            family_kind = f"{hit.get('family', '')}/{hit.get('kind', '')}"
            table.add_row(
                f"{hit.get('score', 0.0):.2f}",
                _permission_badge(hit.get("mcp_permissions")),
                family_kind,
                hit.get("title", ""),
                snippet,
            )
        _console.print(table)
    else:
        for hit in hits:
            sys.stdout.write(
                f"{hit.get('score', 0.0):.3f}\t{hit.get('id', '')}\t{hit.get('title', '')}\n"
            )


def _colourise_snippet(snippet: str) -> Text:
    text = Text()
    remaining = snippet
    while remaining:
        start = remaining.find("<<")
        if start == -1:
            text.append(remaining)
            break
        end = remaining.find(">>", start + 2)
        if end == -1:
            text.append(remaining)
            break
        text.append(remaining[:start])
        text.append(remaining[start + 2 : end], style="bold yellow")
        remaining = remaining[end + 2 :]
    return text


def render_note(payload: dict[str, Any], *, mode: OutputMode) -> None:
    if mode.json:
        emit_json(payload)
        return
    title = payload.get("title", "")
    note_id = payload.get("id", "")
    family_kind = f"{payload.get('family', '')}/{payload.get('kind', '')}"
    permission = payload.get("mcp_permissions", "ALL")
    header = f"[bold]{title}[/bold]\n[dim]{note_id}  •  {family_kind}  •  mcp={permission}[/dim]"
    if mode.tty:
        _console.print(Panel(header, title=payload.get("filename", ""), expand=False))
        _console.print(payload.get("body", ""))
        backlinks = payload.get("backlinks")
        if backlinks:
            bl_table = Table(title="Backlinks", show_header=False)
            for bl in backlinks:
                bl_table.add_row(bl.get("title", ""), f"[dim]{bl.get('family', '')}[/dim]")
            _console.print(bl_table)
    else:
        sys.stdout.write(f"# {payload.get('title', '')}\n")
        sys.stdout.write(f"id: {payload.get('id', '')}\n\n")
        sys.stdout.write(payload.get("body", ""))
        sys.stdout.write("\n")


def render_summary_list(payload: dict[str, Any], *, mode: OutputMode) -> None:
    if mode.json:
        emit_json(payload)
        return
    notes = payload.get("notes", [])
    total = payload.get("total", len(notes))
    if not notes:
        _console.print("No notes.")
        return
    if mode.tty:
        table = Table(title=f"{len(notes)} / {total} note(s)", show_lines=False)
        table.add_column("P", justify="center")
        table.add_column("Family", style="cyan")
        table.add_column("Kind", style="magenta")
        table.add_column("Filename", style="bold")
        table.add_column("Updated", style="dim")
        for note in notes:
            table.add_row(
                _permission_badge(note.get("mcp_permissions")),
                note.get("family", ""),
                note.get("kind", ""),
                note.get("filename", ""),
                note.get("updated_at", "")[:19],
            )
        _console.print(table)
    else:
        for note in notes:
            sys.stdout.write(f"{note.get('id', '')}\t{note.get('filename', '')}\n")


def render_backlinks(payload: dict[str, Any], *, mode: OutputMode) -> None:
    if mode.json:
        emit_json(payload)
        return
    backlinks = payload.get("backlinks", [])
    if not backlinks:
        _console.print("No backlinks.")
        return
    if mode.tty:
        table = Table(title=f"{len(backlinks)} backlink(s)")
        table.add_column("Title", style="bold")
        table.add_column("Family/Kind", style="cyan")
        for bl in backlinks:
            table.add_row(
                bl.get("title", ""),
                f"{bl.get('family', '')}/{bl.get('kind', '')}",
            )
        _console.print(table)
    else:
        for bl in backlinks:
            sys.stdout.write(f"{bl.get('id', '')}\t{bl.get('title', '')}\n")


def render_counts(payload: dict[str, Any], key: str, *, mode: OutputMode) -> None:
    if mode.json:
        emit_json(payload)
        return
    items = payload.get(key, [])
    if not items:
        _console.print("No data.")
        return
    if mode.tty:
        table = Table(title=key.title())
        table.add_column(key[:-1] if key.endswith("s") else key, style="bold")
        table.add_column("Count", justify="right", style="dim")
        for item in items:
            table.add_row(str(item.get(key[:-1], item.get("tag", ""))), str(item.get("count", 0)))
        _console.print(table)
    else:
        for item in items:
            sys.stdout.write(f"{item.get('count', 0)}\t{item.get(key[:-1], item.get('tag', ''))}\n")


def render_status(payload: dict[str, Any], *, mode: OutputMode) -> None:
    if mode.json:
        emit_json(payload)
        return
    if mode.tty:
        table = Table(title="KastenManager status", show_header=False, box=None, padding=(0, 1))
        table.add_column(style="bold")
        table.add_column()
        for key, value in payload.items():
            rendered = _style_status_value(key, value)
            table.add_row(key, rendered)
        _console.print(table)
    else:
        for key, value in payload.items():
            sys.stdout.write(f"{key}\t{value}\n")


def _style_status_value(key: str, value: Any) -> str:
    """Apply tasteful colouring to status-table values."""
    if value is None:
        return "[dim]—[/dim]"
    text = str(value)
    if key == "last_sync_at":
        return f"[green]{text}[/green]"
    if key in ("local_total", "last_remote_total") and isinstance(value, int) and value > 0:
        return f"[bold]{text}[/bold]"
    if key == "db_size_bytes" and isinstance(value, int):
        return _human_bytes(value)
    if key.startswith("api_token") and text and "*" in text:
        return f"[dim]{text}[/dim]"
    return text


def _human_bytes(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024:
            return f"{count:.1f} {unit}" if unit != "B" else f"{count} {unit}"
        count /= 1024
    return f"{count:.1f} TB"


def render_sync_result(payload: dict[str, Any], *, mode: OutputMode) -> None:
    if mode.json:
        emit_json(payload)
        return
    mode_label = payload.get("mode", "incremental")
    fetched = payload.get("fetched", 0)
    deleted = payload.get("deleted", 0)
    missing_refetched = payload.get("missing_refetched", 0)
    mismatched_refetched = payload.get("mismatched_refetched", 0)
    orphans_removed = payload.get("orphans_removed", 0)
    verified = payload.get("verified_hashes", False)
    remote_total = payload.get("remote_total") or 0
    scanned_remote_ids = payload.get("scanned_remote_ids") or 0
    local_total = payload.get("local_total") or 0
    restricted_placeholders = payload.get("restricted_placeholders", 0)
    elapsed = payload.get("elapsed_seconds", 0)

    # "In sync" means scanned == local AND scanned == remote_total AND nothing
    # repaired. Drift is measured against what the server's `total` field
    # claims (which is what the user sees) AND against what we could actually
    # walk (the ground truth for anything KastenManager can do).
    total_vs_scanned_drift = remote_total - scanned_remote_ids
    scanned_vs_local_drift = scanned_remote_ids - local_total
    in_sync = (
        total_vs_scanned_drift == 0
        and scanned_vs_local_drift == 0
        and fetched == 0
        and deleted == 0
        and restricted_placeholders == 0
        and missing_refetched == 0
        and mismatched_refetched == 0
        and orphans_removed == 0
    )

    header_colour = "green" if in_sync else ("yellow" if total_vs_scanned_drift else "cyan")
    _console.print(f"[{header_colour}]sync {mode_label} complete[/{header_colour}] · {elapsed}s")

    def _fmt(count: int, *, good: str = "green", zero: str = "dim") -> str:
        if count == 0:
            return f"[{zero}]{count}[/{zero}]"
        return f"[{good}]{count}[/{good}]"

    # ── Section 1: counts ───────────────────────────────────────────────
    counts = Table(
        title="Counts",
        title_justify="left",
        title_style="bold",
        show_header=False,
        box=None,
        padding=(0, 1),
    )
    counts.add_column(style="bold")
    counts.add_column(justify="right")
    counts.add_row(
        "Remote — server `total` field",
        f"[bold]{remote_total}[/bold]",
    )
    counts.add_row(
        "Remote — scanned by KastenManager",
        f"[bold]{scanned_remote_ids}[/bold]",
    )
    counts.add_row("Local store", f"[bold]{local_total}[/bold]")
    if total_vs_scanned_drift:
        counts.add_row(
            "  Δ server `total` − scanned",
            f"[red]{total_vs_scanned_drift:+d}[/red]",
        )
    if scanned_vs_local_drift:
        counts.add_row(
            "  Δ scanned − local",
            f"[red]{scanned_vs_local_drift:+d}[/red]",
        )
    _console.print(counts)

    # ── Section 2: what changed this run ───────────────────────────────
    changed = Table(
        title="Changes this run",
        title_justify="left",
        title_style="bold",
        show_header=False,
        box=None,
        padding=(0, 1),
    )
    changed.add_column(style="bold")
    changed.add_column(justify="right")
    changed.add_row("Fetched / updated bodies", _fmt(fetched, good="green"))
    changed.add_row(
        "Restricted placeholders added",
        _fmt(restricted_placeholders, good="magenta")
        + " [dim](LIST but not READ — body not fetchable)[/dim]",
    )
    changed.add_row("Deleted (remote gone)", _fmt(deleted, good="red"))
    _console.print(changed)

    # ── Section 3: mirror / index repair ────────────────────────────────
    repair = Table(
        title="Mirror repair",
        title_justify="left",
        title_style="bold",
        show_header=False,
        box=None,
        padding=(0, 1),
    )
    repair.add_column(style="bold")
    repair.add_column(justify="right")
    repair.add_row("Re-fetched (missing file)", _fmt(missing_refetched, good="yellow"))
    repair.add_row(
        "Re-fetched (hash drift)",
        _fmt(mismatched_refetched, good="yellow")
        + ("" if verified else " [dim](not checked — pass --verify)[/dim]"),
    )
    repair.add_row("Orphans removed", _fmt(orphans_removed, good="yellow"))
    repair.add_row(
        "Last sync timestamp",
        f"[dim]{payload.get('last_sync_at', '—')}[/dim]",
    )
    _console.print(repair)

    # ── Warnings / next steps ───────────────────────────────────────────
    if total_vs_scanned_drift:
        _console.print(
            f"[yellow]⚠[/yellow] The server's [bold]`total={remote_total}`[/bold] "
            f"disagrees with what pagination actually returned "
            f"([bold]{scanned_remote_ids}[/bold] IDs). "
            f"This usually means the server's count query sees rows that its "
            f"data query hides — possibly notes with [bold]`mcpPermissions = NONE`[/bold] "
            f"or [bold]trashed notes[/bold] leaking into the `total` count.\n"
            f"  Try [bold]`kasten sync --full`[/bold] to confirm the drift is "
            f"stable; if it is, it is a server-side bug in "
            f"[cyan]notes.vcoeur.com[/cyan] (see the conception document on `total` "
            f"audit)."
        )
    elif scanned_vs_local_drift > 0:
        _console.print(
            f"[yellow]⚠[/yellow] Scanned {scanned_remote_ids} remote IDs but local "
            f"store only has {local_total}. A subsequent sync should close this — "
            f"if it does not, run [bold]`kasten sync --full`[/bold]."
        )
    elif in_sync:
        _console.print(
            "[green]✓[/green] Local mirror matches the remote "
            f"({local_total} notes, {restricted_placeholders_total(payload)} restricted)."
        )


def restricted_placeholders_total(payload: dict[str, Any]) -> int:
    """Helper for display — returns the current restricted count from payload."""
    return payload.get("restricted_placeholders", 0)
