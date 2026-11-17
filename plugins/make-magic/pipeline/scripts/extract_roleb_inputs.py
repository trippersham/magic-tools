#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Extract per-deck role-B synthesis inputs from fixture decks or the full gauntlet.

Deterministic: runs compute_facts + name_deck on each deck and writes one JSON
per deck to the role-B work directory.  No LLM calls.

Modes:
    --fixtures   (default) Extract from the 17 committed golden fixtures.
    --gauntlet   Extract from all 170 gauntlet .dck files (lake-backed).

Usage (from pipeline/):
    set -a; . ../.env; set +a
    uv run python scripts/extract_roleb_inputs.py              # fixtures (17)
    uv run python scripts/extract_roleb_inputs.py --gauntlet   # full corpus (170)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---- Resolve the output dir (plan tree) ----
PLAN_ROOT = Path.home() / '.claude/plans/trippersham/magic-tools'
WORK_DIR = PLAN_ROOT / '2026-11-11-strategy-abstraction-consolidation' / 'subagents' / '5.5-roleb-work'
INPUTS_DIR = WORK_DIR / 'inputs'
CHUNKS_DIR = WORK_DIR / 'chunks'
CHUNKS170_DIR = WORK_DIR / 'chunks170'

# ---- Fixture paths ----
FIXTURES = Path(__file__).resolve().parents[1] / 'pipeline' / 'transforms' / 'strategy' / 'tests' / 'fixtures'
DECKS_DIR = FIXTURES / 'decks'
COMBOS_FILE = FIXTURES / 'combos.json'


def load_combos() -> list[dict]:
    return json.loads(COMBOS_FILE.read_text())


def load_deck(slug: str) -> dict:
    return json.loads((DECKS_DIR / f'{slug}.json').read_text())


def extract_one(slug: str, deck: dict, combos: list[dict]) -> dict:
    """Run compute_facts + name_deck and extract the synthesis input for one deck.

    Thin delegate to the packaged single-source builder so the extraction tool
    and the production reader (``engine_synthesis.resolve_engine``) can never
    drift apart (task 7.4).  Any trailing extra fields the gauntlet path adds
    (``_sparse``/``_failed``) are layered on by the caller.
    """
    from pipeline.transforms.strategy.engine_synthesis import build_synthesis_input

    inp = build_synthesis_input(deck, combos, slug=slug)
    return {
        'slug': inp['slug'],
        'deck_name': inp['deck_name'],
        'commanders': inp['commanders'],
        'elected': inp['elected'],
        'archetype_identity': inp['archetype_identity'],
        'detected_combos': inp['detected_combos'],
        'commander_loop': inp['commander_loop'],
        'naming_resolution': inp['naming_resolution'],
        'in_deck_cards': inp['in_deck_cards'],
        'n_nonland': inp['n_nonland'],
        'n_cards': inp['n_cards'],
    }


def chunk_inputs(inputs: list[dict], max_per_chunk: int = 2) -> list[list[dict]]:
    """Split inputs into chunks of at most max_per_chunk decks."""
    chunks = []
    for i in range(0, len(inputs), max_per_chunk):
        chunks.append(inputs[i : i + max_per_chunk])
    return chunks


def gauntlet_slug(bundle: str, stem: str) -> str:
    """Deterministic slug from gauntlet bundle + .dck stem, lowercased."""
    return f'{bundle}__{stem}'.lower()


