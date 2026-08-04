"""Append-only, time-series history mirror of deck + inventory state (DuckDB).

The durable recovery ledger behind the Defensive Deck Integrity build (Pillar 1).
It is NOT Parquet and NOT the offline-fixture kind of "snapshot" (that name is
taken) — these are native DuckDB **history** tables in the existing
``make_magic.duckdb``, written through :func:`pipeline.store.connect` exactly as
the ``app_state`` write path does (``CREATE TABLE IF NOT EXISTS`` + parameterized
``INSERT``).

Two facts shape the design:

1. **Append-only, time-series.** A pure current-state mirror would mirror the
   *drifted* state and lose the good baseline. Every gated capture INSERTs new
   rows; nothing is ever UPDATEd or DELETEd. Known-good is a pure query over the
   accumulated rows, so drift rows never overwrite a good baseline.
2. **Backend-agnostic.** Reads flow through the `CollectionStore` port
   (``list_decks`` / ``list_inventory`` / ``backend_name``), so the mirror works
   against whatever the current source of record is (Airtable today, local YAML,
   or a future primary).

Capture is gated (TTL or churn) and best-effort: :func:`record_snapshot` swallows
lake errors so a mirror write can never raise into a caller's read path. The
capture is NOT yet wired into ``get_deck`` / ``list_*``; Phase 3 (``audit-decks``)
calls :func:`record_snapshot` explicitly.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pipeline import store as _store
from pipeline.contracts import Deck

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from pipeline.collection.store import CollectionStore
    from pipeline.contracts import OwnedCard

__all__ = (
    'append_deck_version',
    'deck_version_rows',
    'last_known_good_deck',
    'last_known_inventory_row',
    'previous_deck_version',
    'record_snapshot',
)

log = logging.getLogger('make_magic.collection.history')

#: The append-only deck history table (one row per deck per gated capture).
_DECK_HISTORY_TABLE = 'deck_history'

#: The append-only inventory history table (one row per owned card per capture).
_INVENTORY_HISTORY_TABLE = 'inventory_history'

#: The append-only, per-EDIT deck-version ledger (W2 companion table).
#: Keyed on the decks-store ``deck_id`` (NOT deck_name+backend like
#: ``deck_history``) and holds the FULL typed ``Deck`` (``deck_json``) plus a
#: rationale note per mutation. Un-gated: every edit appends. This is the decks
#: store's change log — the source of undo / rationale / version-provenance —
#: kept SEPARATE from the recovery-schema ``deck_history`` so neither muddies the
#: other's keying or gating.
_DECK_VERSIONS_TABLE = 'deck_versions'


def _ensure_history_tables(conn: DuckDBPyConnection) -> None:
    """Create the append-only history tables if they do not exist (idempotent)."""
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS {_DECK_HISTORY_TABLE} ('
        'snapshot_ts TIMESTAMP, backend TEXT, deck_name TEXT, deck_format TEXT, '
        'target INTEGER, size INTEGER, passed_invariant BOOLEAN, cards JSON)'
    )
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS {_INVENTORY_HISTORY_TABLE} ('
        'snapshot_ts TIMESTAMP, backend TEXT, card_id TEXT, card_name TEXT, '
        'oracle_id TEXT, fields JSON)'
    )
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS {_DECK_VERSIONS_TABLE} ('
        # A monotonic per-row sequence: BIGINT default from a sequence so append
        # order is total + stable even within one timestamp (ts alone can tie).
        'seq BIGINT, ts TIMESTAMP, deck_id TEXT, version TEXT, rationale TEXT, deck_json JSON)'
    )


# --------------------------------------------------------------------------- #
# Row shaping
# --------------------------------------------------------------------------- #


def _as_naive_utc(when: datetime) -> datetime:
    """Normalize a datetime to naive-UTC for the tz-naive DuckDB ``TIMESTAMP``.

    DuckDB's ``TIMESTAMP`` is tz-naive; inserting a tz-aware value converts it to
    the session's local time and reads it back naive, breaking exact round-trips.
    Converting to UTC and dropping tzinfo keeps stored + injected timestamps
    directly comparable.
    """
    if when.tzinfo is not None:
        when = when.astimezone(UTC).replace(tzinfo=None)
    return when


def _deck_size(deck: Deck) -> int:
    """The deck's size = sum of its cards' quantities."""
    return sum(card.quantity for card in deck.cards)


def _deck_cards_json(deck: Deck) -> str:
    """Serialize a deck's cards to the ``{name, oracle_id, quantity, role}`` array."""
    return json.dumps(
        [{'name': c.name, 'oracle_id': c.oracle_id, 'quantity': c.quantity, 'role': c.role} for c in deck.cards]
    )


