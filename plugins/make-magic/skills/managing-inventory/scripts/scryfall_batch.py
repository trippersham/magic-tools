#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "typer",
#     "pydantic",
# ]
# [tool.uv]
# exclude-newer = "2026-05-13T00:00:00Z"
# ///
"""
Fetch Scryfall metadata for a list of MTG cards.

Input: JSON array of objects with at minimum a "name" key:
    [{"name": "Sol Ring", "id": "recXXX"}, ...]

Output: JSON array with Scryfall metadata merged in:
    [{"name": "Sol Ring", "id": "recXXX", "scryfall": { ... }}, ...]

The "id" field (Airtable record ID) is passed through unchanged.
Cards not found on Scryfall get "scryfall": null with an "error" key.

Rate limiting: 120ms between requests, 65s cooldown on 429.

Usage:
    ./scryfall_batch.py input.json output.json
    uv run --script scryfall_batch.py input.json output.json

Maintenance:
    uv add --script scryfall_batch.py 'package-name'
    uvx ruff format scryfall_batch.py
    uvx ruff check scryfall_batch.py

References:
    - PEP 723 (Inline script metadata): https://peps.python.org/pep-0723/
    - uv scripts guide: https://docs.astral.sh/uv/guides/scripts/
    - Scryfall API: https://scryfall.com/docs/api
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import typer
from pydantic import BaseModel


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


HEADERS = {
    "User-Agent": "MTGInventoryManager/1.0",
    "Accept": "application/json",
}


def fetch_card(name: str) -> dict | None:
    """Fetch a single card from Scryfall by exact name."""
    encoded = urllib.parse.quote(name)
    url = f"https://api.scryfall.com/cards/named?exact={encoded}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise
        return None


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

    for i, card_entry in enumerate(cards):
        name = card_entry["name"]
        typer.echo(f"[{i + 1}/{len(cards)}] {name}")

        try:
            data = fetch_card(name)
        except urllib.error.HTTPError:
            typer.echo("  429 rate limited — cooling down 65s")
            time.sleep(65)
            data = fetch_card(name)

        if data:
            card_entry["scryfall"] = extract_metadata(data).model_dump()
        else:
            card_entry["scryfall"] = None
            card_entry["error"] = f"Not found on Scryfall: {name}"
            typer.echo("  WARNING: not found")

        results.append(card_entry)

        # Write progress incrementally every 50 cards
        if len(results) % 50 == 0:
            output_path.write_text(json.dumps(results, indent=2))

        time.sleep(0.12)

    output_path.write_text(json.dumps(results, indent=2))

    found = sum(1 for r in results if r["scryfall"] is not None)
    typer.echo(f"\nDone: {found}/{len(results)} cards fetched successfully")
    typer.echo(f"Output: {output_path}")


if __name__ == "__main__":
    typer.run(main)
