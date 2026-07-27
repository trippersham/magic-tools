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
    - Base ``appw7QPMoqktrgDc1``; tables mirrored by id (see references/
      airtable-schema.md).
    - Incremental: if the table has a ``Last Modified``-style field, watermark on
      its max value and filter to only newer records; else full-refresh-replace.
    - Land each table to ``raw/airtable_<table>.parquet`` via the store.
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
from pipeline.ingest._common import Watermark, is_newer

log = logging.getLogger("make_magic.ingest.airtable")

BASE_ID = "appw7QPMoqktrgDc1"
API_ROOT = "https://api.airtable.com/v0"
META_ROOT = "https://api.airtable.com/v0/meta"
HEADERS_UA = {"User-Agent": "make-magic-plugin/2.0"}
RATE_LIMIT_MS = 210  # Airtable caps at 5 req/s per base; stay under.

#: The human-edited tables mirrored (data-architecture: decks/trades/chase pull).
#: name -> (table id, last-modified field id or None for full-refresh).
TABLES: dict[str, tuple[str, str | None]] = {
    # cards: NO whole-record lastModifiedTime exists — the two lastModifiedTime
    # fields are field-SCOPED ("Price Last Updated" -> Price only; "Last Acquired
    # / Sold At" -> Number Owned only), so keying on either silently misses edits
    # to Condition/Sources/links/etc. Full-refresh (None) is correct + safe for a
    # read-only derived mirror of a modest table. (To re-enable incremental, add a
    # whole-record "Last Modified" lastModifiedTime field to Cards and key on it.)
    "cards": ("tbl3UgZZPJGQhEFo8", None),
    "decks": ("tblIfqVuVHNQza1K3", None),  # no whole-record lastModified field
    "trades": ("tblgqqIvTuz0l5SZM", None),
    "chase_cards": (
        "tblXsNtGgT7UQLPXZ",
        "fldtYh0qTTObjRkJ7",
    ),  # Last Modified (whole-record lastModifiedTime)
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
        self.__auth = {"Authorization": f"Bearer {token}", **HEADERS_UA}

    def request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        """Issue a request — REJECTING any method other than GET."""
        if method.upper() != "GET":
            raise NonGetMethodError(
                f"Airtable mirror is PULL-ONLY; refused {method!r} to {url}. This module must never mutate Airtable."
            )
        headers = self.__auth
        return self.__client.request("GET", url, params=params, headers=headers)

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        """Convenience GET (still routed through the guarded :meth:`request`)."""
        return self.request("GET", url, params=params)

    def close(self) -> None:
        self.__client.close()


def _list_records(
    client: GetOnlyClient, table_id: str, *, since: str | None, since_field: str | None
) -> list[dict[str, Any]]:
    """List all records for a table (paginated), returning flattened rows.

    When ``since``/``since_field`` are given, adds a formula filter so only
    records newer than the watermark are pulled (incremental). Records come back
    keyed by FIELD ID (``returnFieldsByFieldId=true``) for stable joins.
    """
    url = f"{API_ROOT}/{BASE_ID}/{table_id}"
    params: dict[str, Any] = {"pageSize": 100, "returnFieldsByFieldId": "true"}
    if since and since_field:
        # IS_AFTER({fld}, since) — Airtable filterByFormula over the modified field.
        params["filterByFormula"] = (
            f"IS_AFTER({{{since_field}}}, DATETIME_PARSE('{since}'))"
        )
    rows: list[dict[str, Any]] = []
    offset: str | None = None
    while True:
        page_params = dict(params)
        if offset:
            page_params["offset"] = offset
        resp = client.get(url, params=page_params)
        resp.raise_for_status()
        payload = resp.json()
        for rec in payload.get("records", []):
            row = {"_record_id": rec["id"], "_created_time": rec.get("createdTime")}
            row.update(rec.get("fields", {}))
            rows.append(row)
        offset = payload.get("offset")
        if not offset:
            break
        time.sleep(RATE_LIMIT_MS / 1000)
    return rows


def _land(table: str, rows: list[dict[str, Any]]) -> Path:
    """Land ``rows`` to ``raw/airtable_<table>.parquet`` via the store.

    Airtable field values are heterogeneous (scalars, arrays, link-id lists), so
    each row is JSON-serialized per field into a stable string column set is
    avoided; instead DuckDB infers a union schema from the JSON. Empty tables
    land a zero-row Parquet with a minimal schema.
    """
    name = f"airtable_{table}"
    with store.connect() as conn:
        raw_dir = store.StorePaths.resolve().layer_dir("raw", create=True)
        tmp = raw_dir / f"_{name}.tmp.json"
        # json-encode field values that are dict/list so DuckDB gets stable text.
        norm = [
            {
                k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                for k, v in row.items()
            }
            for row in rows
        ]
        if not norm:
            norm = [{"_record_id": None}]  # keep a schema even when empty
        tmp.write_text(json.dumps(norm), encoding="utf-8")
        try:
            rel = conn.read_json(str(tmp))
            path = store.write_parquet(conn, rel, "raw", name)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def _max_modified(rows: list[dict[str, Any]], field_id: str) -> str | None:
    """Max value of the last-modified field across ``rows`` (the new watermark)."""
    values = [str(r[field_id]) for r in rows if r.get(field_id)]
    return max(values) if values else None


def run_table(
    table: str,
    *,
    client: GetOnlyClient,
    force: bool = False,
) -> Path:
    """Pull a single table into ``raw/airtable_<table>``; return the path."""
    if table not in TABLES:
        raise ValueError(f"Unknown table {table!r}; expected one of {sorted(TABLES)}.")
    table_id, mod_field = TABLES[table]
    source = f"airtable_{table}"
    wm = Watermark.load()
    prior = wm.get(source)

    since = prior if (mod_field and not force) else None
    rows = _list_records(client, table_id, since=since, since_field=mod_field)
    path = _land(table, rows)

    if mod_field:
        new_wm = _max_modified(rows, mod_field)
        if new_wm and is_newer(prior, new_wm):
            wm.set(source, new_wm)
            wm.save()
    log.info("airtable: landed %d rows for %s.", len(rows), table)
    return path


def run(*, force: bool = False, tables: list[str] | None = None) -> dict[str, Path]:
    """Pull all (or ``tables``) human-edited tables; return ``{table: path}``.

    Requires ``AIRTABLE_API_KEY``. PULL-ONLY: all requests are GET (guarded).
    """
    token = os.environ.get("AIRTABLE_API_KEY")
    if not token:
        raise RuntimeError("AIRTABLE_API_KEY is not set; cannot pull Airtable.")
    client = GetOnlyClient(token)
    try:
        targets = tables or list(TABLES)
        return {t: run_table(t, client=client, force=force) for t in targets}
    finally:
        client.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    paths = run()
    for table, path in paths.items():
        print(f"landed airtable_{table} -> {path}")


if __name__ == "__main__":
    main()
