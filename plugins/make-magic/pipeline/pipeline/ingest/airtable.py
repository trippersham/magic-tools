"""Puller: Airtable human-edited tables -> ``raw/airtable_<table>`` (PULL-ONLY).

THE GOVERNING SAFETY PROPERTY of this module: it issues **GET requests only**.
Airtable is the authoritative source of truth for human-edited data (Decks,
Trades, Chase Cards, Cards); the local lake is a READ-ONLY mirror. This module
must NEVER create/update/delete anything in Airtable.

That property is enforced structurally, not by convention: EVERY outbound
request goes through :meth:`GetOnlyClient.request`, which RAISES
``NonGetMethodError`` on any method other than GET. There is no code path in
this module that constructs a POST/PATCH/PUT/DELETE — and even if one were added
by mistake, the wrapper would reject it at runtime. The wrapper is the single
choke point; the httpx client is private so callers can't bypass it.

Flow (data-architecture §Airtable pull):
    - Auth: ``AIRTABLE_API_KEY`` Bearer PAT (env).
    - Base + table NAMES come from :mod:`pipeline.config` (env-driven; turnkey
      defaults match the current base but every value is overridable, so the
      pipeline is NOT locked to one Airtable instance). Table/field NAMES are
      resolved to per-base ``tbl…``/``fld…`` ids AT RUNTIME via the meta API
      (:class:`pipeline.config.AirtableResolver`), cached once per run.
    - Incremental: if the table has a ``Last Modified``-style field, cursor on
      its max value and filter to only newer records; else full-refresh-replace.
    - Load each table to ``raw/airtable_<table>.parquet`` via the store.
    - Uses ``returnFieldsByFieldId=true`` so we key on stable field ids.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from pipeline import store
from pipeline.config import AirtableResolver, get_settings
from pipeline.ingest._common import Cursor, is_newer

log = logging.getLogger('make_magic.ingest.airtable')

API_ROOT = 'https://api.airtable.com/v0'
META_ROOT = 'https://api.airtable.com/v0/meta'
HEADERS_UA = {'User-Agent': 'make-magic-plugin/2.0'}
RATE_LIMIT_MS = 210  # Airtable caps at 5 req/s per base; stay under.


def _tables() -> dict[str, tuple[str, str | None]]:
    """The human-edited tables mirrored, keyed by our internal handle.

    Returns ``{handle: (airtable_table_name, last_modified_field_name | None)}``
    where the NAMES come from env-driven :class:`~pipeline.config.Settings` (not
    hard-coded ids). NAMES are resolved to per-base ids at pull time. A ``None``
    last-modified field means full-refresh-replace.
    """
    s = get_settings()
    return {
        # cards: NO whole-record lastModifiedTime exists — the two lastModifiedTime
        # fields are field-SCOPED ("Price Last Updated" -> Price only; "Last
        # Acquired / Sold At" -> Number Owned only), so keying on either silently
        # misses edits to Condition/Sources/links/etc. Full-refresh (None) is
        # correct + safe for a read-only derived mirror of a modest table.
        'cards': (s.cards_table, None),
        'decks': (s.decks_table, None),  # no whole-record lastModified field
        'trades': (s.trades_table, None),
        # Chase Cards has a whole-record "Last Modified" lastModifiedTime field.
        'chase_cards': (s.chase_table, 'Last Modified'),
    }


class NonGetMethodError(RuntimeError):
    """Raised when any non-GET HTTP method is attempted from this module.

    This is the enforcement of the PULL-ONLY invariant: the Airtable mirror is
    read-only and must never mutate the source of truth.
    """


class GetOnlyClient:
    """A thin httpx wrapper that permits GET and ONLY GET.

    The wrapped ``httpx.Client`` is private (name-mangled) so the sole way to
    issue a request is :meth:`request`/:meth:`get`, both of which enforce the
    method guard. Any attempt to issue POST/PATCH/PUT/DELETE raises
    :class:`NonGetMethodError` before a byte leaves the process.
    """

    def __init__(self, token: str, *, _client: httpx.Client | None = None) -> None:
        self.__client = _client or httpx.Client(timeout=30)
        self.__auth = {'Authorization': f'Bearer {token}', **HEADERS_UA}

    def request(self, method: str, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        """Issue a request — REJECTING any method other than GET."""
        if method.upper() != 'GET':
            raise NonGetMethodError(
                f'Airtable mirror is PULL-ONLY; refused {method!r} to {url}. This module must never mutate Airtable.'
            )
        headers = self.__auth
        return self.__client.request('GET', url, params=params, headers=headers)

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        """Convenience GET (still routed through the guarded :meth:`request`)."""
        return self.request('GET', url, params=params)

    def get_meta_tables(self, base_id: str) -> dict[str, Any]:
        """Fetch the base schema (``GET /v0/meta/bases/{base}/tables``).

        Satisfies :class:`pipeline.config.SupportsMetaTables`. Routed through the
        GET-only guard, so schema discovery stays pull-only like everything else.
        """
        resp = self.get(f'{META_ROOT}/bases/{base_id}/tables')
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self.__client.close()


def _list_records(
    client: GetOnlyClient, base_id: str, table_id: str, *, since: str | None, since_field: str | None
) -> list[dict[str, Any]]:
    """List all records for a table (paginated), returning flattened rows.

    When ``since``/``since_field`` are given, adds a formula filter so only
    records newer than the watermark are pulled (incremental). Records come back
    keyed by FIELD ID (``returnFieldsByFieldId=true``) for stable joins.
    """
    url = f'{API_ROOT}/{base_id}/{table_id}'
    params: dict[str, Any] = {'pageSize': 100, 'returnFieldsByFieldId': 'true'}
    if since and since_field:
        # IS_AFTER({fld}, since) — Airtable filterByFormula over the modified field.
        params['filterByFormula'] = f"IS_AFTER({{{since_field}}}, DATETIME_PARSE('{since}'))"
    rows: list[dict[str, Any]] = []
    offset: str | None = None
    while True:
        page_params = dict(params)
        if offset:
            page_params['offset'] = offset
        resp = client.get(url, params=page_params)
        resp.raise_for_status()
        payload = resp.json()
        for rec in payload.get('records', []):
            row = {'_record_id': rec['id'], '_created_time': rec.get('createdTime')}
            row.update(rec.get('fields', {}))
            rows.append(row)
        offset = payload.get('offset')
        if not offset:
            break
        time.sleep(RATE_LIMIT_MS / 1000)
    return rows


def _load(table: str, rows: list[dict[str, Any]]) -> Path:
    """Load ``rows`` to ``raw/airtable_<table>.parquet`` via the store.

    Airtable field values are heterogeneous (scalars, arrays, link-id lists), so
    each row is JSON-serialized per field into a stable string column set is
    avoided; instead DuckDB infers a union schema from the JSON. Empty tables
    load a zero-row Parquet with a minimal schema.
    """
    name = f'airtable_{table}'
    with store.connect() as conn:
        raw_dir = store.StorePaths.resolve().layer_dir('raw', create=True)
        tmp = raw_dir / f'_{name}.tmp.json'
        # json-encode field values that are dict/list so DuckDB gets stable text.
        norm = [{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()} for row in rows]
        if not norm:
            norm = [{'_record_id': None}]  # keep a schema even when empty
        tmp.write_text(json.dumps(norm), encoding='utf-8')
        try:
            rel = conn.read_json(str(tmp))
            path = store.write_parquet(conn, rel, 'raw', name)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def _max_modified(rows: list[dict[str, Any]], field_id: str) -> str | None:
    """Max value of the last-modified field across ``rows`` (the new cursor)."""
    values = [str(r[field_id]) for r in rows if r.get(field_id)]
    return max(values) if values else None


def run_table(
    table: str,
    *,
    client: GetOnlyClient,
    resolver: AirtableResolver | None = None,
    force: bool = False,
) -> Path:
    """Pull a single table into ``raw/airtable_<table>``; return the path.

    The table's Airtable NAME (and, for chase, its Last-Modified field NAME) come
    from env-driven :func:`_tables`; the NAMES are resolved to per-base ids at
    runtime via ``resolver`` (built from ``client`` if not supplied). Preserves
    behavior: cards/decks/trades full-refresh, chase incremental on its
    Last-Modified field.
    """
    tables = _tables()
    if table not in tables:
        raise ValueError(f'Unknown table {table!r}; expected one of {sorted(tables)}.')
    settings = get_settings()
    base_id = settings.airtable_base_id
    if resolver is None:
        resolver = AirtableResolver(client, base_id=base_id)

    table_name, mod_field_name = tables[table]
    table_id = resolver.table_id(table_name)
    mod_field_id = resolver.field_id(table_name, mod_field_name) if mod_field_name else None

    source = f'airtable_{table}'
    cursor = Cursor.load()
    prior = cursor.get(source)

    since = prior if (mod_field_id and not force) else None
    rows = _list_records(client, base_id, table_id, since=since, since_field=mod_field_id)
    path = _load(table, rows)

    if mod_field_id:
        new_cursor = _max_modified(rows, mod_field_id)
        if new_cursor and is_newer(prior, new_cursor):
            cursor.set(source, new_cursor)
            cursor.save()
    log.info('airtable: loaded %d rows for %s.', len(rows), table)
    return path


def sync(*, force: bool = False, tables: list[str] | None = None) -> dict[str, Path]:
    """Pull all (or ``tables``) human-edited tables; return ``{table: path}``.

    Requires ``AIRTABLE_API_KEY``. PULL-ONLY: all requests are GET (guarded).
    Table/field NAMES are resolved to per-base ids once via a shared resolver.
    """
    token = os.environ.get('AIRTABLE_API_KEY')
    if not token:
        raise RuntimeError('AIRTABLE_API_KEY is not set; cannot pull Airtable.')
    client = GetOnlyClient(token)
    resolver = AirtableResolver(client, base_id=get_settings().airtable_base_id)
    try:
        targets = tables or list(_tables())
        return {t: run_table(t, client=client, resolver=resolver, force=force) for t in targets}
    finally:
        client.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    paths = sync()
    for table, path in paths.items():
        print(f'loaded airtable_{table} -> {path}')


if __name__ == '__main__':
    main()
