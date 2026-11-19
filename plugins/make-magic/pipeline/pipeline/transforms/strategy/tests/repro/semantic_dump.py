"""Component 2 — per-deck semantic extraction dump across the five engine layers.

Observe-only instrument (nothing shipped imports this). For every deck in the
corpus (170 gauntlet + 17 hermetic fixtures) it serializes one *normalized*
record of the five strategy-engine layers, sourced ENTIRELY from the existing
``resolve()`` record (which already carries ``ctx.facts`` under ``rec['facts']``)
plus the deck's input card pool — nothing is recomputed via new queries.

Layers (field-source map, verified against ``resolve.py`` / ``strategy_graph.py``
/ ``naming.py`` @ 327e57a):

* ``attributes``   — ``rec['facts']['attributes_present']`` keys (the admitted
  node attributes); sorted + deduped.
* ``events``       — ``rec['facts']['engines']['engine_units']['engines']``: every
  derived event with its in-deck ``produced_by`` membership (``producers``) and
  ``triggers`` membership (``consumers``).
* ``eventeffects`` — ``...['engine_units']['forward_chains']``: event→event edges
  (feeder_event → fed_event).
* ``archetype_identity`` — the compatibility/naming projection merged onto the
  record (NOT the strategy graph itself): ``archetype_identity`` (ranked
  {strategy, weighted_quantity, share}), ``ranked_identity``, ``combo_identity``,
  ``headline`` (derived via ``naming.headline_strategy``), ``pre_combo_name``,
  ``confidence``, ``bracket``, ``n_win_combos``, ``n_detected_combos``.
* ``engines``      — the ELECTED ``...['engine_units']['ranked_units']`` (what
  "a deck gained/lost an engine" means), keyed by event, each enriched with
  ``producers``/``consumers``/``kind`` joined from the complete ``engines`` list
  by event name: {event → {producers, consumers, payoff_archetypes, mass, kind}}.
* ``pool``         — the deck's resolved card list as sorted ``[name, oid]`` pairs
  (the INPUT, read from ``deck['cards']`` — not a computed layer), so the differ
  can attribute divergence to a pool change vs retagging.

Normalization: every collection is sorted deterministically, all floats are
rounded to 6 dp, and presentation-only fields are never emitted. ``created_at``
and ``env`` live ONLY under ``meta`` and are excluded from comparison by the
differ.

CLI::

    uv run python -m pipeline.transforms.strategy.tests.repro.semantic_dump --out dump.json
    uv run python -m pipeline.transforms.strategy.tests.repro.semantic_dump --out d.json --decks 3
    uv run python -m pipeline.transforms.strategy.tests.repro.semantic_dump --out d.json --only ozai
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.transforms.strategy.naming import headline_strategy

#: The five engine layers + the input card pool, in dump order.
LAYERS: tuple[str, ...] = ('attributes', 'events', 'eventeffects', 'archetype_identity', 'engines', 'pool')

#: Frozen clock used by ``--frozen-meta`` so two runs are byte-identical.
_FROZEN_CLOCK = '1970-01-01T00:00:00+00:00'

_FLOAT_DP = 6


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #


def _r(x: Any) -> Any:
    """Round a float to the fixed precision; pass everything else through."""
    return round(x, _FLOAT_DP) if isinstance(x, float) else x


def _engine_units(rec: dict) -> dict:
    return rec['facts']['engines']['engine_units']


# --------------------------------------------------------------------------- #
# Per-layer extractors (pure; read the resolve() record / deck input)
# --------------------------------------------------------------------------- #


def _attributes_layer(rec: dict) -> list[str]:
    return sorted(rec['facts']['attributes_present'])


def _events_layer(rec: dict) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    for e in _engine_units(rec)['engines']:
        out[e['event']] = {
            'producers': sorted(e['producers']),
            'consumers': sorted(e['consumers']),
        }
    return out


def _eventeffects_layer(rec: dict) -> list[list[str]]:
    return sorted([list(pair) for pair in _engine_units(rec)['forward_chains']])


def _engines_layer(rec: dict) -> dict[str, dict[str, Any]]:
    eu = _engine_units(rec)
    by_event = {e['event']: e for e in eu['engines']}
    out: dict[str, dict[str, Any]] = {}
    for u in eu['ranked_units']:
        ev = u['event']
        full = by_event.get(ev, {})
        out[ev] = {
            'producers': sorted(full.get('producers', [])),
            'consumers': sorted(full.get('consumers', [])),
            'payoff_archetypes': sorted(u.get('payoff_archetypes', [])),
            'mass': _r(float(u['mass'])),
            'kind': 'recursive-engine' if u.get('recursive') else 'static-payoff',
        }
    return out


def _archetype_identity_layer(rec: dict) -> dict[str, Any]:
    ai = [
        {
            'strategy': d['strategy'],
            'weighted_quantity': _r(float(d['weighted_quantity'])),
            'share': _r(float(d['share'])),
        }
        for d in (rec.get('archetype_identity') or [])
    ]
    ranked = [[a, _r(float(w)), bool(rm)] for a, w, rm in (rec.get('ranked_identity') or [])]
    return {
        'archetype_identity': ai,
        'ranked_identity': ranked,
        'combo_identity': rec.get('combo_identity'),
        'headline': headline_strategy(rec),
        'pre_combo_name': rec.get('pre_combo_name'),
        'confidence': rec.get('confidence'),
        'bracket': rec.get('bracket'),
        'n_win_combos': rec.get('n_win_combos'),
        'n_detected_combos': rec.get('n_detected_combos'),
    }


def _pool_layer(deck: dict) -> list[list[Any]]:
    return sorted([[c.get('name'), c.get('oracle_id')] for c in deck.get('cards', [])])


def build_record(deck: dict, rec: dict) -> dict:
    """Assemble the normalized per-deck record from a resolve() record + its deck.

    Pure: does not mutate ``deck`` or ``rec``.
    """
    return {
        'attributes': _attributes_layer(rec),
        'events': _events_layer(rec),
        'eventeffects': _eventeffects_layer(rec),
        'archetype_identity': _archetype_identity_layer(rec),
        'engines': _engines_layer(rec),
        'pool': _pool_layer(deck),
    }


def deck_key(rec: dict) -> str:
    """Unique per-deck key: ``<bundle|source>::<name>`` (never name alone).

    The gauntlet has recurring deck names across bundles, so the key is scoped
    by bundle (gauntlet) / source; fixtures pass an explicit ``fixture::<slug>``.
    """
    bundle = rec.get('bundle') or rec.get('source') or 'deck'
    return f'{bundle}::{rec.get("name")}'


# --------------------------------------------------------------------------- #
# Corpus iteration (lake-backed) — mirrors run.recalc_corpus deck-for-deck.
# --------------------------------------------------------------------------- #


def iter_gauntlet(
    limit: int | None = None,
    only: str | None = None,
    *,
    cfg: object | None = None,
) -> Iterator[tuple[str, dict, dict]]:
    """Yield ``(key, deck, resolve_record)`` for every gauntlet .dck, in recalc order."""
    from pipeline.transforms.strategy import load_assets, resolve
    from pipeline.transforms.strategy.combo import load_combos_from_lake
    from pipeline.transforms.strategy.corpus import (
        Lake,
        attach_meta,
        build_deck_from_dck,
        gauntlet_decks,
        load_card_meta,
    )

    assets = load_assets(cfg)  # type: ignore[arg-type]
    lake = Lake()
    meta = load_card_meta()
    combos = load_combos_from_lake()
    n = 0
    for info in gauntlet_decks():
        deck = attach_meta(build_deck_from_dck(info, lake), meta)
        if only is not None and only.lower() not in (deck.get('name') or '').lower():
            continue
        rec = resolve(deck, combos, assets=assets)
        yield deck_key(rec), deck, rec
        n += 1
        if limit is not None and n >= limit:
            break


def iter_fixtures(only: str | None = None, *, cfg: object | None = None) -> Iterator[tuple[str, dict, dict]]:
    """Yield ``(key, deck, resolve_record)`` for every hermetic fixture deck."""
    from pipeline.transforms.strategy import load_assets, resolve
    from pipeline.transforms.strategy.tests.engine_parity_oracle import (
        DECKS,
        _fixture_slugs,
        _load_combos,
    )

    assets = load_assets(cfg)  # type: ignore[arg-type]
    combos = _load_combos()
    for slug in _fixture_slugs():
        if only is not None and only.lower() not in slug.lower():
            continue
        deck = json.loads((DECKS / f'{slug}.json').read_text())
        rec = resolve(deck, list(combos), assets=assets)
        yield f'fixture::{slug}', deck, rec


# --------------------------------------------------------------------------- #
# Meta (lake shas + composites) — excluded from comparison by the differ.
# --------------------------------------------------------------------------- #


def _sha256_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = path.read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def lake_file_shas() -> dict[str, Any]:
    """sha256 + byte length of the three lake parquet files under the active data dir."""
    from pipeline.transforms.strategy.corpus import lake_root

    root = lake_root()
    return {
        'raw/oracle_cards.parquet': _sha256_file(root / 'raw' / 'oracle_cards.parquet'),
        'raw/oracle_tags.parquet': _sha256_file(root / 'raw' / 'oracle_tags.parquet'),
        'normalized/card_otag.parquet': _sha256_file(root / 'normalized' / 'card_otag.parquet'),
        'root': str(root),
    }


def _sha_json(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=list).encode()).hexdigest()


def _composites(gauntlet_recs: list[dict], fixture_recs: dict[str, dict], *, full_gauntlet: bool) -> dict[str, Any]:
    """Cheap tripwire composites computed from the records already in hand.

    ``full_corpus`` matches ``engine_parity_oracle.full_corpus_sha()`` only on a
    complete gauntlet run (no --decks/--only); otherwise it is reported as a
    partial hash labelled with the record count.
    """
    out: dict[str, Any] = {}
    if gauntlet_recs:
        blob = json.dumps(gauntlet_recs, sort_keys=True, separators=(',', ':'), default=list).encode()
        out['full_corpus'] = {
            'sha': hashlib.sha256(blob).hexdigest(),
            'n': len(gauntlet_recs),
            'canonical': full_gauntlet,
        }
    if fixture_recs:
        engine_map = {s: _sha_json(_engine_units(r)) for s, r in fixture_recs.items()}
        resolve_map = {s: _sha_json(r) for s, r in fixture_recs.items()}
        out['engine'] = {'sha': _sha_json(engine_map), 'n': len(engine_map)}
        out['resolve'] = {'sha': _sha_json(resolve_map), 'n': len(resolve_map)}
    return out


def build_dump(
    *,
    limit: int | None = None,
    only: str | None = None,
    include_gauntlet: bool = True,
    include_fixtures: bool = True,
    frozen_meta: bool = False,
    cfg: object | None = None,
) -> dict:
    """Build the full ``{meta, decks}`` dump. Lake-backed when gauntlet is included."""
    decks: dict[str, dict] = {}
    gauntlet_recs: list[dict] = []
    fixture_recs: dict[str, dict] = {}

    def _add(key: str, deck: dict, rec: dict) -> None:
        k, i = key, 2
        while k in decks:  # deterministic collision guard (never expected)
            k = f'{key}#{i}'
            i += 1
        decks[k] = build_record(deck, rec)

    if include_gauntlet:
        for key, deck, rec in iter_gauntlet(limit, only, cfg=cfg):
            _add(key, deck, rec)
            gauntlet_recs.append(rec)
    if include_fixtures:
        for key, deck, rec in iter_fixtures(only, cfg=cfg):
            _add(key, deck, rec)
            fixture_recs[key.split('::', 1)[1]] = rec

    full_gauntlet = include_gauntlet and limit is None and only is None
    meta: dict[str, Any] = {
        'created_at': _FROZEN_CLOCK if frozen_meta else datetime.now(UTC).isoformat(),
        'env': {}
        if frozen_meta
        else {
            'python': sys.version.split()[0],
            'platform': platform.platform(),
            'PYTHONHASHSEED': os.environ.get('PYTHONHASHSEED', 'unset'),
        },
        'lake_file_shas': lake_file_shas(),
        'composites': _composites(gauntlet_recs, fixture_recs, full_gauntlet=full_gauntlet),
        'n_gauntlet': len(gauntlet_recs),
        'n_fixtures': len(fixture_recs),
        'limit': limit,
        'only': only,
    }
    return {'meta': meta, 'decks': decks}


def serialize(dump: dict) -> str:
    """Byte-deterministic JSON for a dump (sorted keys, 2-space indent)."""
    return json.dumps(dump, sort_keys=True, indent=2, default=list)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Per-deck semantic extraction dump (reproducibility instrument).')
    ap.add_argument('--out', type=Path, required=True, help='write the dump JSON here')
    ap.add_argument('--decks', type=int, default=None, help='limit gauntlet to the first N decks')
    ap.add_argument('--only', type=str, default=None, help='only decks whose name/slug contains this substring')
    ap.add_argument('--no-gauntlet', action='store_true', help='skip the lake-backed gauntlet (fixtures only)')
    ap.add_argument('--no-fixtures', action='store_true', help='skip the hermetic fixtures')
    ap.add_argument('--frozen-meta', action='store_true', help='freeze created_at/env for byte-identical reruns')
    args = ap.parse_args(argv)

    dump = build_dump(
        limit=args.decks,
        only=args.only,
        include_gauntlet=not args.no_gauntlet,
        include_fixtures=not args.no_fixtures,
        frozen_meta=args.frozen_meta,
    )
    args.out.write_text(serialize(dump))
    print(
        f'wrote {len(dump["decks"])} deck records '
        f'({dump["meta"]["n_gauntlet"]} gauntlet + {dump["meta"]["n_fixtures"]} fixtures) -> {args.out}'
    )
    comp = dump['meta']['composites']
    if 'full_corpus' in comp:
        tag = 'canonical' if comp['full_corpus']['canonical'] else 'partial'
        print(f'  full_corpus[{tag}] {comp["full_corpus"]["sha"][:16]}... (n={comp["full_corpus"]["n"]})')
    if 'engine' in comp:
        print(f'  engine   {comp["engine"]["sha"][:16]}...   resolve  {comp["resolve"]["sha"][:16]}...')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
