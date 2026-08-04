"""The local DuckDB deck store — typed ``Deck``s as the working-copy layer.

``DecksStore`` is a DuckDB-backed store of current-state typed ``Deck``s in the
existing ``make_magic.duckdb`` (same ``store.connect`` path as ``app_state`` /
history). The typed ``Deck`` model IS the schema: ``deck_json`` round-trips
through ``model_dump_json`` / ``model_validate_json``, so EVERY read is a
VALIDATED ``Deck`` and every guard applies unchanged.

The whole point is TYPED EDITS — no dict-surgery, no parallel deck structure
(that is the thing the redesign deletes). Each edit re-reads the ``Deck``,
mutates the typed model, re-validates, and writes ``deck_json``. The round-3
bugs are made impossible BY CONSTRUCTION:

- ``remove_card`` is QUANTITY-AWARE: it decrements and drops the entry only at 0
  (a multi-copy basic loses ONE copy, not all);
- ``swap`` is size-preserving (``remove_card(cut, 1)`` + ``add_card(add)``) and
  commander-safe (never cuts the sole commander; never lets an added commander
  create a second) — the ``Deck`` model's role validation plus explicit guards;
- one entry per card NAME (``add_card`` merges into the existing entry), so a
  singleton duplicate can never be spawned.

Sync (pull/push) is Phase 2 and lives elsewhere. This module is decks only —
inventory / chase / trades are untouched.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pipeline import store as _store
from pipeline.collection.guards import shrink_check
from pipeline.contracts import ROLE_COMMANDER, Deck, DeckCard

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from duckdb import DuckDBPyConnection

__all__ = ('DecksError', 'DecksStore')

#: The DuckDB table holding the local working-copy decks.
_DECKS_TABLE = 'decks'


class DecksError(Exception):
    """A guard violation in the decks store (commander rule, missing cut, shrink)."""


def _ensure_decks_table(conn: DuckDBPyConnection) -> None:
    """Create the ``decks`` table if it does not yet exist (idempotent).

    ``deck_json`` is the SYNCED payload (the typed ``Deck``); the remaining columns
    are LOCAL-ONLY sync/freshness bookkeeping (design §3) and are stored here now
    so Phase 2 can populate them without a migration.
    """
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS {_DECKS_TABLE} ('
        'deck_id TEXT PRIMARY KEY, '
        'name TEXT, '
        'deck_json TEXT, '
        'sync_status TEXT, '  # 'ephemeral' | 'synced'
        'source_ref TEXT, '  # JSON {backend, name/record-id} | NULL (ephemeral)
        'synced_baseline TEXT, '  # version() of source at last sync | NULL
        'freshness TEXT, '  # JSON {assessment: <hash>, sim: <hash>} | NULL
        'last_sim TEXT)'  # JSON {result, deck_version} | NULL
    )


class DecksStore:
    """A DuckDB-backed local store of typed ``Deck``s (the working-copy layer)."""

    def __init__(self, *, db_path: str | os.PathLike[str] | None = None) -> None:
        """Open the store against ``make_magic.duckdb`` (or ``db_path`` for tests).

        The table is ensured on construction so the first read never 404s on a
        missing table. ``db_path`` is retained so every op targets the same file.
        """
        self._db_path = db_path
        with self._connect() as conn:
            _ensure_decks_table(conn)

    def _connect(self) -> AbstractContextManager[DuckDBPyConnection]:
        """Open a DuckDB connection via the io module (never ``import duckdb`` here)."""
        return _store.connect(self._db_path)

    # ----------------------------------------------------------------------- #
    # Reads
    # ----------------------------------------------------------------------- #

    def get(self, deck_id: str) -> Deck | None:
        """Return the VALIDATED ``Deck`` for ``deck_id``, or ``None`` if absent."""
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT deck_json FROM {_DECKS_TABLE} WHERE deck_id = ?', [deck_id]
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return Deck.model_validate_json(row[0])

    def list(self) -> list[Deck]:
        """Return every stored deck as a VALIDATED ``Deck`` (deck_id order)."""
        with self._connect() as conn:
            _ensure_decks_table(conn)
            rows = conn.execute(
                f'SELECT deck_json FROM {_DECKS_TABLE} ORDER BY deck_id'
            ).fetchall()
        return [Deck.model_validate_json(r[0]) for r in rows if r[0] is not None]

    def exists(self, deck_id: str) -> bool:
        """True iff a row for ``deck_id`` exists."""
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT 1 FROM {_DECKS_TABLE} WHERE deck_id = ?', [deck_id]
            ).fetchone()
        return row is not None

    # ----------------------------------------------------------------------- #
    # Writes (row-level upsert)
    # ----------------------------------------------------------------------- #

    def put(
        self,
        deck: Deck,
        *,
        deck_id: str,
        sync_status: str = 'ephemeral',
        source_ref: str | None = None,
        synced_baseline: str | None = None,
    ) -> None:
        """Upsert the row for ``deck_id`` (``deck_json`` = ``deck.model_dump_json``).

        The sync/freshness columns are set from the given bookkeeping (defaults =
        an ephemeral, sourceless draft). ``freshness`` / ``last_sim`` are left NULL
        here — they are Phase-2 concerns.
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(
                f'INSERT INTO {_DECKS_TABLE} '
                '(deck_id, name, deck_json, sync_status, source_ref, synced_baseline, freshness, last_sim) '
                'VALUES (?, ?, ?, ?, ?, ?, NULL, NULL) '
                'ON CONFLICT (deck_id) DO UPDATE SET '
                'name = excluded.name, deck_json = excluded.deck_json, '
                'sync_status = excluded.sync_status, source_ref = excluded.source_ref, '
                'synced_baseline = excluded.synced_baseline',
                [deck_id, deck.name, deck.model_dump_json(), sync_status, source_ref, synced_baseline],
            )

    def create_ephemeral(self, deck: Deck, deck_id: str) -> None:
        """Create an EPHEMERAL (local-only, no source) deck row for ``deck_id``."""
        self.put(deck, deck_id=deck_id, sync_status='ephemeral', source_ref=None, synced_baseline=None)

    # ----------------------------------------------------------------------- #
    # Typed edits — the whole point (no dict-surgery)
    # ----------------------------------------------------------------------- #

    def _require(self, deck_id: str) -> Deck:
        """Read the deck for ``deck_id`` or raise (edits operate on an existing deck)."""
        deck = self.get(deck_id)
        if deck is None:
            raise DecksError(f'no deck with id {deck_id!r}')
        return deck

    def _write_edit(self, deck_id: str, deck: Deck) -> None:
        """Persist an edited deck, PRESERVING the row's sync bookkeeping.

        An edit changes only ``deck_json`` / ``name``; ``sync_status`` /
        ``source_ref`` / ``synced_baseline`` must survive (an edit does not
        re-sync). Re-validate by round-tripping through the model on write.
        """
        deck = Deck.model_validate_json(deck.model_dump_json())
        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET name = ?, deck_json = ? WHERE deck_id = ?',
                [deck.name, deck.model_dump_json(), deck_id],
            )

    def _apply_add(self, deck: Deck, card: DeckCard) -> Deck:
        """Return a NEW ``Deck`` with ``card`` added — INCREMENT the existing entry,
        else append (pure; does not persist).

        One entry per card NAME: adding a name already present increments that
        entry's quantity (so a singleton duplicate can never be spawned). The role
        of the existing entry wins for a plain add; an intentional role change is
        an explicit edit, not an incidental add.
        """
        new_cards = [c.model_copy() for c in deck.cards]
        for existing in new_cards:
            if existing.name == card.name:
                existing.quantity += card.quantity
                break
        else:
            new_cards.append(card)
        return deck.model_copy(update={'cards': new_cards})

    def add_card(self, deck_id: str, card: DeckCard) -> None:
        """Add ``card`` to the deck — INCREMENT the existing entry, else append."""
        deck = self._require(deck_id)
        self._write_edit(deck_id, self._apply_add(deck, card))

    def _apply_remove(self, deck: Deck, name: str, qty: int) -> Deck:
        """Return a NEW ``Deck`` with ``qty`` copies of ``name`` removed (pure).

        QUANTITY-AWARE (the R3 fix): decrements the entry and drops it ONLY when
        it reaches 0, so a multi-copy basic loses ``qty`` copies, not all of them.
        Raises if ``name`` is not present. Does NOT persist and does NOT guard —
        the shrink guard is applied by the caller (``remove_card``), while a
        size-preserving ``swap`` reuses this without the guard.
        """
        entry = next((c for c in deck.cards if c.name == name), None)
        if entry is None:
            raise DecksError(f'card {name!r} is not in deck {deck.name!r}')
        new_cards = [c for c in deck.cards if c is not entry]
        remaining = entry.quantity - qty
        if remaining > 0:
            new_cards.append(entry.model_copy(update={'quantity': remaining}))
        return deck.model_copy(update={'cards': new_cards})

    def remove_card(self, deck_id: str, name: str, *, qty: int = 1) -> None:
        """Decrement ``name`` by ``qty``; drop the entry ONLY when it reaches 0.

        QUANTITY-AWARE (the R3 fix): a multi-copy basic loses ``qty`` copies, not
        the whole entry. Guarded like ``save_deck``: if the removal would drop a
        deck that currently MEETS its target below it, refuse (``DecksError``) —
        the ``shrink_check`` semantics, reused so a bare removal can't silently
        gut a legal deck.
        """
        deck = self._require(deck_id)
        new_deck = self._apply_remove(deck, name, qty)
        if shrink_check(deck, new_deck):
            raise DecksError(
                f'removing {qty}x {name!r} would drop deck {deck_id!r} under its target '
                f'({deck.target_size}); refusing (would shrink an at-target deck)'
            )
        self._write_edit(deck_id, new_deck)

    def set_strategy(self, deck_id: str, text: str | None) -> None:
        """Set the deck's free-text strategy."""
        deck = self._require(deck_id)
        self._write_edit(deck_id, deck.model_copy(update={'strategy': text}))

    def set_assessment(self, deck_id: str, text: str | None) -> None:
        """Set the deck's assessment (reality synthesis)."""
        deck = self._require(deck_id)
        self._write_edit(deck_id, deck.model_copy(update={'assessment': text}))

    def set_focus_otags(self, deck_id: str, otags: list[str]) -> None:
        """Set the deck's declared focus otags."""
        deck = self._require(deck_id)
        self._write_edit(deck_id, deck.model_copy(update={'focus_otags': list(otags)}))

    def swap(self, deck_id: str, *, add: DeckCard, cut: str) -> None:
        """Cut one copy of ``cut`` and add ``add`` — SIZE-PRESERVING by construction.

        ``swap = remove_card(cut, qty=1) + add_card(add)``, so it removes exactly
        one copy and adds one, leaving the deck size unchanged (never shrinking, so
        the shrink guard never trips). Commander-safe:

        - refuse if ``cut`` is not present;
        - never cut the SOLE commander (that would leave a commander deck without
          one);
        - never let ``add.role == 'commander'`` create a SECOND commander.

        The commander invariants are checked BEFORE any write, so a violation
        leaves the stored deck untouched (the whole op is refused, not half-applied).
        """
        deck = self._require(deck_id)
        cut_entry = next((c for c in deck.cards if c.name == cut), None)
        if cut_entry is None:
            raise DecksError(f'cut card {cut!r} is not in deck {deck_id!r}')

        commanders = deck.commanders
        cutting_sole_commander = cut_entry.role == ROLE_COMMANDER and len(commanders) <= 1
        if cutting_sole_commander:
            raise DecksError(f'refusing to cut the sole commander {cut!r} from deck {deck_id!r}')

        adding_commander = add.role == ROLE_COMMANDER
        # After the cut, how many commanders remain? (the cut may itself be a commander)
        commanders_after_cut = len(commanders) - (1 if cut_entry.role == ROLE_COMMANDER else 0)
        if adding_commander and add.name not in {c.name for c in commanders} and commanders_after_cut >= 1:
            raise DecksError(
                f'refusing to add a second commander {add.name!r} to deck {deck_id!r} '
                f'(already has: {[c.name for c in commanders]})'
            )

        # Apply remove(1) + add on the SAME deck in one pass: both are
        # quantity-aware / merge-by-name. Because the op removes exactly one copy
        # and adds one, it is SIZE-PRESERVING by construction and never shrinks —
        # so it correctly bypasses the per-removal shrink guard (which would
        # otherwise trip on the transient sub-target state between the two halves).
        swapped = self._apply_add(self._apply_remove(deck, cut, 1), add)
        self._write_edit(deck_id, swapped)
