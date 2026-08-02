"""Puller: MythicSpoiler preview scrape -> ``raw/spoilers``.

Migrated from the standalone ``scripts/spoiler_sync.py`` SQLite state machine.
The scrape (``httpx`` + ``BeautifulSoup``) that used to feed ``spoiler_cache.db``
now lands raw preview rows into ``raw/spoilers`` Parquet, and the cross-run
"new since last sync" diff derives from the lake (current snapshot vs. the prior
``normalized/spoilers``) instead of a SQLite ``meta`` table.

Flow (mirrors ``scryfall_bulk`` / ``spellbook``):
    1. Scrape each target set's MythicSpoiler index page (+ ``newspoilers.html``)
       for card entries — slug, image, set code.
    2. Append-dedupe the rows into ``raw/spoilers`` Parquet via ``store``.
    3. FAIL-OPEN (invariant I5): if MythicSpoiler is unreachable, keep the last
       ``raw/spoilers`` snapshot (return it untouched) rather than crashing.

There is NO bundled snapshot for spoilers (they are set-specific and ephemeral);
the fail-open baseline is whatever the LAST successful scrape already landed in
``raw/spoilers``. On a first-ever run with no prior snapshot and a dead network,
an empty table is materialized so downstream reads still succeed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from bs4 import BeautifulSoup

from pipeline import store
from pipeline.sources._common import Cursor, dedupe

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger('make_magic.sources.spoilers')

SOURCE = 'spoilers'
MYTHICSPOILER_BASE = 'https://mythicspoiler.com'
HEADERS = {'User-Agent': 'make-magic-plugin/2.0'}
RATE_LIMIT_S = 1.5


# --------------------------------------------------------------------------- #
# Scrape — the MythicSpoiler HTML parse (moved verbatim from spoiler_sync.py).
# --------------------------------------------------------------------------- #


def _attr_str(value: object) -> str:
    """Narrow a BeautifulSoup attribute (``str | list[str] | None``) to a ``str``.

    ``Tag.get`` is typed ``_AttributeValue | None`` — a multi-valued attribute is
    a ``list[str]`` and a missing one is ``None``. HTML ``href``/``src`` are
    single-valued, so this collapses a list to its first item and a miss to the
    empty string, giving downstream string ops a definite ``str``.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        first = value[0]
        return first if isinstance(first, str) else ''
    return ''


def scrape_set(client: httpx.Client, base_url: str, set_code: str) -> list[dict[str, str]]:
    """Scrape a set's index page for every card entry.

    Returns a list of ``{slug, image_url, set_code, detail_url}`` dicts. Raises
    on an HTTP error so the caller's fail-open branch can catch it.
    """
    url = f'{base_url}/{set_code}/index.html'
    resp = client.get(url)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, 'lxml')
    cards: list[dict[str, str]] = []
    for link in soup.find_all('a', class_='card'):
        href = _attr_str(link.get('href'))
        img = link.find('img')
        if not href or not img:
            continue
        slug = href.replace('cards/', '').replace('.html', '')
        img_src = _attr_str(img.get('src'))
        image_url = f'{base_url}/{set_code}/{img_src}' if not img_src.startswith('http') else img_src
        detail_url = f'{base_url}/{set_code}/{href}' if not href.startswith('http') else href
        cards.append({'slug': slug, 'image_url': image_url, 'set_code': set_code, 'detail_url': detail_url})
    return cards


def scrape_new(client: httpx.Client, base_url: str, target_sets: Iterable[str]) -> list[dict[str, str]]:
    """Scrape ``/newspoilers.html`` for recently added cards in ``target_sets``."""
    targets = set(target_sets)
    url = f'{base_url}/newspoilers.html'
    resp = client.get(url)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, 'lxml')
    cards: list[dict[str, str]] = []
    current_set: str | None = None
    for div in soup.find_all('div', class_=['grid-span', 'grid-card']):
        raw_classes = div.get('class')
        classes = raw_classes if isinstance(raw_classes, list) else [raw_classes] if raw_classes else []
        if 'grid-span' in classes:
            text = div.get_text(' ', strip=True).lower()
            current_set = next((sc for sc in targets if sc in text), None)
            continue
        if 'grid-card' in classes and current_set:
            link = div.find('a', href=re.compile(r'/cards/'))
            if not link:
                continue
            href = _attr_str(link.get('href'))
            img = link.find('img')
            if not img:
                continue
            match = re.search(rf'({current_set})/cards/(.+?)\.html', href)
            if not match:
                continue
            slug = match.group(2)
            img_src = _attr_str(img.get('src'))
            image_url = f'{base_url}/{img_src}' if not img_src.startswith('http') else img_src
            detail_url = f'{base_url}/{href}' if not href.startswith('http') else href
            cards.append({'slug': slug, 'image_url': image_url, 'set_code': current_set, 'detail_url': detail_url})
    return cards


