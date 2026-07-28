"""Airtable records `CollectionStore` adapter — record-level CRUD over httpx.

This is the Airtable-mode implementation of the `CollectionStore` port. Unlike
the bulk-ETL `sources/airtable.py` (whole-table pull) and `destinations/airtable.py`
(derived write-back), this adapter does RECORD-LEVEL CRUD: read one deck, add one
inventory card, log one trade — the granular operations the skills drive.

Delta D1 (authoritative): the whole Airtable layer is **httpx**, not pyairtable.
This module mirrors that choice and reuses:
    - `config.AirtableResolver` for name -> per-base `tbl…`/`fld…` id resolution
      (via a GET-only meta client), so writes/reads key on stable field ids
      (``returnFieldsByFieldId=true`` / ``use_field_ids`` semantics).
    - The `destinations/airtable.py` guarded-write ethos: writes are OPT-IN. The
      adapter is constructed read-only by default; a mutating call raises
      :class:`ReadOnlyStoreError` unless the adapter was built with
      ``writes_enabled=True``.

Hydration sources (design note): they differ by read.
    - INVENTORY / chase / trade reads hydrate the base-`Card` portion DIRECTLY
      from the Airtable enrichment columns (Card Type, CMC, Mana Cost, Oracle
      Text, Color Identity, …) — **no** `CardResolver`.
    - DECK reads via ``get_deck`` hydrate each card through the injected
      `CardResolver` (Scryfall -> oracle_id + full enrichment): the Decks link
      fields only carry names/record-ids, and the fact sheet needs oracle_id.
    - ``list_decks`` (list / copy) is NAME-ONLY — it makes NO resolver calls, so
      listing stays O(rows) instead of O(rows*cards) paced lookups.

Deck reconstruction (design): a `Deck.cards` list is rebuilt from
    - the ``Commander`` link  -> `DeckCard(role='commander')`
    - the ``Cards`` link       -> maindeck `DeckCard`s (quantity 1 each)
    - the basic-land count fields (Plains/Islands/…) -> a `DeckCard` per nonzero
      count, with that quantity
    - ``Repeat Cards Count`` is carried on the deck for fidelity (the per-card
      multiplicity is not recoverable from the rollup alone; see decks.md).
Link fields hold Airtable RECORD IDs, so the adapter builds a
``{record_id: card_name}`` map from the Inventory Cards table once per deck read.
The write path resolves the INVERSE (``{card_name: record_id}`` /
``{deck_name: record_id}``) so link fields (Commander/Cards on Decks; From/To
(Deck) + Cards into/out of Destination on Trades; Target Decks on Chase) are
written as lists of record ids.

Inherent divergence from the local YAML adapter (design note): the live Chase
Cards table has **no** ``Priority`` / ``Status`` / ``Target Price`` columns, so
``add_chase``'s ``priority`` / ``status`` / ``target_price`` arguments are
**not persistable** here and are silently skipped (the local YAML adapter, which
is schema-free, DOES retain them). ``add_chase`` returns a human-readable note
listing any such dropped fields so a caller can surface the limitation. It DOES,
however, carry the nine Scryfall-derived columns (Card Type … Price (TCGPlayer) …
Color Identity) — verified on the live base — which the inline chase derived-write
(5b-3) refreshes after each chase mutation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from pipeline.collection.errors import CollectionError
from pipeline.config import AirtableConfigError, AirtableResolver, get_settings
from pipeline.contracts import ChaseCard, Deck, DeckCard, OwnedCard, Trade

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pipeline.collection.store import CardResolver

log = logging.getLogger('make_magic.collection.airtable')

API_ROOT = 'https://api.airtable.com/v0'
META_ROOT = 'https://api.airtable.com/v0/meta'
HEADERS_UA = {'User-Agent': 'make-magic-plugin/2.0'}

#: The six Airtable basic-land count fields on Decks, in canonical order. Each
#: maps its field NAME to the basic-land card NAME the count represents.
BASIC_LAND_FIELDS: tuple[tuple[str, str], ...] = (
    ('Plains', 'Plains'),
    ('Islands', 'Island'),
    ('Swamps', 'Swamp'),
    ('Mountains', 'Mountain'),
    ('Forests', 'Forest'),
    ('Wastes', 'Wastes'),
)

#: ``card name -> Decks count-field name`` for the six basic lands. Basic lands
#: are persisted as NUMBER counts on Decks, not as ``Cards`` link rows.
BASIC_LAND_TO_FIELD: dict[str, str] = {land: field for field, land in BASIC_LAND_FIELDS}

__all__ = ('AirtableCollectionStore', 'ReadOnlyStoreError')


class ReadOnlyStoreError(RuntimeError):
    """Raised when a mutating call is made on a read-only Airtable store.

    Writes are OPT-IN (mirroring the ``destinations/airtable.py`` guarded-write
    ethos): construct the adapter with ``writes_enabled=True`` to permit them.
    """


class _RecordClient:
    """A thin httpx record-CRUD client for one Airtable base (GET always; POST/
    PATCH/DELETE only when ``writes_enabled``).

    Also satisfies :class:`~pipeline.config.SupportsMetaTables` so an
    :class:`~pipeline.config.AirtableResolver` can be built from it — one meta
    call per run resolves every table/field NAME to its per-base id.

    The wrapped ``httpx.Client`` is name-mangled (private) to deter accidental
    bypass of the write guard (a deterrent, not a hard boundary).
    """

    def __init__(
        self,
        token: str,
        *,
        base_id: str,
        writes_enabled: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.__client = client or httpx.Client(timeout=30)
        self.__auth = {'Authorization': f'Bearer {token}', **HEADERS_UA}
        self._base_id = base_id
        self._writes_enabled = writes_enabled
        #: Retained so the inline derived-column write (#5, 5b-2) can build its
        #: guarded ``AllowlistWriteClient`` REUSING this same httpx connection and
        #: PAT — no per-mutation client/connection churn. Read-only handles.
        self._token = token

    @property
    def httpx_client(self) -> httpx.Client:
        """The underlying httpx client, exposed for connection REUSE only.

        The inline derived-column write path (#5) constructs its own guarded
        ``AllowlistWriteClient`` over this same connection so a collection
        mutation does not open a second socket. The write guard is enforced by
        that client, not here; this is purely a connection handle.
        """
        return self.__client

    # --- meta (SupportsMetaTables) ------------------------------------------ #

    def get_meta_tables(self, base_id: str) -> dict[str, Any]:
        resp = self.__client.get(f'{META_ROOT}/bases/{base_id}/tables', headers=self.__auth)
        resp.raise_for_status()
        return resp.json()

    # --- reads -------------------------------------------------------------- #

    def list_records(
        self,
        table_id: str,
        *,
        filter_by_formula: str | None = None,
        fields: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List all records (paginated) for a table, keyed by FIELD ID.

        ``fields`` narrows the columns fetched (query efficiency lives HERE, not
        in the skills); ``filter_by_formula`` scopes the rows. Both are the
        adapter's job so a skill never hand-rolls a formula.
        """
        url = f'{API_ROOT}/{self._base_id}/{table_id}'
        params: list[tuple[str, Any]] = [('pageSize', '100'), ('returnFieldsByFieldId', 'true')]
        if filter_by_formula:
            params.append(('filterByFormula', filter_by_formula))
        for f in fields or []:
            params.append(('fields[]', f))
        rows: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            page = list(params)
            if offset:
                page.append(('offset', offset))
            resp = self.__client.get(url, params=page, headers=self.__auth)
            resp.raise_for_status()
            payload = resp.json()
            rows.extend(payload.get('records', []))
            offset = payload.get('offset')
            if not offset:
                break
        return rows

    # --- writes (opt-in) ---------------------------------------------------- #

    def _require_writes(self) -> None:
        if not self._writes_enabled:
            raise ReadOnlyStoreError(
                'This Airtable collection store is READ-ONLY. Construct it with '
                'writes_enabled=True to permit create/update/delete.'
            )

    def create_record(self, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self._require_writes()
        url = f'{API_ROOT}/{self._base_id}/{table_id}'
        body = {'fields': fields, 'returnFieldsByFieldId': True, 'typecast': True}
        resp = self.__client.post(url, json=body, headers=self.__auth)
        resp.raise_for_status()
        return resp.json()

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self._require_writes()
        url = f'{API_ROOT}/{self._base_id}/{table_id}'
        body = {
            'records': [{'id': record_id, 'fields': fields}],
            'returnFieldsByFieldId': True,
            'typecast': True,
        }
        resp = self.__client.patch(url, json=body, headers=self.__auth)
        resp.raise_for_status()
        return resp.json()['records'][0]

    def delete_record(self, table_id: str, record_id: str) -> None:
        self._require_writes()
        url = f'{API_ROOT}/{self._base_id}/{table_id}'
        resp = self.__client.request('DELETE', url, params=[('records[]', record_id)], headers=self.__auth)
        resp.raise_for_status()

    def close(self) -> None:
        self.__client.close()


def _first(value: Any) -> Any:
    """Airtable multi-value fields come back as lists; take the first scalar."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _as_list(value: Any) -> list[str]:
    """Normalize an Airtable field to a list of strings (scalars -> single-item)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


class AirtableCollectionStore:
    """The Airtable-records implementation of `CollectionStore`.

    Inventory / chase / trade reads hydrate from the Airtable enrichment columns
    (no `CardResolver`). Deck reads via ``get_deck`` hydrate cards through the
    injected `CardResolver`; ``list_decks`` is name-only (no resolver calls).
    Writes are opt-in (see :class:`ReadOnlyStoreError`).
    """

    # Inventory Cards field NAMES (resolved to ids at runtime).
    _INV_NAME: ClassVar[str] = 'Card Name'
    _INV_OWNED: ClassVar[str] = 'Number Owned'
    _INV_FOIL: ClassVar[str] = 'Foil Count'
    _INV_CONDITION: ClassVar[str] = 'Condition'
    _INV_SETS: ClassVar[str] = 'Sets'
    _INV_SOURCES: ClassVar[str] = 'Sources'
    _INV_TYPE: ClassVar[str] = 'Card Type'
    _INV_CMC: ClassVar[str] = 'CMC'
    _INV_MANA_COST: ClassVar[str] = 'Mana Cost'
    _INV_ORACLE: ClassVar[str] = 'Oracle Text'
    _INV_COLOR_ID: ClassVar[str] = 'Color Identity'

    # Decks field NAMES.
    _DECK_NAME: ClassVar[str] = 'Name'
    _DECK_STRATEGY: ClassVar[str] = 'Strategy'
    _DECK_ASSESSMENT: ClassVar[str] = 'Assessment'
    _DECK_FOCUS: ClassVar[str] = 'Focus Otags'
    _DECK_COMMANDER: ClassVar[str] = 'Commander'
    _DECK_CARDS: ClassVar[str] = 'Cards'
    _DECK_REPEAT: ClassVar[str] = 'Repeat Cards Count'

    # Chase Cards field NAMES.
    _CHASE_NAME: ClassVar[str] = 'Card Name'
    _CHASE_TYPE: ClassVar[str] = 'Card Type'
    _CHASE_CMC: ClassVar[str] = 'CMC'
    _CHASE_MANA_COST: ClassVar[str] = 'Mana Cost'
    _CHASE_ORACLE: ClassVar[str] = 'Oracle Text'
    _CHASE_COLOR_ID: ClassVar[str] = 'Color Identity'
    _CHASE_TARGET_DECKS: ClassVar[str] = 'Target Decks'

    # Trades field NAMES.
    _TRADE_DATE: ClassVar[str] = 'Date'
    _TRADE_FROM_SRC: ClassVar[str] = 'From (Source)'
    _TRADE_TO_DST: ClassVar[str] = 'To (Destination)'
    _TRADE_FROM_DECK: ClassVar[str] = 'From (Deck)'
    _TRADE_TO_DECK: ClassVar[str] = 'To (Deck)'
    _TRADE_CARDS_IN: ClassVar[str] = 'Cards into Destination'
    _TRADE_CARDS_OUT: ClassVar[str] = 'Cards out of Destination'
    _TRADE_STATUS: ClassVar[str] = 'Status'
    _TRADE_COMPLETED: ClassVar[str] = 'Completed Date'
    #: The live base has separate ``Reason`` (singleLineText) and ``Notes``
    #: (multilineText) columns; the free-text ``Trade.notes`` maps to ``Notes``.
    _TRADE_NOTES: ClassVar[str] = 'Notes'

    def __init__(
        self,
        client: _RecordClient,
        resolver: AirtableResolver,
        *,
        cards_table: str,
        decks_table: str,
        trades_table: str,
        chase_table: str,
        card_resolver: CardResolver | None = None,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._cards_table = cards_table
        self._decks_table = decks_table
        self._trades_table = trades_table
        self._chase_table = chase_table
        # DECK cards are hydrated via a `CardResolver` (Scryfall -> oracle_id +
        # full enrichment), same as the local adapter — the Airtable link fields
        # only carry names/record-ids, and the fact sheet needs oracle_id (otags)
        # + type/CMC/oracle_text. (Inventory rows carry enrichment but no
        # oracle_id, so a row-join alone can't feed the otag layer.) Tests inject
        # a stub; #5 swaps the default for a pipeline-backed resolver.
        if card_resolver is None:
            from pipeline.collection.resolver import default_card_resolver

            card_resolver = default_card_resolver()
        self._card_resolver = card_resolver
        #: Lazily-built guarded derived-column writer (#5, 5b-2), reusing THIS
        #: adapter's httpx connection + PAT. Built on the first inline write so a
        #: read-only store never constructs it.
        self._derived_writer: Any | None = None
        #: Card-dim resolver + live price fetcher for the inline derived write,
        #: injected LAZILY (defaults to the package card dim). Held so tests can
        #: substitute stubs without a real lake / network.
        self._derived_resolver: Any | None = None
        self._derived_price_fetcher: Any | None = None
        #: Lazily-built CHASE-bound guarded derived-column writer (#5, 5b-3),
        #: reusing THIS adapter's httpx connection + PAT. Distinct from the owned
        #: writer above because it binds to a DIFFERENT table (Chase Cards) with a
        #: DIFFERENT allowlist/denylist. Built on the first inline chase write.
        self._chase_derived_writer: Any | None = None

    # --- inline derived-column write (#5 / 5b-2) ----------------------------- #

    def _write_derived_inline(self, name: str, record_id: str | None) -> None:
        """Persist the Scryfall-DERIVED columns for ONE card, INLINE, best-effort.

        Called AFTER a collection MUTATION (owned/chase add or update) has
        persisted its human/owned facts. It writes the nine card-dim derived
        columns (Card Type, Mana Cost, CMC, Power / Toughness, Oracle Text, Card
        Art, Scryfall URL, Price (TCGPlayer), Color Identity) for THAT card via
        the 5b-1 primitive :func:`destinations.airtable.write_derived_fields`,
        following the mutation's apply semantics (``apply=True``, NOT dry-run — a
        user-initiated mutation, not a bulk refresh).

        Safety properties:
            - The 5b-1 primitive SELF-GUARDS on ``resolve_backend()``: in local
              mode it is a strict NO-OP (zero Airtable calls). This adapter is the
              airtable path, but the primitive's guard is the enforcement.
            - It writes ONLY the allowlisted derived columns — NEVER any human /
              owned field #6 wrote (the primitive's 5a guard enforces the
              derived-vs-human partition on every payload + at the wire).
            - FAIL-OPEN on the derived side: a resolve failure or transport error
              is loud-logged and SWALLOWED so it never breaks the collection
              mutation whose owned/chase facts already succeeded.

        A missing ``record_id`` (the mutation did not surface one) is a no-op.
        """
        if not record_id:
            return
        try:
            from pipeline.destinations import airtable as _wb

            client = self._ensure_derived_writer()
            resolver = self._ensure_derived_resolver()
            price_fetcher = self._ensure_derived_price_fetcher()
            _wb.write_derived_fields(
                {record_id: name},
                resolver=resolver,
                price_fetcher=price_fetcher,
                client=client,
                apply=True,
                dry_run=False,
            )
        except Exception as exc:  # fail-open: the collection mutation already succeeded.
            log.warning(
                'Inline derived-column write for %r (record %s) failed and was skipped '
                '(owned/chase facts persisted; derived write is best-effort): %s',
                name,
                record_id,
                exc,
            )

    def _ensure_derived_writer(self) -> Any:
        """Build (once) the guarded derived-column writer over the SHARED httpx client.

        Reuses THIS adapter's httpx connection + PAT so an inline write does not
        open a second socket per mutation (5b-1 connection-churn concern). The
        writer is an ``AllowlistWriteClient`` — the same structural allowlist +
        wire guard the bulk refresh uses.
        """
        if self._derived_writer is None:
            from pipeline.destinations.airtable import AllowlistWriteClient

            self._derived_writer = AllowlistWriteClient(
                self._token,
                _client=self._client.httpx_client,
            )
        return self._derived_writer

    def _ensure_derived_resolver(self) -> Any:
        """The card-dim resolver used for inline derived writes (lazy default)."""
        if self._derived_resolver is None:
            from pipeline.destinations.airtable import default_card_resolver

            self._derived_resolver = default_card_resolver()
        return self._derived_resolver

    def _ensure_derived_price_fetcher(self) -> Any:
        """The LIVE price fetcher used for inline derived writes (lazy default)."""
        if self._derived_price_fetcher is None:
            from pipeline.destinations.airtable import _live_price_fetcher

            self._derived_price_fetcher = _live_price_fetcher()
        return self._derived_price_fetcher

    # --- inline CHASE derived-column write (#5 / 5b-3) ----------------------- #

    def _write_chase_derived_inline(self, name: str, record_id: str | None) -> None:
        """Persist the ELEVEN Chase Cards DERIVED columns for ONE card, INLINE, best-effort.

        The Chase Cards analogue of :meth:`_write_derived_inline`. Called AFTER a
        chase MUTATION (``add_chase`` create/update) has persisted its human/owned
        chase facts (Card Name + Target Decks). The Chase table now carries the SAME
        eleven engine-derived columns as Inventory: the nine Scryfall-derived columns
        (Card Type, Mana Cost, CMC, Power / Toughness, Oracle Text, Card Art, Scryfall
        URL, Price (TCGPlayer), Color Identity — including a LIVE price) PLUS the two
        engine ⚙ otag fields (⚙ Buckets / ⚙ Otags), so it writes all eleven for THAT
        card via the primitive :func:`destinations.airtable.write_chase_derived_fields`,
        following the mutation's apply semantics (``apply=True``, NOT dry-run). Unlike
        Inventory (whose ⚙ come from the separate otag SYNC), Chase has NO otag sync,
        so this inline write is chase's ONLY path to the two ⚙ fields.

        Safety properties (mirroring the owned hook):
            - The primitive SELF-GUARDS on ``resolve_backend()``: in local mode it
              is a strict NO-OP (zero Airtable calls).
            - It writes ONLY the eleven chase-allowlisted derived columns — NEVER any
              chase human field (#6 wrote Card Name / Target Decks). The CHASE-bound
              guard (:func:`assert_no_chase_human_fields` + the chase wire guard)
              enforces the derived-vs-human partition on the Chase table.
            - FAIL-OPEN on the derived side: a resolve failure or transport error is
              loud-logged and SWALLOWED so it never breaks the chase mutation whose
              chase facts already succeeded.

        A missing ``record_id`` is a no-op.
        """
        if not record_id:
            return
        try:
            from pipeline.destinations import airtable as _wb

            client = self._ensure_chase_derived_writer()
            resolver = self._ensure_derived_resolver()
            price_fetcher = self._ensure_derived_price_fetcher()
            _wb.write_chase_derived_fields(
                {record_id: name},
                resolver=resolver,
                price_fetcher=price_fetcher,
                client=client,
                apply=True,
                dry_run=False,
            )
        except Exception as exc:  # fail-open: the chase mutation already succeeded.
            log.warning(
                'Inline chase derived-column write for %r (record %s) failed and was skipped '
                '(chase facts persisted; derived write is best-effort): %s',
                name,
                record_id,
                exc,
            )

    def _ensure_chase_derived_writer(self) -> Any:
        """Build (once) the CHASE-bound guarded derived writer over the SHARED httpx client.

        Reuses THIS adapter's httpx connection + PAT (no second socket). The writer
        is an ``AllowlistWriteClient`` bound to the CHASE table: its
        ``cards_table_name`` is the Chase Cards table, and its allowlist / denylist /
        guard are the CHASE ones — so the wrong-table guard binds to Chase Cards and
        only the nine chase derived columns may ever be written.
        """
        if self._chase_derived_writer is None:
            from pipeline.destinations.airtable import (
                CHASE_ALLOWLIST_NAMES,
                CHASE_PROBE_FIELDS,
                AllowlistWriteClient,
                _chase_human_denylist,
                assert_no_chase_human_fields,
            )

            self._chase_derived_writer = AllowlistWriteClient(
                self._token,
                cards_table_name=self._chase_table,
                allowlist=CHASE_ALLOWLIST_NAMES,
                denylist_fn=_chase_human_denylist,
                guard=assert_no_chase_human_fields,
                probe_fields=CHASE_PROBE_FIELDS,
                _client=self._client.httpx_client,
            )
        return self._chase_derived_writer

    @property
    def _token(self) -> str:
        return self._client._token

    # --- construction -------------------------------------------------------- #

    @classmethod
    def from_settings(
        cls,
        token: str,
        *,
        writes_enabled: bool = False,
        client: httpx.Client | None = None,
        card_resolver: CardResolver | None = None,
    ) -> AirtableCollectionStore:
        """Build the store from env-driven `Settings` (base id + table names).

        ``client`` is an injectable ``httpx.Client`` (tests pass one wired to a
        ``MockTransport`` — no network, no creds). ``card_resolver`` is the deck-card
        hydration seam (tests pass a stub so deck reads make no Scryfall calls); it
        defaults to the package resolver.
        """
        settings = get_settings()
        record_client = _RecordClient(
            token,
            base_id=settings.airtable_base_id,
            writes_enabled=writes_enabled,
            client=client,
        )
        resolver = AirtableResolver(record_client, base_id=settings.airtable_base_id)
        return cls(
            record_client,
            resolver,
            cards_table=settings.cards_table,
            decks_table=settings.decks_table,
            trades_table=settings.trades_table,
            chase_table=settings.chase_table,
            card_resolver=card_resolver,
        )

    # --- id/name helpers ----------------------------------------------------- #

    def _fid(self, table: str, field: str) -> str:
        return self._resolver.field_id(table, field)

    def _get(self, table: str, rec: dict[str, Any], field: str) -> Any:
        """Read a field from a field-id-keyed record's ``fields`` dict."""
        return (rec.get('fields') or {}).get(self._fid(table, field))

    def _get_optional(self, table: str, rec: dict[str, Any], field: str) -> Any:
        """Read a field that may be ABSENT from this base's schema.

        Skill-authored deck fields (``Assessment``, ``Focus Otags``) do not exist
        on every base — the live base has ``Strategy`` but neither. Resolving a
        missing field id raises :class:`AirtableConfigError`; here we treat that as
        "field not present" and return ``None`` instead of crashing the read. The
        write path stays strict (a clear error if you try to write a field the base
        lacks).
        """
        try:
            fid = self._fid(table, field)
        except AirtableConfigError:
            return None
        return (rec.get('fields') or {}).get(fid)

    # --- link resolution (name -> record id) --------------------------------- #

    def _inventory_id_map(self) -> dict[str, str]:
        """``{card_name: record_id}`` for the Inventory Cards table (write side)."""
        table_id = self._resolver.table_id(self._cards_table)
        rows = self._client.list_records(table_id, fields=[self._INV_NAME])
        out: dict[str, str] = {}
        for r in rows:
            name = self._get(self._cards_table, r, self._INV_NAME)
            if r.get('id') and name is not None:
                out[str(name)] = r['id']
        return out

    def _deck_id_map(self) -> dict[str, str]:
        """``{deck_name: record_id}`` for the Decks table (write side)."""
        table_id = self._resolver.table_id(self._decks_table)
        rows = self._client.list_records(table_id, fields=[self._DECK_NAME])
        out: dict[str, str] = {}
        for r in rows:
            name = self._get(self._decks_table, r, self._DECK_NAME)
            if r.get('id') and name is not None:
                out[str(name)] = r['id']
        return out

    @staticmethod
    def _resolve_links(names: Iterable[str], name_map: dict[str, str], *, kind: str) -> list[str]:
        """Resolve card/deck NAMES to a list of record ids via ``name_map``.

        Link fields are written as lists of record ids. If any name is not in the
        base, raise a clear :class:`ValueError` listing the unresolved names (never
        silently drop) — the caller must add them to the base first.
        """
        ids: list[str] = []
        missing: list[str] = []
        for name in names:
            rid = name_map.get(name)
            if rid is None:
                missing.append(name)
            else:
                ids.append(rid)
        if missing:
            raise CollectionError(
                f'Cannot resolve {kind} link(s) — not found in the Airtable base: '
                f'{missing!r}. Add them first (e.g. via add_card), then retry.'
            )
        return ids

    # --- Meta ---------------------------------------------------------------- #

    @property
    def backend_name(self) -> str:
        return 'airtable'

    # --- Inventory ----------------------------------------------------------- #

    def _row_to_owned(self, rec: dict[str, Any]) -> OwnedCard:
        t = self._cards_table
        return OwnedCard(
            name=str(self._get(t, rec, self._INV_NAME)),
            owned=int(self._get(t, rec, self._INV_OWNED) or 0),
            foil=int(self._get(t, rec, self._INV_FOIL) or 0),
            condition=_as_list(self._get(t, rec, self._INV_CONDITION)),
            sets=_as_list(self._get(t, rec, self._INV_SETS)),
            sources=_as_list(self._get(t, rec, self._INV_SOURCES)),
            # Enrichment hydrated DIRECTLY from the row (no CardResolver).
            type_line=self._get(t, rec, self._INV_TYPE),
            mana_value=self._get(t, rec, self._INV_CMC),
            mana_cost=self._get(t, rec, self._INV_MANA_COST),
            oracle_text=self._get(t, rec, self._INV_ORACLE),
            color_identity=_as_list(self._get(t, rec, self._INV_COLOR_ID)),
            airtable_record_id=rec.get('id'),
        )

    def list_inventory(self) -> list[OwnedCard]:
        table_id = self._resolver.table_id(self._cards_table)
        return [self._row_to_owned(r) for r in self._client.list_records(table_id)]

    def _find_inventory_record(self, ref: str) -> dict[str, Any] | None:
        table_id = self._resolver.table_id(self._cards_table)
        formula = f"{{{self._INV_NAME}}} = '{_escape(ref)}'"
        rows = self._client.list_records(table_id, filter_by_formula=formula)
        return rows[0] if rows else None

    def add_card(
        self,
        ref: str,
        qty: int = 1,
        *,
        condition: list[str] | None = None,
        foil: int = 0,
        sets: list[str] | None = None,
        sources: list[str] | None = None,
    ) -> None:
        table_id = self._resolver.table_id(self._cards_table)
        existing = self._find_inventory_record(ref)
        if existing is None:
            fields: dict[str, Any] = {self._fid(self._cards_table, self._INV_NAME): ref}
            self._set_owned_fields(fields, qty, condition, foil, sets, sources)
            created = self._client.create_record(table_id, fields)
            # INLINE derived-column write (#5): follow the owned-facts mutation.
            self._write_derived_inline(ref, created.get('id'))
            return
        cur_owned = int(self._get(self._cards_table, existing, self._INV_OWNED) or 0)
        cur_foil = int(self._get(self._cards_table, existing, self._INV_FOIL) or 0)
        fields = {self._fid(self._cards_table, self._INV_OWNED): cur_owned + qty}
        if foil:
            fields[self._fid(self._cards_table, self._INV_FOIL)] = cur_foil + foil
        if condition:
            fields[self._fid(self._cards_table, self._INV_CONDITION)] = condition
        if sets:
            fields[self._fid(self._cards_table, self._INV_SETS)] = sets
        if sources:
            fields[self._fid(self._cards_table, self._INV_SOURCES)] = sources
        self._client.update_record(table_id, existing['id'], fields)
        # INLINE derived-column refresh (#5): follow the owned-facts update.
        self._write_derived_inline(ref, existing['id'])

    def _set_owned_fields(
        self,
        fields: dict[str, Any],
        qty: int,
        condition: list[str] | None,
        foil: int,
        sets: list[str] | None,
        sources: list[str] | None,
    ) -> None:
        fields[self._fid(self._cards_table, self._INV_OWNED)] = qty
        if foil:
            fields[self._fid(self._cards_table, self._INV_FOIL)] = foil
        if condition:
            fields[self._fid(self._cards_table, self._INV_CONDITION)] = condition
        if sets:
            fields[self._fid(self._cards_table, self._INV_SETS)] = sets
        if sources:
            fields[self._fid(self._cards_table, self._INV_SOURCES)] = sources

    def set_quantity(self, ref: str, qty: int) -> None:
        table_id = self._resolver.table_id(self._cards_table)
        existing = self._find_inventory_record(ref)
        owned_fid = self._fid(self._cards_table, self._INV_OWNED)
        if existing is None:
            created = self._client.create_record(
                table_id,
                {self._fid(self._cards_table, self._INV_NAME): ref, owned_fid: qty},
            )
            self._write_derived_inline(ref, created.get('id'))
            return
        self._client.update_record(table_id, existing['id'], {owned_fid: qty})
        self._write_derived_inline(ref, existing['id'])

    def remove_card(self, ref: str) -> None:
        table_id = self._resolver.table_id(self._cards_table)
        existing = self._find_inventory_record(ref)
        if existing is not None:
            self._client.delete_record(table_id, existing['id'])

    # --- Chase --------------------------------------------------------------- #

    def _row_to_chase(self, rec: dict[str, Any], deck_map: dict[str, str]) -> ChaseCard:
        t = self._chase_table
        return ChaseCard(
            name=str(self._get(t, rec, self._CHASE_NAME)),
            type_line=self._get(t, rec, self._CHASE_TYPE),
            mana_value=self._get(t, rec, self._CHASE_CMC),
            mana_cost=self._get(t, rec, self._CHASE_MANA_COST),
            oracle_text=self._get(t, rec, self._CHASE_ORACLE),
            color_identity=_as_list(self._get(t, rec, self._CHASE_COLOR_ID)),
            # Target Decks is a link -> resolve record ids back to deck names.
            for_decks=[deck_map.get(rid, rid) for rid in _as_list(self._get(t, rec, self._CHASE_TARGET_DECKS))],
            airtable_record_id=rec.get('id'),
        )

    def list_chase(self) -> list[ChaseCard]:
        table_id = self._resolver.table_id(self._chase_table)
        deck_map = self._deck_name_map()
        return [self._row_to_chase(r, deck_map) for r in self._client.list_records(table_id)]

    def _find_chase_record(self, ref: str) -> dict[str, Any] | None:
        table_id = self._resolver.table_id(self._chase_table)
        formula = f"{{{self._CHASE_NAME}}} = '{_escape(ref)}'"
        rows = self._client.list_records(table_id, filter_by_formula=formula)
        return rows[0] if rows else None

    def add_chase(
        self,
        ref: str,
        *,
        priority: int | None = None,
        for_deck: str | None = None,
        status: str | None = None,
        target_price: float | None = None,
    ) -> str | None:
        """Add/update a Chase Cards record.

        Writes ``Card Name`` and, when ``for_deck`` is given, appends the deck to
        the ``Target Decks`` link (deck name -> Decks record id). An EXISTING
        record is UPDATEd (its Target Decks are extended, not clobbered).

        ``priority`` / ``status`` / ``target_price`` have NO column on this base
        (the local YAML adapter retains them; Airtable cannot) — they are skipped
        rather than written (which would raise). Returns a human-readable note
        naming any skipped fields, or ``None`` when nothing was dropped.
        """
        table_id = self._resolver.table_id(self._chase_table)
        t = self._chase_table
        existing = self._find_chase_record(ref)

        target_ids: list[str] = []
        if for_deck is not None:
            target_ids = self._resolve_links([for_deck], self._deck_id_map(), kind='target deck')

        record_id: str | None
        if existing is None:
            fields: dict[str, Any] = {self._fid(t, self._CHASE_NAME): ref}
            if target_ids:
                fields[self._fid(t, self._CHASE_TARGET_DECKS)] = target_ids
            created = self._client.create_record(table_id, fields)
            record_id = created.get('id')
        else:
            record_id = existing['id']
            if target_ids:
                current = _as_list(self._get(t, existing, self._CHASE_TARGET_DECKS))
                merged = current + [rid for rid in target_ids if rid not in current]
                self._client.update_record(table_id, existing['id'], {self._fid(t, self._CHASE_TARGET_DECKS): merged})

        # INLINE CHASE derived-column write (#5): follow the chase-facts mutation.
        # Writes the ELEVEN chase derived columns (nine Scryfall — Card Type, Mana
        # Cost, CMC, Power / Toughness, Oracle Text, Card Art, Scryfall URL, Price
        # (TCGPlayer), Color Identity — PLUS ⚙ Buckets / ⚙ Otags) for this card via
        # the CHASE-bound guarded primitive — best-effort / fail-open, backend-
        # guarded, never touching a chase human field. Chase has NO otag sync, so
        # this inline write is chase's only path to the two ⚙ fields.
        self._write_chase_derived_inline(ref, record_id)

        skipped = [
            label
            for label, value in (('priority', priority), ('status', status), ('target_price', target_price))
            if value is not None
        ]
        if skipped:
            return (
                f'Note: {", ".join(skipped)} not persisted — the Airtable Chase Cards '
                f'table has no matching column (these are retained only in local mode).'
            )
        return None

    def remove_chase(self, ref: str) -> None:
        table_id = self._resolver.table_id(self._chase_table)
        existing = self._find_chase_record(ref)
        if existing is not None:
            self._client.delete_record(table_id, existing['id'])

    # --- Trades -------------------------------------------------------------- #

    def _row_to_trade(self, rec: dict[str, Any], inv_map: dict[str, str], deck_map: dict[str, str]) -> Trade:
        t = self._trades_table
        from_deck_ids = _as_list(self._get(t, rec, self._TRADE_FROM_DECK))
        to_deck_ids = _as_list(self._get(t, rec, self._TRADE_TO_DECK))
        return Trade(
            date=self._get(t, rec, self._TRADE_DATE),
            from_source=str(_first(self._get(t, rec, self._TRADE_FROM_SRC)) or ''),
            to_destination=str(_first(self._get(t, rec, self._TRADE_TO_DST)) or ''),
            # Deck links resolve back to deck names (record-id -> name).
            from_deck=deck_map.get(from_deck_ids[0], from_deck_ids[0]) if from_deck_ids else None,
            to_deck=deck_map.get(to_deck_ids[0], to_deck_ids[0]) if to_deck_ids else None,
            # Card links resolve back to card names (record-id -> name).
            cards_in=[inv_map.get(rid, rid) for rid in _as_list(self._get(t, rec, self._TRADE_CARDS_IN))],
            cards_out=[inv_map.get(rid, rid) for rid in _as_list(self._get(t, rec, self._TRADE_CARDS_OUT))],
            status=self._get(t, rec, self._TRADE_STATUS),
            completed_date=self._get(t, rec, self._TRADE_COMPLETED),
            notes=self._get(t, rec, self._TRADE_NOTES),
            airtable_record_id=rec.get('id'),
        )

    def list_trades(self) -> list[Trade]:
        table_id = self._resolver.table_id(self._trades_table)
        inv_map = self._inventory_name_map()
        deck_map = self._deck_name_map()
        return [self._row_to_trade(r, inv_map, deck_map) for r in self._client.list_records(table_id)]

    def log_trade(self, trade: Trade) -> None:
        """Create a Trades record with its category, date, status, notes AND links.

        ``cards_in`` / ``cards_out`` (card names) resolve to Inventory Cards record
        ids for the ``Cards into/out of Destination`` links; ``from_deck`` /
        ``to_deck`` (deck names) resolve to Decks record ids for the ``From/To
        (Deck)`` links. Unresolved names raise a clear ``ValueError``. ``notes``
        maps to the ``Notes`` column (consistent with the read path).
        """
        table_id = self._resolver.table_id(self._trades_table)
        t = self._trades_table
        fields: dict[str, Any] = {
            self._fid(t, self._TRADE_FROM_SRC): trade.from_source,
            self._fid(t, self._TRADE_TO_DST): trade.to_destination,
        }
        if trade.date is not None:
            fields[self._fid(t, self._TRADE_DATE)] = trade.date
        if trade.status is not None:
            fields[self._fid(t, self._TRADE_STATUS)] = trade.status
        if trade.completed_date is not None:
            fields[self._fid(t, self._TRADE_COMPLETED)] = trade.completed_date
        if trade.notes is not None:
            fields[self._fid(t, self._TRADE_NOTES)] = trade.notes

        if trade.cards_in or trade.cards_out:
            inv_map = self._inventory_id_map()
            if trade.cards_in:
                fields[self._fid(t, self._TRADE_CARDS_IN)] = self._resolve_links(
                    trade.cards_in, inv_map, kind='cards_in'
                )
            if trade.cards_out:
                fields[self._fid(t, self._TRADE_CARDS_OUT)] = self._resolve_links(
                    trade.cards_out, inv_map, kind='cards_out'
                )
        if trade.from_deck is not None or trade.to_deck is not None:
            deck_map = self._deck_id_map()
            if trade.from_deck is not None:
                fields[self._fid(t, self._TRADE_FROM_DECK)] = self._resolve_links(
                    [trade.from_deck], deck_map, kind='from_deck'
                )
            if trade.to_deck is not None:
                fields[self._fid(t, self._TRADE_TO_DECK)] = self._resolve_links(
                    [trade.to_deck], deck_map, kind='to_deck'
                )
        self._client.create_record(table_id, fields)

    # --- Decks --------------------------------------------------------------- #

    def _inventory_name_map(self) -> dict[str, str]:
        """``{record_id: card_name}`` for resolving deck link fields to names."""
        table_id = self._resolver.table_id(self._cards_table)
        rows = self._client.list_records(table_id, fields=[self._INV_NAME])
        out: dict[str, str] = {}
        for r in rows:
            name = self._get(self._cards_table, r, self._INV_NAME)
            if r.get('id') and name is not None:
                out[r['id']] = str(name)
        return out

    def _deck_name_map(self) -> dict[str, str]:
        """``{record_id: deck_name}`` for resolving Decks link fields to names.

        Used by the trade / chase read paths to turn ``From/To (Deck)`` and
        ``Target Decks`` record-id links back into deck names (the inverse of the
        write-side ``_deck_id_map``).
        """
        table_id = self._resolver.table_id(self._decks_table)
        rows = self._client.list_records(table_id, fields=[self._DECK_NAME])
        out: dict[str, str] = {}
        for r in rows:
            name = self._get(self._decks_table, r, self._DECK_NAME)
            if r.get('id') and name is not None:
                out[r['id']] = str(name)
        return out

    def _hydrate(self, name: str) -> dict[str, Any]:
        """Resolver enrichment for `name` as base-`Card` fields (name-only if unresolved).

        The Airtable link NAME is authoritative: a resolver fuzzy-match may return
        a slightly-different Scryfall name (e.g. ``Sol Rin`` -> ``Sol Ring``), so we
        override ``name`` with the original link name — enrichment (type/CMC/
        oracle_id) still comes from the resolved card, but no silent rename.
        """
        card = self._card_resolver.get_card(name)
        if card is None:
            return {'name': name}
        fields = card.model_dump()
        fields['name'] = name  # keep the authoritative Airtable link name
        return fields

    def _row_to_deck(self, rec: dict[str, Any], name_map: dict[str, str], *, hydrate: bool) -> Deck:
        """Reconstruct a `Deck` from a Decks row.

        ``hydrate`` gates per-card resolver lookups: ``get_deck`` passes ``True``
        (the fact sheet needs oracle_id + type/CMC/oracle_text); ``list_decks``
        passes ``False`` -> name-only DeckCards (name + role + quantity + basic-land
        type) with NO resolver calls, so list/copy stay O(rows), not O(rows*cards).
        """
        t = self._decks_table

        def _fields(name: str) -> dict[str, Any]:
            return self._hydrate(name) if hydrate else {'name': name}

        cards: list[DeckCard] = []
        for rid in _as_list(self._get(t, rec, self._DECK_COMMANDER)):
            cards.append(DeckCard(**_fields(name_map.get(rid, rid)), role='commander'))
        for rid in _as_list(self._get(t, rec, self._DECK_CARDS)):
            cards.append(DeckCard(**_fields(name_map.get(rid, rid))))
        for field_name, land_name in BASIC_LAND_FIELDS:
            count = int(self._get(t, rec, field_name) or 0)
            if count:
                # Basics carry a known land type so the fact sheet's land/nonland
                # split is correct without a resolver round-trip per basic.
                cards.append(DeckCard(name=land_name, quantity=count, type_line=f'Basic Land — {land_name}'))
        return Deck(
            name=str(self._get(t, rec, self._DECK_NAME)),
            strategy=self._get(t, rec, self._DECK_STRATEGY),
            assessment=self._get_optional(t, rec, self._DECK_ASSESSMENT),
            focus_otags=_as_list(self._get_optional(t, rec, self._DECK_FOCUS)),
            cards=cards,
            airtable_record_id=rec.get('id'),
        )

    def _find_deck_record(self, name: str) -> dict[str, Any] | None:
        table_id = self._resolver.table_id(self._decks_table)
        formula = f"{{{self._DECK_NAME}}} = '{_escape(name)}'"
        rows = self._client.list_records(table_id, filter_by_formula=formula)
        return rows[0] if rows else None

    def get_deck(self, name: str) -> Deck:
        rec = self._find_deck_record(name)
        if rec is None:
            raise FileNotFoundError(f'No Airtable Decks record named {name!r}.')
        return self._row_to_deck(rec, self._inventory_name_map(), hydrate=True)

    def list_decks(self) -> list[Deck]:
        table_id = self._resolver.table_id(self._decks_table)
        rows = self._client.list_records(table_id)
        name_map = self._inventory_name_map()
        # Name-only: list/copy need names/roles/quantities, not per-card enrichment.
        # Hydrating every card here is O(rows*cards) paced Scryfall lookups.
        return [self._row_to_deck(r, name_map, hydrate=False) for r in rows]

    def save_deck(self, deck: Deck) -> None:
        """Persist the WHOLE deck: metadata + full membership.

        Membership maps to the live Decks schema as:
            - ``Commander`` link  <- DeckCards with ``role == 'commander'``
            - ``Cards`` link      <- non-commander, non-basic-land DeckCards
            - the six basic-land NUMBER counts <- basic-land DeckCards (summed qty)
            - ``Repeat Cards Count`` <- sum of ``(quantity - 1)`` over non-basic,
              non-commander cards (the multiplicity a single link row can't hold)

        Non-basic cards must already exist in Inventory Cards (link fields are
        record ids) — an unresolved name raises a clear ``ValueError``. Basic
        lands are NUMBERS, so they never need an Inventory row.

        ``Strategy`` is only written when set (never clobbered with None).
        ``Assessment`` / ``Focus Otags`` are written when set; on a base lacking
        those columns that raises the clear "field not on base" error (correct).
        """
        table_id = self._resolver.table_id(self._decks_table)
        t = self._decks_table
        fields: dict[str, Any] = {self._fid(t, self._DECK_NAME): deck.name}
        if deck.strategy is not None:
            fields[self._fid(t, self._DECK_STRATEGY)] = deck.strategy
        if deck.assessment is not None:
            fields[self._fid(t, self._DECK_ASSESSMENT)] = deck.assessment
        if deck.focus_otags:
            fields[self._fid(t, self._DECK_FOCUS)] = list(deck.focus_otags)

        commander_names: list[str] = []
        card_names: list[str] = []
        basic_counts: dict[str, int] = {}
        repeat_count = 0
        for c in deck.cards:
            if c.name in BASIC_LAND_TO_FIELD:
                land_field = BASIC_LAND_TO_FIELD[c.name]
                basic_counts[land_field] = basic_counts.get(land_field, 0) + c.quantity
            elif c.role == 'commander':
                commander_names.append(c.name)
            else:
                card_names.append(c.name)
                repeat_count += max(c.quantity - 1, 0)

        if commander_names or card_names:
            inv_map = self._inventory_id_map()
            if commander_names:
                fields[self._fid(t, self._DECK_COMMANDER)] = self._resolve_links(
                    commander_names, inv_map, kind='commander card'
                )
            if card_names:
                fields[self._fid(t, self._DECK_CARDS)] = self._resolve_links(card_names, inv_map, kind='deck card')
        for field_name, count in basic_counts.items():
            fields[self._fid(t, field_name)] = count
        if repeat_count:
            fields[self._fid(t, self._DECK_REPEAT)] = repeat_count

        if deck.airtable_record_id:
            self._client.update_record(table_id, deck.airtable_record_id, fields)
        else:
            self._client.create_record(table_id, fields)

    def _set_deck_field(self, name: str, field: str, value: Any) -> None:
        rec = self._find_deck_record(name)
        if rec is None:
            raise FileNotFoundError(f'No Airtable Decks record named {name!r}.')
        table_id = self._resolver.table_id(self._decks_table)
        self._client.update_record(table_id, rec['id'], {self._fid(self._decks_table, field): value})

    def set_strategy(self, name: str, text: str) -> None:
        self._set_deck_field(name, self._DECK_STRATEGY, text)

    def set_assessment(self, name: str, text: str) -> None:
        self._set_deck_field(name, self._DECK_ASSESSMENT, text)

    def set_focus_otags(self, name: str, otags: list[str]) -> None:
        self._set_deck_field(name, self._DECK_FOCUS, list(otags))


def _escape(value: str) -> str:
    """Escape single quotes for an Airtable ``filterByFormula`` string literal."""
    return value.replace("'", "\\'")
