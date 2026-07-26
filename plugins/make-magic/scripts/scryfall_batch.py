#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "typer",
#     "pydantic",
# ]
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

Uses scryfall_cache.py for all Scryfall lookups (session-scoped caching).

Usage:
    ./scryfall_batch.py input.json output.json
    uv run --script scryfall_batch.py input.json output.json

Maintenance:
    uv add --script scryfall_batch.py 'package-name'
    uvx ruff format scryfall_batch.py
    uvx ruff check scryfall_batch.py
"""

import json
import sys
from pathlib import Path

import typer
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from scryfall_cache import ScryfallCache


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


def _get_face_field(card: dict, field: str, default: str = "") -> str:
    """Get a field from top-level or first card face (for double-faced cards)."""
    value = card.get(field, default)
    if not value and card.get("card_faces"):
        value = card["card_faces"][0].get(field, default)
    return value


def extract_metadata(card: dict) -> ScryfallMetadata:
    """Extract inventory-relevant fields from a Scryfall card object."""
    image_uris = card.get("image_uris") or {}
    if not image_uris and card.get("card_faces"):
        image_uris = card["card_faces"][0].get("image_uris", {})

    oracle_text = card.get("oracle_text", "")
    if not oracle_text and card.get("card_faces"):
        oracle_text = " // ".join(f.get("oracle_text", "") for f in card["card_faces"])

    return ScryfallMetadata(
        card_type=card.get("type_line", ""),
        mana_cost=_get_face_field(card, "mana_cost"),
        cmc=card.get("cmc", 0),
        oracle_text=oracle_text,
        power=card.get("power") or _get_face_field(card, "power") or None,
        toughness=card.get("toughness") or _get_face_field(card, "toughness") or None,
        art_crop=image_uris.get("art_crop", ""),
        scryfall_uri=card.get("scryfall_uri", ""),
        price_usd=card.get("prices", {}).get("usd"),
        set_name=card.get("set_name", ""),
        color_identity=card.get("color_identity", []),
    )


def main(
    input_path: Path = typer.Argument(
        ..., help="JSON file with card names and optional record IDs"
    ),
    output_path: Path = typer.Argument(
        ..., help="Output JSON file with Scryfall metadata merged in"
    ),
) -> None:
    """Fetch Scryfall metadata for a batch of MTG cards."""
    cards = json.loads(input_path.read_text())
    results = []
    cache = ScryfallCache()

    for i, card_entry in enumerate(cards):
        name = card_entry["name"]
        typer.echo(f"[{i + 1}/{len(cards)}] {name}")

        data = cache.get_card(name)

        if data:
            card_entry["scryfall"] = extract_metadata(data).model_dump()
        else:
            card_entry["scryfall"] = None
            card_entry["error"] = f"Not found on Scryfall: {name}"
            typer.echo("  WARNING: not found")

        results.append(card_entry)

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
