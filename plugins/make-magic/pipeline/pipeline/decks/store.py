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

import json
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

    ``deck_uuid`` is the row's stable, name-independent identity (the PK) — names
    are labels. ``external_ids`` is a JSON map ``{backend: native_ref}`` (the sync
    binding key); ``derived_from`` is the parent ``deck_uuid`` for ``--from``
    exploration drafts.
    """

    deck_uuid: str
    name: str
    sync_status: str
    source_ref: str | None = None
    synced_baseline: str | None = None
    freshness: str | None = None
    last_sim: str | None = None
    archived: bool = False
    external_ids: str | None = None
    derived_from: str | None = None

#: The DuckDB table holding the local working-copy decks.
_DECKS_TABLE = 'decks'


class DecksError(Exception):
    """A guard violation in the decks store (commander rule, missing cut, shrink)."""


#: The new-shape ``decks`` DDL (the identity-migrated shape). ``deck_uuid`` is the
#: stable, name-independent PK; ``external_ids`` is the JSON sync-binding map and
#: ``derived_from`` the parent uuid for exploration drafts.
_DECKS_DDL = (
    f'CREATE TABLE IF NOT EXISTS {_DECKS_TABLE} ('
    'deck_uuid TEXT PRIMARY KEY, '  # stable, name-independent identity (minted once)
    'name TEXT, '  # a LABEL (dup names allowed; resolve by name -> uuid)
    'deck_json TEXT, '
    "sync_status TEXT, "  # 'ephemeral' | 'synced' | 'consumed'
    'source_ref TEXT, '  # JSON {backend, name/record-id} | NULL (ephemeral)
    'synced_baseline TEXT, '  # version() of source at last sync | NULL
    'freshness TEXT, '  # JSON {assessment: <hash>, sim: <hash>} | NULL
    'last_sim TEXT, '  # JSON {result, deck_version} | NULL
    'archived BOOLEAN DEFAULT FALSE, '  # local lifecycle: hide from default list
    "external_ids TEXT DEFAULT '{}', "  # JSON {backend: native_ref} — the sync binding key
    'derived_from TEXT)'  # parent deck_uuid for --from drafts | NULL
)


def _ensure_decks_table(conn: DuckDBPyConnection) -> None:
    """Create the ``decks`` table, migrating an OLD-shape table in place (idempotent).

    ``deck_json`` is the SYNCED payload (the typed ``Deck``); the remaining columns
    are LOCAL-ONLY sync/freshness bookkeeping (design §3).

    The identity migration (P1): the store used to key rows on a name-derived
    ``deck_id = '<backend>:<name>'``. This re-keys to a stable, name-independent
    ``deck_uuid`` PK and adds ``external_ids`` / ``derived_from``. Because DuckDB
    cannot repoint a PRIMARY KEY via ALTER, the migration REBUILDS the table
    (create new-shape, copy rows in with minted uuids, drop/rename). It is:

      * one-time — detected via ``PRAGMA table_info`` (old = has ``deck_id`` and
        no ``deck_uuid``); once migrated a second call is a pure no-op;
      * LOCAL-ONLY — ``external_ids['airtable']`` is read from the EXISTING LOCAL
        row's ``Deck.airtable_record_id`` (never from Airtable). No network.

    ``archived`` (older tables predate it) is still ADD-COLUMN-migrated in place
    for a new-shape table that merely lacks it — cheap, idempotent, no data move.
    """
    conn.execute(_DECKS_DDL)
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({_DECKS_TABLE})').fetchall()}

    # The old-shape signature: keyed on `deck_id`, no `deck_uuid`. A REBUILD.
    if 'deck_id' in cols and 'deck_uuid' not in cols:
        _migrate_deck_id_to_uuid(conn)
        return  # the rebuild lands the full new shape — no further ALTERs needed.

    # A new-shape table that predates a later column: ADD it in place (guarded so
    # DuckDB's re-applied DEFAULT can't reset an existing column's rows).
    if 'archived' not in cols:
        conn.execute(f'ALTER TABLE {_DECKS_TABLE} ADD COLUMN archived BOOLEAN DEFAULT FALSE')
    if 'external_ids' not in cols:
        conn.execute(f"ALTER TABLE {_DECKS_TABLE} ADD COLUMN external_ids TEXT DEFAULT '{{}}'")
    if 'derived_from' not in cols:
        conn.execute(f'ALTER TABLE {_DECKS_TABLE} ADD COLUMN derived_from TEXT')


def _migrate_deck_id_to_uuid(conn: DuckDBPyConnection) -> None:
    """REBUILD the old-shape ``decks`` table onto the ``deck_uuid`` PK (one-time).

    For each existing row: mint a fresh ``deck_uuid``; set ``external_ids`` from
    the LOCAL row (``{"airtable": <record_id>}`` when the deck holds one, else
    ``{"local": <deck_uuid>}`` — the key the binder reads); re-key that row's
    ``deck_versions`` ledger
    entries from the old ``deck_id`` to the new ``deck_uuid`` so undo still reaches
    the pre-migration history. Purely local — no Airtable call, no delete_record.
    """
    import json as _json
    from uuid import uuid4

    # Ensure the ledger's key column is itself renamed (deck_id -> deck_uuid) BEFORE
    # we re-key its row values below — the history module owns that column rename.
    history._ensure_history_tables(conn)

    old_rows = conn.execute(
        f'SELECT deck_id, name, deck_json, sync_status, source_ref, synced_baseline, '
        f'freshness, last_sim, archived FROM {_DECKS_TABLE}'
    ).fetchall()

    # Stage the new-shape table alongside the old, populate, then swap names. The
    # ledger column was already renamed deck_id -> deck_uuid by the history module;
    # here we rewrite the ledger's ROW VALUES from the old id to the minted uuid.
    staging = f'{_DECKS_TABLE}__uuid_migration'
    conn.execute(f'DROP TABLE IF EXISTS {staging}')
    conn.execute(_DECKS_DDL.replace(f'IF NOT EXISTS {_DECKS_TABLE}', f'IF NOT EXISTS {staging}'))

    for old_id, name, deck_json, sync_status, source_ref, synced_baseline, freshness, last_sim, archived in old_rows:
        new_uuid = uuid4().hex
        record_id = _airtable_record_id(deck_json)
        # Key MUST be 'local' (not 'local_yaml') to match the binder
        # (``access._external_ref`` / ``uuid_for_external_ref``): a mismatched key is
        # dead weight and forces the name fallback F2/F4 exploit. The value is the
        # row's minted uuid as a self-consistent placeholder; the real file-uuid
        # binding self-heals on the first pull (``access._pull_bound`` →
        # ``set_external_id``) and the local YAML backfill injects the matching
        # in-file uuid additively.
        external_ids = {'airtable': record_id} if record_id else {'local': new_uuid}
        conn.execute(
            f'INSERT INTO {staging} '
            '(deck_uuid, name, deck_json, sync_status, source_ref, synced_baseline, '
            'freshness, last_sim, archived, external_ids, derived_from) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)',
            [
                new_uuid, name, deck_json, sync_status, source_ref, synced_baseline,
                freshness, last_sim, bool(archived), _json.dumps(external_ids),
            ],
        )
        # Re-key the ledger rows for this deck (old id value -> the minted uuid).
        conn.execute(
            'UPDATE deck_versions SET deck_uuid = ? WHERE deck_uuid = ?', [new_uuid, old_id]
        )

    # Crash-atomic swap (F14): DROP + RENAME as ONE transaction so a crash between
    # them can never leave the store with no ``decks`` table (which would strand all
    # rows in the orphaned staging table). DuckDB rolls the whole BEGIN..COMMIT back
    # on failure.
    conn.execute('BEGIN TRANSACTION')
    try:
        conn.execute(f'DROP TABLE {_DECKS_TABLE}')
        conn.execute(f'ALTER TABLE {staging} RENAME TO {_DECKS_TABLE}')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


def _airtable_record_id(deck_json: str | None) -> str | None:
    """Read ``airtable_record_id`` from a stored deck's JSON (LOCAL read; no network)."""
    if not deck_json:
        return None
    try:
        data = json.loads(deck_json)
    except (json.JSONDecodeError, TypeError):
        return None
    rec = data.get('airtable_record_id') if isinstance(data, dict) else None
    return rec if isinstance(rec, str) and rec else None


