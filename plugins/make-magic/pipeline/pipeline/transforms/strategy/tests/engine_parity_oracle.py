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
# Re-baselined 2026-11-17 (#74 Tier-A catalog corrections + monarch/extra-turn
# split): sac->sacrifice-outlet (card-enters-graveyard recovers the 1,554-card
# sacrifice-outlet family), clones->clone, food-created -repeatable-treasures
# (3 FALSE treasure-as-food engines dropped: Karlov/Liesa/Judith), + game-end/
# spell-cast/proliferate/monarch catalog corrections. Measured OFF/ON delta:
# 60/157 gauntlet + 5/17 fixture decks moved, 0 engines lost except the 3 intended
# food false-edges, every edge traced to one of the 7 fixes (proof in subagents/
# tier-a-corrections/outbox.md). This time the hermetic fixtures DO carry the
# touched families, so ENGINE + RESOLVE moved too:
#   FULL_CORPUS 188992d290aa98f3… -> 38dd7733a440fb13…
#   ENGINE      7a73aba98b9fb877… -> 1eb27096118d38ad…
#   RESOLVE     1e54293cd148f2fd… -> 00fbb8360a711583…
# Re-baselined 2026-11-17 (#74 decomposition+gaps wave): monarch-maker synth attr
# (produced_by monarch-matters -> monarch-maker; the maker->payoff split lights up
# the monarch node), energy gap (player-counter.produced_by += energy-generator,
# triggers += counter-fuel-energy moved from counter-placed), and the new poisoned
# event (producers = poison givers, triggers = synergy-poison) + its poisoned ->
# game-end WIN rule-edge in combo_win.py. Measured OFF/ON delta: exactly 1/157
# gauntlet deck moved (WakandaForever_2026 gains the monarch maker->payoff engine);
# the energy + poison edges are proven live on synthetic decks (no gauntlet deck
# runs an energy or infect/toxic engine). 0 regressions, every change traced to
# one of the 3 edits (proof in subagents/monarch-energy-poison/outbox.md). The 17
# hermetic fixtures carry NONE of the touched families, so ENGINE + RESOLVE are
# UNCHANGED this wave — only FULL_CORPUS moves:
#   FULL_CORPUS 38dd7733a440fb13… -> 604f6673e7d3d42c…
#   ENGINE      1eb27096118d38ad… (unchanged)
#   RESOLVE     00fbb8360a711583… (unchanged)
# Re-baselined 2026-11-17 (ADR 0024 realization): the `domain` state-event + the
# `color-fixing` producer (union oracle:'mana of any color' OR otag:rainbow-land,
# 709 fixers) + the `five-color-win` consumer (Coalition Victory + Call the Spirit
# Dragons) + folding `free-cast-another` (437) into spell-cast.produced_by.
# life-threshold was DROPPED (the existing `lifegain` event already owns
# altwin::lifegain = Felidar/Angel/Test; a second event would double-count). Event
# count 29 -> 30. Measured OFF (code@736afe0) vs ON: 105/157 gauntlet decks moved,
# 0 label/identity flips, 0 spurious leads, 0 evictions; every changed leaf traces
# to color-fixing (attribute surfacing + ramp-community membership + n_nodes+1) or
# free-cast-another (spell-cast producer + 5 commander-loop closures). `domain` is
# engine-inert on the corpus (no deck runs the 2 wincon cards, and the consumer
# can never reach node-admission support>=3) — exactly as `poisoned` is walk-inert;
# the wiring + rule-edge are proven live (five-color-win/color-fixing membership +
# derived-catalog match). The 17 hermetic fixtures DO carry color-fixing/free-cast,
# so ENGINE (3 decks: urza/grandlarceny/fantastic-four) + RESOLVE (12 decks) moved:
#   FULL_CORPUS 604f6673e7d3d42c… -> 39f1136e9c2289ef…
#   ENGINE      1eb27096118d38ad… -> 84b9617efcf0ec74…
#   RESOLVE     00fbb8360a711583… -> 5a43fc56ce9fd371…
# Proof: subagents/r1-realize-adr0024/outbox.md.
# Re-baselined 2026-11-17 (R1b: decompose the alt-wincon/combo DISTILL catch-all).
# The `alt-wincon/combo` catch-all identity (`otag:wincon OR otag:alternate-win-
# condition`; the 2nd disjunct a DEAD REF) is decomposed into honest archetype names:
# `deckout/thoracle` (synth `deckout-win` = otag:alternate-win-condition AND
# otag:empty-library, 5 cards) + `domain` (the five-color-win payoff now carries an
# archetype edge). Every other wincon RIDES its family (lifegain/poison/spellslinger)
# or distills to None (the standalone-state tail — no `misc-wincon` bucket, which would
# re-bundle unrelated mechanics). The combo-override HEADLINE literal (naming.ALT_WINCON
# _COMBO) is a SEPARATE axis and is untouched (29 combo-override decks, incl. Ghave,
# byte-identical). Measured OFF/ON (157 gauntlet + 17 fixtures): 2 gauntlet decks
# (Azami, Baral) + 2 fixtures (Baral, Peace Offering) moved — exactly the 4 that admit
# `wincon` as a node (>=3 wincon cards); 0 headline flips, 0 archetype_identity
# membership flips, 0 confidence-tier flips, 0 spurious leads. Every changed leaf traces
# to dropping the inert `alt-wincon/combo` strategy from those decks' identity-mass
# accounting (confidence scores nudged UP as the catch-all stops diluting the real
# archetype's share); the new deckout/domain identities are engine-inert (never reach
# node-admission). ENGINE UNCHANGED (elect_engine_units never read the distill archetype);
# RESOLVE + FULL_CORPUS moved (they carry the facts-graph strategies/community archetypes):
#   FULL_CORPUS 39f1136e9c2289ef… -> 62abb4b19c4eddfce…
#   ENGINE      84b9617efcf0ec74… (unchanged)
#   RESOLVE     5a43fc56ce9fd371… -> 50cd387393ba7a7a…
# Proof: subagents/r1b-decompose-altwincon/outbox.md.
FULL_CORPUS_SHA = '62abb4b19c4eddfce01445fbfcf75afb3bc091ba88b3ebe3cb9d54f22341fddc'


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
