"""Engine-isolation byte-parity oracle for the strategy engine.

Records a SHA over (a) the serialized ``elect_engine_units`` output and (b) the
full ``resolve()`` records for the hermetic fixture corpus, so later phases can
prove they didn't shift the HELD curated layer.

**Run standalone** to print + record baseline SHAs::

    cd plugins/make-magic/pipeline
    uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle

**Import** in tests::

    from pipeline.transforms.strategy.tests.engine_parity_oracle import (
        compute_engine_isolation_baselines,
        compute_resolve_baselines,
        composite_sha,
        full_corpus_sha,
        FULL_CORPUS_SHA,
    )

Limitation
----------
The oracle covers the **17 hermetic fixture decks** (offline, deterministic).
The full 170-deck curated corpus requires lake access (DuckDB + card-meta +
combo tables via ``recalc_corpus()`` in ``run.py``) and is NOT reproducible
offline.  When lake access IS available, ``full_corpus_sha()`` computes the
**canonical** full-corpus SHA using a fixed serialization method (see its
docstring).  The recorded baseline is ``FULL_CORPUS_SHA``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline.transforms.strategy import resolve
from pipeline.transforms.strategy.strategy_graph import compute_facts

FIX = Path(__file__).resolve().parent / 'fixtures'
DECKS = FIX / 'decks'
ENGINE_ISOLATION_BASELINE_FILE = FIX / 'engine_isolation_baseline.json'


def _sha256(obj: object) -> str:
    """Stable SHA-256 of a JSON-serializable object."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=list).encode()).hexdigest()


def _fixture_slugs() -> list[str]:
    """All fixture deck slugs, sorted (the hermetic corpus)."""
    return sorted(p.stem for p in DECKS.glob('*.json'))


def _load_combos() -> list[dict]:
    return [dict(c) for c in json.loads((FIX / 'combos.json').read_text())]


def compute_engine_isolation_baselines() -> dict[str, str]:
    """Per-deck SHA of ``facts['engines']['engine_units']`` (the ``elect_engine_units`` output).

    This is the (a) oracle: a SHA over the held engine-units layer's serialized
    output for each hermetic fixture deck.
    """
    combos = _load_combos()
    baselines: dict[str, str] = {}
    for slug in _fixture_slugs():
        deck = json.loads((DECKS / f'{slug}.json').read_text())
        ctx = compute_facts(deck, list(combos))
        engine_iso = ctx.facts['engines']['engine_units']
        baselines[slug] = _sha256(engine_iso)
    return baselines


def compute_resolve_baselines() -> dict[str, str]:
    """Per-deck SHA of the full ``resolve()`` record.

    This is the (b) oracle: matches the existing ``distill_parity_baseline.json``
    format and can cross-check against it.
    """
    combos = _load_combos()
    baselines: dict[str, str] = {}
    for slug in _fixture_slugs():
        deck = json.loads((DECKS / f'{slug}.json').read_text())
        rec = resolve(deck, list(combos))
        baselines[slug] = _sha256(rec)
    return baselines


