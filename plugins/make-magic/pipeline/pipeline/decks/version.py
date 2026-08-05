"""The canonical deck ``version`` primitive — a content hash over persisted facts.

A stable sha256 over only a deck's persisted, non-derived facts. One primitive
serves both sync-drift and freshness — one content hash, no commit graph.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.contracts import Deck

__all__ = ('version',)


def version(deck: Deck) -> str:
    """A stable sha256 over a deck's persisted, non-derived facts.

    Canonical projection (everything a commit durably writes; nothing volatile):
      * the card list as sorted ``(name, quantity, role)`` tuples — order-independent;
      * ``strategy``;
      * ``assessment``;
      * ``focus_otags`` (sorted — order-independent);
      * ``format``.

    Excludes hydrated Scryfall enrichment (mana cost, oracle text, image urls,
    colors, oracle_id, etc.): that is derived-on-read and volatile, so including it
    would make the hash flap for reasons that are not a real edit to the persisted
    record.

    Deterministic: the same persisted facts always yield the same hex digest, and
    the card + otag lists are sorted so insertion order never changes the result.
    Returns a 64-char sha256 hex string.
    """
    cards = sorted((card.name, card.quantity, card.role or '') for card in deck.cards)
    payload = {
        'cards': cards,
        'strategy': deck.strategy,
        'assessment': deck.assessment,
        'focus_otags': sorted(deck.focus_otags),
        'format': deck.format,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
