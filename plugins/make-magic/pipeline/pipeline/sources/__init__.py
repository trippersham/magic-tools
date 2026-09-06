"""Sources — hand-rolled per-source pullers loading raw data into the lake.

Each puller follows the same shape:
``fetch -> cursor/updated_at check -> append-dedupe into raw/``, and is
FAIL-OPEN: a dead network degrades to a bundled snapshot (where one exists)
rather than crashing a caller.

Shared primitives live in :mod:`pipeline.sources._common` (cursor + dedupe).
Run a single stage as ``python -m pipeline.sources.<source>`` or via the
dispatcher ``python -m pipeline.sources.run <source>``.

    SOURCES:
        oracle_tags   -> Scryfall oracle-tags bulk (bundled snapshot baseline)
        combos        -> Commander Spellbook variants (bundled snapshot baseline)
        scryfall_bulk -> Scryfall oracle_cards bulk (fetch-on-demand, not bundled)
        airtable      -> human-edited tables mirror (PULL-ONLY, GET requests only)
        spoilers      -> MythicSpoiler preview scrape (fail-open to last snapshot)

The :mod:`pipeline.sources.deck_import` sub-package is a sibling of these lake
pullers, at a different granularity: deck-level ingestion — an external deck
reference (an Archidekt/EDHREC/Moxfield URL, a file, or pasted text) resolved to a
typed :class:`~pipeline.contracts.Deck` via the ``DeckImporter`` registry. It
caches a normalized ``RawDeck`` under ``raw/deck_import/`` rather than appending
card rows into the lake, so it is distinct from the card-data pullers above.
"""

from __future__ import annotations

from pipeline.sources._common import Cursor, dedupe, is_newer

__all__ = ('Cursor', 'dedupe', 'is_newer')