def _row_to_deckrow(row: Sequence[Any]) -> DeckRow:
    """Build a :class:`DeckRow` from a SELECT tuple.

    Column order (shared by ``get_row`` / ``list_rows``): deck_uuid, name,
    sync_status, source_ref, synced_baseline, freshness, last_sim, archived,
    external_ids, derived_from.
    """
    return DeckRow(
        deck_uuid=row[0],
        name=row[1],
        sync_status=row[2] or 'ephemeral',
        source_ref=row[3],
        synced_baseline=row[4],
        freshness=row[5],
        last_sim=row[6],
        archived=bool(row[7]),
        external_ids=row[8],
        derived_from=row[9],
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

    #: The column list every ``DeckRow`` SELECT reads, in ``_row_to_deckrow`` order.
    _ROW_COLUMNS = (
        'deck_uuid, name, sync_status, source_ref, synced_baseline, '
        'freshness, last_sim, archived, external_ids, derived_from'
    )

    def get(self, deck_uuid: str) -> Deck | None:
        """Return the VALIDATED ``Deck`` for ``deck_uuid``, or ``None`` if absent."""
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT deck_json FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid]
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return Deck.model_validate_json(row[0])

    def list(self, *, include_archived: bool = False) -> list[Deck]:
        """Return stored decks as VALIDATED ``Deck``s (deck_uuid order).

        Archived decks are EXCLUDED by default (an archived draft is evicted from
        the default list without being deleted); pass ``include_archived=True`` to
        surface them again (the recovery view).
        """
        where = '' if include_archived else 'WHERE NOT archived '
        with self._connect() as conn:
            _ensure_decks_table(conn)
            rows = conn.execute(
                f'SELECT deck_json FROM {_DECKS_TABLE} {where}ORDER BY deck_uuid'
            ).fetchall()
        return [Deck.model_validate_json(r[0]) for r in rows if r[0] is not None]

    def exists(self, deck_uuid: str) -> bool:
        """True iff a row for ``deck_uuid`` exists."""
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT 1 FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid]
            ).fetchone()
        return row is not None

    def uuid_for_name(self, name: str) -> str | None:
        """Return the ``deck_uuid`` of the single NON-consumed row named ``name`` (or None).

        The name->uuid resolution the access shim leans on (design §3). Names are
        labels, so this is a lookup, not identity. A ``consumed`` draft is excluded
        (a retired exploration draft, not an addressable deck). When more than one
        live row shares a name this returns the OLDEST (lowest rowid) as a
        deterministic tie-break — but the P2 name resolver (``DeckAccess.resolve``)
        REFUSES a dup name with a candidate list rather than lean on this; it is
        kept only for the promote lookup where an exact single-name draft is meant.
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT deck_uuid FROM {_DECKS_TABLE} '
                "WHERE name = ? AND sync_status IS DISTINCT FROM 'consumed' "
                'ORDER BY rowid LIMIT 1',
                [name],
            ).fetchone()
        return row[0] if row is not None else None

    def rows_for_name(self, name: str) -> list[DeckRow]:
        """Return ALL live (NON-consumed) rows named ``name``, rowid order.

        The dup-name candidate set the P2 resolver disambiguates over (design §2).
        A ``consumed`` draft is excluded (not addressable). Archived rows ARE
        included — they remain addressable by ``--id`` and appear in the candidate
        list (tagged ``archived``).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            rows = conn.execute(
                f'SELECT {self._ROW_COLUMNS} FROM {_DECKS_TABLE} '
                "WHERE name = ? AND sync_status IS DISTINCT FROM 'consumed' "
                'ORDER BY rowid',
                [name],
            ).fetchall()
        return [_row_to_deckrow(r) for r in rows]

    def uuids_by_prefix(self, prefix: str) -> list[str]:
        """Return every NON-consumed ``deck_uuid`` that starts with ``prefix``.

        Backs the ``--id <prefix>`` escape hatch (design §2/§3): 0 -> the caller
        errors, 1 -> resolve, >1 -> ambiguous-prefix error. A ``consumed`` draft is
        excluded (not addressable).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            rows = conn.execute(
                f'SELECT deck_uuid FROM {_DECKS_TABLE} '
                "WHERE deck_uuid LIKE ? AND sync_status IS DISTINCT FROM 'consumed' "
                'ORDER BY rowid',
                [prefix.replace('%', r'\%').replace('_', r'\_') + '%'],
            ).fetchall()
        return [r[0] for r in rows]

    def uuid_for_external_ref(self, backend: str, native_ref: str) -> str | None:
        """Return the ``deck_uuid`` bound to ``external_ids[backend] == native_ref`` (or None).

        The sync BINDING lookup (design §4): "same external ref = same deck". A
        source deck's stable native ref (a local in-file uuid / an Airtable
        recordId) locates its ONE local row regardless of the name it was addressed
        under — the mechanism that kills M4 (case-alias / rename dual rows). A
        ``consumed`` draft is excluded (it is a retired lineage, not the live row).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            rows = conn.execute(
                f'SELECT deck_uuid, external_ids FROM {_DECKS_TABLE} '
                "WHERE sync_status IS DISTINCT FROM 'consumed'",
            ).fetchall()
        for deck_uuid, raw in rows:
            try:
                ext = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(ext, dict) and ext.get(backend) == native_ref:
                return deck_uuid
        return None

    def set_external_id(self, deck_uuid: str, backend: str, native_ref: str) -> None:
        """Bind ``external_ids[backend] = native_ref`` on a row (merge, don't clobber).

        Bookkeeping only — it does NOT touch ``deck_json`` and therefore does NOT
        append a ledger version. Merges into any existing map so a row can carry
        both a ``local`` and an ``airtable`` ref (cross-machine identity, design §2).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT external_ids FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid]
            ).fetchone()
            if row is None:
                raise DecksError(f'no deck with id {deck_uuid!r}')
            try:
                ext = json.loads(row[0]) if row[0] else {}
            except (json.JSONDecodeError, TypeError):
                ext = {}
            if not isinstance(ext, dict):
                ext = {}
            ext[backend] = native_ref
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET external_ids = ? WHERE deck_uuid = ?',
                [json.dumps(ext), deck_uuid],
            )

    def replace_external_ids(self, deck_uuid: str, ext: dict[str, str]) -> None:
        """Replace a row's ``external_ids`` map wholesale (used to DROP a dead ref, P11).

        Unlike :meth:`set_external_id` (which merges a single key), this writes the
        whole map — the fresh-identity recovery save (``access._prepare_recovery``,
        P11) drops the now-dead ref so the subsequent create is a clean first push, not
        an adoption of the stale slug path. Bookkeeping only: it does NOT touch
        ``deck_json`` (no ledger version).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT deck_uuid FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid]
            ).fetchone()
            if row is None:
                raise DecksError(f'no deck with id {deck_uuid!r}')
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET external_ids = ? WHERE deck_uuid = ?',
                [json.dumps(ext) if ext else None, deck_uuid],
            )

    def external_ids(self, deck_uuid: str) -> str | None:
        """Return the raw ``external_ids`` JSON for ``deck_uuid`` (or None if absent).

        The sync BINDING key ``{backend: native_ref}`` (design §3) — populated by
        the identity migration and, going forward, by pull/promote (P2/P3).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT external_ids FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid]
            ).fetchone()
        return row[0] if row is not None else None

    def get_row(self, deck_uuid: str) -> DeckRow | None:
        """Return the LOCAL-ONLY sync bookkeeping for ``deck_uuid`` (or None if absent).

        The typed view of the non-``deck_json`` columns — ``sync_status`` /
        ``source_ref`` / ``synced_baseline`` / ``freshness`` / ``last_sim`` /
        ``external_ids`` / ``derived_from`` — used by the sync ops (drift guard)
        and undo (bookkeeping preservation).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT {self._ROW_COLUMNS} FROM {_DECKS_TABLE} WHERE deck_uuid = ?',
                [deck_uuid],
            ).fetchone()
        if row is None:
            return None
        return _row_to_deckrow(row)

    def list_rows(
        self, *, sync_status: str | None = None, include_archived: bool = False
    ) -> list[DeckRow]:
        """Return the LOCAL-ONLY sync bookkeeping rows (deck_uuid order).

        Optionally filtered by ``sync_status`` (``'ephemeral'`` / ``'synced'`` /
        ``'consumed'``); archived rows are EXCLUDED unless ``include_archived=True``.
        Used by the ``list-decks`` CLI to surface local ephemeral drafts alongside
        the source.
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
                f'SELECT {self._ROW_COLUMNS} FROM {_DECKS_TABLE} {where}ORDER BY deck_uuid',
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
        deck_uuid: str,
        sync_status: str = 'ephemeral',
        source_ref: str | None = None,
        synced_baseline: str | None = None,
        derived_from: str | None = None,
        rationale: str | None = None,
    ) -> None:
        """Upsert the row for ``deck_uuid`` (``deck_json`` = ``deck.model_dump_json``).

        The sync/freshness columns are set from the given bookkeeping (defaults =
        an ephemeral, sourceless draft). ``freshness`` / ``last_sim`` are left NULL
        here — they are Phase-2 concerns.

        ``derived_from`` (the parent uuid for a ``--from`` exploration draft, design
        §3) is written on INSERT; on a re-``put`` (conflict) it is preserved unless a
        NON-NULL value is supplied (so a lineage stamp survives a re-pull, and an
        explicit re-stamp still lands).

        ``put`` establishes / replaces a deck's state, so it APPENDS a ledger
        version (the undo floor), but ONLY when the incoming content actually
        differs from the current head version for ``deck_uuid`` — an idempotent
        re-``put`` (e.g. a re-pull of an unchanged source) does not spam the
        ledger. ``rationale`` labels the append (default: ``'put'``).
        """
        from pipeline.decks.version import version as _version

        new_version = _version(deck)
        with self._connect() as conn:
            _ensure_decks_table(conn)
            head = history.deck_version_rows(conn, deck_uuid)
            conn.execute(
                f'INSERT INTO {_DECKS_TABLE} '
                '(deck_uuid, name, deck_json, sync_status, source_ref, synced_baseline, '
                'derived_from, freshness, last_sim) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL) '
                'ON CONFLICT (deck_uuid) DO UPDATE SET '
                'name = excluded.name, deck_json = excluded.deck_json, '
                'sync_status = excluded.sync_status, source_ref = excluded.source_ref, '
                'synced_baseline = excluded.synced_baseline, '
                # COALESCE preserves an existing lineage unless a non-NULL is given.
                f'derived_from = COALESCE(excluded.derived_from, {_DECKS_TABLE}.derived_from)',
                # NOTE: `archived` / `external_ids` are deliberately NOT in the UPDATE
                # set — a re-put (e.g. a re-pull of an unchanged source) must PRESERVE
                # the local archived flag + binding identity rather than reset them.
                [deck_uuid, deck.name, deck.model_dump_json(), sync_status, source_ref,
                 synced_baseline, derived_from],
            )
            if not head or head[-1]['version'] != new_version:
                history.append_deck_version(conn, deck_uuid=deck_uuid, deck=deck, rationale=rationale or 'put')

    def create_ephemeral(self, deck: Deck, *, derived_from: str | None = None) -> str:
        """Create an EPHEMERAL (local-only, no source) draft; return its minted uuid.

        Identity is name-independent (design §3): a fresh ``deck_uuid`` is minted
        here (taken from the deck's own ``uuid``), so a new draft can NEVER hijack
        an existing row — even one that reuses a previously-archived name. The row
        is born VISIBLE (``archived = FALSE``, the insert default) so a draft that
        reuses an archived name is not silently hidden (fixes m4).

        ``derived_from`` is the parent ``deck_uuid`` for a ``new-draft --from`` copy
        (the LINEAGE that lets ``promote`` commit the exploration back onto the
        parent's external ref rather than clobbering an unrelated deck by name).
        A clean-slate draft passes ``None``.
        """
        deck_uuid = deck.uuid
        self.put(
            deck, deck_uuid=deck_uuid, sync_status='ephemeral', source_ref=None,
            synced_baseline=None, derived_from=derived_from,
        )
        return deck_uuid

    def consume(self, deck_uuid: str) -> None:
        """Retire an exploration draft: mark it ``consumed`` + ``archived`` (inert).

        The tail of an exploration ``promote`` (design §4): once the draft's content
        is committed onto the parent's source, the draft ROW is retired — a consumed
        row is excluded from name/prefix resolution, refuses edits + push, and can
        never materialize a source deck (kills the B2 zombie). Bookkeeping only (no
        ``deck_json`` change, no ledger append) — the content lives on the parent now.
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            exists = conn.execute(
                f'SELECT 1 FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid]
            ).fetchone()
            if exists is None:
                raise DecksError(f'no deck with id {deck_uuid!r}')
            conn.execute(
                f"UPDATE {_DECKS_TABLE} SET sync_status = 'consumed', archived = TRUE "
                'WHERE deck_uuid = ?',
                [deck_uuid],
            )

    def delete(self, deck_uuid: str) -> None:
        """Remove a row and ALL its ledger versions (first-save rollback, P10).

        Used only to undo a FIRST ``save_deck`` whose commit-push was refused: the
        just-created row never had prior content to roll back to, so it is deleted
        wholesale (row + versions + undo cursor) — nothing refused lingers for a later
        ``sync`` to land. A missing row is a no-op (idempotent).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(f'DELETE FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid])
            history.delete_deck_versions(conn, deck_uuid)

    def set_freshness(self, deck_uuid: str, freshness: dict[str, object]) -> None:
        """Set the LOCAL-ONLY ``freshness`` JSON for a row (pull stamp / artifact hashes).

        Bookkeeping only — it does NOT change ``deck_json`` and therefore does NOT
        append a ledger version (freshness is not a deck-content edit).

        NOTE: this REPLACES the whole freshness blob. To add/replace a single key
        while preserving the others (e.g. stamp ``assessment`` without clobbering
        the W4 ``pulled_at``), use :meth:`merge_freshness`.
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET freshness = ? WHERE deck_uuid = ?',
                [json.dumps(freshness), deck_uuid],
            )

    def merge_freshness(self, deck_uuid: str, updates: dict[str, object]) -> None:
        """MERGE ``updates`` into the row's ``freshness`` JSON (preserve the rest).

        The provenance freshness blob (design §6 / M7) is a MAP of independent
        stamps — the W4 ``pulled_at`` and the P5 ``assessment`` stamp coexist. A
        naive :meth:`set_freshness` would clobber the siblings, so the assessment
        stamp reads the current JSON, sets/replaces only the given keys, and writes
        the merged result back. Bookkeeping only — no ``deck_json`` change, no
        ledger append. A missing row is a silent no-op (the caller upserted first).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            row = conn.execute(
                f'SELECT freshness FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid]
            ).fetchone()
            if row is None:
                return
            try:
                current = json.loads(row[0]) if row[0] else {}
            except (json.JSONDecodeError, TypeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            current.update(updates)
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET freshness = ? WHERE deck_uuid = ?',
                [json.dumps(current), deck_uuid],
            )

    def set_last_sim(self, deck_uuid: str, *, result: object) -> None:
        """Stamp ``last_sim = {result, deck_version, at}`` on a row (the M7 sim stamp).

        The thin VALIDATE hook (preflight W-E): the ``pipeline/sim/`` subsystem is
        UNTOUCHED — the simulate step calls this with its run summary. ``result`` is
        stored VERBATIM (a parsed JSON blob — win-rate, games, etc. — or a raw
        string). ``deck_version`` is stamped as the deck's CURRENT :func:`version`,
        so a later content edit makes the stamp stale (:meth:`sim_state`).

        Bookkeeping only — it does NOT change ``deck_json`` and therefore does NOT
        append a ledger version (a sim result is not a deck-content edit). Raises
        ``DecksError`` if the deck is absent (nothing to stamp against).
        """
        from datetime import UTC, datetime

        from pipeline.decks.version import version as _version

        deck = self.get(deck_uuid)
        if deck is None:
            raise DecksError(f'no deck with id {deck_uuid!r}')
        blob = {
            'result': result,
            'deck_version': _version(deck),
            'at': datetime.now(tz=UTC).isoformat(),
        }
        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET last_sim = ? WHERE deck_uuid = ?',
                [json.dumps(blob), deck_uuid],
            )

    # ----------------------------------------------------------------------- #
    # Derived staleness (tri-state: fresh | stale | absent) — design §6 / M7
    # ----------------------------------------------------------------------- #

    def assessment_state(self, deck_uuid: str) -> str:
        """The tri-state freshness of the deck's ASSESSMENT stamp vs current ``version()``.

        ``absent`` (never assessed — no stamp), ``fresh`` (stamped version ==
        current), or ``stale`` (a content edit moved the version since the stamp).
        Distinguishing ``absent`` from ``stale`` lets prose say "never validated"
        honestly rather than conflating it with a drifted stamp. Derived, not
        remembered — read purely from the stored ``freshness.assessment.version``.
        """
        row = self.get_row(deck_uuid)
        deck = self.get(deck_uuid)
        if row is None or deck is None:
            return 'absent'
        stamped = self._stamped_version(row.freshness, 'assessment', 'version')
        return self._tri_state(stamped, deck)

    def sim_state(self, deck_uuid: str) -> str:
        """The tri-state freshness of the deck's ``last_sim`` stamp vs current ``version()``.

        ``absent`` (never simmed), ``fresh`` (stamped ``deck_version`` == current),
        or ``stale`` (an edit moved the version since the sim). Derived from the
        stored ``last_sim.deck_version`` — the cross-session staleness M7 delivers.
        """
        row = self.get_row(deck_uuid)
        deck = self.get(deck_uuid)
        if row is None or deck is None:
            return 'absent'
        stamped = self._stamped_version(row.last_sim, None, 'deck_version')
        return self._tri_state(stamped, deck)

    @staticmethod
    def _tri_state(stamped_version: str | None, deck: Deck) -> str:
        """Map a stamped version (or None) against ``deck``'s current version to the tri-state."""
        from pipeline.decks.version import version as _version

        if stamped_version is None:
            return 'absent'
        return 'fresh' if stamped_version == _version(deck) else 'stale'

    @staticmethod
    def _stamped_version(raw: str | None, section: str | None, key: str) -> str | None:
        """Pull a stamped version string out of a freshness / last_sim JSON blob.

        ``section`` selects a nested object first (``freshness['assessment']``);
        pass ``None`` to read ``key`` off the top level (``last_sim['deck_version']``).
        Any malformed / missing stamp yields ``None`` (treated as absent).
        """
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if section is not None:
            data = data.get(section)
            if not isinstance(data, dict):
                return None
        value = data.get(key)
        return value if isinstance(value, str) else None

    # ----------------------------------------------------------------------- #
    # Archive lifecycle — hide a draft from the default list without deleting it
    # ----------------------------------------------------------------------- #

    def archive(self, deck_uuid: str) -> None:
        """Mark ``deck_uuid`` archived (evict from the default list). Raise if absent.

        Archiving is for DRAFTS (ephemeral / consumed), not source-backed decks: a
        ``synced`` row is REFUSED (m3) — you cannot hide a source deck by archiving
        its local shadow; delete it at the source to remove it.
        """
        row = self.get_row(deck_uuid)
        if row is not None and row.sync_status == 'synced':
            raise DecksError(
                f'{row.name!r} is a synced deck, not a draft — archive is for drafts; '
                'delete it at the source to remove it'
            )
        self._set_archived(deck_uuid, archived=True)

    def unarchive(self, deck_uuid: str) -> None:
        """Clear the archived flag on ``deck_uuid`` (restore it). Raise if absent."""
        self._set_archived(deck_uuid, archived=False)

    def _set_archived(self, deck_uuid: str, *, archived: bool) -> None:
        """Set the ``archived`` flag; raise ``DecksError`` when ``deck_uuid`` is absent.

        Bookkeeping only — it does NOT touch ``deck_json`` and therefore does NOT
        append a ledger version (archiving is not a deck-content edit).
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            exists = conn.execute(
                f'SELECT 1 FROM {_DECKS_TABLE} WHERE deck_uuid = ?', [deck_uuid]
            ).fetchone()
            if exists is None:
                raise DecksError(f'no deck with id {deck_uuid!r}')
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET archived = ? WHERE deck_uuid = ?', [archived, deck_uuid]
            )

    # ----------------------------------------------------------------------- #
    # Typed edits — the whole point (no dict-surgery)
    # ----------------------------------------------------------------------- #

    def _require(self, deck_uuid: str) -> Deck:
        """Read the deck for ``deck_uuid`` or raise (edits operate on an existing deck).

        A ``consumed`` draft is INERT (design §4, kills B2): its content already
        lives on the parent's source, so an edit is REFUSED with a clear message
        rather than mutating a retired lineage. Reads (``get`` / ``get_row``) still
        work for forensics; only the write path refuses here.
        """
        row = self.get_row(deck_uuid)
        if row is not None and row.sync_status == 'consumed':
            raise DecksError(
                f'{row.name!r} is a consumed draft (retired after promote) and cannot be '
                'edited or pushed; start a fresh --from draft off the parent to explore again'
            )
        deck = self.get(deck_uuid)
        if deck is None:
            raise DecksError(f'no deck with id {deck_uuid!r}')
        return deck

    #: The edit-note used when a typed edit is applied without an explicit rationale
    #: (Phase-1 behavior is unchanged — the edit still appends a version, just with
    #: this default note rather than a caller-supplied one).
    _DEFAULT_RATIONALE = 'edit'

    def _write_edit(
        self, deck_uuid: str, deck: Deck, *, rationale: str | None, is_undo: bool = False
    ) -> None:
        """Persist an edited deck + APPEND a version to the ledger (edit-triggered).

        An edit changes only ``deck_json`` / ``name``; ``sync_status`` /
        ``source_ref`` / ``synced_baseline`` must survive (an edit does not
        re-sync). Re-validate by round-tripping through the model on write.

        Every edit APPENDS a version row (design §6, W2) — un-gated — so undo and
        rationale fall out of the append-only ledger. ``rationale`` defaults to a
        generic note when omitted, so Phase-1 callers keep working unchanged (they
        still append, just with the default note). The deck write + the ledger
        append share ONE connection so they land together.

        Undo cursor (M1): a NEW edit (``is_undo=False``) RESETS the persistent undo
        cursor so the next undo re-anchors at head (a fresh edit branches the
        timeline; the forward history is abandoned — no redo). An undo-triggered
        write (``is_undo=True``) sets the cursor to the restored version's seq in
        :meth:`undo` instead, so a consecutive undo continues walking backward.
        """
        deck = Deck.model_validate_json(deck.model_dump_json())
        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET name = ?, deck_json = ? WHERE deck_uuid = ?',
                [deck.name, deck.model_dump_json(), deck_uuid],
            )
            history.append_deck_version(
                conn, deck_uuid=deck_uuid, deck=deck, rationale=rationale or self._DEFAULT_RATIONALE
            )
            if not is_undo:
                history.reset_undo_cursor(conn, deck_uuid)

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

    def _guard_commander_singleton(self, deck: Deck, card: DeckCard) -> None:
        """Refuse an ADD that would push a commander to qty >= 2 (m1 — singleton rule).

        A commander is a SINGLETON: adding a card that is already a commander in the
        deck would increment it to qty 2 (or adding a second, distinct commander).
        Both are refused BEFORE any write, so the stored deck is untouched. A plain
        (non-commander) increment of a normal card is fine — only the commander line
        is protected here.
        """
        existing = next((c for c in deck.cards if c.name == card.name), None)
        # Incrementing an existing commander -> would create a 2nd copy of it.
        if existing is not None and existing.role == ROLE_COMMANDER:
            raise DecksError(
                f'refusing to increment commander {card.name!r} in deck {deck.name!r} '
                '(a commander is a singleton — qty must stay 1)'
            )
        # Adding a distinct commander when one already exists -> a 2nd commander.
        if card.role == ROLE_COMMANDER and existing is None:
            others = [c.name for c in deck.commanders]
            if others:
                raise DecksError(
                    f'refusing to add a second commander {card.name!r} '
                    f'(deck already has: {others})'
                )

    def add_card(self, deck_uuid: str, card: DeckCard, *, rationale: str | None = None) -> None:
        """Add ``card`` to the deck — INCREMENT the existing entry, else append.

        Commander-safe (m1): refuses an add that would push a commander to qty >= 2
        (incrementing the existing commander) or introduce a SECOND commander.
        """
        deck = self._require(deck_uuid)
        self._guard_commander_singleton(deck, card)
        self._write_edit(deck_uuid, self._apply_add(deck, card), rationale=rationale)

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

    def remove_card(self, deck_uuid: str, name: str, *, qty: int = 1, rationale: str | None = None) -> None:
        """Decrement ``name`` by ``qty``; drop the entry ONLY when it reaches 0.

        QUANTITY-AWARE (the R3 fix): a multi-copy basic loses ``qty`` copies, not
        the whole entry. Guarded like ``save_deck``: if the removal would drop a
        deck that currently MEETS its target below it, refuse (``DecksError``) —
        the ``shrink_check`` semantics, reused so a bare removal can't silently
        gut a legal deck.
        """
        deck = self._require(deck_uuid)
        entry = next((c for c in deck.cards if c.name == name), None)
        # m2: never cut the SOLE commander (that would leave a commander deck without
        # one). Checked BEFORE any write, so the stored deck is untouched on refusal.
        if entry is not None and entry.role == ROLE_COMMANDER and len(deck.commanders) <= 1:
            raise DecksError(
                f'refusing to remove the sole commander {name!r} from deck {deck_uuid!r} '
                '(a commander deck must keep its commander)'
            )
        new_deck = self._apply_remove(deck, name, qty)
        if shrink_check(deck, new_deck):
            raise DecksError(
                f'removing {qty}x {name!r} would drop deck {deck_uuid!r} under its target '
                f'({deck.target_size}); refusing (would shrink an at-target deck)'
            )
        self._write_edit(deck_uuid, new_deck, rationale=rationale)

    def rollback_failed_edit(self, deck_uuid: str, pre_edit: Deck) -> None:
        """Restore ``deck_uuid`` to ``pre_edit`` after a REFUSED commit (M5 tail).

        Transactional edit+commit: when a just-applied edit's commit-push is refused
        (shrink guard / sync drift), the edit must NOT half-land. This restores the
        local ``deck_json`` to the pre-edit content AND pops the poison head ledger
        version, so a later verb reads the un-poisoned deck and undo never steps onto
        the reverted edit. Bookkeeping (sync_status / source_ref / baseline) survives.
        The restore is a pure ROLLBACK — it does NOT append a new version.
        """
        pre_edit = Deck.model_validate_json(pre_edit.model_dump_json())
        with self._connect() as conn:
            _ensure_decks_table(conn)
            conn.execute(
                f'UPDATE {_DECKS_TABLE} SET name = ?, deck_json = ? WHERE deck_uuid = ?',
                [pre_edit.name, pre_edit.model_dump_json(), deck_uuid],
            )
            history.drop_deck_version_head(conn, deck_uuid)

    def set_strategy(self, deck_uuid: str, text: str | None, *, rationale: str | None = None) -> None:
        """Set the deck's free-text strategy."""
        deck = self._require(deck_uuid)
        self._write_edit(deck_uuid, deck.model_copy(update={'strategy': text}), rationale=rationale)

    def set_assessment(self, deck_uuid: str, text: str | None, *, rationale: str | None = None) -> None:
        """Set the deck's assessment (reality synthesis) + STAMP its freshness (M7).

        The assessment is a persisted deck fact (it feeds :func:`version`), so the
        edit itself moves the version. AFTER the write we stamp
        ``freshness.assessment = {version(deck_after), at}`` — MERGED into the
        existing freshness blob so the W4 ``pulled_at`` (and any sibling stamp)
        survives. The stamp records the POST-edit version, so a later content edit
        makes :meth:`assessment_state` read ``stale``.
        """
        from datetime import UTC, datetime

        from pipeline.decks.version import version as _version

        deck = self._require(deck_uuid)
        updated = deck.model_copy(update={'assessment': text})
        self._write_edit(deck_uuid, updated, rationale=rationale)
        self.merge_freshness(
            deck_uuid,
            {'assessment': {'version': _version(updated), 'at': datetime.now(tz=UTC).isoformat()}},
        )

    def set_focus_otags(self, deck_uuid: str, otags: list[str], *, rationale: str | None = None) -> None:
        """Set the deck's declared focus otags."""
        deck = self._require(deck_uuid)
        self._write_edit(deck_uuid, deck.model_copy(update={'focus_otags': list(otags)}), rationale=rationale)

    def swap(self, deck_uuid: str, *, add: DeckCard, cut: str, rationale: str | None = None) -> None:
        """Cut one copy of ``cut`` and add ``add`` — SIZE-PRESERVING by construction.

        ``swap = remove_card(cut, qty=1) + add_card(add)``, so it removes exactly
        one copy and adds one, leaving the deck size unchanged (never shrinking, so
        the shrink guard never trips). Commander-safe:

        - refuse if ``cut`` is not present;
        - never cut the SOLE commander (that would leave a commander deck without
          one);
        - never let the add create a SECOND commander OR increment an EXISTING
          commander to qty 2 (the m1 hole: the old exemption for
          ``add.name in commanders`` let a swap re-add the existing commander and
          push it to qty 2 — now closed by guarding against the POST-CUT deck).

        The commander invariants are checked BEFORE any write, so a violation
        leaves the stored deck untouched (the whole op is refused, not half-applied).
        """
        deck = self._require(deck_uuid)
        cut_entry = next((c for c in deck.cards if c.name == cut), None)
        if cut_entry is None:
            raise DecksError(f'cut card {cut!r} is not in deck {deck_uuid!r}')

        commanders = deck.commanders
        cutting_sole_commander = cut_entry.role == ROLE_COMMANDER and len(commanders) <= 1
        if cutting_sole_commander:
            raise DecksError(f'refusing to cut the sole commander {cut!r} from deck {deck_uuid!r}')

        # Guard the ADD against the POST-CUT deck (the cut may itself remove the
        # colliding commander): reuse the singleton guard so a swap can neither
        # add a second commander NOR increment the existing one to qty 2 (m1).
        post_cut = self._apply_remove(deck, cut, 1)
        self._guard_commander_singleton(post_cut, add)

        # Apply remove(1) + add on the SAME deck in one pass: both are
        # quantity-aware / merge-by-name. Because the op removes exactly one copy
        # and adds one, it is SIZE-PRESERVING by construction and never shrinks —
        # so it correctly bypasses the per-removal shrink guard (which would
        # otherwise trip on the transient sub-target state between the two halves).
        swapped = self._apply_add(self._apply_remove(deck, cut, 1), add)
        self._write_edit(deck_uuid, swapped, rationale=rationale)

    # ----------------------------------------------------------------------- #
    # Undo — restore the prior ledger version (design §6 feature-baby)
    # ----------------------------------------------------------------------- #

    def undo(self, deck_uuid: str) -> Deck | None:
        """Restore the deck one step BACKWARD along the undo cursor; return it (or None).

        The real undo walk (M1) — NOT the old OFFSET-1-from-head, which oscillated
        (each undo appended the restore, so the next undo reverted THAT) and
        deadlocked on identical-content versions. Instead:

        - :func:`history.undo_target` walks strictly backward from the persisted
          CURSOR anchor (or head when unset) to the newest version whose *content*
          differs from the current deck, SKIPPING identical-content versions (no
          oscillation, no deadlock);
        - the restore is written back (bookkeeping preserved — undo is a local edit,
          not a re-sync) as ``is_undo`` so it does NOT reset the cursor;
        - the cursor is then advanced to the restored version's seq, so a
          CONSECUTIVE undo continues from there (persisted across CLI invocations),
          revealing N successively older states over N undos.

        Returns ``None`` (no write, cursor untouched) at the undo floor — no
        distinct-older content remains.
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            target = history.undo_target(conn, deck_uuid)
        if target is None:
            return None
        prior, prior_seq = target
        self._write_edit(deck_uuid, prior, rationale='undo', is_undo=True)
        with self._connect() as conn:
            _ensure_decks_table(conn)
            history.set_undo_cursor(conn, deck_uuid, prior_seq)
        return prior

    def undo_cursor(self, deck_uuid: str) -> int | None:
        """Return the persisted undo-cursor seq (or None at head) — r8-m4 rollback snapshot."""
        with self._connect() as conn:
            _ensure_decks_table(conn)
            return history.get_undo_cursor(conn, deck_uuid)

    def restore_undo_cursor(self, deck_uuid: str, cursor: int | None) -> None:
        """Reset the undo cursor to a prior snapshot after a REFUSED undo commit (r8-m4).

        A drift/dead-binding-refused ``undo-deck`` rolls the CONTENT back
        (:meth:`rollback_failed_edit` pops the restored head version), but the cursor
        had already been advanced to the restored version's seq — so the next undo
        would SKIP the refused step. This resets the cursor to what it was BEFORE the
        refused undo (``None`` restores it to head), so no step is silently burned.
        """
        with self._connect() as conn:
            _ensure_decks_table(conn)
            if cursor is None:
                history.reset_undo_cursor(conn, deck_uuid)
            else:
                history.set_undo_cursor(conn, deck_uuid, cursor)
