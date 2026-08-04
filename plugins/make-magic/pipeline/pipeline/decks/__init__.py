"""The local decks store — typed ``Deck`` working copies in DuckDB + the version
primitive.

Phase 1 of the decks-local-store redesign: the working-copy layer (``DecksStore``)
and the canonical content hash (``version``). Sync (pull/push) is Phase 2 and is
NOT built here.
"""

from __future__ import annotations

from pipeline.decks.store import DeckRow, DecksError, DecksStore
from pipeline.decks.version import version

__all__ = ('DeckRow', 'DecksError', 'DecksStore', 'version')
