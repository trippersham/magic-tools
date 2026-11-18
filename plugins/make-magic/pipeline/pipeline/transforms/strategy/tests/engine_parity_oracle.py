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
# Re-baselined 2026-11-17 (R2 wave 1): life-loss-drain DROPS the `burn` umbrella + dead
# `sac` ref (CR 120.3 — only player damage is life loss). FULL_CORPUS 62abb4b1… -> 9c26ad89…,
# RESOLVE 50cd3873… -> 3fbd927f…, ENGINE unchanged. (Commit e579682; superseded by wave 2.)
# Re-baselined 2026-11-17 (R2 wave 2 — owner-adjudicated resolutions of the two wave-1 STOPs;
# all three Tier-B narrowings now shipped as one re-baseline on top of e579682):
#   * permanent-dies: the raw `removal` producer seed is replaced by the SUBTRACTIVE synth
#     CardAttribute `dies-removal` = `otag:removal AND NOT removal-exile AND NOT removal-bounce
#     AND NOT removal-tuck` (distill_strategies.py, ADR 0009 in-vocabulary — no new atom). This
#     keeps every destroy/burn/sacrifice/sweeper (the 141 sweeper/sunder deaths return) and
#     subtracts only the 1,200 pure zone-changes. 5,551 members (from raw removal 6,751); residual
#     disable-auras ~84 pacifism (1.5% << owner's 10% STOP); known conservative under-coverage ~182
#     MODAL cards (death + an exile/bounce/tuck mode). Holistic `removal` UNTOUCHED elsewhere.
#   * artifact-enters: DROP animate-artifact + artifactify (CR 603.6a — a type-change is not an
#     ETB; 136 cards).
#   * creature-enters-battlefield (coupled fix): seed `repeatable-token-generator` ->
#     `repeatable-creature-tokens` — the umbrella's non-creature-token children (446 cards:
#     artifact/enchantment/land/noncreature token generators, 0 making creature tokens) were false
#     creature-ETB edges; this also clears the validate_seed_map cross-mechanic flag the
#     artifact-enters narrowing surfaced.
# Measured OFF (e579682) / ON: 153/170 gauntlet records moved (dies-removal is a new corpus-wide
# producer attribute, surfacing on 146 decks — like color-fixing in R1), but 0 headline/identity/
# confidence-tier/combo/bracket/win-combo flips, 0 engine-set evictions (every deck keeps its
# engine identity-set), 1 intended loop correction (Sydri commander_loop.closes True->False — her
# artifactify is a type-change, not an ETB). Every changed leaf traces to one of the 3 edits
# (dies-removal attribute surfacing + n_nodes/community/edge/confidence drift, animate-artifact/
# artifactify drop, creature-token producer trim). The 17 hermetic fixtures carry the touched
# families, so all three SHAs moved:
#   FULL_CORPUS 9c26ad8907b63e7d… -> 62d5fa5c87e2c962…
#   ENGINE      84b9617efcf0ec74… -> 9559fa42ea76f661…
#   RESOLVE     3fbd927f92d21dbb… -> b88d8bb5d3a32414…
# Proof: subagents/r2-tier-b-narrowing/outbox.md.
# Re-baselined 2026-11-17 (R3: recursion-from-graveyard restructure — Reading B, audit §3.6 #19).
# The event was role-inverted: produced_by = the graveyard-FILLERS (discard/mill/sac) and triggers =
# the reanimate/recursion family — i.e. it re-modeled "graveyard stocked -> recursion consumes",
# which IS card-enters-graveyard's semantics (same filler producers, same reanimate-family
# consumers), a role-inverted near-duplicate.  Reading B restores the correct orientation:
#   * produced_by -> the `recursion` ROOT (2,370 cards / 100-slug closure, every reanimate-*/
#     recursion-* + regrowth/restock/... ; mechanic-exclusive, precision 1.0, no foreign heavy
#     child, a root with nothing to climb to) — the cards that DO the returning.
#   * triggers -> `leaving-graveyard-matters` (59) + `trigger-from-graveyard` (147) — the payoffs.
#   * the gy-FILLERS (discard/mill/sac) move OUT (they own card-enters-graveyard.produced_by, where
#     the filler->reanimate graveyard engine still lives); the opponent-scoped `discard` otag is
#     dropped (§3.3) and the dead `sac` ref goes with it.
# Measured OFF (31f5c7c) / ON (157 gauntlet): 85/157 records moved, but 0 headline/pre_combo_name/
# archetype_identity/ranked_identity/confidence/bracket/combo/win-combo/tribe flips and 0 DETERMINISM
# flips.  No reanimator deck loses its engine: recursion-from-graveyard stops electing as a redundant
# graveyard engine on 23 decks (it was a role-inverted dup of card-enters-graveyard they already had,
# or it RELOCATES — 8 decks, incl. the graveyard/reanimator Marchesa-Dealer + Dihada, gain
# card-enters-graveyard as rfg drops) and now elects ONLY where a genuine returns->payoff pair exists
# (braids, xu-ifit: producers=recursion/reanimate, consumers=leaving-graveyard-matters).  8
# commander_loop.closes True->False (Karador/Nethroi/Sheoldred/Mimeoplasm/Sharuum/Hashaton/Kess/
# Feather) = the correct removal of the SPURIOUS self-loop the role-inversion created (OFF, `recursion`
# was a PRODUCER and the reanimate family CONSUMERS of the SAME event, so any commander carrying a
# recursion attr trivially closed a loop); none change determinism (all keep mass-elected engines).
# The 17 hermetic fixtures carry the recursion/reanimate families, so all three SHAs moved:
#   FULL_CORPUS 62d5fa5c87e2c962… -> cb1a103be7614116…
#   ENGINE      9559fa42ea76f661… -> 4549062831f5bfc9…
#   RESOLVE     b88d8bb5d3a32414… -> f209237b352ebf62…
# Proof: subagents/r3-recursion-restructure/outbox.md.
# Re-baselined 2026-11-17 (R5a: latent coverage-bug otag refs corrected + distill dead-ref gate
# activated). Three DISTILL-identity corrections, all inside the closed query vocabulary (ADR 0009):
#   * counters: the dead `otag:proliferate` (an EVENT name, 0 cards) -> the live proliferate family
#     `otag:repeatable-proliferate OR otag:pseudo-proliferate OR otag:synergy-proliferate` (3
#     attribute names None -> +1/+1 counters).
#   * voltron/equipment: DROP the dead `otag:auras` (0 cards; `synergy-aura` already in the query
#     covers aura-voltron) — member-neutral.
#   * deathtouch: the dead `otag:deathtouch-combat` (an EVENT name, 0 cards) -> `otag:gains-deathtouch
#     OR otag:gives-deathtouch` (the deathtouch-GRANTING / Shelob family, 314 cards; the deathtouch-
#     combat EVENT already carried these as producers — only the distill identity under-covered).
# These edit the NAMING layer only — NOT events/EventEffect edges. PROVEN: ENGINE composite +
# every per-deck `facts['engines']` SHA (all 170 gauntlet + 17 fixtures) are BYTE-IDENTICAL; the
# Shelob fixture (owned__spiders-play-with-their-food) `engines`+`edges` SHAs are identical OFF/ON,
# only its archetype label gains deathtouch (headline/bracket/confidence unchanged). Measured OFF
# (86a7087) / ON: 7/170 gauntlet records moved (3 gain the deathtouch identity: kathril/nethroi/
# judith; 4 counters decks gain `repeatable-proliferate` in contributing_attrs with member_cards +
# weight UNCHANGED — pure provenance) + 2/17 fixtures (Shelob + fantastic-four). 0 hard flips
# (pre_combo/combo/confidence/bracket/abstain), 1 legit top-3 reshuffle (kathril's #3 deathtouch
# over +1/+1 counters), 0 spurious: nethroi's 1-card deathtouch is gated out by the substantiveness
# bar (ADR 0004: >=3, or >=2 with commander). ENGINE UNCHANGED; RESOLVE + FULL_CORPUS moved:
#   FULL_CORPUS cb1a103be7614116… -> 38aad6df13fc352d…
#   ENGINE      4549062831f5bfc9… (unchanged)
#   RESOLVE     f209237b352ebf62… -> ed36de37ee5436bb…
# Proof: subagents/r5a-latent-bug-hunt/outbox.md.
# Re-baselined 2026-11-17 (R5a consistency wave — owner-approved under-coverage adds on top of the
# R5a dead-ref re-baseline). Two DISTILL-identity additions, both live otags inside the closed
# vocabulary (ADR 0009):
#   * counters += `otag:gives-pp-counters` (1,772 cards) — resolves the granted-keyword asymmetry
#     (counters excluded it while mm-counters/deathtouch include their gives-*); a card that GRANTS
#     +1/+1 counters is a counters-archetype card.
#   * deathtouch += `otag:gives-deathtouch-noncreature` (2 cards) — the trivial completion of the
#     deathtouch-granting coverage.
# NAMING-layer only. PROVEN engine-untouched: ENGINE composite + every per-deck `facts['engines']`
# SHA (170 gauntlet + 17 fixtures) byte-identical; the 6 moved fixtures (abzanarmor/sokka/Shelob/
# fantastic-four/world-reclaimer/peace-offering) have identical engines+edges SHAs and unchanged
# headline/bracket/confidence. Measured OFF (0ec5759) / ON: 2 attribute names move None -> their
# identity; 48 gauntlet records + 6 fixtures change ONLY the `+1/+1 counters` (47) / `deathtouch`
# (1) `contributing_attrs` provenance — member_cards + weighted_quantity + eligibility IDENTICAL in
# every case (0 CORE mismatches), **0 label changes, 0 headline flips, 0 spurious joins** (every
# gives-* card was already a member via another attr; the >=3 substantiveness bar is never crossed
# anew). ENGINE UNCHANGED; RESOLVE + FULL_CORPUS moved:
#   FULL_CORPUS 38aad6df13fc352d… -> 855edd458402f0f6…
#   ENGINE      4549062831f5bfc9… (unchanged)
#   RESOLVE     ed36de37ee5436bb… -> be2981c37ff3c3a0…
# Proof: subagents/r5a-latent-bug-hunt/outbox.md.
# Re-baselined 2026-11-18 (ADR 0025: collapse deathtouch-combat per the grant-vs-use rule).
# deathtouch is a static combat-damage modifier (like double-strike/trample/evasion), not its
# own CR event: the deathtouch-combat pseudo-event is REMOVED and its grants (deathtouch-counter/
# gains-deathtouch/gives-deathtouch) become PRODUCERS of combat-damage-dealt (#9) AND permanent-dies
# (#4 — deathtouch damage is lethal, so it kills what it fights). synergy-deathtouch / old-*-deathtouch
# payoffs stay in the `deathtouch` distill identity (naming layer, untouched). Event count 30 -> 29.
# EVENT_EDGE_CLASSES unchanged (deathtouch-combat was never a key). Measured OFF (a696bd0) / ON
# (157 gauntlet + 17 fixtures): exactly 2/170 gauntlet records moved (Kathril, Nethroi) + 1/17
# fixtures (Shelob); 0 pre_combo_name / bracket / confidence / n_nodes flips, 0 spurious new engines,
# ranked_identity top-3 byte-identical on every moved deck. The deathtouch archetype identity is
# byte-identical on Kathril (deathtouch #3, share 0.275) and Shelob. The only engine-unit removed is
# Kathril's standalone `deathtouch-combat` pseudo-engine (mass 38; a grant->synergy keyword loop) —
# the intended collapse target; Shelob's permanent-dies engine STRENGTHENS (mass 68->74, gains
# gives-deathtouch as a producer) and Nethroi's likewise (49->54). The 17 hermetic fixtures carry the
# deathtouch-granting family (Shelob), so ENGINE + RESOLVE moved alongside FULL_CORPUS:
#   FULL_CORPUS 855edd458402f0f6… -> ab1a6074a6cff086…
#   ENGINE      4549062831f5bfc9… -> f4b98a79a70bdf02…
#   RESOLVE     be2981c37ff3c3a0… -> 7b1245a16310da63…
# Proof: subagents/deathtouch-collapse/outbox.md.
# Re-baselined 2026-11-18 (ADR 0025 resource-split doctrine — Batch A: two event splits).
# A1 spell-cast split (29->31): spell-cast is now the CAST-ONLY node (producers impulse-cast/
# free-cast-another); copies LEFT it to the two new events spell-copied (copy-* producers) and
# spell-on-stack (the cast-or-copy union, magecraft home). A2 player-counter split (31->33): the
# generic player-counter node is REPLACED by energy (energy-generator + proliferate -> counter-
# fuel-energy), rad (rad-counter synth + proliferate -> self-mill), and experience (NEW synth
# `experience-counter` oracle:'experience counter', 19 members 0 FP). Net event count 29 -> 33
# (6 new: spell-cast[redef]/spell-copied/spell-on-stack, energy/rad/experience; 2 removed: old
# spell-cast, player-counter). combo_win: EVENT_EDGE_CLASSES['spell-cast'] keeps cast_drain+altwin::
# spell-cast; spell-on-stack gets cast_drain (magecraft drains on cast-or-copy); player-counter's
# altwin::player-counter (Azor's filibuster) is now UNMAPPED (energy/experience no longer spuriously
# reach it); fid routes: 7 magecraft/copy fids -> spell-on-stack, energy fids(317/2018) -> energy,
# experience fids(1179/2646) -> experience. Measured OFF (34b2a08) / ON (157 gauntlet + 17 fixtures):
# 0 pre_combo_name / bracket / confidence / archetype_identity / ranked_identity / combo_identity flips;
# 1 benign n_nodes +1 (simic__ezuri, now carries the experience-counter attr); 10/157 gauntlet engine
# relocations, ALL spell-cast<->spell-copied/spell-on-stack: genuine cast decks (impulse-cast/free-cast
# producers) KEEP spell-cast; pure-copy decks correctly drop the mis-attributed copy->storm/prowess
# engine; copy+magecraft decks (veyran/kalamax/JeskaiStriker/shiko) relocate to spell-on-stack
# (copy->magecraft). 0 player-counter engines existed in the gauntlet (A2 engine impact nil there).
# Exactly 1/17 fixtures moved (owned__the-fantastic-four, a spellslinger that KEEPS spell-cast via
# free-cast-another with copies relocated; pre_combo_name=spellslinger / bracket 3 / MED preserved):
#   FULL_CORPUS ab1a6074a6cff086… -> 03a5880c44d4cdd3…
#   ENGINE      f4b98a79a70bdf02… -> 7a83891a6c191f2b…
#   RESOLVE     7b1245a16310da63… -> a63f268b0884a474…
# Proof: subagents/batch-a-splits/outbox.md.
FULL_CORPUS_SHA = '03a5880c44d4cdd32c4811625c0d0bbbe0efc77902218b60da091a9318d672d1'


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
