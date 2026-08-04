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
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from pipeline import store as _store
from pipeline.collection import history
from pipeline.collection.guards import shrink_check
from pipeline.contracts import ROLE_COMMANDER, Deck, DeckCard

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from duckdb import DuckDBPyConnection

__all__ = ('DeckRow', 'DecksError', 'DecksStore')


class DeckRow(BaseModel):
    """The LOCAL-ONLY sync/freshness bookkeeping for a stored deck row.

    A typed view of the non-``deck_json`` columns (design §3) so callers (sync,
    undo) read the sync state without re-parsing raw tuples. ``deck_json`` itself
    is returned as a validated ``Deck`` via :meth:`DecksStore.get`.
    """

    deck_id: str
    name: str
    sync_status: str
    source_ref: str | None = None
    synced_baseline: str | None = None
    freshness: str | None = None
    last_sim: str | None = None
    archived: bool = False

#: The DuckDB table holding the local working-copy decks.
_DECKS_TABLE = 'decks'


class DecksError(Exception):
    """A guard violation in the decks store (commander rule, missing cut, shrink)."""


def _ensure_decks_table(conn: DuckDBPyConnection) -> None:
    """Create the ``decks`` table if it does not yet exist (idempotent).

    ``deck_json`` is the SYNCED payload (the typed ``Deck``); the remaining columns
    are LOCAL-ONLY sync/freshness bookkeeping (design §3) and are stored here now
    so Phase 2 can populate them without a migration.

    ``archived`` is a LOCAL lifecycle flag (default FALSE): archiving evicts an
    experimental draft from the default deck list without deleting it. Existing
    local tables (created before this column existed) are migrated in-place via an
    ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` — cheap, idempotent, no data move.
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
        'last_sim TEXT, '  # JSON {result, deck_version} | NULL
        'archived BOOLEAN DEFAULT FALSE)'  # local lifecycle: hide from default list
    )
    # Migrate a pre-existing table (created before `archived` existed) in place.
    #
    # NOTE: DuckDB's `ADD COLUMN IF NOT EXISTS ... DEFAULT FALSE` re-applies the
    # DEFAULT to existing rows even when the column already exists — running it on
    # every read would silently RESET `archived` back to FALSE. So guard it: only
    # ALTER when the column is genuinely absent (a real, one-time migration).
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({_DECKS_TABLE})').fetchall()}
    if 'archived' not in cols:
        conn.execute(f'ALTER TABLE {_DECKS_TABLE} ADD COLUMN archived BOOLEAN DEFAULT FALSE')


def _row_to_deckrow(row: Sequence[Any]) -> DeckRow:
    """Build a :class:`DeckRow` from a SELECT tuple.

    Column order (shared by ``get_row`` / ``list_rows``): deck_id, name,
    sync_status, source_ref, synced_baseline, freshness, last_sim, archived.
    """
    return DeckRow(
        deck_id=row[0],
        name=row[1],
        sync_status=row[2] or 'ephemeral',
        source_ref=row[3],
        synced_baseline=row[4],
        freshness=row[5],
        last_sim=row[6],
        archived=bool(row[7]),
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

    def list(self, *, include_archived: bool = False) -> list[Deck]:
        """Return stored decks as VALIDATED ``Deck``s (deck_id order).

        Archived decks are EXCLUDED by default (an archived draft is evicted from
        the default list without being deleted); pass ``include_archived=True`` to
        surface them again (the recovery view).
        """
        where = '' if include_archived else 'WHERE NOT archived '
        with self._connect() as conn:
            _ensure_decks_table(conn)
            rows = conn.execute(
                f'SELECT deck_json FROM {_DECKS_TABLE} {where}ORDER BY deck_id'
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

    def get_row(self, deck_id: str) -> DeckRow | None:
        """Return the LOCAL-ONLY sync bookkeeping for ``deck_id`` (or None if absent).

        The typed view of the non-``deck_json`` columns — ``sync_status`` /
        ``source_ref`` / ``synced_baseline`` / ``freshness`` / ``last_sim`` — used
        by the sync ops (drift guard) and undo (bookkeeping preservation).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT deck_id, name, sync_status, source_ref, synced_baseline, freshness, last_sim, archived '
                f'FROM {_DECKS_TABLE} WHERE deck_id = ?',
                [deck_id],
            ).fetchone()
        if row is None:
            return None
        return _row_to_deckrow(row)

    def list_rows(
        self, *, sync_status: str | None = None, include_archived: bool = False
    ) -> list[DeckRow]:
        """Return the LOCAL-ONLY sync bookkeeping rows (deck_id order).

        Optionally filtered by ``sync_status`` (``'ephemeral'`` / ``'synced'``);
        archived rows are EXCLUDED unless ``include_archived=True``. Used by the
        ``list-decks`` CLI to surface local ephemeral drafts alongside the source.
        """
        clauses: list[str] = []
        params: list[object] = []
        if sync_status is not None:
            clauses.append('sync_status = ?')
            params.append(sync_status)
        if not include_archived:
            clauses.append('NOT archived')
        where = f'WHERE {" AND ".join(clauses)} ' if clauses else ''
        with self._connect() as conn:
            _ensure_decks_table(conn)
            rows = conn.execute(
                f'SELECT deck_id, name, sync_status, source_ref, synced_baseline, freshness, last_sim, archived '
                f'FROM {_DECKS_TABLE} {where}ORDER BY deck_id',
                params,
            ).fetchall()
        return [_row_to_deckrow(r) for r in rows]

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
        rationale: str | None = None,
    ) -> None:
        """Upsert the row for ``deck_id`` (``deck_json`` = ``deck.model_dump_json``).

        The sync/freshness columns are set from the given bookkeeping (defaults =
        an ephemeral, sourceless draft). ``freshness`` / ``last_sim`` are left NULL
        here — they are Phase-2 concerns.

        ``put`` establishes / replaces a deck's state, so it APPENDS a ledger
        version (the undo floor), but ONLY when the incoming content actually
        differs from the current head version for ``deck_id`` — an idempotent
        re-``put`` (e.g. a re-pull of an unchanged source) does not spam the
        ledger. ``rationale`` labels the append (default: ``'put'``).
        """
        from pipeline.decks.version import version as _version

        new_version = _version(deck)
        with self._connect() as conn:
            _ensure_decks_table(conn)
            head = history.deck_version_rows(conn, deck_id)
            conn.execute(
                f'INSERT INTO {_DECKS_TABLE} '
                '(deck_id, name, deck_json, sync_status, source_ref, synced_baseline, freshness, last_sim) '
                'VALUES (?, ?, ?, ?, ?, ?, NULL, NULL) '
                'ON CONFLICT (deck_id) DO UPDATE SET '
                'name = excluded.name, deck_json = excluded.deck_json, '
                'sync_status = excluded.sync_status, source_ref = excluded.source_ref, '
                'synced_baseline = excluded.synced_baseline',
                # NOTE: `archived` is deliberately NOT in the UPDATE set — a re-put
                # (e.g. a re-pull of an unchanged source) must PRESERVE the local
                # archived flag rather than reset it to the insert default.
                [deck_id, deck.name, deck.model_dump_json(), sync_status, source_ref, synced_baseline],
            )
            if not head or head[-1]['version'] != new_version:
                history.append_deck_version(conn, deck_id=deck_id, deck=deck, rationale=rationale or 'put')

    def create_ephemeral(self, deck: Deck, deck_id: str) -> None:
        """Create an EPHEMERAL (local-only, no source) deck row for ``deck_id``."""
        self.put(deck, deck_id=deck_id, sync_status='ephemeral', source_ref=None, synced_baseline=None)

    def set_freshness(self, deck_id: str, freshness: dict[str, object]) -> None:
        """Set the LOCAL-ONLY ``freshness`` JSON for a row (pull stamp / artifact hashes).

        Bookkeeping only — it does NOT change ``deck_json`` and therefore does NOT
        append a ledger version (freshness is not a deck-content edit).
        """
        import json as _json

        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET freshness = ? WHERE deck_id = ?',
                [_json.dumps(freshness), deck_id],
            )

    # ----------------------------------------------------------------------- #
    # Archive lifecycle — hide a draft from the default list without deleting it
    # ----------------------------------------------------------------------- #

    def archive(self, deck_id: str) -> None:
        """Mark ``deck_id`` archived (evict from the default list). Raise if absent."""
        self._set_archived(deck_id, archived=True)

    def unarchive(self, deck_id: str) -> None:
        """Clear the archived flag on ``deck_id`` (restore it). Raise if absent."""
        self._set_archived(deck_id, archived=False)

    def _set_archived(self, deck_id: str, *, archived: bool) -> None:
        """Set the ``archived`` flag; raise ``DecksError`` when ``deck_id`` is absent.

        Bookkeeping only — it does NOT touch ``deck_json`` and therefore does NOT
        append a ledger version (archiving is not a deck-content edit).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            exists = conn.execute(
                f'SELECT 1 FROM {_DECKS_TABLE} WHERE deck_id = ?', [deck_id]
            ).fetchone()
            if exists is None:
                raise DecksError(f'no deck with id {deck_id!r}')
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET archived = ? WHERE deck_id = ?', [archived, deck_id]
            )

    # ----------------------------------------------------------------------- #
    # Typed edits — the whole point (no dict-surgery)
    # ----------------------------------------------------------------------- #

    def _require(self, deck_id: str) -> Deck:
        """Read the deck for ``deck_id`` or raise (edits operate on an existing deck)."""
        deck = self.get(deck_id)
        if deck is None:
            raise DecksError(f'no deck with id {deck_id!r}')
        return deck

    #: The edit-note used when a typed edit is applied without an explicit rationale
    #: (Phase-1 behavior is unchanged — the edit still appends a version, just with
    #: this default note rather than a caller-supplied one).
    _DEFAULT_RATIONALE = 'edit'

    def _write_edit(self, deck_id: str, deck: Deck, *, rationale: str | None) -> None:
        """Persist an edited deck + APPEND a version to the ledger (edit-triggered).

        An edit changes only ``deck_json`` / ``name``; ``sync_status`` /
        ``source_ref`` / ``synced_baseline`` must survive (an edit does not
        re-sync). Re-validate by round-tripping through the model on write.

        Every edit APPENDS a version row (design §6, W2) — un-gated — so undo and
        rationale fall out of the append-only ledger. ``rationale`` defaults to a
        generic note when omitted, so Phase-1 callers keep working unchanged (they
        still append, just with the default note). The deck write + the ledger
        append share ONE connection so they land together.
        """
        deck = Deck.model_validate_json(deck.model_dump_json())
        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET name = ?, deck_json = ? WHERE deck_id = ?',
                [deck.name, deck.model_dump_json(), deck_id],
            )
            history.append_deck_version(
                conn, deck_id=deck_id, deck=deck, rationale=rationale or self._DEFAULT_RATIONALE
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

    def add_card(self, deck_id: str, card: DeckCard, *, rationale: str | None = None) -> None:
        """Add ``card`` to the deck — INCREMENT the existing entry, else append."""
        deck = self._require(deck_id)
        self._write_edit(deck_id, self._apply_add(deck, card), rationale=rationale)

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

    def remove_card(self, deck_id: str, name: str, *, qty: int = 1, rationale: str | None = None) -> None:
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
        self._write_edit(deck_id, new_deck, rationale=rationale)

    def set_strategy(self, deck_id: str, text: str | None, *, rationale: str | None = None) -> None:
        """Set the deck's free-text strategy."""
        deck = self._require(deck_id)
        self._write_edit(deck_id, deck.model_copy(update={'strategy': text}), rationale=rationale)

    def set_assessment(self, deck_id: str, text: str | None, *, rationale: str | None = None) -> None:
        """Set the deck's assessment (reality synthesis)."""
        deck = self._require(deck_id)
        self._write_edit(deck_id, deck.model_copy(update={'assessment': text}), rationale=rationale)

    def set_focus_otags(self, deck_id: str, otags: list[str], *, rationale: str | None = None) -> None:
        """Set the deck's declared focus otags."""
        deck = self._require(deck_id)
        self._write_edit(deck_id, deck.model_copy(update={'focus_otags': list(otags)}), rationale=rationale)

    def swap(self, deck_id: str, *, add: DeckCard, cut: str, rationale: str | None = None) -> None:
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
        self._write_edit(deck_id, swapped, rationale=rationale)

    # ----------------------------------------------------------------------- #
    # Undo — restore the prior ledger version (design §6 feature-baby)
    # ----------------------------------------------------------------------- #

    def undo(self, deck_id: str) -> Deck | None:
        """Restore the deck to its PRIOR ledger version; return it (or None).

        Reads the version BEFORE the current head from the append-only ledger and
        writes it back as the current ``deck_json`` — the sync bookkeeping is
        PRESERVED (undo is a local edit, not a re-sync). Returns ``None`` (no
        write) when there is no prior version to restore to.

        The restore is itself an edit, so it APPENDS a new ledger row (the ledger
        stays append-only — undo never mutates or deletes past rows). Undoing again
        therefore reverts THAT restore's predecessor, which is the expected
        step-back-through-history behavior.
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            prior = history.previous_deck_version(conn, deck_id)
        if prior is None:
            return None
        self._write_edit(deck_id, prior, rationale='undo')
        return prior
