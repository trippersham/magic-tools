#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "typer",
#     "pydantic>=2.7",
# ]
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""The interim Scryfall-cache-backed `CardResolver` — lives at the SCRIPT EDGE.

The `CollectionStore` local adapter hydrates base-`Card` enrichment through an
injected `CardResolver`; the pipeline package deliberately does NOT import
`scripts/`. This module is that edge: it is allowed to import `scryfall_cache`
and maps a Scryfall card dict into a `contracts.Card`.

Interim per Open Question OQ1: until #5 (pipeline-backed card resolution) lands,
this Scryfall-cache resolver is the hydration source. Swapping in the #5 read
layer later is a one-line change at the wiring point (this class is the seam).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.contracts import Card

_HERE = Path(__file__).resolve().parent
_PIPELINE_ROOT = _HERE.parent / 'pipeline'


def _ensure_paths() -> None:
    """Put the pipeline package + this script dir on sys.path (idempotent)."""
    for p in (str(_PIPELINE_ROOT), str(_HERE)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _card_from_scryfall(data: dict) -> Card:
    """Map a Scryfall card dict into a `contracts.Card` (front face for DFCs)."""
    from pipeline.contracts import Card

    face = data
    if data.get('oracle_text') is None and data.get('card_faces'):
        face = data['card_faces'][0]
    return Card(
        name=data.get('name', ''),
        oracle_id=data.get('oracle_id'),
        mana_value=data.get('cmc'),
        mana_cost=data.get('mana_cost') or face.get('mana_cost'),
        type_line=data.get('type_line') or face.get('type_line'),
        colors=data.get('colors') or [],
        color_identity=data.get('color_identity') or [],
        produced_mana=data.get('produced_mana') or [],
        keywords=data.get('keywords') or [],
        oracle_text=face.get('oracle_text') or data.get('oracle_text'),
    )


class ScryfallCacheResolver:
    """A `CardResolver` backed by the session-scoped Scryfall cache.

    Structurally satisfies `pipeline.collection.CardResolver`. An UNRESOLVED name
    (404 / not found) returns None so the adapter reads it back name-only.
    """

    def __init__(self, cache: object | None = None) -> None:
        _ensure_paths()
        if cache is None:
            from scryfall_cache import ScryfallCache

            cache = ScryfallCache()
        self._cache = cache

    def get_card(self, name: str) -> Card | None:
        data = self._cache.get_card(name)  # type: ignore[attr-defined]
        if not data:
            return None
        return _card_from_scryfall(data)
