"""T7 full-corpus recalc entrypoint — emit the facts graph + label + confidence per deck.

Resolves every gauntlet ``.dck`` (and, optionally, a directory of owned-deck
JSON) against the lake, runs the strategy engine, and writes one record per deck
(the facts graph + named_strategy + confidence + the degree/power + combo
surface). Byte-deterministic output (``sort_keys=True``).

Usage::

    python -m pipeline.transforms.strategy.run                     # gauntlet only -> stdout summary
    python -m pipeline.transforms.strategy.run --out recalc.json   # + full records
    python -m pipeline.transforms.strategy.run --owned ~/decks     # include owned decks

Programmatic::

    from pipeline.transforms.strategy.run import recalc_corpus
    records = recalc_corpus()                       # list[dict], one per deck
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from pipeline.transforms.strategy import StrategyConfig, load_assets, resolve
from pipeline.transforms.strategy.combo import load_combos_from_lake
from pipeline.transforms.strategy.corpus import (
    GAUNTLET_DIR,
    Lake,
    attach_meta,
    build_deck_from_dck,
    gauntlet_decks,
    load_card_meta,
    load_owned,
)


def recalc_corpus(
    gauntlet_dir: Path = GAUNTLET_DIR,
    owned_dir: Path | None = None,
    *,
    cfg: StrategyConfig | None = None,
) -> list[dict]:
    """Resolve the whole corpus to strategy records. Lake-backed; deterministic."""
    assets = load_assets(cfg)
    lake = Lake()
    meta = load_card_meta()
    combos = load_combos_from_lake()
    records: list[dict] = []
    for info in gauntlet_decks(gauntlet_dir):
        deck = attach_meta(build_deck_from_dck(info, lake), meta)
        records.append(resolve(deck, combos, assets=assets))
    if owned_dir is not None:
        for deck in load_owned(owned_dir):
            records.append(resolve(attach_meta(deck, meta), combos, assets=assets))
    return records


def _summary(records: list[dict]) -> str:
    labels = Counter(r['named_strategy'] for r in records)
    conf = Counter(r['confidence'] for r in records if r['confidence'])
    overrides = [r for r in records if r.get('combo_override_applied')]
    f4_blocked = [r for r in records if r.get('combo_override_f4_blocked')]
    lines = [
        f'decks: {len(records)}',
        '  labels: ' + json.dumps(dict(sorted(labels.items(), key=lambda kv: -kv[1])), default=str),
        '  confidence: ' + json.dumps(dict(conf)),
        f'  combo-override fired: {len(overrides)}  (F4-blocked dense primaries: {len(f4_blocked)})',
    ]
    for r in sorted(f4_blocked, key=lambda r: r['name'] or ''):
        lines.append(f"    F4-blocked: {(r.get('commander_hint') or r['name'])[:34]:35} "
                     f"kept {r['pre_combo_name']} (share/margin dense)")
    return '\n'.join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description='Strategy-engine full-corpus recalc (T7).')
    ap.add_argument('--gauntlet', type=Path, default=GAUNTLET_DIR, help='gauntlet .dck root')
    ap.add_argument('--owned', type=Path, default=None, help='optional dir of owned-deck JSON')
    ap.add_argument('--out', type=Path, default=None, help='write full records JSON here')
    args = ap.parse_args()

    records = recalc_corpus(args.gauntlet, args.owned)
    if args.out is not None:
        args.out.write_text(json.dumps(records, indent=2, sort_keys=True, default=list))
        print(f'wrote {len(records)} records -> {args.out}')
    print(_summary(records))


if __name__ == '__main__':
    main()
