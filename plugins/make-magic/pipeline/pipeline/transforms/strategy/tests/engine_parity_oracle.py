"""Engine-isolation byte-parity oracle for the strategy engine.

Records a SHA over (a) the serialized ``_isolate_engines`` output and (b) the
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
from pipeline.transforms.strategy.facts import compute_facts

FIX = Path(__file__).resolve().parent / 'fixtures'
DECKS = FIX / 'decks'
ENGINE_ISOLATION_BASELINE_FILE = FIX / 'engine_isolation_baseline.json'


def _sha256(obj: object) -> str:
    """Stable SHA-256 of a JSON-serializable object."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=list).encode()
    ).hexdigest()


def _fixture_slugs() -> list[str]:
    """All fixture deck slugs, sorted (the hermetic corpus)."""
    return sorted(p.stem for p in DECKS.glob('*.json'))


def _load_combos() -> list[dict]:
    return [dict(c) for c in json.loads((FIX / 'combos.json').read_text())]


def compute_engine_isolation_baselines() -> dict[str, str]:
    """Per-deck SHA of ``facts['engines']['engine_isolation']`` (the ``_isolate_engines`` output).

    This is the (a) oracle: a SHA over the held engine-isolation layer's serialized
    output for each hermetic fixture deck.
    """
    combos = _load_combos()
    baselines: dict[str, str] = {}
    for slug in _fixture_slugs():
        deck = json.loads((DECKS / f'{slug}.json').read_text())
        ctx = compute_facts(deck, list(combos))
        engine_iso = ctx.facts['engines']['engine_isolation']
        baselines[slug] = _sha256(engine_iso)
    return baselines


def compute_resolve_baselines() -> dict[str, str]:
    """Per-deck SHA of the full ``resolve()`` record.

    This is the (b) oracle: matches the existing ``vocab_parity_baseline.json``
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
    return hashlib.sha256(
        json.dumps(per_deck, sort_keys=True).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Full-corpus canonical SHA (lake-backed, reproducible)
# ---------------------------------------------------------------------------

# The canonical full-corpus SHA, computed by ``full_corpus_sha()`` below.
# Supersedes the ad-hoc ``88de96a7`` value recorded in ADR 0003 (which was
# computed by an unrecoverable method and is NOT reproducible).
FULL_CORPUS_SHA = 'de5313af330f802f9381e3d587cb54d3a87a637dcbb368db28f47091f2aa468f'


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

    print('--- (a) _isolate_engines output ---')
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

    # Cross-check against committed vocab_parity_baseline
    committed_path = FIX / 'vocab_parity_baseline.json'
    committed = json.loads(committed_path.read_text())
    mismatches = {s for s in resolve_baselines if resolve_baselines[s] != committed.get(s)}
    if mismatches:
        print(f'\n  WARNING: resolve SHAs diverge from committed baseline for: {sorted(mismatches)}')
    else:
        print('\n  OK: resolve SHAs match committed vocab_parity_baseline.json')

    # Write engine isolation baseline
    ENGINE_ISOLATION_BASELINE_FILE.write_text(
        json.dumps(engine_baselines, indent=2, sort_keys=True) + '\n')
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
