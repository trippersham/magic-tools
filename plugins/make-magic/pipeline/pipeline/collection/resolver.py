"""Package-native default `CardResolver` — a Scryfall name -> `Card` lookup.

This is the default hydration source for the local `CollectionStore`, so
`get_store()` returns a working store with **no injected resolver**: the package
never imports `scripts/`. The `CardResolver` port stays the swap-point.

This default's internals are a **DuckDB query over the `raw/oracle_cards` bulk**
joined with the `card_otag` rollup — the lake is the durable card dim. Resolution
is offline-first: a name present in the bulk resolves with zero network. A bulk
miss falls back to a single live Scryfall fetch (exact then fuzzy) whose result is
**landed durably** into `raw/oracle_cards`, so the next lookup for that name is
offline. Everything fails open: a missing lake, a missing otag layer, or a network
error degrades — an unresolved name returns None and the adapter reads the card
back name-only. It never crashes a consumer.

The live-fallback fetch paces requests to Scryfall's courtesy interval and
retries a 429/503 throttle (honoring ``Retry-After``, capped) so a bulk miss on a
brand-new set — where several cards fall through to live lookups in one run —
doesn't trip a wall of throttles and leave the deck un-hydrated. A transient
failure (network / timeout / non-404 HTTP) is distinguished from a definitive 404
so a transient miss is not landed (it degrades to name-only for this run and the
next lookup retries), while a definitive 404 simply resolves to None.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx

from pipeline import store
from pipeline.contracts import Card
from pipeline.transforms.crosswalk import buckets_for

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger('make_magic.collection.resolver')

_NAMED_URL = 'https://api.scryfall.com/cards/named'

#: The lake coordinates the resolver reads from (offline-first).
_ORACLE_CARDS = ('raw', 'oracle_cards')
_CARD_OTAG = ('normalized', 'card_otag')

#: The columns the resolver projects out of the widened oracle_cards bulk. Kept
#: explicit so a schema drift surfaces as a clear KeyError rather than a silent
#: null, and so the SELECT is stable across DuckDB column ordering.
_CARD_COLUMNS = (
    'oracle_id',
    'name',
    'cmc',
    'mana_cost',
    'type_line',
    'colors',
    'color_identity',
    'produced_mana',
    'keywords',
    'oracle_text',
    'power',
    'toughness',
    'art_crop',
    'scryfall_uri',
    'set_name',
)

#: Scryfall asks for ~50-100ms between requests; pace the live-fallback fetch to
#: that so a bulk-miss burst (a brand-new set with several cards not yet landed)
#: doesn't trip 429s (a wall of transient failures that leaves a deck
#: un-hydrated). Lake hits issue zero network, so this only paces the fallback.
_MIN_INTERVAL = 0.1
#: Bounded retries on a throttle/unavailable (429/503), honoring Retry-After.
_MAX_RETRIES = 3
_RETRYABLE_STATUS = frozenset({429, 503})
#: Cap any single retry sleep (seconds). A server ``Retry-After`` can be huge
#: (e.g. ``3600``) — honoring it verbatim would hang a ``get_card`` mid-read.
#: Cap it: after ``_MAX_RETRIES`` throttles the lookup still falls to name-only.
_MAX_BACKOFF = 5.0

#: Below this ``raw/oracle_cards`` row count the lake is treated as a seed/stub,
#: not a real card dim: the committed seed is a single row and a real Scryfall
#: oracle bulk is ~38k cards, so a floor comfortably between the two distinguishes
#: them. Scoring against a stub silently under-resolves — the guard refuses instead.
LAKE_STUB_FLOOR = 1000

__all__ = ('DuckDBCardResolver', 'default_card_resolver', 'fetch_card_raw', 'lake_row_count', 'lake_status')


class _Transient(Enum):
    """Sentinel: a live fetch failed TRANSIENTLY (network / timeout / non-404 HTTP).

    Distinct from ``None`` (a definitive 404 = card not found): a transient result
    must not be landed into the lake, so the next lookup retries rather than
    permanently stripping enrichment. Both degrade this run to a name-only card,
    but only a transient result is withheld from durable landing.
    """

    RESULT = 0


_TRANSIENT = _Transient.RESULT


def _card_from_scryfall(data: dict[str, Any]) -> Card:
    """Map a live Scryfall card dict -> `contracts.Card` (front face for DFCs).

    Used only on the live-fallback path (bulk miss); lake hits map from the
    projected row. The `otag_*` fields are left empty here — a freshly-landed
    card is not yet in the `card_otag` rollup, and the resolver fills otags only
    from the lake (fail-open empty otherwise).
    """
    face = data
    if data.get('oracle_text') is None and data.get('card_faces'):
        face = data['card_faces'][0]
    image_uris = data.get('image_uris') or face.get('image_uris') or {}
    return Card(
        name=data.get('name', ''),
        oracle_id=data.get('oracle_id'),
        mana_value=data.get('cmc'),
        mana_cost=data.get('mana_cost') or face.get('mana_cost'),
        type_line=data.get('type_line') or face.get('type_line'),
        colors=data.get('colors') or [],
        color_identity=data.get('color_identity') or [],
        produced_mana=data.get('produced_mana') or [],
        keywords=data.get('keywords') or [],
        oracle_text=face.get('oracle_text') or data.get('oracle_text'),
        power=data.get('power') if data.get('power') is not None else face.get('power'),
        toughness=data.get('toughness') if data.get('toughness') is not None else face.get('toughness'),
        art_crop=image_uris.get('art_crop'),
        scryfall_uri=data.get('scryfall_uri'),
        set_name=data.get('set_name'),
    )


#: List-valued oracle_cards columns whose `read_json` type inference is UNSTABLE:
#: a column that is empty (`[]`) in every row infers as `JSON[]`, not `VARCHAR[]`.
#: A later live-land then does `INSERT INTO _land (from read_parquet) SELECT (from
#: read_json)` where the two sides disagree on the element type — inserting a
#: `VARCHAR[]` value like `['W']` into a `JSON[]` column raises
#: `Conversion Error: Malformed JSON ... Input: "W"`, so the card fails to land and
#: the resolver falls back to "serving live only" forever (#50). Casting BOTH sides
#: to `VARCHAR[]` makes the types always agree. It is SAFE: `read_json` only infers
#: `JSON[]` when EVERY array is empty (no elements to type), so the cast never has a
#: real JSON string to unwrap — an all-empty `JSON[]` casts to an empty `VARCHAR[]`.
_LIST_COLUMNS = ('colors', 'color_identity', 'produced_mana', 'keywords')


def _land_projection() -> str:
    """The `_CARD_COLUMNS` projection with list columns pinned to ``VARCHAR[]``.

    Used on both sides of the landing (the read_parquet target table and the
    read_json insert source) so their list-column types can never disagree.
    """
    return ', '.join(f'CAST({c} AS VARCHAR[]) AS {c}' if c in _LIST_COLUMNS else c for c in _CARD_COLUMNS)


def _land_card(data: dict[str, Any]) -> None:
    """Durably append a live-fetched Scryfall card into `raw/oracle_cards`.

    Projects the card to the same column set the bulk puller writes (widened
    presentation fields included; price is not landed — served live), then
    inserts it into the existing table, or creates the table from the row if the
    bulk is absent. Fails open: a store/duckdb error is logged and swallowed so a
    landing failure never breaks resolution (the card is still returned live).
    """
    import json

    row = _project_scryfall(data)
    try:
        raw_dir = store.StorePaths.resolve().layer_dir('raw', create=True)
        tmp = raw_dir / '_oracle_cards_land.tmp.json'
        tmp.write_text(json.dumps([row]), encoding='utf-8')
        try:
            with store.connect() as conn:
                # List columns pinned to VARCHAR[] on BOTH sides so the read_parquet
                # target and the read_json source can't disagree on element type (#50).
                sel = _land_projection()
                if store.table_exists(*_ORACLE_CARDS):
                    path = store.StorePaths.resolve().parquet_path(*_ORACLE_CARDS, create=False)
                    # Materialize existing rows into an in-memory table first so the
                    # later COPY can safely overwrite the same file (no read-while-write).
                    conn.execute(f"CREATE TEMP TABLE _land AS SELECT {sel} FROM read_parquet('{path}')")
                    oid = row.get('oracle_id')
                    if oid is not None:
                        dup = conn.execute(
                            f"SELECT 1 FROM _land WHERE oracle_id = '{_sql_escape(str(oid))}' LIMIT 1"
                        ).fetchone()
                        if dup is not None:
                            return
                    conn.execute(f"INSERT INTO _land SELECT {sel} FROM read_json('{tmp}')")
                    store.write_parquet(conn, conn.table('_land'), *_ORACLE_CARDS)
                else:
                    new_rel = conn.read_json(str(tmp)).select(sel)
                    store.write_parquet(conn, new_rel, *_ORACLE_CARDS)
        finally:
            tmp.unlink(missing_ok=True)
    except Exception as exc:
        log.warning('card-dim: failed to land %r into the lake (%s); serving live only.', row.get('name'), exc)


def _project_scryfall(card: dict[str, Any]) -> dict[str, Any]:
    """Project a live Scryfall card to the `raw/oracle_cards` column set.

    Mirrors `sources.scryfall_bulk._project` so a landed live card is
    schema-compatible with the bulk rows (front face for DFCs on nested fields).
    """
    face = card
    if card.get('oracle_text') is None and card.get('card_faces'):
        face = card['card_faces'][0]
    image_uris = card.get('image_uris') or face.get('image_uris') or {}
    return {
        'oracle_id': card.get('oracle_id'),
        'name': card.get('name'),
        'cmc': card.get('cmc'),
        'mana_cost': card.get('mana_cost') or face.get('mana_cost'),
        'type_line': card.get('type_line') or face.get('type_line'),
        'colors': card.get('colors') or [],
        'color_identity': card.get('color_identity') or [],
        'produced_mana': card.get('produced_mana') or [],
        'keywords': card.get('keywords') or [],
        'oracle_text': face.get('oracle_text') or card.get('oracle_text'),
        'power': card.get('power') if card.get('power') is not None else face.get('power'),
        'toughness': card.get('toughness') if card.get('toughness') is not None else face.get('toughness'),
        'art_crop': image_uris.get('art_crop'),
        'scryfall_uri': card.get('scryfall_uri'),
        'set_name': card.get('set_name'),
    }


def _sql_escape(value: str) -> str:
    """Escape single quotes for a DuckDB string literal."""
    return value.replace("'", "''")


class DuckDBCardResolver:
    """The lake-backed `CardResolver`: a name -> enriched `Card` (or None).

    Structurally satisfies `pipeline.collection.store.CardResolver`. Reads
    `raw/oracle_cards` (offline-first, exact case-insensitive name match) and
    joins the `card_otag` rollup by `oracle_id` for `otags` / `otag_buckets`. A
    bulk miss falls back to a single live Scryfall fetch (exact then fuzzy) that
    is paced + retries a 429/503 throttle (honoring ``Retry-After``, capped), and
    on a hit is landed durably so the next lookup is offline. A name that resolves
    to nothing returns None (fail-open); a network or store error never crashes.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        min_interval: float = _MIN_INTERVAL,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._min_interval = min_interval  # tests pass 0.0 to skip pacing sleeps
        self._last_request = 0.0
        self._mem: dict[str, Card | None] = {}

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30, headers={'User-Agent': 'make-magic-plugin/2.0'})
        return self._client

    def get_card(self, name: str) -> Card | None:
        if name in self._mem:
            return self._mem[name]
        card = self._resolve_from_lake(name)
        if card is None:
            card = self._resolve_live(name)
        self._mem[name] = card
        return card

    # -- lake read (offline-first) ---------------------------------------- #

    def _resolve_from_lake(self, name: str) -> Card | None:
        """Name lookup over `raw/oracle_cards` — exact, else a DFC/split/adventure FACE match.

        An exact (case-insensitive) match wins. On a miss, a BARE face name (one
        with no ``' // '``) is matched against the lake's full ``Front // Back``
        name — the FRONT face preferred over the back — so a decklist entry like
        ``'Brazen Borrower'`` resolves to ``'Brazen Borrower // Petty Theft'`` AND
        picks up its otags. Without this, a face name misses the exact lake lookup,
        falls through to the live fetch (which does NOT join `card_otag`), and comes
        back with empty `otag_buckets` — silently UNDER-COUNTING the card in the
        piloting/deck classification. Both faces map to the SAME full card, so a
        back-face entry resolves identically.

        Returns the enriched `Card` (presentation + otags) or None on a miss /
        absent lake. Fails open: any store error degrades to None (-> live).
        """
        try:
            if not store.table_exists(*_ORACLE_CARDS):
                return None
            with store.connect(read_only=True) as conn:
                path = store.StorePaths.resolve().parquet_path(*_ORACLE_CARDS, create=False)
                rel = conn.read_parquet(str(path))
                cols = ', '.join(_CARD_COLUMNS)
                esc = _sql_escape(name)
                # Priority: exact > front face ('name // …') > back face ('… // name').
                # Each step short-circuits, so the common exact hit is ONE query. Face
                # matching runs ONLY for a bare name — a full 'A // B' query is
                # exact-or-miss (it cannot be a face of another card). Card names carry
                # no LIKE wildcards (`%`/`_`), so no wildcard escaping is needed.
                row = rel.filter(f"lower(name) = lower('{esc}')").select(cols).limit(1).fetchone()
                if row is None and ' // ' not in name:
                    row = rel.filter(f"lower(name) LIKE lower('{esc}') || ' // %'").select(cols).limit(1).fetchone()
                    if row is None:
                        row = rel.filter(f"lower(name) LIKE '% // ' || lower('{esc}')").select(cols).limit(1).fetchone()
                if row is None:
                    return None
                record: dict[str, Any] = dict(zip(_CARD_COLUMNS, row, strict=True))
                otags = self._otags_for(conn, record.get('oracle_id'))
        except Exception as exc:
            log.warning('card-dim: lake lookup for %r failed (%s); trying live.', name, exc)
            return None
        return _card_from_row(record, otags)

    def _otags_for(self, conn: object, oracle_id: object) -> list[str]:
        """Return the rolled-up otag slugs for `oracle_id` (empty if absent).

        Fail-open: a missing `card_otag` table (or any query error) yields an
        empty list — the card resolves without functional tags rather than
        crashing.
        """
        if oracle_id is None or not store.table_exists(*_CARD_OTAG):
            return []
        try:
            path = store.StorePaths.resolve().parquet_path(*_CARD_OTAG, create=False)
            rel = conn.read_parquet(str(path))  # type: ignore[attr-defined]
            rows = rel.filter(f"oracle_id = '{_sql_escape(str(oracle_id))}'").select('slug').fetchall()
        except Exception as exc:
            log.warning('card-dim: otag join for %r failed (%s); empty otags.', oracle_id, exc)
            return []
        return sorted({str(slug) for (slug,) in rows if slug is not None})

    # -- live fallback (bulk miss) ---------------------------------------- #

    def _resolve_live(self, name: str) -> Card | None:
        """Single live Scryfall fetch (exact then fuzzy); land durably on a hit.

        Fail-open: a definitive 404 (both exact and fuzzy) OR a transient failure
        (network / timeout / throttle after bounded retries) returns None. Only a
        real card is landed; a transient result is withheld so the next lookup
        retries rather than caching a miss.
        """
        data = self._fetch(name)
        if data is None or data is _TRANSIENT:
            return None
        _land_card(data)
        return _card_from_scryfall(data)

    def _fetch(self, name: str) -> dict[str, Any] | _Transient | None:
        """Return a card dict (found), ``None`` (definitive 404), or ``_TRANSIENT``.

        Only a real card or a confirmed 404 is a definitive result; any network
        error / timeout / non-404 HTTP status (including a 429/503 that exhausts
        the bounded retries) is transient and must not be landed.
        """
        try:
            resp = self._request({'exact': name})
            if resp.status_code == 404:
                resp = self._request({'fuzzy': name})
            if resp.status_code == 404:
                return None  # definitive: card not found
            resp.raise_for_status()  # non-404 4xx/5xx -> HTTPStatusError below
            return resp.json()
        except httpx.HTTPError:
            return _TRANSIENT  # network/timeout/5xx: do not land, retry next run

    def _request(self, params: dict[str, str]) -> httpx.Response:
        """GET the named endpoint, paced + with bounded retry on 429/503.

        Paces to ``_min_interval`` between requests (Scryfall's courtesy ask) so a
        bulk-miss burst doesn't get throttled, and retries a throttle/unavailable
        response (honoring ``Retry-After``, capped) up to ``_MAX_RETRIES`` before
        giving the caller the final response (whose ``raise_for_status`` makes it
        transient).
        """
        client = self._get_client()
        resp: httpx.Response | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if self._min_interval:
                wait = self._min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
            resp = client.get(_NAMED_URL, params=params)
            self._last_request = time.monotonic()
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                retry_after = resp.headers.get('Retry-After', '')
                backoff = (
                    min(float(retry_after), _MAX_BACKOFF)
                    if retry_after.isdigit()
                    else min(self._min_interval * (2**attempt) + 0.25, _MAX_BACKOFF)
                )
                time.sleep(backoff)
                continue
            return resp
        assert resp is not None
        return resp


