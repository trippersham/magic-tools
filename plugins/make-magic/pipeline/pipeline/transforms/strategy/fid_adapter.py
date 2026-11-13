"""fid-to-Event adapter: production data layer for Spellbook feature classification.

Promotes the validated research classifier (probe2/classify2 + val/outcome_map)
into production data.  Each of the 1,348 Spellbook features maps to:

    fid  ->  {event, magnitude, scope, object_descriptor}

Where ``event`` is a slug from the bounded trigger taxonomy
(``data/event_catalog_v4.json``), or ``None`` for features with no engine role
(oneshot effects, card-class enablers, inert symmetrics).

This module is DATA + LOADER only.  It is NOT consumed by ``resolve()`` or
``_isolate_engines()`` yet — win-detection still runs on the existing regex
path.  The adapter will be wired into win-detection in a later step (5b/6).

Owner-ruled dispositions applied during promotion:

- etb-ltb bucket split: death→permanent-dies, LTB→permanent-leaves-battlefield,
  landfall→land-enters-battlefield, ETB→creature-enters-battlefield.
- scry/surveil → ``None`` (distinct selection, not draw-a-card).
- bounce → ``permanent-leaves-battlefield``.
- life-total-set → ``life-loss-drain`` (pragmatic: threatens same SBA).
- fid 2113 "Lich" → ``cant-lose`` (reclassified from terminal-win).
- fid 1742 "Extra Turn Spell" → ``monarch-or-extra-turn`` (finite magnitude).
- fid 1394 "Skip your future turns" → ``None`` (non-terminal standing condition;
  all-players-no-turns is a derived ``game-end(draw)`` the walk recognizes).
- Saga/dice/dungeon: NOT lumped — saga→counter-placed (lore counters),
  die-rolled/ventured/dungeon→None (separate events, not yet in catalog).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).resolve().parent / 'data' / 'fid_event_adapter.json'


@dataclass(frozen=True)
class FidMapping:
    """One fid's production classification."""

    fid: int
    name: str
    event: str | None
    magnitude: str
    scope: str
    object_descriptor: dict[str, Any] | None
    note: str | None = None


@cache
def _load_raw() -> list[dict]:
    """Load the raw adapter rows (cached, immutable)."""
    with _DATA_PATH.open() as f:
        return json.load(f)


@cache
def load_adapter() -> dict[int, FidMapping]:
    """Load the fid→Event adapter as a ``{fid: FidMapping}`` dict.

    Cached after first call.  The adapter is production data — it is not
    consumed by the engine yet (win-detection still uses regexes).
    """
    rows = _load_raw()
    return {
        row['fid']: FidMapping(
            fid=row['fid'],
            name=row['name'],
            event=row.get('event'),
            magnitude=row['magnitude'],
            scope=row['scope'],
            object_descriptor=row.get('object_descriptor'),
            note=row.get('note'),
        )
        for row in rows
    }


def lookup(fid: int) -> FidMapping | None:
    """Look up one fid's production classification (or ``None`` if unknown)."""
    return load_adapter().get(fid)


def fids_for_event(event_slug: str) -> list[FidMapping]:
    """All fids that map to the given event slug."""
    return [m for m in load_adapter().values() if m.event == event_slug]


def coverage_stats() -> dict[str, int]:
    """Quick coverage summary: total, with_event, null_event."""
    adapter = load_adapter()
    with_event = sum(1 for m in adapter.values() if m.event is not None)
    return {
        'total': len(adapter),
        'with_event': with_event,
        'null_event': len(adapter) - with_event,
    }
