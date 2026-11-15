#!/usr/bin/env python3
"""Resilience-30: run the PRODUCTION role-B path (resolve_engine, claude -p) on the
30 selected external decks.  Sequential, idempotent (isolated cache dir), no
writes outside subagents/7.7-resilience-30/.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
DCK_DIR = HERE / 'dck'
CACHE_DIR = HERE / 'engines'
OUT = HERE / 'engines_30.jsonl'

# 30 selected of the 37 fetched (7 dropped for set-redundancy, see outbox).
SELECTED = [
    'Science_PIP', 'MutantMenace_PIP', 'HailCaesar_PIP',
    'LivingEnergy_DRC',
    'DeathToll_DSC', 'JumpScare_DSC',
    'BlastFromThePast_WHO', 'TimeyWimey_WHO', 'ParadoxPower_WHO',
    'CounterBlitzFinalFantasyX_FIC', 'LimitBreakFinalFantasyVii_FIC',
    'RevivalTranceFinalFantasyVi_FIC', 'ScionsSpellcraftFinalFantasyXiv_FIC',
    'CounterIntelligence_EOC', 'WorldShaper_EOC',
    'BloodRites_LCC', 'VelociRampTor_LCC',
    'FoodAndFellowship_LTC', 'TheHostsOfMordor_LTC', 'ElvenCouncil_LTC',
    'VirtueAndValor_WOC',
    'PartyTime_CLB', 'DraconicDissent_CLB', 'ExitFromExile_CLB',
    'CorruptingInfluence_ONC',
    'MishraSBurnishedBanner_BRC',
    'PlaneswalkerParty_CMM', 'SliverSwarm_CMM',
    'Painbow_DMC',
    'TheRuinousPowers_40K',
]

from pipeline.transforms.strategy.combo import load_combos_from_lake
from pipeline.transforms.strategy.corpus import (
    Lake, attach_meta, build_deck_from_dck, load_card_meta,
)
from pipeline.transforms.strategy.engine_synthesis import (
    ClaudeCliBackend, resolve_engine,
)


def main() -> int:
    assert len(SELECTED) == 30, len(SELECTED)
    lake = Lake()
    meta = load_card_meta()
    combos = load_combos_from_lake()
    backend = ClaudeCliBackend()
    CACHE_DIR.mkdir(exist_ok=True)

    lines = []
    counts = {'hit': 0, 'synthesized': 0, 'miss': 0, 'error': 0}
    for stem in SELECTED:
        slug = f'resilience30__{stem.lower()}'
        info = {'path': DCK_DIR / f'{stem}.dck', 'bundle': 'resilience30',
                'bracket': 2, 'commander_hint': None, 'source': 'external'}
        try:
            deck = attach_meta(build_deck_from_dck(info, lake), meta)
            engine, m = resolve_engine(
                deck, combos, slug=slug, backend=backend,
                cache_dir=CACHE_DIR, allow_synth=True, persist=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f'FAIL {slug}: {exc}', flush=True)
            traceback.print_exc()
            counts['error'] += 1
            lines.append(json.dumps({'slug': slug, 'status': 'driver-error', 'error': str(exc)}))
            continue
        counts[m['status']] = counts.get(m['status'], 0) + 1
        row = {'slug': slug, 'status': m['status'],
               'engine': engine.as_dict() if engine else None,
               'problems': m.get('problems')}
        lines.append(json.dumps(row, ensure_ascii=False))
        if engine:
            print(f"{m['status'].upper():12s} {slug}: {engine.strategy} [{engine.confidence}]", flush=True)
        else:
            print(f"{m['status'].upper():12s} {slug}: NO ENGINE problems={m.get('problems')}", flush=True)

    OUT.write_text('\n'.join(lines) + '\n')
    print(f'\nSUMMARY: {counts}')
    return 0 if counts['error'] == 0 and counts['miss'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