def _card_from_row(record: dict[str, Any], otags: Iterable[str]) -> Card:
    """Build the extended `Card` from a projected lake row + its otag slugs.

    power/toughness stay strings (never coerced). `otag_buckets` is the crosswalk
    over the full slug closure; `otags` is the raw rolled-up slug list.
    """
    otag_list = list(otags)
    buckets = sorted(buckets_for(set(otag_list)))
    return Card(
        name=record.get('name') or '',
        oracle_id=_opt_str(record.get('oracle_id')),
        mana_value=record.get('cmc'),
        mana_cost=record.get('mana_cost'),
        type_line=record.get('type_line'),
        colors=list(record.get('colors') or []),
        color_identity=list(record.get('color_identity') or []),
        produced_mana=list(record.get('produced_mana') or []),
        keywords=list(record.get('keywords') or []),
        oracle_text=record.get('oracle_text'),
        power=_opt_str(record.get('power')),
        toughness=_opt_str(record.get('toughness')),
        art_crop=record.get('art_crop'),
        scryfall_uri=record.get('scryfall_uri'),
        set_name=record.get('set_name'),
        otags=otag_list,
        otag_buckets=buckets,
    )


def _opt_str(value: object) -> str | None:
    """Coerce a DuckDB scalar to str|None (oracle_id may be a uuid.UUID)."""
    return None if value is None else str(value)