def _scrape_sets(client: httpx.Client, base_url: str, set_codes: list[str]) -> list[dict[str, str]]:
    """Scrape every target set's index + newspoilers, deduped on ``(set_code, slug)``.

    Raises on the FIRST unreachable-network error so ``sync`` fails open to the
    last snapshot (a partial scrape would silently drop already-seen cards).
    """
    rows: list[dict[str, str]] = []
    for sc in set_codes:
        index_cards = scrape_set(client, base_url, sc)
        rows.extend(index_cards)
        time.sleep(RATE_LIMIT_S)
        new_cards = scrape_new(client, base_url, {sc})
        rows.extend(new_cards)
    return dedupe(rows, key=lambda r: (r['set_code'], r['slug']))


# --------------------------------------------------------------------------- #
# Load — materialize raw/spoilers (append-dedupe on the durable slug key).
# --------------------------------------------------------------------------- #


def _read_existing() -> list[dict[str, str]]:
    """Read the current ``raw/spoilers`` rows back as dicts (empty if absent)."""
    if not store.table_exists('raw', SOURCE):
        return []
    with store.connect() as conn:
        rel = store.read_parquet(conn, 'raw', SOURCE)
        cols = ['slug', 'image_url', 'set_code', 'detail_url']
        rows = rel.select(', '.join(cols)).fetchall()
    return [dict(zip(cols, row, strict=True)) for row in rows]


def _load(rows: list[dict[str, str]]) -> Path:
    """Materialize ``rows`` to ``raw/spoilers.parquet`` (deduped on ``(set_code, slug)``)."""
    rows = dedupe(rows, key=lambda r: (r['set_code'], r['slug']))
    with store.connect() as conn:
        raw_dir = store.StorePaths.resolve().layer_dir('raw', create=True)
        tmp = raw_dir / f'_{SOURCE}.tmp.json'
        tmp.write_text(json.dumps(rows), encoding='utf-8')
        try:
            if rows:
                rel = conn.read_json(str(tmp))
            else:
                rel = conn.sql(
                    'SELECT NULL::VARCHAR AS slug, NULL::VARCHAR AS image_url, '
                    'NULL::VARCHAR AS set_code, NULL::VARCHAR AS detail_url WHERE 1=0'
                )
            path = store.write_parquet(conn, rel, 'raw', SOURCE)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def sync(
    set_codes: list[str],
    *,
    client: httpx.Client | None = None,
    base_url: str = MYTHICSPOILER_BASE,
) -> Path:
    """Scrape ``set_codes`` from MythicSpoiler into ``raw/spoilers``; return the path.

    Append-dedupe (a re-scraped slug refreshes the row, keeping first-seen order),
    then advance the spoiler cursor to the run timestamp — the ``first_seen_cursor``
    the transform stamps on newly-seen rows. FAIL-OPEN: any scrape failure keeps
    the last snapshot (materialized untouched) rather than crashing (invariant I5).
    """
    codes = [sc.lower() for sc in set_codes]
    cursor = Cursor.load()
    owns_client = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)
    try:
        try:
            scraped = _scrape_sets(client, base_url, codes)
        except httpx.HTTPError as exc:
            log.warning('spoilers: scrape failed (%s); keeping the last snapshot.', exc)
            existing = _read_existing()
            path = _load(existing)
            log.info('spoilers: kept %d rows from the last snapshot.', len(existing))
            return path
        merged = _read_existing() + scraped
        path = _load(merged)
        token = _now_cursor()
        cursor.set(SOURCE, token)
        cursor.save()
        log.info('spoilers: scraped %d rows across %s (cursor=%s).', len(scraped), codes, token)
        return path
    finally:
        if owns_client:
            client.close()


def _now_cursor() -> str:
    """The spoiler cursor token — the run's UTC timestamp (ISO-8601)."""
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    parser = argparse.ArgumentParser(description='Scrape MythicSpoiler previews into raw/spoilers.')
    parser.add_argument('set_codes', nargs='+', help='Set code(s) to scrape (e.g. eoe tdm).')
    args = parser.parse_args()
    path = sync(args.set_codes)
    print(f'loaded spoilers -> {path}')


if __name__ == '__main__':
    main()
