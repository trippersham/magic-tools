#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "make-magic-pipeline",
#     "httpx",
#     "typer",
#     "pydantic",
# ]
# [tool.uv.sources]
# make-magic-pipeline = { path = "../pipeline", editable = true }
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
Fetch Scryfall metadata for a list of MTG cards.

Input: JSON array of objects with at minimum a "name" key:
    [{"name": "Sol Ring", "id": "recXXX"}, ...]

Output: JSON array with Scryfall metadata merged in:
    [{"name": "Sol Ring", "id": "recXXX", "scryfall": { ... }}, ...]

The "id" field (Airtable record ID) is passed through unchanged.
Cards not found on Scryfall get "scryfall": null with an "error" key.

This script is a consumer of the package card resolver
(`pipeline.collection.resolver`) — it resolves each name to an enriched `Card`
(card_type / mana_cost / cmc / oracle_text / power / toughness / art_crop /
scryfall_uri / set_name / color_identity) and gets the live price separately
(price is volatile and not on the `Card` contract — served live via the
scryfall_cache façade). The OUTPUT JSON shape (the `scryfall` metadata block the
managing-inventory skill consumes) is unchanged.

Package access (house convention, same as `scripts/collection`): this PEP-723
script pins the local package via `[tool.uv.sources]` as an editable path dep, so
`uv run` resolves `pipeline` on invocation — the package never imports `scripts/`.

Usage:
    ./scryfall_batch.py input.json output.json
    uv run --script scryfall_batch.py input.json output.json

Maintenance:
    uvx ruff format scryfall_batch.py
    uvx ruff check scryfall_batch.py
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import typer
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))


class ScryfallMetadata(BaseModel):
    card_type: str = ""
    mana_cost: str = ""
    cmc: float = 0
    oracle_text: str = ""
    power: str | None = None
    toughness: str | None = None
    art_crop: str = ""
    scryfall_uri: str = ""
    price_usd: str | None = None
    set_name: str = ""
    color_identity: list[str] = []


class _Resolver(Protocol):
    """The card-resolver seam: name -> enriched `Card` (or None). Structurally the
    package `CardResolver` port; injected for tests."""

    def get_card(self, name: str) -> object | None: ...


def metadata_from_card(card: object, price_usd: str | None) -> ScryfallMetadata:
    """Project a resolved `contracts.Card` (+ the live price) into the metadata
    block, preserving the exact output shape the managing-inventory skill consumes.

    Enrichment comes from the card dim (`Card`); the price is passed in separately
    (volatile, served live, never a static `Card` field).
    """
    return ScryfallMetadata(
        card_type=card.type_line or "",
        mana_cost=card.mana_cost or "",
        cmc=card.mana_value if card.mana_value is not None else 0,
        oracle_text=card.oracle_text or "",
        power=card.power,
        toughness=card.toughness,
        art_crop=card.art_crop or "",
        scryfall_uri=card.scryfall_uri or "",
        price_usd=price_usd,
        set_name=card.set_name or "",
        color_identity=list(card.color_identity or []),
    )


def _process_entry(
    card_entry: dict,
    *,
    resolver: _Resolver,
    price_lookup: Callable[[str], str | None],
    echo: Callable[[str], None],
) -> dict:
    """Resolve + price ONE entry, merging the metadata into the record-keyed dict.

    A resolved name gets a full `scryfall` metadata block (enrichment from the
    resolver + a live price); an unresolved name gets `scryfall: null` + an
    `error`. The Airtable record `id` is passed through unchanged. Price is looked
    up ONLY for a resolved card.
    """
    name = card_entry["name"]
    card = resolver.get_card(name)
    if card is not None:
        price_usd = price_lookup(name)
        card_entry["scryfall"] = metadata_from_card(card, price_usd).model_dump()
    else:
        card_entry["scryfall"] = None
        card_entry["error"] = f"Not found on Scryfall: {name}"
        echo("  WARNING: not found")
    return card_entry


def run_batch(
    cards: list[dict],
    *,
    resolver: _Resolver,
    price_lookup: Callable[[str], str | None],
    echo: Callable[[str], None] = lambda _msg: None,
) -> list[dict]:
    """Resolve + price every entry, merging metadata into each record-keyed dict.

    Thin loop over `_process_entry`; the resolver is the enrichment source and the
    price is looked up separately (live). Output shape matches the prior projection.
    """
    results: list[dict] = []
    for i, card_entry in enumerate(cards):
        echo(f"[{i + 1}/{len(cards)}] {card_entry['name']}")
        results.append(
            _process_entry(
                card_entry, resolver=resolver, price_lookup=price_lookup, echo=echo
            )
        )
    return results


def main(
    input_path: Path = typer.Argument(
        ..., help="JSON file with card names and optional record IDs"
    ),
    output_path: Path = typer.Argument(
        ..., help="Output JSON file with Scryfall metadata merged in"
    ),
) -> None:
    """Fetch Scryfall metadata for a batch of MTG cards (resolver + live price)."""
    from pipeline.collection.resolver import default_card_resolver
    from scryfall_cache import ScryfallCache

    cards = json.loads(input_path.read_text())
    resolver = default_card_resolver()
    cache = ScryfallCache()

    results: list[dict] = []
    for i, card_entry in enumerate(cards):
        typer.echo(f"[{i + 1}/{len(cards)}] {card_entry['name']}")
        results.append(
            _process_entry(
                card_entry,
                resolver=resolver,
                price_lookup=cache.get_price_usd,
                echo=typer.echo,
            )
        )
        # Checkpoint every 50 records (preserves prior partial-flush behavior).
        if len(results) % 50 == 0:
            output_path.write_text(json.dumps(results, indent=2))

    output_path.write_text(json.dumps(results, indent=2))

    found = sum(1 for r in results if r["scryfall"] is not None)
    typer.echo(f"\nDone: {found}/{len(results)} cards fetched successfully")
    typer.echo(f"Output: {output_path}")

    stats = cache.cache_stats()
    typer.echo(f"Cache: {stats['session_hits']} hits, {stats['session_misses']} misses")
    cache.close()


if __name__ == "__main__":
    typer.run(main)