def lake_row_count() -> int | None:
    """Return the ``raw/oracle_cards`` row count, or None when the table is absent.

    Fail-open: any store/duckdb error is swallowed to None (treated as absent) so a
    status probe never crashes a scoring verb.
    """
    if not store.table_exists(*_ORACLE_CARDS):
        return None
    try:
        with store.connect(read_only=True) as conn:
            path = store.StorePaths.resolve().parquet_path(*_ORACLE_CARDS, create=False)
            row = conn.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()
    except Exception as exc:  # pragma: no cover - defensive: a corrupt/locked lake.
        log.warning('card-dim: oracle_cards row-count probe failed (%s); treating as absent.', exc)
        return None
    return int(row[0]) if row is not None else None


def lake_status() -> str:
    """Classify the card lake: ``'absent'`` | ``'stub'`` | ``'ready'``.

    The single signal the scoring guard keys on. ``absent`` = no ``raw/oracle_cards``
    table; ``stub`` = present but below :data:`LAKE_STUB_FLOOR` (the committed seed
    row, not a real dim); ``ready`` = a real oracle bulk. Only ``ready`` is safe to
    score against — ``absent``/``stub`` mean scoring would resolve to all-zeros.
    """
    count = lake_row_count()
    if count is None:
        return 'absent'
    if count < LAKE_STUB_FLOOR:
        return 'stub'
    return 'ready'