def composite_sha(per_deck: dict[str, str]) -> str:
    """A single SHA over the sorted per-deck SHA map (the one-number parity check).

    Later phases compare this one number to prove the HELD layer didn't shift.
    """
    return hashlib.sha256(json.dumps(per_deck, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Full-corpus canonical SHA (lake-backed, reproducible)
# ---------------------------------------------------------------------------

# The canonical full-corpus SHA, computed by ``full_corpus_sha()`` below.
# Supersedes the ad-hoc ``88de96a7`` value recorded in ADR 0003 (which was
# computed by an unrecoverable method and is NOT reproducible).
# Re-baselined 2026-11-17 (#74 decision 5): wiring the impulse cheat-into-play
# seeds (sneak-from-library/sneak-artifact) moved the full-corpus SHA
# 07eb6673… -> 188992d2… (5-deck election delta, 0 regressions; proof in ADR
# 0003 + 2026-11-11-strategy-abstraction-consolidation/research/impulse-seed-scope.md).
# ENGINE (7a73aba9) + RESOLVE (1e54293c) were UNCHANGED — the 17 hermetic
# fixtures carry none of the touched cheat-into-play commanders.
FULL_CORPUS_SHA = '188992d290aa98f3731b6e2ffb8a43dfe25c5dbcde19fb1c8866625c7708fae6'


def full_corpus_sha() -> str:
    """Compute the canonical SHA-256 over the full 170-deck gauntlet corpus.

    **Requires lake access** (DuckDB + Scryfall parquets + combo tables).

    Canonical serialization (FIXED — do not change without a re-baseline)::

        json.dumps(records, sort_keys=True, separators=(',', ':'), default=list)

    The separators ``(',', ':')`` produce compact JSON with no whitespace,
    making the hash independent of ``json.dumps``'s default pretty-printing.
    ``sort_keys=True`` makes key order deterministic.  ``default=list`` coerces
    sets (from the engine) to sorted lists.

    The 170 records come from ``recalc_corpus()`` (gauntlet-only, no owned
    decks), which itself is deterministic given the same lake + combo data.

    Returns the full 64-char hex digest.
    """
    from pipeline.transforms.strategy.run import recalc_corpus

    records = recalc_corpus()
    blob = json.dumps(records, sort_keys=True, separators=(',', ':'), default=list).encode()
    return hashlib.sha256(blob).hexdigest()


def main() -> None:
    print('=== Engine Parity Oracle ===\n')

    print('--- (a) elect_engine_units output ---')
    engine_baselines = compute_engine_isolation_baselines()
    for slug, sha in sorted(engine_baselines.items()):
        print(f'  {slug}: {sha[:16]}...')
    engine_composite = composite_sha(engine_baselines)
    print(f'\n  COMPOSITE engine_isolation SHA: {engine_composite}')

    print('\n--- (b) resolve() records (fixture corpus) ---')
    resolve_baselines = compute_resolve_baselines()
    for slug, sha in sorted(resolve_baselines.items()):
        print(f'  {slug}: {sha[:16]}...')
    resolve_composite = composite_sha(resolve_baselines)
    print(f'\n  COMPOSITE resolve SHA: {resolve_composite}')

    # Cross-check against committed distill_parity_baseline
    committed_path = FIX / 'distill_parity_baseline.json'
    committed = json.loads(committed_path.read_text())
    mismatches = {s for s in resolve_baselines if resolve_baselines[s] != committed.get(s)}
    if mismatches:
        print(f'\n  WARNING: resolve SHAs diverge from committed baseline for: {sorted(mismatches)}')
    else:
        print('\n  OK: resolve SHAs match committed distill_parity_baseline.json')

    # Write engine isolation baseline
    ENGINE_ISOLATION_BASELINE_FILE.write_text(json.dumps(engine_baselines, indent=2, sort_keys=True) + '\n')
    print(f'\n  Wrote engine isolation baseline -> {ENGINE_ISOLATION_BASELINE_FILE.name}')

    print('\n--- (c) full-corpus canonical SHA (lake-backed) ---')
    try:
        corpus_sha = full_corpus_sha()
        print(f'  Canonical full-corpus SHA: {corpus_sha}')
        if corpus_sha == FULL_CORPUS_SHA:
            print('  OK: matches recorded FULL_CORPUS_SHA')
        else:
            print(f'  DRIFT: recorded {FULL_CORPUS_SHA[:16]}... != computed {corpus_sha[:16]}...')
    except Exception as exc:
        print(f'  SKIPPED (lake not available): {exc}')

    print('\n--- Notes ---')
    print('  The 17 fixture decks are fully hermetic (offline, deterministic).')
    print('  The 170-deck full corpus requires lake access (DuckDB + Scryfall).')
    print('  Use full_corpus_sha() for the canonical reproducible corpus hash.')


if __name__ == '__main__':
    main()
