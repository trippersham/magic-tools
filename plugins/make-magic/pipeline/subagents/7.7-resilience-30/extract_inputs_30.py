#!/usr/bin/env python3
"""Resilience-30: resolve candidate .dck files against the lake and extract role-B inputs.

Read-only against the lake. Writes inputs/<slug>.json + resolution_report.json
under subagents/7.7-resilience-30/.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
DCK_DIR = HERE / 'dck'
INPUTS_DIR = HERE / 'inputs'
INPUTS_DIR.mkdir(exist_ok=True)

from pipeline.transforms.strategy.combo import load_combos_from_lake
from pipeline.transforms.strategy.corpus import (
    Lake,
    attach_meta,
    build_deck_from_dck,
    load_card_meta,
)
from pipeline.transforms.strategy.engine_synthesis import build_synthesis_input


def main() -> int:
    lake = Lake()
    meta = load_card_meta()
    combos = load_combos_from_lake()
    report = []
    for p in sorted(DCK_DIR.glob('*.dck')):
        slug = f'resilience30__{p.stem.lower()}'
        info = {'path': p, 'bundle': 'resilience30', 'bracket': 2,
                'commander_hint': None, 'source': 'external'}
        try:
            deck = attach_meta(build_deck_from_dck(info, lake), meta)
            n = len(deck['cards'])
            unresolved = deck['unresolved']
            tagged = sum(1 for c in deck['cards'] if c['otags'])
            inp = build_synthesis_input(deck, combos, slug=slug)
            (INPUTS_DIR / f'{slug}.json').write_text(
                json.dumps(inp, indent=2, ensure_ascii=False) + '\n')
            row = {
                'slug': slug, 'stem': p.stem, 'n_cards': n,
                'unresolved': unresolved, 'n_unresolved': len(unresolved),
                'otag_coverage': round(tagged / n, 3),
                'named_strategy': inp['naming_resolution']['named_strategy'],
                'confidence': inp['naming_resolution']['confidence'],
                'archetypes': [(a['strategy'], a['share']) for a in inp['archetype_identity'][:4]],
                'elected': [(e.get('event'), round(e.get('share', 0), 3)) for e in inp['elected'][:4]],
                'win_combos': sum(1 for c in inp['detected_combos'] if c['win']),
            }
        except Exception as e:
            row = {'slug': slug, 'stem': p.stem, 'error': str(e)}
            traceback.print_exc()
        report.append(row)
        print(json.dumps(row, ensure_ascii=False))
    (HERE / 'resolution_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
    bad = [r for r in report if r.get('error') or r.get('n_unresolved')]
    print(f'\n{len(report)} decks, {len(bad)} with problems')
    return 0


if __name__ == '__main__':
    sys.exit(main())
