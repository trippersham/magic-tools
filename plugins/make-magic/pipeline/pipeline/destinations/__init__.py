"""Destinations — plain-module surfaces behind narrow ``typing.Protocol`` ports.

Per the data-architecture per-field authority split:
    - derived/bulk (cards, otags, combos, fact-sheet rollups) -> Local->Airtable SYNC
    - human-edited (decks, trades, chase) -> Airtable->Local PULL (see ``sources/airtable.py``)

The Airtable destination is the only module in the whole pipeline that writes to
Airtable, and it may only ever write the small derived-field allowlist it owns.
"""

from __future__ import annotations

from pipeline.destinations.airtable import ensure_fields, sync

__all__ = ('ensure_fields', 'sync')
