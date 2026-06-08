#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "beautifulsoup4",
#     "httpx",
#     "lxml",
#     "pydantic",
#     "pydantic-settings",
#     "rich",
#     "typer",
# ]
# [tool.uv]
# exclude-newer = "2026-06-04T00:00:00Z"
# ///
"""
Multi-source MTG spoiler sync: MythicSpoiler (fast) + Scryfall (authoritative).

Polls MythicSpoiler for early card detection, then reconciles with Scryfall's
full metadata. Tracks state in a local SQLite database for diffing across runs.

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
    SPOILER_DB_PATH         Database path override

Maintenance:
    uv add --script spoiler_sync.py 'package-name'
    uvx ruff format spoiler_sync.py
    uvx ruff check spoiler_sync.py

References:
    - PEP 723: https://peps.python.org/pep-0723/
    - uv scripts: https://docs.astral.sh/uv/guides/scripts/
    - Scryfall API: https://scryfall.com/docs/api
    - MythicSpoiler: https://mythicspoiler.com
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from rich.console import Console
from rich.table import Table

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from scryfall_cache import ScryfallCache  # noqa: E402

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    db_path: Path = Path("spoiler_cache.db")
    scryfall_base: str = "https://api.scryfall.com"
    mythicspoiler_base: str = "https://mythicspoiler.com"
    set_codes: list[str] = Field(default_factory=list)
    user_agent: str = "mtg-deck-designer-spoiler-sync/1.0"
    scryfall_delay: float = 0.5
    mythicspoiler_delay: float = 1.5

    model_config = {"env_prefix": "SPOILER_"}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Source(str, Enum):
    MYTHICSPOILER = "mythicspoiler"
    SCRYFALL = "scryfall"


class MythicSpoilerCard(BaseModel):
    slug: str
    image_url: str
    set_code: str
    detail_url: str


class ScryfallCard(BaseModel):
    scryfall_id: str = Field(alias="id")
    name: str
    set_code: str = Field(alias="set")
    collector_number: str
    mana_cost: str | None = None
    type_line: str | None = None
    oracle_text: str | None = None
    power: str | None = None
    toughness: str | None = None
    rarity: str | None = None
    layout: str | None = None
    image_url: str | None = None
    preview_source: str | None = None
    previewed_at: str | None = None

    model_config = {"populate_by_name": True}

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ScryfallCard:
        image_url = None
        if uris := data.get("image_uris"):
            image_url = uris.get("normal") or uris.get("large")
        elif faces := data.get("card_faces"):
            if face_uris := faces[0].get("image_uris"):
                image_url = face_uris.get("normal") or face_uris.get("large")

        preview = data.get("preview") or {}
        return cls(
            id=data["id"],
            name=data["name"],
            set=data["set"],
            collector_number=data["collector_number"],
            mana_cost=data.get("mana_cost"),
            type_line=data.get("type_line"),
            oracle_text=data.get("oracle_text"),
            power=data.get("power"),
            toughness=data.get("toughness"),
            rarity=data.get("rarity"),
            layout=data.get("layout"),
            image_url=image_url,
            preview_source=preview.get("source"),
            previewed_at=preview.get("previewed_at"),
        )


class SyncStats(BaseModel):
    new_from_mythicspoiler: list[str] = []
    new_from_scryfall: list[str] = []
    backfilled: list[str] = []
    total_mythicspoiler: int = 0
    total_scryfall: int = 0
    total_stored: int = 0


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    scryfall_id TEXT UNIQUE,
    collector_number TEXT,
    set_code TEXT NOT NULL,
    mana_cost TEXT,
    type_line TEXT,
    oracle_text TEXT,
    power TEXT,
    toughness TEXT,
    rarity TEXT,
    layout TEXT,
    image_url TEXT,
    mythicspoiler_slug TEXT,
    mythicspoiler_image_url TEXT,
    preview_source TEXT,
    previewed_at TEXT,
    first_seen_source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    confirmed_by_scryfall INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_mythicspoiler
    ON cards(set_code, mythicspoiler_slug)
    WHERE mythicspoiler_slug IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cards_set ON cards(set_code);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_known_mythicspoiler_slugs(conn: sqlite3.Connection, set_code: str) -> set[str]:
    rows = conn.execute(
        "SELECT mythicspoiler_slug FROM cards WHERE set_code = ? AND mythicspoiler_slug IS NOT NULL",
        (set_code,),
    ).fetchall()
    return {r["mythicspoiler_slug"] for r in rows}


def get_known_scryfall_ids(conn: sqlite3.Connection, set_code: str) -> set[str]:
    rows = conn.execute(
        "SELECT scryfall_id FROM cards WHERE set_code = ? AND scryfall_id IS NOT NULL",
        (set_code,),
    ).fetchall()
    return {r["scryfall_id"] for r in rows}


def get_unconfirmed_cards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cards WHERE confirmed_by_scryfall = 0"
    ).fetchall()


def upsert_mythicspoiler_card(
    conn: sqlite3.Connection, card: MythicSpoilerCard
) -> bool:
    """Insert a MythicSpoiler-discovered card. Returns True if new."""
    now = _now()
    existing = conn.execute(
        "SELECT id FROM cards WHERE set_code = ? AND mythicspoiler_slug = ?",
        (card.set_code, card.slug),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE cards SET mythicspoiler_image_url = ?, updated_at = ? WHERE id = ?",
            (card.image_url, now, existing["id"]),
        )
        conn.commit()
        return False

    conn.execute(
        """INSERT INTO cards (
            set_code, mythicspoiler_slug, mythicspoiler_image_url,
            first_seen_source, first_seen_at, confirmed_by_scryfall,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            card.set_code,
            card.slug,
            card.image_url,
            Source.MYTHICSPOILER.value,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return True


def upsert_scryfall_card(
    conn: sqlite3.Connection, card: ScryfallCard
) -> tuple[bool, bool]:
    """Insert or update from Scryfall. Returns (is_new, was_backfill)."""
    now = _now()
    existing = conn.execute(
        "SELECT id, confirmed_by_scryfall FROM cards WHERE scryfall_id = ?",
        (card.scryfall_id,),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE cards SET
                name = ?, collector_number = ?, mana_cost = ?, type_line = ?,
                oracle_text = ?, power = ?, toughness = ?, rarity = ?, layout = ?,
                image_url = ?, preview_source = ?, previewed_at = ?,
                confirmed_by_scryfall = 1, updated_at = ?
            WHERE id = ?""",
            (
                card.name,
                card.collector_number,
                card.mana_cost,
                card.type_line,
                card.oracle_text,
                card.power,
                card.toughness,
                card.rarity,
                card.layout,
                card.image_url,
                card.preview_source,
                card.previewed_at,
                now,
                existing["id"],
            ),
        )
        conn.commit()
        was_backfill = not existing["confirmed_by_scryfall"]
        return False, was_backfill

    conn.execute(
        """INSERT INTO cards (
            name, scryfall_id, collector_number, set_code, mana_cost, type_line,
            oracle_text, power, toughness, rarity, layout, image_url,
            preview_source, previewed_at,
            first_seen_source, first_seen_at, confirmed_by_scryfall,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            card.name,
            card.scryfall_id,
            card.collector_number,
            card.set_code,
            card.mana_cost,
            card.type_line,
            card.oracle_text,
            card.power,
            card.toughness,
            card.rarity,
            card.layout,
            card.image_url,
            card.preview_source,
            card.previewed_at,
            Source.SCRYFALL.value,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return True, False


def link_mythicspoiler_to_scryfall(
    conn: sqlite3.Connection, mythicspoiler_row_id: int, card: ScryfallCard
) -> None:
    """Link an unconfirmed MythicSpoiler row to its Scryfall match."""
    now = _now()
    ms_row = conn.execute(
        "SELECT mythicspoiler_slug, mythicspoiler_image_url, first_seen_at FROM cards WHERE id = ?",
        (mythicspoiler_row_id,),
    ).fetchone()

    existing_sf = conn.execute(
        "SELECT id FROM cards WHERE scryfall_id = ?", (card.scryfall_id,)
    ).fetchone()

    if existing_sf:
        slug = ms_row["mythicspoiler_slug"]
        img = ms_row["mythicspoiler_image_url"]
        conn.execute("DELETE FROM cards WHERE id = ?", (mythicspoiler_row_id,))
        conn.execute(
            """UPDATE cards SET
                mythicspoiler_slug = ?, mythicspoiler_image_url = ?, updated_at = ?
            WHERE id = ?""",
            (slug, img, now, existing_sf["id"]),
        )
    else:
        conn.execute(
            """UPDATE cards SET
                name = ?, scryfall_id = ?, collector_number = ?, mana_cost = ?,
                type_line = ?, oracle_text = ?, power = ?, toughness = ?, rarity = ?,
                layout = ?, image_url = ?, preview_source = ?, previewed_at = ?,
                confirmed_by_scryfall = 1, updated_at = ?
            WHERE id = ?""",
            (
                card.name,
                card.scryfall_id,
                card.collector_number,
                card.mana_cost,
                card.type_line,
                card.oracle_text,
                card.power,
                card.toughness,
                card.rarity,
                card.layout,
                card.image_url,
                card.preview_source,
                card.previewed_at,
                now,
                mythicspoiler_row_id,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# MythicSpoiler Scraper
# ---------------------------------------------------------------------------


def slugify_for_match(slug: str) -> str:
    """Normalize a MythicSpoiler slug for fuzzy comparison."""
    clean = re.sub(r"[^a-z0-9]", "", slug.lower())
    return clean


def scrape_mythicspoiler_set(
    client: httpx.Client, base_url: str, set_code: str
) -> list[MythicSpoilerCard]:
    """Scrape the set index page for all card entries."""
    url = f"{base_url}/{set_code}/index.html"
    resp = client.get(url)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    cards: list[MythicSpoilerCard] = []

    for link in soup.find_all("a", class_="card"):
        href = link.get("href", "")
        img = link.find("img")
        if not href or not img:
            continue

        slug = href.replace("cards/", "").replace(".html", "")
        img_src = img.get("src", "")
        image_url = (
            f"{base_url}/{set_code}/{img_src}"
            if not img_src.startswith("http")
            else img_src
        )
        detail_url = (
            f"{base_url}/{set_code}/{href}" if not href.startswith("http") else href
        )

        cards.append(
            MythicSpoilerCard(
                slug=slug, image_url=image_url, set_code=set_code, detail_url=detail_url
            )
        )

    return cards


def scrape_mythicspoiler_new(
    client: httpx.Client, base_url: str, target_sets: set[str]
) -> list[MythicSpoilerCard]:
    """Scrape /newspoilers.html for recently added cards in target sets."""
    url = f"{base_url}/newspoilers.html"
    resp = client.get(url)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    cards: list[MythicSpoilerCard] = []
    current_set: str | None = None

    for div in soup.find_all("div", class_=["grid-span", "grid-card"]):
        classes = div.get("class", [])

        if "grid-span" in classes:
            text = div.get_text(" ", strip=True).lower()
            current_set = None
            for sc in target_sets:
                if sc in text:
                    current_set = sc
                    break
            continue

        if "grid-card" in classes and current_set:
            link = div.find("a", href=re.compile(r"/cards/"))
            if not link:
                continue
            href = link.get("href", "")
            img = link.find("img")
            if not img:
                continue

            match = re.search(rf"({current_set})/cards/(.+?)\.html", href)
            if not match:
                continue

            slug = match.group(2)
            img_src = img.get("src", "")
            image_url = (
                f"{base_url}/{img_src}" if not img_src.startswith("http") else img_src
            )

            cards.append(
                MythicSpoilerCard(
                    slug=slug,
                    image_url=image_url,
                    set_code=current_set,
                    detail_url=f"{base_url}/{href}"
                    if not href.startswith("http")
                    else href,
                )
            )

    return cards


# ---------------------------------------------------------------------------
# Scryfall Client
# ---------------------------------------------------------------------------


def _fuzzy_match_via_cache(
    cache: ScryfallCache,
    base_url: str,
    name_guess: str,
    set_code: str,
) -> ScryfallCard | None:
    """Try to fuzzy-match a card name against Scryfall via the shared cache layer."""
    try:
        data = cache._fetch(
            f"{base_url}/cards/named",
            params={"fuzzy": name_guess, "set": set_code},
        )
        return ScryfallCard.from_api(data)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


# ---------------------------------------------------------------------------
# Sync Engine
# ---------------------------------------------------------------------------


def slug_to_name_guess(slug: str) -> str:
    """Convert a MythicSpoiler slug to a rough card name for fuzzy search."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", slug)
    spaced = re.sub(r"(\d+)", r" \1 ", spaced)
    return spaced.strip()


def run_sync(
    settings: Settings, console: Console, target_sets: list[str] | None = None
) -> SyncStats:
    set_codes = [s.lower() for s in (target_sets or settings.set_codes)]
    stats = SyncStats()
    conn = init_db(settings.db_path)

    client = httpx.Client(
        headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        timeout=30.0,
        follow_redirects=True,
    )
    cache = ScryfallCache()

    # --- Phase 1: MythicSpoiler (fast, partial data) ---
    console.print("\n[bold cyan]Phase 1:[/] Scraping MythicSpoiler…")

    for sc in set_codes:
        known_slugs = get_known_mythicspoiler_slugs(conn, sc)
        try:
            ms_cards = scrape_mythicspoiler_set(client, settings.mythicspoiler_base, sc)
            stats.total_mythicspoiler += len(ms_cards)
            time.sleep(settings.mythicspoiler_delay)

            new_from_ms = scrape_mythicspoiler_new(
                client, settings.mythicspoiler_base, {sc}
            )
            seen_slugs = {c.slug for c in ms_cards}
            for c in new_from_ms:
                if c.slug not in seen_slugs:
                    ms_cards.append(c)
                    seen_slugs.add(c.slug)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                console.print(f"  [dim]{sc.upper()}: not on MythicSpoiler (404)[/]")
            else:
                console.print(
                    f"  [yellow]Warning:[/] MythicSpoiler failed for {sc}: {e}"
                )
            continue
        except httpx.HTTPError as e:
            console.print(f"  [yellow]Warning:[/] MythicSpoiler failed for {sc}: {e}")
            continue

        new_count = 0
        for card in ms_cards:
            if card.slug not in known_slugs:
                if upsert_mythicspoiler_card(conn, card):
                    name_guess = slug_to_name_guess(card.slug)
                    stats.new_from_mythicspoiler.append(f"{name_guess} ({sc})")
                    new_count += 1

        console.print(
            f"  [green]{sc.upper()}:[/] {len(ms_cards)} cards scraped, {new_count} new"
        )

    # --- Phase 2: Scryfall (authoritative, full data) via shared cache ---
    console.print("\n[bold cyan]Phase 2:[/] Querying Scryfall API (via cache)…")

    sf_cards: list[ScryfallCard] = []
    for sc in set_codes:
        try:
            raw = cache.get_set(sc)
            set_cards = [ScryfallCard.from_api(c) for c in raw]
            sf_cards.extend(set_cards)
        except Exception as e:
            console.print(f"  [yellow]Warning:[/] Scryfall API failed for {sc}: {e}")
            continue

        new_count = 0
        for card in set_cards:
            is_new, was_backfill = upsert_scryfall_card(conn, card)
            if is_new:
                stats.new_from_scryfall.append(f"{card.name} ({sc})")
                new_count += 1
            if was_backfill:
                stats.backfilled.append(card.name)

        console.print(
            f"  [green]{sc.upper()}:[/] {len(set_cards)} cards from API, {new_count} new"
        )

    stats.total_scryfall = len(sf_cards)

    # --- Phase 3: Reconcile unconfirmed MythicSpoiler cards ---
    console.print("\n[bold cyan]Phase 3:[/] Reconciling unconfirmed cards…")

    unconfirmed = get_unconfirmed_cards(conn)
    reconciled = 0
    for row in unconfirmed:
        slug = row["mythicspoiler_slug"]
        if not slug:
            continue
        name_guess = slug_to_name_guess(slug)
        match = _fuzzy_match_via_cache(
            cache,
            settings.scryfall_base,
            name_guess,
            row["set_code"],
        )
        if match:
            link_mythicspoiler_to_scryfall(conn, row["id"], match)
            stats.backfilled.append(match.name)
            reconciled += 1

    console.print(f"  Reconciled {reconciled}/{len(unconfirmed)} unconfirmed cards")

    # --- Summary ---
    stats.total_stored = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    set_meta(conn, "last_sync", _now())
    conn.close()
    client.close()

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(
    help="MTG spoiler sync: MythicSpoiler + Scryfall", no_args_is_help=True
)
console = Console()


@app.command()
def sync(
    set_codes: Annotated[
        list[str] | None,
        typer.Argument(help="Set code(s) to sync (e.g., msh msc blb)"),
    ] = None,
    db: Annotated[Path, typer.Option("--db", help="Database path")] = Path(
        "spoiler_cache.db"
    ),
) -> None:
    """Run a full sync cycle: MythicSpoiler -> Scryfall -> reconcile."""
    settings = Settings(db_path=db)

    # Resolve set codes: CLI args > env var > error
    target = (
        set_codes if set_codes else (settings.set_codes if settings.set_codes else None)
    )
    if not target:
        console.print(
            "[red]Error:[/] No set codes provided. "
            "Pass set codes as arguments or set SPOILER_SET_CODES env var."
        )
        raise typer.Exit(1)

    console.print("[bold]MTG Spoiler Sync[/]")
    console.print(f"Sets: {', '.join(target).upper()}")
    console.print(f"DB: {settings.db_path}")

    stats = run_sync(settings, console, target)

    console.print("\n[bold]─── Summary ───[/]")
    if stats.new_from_mythicspoiler:
        console.print(
            f"[green]New (MythicSpoiler first):[/] {len(stats.new_from_mythicspoiler)}"
        )
        for name in stats.new_from_mythicspoiler:
            console.print(f"  + {name}")
    if stats.new_from_scryfall:
        console.print(f"[green]New (Scryfall first):[/] {len(stats.new_from_scryfall)}")
        for name in stats.new_from_scryfall:
            console.print(f"  + {name}")
    if stats.backfilled:
        console.print(
            f"[blue]Backfilled with Scryfall data:[/] {len(stats.backfilled)}"
        )
    console.print(f"[bold]Total cards in DB:[/] {stats.total_stored}")


@app.command()
def status(
    set_code: Annotated[
        str | None, typer.Option("--set", "-s", help="Filter by set code")
    ] = None,
    db: Annotated[Path, typer.Option("--db", help="Database path")] = Path(
        "spoiler_cache.db"
    ),
) -> None:
    """Show current sync state and card counts."""
    if not db.exists():
        console.print("[yellow]No database found. Run 'sync' first.[/]")
        raise typer.Exit(1)

    conn = init_db(db)
    last_sync = get_meta(conn, "last_sync") or "never"

    table = Table(title="Spoiler Sync Status")
    table.add_column("Set", style="cyan")
    table.add_column("Total", justify="right")
    table.add_column("Confirmed", justify="right", style="green")
    table.add_column("Unconfirmed", justify="right", style="yellow")

    if set_code:
        sets = [(set_code.lower(),)]
    else:
        sets = conn.execute(
            "SELECT DISTINCT set_code FROM cards ORDER BY set_code"
        ).fetchall()
    for row in sets:
        sc = row[0] if isinstance(row, tuple) else row["set_code"]
        total = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE set_code = ?", (sc,)
        ).fetchone()[0]
        confirmed = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE set_code = ? AND confirmed_by_scryfall = 1",
            (sc,),
        ).fetchone()[0]
        table.add_row(sc.upper(), str(total), str(confirmed), str(total - confirmed))

    console.print(table)
    console.print(f"\nLast sync: {last_sync}")
    conn.close()


@app.command(name="list")
def list_cards(
    set_code: Annotated[
        str | None, typer.Option("--set", "-s", help="Filter by set")
    ] = None,
    new: Annotated[
        bool, typer.Option("--new", help="Only show unconfirmed cards")
    ] = False,
    db: Annotated[Path, typer.Option("--db", help="Database path")] = Path(
        "spoiler_cache.db"
    ),
) -> None:
    """List all known spoiler cards."""
    if not db.exists():
        console.print("[yellow]No database found. Run 'sync' first.[/]")
        raise typer.Exit(1)

    conn = init_db(db)

    query = "SELECT * FROM cards WHERE 1=1"
    params: list[str] = []
    if set_code:
        query += " AND set_code = ?"
        params.append(set_code.lower())
    if new:
        query += " AND confirmed_by_scryfall = 0"
    query += " ORDER BY set_code, collector_number, mythicspoiler_slug"

    rows = conn.execute(query, params).fetchall()

    table = Table(title=f"Spoiler Cards ({len(rows)})")
    table.add_column("Set", style="cyan", width=4)
    table.add_column("#", justify="right", width=4)
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Source", width=8)
    table.add_column("Confirmed", justify="center", width=4)

    for row in rows:
        name = row["name"] or slug_to_name_guess(row["mythicspoiler_slug"] or "???")
        table.add_row(
            row["set_code"].upper(),
            row["collector_number"] or "—",
            name,
            row["type_line"] or "—",
            row["first_seen_source"],
            "[green]Y[/]" if row["confirmed_by_scryfall"] else "[yellow]N[/]",
        )

    console.print(table)
    conn.close()


if __name__ == "__main__":
    app()
