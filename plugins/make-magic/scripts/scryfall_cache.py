#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "typer",
# ]
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
Session-scoped Scryfall cache with SQLite backend.

Eliminates redundant Scryfall API calls across all plugin operations.
Cache is ephemeral — stored in $TMPDIR and dies with the session.

Usage (CLI):
    ./scryfall_cache.py get-card "Sol Ring"
    ./scryfall_cache.py get-set stx
    ./scryfall_cache.py search "t:creature c:red cmc:3"
    ./scryfall_cache.py cache-stats

Usage (import):
    from scryfall_cache import ScryfallCache
    cache = ScryfallCache()
    card = cache.get_card("Sol Ring")

Maintenance:
    uv add --script scryfall_cache.py 'package-name'
    uvx ruff format scryfall_cache.py
    uvx ruff check scryfall_cache.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
import typer

app = typer.Typer()

DB_PATH = Path(os.environ.get("TMPDIR", "/tmp")) / "make-magic-scryfall-cache.db"
SCRYFALL_BASE = "https://api.scryfall.com"
HEADERS = {"User-Agent": "make-magic-plugin/2.0"}
RATE_LIMIT_MS = 100


class ScryfallCache:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._client = httpx.Client(headers=HEADERS, timeout=30)
        self._last_request: float = 0
        self._hits = 0
        self._misses = 0

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS cards (
                name TEXT PRIMARY KEY,
                scryfall_id TEXT,
                data JSON NOT NULL,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sets (
                set_code TEXT PRIMARY KEY,
                card_count INTEGER,
                data JSON NOT NULL,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS searches (
                query TEXT PRIMARY KEY,
                data JSON NOT NULL,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request
        wait = (RATE_LIMIT_MS / 1000) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def _fetch(self, url: str, params: dict | None = None) -> dict:
        self._rate_limit()
        resp = self._client.get(url, params=params)
        if resp.status_code == 429:
            time.sleep(65)
            self._rate_limit()
            resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _fetch_paginated(self, url: str) -> list[dict]:
        all_items: list[dict] = []
        while url:
            data = self._fetch(url)
            all_items.extend(data.get("data", []))
            url = data.get("next_page") if data.get("has_more") else None
        return all_items

    def get_card(self, name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT data FROM cards WHERE name = ?", (name,)
        ).fetchone()
        if row:
            self._hits += 1
            return json.loads(row[0])

        self._misses += 1
        try:
            data = self._fetch(f"{SCRYFALL_BASE}/cards/named", params={"exact": name})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        self.conn.execute(
            "INSERT OR REPLACE INTO cards (name, scryfall_id, data) VALUES (?, ?, ?)",
            (name, data.get("id"), json.dumps(data)),
        )
        self.conn.commit()
        return data

    def get_set(self, code: str) -> list[dict]:
        row = self.conn.execute(
            "SELECT data FROM sets WHERE set_code = ?", (code,)
        ).fetchone()
        if row:
            self._hits += 1
            return json.loads(row[0])

        self._misses += 1
        cards = self._fetch_paginated(
            f"{SCRYFALL_BASE}/cards/search?q=set%3A{code}&order=spoiled"
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO sets (set_code, card_count, data) VALUES (?, ?, ?)",
            (code, len(cards), json.dumps(cards)),
        )
        self.conn.commit()
        return cards

    def search(self, query: str) -> list[dict]:
        row = self.conn.execute(
            "SELECT data FROM searches WHERE query = ?", (query,)
        ).fetchone()
        if row:
            self._hits += 1
            return json.loads(row[0])

        self._misses += 1
        cards = self._fetch_paginated(
            f"{SCRYFALL_BASE}/cards/search?q={query}"
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO searches (query, data) VALUES (?, ?)",
            (query, json.dumps(cards)),
        )
        self.conn.commit()
        return cards

    def get_price(self, name: str) -> float | None:
        card = self.get_card(name)
        if not card:
            return None
        usd = card.get("prices", {}).get("usd")
        return float(usd) if usd else None

    def cache_stats(self) -> dict:
        cards = self.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        sets = self.conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
        searches = self.conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "cached_cards": cards,
            "cached_sets": sets,
            "cached_searches": searches,
            "session_hits": self._hits,
            "session_misses": self._misses,
        }

    def close(self) -> None:
        self._client.close()
        self.conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────

_cache: ScryfallCache | None = None


def _get_cache() -> ScryfallCache:
    global _cache
    if _cache is None:
        _cache = ScryfallCache()
    return _cache


@app.command()
def get_card(name: str) -> None:
    """Fetch a card by exact name (cached)."""
    card = _get_cache().get_card(name)
    if card:
        typer.echo(json.dumps(card, indent=2))
    else:
        typer.echo(f"Not found: {name}", err=True)
        raise typer.Exit(1)


@app.command()
def get_set(code: str) -> None:
    """Fetch all cards in a set (cached)."""
    cards = _get_cache().get_set(code)
    typer.echo(json.dumps(cards, indent=2))


@app.command()
def search(query: str) -> None:
    """Run a Scryfall search query (cached)."""
    cards = _get_cache().search(query)
    typer.echo(json.dumps(cards, indent=2))


@app.command()
def cache_stats() -> None:
    """Show cache statistics."""
    stats = _get_cache().cache_stats()
    for k, v in stats.items():
        typer.echo(f"{k}: {v}")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
