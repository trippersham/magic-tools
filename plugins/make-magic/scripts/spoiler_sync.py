#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "make-magic-pipeline",
#     "rich",
#     "typer",
# ]
# [tool.uv.sources]
# make-magic-pipeline = { path = "../pipeline", editable = true }
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
Multi-source MTG spoiler sync: MythicSpoiler (fast) + Scryfall (authoritative).

#5 migrated the spoiler tracker onto the pipeline lake — the bespoke
``spoiler_cache.db`` SQLite state machine is RETIRED (it was the last SQLite in
the plugin). This script is now a THIN FAÇADE over two pipeline components:

    - ``pipeline.sources.spoilers``   — scrape MythicSpoiler HTML -> ``raw/spoilers``.
    - ``pipeline.transforms.spoilers`` — reconcile slug <-> Scryfall oracle_id via
      the lake-backed card resolver -> ``normalized/spoilers`` (the ``Spoiler``
      contract). "New since last sync" derives from the lake (current snapshot vs.
      the prior normalized table), NOT a SQLite meta table.

The ``sync`` / ``status`` / ``list`` verbs AND their output shape are PRESERVED so
``chasing-cards`` keeps working unchanged.

Package access (house convention, same as ``scripts/collection`` / the P2
``scryfall_cache`` rewrite): this PEP-723 script pins the local package via
``[tool.uv.sources]`` as an editable path dep, so ``uv run`` resolves ``pipeline``
on invocation — the package never imports ``scripts/`` (coupling stays
one-directional).

Usage:
    ./spoiler_sync.py sync msh msc      # Sync multiple sets (positional args)
    ./spoiler_sync.py sync blb          # Sync a single set
    ./spoiler_sync.py status            # Show current sync state
    ./spoiler_sync.py status --set msh  # Show status for a specific set
    ./spoiler_sync.py list              # List all known cards
    ./spoiler_sync.py list --set msh    # List cards for a specific set
    ./spoiler_sync.py list --new        # List cards found since last sync

Environment:
    SPOILER_SET_CODES       Fallback set codes for 'sync' (comma-separated)
    MAKE_MAGIC_DATA_DIR     Lake location override (shared with the pipeline)

Maintenance:
    uvx ruff format spoiler_sync.py
    uvx ruff check spoiler_sync.py

References:
    - PEP 723: https://peps.python.org/pep-0723/
    - Scryfall API: https://scryfall.com/docs/api
    - MythicSpoiler: https://mythicspoiler.com
"""

from __future__ import annotations

import os
from typing import Annotated

import typer
from pipeline.sources import spoilers as source
from pipeline.transforms import spoilers as transform
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="MTG spoiler sync: MythicSpoiler + Scryfall", no_args_is_help=True
)
console = Console()


def _resolve_targets(set_codes: list[str] | None) -> list[str] | None:
    """Resolve target set codes: CLI args > SPOILER_SET_CODES env > None."""
    if set_codes:
        return set_codes
    env = os.getenv("SPOILER_SET_CODES", "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return None


@app.command()
def sync(
    set_codes: Annotated[
        list[str] | None,
        typer.Argument(help="Set code(s) to sync (e.g., msh msc blb)"),
    ] = None,
) -> None:
    """Run a full sync cycle: MythicSpoiler scrape -> Scryfall reconcile (via the lake)."""
    target = _resolve_targets(set_codes)
    if not target:
        console.print(
            "[red]Error:[/] No set codes provided. "
            "Pass set codes as arguments or set SPOILER_SET_CODES env var."
        )
        raise typer.Exit(1)

    console.print("[bold]MTG Spoiler Sync[/]")
    console.print(f"Sets: {', '.join(target).upper()}")

    # --- Phase 1: MythicSpoiler scrape -> raw/spoilers --- #
    console.print("\n[bold cyan]Phase 1:[/] Scraping MythicSpoiler…")
    source.sync(target)

    # --- Phase 2+3: reconcile via the card dim -> normalized/spoilers --- #
    console.print(
        "\n[bold cyan]Phase 2:[/] Reconciling with Scryfall (via the card dim)…"
    )
    _, new_slugs = transform.build()

    all_spoilers = transform.load_spoilers()
    confirmed = sum(1 for s in all_spoilers if s.confirmed)

    console.print("\n[bold]─── Summary ───[/]")
    if new_slugs:
        new_records = [s for s in all_spoilers if s.slug in set(new_slugs)]
        console.print(f"[green]New since last sync:[/] {len(new_slugs)}")
        for s in new_records:
            console.print(f"  + {s.name} ({s.set_code.upper()})")
    console.print(f"[blue]Confirmed by Scryfall:[/] {confirmed}")
    console.print(f"[bold]Total cards in lake:[/] {len(all_spoilers)}")


@app.command()
def status(
    set_code: Annotated[
        str | None, typer.Option("--set", "-s", help="Filter by set code")
    ] = None,
) -> None:
    """Show current sync state and card counts."""
    spoilers = transform.load_spoilers(set_code=set_code)
    if not spoilers and not transform.store.table_exists("normalized", "spoilers"):
        console.print("[yellow]No spoiler data found. Run 'sync' first.[/]")
        raise typer.Exit(1)

    table = Table(title="Spoiler Sync Status")
    table.add_column("Set", style="cyan")
    table.add_column("Total", justify="right")
    table.add_column("Confirmed", justify="right", style="green")
    table.add_column("Unconfirmed", justify="right", style="yellow")

    by_set: dict[str, list] = {}
    for s in spoilers:
        by_set.setdefault(s.set_code, []).append(s)
    for sc in sorted(by_set):
        rows = by_set[sc]
        total = len(rows)
        confirmed = sum(1 for r in rows if r.confirmed)
        table.add_row(sc.upper(), str(total), str(confirmed), str(total - confirmed))

    console.print(table)


@app.command(name="list")
def list_cards(
    set_code: Annotated[
        str | None, typer.Option("--set", "-s", help="Filter by set")
    ] = None,
    new: Annotated[
        bool, typer.Option("--new", help="Only show unconfirmed cards")
    ] = False,
) -> None:
    """List all known spoiler cards."""
    spoilers = transform.load_spoilers(set_code=set_code, only_new=new)
    if not spoilers and not transform.store.table_exists("normalized", "spoilers"):
        console.print("[yellow]No spoiler data found. Run 'sync' first.[/]")
        raise typer.Exit(1)

    table = Table(title=f"Spoiler Cards ({len(spoilers)})")
    table.add_column("Set", style="cyan", width=4)
    table.add_column("Name", style="bold")
    table.add_column("Source", width=12)
    table.add_column("Confirmed", justify="center", width=4)

    for s in spoilers:
        table.add_row(
            s.set_code.upper(),
            s.name,
            s.source,
            "[green]Y[/]" if s.confirmed else "[yellow]N[/]",
        )

    console.print(table)


if __name__ == "__main__":
    app()
