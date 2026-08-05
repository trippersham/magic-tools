"""Puller: Scryfall oracle-tags -> ``raw/oracle_tags``.

Flow:
    1. GET ``https://api.scryfall.com/bulk-data/oracle-tags`` for the metadata
       (``updated_at`` + ``jsonl_download_uri``).
    2. Cursor check: skip if ``updated_at`` is not newer than the last load.
    3. GET the ``jsonl_download_uri`` gzipped JSONL (~18 MB), load it to ``raw/oracle_tags``.
    4. FAIL-OPEN: any network/HTTP error falls back to the bundled compressed
       snapshot (``data/snapshots/oracle_tags.json.gz``) — the offline baseline —
       so a caller always gets tags. Logs, never crashes.

Snapshot trim: the committed snapshot keeps the full 4,499-tag DAG (all
parent/child edges — mandatory, since root tags carry 0 taggings and you must
roll leaves up) but caps taggings at 8/tag (~24.5k of 229.9k) to stay under
~1 MB. The rollup needs the whole structure; a bounded tagging sample is enough
for the offline baseline. The full daily file refreshes on demand.

Scryfall conventions: a descriptive ``User-Agent`` and a courteous rate-limit
pause.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from pipeline import store
from pipeline.sources._common import Cursor, is_newer

log = logging.getLogger('make_magic.sources.oracle_tags')

SOURCE = 'oracle_tags'
BULK_META_URL = 'https://api.scryfall.com/bulk-data/oracle-tags'
# Descriptive UA + courteous pacing.
HEADERS = {'User-Agent': 'make-magic-plugin/2.0'}
RATE_LIMIT_MS = 100
SNAPSHOT = Path(__file__).resolve().parents[2] / 'data' / 'snapshots' / 'oracle_tags.json.gz'


def _fetch_meta(client: httpx.Client) -> tuple[str, str]:
    """GET the cheap bulk-meta JSON. Returns ``(jsonl_download_uri, updated_at)``.

    This is the cursor probe: it fetches only the small metadata document (the
    ``updated_at`` change token + the ``jsonl_download_uri`` of the big payload)
    and does not download the ~18 MB file. ``sync`` gates on ``updated_at`` before
    calling :func:`_fetch_payload`, so a not-newer run never pays for the payload.
    Scryfall serves the payload as a gzipped ``jsonl_download_uri``.

    Raises on any HTTP/network failure — the caller catches and falls back.
    """
    meta = client.get(BULK_META_URL, headers=HEADERS, timeout=30)
    meta.raise_for_status()
    meta_json = meta.json()
    updated_at = str(meta_json['updated_at'])
    download_uri = str(meta_json['jsonl_download_uri'])
    return download_uri, updated_at


def _fetch_payload(client: httpx.Client, download_uri: str) -> list[dict[str, Any]]:
    """GET the gzipped-JSONL tags payload (~18 MB) and return the tag list.

    Scryfall serves this as gzipped JSONL (one tag object per line); we
    download, gunzip, and parse line-by-line. Called only after the cursor gate in
    :func:`sync` passes (newer or forced), so the big download is skipped on a
    not-newer run. Raises on any HTTP/network failure — the caller catches and
    falls back to the bundled snapshot.
    """
    time.sleep(RATE_LIMIT_MS / 1000)
    resp = client.get(download_uri, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    raw = gzip.decompress(resp.content)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _load_snapshot() -> list[dict[str, Any]]:
    """Load the bundled offline snapshot (gzipped JSON array of tags)."""
    with gzip.open(SNAPSHOT, 'rt', encoding='utf-8') as f:
        return json.load(f)


def _load(tags: list[dict[str, Any]]) -> Path:
    """Load ``tags`` to ``raw/oracle_tags.parquet`` via the store.

    DuckDB infers the (nested) schema from the JSON, so we write the array to a
    temp file and let ``read_json`` write it as Parquet.
    """
    with store.connect() as conn:
        raw_dir = store.StorePaths.resolve().layer_dir('raw', create=True)
        tmp = raw_dir / '_oracle_tags.tmp.json'
        tmp.write_text(json.dumps(tags), encoding='utf-8')
        try:
            rel = conn.read_json(str(tmp))
            path = store.write_parquet(conn, rel, 'raw', SOURCE)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def sync(*, client: httpx.Client | None = None, force: bool = False) -> Path:
    """Pull oracle-tags into ``raw/oracle_tags``; return the loaded Parquet path.

    Cursor-gated (skip-if-not-newer) and FAIL-OPEN: on any fetch failure,
    loads the bundled snapshot instead of raising.
    """
    cursor = Cursor.load()
    prior = cursor.get(SOURCE)
    owns_client = client is None
    client = client or httpx.Client()
    try:
        # 1. Cheap cursor probe first — small meta JSON only, no big download.
        download_uri, updated_at = _fetch_meta(client)
        # 2. Cursor gate: skip the ~18 MB payload GET entirely if not newer.
        if not force and not is_newer(prior, updated_at) and store.table_exists('raw', SOURCE):
            log.info('oracle_tags: %s not newer than %s; skipping load.', updated_at, prior)
            return store.StorePaths.resolve().parquet_path('raw', SOURCE, create=False)
        # 3. Only now (newer, forced, or first-run) download the big payload + load.
        tags = _fetch_payload(client, download_uri)
        path = _load(tags)
        cursor.set(SOURCE, updated_at)
        cursor.save()
        log.info('oracle_tags: loaded %d tags (updated_at=%s).', len(tags), updated_at)
        return path
    except Exception as exc:
        log.warning('oracle_tags: fetch failed (%s); falling back to bundled snapshot.', exc)
        tags = _load_snapshot()
        path = _load(tags)
        log.info('oracle_tags: loaded %d tags from snapshot.', len(tags))
        return path
    finally:
        if owns_client:
            client.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    path = sync()
    print(f'loaded oracle_tags -> {path}')


if __name__ == '__main__':
    main()