def default_card_resolver() -> DuckDBCardResolver:
    """The package default resolver — lake-backed (DuckDB over `oracle_cards`).

    Offline-first from the lake; live-fallback on a bulk miss (paced + retried,
    landed durably). No JSON cache: the lake is the durable store.
    """
    return DuckDBCardResolver()


def fetch_card_raw(name: str, *, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """Fetch the full raw Scryfall card dict for `name` and land it durably.

    The package façade for `scripts/scryfall_cache`: shares this layer's single
    live-fetch (exact then fuzzy, paced + retried) + durable-landing path, and the
    lake is the one durable store. Returns the unprojected Scryfall dict (the six
    consumers rely on the full shape — `prices`, `card_faces`, `image_uris`, …),
    so the projection to `Card` is not applied here. Fails open: a 404
    (exact+fuzzy) or transient/network error returns None.

    Note this is the live/durable path only; a lake HIT for a name already landed
    is served by `DuckDBCardResolver.get_card` (which returns the projected
    `Card`). `scryfall_cache` calls this to preserve raw-dict fidelity for its
    consumers while still landing everything durably in the lake.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30, headers={'User-Agent': 'make-magic-plugin/2.0'})
    try:
        resolver = DuckDBCardResolver(client=client)
        data = resolver._fetch(name)
        if data is None or data is _TRANSIENT:
            return None
        _land_card(data)
        return data
    finally:
        if owns:
            client.close()
