"""Package-native default `CardResolver` — a Scryfall name -> `Card` lookup.

This is the DEFAULT hydration source for the local `CollectionStore`, so
`get_store()` returns a working store with **no injected resolver**: the CLI no
longer wires one in from the `scripts/` edge, and the package still never imports
`scripts/`. The `CardResolver` port stays the swap-point — tests inject a stub,
and **#5** (pipeline-backed card resolution) swaps this default's internals to a
DuckDB query over the `scryfall_bulk` card table with callers unchanged.

INTERIM (per OQ1): per-card Scryfall lookups with a small on-disk JSON cache under
the store data dir so a deck fact sheet doesn't refetch across runs. Fail-open —
an unresolved name (404 / network error) returns None, and the adapter reads the
card back name-only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

from pipeline.contracts import Card
from pipeline.store.paths import StorePaths

if TYPE_CHECKING:
    from pathlib import Path

_NAMED_URL = 'https://api.scryfall.com/cards/named'
_CACHE_FILENAME = 'scryfall_names.json'


def _card_from_scryfall(data: dict) -> Card:
    """Map a Scryfall card dict -> `contracts.Card` (front face for DFCs)."""
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


class ScryfallResolver:
    """Default `CardResolver`: resolve a card NAME -> `Card` via Scryfall, cached.

    Structurally satisfies `pipeline.collection.store.CardResolver`. Lookups are
    memoized in-process and persisted to a JSON cache so repeat runs don't refetch.
    A 404 or network failure returns None (fail-open) — the adapter then reads the
    card back name-only.
    """

    def __init__(self, *, cache_path: Path | None = None, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=30, headers={'User-Agent': 'make-magic-plugin/2.0'})
        self._cache_path = cache_path
        self._mem: dict[str, dict | None] = {}
        if cache_path is not None and cache_path.exists():
            try:
                self._mem = json.loads(cache_path.read_text())
            except (OSError, ValueError):
                self._mem = {}

    def get_card(self, name: str) -> Card | None:
        if name not in self._mem:
            self._mem[name] = self._fetch(name)
            self._flush()
        data = self._mem[name]
        return _card_from_scryfall(data) if data else None

    def _fetch(self, name: str) -> dict | None:
        try:
            resp = self._client.get(_NAMED_URL, params={'exact': name})
            if resp.status_code == 404:
                resp = self._client.get(_NAMED_URL, params={'fuzzy': name})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            return None  # fail-open: unresolved -> name-only card

    def _flush(self) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._mem))
        except OSError:
            pass


def default_card_resolver() -> ScryfallResolver:
    """The package default resolver, caching under the resolved store data dir.

    This is the swap-point for #5: replace the body to return a DuckDB-backed
    resolver over the `scryfall_bulk` card table — no caller changes.
    """
    paths = StorePaths.resolve()
    return ScryfallResolver(cache_path=paths.data_dir / _CACHE_FILENAME)
