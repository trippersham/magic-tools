#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Materialize per-deck core Engines (role-B / L5) into the engine cache.

The production materialization driver (task 7.4).  For each deck it:

  1. extracts the role-B input on the CURRENT engine (B1 combo-edges + B6
     archetype-gated election), via the packaged single-source builder;
  2. computes the staleness key and SKIPS decks already fresh in the cache;
  3. on a miss, calls ``claude -p`` (headless Claude Code, ambient auth -- NO
     ANTHROPIC_API_KEY) to synthesise the Engine;
  4. verifies the JSON is well-formed and ``key_pieces`` are in-deck (rejects +
     logs otherwise);
  5. writes the verified Engine to the JSONL cache.

Sequential, idempotent, resumable.  Never fans ``claude -p`` out in parallel.

Deck sources
------------
``--fixtures``   the 17 committed golden fixtures (incl. all 7 B-test anchors);
                 lake-free, fast -- the representative re-validation sample.
``--gauntlet``   all 170 gauntlet .dck files (lake-backed) -- the full corpus.
``--slugs a,b``  restrict either source to a comma-separated slug list.

Usage (from pipeline/):
    set -a; . ../.env; set +a
    uv run python scripts/materialize_engines.py --fixtures
    uv run python scripts/materialize_engines.py --gauntlet --model claude-sonnet-4-5
    uv run python scripts/materialize_engines.py --fixtures --dry-run   # extract+hash only
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from pipeline.transforms.strategy import engine_cache
from pipeline.transforms.strategy.engine_synthesis import (
    DEFAULT_MODEL,
    ClaudeCliBackend,
    SynthError,
    build_synthesis_input,
    deck_card_names,
    synthesize_engine,
)

# ---- Fixture paths (mirror extract_roleb_inputs.py) ----
FIXTURES = Path(__file__).resolve().parents[1] / 'pipeline' / 'transforms' / 'strategy' / 'tests' / 'fixtures'
DECKS_DIR = FIXTURES / 'decks'
COMBOS_FILE = FIXTURES / 'combos.json'


def gauntlet_slug(bundle: str, stem: str) -> str:
    return f'{bundle}__{stem}'.lower()


def iter_fixture_decks(slugs: set[str] | None):
    """Yield (slug, deck, combos) for the committed golden fixtures."""
    combos = json.loads(COMBOS_FILE.read_text())
    for f in sorted(DECKS_DIR.glob('*.json')):
        slug = f.stem
        if slugs is not None and slug not in slugs:
            continue
        deck = json.loads(f.read_text())
        yield slug, deck, combos


def iter_gauntlet_decks(slugs: set[str] | None):
    """Yield (slug, deck, combos) for the full 170-deck gauntlet (lake-backed)."""
    from pipeline.transforms.strategy.combo import load_combos_from_lake
    from pipeline.transforms.strategy.corpus import (
        Lake,
        attach_meta,
        build_deck_from_dck,
        gauntlet_decks,
        load_card_meta,
    )

    lake = Lake()
    meta = load_card_meta()
    combos = load_combos_from_lake()
    for info in gauntlet_decks():
        slug = gauntlet_slug(info['bundle'], info['path'].stem)
        if slugs is not None and slug not in slugs:
            continue
        deck = attach_meta(build_deck_from_dck(info, lake), meta)
        yield slug, deck, combos


