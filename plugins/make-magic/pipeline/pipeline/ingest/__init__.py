"""Ingest — hand-rolled per-source pullers landing raw data into the lake.

Each puller follows the same shape (data-architecture §ingest):
``fetch -> watermark/updated_at check -> append-dedupe into raw/``, and is
FAIL-OPEN: a dead network degrades to a bundled snapshot (where one exists)
rather than crashing a caller.

Shared primitives live in :mod:`pipeline.ingest._common` (watermark + dedupe).
Run a single stage as ``python -m pipeline.ingest.<source>`` or via the
dispatcher ``python -m pipeline.ingest.run <source>``.

    SOURCES:
        oracle_tags   -> Scryfall oracle-tags bulk (bundled snapshot baseline)
        combos        -> Commander Spellbook variants (bundled snapshot baseline)
        scryfall_bulk -> Scryfall oracle_cards bulk (fetch-on-demand, NOT bundled)
        airtable      -> human-edited tables mirror (PULL-ONLY, GET requests only)
"""

from __future__ import annotations

from pipeline.ingest._common import Watermark, dedupe, is_newer

__all__ = ["Watermark", "dedupe", "is_newer"]