def _inventory_fields_json(card: OwnedCard) -> str:
    """Serialize the subset of an owned card's fields needed to recreate its row."""
    return json.dumps(
        {
            'name': card.name,
            'oracle_id': card.oracle_id,
            'number_owned': card.owned,
            'foil_count': card.foil,
            'condition': card.condition,
            'sets': card.sets,
            'sources': card.sources,
        }
    )


def _deck_card_names(deck: Deck) -> set[str]:
    """The deck's card-name set (the churn signal for a single deck)."""
    return {card.name for card in deck.cards}


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #


def _latest_deck_snapshot_ts(conn: DuckDBPyConnection, backend: str) -> datetime | None:
    """The most recent ``deck_history`` snapshot_ts for this backend, or None."""
    row = conn.execute(f'SELECT MAX(snapshot_ts) FROM {_DECK_HISTORY_TABLE} WHERE backend = ?', [backend]).fetchone()
    return row[0] if row else None


def _latest_deck_card_names(conn: DuckDBPyConnection, backend: str) -> dict[str, set[str]]:
    """Map each deck name -> its card-name set from the latest snapshot per deck."""
    rows = conn.execute(
        f'SELECT deck_name, cards FROM {_DECK_HISTORY_TABLE} dh '
        f'WHERE backend = ? AND snapshot_ts = ('
        f'  SELECT MAX(snapshot_ts) FROM {_DECK_HISTORY_TABLE} WHERE backend = ? AND deck_name = dh.deck_name)',
        [backend, backend],
    ).fetchall()
    result: dict[str, set[str]] = {}
    for deck_name, cards_json in rows:
        cards = json.loads(cards_json) if cards_json else []
        result[deck_name] = {c['name'] for c in cards}
    return result


def _latest_inventory_ids(conn: DuckDBPyConnection, backend: str) -> set[str]:
    """The identity set (card_id or name) of the latest inventory snapshot."""
    ts_row = conn.execute(
        f'SELECT MAX(snapshot_ts) FROM {_INVENTORY_HISTORY_TABLE} WHERE backend = ?', [backend]
    ).fetchone()
    latest_ts = ts_row[0] if ts_row else None
    if latest_ts is None:
        return set()
    rows = conn.execute(
        f'SELECT card_id, card_name FROM {_INVENTORY_HISTORY_TABLE} WHERE backend = ? AND snapshot_ts = ?',
        [backend, latest_ts],
    ).fetchall()
    return {(card_id or card_name) for card_id, card_name in rows}


def _inventory_identity(card: OwnedCard) -> str:
    """A card's stable identity for churn detection: its record id, else its name."""
    return card.airtable_record_id or card.name


