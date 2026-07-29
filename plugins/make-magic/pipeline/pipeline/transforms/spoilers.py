"""Normalize + reconcile spoiler previews: ``raw/spoilers`` -> ``normalized/spoilers``.

Migrated from ``scripts/spoiler_sync.py``'s SQLite reconciliation (the meta cursor
+ seen-slug/seen-id sets + the ``confirmed_by_scryfall`` linking). This transform:

    1. Reads ``raw/spoilers`` (the current MythicSpoiler scrape).
    2. Reconciles each slug to a Scryfall identity via the card resolver
       (``default_card_resolver().get_card`` — replacing the old
       ``cache._fetch`` reach-in). A confirmed match carries ``oracle_id`` +
       ``confirmed=True``; an unreconciled preview stays ``oracle_id=None,
       confirmed=False``.
    3. Detects "new since last sync" from the LAKE — slugs present now but ABSENT
       from the PRIOR ``normalized/spoilers`` — not a SQLite ``meta`` table.
    4. Materializes ``normalized/spoilers`` carrying the ``Spoiler`` contract,
       preserving ``first_seen_cursor`` for rows seen before and stamping the
       current cursor on the newly-seen ones.

"New since last sync" mechanism (design Decision 6 open item):
    A SNAPSHOT-DIFF against the prior ``normalized/spoilers`` table — the lake IS
    the durable last-seen marker. A slug's ``first_seen_cursor`` is preserved from
    the prior row when it existed, so "new" == "no prior row for this slug". This
    keeps state entirely in the lake (no watermark table, no per-card meta rows),
    is idempotent (a re-run with the same raw snapshot yields the same normalized
    table + an empty new-set), and is cursor-bounded via the ``sources.spoilers``
    cursor stamped as ``first_seen_cursor``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import store
from pipeline.contracts import Spoiler
from pipeline.sources._common import Cursor

if TYPE_CHECKING:
    from pipeline.collection.store import CardResolver

log = logging.getLogger('make_magic.transforms.spoilers')

RAW_SOURCE = 'spoilers'
NORMALIZED_TABLE = 'spoilers'
_SPOILER_COLS = ('slug', 'set_code', 'name', 'oracle_id', 'source', 'first_seen_cursor', 'confirmed')


# --------------------------------------------------------------------------- #
# Slug <-> name reconciliation helpers (pure).
# --------------------------------------------------------------------------- #


def slug_to_name_guess(slug: str) -> str:
    """Convert a MythicSpoiler slug to a rough card name for resolver lookup.

    Splits camelCase and pads digits — the same heuristic ``spoiler_sync`` used
    before handing the guess to fuzzy Scryfall matching.
    """
    spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', slug)
    spaced = re.sub(r'(\d+)', r' \1 ', spaced)
    return spaced.strip()


# --------------------------------------------------------------------------- #
# Lake I/O.
# --------------------------------------------------------------------------- #


def _load_raw() -> list[dict[str, str]]:
    """Read ``raw/spoilers`` rows back as dicts (empty if absent)."""
    if not store.table_exists('raw', RAW_SOURCE):
        return []
    with store.connect() as conn:
        rel = store.read_parquet(conn, 'raw', RAW_SOURCE)
        cols = ['slug', 'set_code', 'detail_url', 'image_url']
        rows = rel.select(', '.join(cols)).fetchall()
    return [dict(zip(cols, row, strict=True)) for row in rows]


def _load_prior_normalized() -> dict[str, dict[str, object]]:
    """Read the PRIOR ``normalized/spoilers`` keyed by slug (empty if absent).

    This is the durable "last seen" marker: a slug present here is NOT new.
    ``first_seen_cursor`` is carried forward from these rows.
    """
    if not store.table_exists('normalized', NORMALIZED_TABLE):
        return {}
    with store.connect() as conn:
        rel = store.read_parquet(conn, 'normalized', NORMALIZED_TABLE)
        rows = rel.select(', '.join(_SPOILER_COLS)).fetchall()
    prior: dict[str, dict[str, object]] = {}
    for row in rows:
        rec = dict(zip(_SPOILER_COLS, row, strict=True))
        prior[str(rec['slug'])] = rec
    return prior


def _materialize(spoilers: list[Spoiler]) -> Path:
    """Materialize ``spoilers`` to ``normalized/spoilers.parquet`` (Spoiler-shaped)."""
    payload = [s.model_dump() for s in spoilers]
    with store.connect() as conn:
        norm_dir = store.StorePaths.resolve().layer_dir('normalized', create=True)
        tmp = norm_dir / f'_{NORMALIZED_TABLE}.tmp.json'
        tmp.write_text(json.dumps(payload), encoding='utf-8')
        try:
            if payload:
                rel = conn.read_json(str(tmp))
            else:
                # Explicit schema keeps an empty table well-typed.
                rel = conn.sql(
                    'SELECT NULL::VARCHAR AS slug, NULL::VARCHAR AS set_code, NULL::VARCHAR AS name, '
                    'NULL::VARCHAR AS oracle_id, NULL::VARCHAR AS source, '
                    'NULL::VARCHAR AS first_seen_cursor, NULL::BOOLEAN AS confirmed WHERE 1=0'
                )
            path = store.write_parquet(conn, rel, 'normalized', NORMALIZED_TABLE)
        finally:
            tmp.unlink(missing_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Reconcile — pure over injected data (unit-testable with a stub resolver).
# --------------------------------------------------------------------------- #


def reconcile(
    raw_rows: list[dict[str, str]],
    prior: dict[str, dict[str, object]],
    resolver: CardResolver,
    *,
    cursor_token: str | None,
) -> tuple[list[Spoiler], list[str]]:
    """Reconcile raw preview rows into ``Spoiler`` records + the new-slug list.

    - A slug ABSENT from ``prior`` is NEW (stamped with ``cursor_token`` as
      ``first_seen_cursor``); a slug already in ``prior`` carries its prior
      ``first_seen_cursor`` forward.
    - Each slug is reconciled to a Scryfall identity via ``resolver.get_card`` on
      a slug-derived name guess; a hit sets ``oracle_id`` + ``confirmed=True`` and
      uses the resolved name; a miss stays ``oracle_id=None, confirmed=False`` and
      falls back to the name guess (or a prior-confirmed name/oracle_id if present).

    Returns ``(spoilers, new_slugs)`` where ``new_slugs`` are the slugs not seen
    in ``prior`` — the "new since last sync" set.
    """
    spoilers: list[Spoiler] = []
    new_slugs: list[str] = []
    for row in raw_rows:
        slug = str(row['slug'])
        set_code = str(row.get('set_code') or '')
        prior_row = prior.get(slug)
        is_new = prior_row is None
        if is_new:
            new_slugs.append(slug)
        first_seen = cursor_token if is_new else _opt_str(prior_row.get('first_seen_cursor'))

        name_guess = slug_to_name_guess(slug)
        card = resolver.get_card(name_guess)
        if card is not None and card.oracle_id is not None:
            spoilers.append(
                Spoiler(
                    slug=slug,
                    set_code=set_code,
                    name=card.name or name_guess,
                    oracle_id=str(card.oracle_id),
                    source='scryfall',
                    first_seen_cursor=first_seen,
                    confirmed=True,
                )
            )
            continue

        # Unreconciled preview — keep a prior confirmation if we had one.
        prior_oracle = _opt_str(prior_row.get('oracle_id')) if prior_row else None
        prior_confirmed = bool(prior_row.get('confirmed')) if prior_row else False
        prior_name = _opt_str(prior_row.get('name')) if prior_row else None
        spoilers.append(
            Spoiler(
                slug=slug,
                set_code=set_code,
                name=prior_name or name_guess,
                oracle_id=prior_oracle,
                source='scryfall' if prior_confirmed else 'mythicspoiler',
                first_seen_cursor=first_seen,
                confirmed=prior_confirmed,
            )
        )
    return spoilers, new_slugs


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #


def build(resolver: CardResolver | None = None) -> tuple[Path, list[str]]:
    """Read ``raw/spoilers``, reconcile, materialize ``normalized/spoilers``.

    Returns ``(path, new_slugs)`` — the materialized Parquet path and the slugs
    new since the last run (absent from the prior ``normalized/spoilers``).
    The resolver defaults to the package lake-backed resolver; tests inject a stub.
    """
    if resolver is None:
        from pipeline.collection.resolver import default_card_resolver

        resolver = default_card_resolver()

    raw_rows = _load_raw()
    prior = _load_prior_normalized()
    token = Cursor.load().get(RAW_SOURCE)
    spoilers, new_slugs = reconcile(raw_rows, prior, resolver, cursor_token=token)
    path = _materialize(spoilers)
    log.info(
        'spoilers: reconciled %d previews (%d new, %d confirmed).',
        len(spoilers),
        len(new_slugs),
        sum(1 for s in spoilers if s.confirmed),
    )
    return path, new_slugs


def load_spoilers(set_code: str | None = None, *, only_new: bool = False) -> list[Spoiler]:
    """Read ``normalized/spoilers`` back as ``Spoiler`` records for the CLI façade.

    Optionally filter to a ``set_code`` (case-insensitive). ``only_new`` (the
    ``--new`` verb) filters to UNCONFIRMED spoilers, mirroring today's
    ``list --new`` (which surfaced ``confirmed_by_scryfall = 0`` rows). Fail-open:
    an absent table yields an empty list.
    """
    if not store.table_exists('normalized', NORMALIZED_TABLE):
        return []
    with store.connect() as conn:
        rel = store.read_parquet(conn, 'normalized', NORMALIZED_TABLE)
        rows = rel.select(', '.join(_SPOILER_COLS)).fetchall()
    spoilers: list[Spoiler] = []
    for row in rows:
        rec = dict(zip(_SPOILER_COLS, row, strict=True))
        spoiler = Spoiler(
            slug=str(rec['slug']),
            set_code=str(rec['set_code']),
            name=str(rec['name']),
            oracle_id=_opt_str(rec['oracle_id']),
            source=str(rec['source']),
            first_seen_cursor=_opt_str(rec['first_seen_cursor']),
            confirmed=bool(rec['confirmed']),
        )
        if set_code is not None and spoiler.set_code.lower() != set_code.lower():
            continue
        if only_new and spoiler.confirmed:
            continue
        spoilers.append(spoiler)
    spoilers.sort(key=lambda s: (s.set_code, s.name, s.slug))
    return spoilers


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    path, new_slugs = build()
    print(f'materialized spoilers -> {path} ({len(new_slugs)} new)')


if __name__ == '__main__':
    main()