def materialize(
    deck_iter,
    *,
    backend: ClaudeCliBackend | None,
    cache_dir: Path | None,
    dry_run: bool,
    outputs_jsonl: Path | None,
) -> dict:
    """Drive synthesis over ``deck_iter``.  Returns a summary dict."""
    cache = engine_cache.load_cache(cache_dir)
    summary = {'hit': 0, 'synthesized': 0, 'rejected': 0, 'failed': 0, 'dry': 0}
    out_lines: list[str] = []

    for slug, deck, combos in deck_iter:
        try:
            roleb_input = build_synthesis_input(deck, combos, slug=slug)
        except Exception as exc:  # noqa: BLE001 - driver must not die on one deck
            print(f'  FAIL  {slug}: input extraction error: {exc}')
            traceback.print_exc()
            summary['failed'] += 1
            continue

        ih = engine_cache.input_hash(roleb_input)

        if cache.is_fresh(slug, ih):
            rec = cache.get(slug)
            assert rec is not None
            print(f'  HIT   {slug}: {rec.engine.strategy} [{rec.engine.confidence}]')
            summary['hit'] += 1
            if outputs_jsonl is not None:
                out_lines.append(json.dumps({'slug': slug, 'engine': rec.engine.as_dict()}, ensure_ascii=False))
            continue

        if dry_run or backend is None:
            print(f'  MISS  {slug}: input_hash={ih[:12]} (dry-run; would synthesise)')
            summary['dry'] += 1
            continue

        try:
            engine = synthesize_engine(
                roleb_input, backend, card_names=deck_card_names(deck),
            )
        except SynthError as exc:
            print(f'  REJECT {slug}: {exc}')
            summary['rejected'] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            print(f'  FAIL  {slug}: synthesis error: {exc}')
            summary['failed'] += 1
            continue

        record = engine_cache.make_record(
            slug,
            roleb_input.get('deck_name', slug),
            roleb_input,
            engine,
            model_id=backend.model,
        )
        engine_cache.persist(record, cache_dir)
        # Keep the in-memory cache current so a resumed loop sees the write.
        cache.records[slug] = record
        print(f'  SYNTH {slug}: {engine.strategy} [{engine.confidence}] '
              f'-> {len(engine.key_pieces)} key pieces')
        summary['synthesized'] += 1
        if outputs_jsonl is not None:
            out_lines.append(json.dumps({'slug': slug, 'engine': engine.as_dict()}, ensure_ascii=False))

    if outputs_jsonl is not None and out_lines:
        outputs_jsonl.write_text('\n'.join(out_lines) + '\n', encoding='utf-8')
        print(f'\nWrote {len(out_lines)} engines to {outputs_jsonl}')

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group()
    src.add_argument('--fixtures', action='store_true', help='17 golden fixtures (default)')
    src.add_argument('--gauntlet', action='store_true', help='full 170-deck gauntlet (lake)')
    ap.add_argument('--slugs', default='', help='comma-separated slug allow-list')
    ap.add_argument('--model', default=DEFAULT_MODEL, help=f'claude -p model (default {DEFAULT_MODEL})')
    ap.add_argument('--cache-dir', default='', help='override engine-cache dir (default XDG)')
    ap.add_argument('--dry-run', action='store_true', help='extract + hash only; no claude -p')
    ap.add_argument('--outputs', default='', help='also write a measure-harness JSONL here')
    args = ap.parse_args()

    slugs = {s.strip() for s in args.slugs.split(',') if s.strip()} or None
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    outputs_jsonl = Path(args.outputs) if args.outputs else None
    backend = None if args.dry_run else ClaudeCliBackend(model=args.model)

    if args.gauntlet:
        deck_iter = iter_gauntlet_decks(slugs)
        src_name = 'gauntlet (170)'
    else:
        deck_iter = iter_fixture_decks(slugs)
        src_name = 'fixtures (17)'

    print(f'Materializing engines from {src_name}'
          f'{" [dry-run]" if args.dry_run else f" via claude -p --model {args.model}"}')
    print(f'Cache dir: {cache_dir or engine_cache._DEFAULT_STORE_DIR}\n')

    summary = materialize(
        deck_iter,
        backend=backend,
        cache_dir=cache_dir,
        dry_run=args.dry_run,
        outputs_jsonl=outputs_jsonl,
    )

    print(f'\n{"="*60}')
    print(f'SUMMARY: {summary}')
    print(f'{"="*60}')
    # Non-zero exit if any deck was rejected or failed (operator should look).
    return 1 if (summary['rejected'] or summary['failed']) else 0


if __name__ == '__main__':
    sys.exit(main())
