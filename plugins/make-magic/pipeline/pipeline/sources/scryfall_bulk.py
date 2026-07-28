"""Puller: Scryfall ``oracle_cards`` bulk -> ``raw/oracle_cards``.

Flow (data-architecture §ingest, incremental step 3 — "the most annoying to
refresh"):
    1. GET ``https://api.scryfall.com/bulk-data/oracle_cards`` metadata
       (``updated_at`` + ``download_uri``).
    2. Cursor check: skip if ``updated_at`` is not newer than the last load.
    3. Stream the ``download_uri`` JSON (~140 MB) and load to
       ``raw/oracle_cards``.

FETCH-ON-DEMAND, NOT BUNDLED: the oracle_cards file is ~140 MB, far too large to
commit. There is no offline snapshot — instead the puller streams the file on
demand and caches it as Parquet in ``raw/`` (which is git-ignored). ``max_cards``
caps the number of cards loaded, so verification/tests never pull the whole
file. In production, call ``sync()`` with no cap for a full refresh.

This is deliberately NOT fail-open to a bundled baseline (there is none); on
failure it raises so a caller knows the (large, on-demand) fetch did not load.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from pipeline import store
from pipeline.sources._common import Cursor, is_newer

log = logging.getLogger('make_magic.sources.scryfall_bulk')

SOURCE = 'oracle_cards'
BULK_META_URL = 'https://api.scryfall.com/bulk-data/oracle_cards'
HEADERS = {'User-Agent': 'make-magic-plugin/2.0'}
RATE_LIMIT_MS = 100


def _fetch_meta(client: httpx.Client) -> tuple[str, str]:
    """Return ``(download_uri, updated_at)`` for the oracle_cards bulk file."""
    resp = client.get(BULK_META_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    meta = resp.json()
    return str(meta['download_uri']), str(meta['updated_at'])


def _stream_cards(client: httpx.Client, download_uri: str, max_cards: int | None) -> Iterator[dict[str, Any]]:
    """Stream cards from the bulk download, stopping after ``max_cards``.

    The bulk file is a single JSON array; we stream bytes and incrementally
    decode top-level objects so a bounded pull never buffers the whole ~140 MB.
    """
    time.sleep(RATE_LIMIT_MS / 1000)
    count = 0
    with client.stream('GET', download_uri, headers=HEADERS, timeout=120) as resp:
        resp.raise_for_status()
        decoder = json.JSONDecoder()
        buf = ''
        started = False
        for chunk in resp.iter_text():
            buf += chunk
            if not started:
                idx = buf.find('[')
                if idx == -1:
                    continue
                buf = buf[idx + 1 :]
                started = True
            while True:
                buf = buf.lstrip().lstrip(',').lstrip()
                if not buf or buf[0] == ']':
                    break
                try:
                    obj, end = decoder.raw_decode(buf)
                except json.JSONDecodeError:
                    break  # need more bytes
                buf = buf[end:]
                yield obj
                count += 1
                if max_cards is not None and count >= max_cards:
                    return


def _load(cards: list[dict[str, Any]]) -> Path:
    """Load ``cards`` to ``raw/oracle_cards.parquet`` via the store.

    Scryfall oracle cards carry deeply nested/heterogeneous fields; to keep the
    Parquet schema stable and small we project the columns the pipeline needs
    (matching contracts.Card + a couple of facts) and drop the rest.
    """
    projected = [_project(c) for c in cards]
    with store.connect() as conn:
        raw_dir = store.StorePaths.resolve().layer_dir('raw', create=True)
        tmp = raw_dir / '_oracle_cards.tmp.json'
        tmp.write_text(json.dumps(projected), encoding='utf-8')
        try:
            rel = conn.read_json(str(tmp))
            path = store.write_parquet(conn, rel, 'raw', SOURCE)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def _project(card: dict[str, Any]) -> dict[str, Any]:
    """Project a Scryfall oracle card to the columns the pipeline consumes."""
    return {
        'oracle_id': card.get('oracle_id'),
        'name': card.get('name'),
        'cmc': card.get('cmc'),
        'type_line': card.get('type_line'),
        'colors': card.get('colors', []),
        'color_identity': card.get('color_identity', []),
        'produced_mana': card.get('produced_mana', []),
        'keywords': card.get('keywords', []),
        'oracle_text': card.get('oracle_text'),
        # --- #5 presentation fields (all present in the daily oracle_cards bulk;
        #     price is NOT projected — it is volatile and served live) --- #
        'power': card.get('power'),
        'toughness': card.get('toughness'),
        'art_crop': card.get('image_uris', {}).get('art_crop'),
        'scryfall_uri': card.get('scryfall_uri'),
        'set_name': card.get('set_name'),
    }


def sync(
    *,
    client: httpx.Client | None = None,
    force: bool = False,
    max_cards: int | None = None,
) -> Path:
    """Pull oracle_cards into ``raw/oracle_cards``; return the loaded path.

    Cursor-gated (skip-if-not-newer). ``max_cards`` bounds the pull (None =
    full refresh, ~140 MB). NOT fail-open — there is no bundled snapshot; a fetch
    failure raises.
    """
    cursor = Cursor.load()
    prior = cursor.get(SOURCE)
    owns_client = client is None
    client = client or httpx.Client()
    try:
        download_uri, updated_at = _fetch_meta(client)
        if not force and not is_newer(prior, updated_at) and store.table_exists('raw', SOURCE):
            log.info('oracle_cards: %s not newer than %s; skipping load.', updated_at, prior)
            return store.StorePaths.resolve().parquet_path('raw', SOURCE, create=False)
        cards = list(_stream_cards(client, download_uri, max_cards))
        path = _load(cards)
        # A capped pull is NOT a full refresh; only advance the cursor on full.
        if max_cards is None:
            cursor.set(SOURCE, updated_at)
            cursor.save()
        log.info(
            'oracle_cards: loaded %d cards (updated_at=%s, cap=%s).',
            len(cards),
            updated_at,
            max_cards,
        )
        return path
    finally:
        if owns_client:
            client.close()


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    parser = argparse.ArgumentParser(description='Pull Scryfall oracle_cards bulk.')
    parser.add_argument(
        '--max-cards',
        type=int,
        default=None,
        help='Cap cards loaded (default: full ~140MB refresh).',
    )
    parser.add_argument('--force', action='store_true', help='Load even if not newer.')
    args = parser.parse_args()
    path = sync(max_cards=args.max_cards, force=args.force)
    print(f'loaded oracle_cards -> {path}')


if __name__ == '__main__':
    main()