def _gate_trips(
    conn: DuckDBPyConnection,
    *,
    backend: str,
    decks: list[Deck],
    inventory: list[OwnedCard],
    now: datetime,
    ttl_hours: int,
    churn_min_cards: int,
) -> bool:
    """Decide whether to persist a new snapshot.

    Persist when EITHER (a) TTL: no prior snapshot within ``ttl_hours``; OR
    (b) churn: a structural change vs the latest prior snapshot — a deck added or
    removed, any deck's card-name set changed by ``> churn_min_cards`` names, or
    any inventory row added or removed (by card_id/name).
    """
    last_ts = _latest_deck_snapshot_ts(conn, backend)
    if last_ts is None:
        return True  # no prior snapshot at all -> always persist.
    age_hours = (_as_naive_utc(now) - _as_naive_utc(last_ts)).total_seconds() / 3600.0
    if age_hours >= ttl_hours:
        return True  # TTL crossed.

    # Churn: deck set membership changed.
    prior_deck_names = _latest_deck_card_names(conn, backend)
    live_deck_names = {deck.name for deck in decks}
    if live_deck_names != set(prior_deck_names):
        return True  # a deck was added or removed.

    # Churn: any deck's card-name set moved by more than the threshold.
    for deck in decks:
        prior = prior_deck_names.get(deck.name, set())
        live = _deck_card_names(deck)
        delta = len(prior.symmetric_difference(live))
        if delta > churn_min_cards:
            return True

    # Churn: an inventory row added or removed.
    prior_inv = _latest_inventory_ids(conn, backend)
    live_inv = {_inventory_identity(card) for card in inventory}
    return prior_inv != live_inv


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


def _persist(
    conn: DuckDBPyConnection,
    *,
    backend: str,
    decks: list[Deck],
    inventory: list[OwnedCard],
    now: datetime,
) -> None:
    """INSERT one deck-history row per deck and one inventory-history row per card."""
    now = _as_naive_utc(now)
    for deck in decks:
        size = _deck_size(deck)
        target = deck.target_size
        passed = True if target is None else size >= target
        conn.execute(
            f'INSERT INTO {_DECK_HISTORY_TABLE} '
            '(snapshot_ts, backend, deck_name, deck_format, target, size, passed_invariant, cards) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [now, backend, deck.name, deck.format, target, size, passed, _deck_cards_json(deck)],
        )
    for card in inventory:
        conn.execute(
            f'INSERT INTO {_INVENTORY_HISTORY_TABLE} '
            '(snapshot_ts, backend, card_id, card_name, oracle_id, fields) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            [now, backend, card.airtable_record_id, card.name, card.oracle_id, _inventory_fields_json(card)],
        )


def _record_snapshot(
    store: CollectionStore,
    *,
    now: datetime,
    ttl_hours: int,
    churn_min_cards: int,
) -> bool:
    """The non-swallowing inner capture — reads, gates, and appends. Returns persisted?

    Split out from :func:`record_snapshot` so tests can observe raising behavior
    while the public entry point stays best-effort (lake errors swallowed).
    """
    backend = store.backend_name
    decks = store.list_decks()
    inventory = store.list_inventory()
    with _store.connect() as conn:
        _ensure_history_tables(conn)
        if not _gate_trips(
            conn,
            backend=backend,
            decks=decks,
            inventory=inventory,
            now=now,
            ttl_hours=ttl_hours,
            churn_min_cards=churn_min_cards,
        ):
            return False
        _persist(conn, backend=backend, decks=decks, inventory=inventory, now=now)
    return True


