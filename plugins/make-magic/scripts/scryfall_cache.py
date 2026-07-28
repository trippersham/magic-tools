#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "make-magic-pipeline",
#     "httpx",
#     "typer",
# ]
# [tool.uv.sources]
# make-magic-pipeline = { path = "../pipeline", editable = true }
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
Scryfall lookup façade over the pipeline's lake-backed card dim.

#5 retired the session-scoped SQLite cache: the DuckDB/Parquet lake is now the
SOLE durable store. `ScryfallCache` is a THIN façade that delegates card lookups
to the package (`pipeline.collection.resolver`) via `fetch_card_raw`, which does
a single live Scryfall fetch and LANDS the card durably into the lake — so no
SQLite, no `$TMPDIR/*.db`, and the lake is the one shared durable card dim.

Package access (house convention, same as `scripts/collection`): this is a
PEP-723 script that pins the local package via `[tool.uv.sources]` as an editable
path dep, so `uv run` resolves `pipeline` on invocation — the package never
imports `scripts/` (the coupling stays one-directional).

Output shape is UNCHANGED for the six consumers (`chasing-cards`,
`building-decks`, `deck_factsheet.py`, `card_tagger.py`, `scryfall_batch.py`,
`spoiler_sync.py`): `get_card`/`search`/`get_set` still return raw Scryfall
dicts. NOTE the FULL raw dict (`prices`, `card_faces`, `image_uris`, …) is a
superset of the projected `Card` the lake stores; the lake cannot reconstruct it,
so `get_card` fetches live-per-card (in-process memo, then lands durably into the
lake for `Card`-consumers). `search`/`get_set` likewise stay live (no lake/DuckDB
equivalent yet — a later phase migrates their consumers onto the projected
`Card`, at which point offline-first applies).

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
    uvx ruff format scryfall_cache.py
    uvx ruff check scryfall_cache.py
"""

from __future__ import annotations

import json
import time

import httpx
import typer
from pipeline.collection import resolver as resolver_mod

app = typer.Typer()

SCRYFALL_BASE = "https://api.scryfall.com"
HEADERS = {"User-Agent": "make-magic-plugin/2.0"}
RATE_LIMIT_MS = 100


class ScryfallCache:
    """Thin façade: `get_card` delegates to the package (`fetch_card_raw`, which
    lands the card durably in the lake); `search` / `get_set` stay live. All three
    use an in-process memo — the SQLite cache is retired.

    `get_card` returns the FULL raw Scryfall dict, preserving the shape every
    consumer relies on (a superset of the projected `Card` the lake stores).
    """

    def __init__(self) -> None:
        self._client = httpx.Client(headers=HEADERS, timeout=30)
        self._last_request: float = 0
        self._hits = 0
        self._misses = 0
        # In-process memo (replaces the SQLite tables) for the live-only surfaces.
        self._card_mem: dict[str, dict | None] = {}
        self._set_mem: dict[str, list[dict]] = {}
        self._search_mem: dict[str, list[dict]] = {}

    # -- live helpers (search/get_set have no lake equivalent yet) --------- #

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

    # -- card lookup: delegated to the lake-backed resolver --------------- #

    def get_card(self, name: str) -> dict | None:
        """Return the raw Scryfall card dict for `name`, or None.

        Delegates to `resolver.fetch_card_raw` (single live fetch — exact then
        fuzzy — that lands the card durably in the lake for `Card`-consumers).
        The full raw dict shape is preserved; an in-process memo avoids a repeat
        round-trip within one session.
        """
        if name in self._card_mem:
            self._hits += 1
            return self._card_mem[name]
        self._misses += 1
        try:
            data = resolver_mod.fetch_card_raw(name, client=self._client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self._card_mem[name] = None
                return None
            raise
        self._card_mem[name] = data
        return data

    def get_set(self, code: str) -> list[dict]:
        if code in self._set_mem:
            self._hits += 1
            return self._set_mem[code]
        self._misses += 1
        cards = self._fetch_paginated(
            f"{SCRYFALL_BASE}/cards/search?q=set%3A{code}&order=spoiled"
        )
        self._set_mem[code] = cards
        return cards

    def search(self, query: str) -> list[dict]:
        if query in self._search_mem:
            self._hits += 1
            return self._search_mem[query]
        self._misses += 1
        cards = self._fetch_paginated(f"{SCRYFALL_BASE}/cards/search?q={query}")
        self._search_mem[query] = cards
        return cards

    def get_price(self, name: str) -> float | None:
        card = self.get_card(name)
        if not card:
            return None
        usd = card.get("prices", {}).get("usd")
        return float(usd) if usd else None

    def cache_stats(self) -> dict:
        return {
            "backend": "pipeline-lake",
            "cached_cards": len(self._card_mem),
            "cached_sets": len(self._set_mem),
            "cached_searches": len(self._search_mem),
            "session_hits": self._hits,
            "session_misses": self._misses,
        }

    def close(self) -> None:
        self._client.close()


# ── CLI ───────────────────────────────────────────────────────────────────

_cache: ScryfallCache | None = None


def _get_cache() -> ScryfallCache:
    global _cache
    if _cache is None:
        _cache = ScryfallCache()
    return _cache


@app.command()
def get_card(name: str) -> None:
    """Fetch a card by exact name (lake-backed; live-fallback on a miss)."""
    card = _get_cache().get_card(name)
    if card:
        typer.echo(json.dumps(card, indent=2))
    else:
        typer.echo(f"Not found: {name}", err=True)
        raise typer.Exit(1)


@app.command()
def get_set(code: str) -> None:
    """Fetch all cards in a set (live)."""
    cards = _get_cache().get_set(code)
    typer.echo(json.dumps(cards, indent=2))


@app.command()
def search(query: str) -> None:
    """Run a Scryfall search query (live)."""
    cards = _get_cache().search(query)
    typer.echo(json.dumps(cards, indent=2))


@app.command()
def cache_stats() -> None:
    """Show session lookup statistics."""
    stats = _get_cache().cache_stats()
    for k, v in stats.items():
        typer.echo(f"{k}: {v}")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