def main_gauntlet() -> None:
    """Extract role-B inputs from the full 170-deck gauntlet (lake-backed).

    Produces:
      - inputs/<bundle>__<stem>.json for each gauntlet deck (170)
      - chunks170/chunk-NN.json chunked at ~10 decks/chunk
        (170 gauntlet + 6 existing owned__ inputs = 176 total)
    """
    from pipeline.transforms.strategy.combo import load_combos_from_lake
    from pipeline.transforms.strategy.corpus import (
        Lake,
        attach_meta,
        build_deck_from_dck,
        gauntlet_decks,
        load_card_meta,
    )

    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    CHUNKS170_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Load lake resources ----
    print('Loading lake resources...')
    lake = Lake()
    meta = load_card_meta()
    combos = load_combos_from_lake()
    print(f'  {len(combos)} combos from lake')

    # ---- Enumerate gauntlet ----
    infos = gauntlet_decks()
    print(f'  {len(infos)} gauntlet decks found')

    gauntlet_inputs: list[dict] = []
    failures: list[dict] = []
    sparse: list[dict] = []

    for info in infos:
        stem = info['path'].stem
        bundle = info['bundle']
        slug = gauntlet_slug(bundle, stem)

        try:
            deck = attach_meta(build_deck_from_dck(info, lake), meta)
            inp = extract_one(slug, deck, combos)

            # Flag sparse decks (stub-like, very few nonland cards)
            if inp['n_nonland'] < 10:
                inp['_sparse'] = True
                sparse.append({'slug': slug, 'n_nonland': inp['n_nonland'], 'n_cards': inp['n_cards']})

            out_path = INPUTS_DIR / f'{slug}.json'
            out_path.write_text(json.dumps(inp, indent=2, ensure_ascii=False) + '\n')
            gauntlet_inputs.append(inp)
            print(
                f'  OK {slug}: {len(inp["elected"])} elected, '
                f'{sum(1 for c in inp["detected_combos"] if c["win"])} win-combos, '
                f'n_nonland={inp["n_nonland"]}, '
                f'archetype={[a["strategy"] for a in inp["archetype_identity"][:3]]}'
            )
        except Exception as e:
            print(f'  FAIL {slug}: {e}')
            # Write a grounded-on-name/commander minimal input
            # Try to get commander names from the raw deck
            try:
                raw_deck = build_deck_from_dck(info, lake)
                cmd_names = [c['name'] for c in raw_deck['cards'] if c.get('role') == 'commander']
            except Exception:
                cmd_names = []
            inp = {
                'slug': slug,
                'deck_name': stem,
                'commanders': [{'name': n, 'oracle_text': ''} for n in cmd_names],
                'elected': [],
                'archetype_identity': [],
                'detected_combos': [],
                'commander_loop': None,
                'naming_resolution': {
                    'named_strategy': None,
                    'combo_override_applied': False,
                    'combo_identity': None,
                    'pre_combo_name': None,
                    'confidence': 'LOW',
                    'combo_override_blocked': False,
                },
                'n_nonland': 0,
                'n_cards': 0,
                '_failed': True,
                '_error': str(e),
            }
            out_path = INPUTS_DIR / f'{slug}.json'
            out_path.write_text(json.dumps(inp, indent=2, ensure_ascii=False) + '\n')
            gauntlet_inputs.append(inp)
            failures.append({'slug': slug, 'error': str(e)})

    # ---- Collect existing non-gauntlet inputs (owned__*) for chunking ----
    gauntlet_slugs = {inp['slug'] for inp in gauntlet_inputs}
    # Also exclude old fixture slugs that duplicate gauntlet decks
    # (gauntlet__* and precon__* are now covered by bundle__stem slugs)
    owned_inputs: list[dict] = []
    for f in sorted(INPUTS_DIR.glob('*.json')):
        fslug = f.stem
        if fslug in gauntlet_slugs:
            continue
        if fslug.startswith('owned__'):
            owned_inputs.append(json.loads(f.read_text()))

    # ---- Chunk: gauntlet + owned, sorted by slug, ~10/chunk ----
    all_for_chunks = sorted(gauntlet_inputs + owned_inputs, key=lambda x: x['slug'])
    chunks = chunk_inputs(all_for_chunks, max_per_chunk=10)
    for i, chunk in enumerate(chunks):
        chunk_path = CHUNKS170_DIR / f'chunk-{i:02d}.json'
        chunk_path.write_text(json.dumps(chunk, indent=2, ensure_ascii=False) + '\n')

    # ---- Summary ----
    print(f'\n{"=" * 60}')
    print(f'Gauntlet extracted: {len(gauntlet_inputs)}')
    print(f'Owned (from existing fixtures): {len(owned_inputs)}')
    print(f'Total for synthesis: {len(all_for_chunks)}')
    print(f'Chunks: {len(chunks)} @ ~10 decks/chunk')
    print(f'Inputs dir: {INPUTS_DIR}')
    print(f'Chunks dir: {CHUNKS170_DIR}')
    if failures:
        print(f'\nFailed extractions ({len(failures)}):')
        for f in failures:
            print(f'  {f["slug"]}: {f["error"]}')
    if sparse:
        print(f'\nSparse decks ({len(sparse)}):')
        for s in sparse:
            print(f'  {s["slug"]}: {s["n_nonland"]} nonland, {s["n_cards"]} total')
    print(f'{"=" * 60}')


def main() -> None:
    combos = load_combos()
    slugs = sorted(f.stem for f in DECKS_DIR.iterdir() if f.suffix == '.json')

    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)

    all_inputs: list[dict] = []
    for slug in slugs:
        deck = load_deck(slug)
        inp = extract_one(slug, deck, combos)
        # Write per-deck input
        out_path = INPUTS_DIR / f'{slug}.json'
        out_path.write_text(json.dumps(inp, indent=2, ensure_ascii=False) + '\n')
        all_inputs.append(inp)
        print(
            f'  {slug}: {len(inp["elected"])} elected, '
            f'{sum(1 for c in inp["detected_combos"] if c["win"])} win-combos, '
            f'archetype={[a["strategy"] for a in inp["archetype_identity"]]}'
        )

    # Write chunks (~2 decks per chunk for sequential agent-layer processing)
    chunks = chunk_inputs(all_inputs, max_per_chunk=2)
    for i, chunk in enumerate(chunks):
        chunk_path = CHUNKS_DIR / f'chunk-{i:02d}.json'
        chunk_path.write_text(json.dumps(chunk, indent=2, ensure_ascii=False) + '\n')

    print(f'\nDone: {len(all_inputs)} inputs, {len(chunks)} chunks')
    print(f'Inputs: {INPUTS_DIR}')
    print(f'Chunks: {CHUNKS_DIR}')


if __name__ == '__main__':
    if '--gauntlet' in sys.argv:
        main_gauntlet()
    else:
        main()
