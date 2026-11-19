"""Hermetic fixtures for the strategy-engine regression suite.

The golden decks are committed as fully-attached deck dicts (post lake
resolution + meta attach) and the combo registry is a pruned, detection-complete
slice of ``normalized/combo``, so the whole suite runs offline — no lake, no
DuckDB, no network — and is byte-deterministic.

Tests that genuinely need the live lake (taxonomy derivation, exclusivity
validation, P0 SHAs computed under derived adjacency) gate on
:data:`LAKE_AVAILABLE` via ``@pytest.mark.skipif(not LAKE_AVAILABLE, ...)``,
matching the convention in ``test_resolution_fidelity.py``.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path

import pytest


def _check_lake() -> bool:
    """True when the lake parquets (including taxonomy) are present."""
    base = os.environ.get('MAKE_MAGIC_DATA_DIR')
    root = Path(base) if base else (Path.home() / '.local' / 'share' / 'make-magic')
    return (root / 'raw' / 'oracle_tags.parquet').exists()


#: ``False`` on offline CI; ``True`` locally with a hydrated lake.
LAKE_AVAILABLE = _check_lake()

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
    'precon__blame-game': 'Blame Game (Nelly Borca)',
    'precon__peace-offering': 'Peace Offering (Ms. Bumbleflower)',
    'precon__political-puppets': 'Political Puppets (Zedruu)',
}
