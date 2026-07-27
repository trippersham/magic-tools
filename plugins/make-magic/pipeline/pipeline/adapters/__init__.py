"""Adapters — plain-module surfaces behind narrow ``typing.Protocol`` ports.

Per the data-architecture per-field authority split:
    - derived/bulk (cards, otags, combos, fact-sheet rollups) -> Local->Airtable PUSH
    - human-edited (decks, trades, chase) -> Airtable->Local PULL (see ``ingest/airtable.py``)

``airtable_writeback`` is the ONLY module in the whole pipeline that writes to
Airtable, and it may only ever write the small derived-field ALLOWLIST it owns.
"""

from __future__ import annotations

from pipeline.adapters.airtable_writeback import ensure_fields, push

__all__ = ('ensure_fields', 'push')