def record_snapshot(
    store: CollectionStore,
    *,
    now: datetime | None = None,
    ttl_hours: int = 24,
    churn_min_cards: int = 0,
) -> bool:
    """Read decks + inventory via ``store``, evaluate the gate, and append if it trips.

    Appends rows for BOTH entities (one per deck, one per owned card) only when the
    TTL or churn gate trips, then returns whether it persisted. Best-effort: any
    lake error is swallowed + logged (returning ``False``) so a mirror write can
    never raise into a caller's read path.

    Args:
        store: The `CollectionStore` to mirror (any backend).
        now: The capture timestamp; defaults to ``datetime.now(tz=UTC)`` (injected
            in tests for deterministic TTL crossing).
        ttl_hours: Persist if no prior snapshot exists within this many hours.
        churn_min_cards: Ignore a deck's card-set delta at or below this many names
            (default 0 = any change trips churn).

    Returns:
        ``True`` if a snapshot was persisted, ``False`` otherwise.
    """
    when = now if now is not None else datetime.now(tz=UTC)
    try:
        return _record_snapshot(store, now=when, ttl_hours=ttl_hours, churn_min_cards=churn_min_cards)
    except Exception:  # best-effort mirror: never raise into a read path.
        log.warning('history: record_snapshot failed; skipping capture', exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Edit-triggered deck-version ledger (W2 — un-gated, append-only, per deck_id)
# --------------------------------------------------------------------------- #


def _next_deck_version_seq(conn: DuckDBPyConnection) -> int:
    """The next monotonic append sequence (``MAX(seq) + 1``, 0 on an empty table).

    A total order over appends that is stable even when two rows share a ``ts``
    (undo must walk the ledger deterministically). Single-writer by construction
    (one edit at a time through the store), so a read-then-insert is safe.
    """
    row = conn.execute(f'SELECT MAX(seq) FROM {_DECK_VERSIONS_TABLE}').fetchone()
    return 0 if row is None or row[0] is None else int(row[0]) + 1


def append_deck_version(
    conn: DuckDBPyConnection,
    *,
    deck_id: str,
    deck: Deck,
    rationale: str,
    now: datetime | None = None,
) -> None:
    """Append one version row for ``deck_id`` — UN-GATED (every edit lands a row).

    The decks store's per-edit change log (design §6): records ``version(deck)``,
    the FULL typed ``Deck`` (``deck_json``, so undo can restore it), the
    ``rationale`` note, and a monotonic ``(seq, ts)`` order. APPEND-ONLY — never
    UPDATEs or DELETEs, so the head history (and undo target) is preserved.

    Unlike :func:`record_snapshot`, this is NOT gated (no TTL/churn): a deck
    version is meaningful per edit, and the ledger IS the undo/rationale record.

    Args:
        conn: An open DuckDB connection (tables ensured on demand).
        deck_id: The decks-store key this version belongs to.
        deck: The typed ``Deck`` AFTER the edit (its ``version`` + json are stored).
        rationale: The edit note (why this change was made).
        now: The append timestamp; defaults to ``datetime.now(tz=UTC)``.
    """
    from pipeline.decks.version import version as _version

    _ensure_history_tables(conn)
    when = _as_naive_utc(now if now is not None else datetime.now(tz=UTC))
    conn.execute(
        f'INSERT INTO {_DECK_VERSIONS_TABLE} (seq, ts, deck_id, version, rationale, deck_json) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        [_next_deck_version_seq(conn), when, deck_id, _version(deck), rationale, deck.model_dump_json()],
    )


def deck_version_rows(conn: DuckDBPyConnection, deck_id: str) -> list[dict[str, Any]]:
    """Return every appended version row for ``deck_id`` in append order (oldest first).

    Pure query over the append-only ledger. Each dict has ``seq, ts, version,
    rationale, deck_json`` (``deck_json`` left as its stored JSON string).
    """
    _ensure_history_tables(conn)
    rows = conn.execute(
        f'SELECT seq, ts, version, rationale, deck_json FROM {_DECK_VERSIONS_TABLE} '
        'WHERE deck_id = ? ORDER BY seq ASC',
        [deck_id],
    ).fetchall()
    return [
        {'seq': seq, 'ts': ts, 'version': ver, 'rationale': rationale, 'deck_json': deck_json}
        for seq, ts, ver, rationale, deck_json in rows
    ]


def previous_deck_version(conn: DuckDBPyConnection, deck_id: str) -> Deck | None:
    """Return the ``Deck`` BEFORE the current head for ``deck_id`` (the undo target).

    Undo restores the second-newest recorded version: the newest row IS the
    current state, so the one before it is what "undo" reverts to. Returns
    ``None`` when there is no prior version (a single or absent history).
    """
    _ensure_history_tables(conn)
    row = conn.execute(
        f'SELECT deck_json FROM {_DECK_VERSIONS_TABLE} WHERE deck_id = ? '
        'ORDER BY seq DESC LIMIT 1 OFFSET 1',
        [deck_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return Deck.model_validate_json(row[0])


# --------------------------------------------------------------------------- #
# Known-good query (pure; append-only means good rows are never overwritten)
# --------------------------------------------------------------------------- #


def last_known_good_deck(conn: DuckDBPyConnection, deck_name: str, backend: str) -> dict[str, Any] | None:
    """Return the most recent at-target ``deck_history`` row for a deck, or None.

    "Known-good" = the latest row where ``passed_invariant AND target IS NOT NULL``
    (an untargeted deck is never a recovery reference). Pure query, no mutation:
    because history is append-only, a later drift row never overwrites the good
    baseline — this simply filters to the good rows and takes the newest.

    Args:
        conn: An open DuckDB connection (tables ensured on demand).
        deck_name: The deck to look up.
        backend: The backend the snapshot was taken against.

    Returns:
        A dict with keys ``snapshot_ts, backend, deck_name, deck_format, target,
        size, passed_invariant, cards`` (``cards`` parsed from JSON), or ``None``
        if the deck has no at-target snapshot.
    """
    _ensure_history_tables(conn)
    row = conn.execute(
        f'SELECT snapshot_ts, backend, deck_name, deck_format, target, size, passed_invariant, cards '
        f'FROM {_DECK_HISTORY_TABLE} '
        'WHERE deck_name = ? AND backend = ? AND passed_invariant AND target IS NOT NULL '
        'ORDER BY snapshot_ts DESC LIMIT 1',
        [deck_name, backend],
    ).fetchone()
    if row is None:
        return None
    snapshot_ts, backend_, deck_name_, deck_format, target, size, passed_invariant, cards_json = row
    return {
        'snapshot_ts': snapshot_ts,
        'backend': backend_,
        'deck_name': deck_name_,
        'deck_format': deck_format,
        'target': target,
        'size': size,
        'passed_invariant': bool(passed_invariant),
        'cards': json.loads(cards_json) if cards_json else [],
    }


def last_known_inventory_row(
    conn: DuckDBPyConnection,
    backend: str,
    *,
    name: str,
    oracle_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the most recent ``inventory_history`` ``fields`` for a card, or None.

    Used by ``recover-decks`` to RECREATE an Inventory row that was deleted: the
    row-recreation payload (name, condition, foil, sets, sources, counts) comes
    from the newest history capture that recorded that card. Matches on
    ``oracle_id`` when supplied (the stabler key) and falls back to ``card_name``.

    Args:
        conn: An open DuckDB connection (tables ensured on demand).
        backend: The backend the snapshot was taken against.
        name: The card name (fallback identity, and the ``card_name`` filter).
        oracle_id: The card's oracle id, matched first when present.

    Returns:
        The parsed ``fields`` dict (the same subset ``_inventory_fields_json``
        writes) from the newest matching capture, or ``None`` if the card was
        never captured.
    """
    _ensure_history_tables(conn)
    if oracle_id is not None:
        row = conn.execute(
            f'SELECT fields FROM {_INVENTORY_HISTORY_TABLE} '
            'WHERE backend = ? AND oracle_id = ? '
            'ORDER BY snapshot_ts DESC LIMIT 1',
            [backend, oracle_id],
        ).fetchone()
        if row is not None:
            return json.loads(row[0]) if row[0] else None
    row = conn.execute(
        f'SELECT fields FROM {_INVENTORY_HISTORY_TABLE} '
        'WHERE backend = ? AND card_name = ? '
        'ORDER BY snapshot_ts DESC LIMIT 1',
        [backend, name],
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0]) if row[0] else None
