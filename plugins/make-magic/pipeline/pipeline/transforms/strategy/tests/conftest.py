"""Hermetic fixtures for the strategy-engine regression suite.

The golden decks are committed as fully-attached deck dicts (post lake
resolution + meta attach) and the combo registry is a pruned, detection-complete
slice of ``normalized/combo``, so the whole suite runs offline — no lake, no
DuckDB, no network — and is byte-deterministic.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / 'fixtures'
DECKS = FIX / 'decks'


@cache
def _combos() -> tuple[dict, ...]:
    return tuple(json.loads((FIX / 'combos.json').read_text()))


@cache
def _deck(slug: str) -> dict:
    return json.loads((DECKS / f'{slug}.json').read_text())


@pytest.fixture
def combos() -> list[dict]:
    """The pruned combo registry (list of runtime combo dicts)."""
    return [dict(c) for c in _combos()]


@pytest.fixture
def load_deck():
    """Return a loader ``load_deck(slug)`` for a committed golden deck fixture."""
    def _load(slug: str) -> dict:
        return json.loads((DECKS / f'{slug}.json').read_text())

    return _load


#: slug -> human label, for the golden table.
GOLDEN_SLUGS = {
    'owned__sokka': 'Sokka',
    'owned__ozai': 'Ozai',
    'owned__world-reclaimer': 'World Reclaimer',
    'owned__the-fantastic-four': 'The Fantastic Four',
    'owned__spiders-play-with-their-food': 'Spiders Play with their Food',
    'owned__welcoming-vampire': 'Welcoming Vampire',
    'gauntlet__tuvasa-the-sunlit': 'Tuvasa the Sunlit',
    'gauntlet__grandlarceny_2024': 'GrandLarceny_2024',
    'gauntlet__kess-dissident-mage': 'Kess, Dissident Mage',
    'gauntlet__urza-lord-high-artificer': 'Urza, Lord High Artificer',
    'gauntlet__krrik-son-of-yawgmoth': "K'rrik, Son of Yawgmoth",
    'gauntlet__baral-chief-of-compliance': 'Baral, Chief of Compliance',
}
