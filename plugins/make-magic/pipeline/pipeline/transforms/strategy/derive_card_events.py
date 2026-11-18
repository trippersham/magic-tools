"""Seed-based event adjacency derivation (Phase 6.1 + 6.2 + 6.1b).

Implements the **Maximal mechanic-exclusive seed** rule as production code
(6.1), the **derived ``produced_by``/``triggers``** as a shadow view behind a
flag (6.2), and the **mechanic-exclusivity validator** that enforces the rule's
stop-condition so a cross-mechanic umbrella (``ramp``, ``triggered-ability``,
``thingfall``) can never enter the derived adjacency (6.1b).

The rule
--------
Given an event side (produce/trigger), each SEED is the **highest ancestor**
in the otag taxonomy whose descendant-closure stays *mechanic-exclusive* — stop
one level below any ancestor that admits a different mechanic.  The 28-event
seed map below was derived by the build-design measurement pass
(``subagents/adjacency-build-design/research/adjacency-build-design.md``) and is
reused here as DATA: each side's ``seeds`` are the owner-approved mechanic-
exclusive **seed concepts** (``mana-producer`` for "makes mana", ``land-ramp``
for "puts a land onto the battlefield", …), sitting at the mechanic frontier.

The exclusivity check (the enforcement — task 6.1b)
---------------------------------------------------
:func:`is_mechanic_exclusive` decides whether a candidate taxonomy node may be
a seed for a side, measured against the **mechanic population** ``M`` that
the side's own seeds establish (the union of their descendant-closure card
sets).  A node is mechanic-exclusive iff it passes BOTH, generically:

* **(b) empirical precision** — ``|cards(node) ∩ M| / |cards(node)| >=``
  :data:`EXCLUSIVITY_PRECISION_MIN`.  Climbing to a node that admits a foreign
  mechanic drops precision (``ramp`` for mana-produced = 0.56; for land-enters
  = 0.28 — both below the floor).
* **(a) structural span** — the node has no *foreign heavy child*: a child
  whose closure is large (``>=`` :data:`EXCLUSIVITY_BRANCH_FLOOR` cards) yet
  mostly outside ``M`` (precision ``<`` :data:`EXCLUSIVITY_BRANCH_PRECISION_MIN`),
  i.e. a sibling branch belonging to a different mechanic.

The rule is GENERIC: no per-event branch, no hand-coded umbrella blocklist.
``ramp`` is rejected from mana-produced AND land-enters by the same predicate
and the same thresholds — see :func:`validate_seed_map` and the
``TestExclusivityValidator`` gate tests.  The derivation never admits ``ramp``
into the derived output (it is not a listed seed, and
:func:`refused_climb_targets` refuses all ancestor umbrellas), so ``ramp``
cannot appear in any derived list — the hand-removal it used to require is now
the rule's own consequence.

Enforcement boundary (honest)
-----------------------------
The rule has **two facets** under the one-named-rule doctrine (ADR 0001):

1. **Seed selection** — the seeds themselves are owner-approved DATA.  A
   cross-mechanic umbrella hand-listed as a seed IS caught by the (fixed)
   :func:`validate_seed_map` cross-mechanic umbrella check (CI-enforced; see
   ``MAKE_MAGIC_VALIDATE_ANCHORS``): a seed whose descendant-closure covers
   >= 50% of a *disjoint* foreign event-side's seed set is flagged.  ``ramp``
   added as a seed is caught because its closure covers 50% of
   ``mana-produced.produced_by`` and 50% of ``land-enters.produced_by`` —
   it IS both mechanics.  Related event-sides (which share seeds) are not
   compared, so legitimate cross-event seeds like ``sac`` are not flagged.
2. **Mechanic-exclusivity** — the ``is_mechanic_exclusive`` predicate refuses
   any candidate ancestor that dilutes the mechanic (the climb guard), and
   ``refused_climb_targets`` proves no refused umbrella leaks into the output.

Together: **seeds are reviewable data now CAUGHT by the CI validator** (a
``ramp`` hand-added as a seed is a data error detected at validation time, not
silently derived).  The output never contains a refused umbrella.

Derived view — authoritative (task 6.5 flip, ADR 0022)
------------------------------------------------------
``derive_card_events(events)`` returns a new events dict where each event's
``produced_by``/``triggers`` lists are the sorted descendant-closure of that
side's seeds.  The committed enumerated lists are a *subset* (no loss).  The
exclusivity layer does NOT change this output (the curated seeds are already the
mechanic frontier).  As of task 6.5 this derivation is applied **at catalog
load** (``assets.load_assets`` and ``engine_hydration._load_events``) and is the
single authoritative source for every downstream reader — the former
``SHADOW_DERIVED_ADJACENCY`` flag is retired.

One named rule (ADR 0001)
-------------------------
The seed map is the SINGLE authoritative source for which otag concepts
participate on each event side.  The rule is ONE implementation — no per-event
branches, no per-deck hardcoding.  New events get a seed-map row; the rule
stays the same.
"""

from __future__ import annotations

import os as _os
from collections import deque
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# 28-event seed map — owner-approved DATA from the validated build-design.
#
# For each event, each side (``triggers`` / ``produced_by``) carries:
#   - ``seeds``: list[str] — otag slugs whose descendant-closure defines
#     the derived list for this side.  Includes BOTH the structural build-
#     design seeds (higher concepts) AND the existing enumerated slugs
#     (for guaranteed slug-level superset).
#   - ``grade``: "S" (clean structural, one exclusive taxonomy seed),
#     "M" (semantic, union of flat concepts), or "empty" (no otag seed —
#     text-synth / terminal / inert).
#
# The union of build-design seeds + enumerated slugs ensures
# derived ⊇ enumerated while the higher seeds unlock descendant coverage
# (the ~63% edge unlock vs 28% enumerated).
# ---------------------------------------------------------------------------

SEED_MAP: dict[str, dict[str, Any]] = {
    # ── 1. land-enters-battlefield ──────────────────────────────────────
    'land-enters-battlefield': {
        'triggers': {
            'seeds': [
                'islandfall',
                'land-count-matters',
                'landfall',
                'landfall-other',
                'lands-in-graveyard-matter',
                'lands-matter',
                'opponent-land-count-matters',
            ],
            'grade': 'S',
        },
        'produced_by': {
            # `ramp` is NOT a seed here — `land-ramp` is the mechanic-exclusive
            # concept for "puts a land onto the battlefield".  The exclusivity
            # rule ENFORCES this: climbing `land-ramp` -> `ramp` is refused
            # because ramp's closure admits mana-producer/ritual (a different
            # mechanic) and drops precision to 0.28 (< EXCLUSIVITY_PRECISION_MIN).
            # Same predicate, same thresholds as mana-produced.produced_by —
            # no per-event special-casing.  See is_mechanic_exclusive /
            # validate_seed_map (task 6.1b).
            'seeds': [
                'extra-land',
                'land-ramp',
                'reanimate-land',
                'recursion-land',
                'sneak-land',
                'tutor-land-to-battlefield',
            ],
            'grade': 'M',
        },
    },
    # ── 2. creature-enters-battlefield ──────────────────────────────────
    'creature-enters-battlefield': {
        'triggers': {
            'seeds': [
                'creature-count-matters',
                'creaturefall',
                'enters-in-company',
                'flicker',
                'leaves-body-behind',
                'multiple-bodies',
                'synergy-token-creature',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'seeds': [
                'flicker',
                'flicker-creature',
                'multiple-bodies',
                'reanimate-artifact-creature',
                'reanimate-creature',
                'reanimate-nonland',
                'reanimate-permanent',
                # `repeatable-token-generator` umbrella REMOVED (#74 R2 §3.4 coupled
                # fix): it is the taxonomy parent of repeatable-{creature,artifact,
                # enchantment,land,noncreature}-tokens — but only CREATURE tokens
                # produce a creature ETB.  The non-creature children were false
                # creature-enters producers.  `repeatable-creature-tokens` (already a
                # seed, just above) is the correct mechanic-exclusive child.  This also
                # resolves the validate_seed_map cross-mechanic flag the artifact-enters
                # narrowing surfaced (the umbrella covered 50% of the 4-seed
                # artifact-enters.produced_by set).
                'repeatable-creature-tokens',
                'sneak-creature',
                # impulse / cheat-into-play (#74 decision 5): maximal onto-bf node
                # on the from-library branch; closure adds sneak-from-library,
                # consult-onto-battlefield, impulse-onto-battlefield (precision
                # 1.000 vs the onto-bf reference; sneak-creature already covers
                # the from-HAND creature slice). Proof: ADR 0003 re-baseline +
                # 2026-11-11-strategy-abstraction-consolidation/research/impulse-seed-scope.md.
                'sneak-from-library',
                'tokens',
            ],
            'grade': 'M',
        },
    },
    # ── 3. permanent-leaves-battlefield ─────────────────────────────────
    'permanent-leaves-battlefield': {
        'triggers': {
            'seeds': [
                'enters-and-leaves-trigger-self',
                'leaves-battlefield-trigger',
                'leaves-body-behind',
                'leaves-trigger-self',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'seeds': [
                'flicker',
                'flicker-artifact',
                'flicker-creature',
                'flicker-enchantment',
                'flicker-land',
                'flicker-nonenchantment',
                'flicker-nonland',
                'flicker-permanent',
                'flicker-planeswalker',
                'flicker-self',
                'flicker-slow',
                'flicker-vehicle',
                'removal',
                'sac',
            ],
            'grade': 'M',
        },
    },
    # ── 4. permanent-dies ───────────────────────────────────────────────
    'permanent-dies': {
        'triggers': {
            'seeds': [
                'cheat-death',
                'cheat-death-self',
                'death-trigger',
                'death-trigger-opponent',
                'death-trigger-self',
                'life-and-death-trigger-self',
                'splits-on-death',
            ],
            'grade': 'S',
        },
        'produced_by': {
            # `removal` → `dies-removal` (#74 R2 §3.1): CR 700.4, "dies" = put into a
            # graveyard FROM the battlefield.  The holistic `removal` closure dragged in
            # removal-exile (617), removal-bounce (448), removal-tuck (151) — zone
            # changes that are NOT deaths and actively dodge death triggers.  Narrowing
            # to method-children (removal-destroy/-burn/-sacrifice) UNDER-covered — it
            # dropped 141 sweeper/sunder board-wipe deaths tagged only removal-toughness
            # (R2 STOP).  The carve is the SUBTRACTIVE synth attr `dies-removal`
            # (distill_strategies.py): `otag:removal AND NOT removal-exile/-bounce/-tuck`
            # — keeps every destroy/burn/sacrifice/sweeper, subtracts only the pure
            # zone-changes.  5,551 members (from 6,751); residual disable-auras ~1.5%.
            # The holistic `removal` otag stays untouched in permanent-leaves-battlefield.
            # deathtouch grants also produce permanent-dies: deathtouch damage is lethal,
            # so a deathtouch creature KILLS what it fights (the Shelob death->food signal).
            # This is the second home of the collapsed deathtouch-combat event (ADR 0025).
            'seeds': [
                'deathtouch-counter',
                'dies-removal',
                'free-sacrifice-outlet',
                'gains-deathtouch',
                'gives-deathtouch',
                'removal-sacrifice',
                'sac',
                'sacrifice-outlet',
                'sacrifice-outlet-artifact',
                'sacrifice-outlet-creature',
                'sacrifice-outlet-enchantment',
                'sacrifice-outlet-land',
                'sacrifice-outlet-nonland',
                'sacrifice-outlet-planeswalker',
                'sacrifice-outlet-token',
                'sacrifice-outlet-universal',
                'sacrifice-self',
            ],
            'grade': 'M',
        },
    },
    # ── 5. permanent-sacrificed ─────────────────────────────────────────
    'permanent-sacrificed': {
        'triggers': {
            'seeds': [
                'death-trigger',
                'death-trigger-opponent',
                'death-trigger-self',
                'opponent-sacrifice-matters',
                'sacrifice-matters',
                'your-sacrifice-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'alternate-cost-sacrifice',
                'free-sacrifice-outlet',
                'removal-sacrifice',
                'repeatable-sacrifice-outlet',
                'sac',
                'sacrifice-outlet',
                'sacrifice-outlet-artifact',
                'sacrifice-outlet-creature',
                'sacrifice-outlet-enchantment',
                'sacrifice-outlet-land',
                'sacrifice-outlet-nonland',
                'sacrifice-outlet-planeswalker',
                'sacrifice-outlet-token',
                'sacrifice-outlet-universal',
                'sacrifice-self',
            ],
            'grade': 'S',
        },
    },
    # ── 6. card-enters-graveyard ────────────────────────────────────────
    'card-enters-graveyard': {
        'triggers': {
            'seeds': [
                'activate-from-graveyard',
                'affinity-for-graveyard',
                'card-types-in-graveyard-matter',
                'cards-in-graveyard-matter',
                'castable-from-graveyard',
                'graveyard-fuel',
                'graveyard-fuel-artifact',
                'graveyard-fuel-creature',
                'graveyard-fuel-enchantment',
                'graveyard-fuel-historic',
                'graveyard-fuel-instant',
                'graveyard-fuel-land',
                'graveyard-fuel-legendary',
                'graveyard-fuel-noncreature',
                'graveyard-fuel-nonland',
                'graveyard-fuel-permanent',
                'graveyard-fuel-saga',
                'graveyard-fuel-self',
                'graveyard-fuel-sorcery',
                'lands-in-graveyard-matter',
                'leaving-graveyard-matters',
                'reanimate',
                'reanimate-artifact',
                'reanimate-artifact-creature',
                'reanimate-aura',
                'reanimate-battle',
                'reanimate-cast',
                'reanimate-creature',
                'reanimate-enchantment',
                'reanimate-equipment',
                'reanimate-face-down',
                'reanimate-from-any',
                'reanimate-from-opponent',
                'reanimate-historic',
                'reanimate-instant-sorcery',
                'reanimate-land',
                'reanimate-legendary',
                'reanimate-matters',
                'reanimate-noncreature',
                'reanimate-nonland',
                'reanimate-permanent',
                'reanimate-planeswalker',
                'reanimate-saga',
                'reanimate-self',
                'reanimate-spacecraft',
                'reanimate-vehicle',
                'recursion',
                'recursion-any',
                'recursion-artifact',
                'recursion-battle',
                'recursion-creature',
                'recursion-enchantment',
                'recursion-from-exile',
                'recursion-instant',
                'recursion-land',
                'recursion-permanent',
                'recursion-planeswalker',
                'recursion-self',
                'recursion-sorcery',
                'synergy-graveyard-cast',
                'trigger-from-graveyard',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'discard',
                'discard-outlet',
                'discard-outlet-artifact',
                'discard-outlet-creature',
                'discard-outlet-land',
                'discard-outlet-last-drawn',
                'discard-outlet-legendary',
                'discard-outlet-nonland',
                'discard-outlet-random',
                'loot',
                'mill',
                'mill-self',
                'rummage',
                'sacrifice-outlet',
                'sacrifice-self',
                'wheel',
            ],
            'grade': 'M',
        },
    },
    # ── 7. spell-cast ───────────────────────────────────────────────────
    'spell-cast': {
        'triggers': {
            'seeds': [
                'cast-trigger',
                'cast-trigger-other',
                'cast-trigger-self',
                'cast-trigger-you',
                'prowess-like',
                'second-spell-matters',
                'storm-count-matters',
                'synergy-instant',
                'synergy-noncreature',
                'synergy-sorcery',
                'times-resolved-matters',
            ],
            'grade': 'S',
        },
        'produced_by': {
            # impulse-cast (#74 §4.1): exile-and-you-may-CAST producers (Sunbird's
            # Invocation, Aetherworks Marvel) make real CR 601.1b casts. copy-*
            # producers are a documented over-promise: copies are NOT cast
            # (CR 707.12), so storm/prowess/cast-only payoffs don't see them —
            # but magecraft (CR 702.157a) counts "casts or copies", and magecraft
            # IS in the trigger closure, so the copy->cast-trigger edge is precise
            # for magecraft consumers. free-cast-another (#74; ADR 0024 wave) is now
            # folded IN by owner call: a card that casts ANOTHER spell for free makes a
            # real CR 601 cast (437 cards, measured in extra-cast-breadth.md), so it is
            # a genuine spell-cast producer. Diagnostic-only this wave (no gauntlet
            # label flips). The remaining cast families (cast-on-resolution/theft-cast)
            # stay OUT (owner call).
            'seeds': [
                'copy-instant',
                'copy-sorcery',
                'copy-spell',
                'free-cast-another',
                'impulse-cast',
            ],
            'grade': 'M',
        },
    },
    # ── 8. creature-attacks ─────────────────────────────────────────────
    'creature-attacks': {
        'triggers': {
            'seeds': [
                'attack-trigger',
                'attacking-matters',
                'attacking-matters-any',
                'attacking-matters-self',
                'attacking-opponents-matters',
                'synergy-solo-attack',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'seeds': [
                'combat',
                'extra-combat-phase',
                'force-attacker',
                'forced-attacker',
                'gives-haste',
                'tokens',
            ],
            'grade': 'M',
        },
    },
    # ── 9. combat-damage-dealt ──────────────────────────────────────────
    'combat-damage-dealt': {
        'triggers': {
            'seeds': [
                'combat-neutral-damage-trigger',
                'retaliate-to-damage',
                'saboteur',
                'scales-with-damage-dealt',
            ],
            'grade': 'S',
        },
        'produced_by': {
            # deathtouch is a static combat-damage modifier ("any damage is lethal"),
            # the same shape as double-strike/trample/evasion — its grant is a PRODUCER
            # of combat-damage-dealt, not its own event (ADR 0025, grant-vs-use rule).
            # The deathtouch-combat pseudo-event was collapsed here (+ permanent-dies);
            # the synergy-deathtouch payoff lives in the `deathtouch` distill identity.
            'seeds': [
                'combat',
                'damage-multiplier',
                'deathtouch-counter',
                'gains-deathtouch',
                'gains-double-strike',
                'gives-deathtouch',
                'gives-double-strike',
                'gives-evasion',
                'gives-trample',
            ],
            'grade': 'M',
        },
    },
    # ── 10. lifegain ────────────────────────────────────────────────────
    'lifegain': {
        'triggers': {
            'seeds': [
                'life-total-matters-self',
                'lifegain-increaser',
                'lifegain-matters',
                'lifegain-to-damage',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'drain-life',
                'gains-lifelink',
                'gives-lifelink',
                'lifegain',
                'repeatable-lifegain',
            ],
            'grade': 'S',
        },
    },
    # ── 11. life-loss-drain ─────────────────────────────────────────────
    'life-loss-drain': {
        'triggers': {
            'seeds': [
                'life-loss-matters',
                'self-life-loss-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # burn DROPPED (#74 §3.2): CR 120.3 — only damage dealt to a PLAYER is
            # life loss.  The `burn` parent closure dragged in removal-burn (2,536),
            # burn-battle, bombard/bombard-self, burn-creature/-planeswalker,
            # removal-fight — damage to creatures/battles/planeswalkers, which causes
            # NO player life loss.  Keeping burn-player (+ burn-player-each) is the
            # exact player-damage slice.  sac was a DEAD ref (0 cards, §0.1) and is
            # conceptually wrong anyway (sacrifice alone ≠ life loss; the aristocrats
            # chain runs sac → dies → drain-life/opponent-loses-life, already present).
            # Measured OFF/ON: 1,253 cards dropped, 0 carried any player-damage tag
            # (100% creature/planeswalker/battle/fight damage) → no player-drain
            # producer lost (proof: subagents/r2-tier-b-narrowing/outbox.md).
            'seeds': [
                'burn-player',
                'burn-player-each',
                'drain-life',
                'omnivore-ability',
                'opponent-loses-life',
            ],
            'grade': 'M',
        },
    },
    # ── 12. counter-placed ──────────────────────────────────────────────
    'counter-placed': {
        'triggers': {
            # counter-fuel-energy MOVED to player-counter (#74 §4.4): energy is a
            # PLAYER counter (CR 122.1a), not a counter placed on a permanent, and
            # no energy producer sat in this event's produced_by — the edges were
            # dead here. It now lives in player-counter.triggers (and stays in
            # proliferate.triggers).
            'seeds': [
                'counter-fuel',
                'counter-fuel-aesthetic',
                'counter-fuel-any',
                'counter-fuel-charge',
                'counter-fuel-fade',
                'counter-fuel-keyword',
                'counter-fuel-lore',
                'counter-fuel-loyalty',
                'counter-fuel-mm',
                'counter-fuel-oil',
                'counter-fuel-other',
                'counter-fuel-pp',
                'counter-fuel-pt',
                'counter-fuel-shield',
                'counter-fuel-stun',
                'counter-fuel-time',
                'counters-matter',
                'gains-mm-counters',
                'gains-pp-counters',
                'mm-counters-matter',
                'pp-counters-matter',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'seeds': [
                'counter-increaser',
                'counters',
                'gives-mm-counters',
                'gives-pp-counters',
                'gives-pp-counters-to-all',
                'repeatable-pp-counters',
                'repeatable-proliferate',
            ],
            'grade': 'M',
        },
    },
    # ── 13. proliferate ─────────────────────────────────────────────────
    'proliferate': {
        'triggers': {
            'seeds': [
                'counter-fuel',
                'counter-fuel-aesthetic',
                'counter-fuel-any',
                'counter-fuel-charge',
                'counter-fuel-energy',
                'counter-fuel-fade',
                'counter-fuel-keyword',
                'counter-fuel-lore',
                'counter-fuel-loyalty',
                'counter-fuel-mm',
                'counter-fuel-oil',
                'counter-fuel-other',
                'counter-fuel-pp',
                'counter-fuel-pt',
                'counter-fuel-shield',
                'counter-fuel-stun',
                'counter-fuel-time',
                'counters-matter',
                'pp-counters-matter',
                'synergy-proliferate',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # synergy-proliferate (#74 §MISCAT) moved to triggers: those 7 cards
            # (Tekuthal, Ezuri Stalker, Venser Corpse Puppet) are proliferate-MATTERS
            # payoffs/amplifiers — they consume/amplify proliferate, they don't perform it.
            'seeds': [
                'pseudo-proliferate',
                'repeatable-proliferate',
            ],
            'grade': 'M',
        },
    },
    # ── 14. token-created ───────────────────────────────────────────────
    'token-created': {
        'triggers': {
            'seeds': [
                'artifact-count-matters',
                'creature-count-matters',
                'sac',
                'synergy-token',
                'synergy-token-creature',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # Build-design P seeds: repeatable-token-generator + multiple-
            # bodies, PLUS seed-discovery candidates promoted by the build-design:
            # leaves-body-behind (death→token), out-of-color-token (off-color
            # token producers), creates-bigger-body (Reef Worm chain).
            'seeds': [
                'artifacts',
                'creates-bigger-body',
                'leaves-body-behind',
                'multiple-bodies',
                'out-of-color-token',
                'repeatable-artifact-tokens',
                'repeatable-clues',
                'repeatable-creature-tokens',
                'repeatable-food',
                'repeatable-noncreature-tokens',
                'repeatable-token-generator',
                'repeatable-treasures',
                'tokens',
            ],
            'grade': 'M',
        },
    },
    # ── 15. artifact-enters ─────────────────────────────────────────────
    'artifact-enters': {
        'triggers': {
            'seeds': [
                'affinity-for-artifacts',
                'artifact-count-matters',
                'artifactfall',
                'synergy-artifact',
                'synergy-artifact-creature',
            ],
            'grade': 'S',
        },
        'produced_by': {
            # animate-artifact + artifactify DROPPED (#74 R2 §3.4): CR 603.6a — an ETB
            # ability triggers on a zone change (a permanent ENTERING).  A permanent
            # that merely BECOMES an artifact (artifactify: Sydri, Encroaching
            # Mycosynth) or is animated into a creature (animate-artifact: Ensoul
            # Artifact, Tezzeret's Touch) does NOT re-enter — no ETB event fires.  The
            # consumers (artifact-count-matters / affinity-for-artifacts) read an ETB
            # zone change, not a type-change, so these are off-mechanic producers.
            # Measured 136 cards dropped, all animation/becoming effects, 0 artifact-ETB
            # engine evicted.  Shipped together with the coupled creature-enters fix
            # (repeatable-token-generator → repeatable-creature-tokens) so
            # validate_seed_map stays at 0 violations (proof:
            # subagents/r2-tier-b-narrowing/outbox.md).
            'seeds': [
                'artifacts',
                'repeatable-artifact-tokens',
                'repeatable-treasures',
                # impulse / cheat-into-play (#74 decision 5): artifact-specific
                # from-hand onto-bf, matching the catalog's artifact-specific
                # design (precision 1.000 vs the onto-bf reference). Proof:
                # ADR 0003 re-baseline + impulse-seed-scope.md.
                'sneak-artifact',
            ],
            'grade': 'M',
        },
    },
    # ── 16. food-created ────────────────────────────────────────────────
    'food-created': {
        'triggers': {
            # eat-food dropped (#74 §3.5): it is a flavor/external-prop Un-set tag
            # ("actual food, generally being eaten"), not a Food-token payoff;
            # synergy-food is the real consumer and stays.
            'seeds': [
                'lifegain-matters',
                'synergy-food',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # repeatable-treasures dropped (#74 §3.5): Treasure != Food (CR 111.10);
            # measured overlap with repeatable-food is 4 cards (98% false).
            'seeds': [
                'repeatable-food',
            ],
            'grade': 'M',
        },
    },
    # ── 17. draw-a-card ─────────────────────────────────────────────────
    'draw-a-card': {
        'triggers': {
            'seeds': [
                'draw-matters',
                'hand-size-matters',
                'second-draw-matters',
                'third-draw-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'burst-draw',
                'draw',
                'draw-engine',
                'repeatable-draw',
            ],
            'grade': 'S',
        },
    },
    # ── 18. discard ─────────────────────────────────────────────────────
    'discard': {
        'triggers': {
            'seeds': [
                'castable-from-graveyard',
                'discard-matters',
                'discarded-type-matters',
                'self-discard-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'discard-outlet',
                'discard-outlet-artifact',
                'discard-outlet-creature',
                'discard-outlet-land',
                'discard-outlet-last-drawn',
                'discard-outlet-legendary',
                'discard-outlet-nonland',
                'discard-outlet-random',
                'free-discard-outlet',
                'loot',
                'rummage',
                'wheel',
            ],
            'grade': 'S',
        },
    },
    # ── 19. recursion-from-graveyard ────────────────────────────────────
    # Reading B (CR 404 + prose; audit §3.6, #19).  The event is "a card
    # RETURNS from the graveyard" (the regrowth/reanimate act), so the PRODUCERS
    # are the cards that DO the returning and the CONSUMERS are the payoffs that
    # care about something leaving the graveyard / triggering from it.
    #
    # Role inversion fixed: the prior shape had produced_by = the graveyard-
    # FILLERS (discard/mill/sac) and triggers = the reanimate/recursion family —
    # i.e. it re-modeled "graveyard stocked -> recursion consumes", which IS
    # `card-enters-graveyard`'s semantics (same filler producers, same reanimate-
    # family consumers) and made this node a role-inverted near-duplicate.
    #   * produced_by -> the `recursion` ROOT (2,370 cards, 100-slug closure
    #     covering EVERY reanimate-*/recursion-* variant + regrowth/restock/
    #     copy-from-graveyard/grim-return/...).  It is mechanic-exclusive against
    #     the returner mechanic (precision 1.0, no foreign heavy child) and the
    #     highest such anchor (a root — nothing to climb to).  grade 'S'.
    #   * triggers -> the recursion PAYOFFS: `leaving-graveyard-matters` (59) +
    #     `trigger-from-graveyard` (147) — the cards that care about a card
    #     leaving / acting from the graveyard.  grade 'M'.
    #   * The gy-FILLERS move OUT: they own `card-enters-graveyard.produced_by`
    #     (discard/mill/sacrifice), where the filler->reanimate "graveyard engine"
    #     still lives (the reanimate family is a consumer THERE).  Relocation,
    #     not loss.
    #   * The opponent-scoped `discard` otag (opponent-hand attack — fills the
    #     OPPONENT's graveyard, wrong scope for your recursion payoffs) is dropped
    #     with the fillers (audit §3.3/§3.6).
    'recursion-from-graveyard': {
        'triggers': {
            'seeds': [
                'leaving-graveyard-matters',
                'trigger-from-graveyard',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'recursion',
            ],
            'grade': 'S',
        },
    },
    # ── (collapsed) deathtouch-combat ───────────────────────────────────
    # REMOVED per ADR 0025 (grant-vs-use rule). deathtouch is a static combat-damage
    # modifier, not its own CR event: its grants (deathtouch-counter / gains-deathtouch /
    # gives-deathtouch) are now PRODUCERS of combat-damage-dealt (#9) AND permanent-dies
    # (#4 — deathtouch kills what it fights). Its synergy-deathtouch / old-*-deathtouch
    # payoffs are keyword-tribal characteristics -> the `deathtouch` distill identity
    # (distill_strategies.py, R5a), not event triggers. Event count 30 -> 29.
    # ── 21. extra-turn (#74 §2 split: was monarch-or-extra-turn) ─────────
    # off-turn-casting-matters dropped: its members are flash/cast-on-opponents'-
    # turns payoffs, produced by neither extra turns nor monarch.
    'extra-turn': {
        'triggers': {
            'seeds': ['turns-taken-matter'],
            'grade': 'M',
        },
        'produced_by': {
            # extra-turn is a text-synth attr (no otag); closure is itself.
            'seeds': ['extra-turn'],
            'grade': 'empty',
        },
    },
    # ── 21b. monarch (#74 §2 split; §2.2 option (ii): FUNCTIONAL SPLIT) ───
    # Scryfall carries ONE monarch-matters tag on both makers and payoffs, so the
    # single attr on both sides formed no edge (the p != c_ self-pair exclusion in
    # strategy_graph). The `monarch-maker` synth attr (distill_strategies.py,
    # oracle:'become the monarch', 62 makers) now isolates the PRODUCER side so
    # maker->payoff edges form: produced_by seeds the makers, triggers keeps the
    # payoff otag monarch-matters. (monarch-maker is a synth attr with no otag, so
    # its closure is itself -> grade 'empty', mirroring rad-counter on #24.)
    'monarch': {
        'triggers': {
            'seeds': ['monarch-matters'],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': ['monarch-maker'],
            'grade': 'empty',
        },
    },
    # ── 22. creature-copied ─────────────────────────────────────────────
    'creature-copied': {
        'triggers': {
            # eat-food dropped (#74 §3.5): flavor Un-set tag that rode along with
            # the Shelob food-copy note; synergy-food is the real consumer.
            'seeds': [
                'creature-count-matters',
                'creaturefall',
                'enters-in-company',
                'leaves-body-behind',
                'synergy-food',
                'synergy-token',
                'synergy-token-creature',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # clones -> clone (#74 #22): the real Scryfall slug is `clone` (77
            # cards, "enter as copies of other things"); `clones` matched zero.
            'seeds': [
                'clone',
                'copy-creature',
                'copy-self',
                'copy-token',
            ],
            'grade': 'M',
        },
    },
    # ── 23. untap ───────────────────────────────────────────────────────
    'untap': {
        'triggers': {
            'seeds': [
                'synergy-untapped',
                'untapped-matters-self',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'etb-untapper',
                'extra-untap',
                'ritual-untap',
                'untapper',
                'untapper-artifact',
                'untapper-creature',
                'untapper-equipment',
                'untapper-land',
                'untapper-nonland',
                'untapper-permanent',
                'untapper-planeswalker',
                'untaps-self',
            ],
            'grade': 'S',
        },
    },
    # ── 24. player-counter ──────────────────────────────────────────────
    'player-counter': {
        'triggers': {
            # counter-fuel-energy (#74 §4.4): energy counters are PLAYER counters
            # (CR 122.1a), so the energy consumer belongs here (moved from
            # counter-placed, where no energy PRODUCER sat). It stays in
            # proliferate.triggers too (proliferate can add energy to a player).
            'seeds': [
                'counter-fuel-energy',
                'mill-any',
                'mill-each',
                'mill-opponent',
                'mill-self',
                'pseudo-proliferate',
                'repeatable-proliferate',
                'synergy-mill',
                'synergy-proliferate',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # rad-counter is a text-synth attr with NO otag, kept for
            # backward compatibility with the committed catalog.
            # energy-generator (#74 §4.4): the real energy PRODUCER otag (137,
            # mechanic-exclusive prec 1.0) — energy counters are player counters,
            # so this lights up energy-generator -> counter-fuel-energy edges that
            # were permanently dead when no energy producer existed anywhere.
            'seeds': ['energy-generator', 'rad-counter'],
            'grade': 'M',
        },
    },
    # ── 25. mana-produced ───────────────────────────────────────────────
    'mana-produced': {
        'triggers': {
            'seeds': [
                'bottomless-mana-sink',
                'mana-increaser',
                'mana-sink',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # THE PROVEN SET (ADR 0020): mana-producer IN, ramp OUT.
            'seeds': [
                'adds-multiple-mana',
                'mana-producer',
                'repeatable-treasures',
                'ritual',
            ],
            'grade': 'M',
        },
    },
    # ── 26. game-end (terminal) ─────────────────────────────────────────
    # #74 §4.2: alternate-win-condition (87: Approach, Thassa's Oracle, Simic
    # Ascendancy, Mechanized Production) genuinely produces game-end; lose-trigger
    # (12: Curse of Vengeance, Sengir the Dark Baron) is a genuine consumer of
    # game-end(loss, opponent-scope). alternate-loss-condition is DEFERRED (the
    # node-level polarity field cannot distinguish loss- from win-producers on the
    # otag side). Seeds live here (not the raw JSON lists) so the committed catalog
    # keeps game-end's raw produced_by/triggers == [] (the terminal invariant) while
    # the derived view carries the edges.
    'game-end': {
        'triggers': {'seeds': ['lose-trigger'], 'grade': 'M'},
        'produced_by': {'seeds': ['alternate-win-condition'], 'grade': 'M'},
    },
    # ── 27. library-depleted ────────────────────────────────────────────
    'library-depleted': {
        'triggers': {'seeds': [], 'grade': 'empty'},
        'produced_by': {'seeds': [], 'grade': 'empty'},
    },
    # ── 28. poisoned (#74 §4.5) ─────────────────────────────────────────
    # Poison counters are PLAYER counters whose SBA ends the game (CR 104.3d /
    # 704.5c: 10 poison = loss). produced_by seeds the mechanic-exclusive poison
    # PRODUCER/giver otags (all prec 1.0 vs the poison family, 163 cards union);
    # the parent `poison-mechanics` (198) is NOT used because its closure also
    # carries the consumer `synergy-poison` and the removes-infect/gains-* cards
    # (producer-role impurity). triggers seeds the payoff/consumer `synergy-poison`
    # (42). The poisoned -> game-end WIN rule-edge lives in combo_win.py
    # (EVENT_EDGE_CLASSES['poisoned'] = ['altwin::poisoned']); it is walk-inert
    # until a poison fid route exists (fid 1800 -> None today, out of scope).
    'poisoned': {
        'triggers': {
            'seeds': ['synergy-poison'],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'gives-infect',
                'gives-poisonous',
                'gives-toxic',
                'poison-opponents',
                'poisonous',
            ],
            'grade': 'M',
        },
    },
    # ── 29. domain (ADR 0024; #74) ──────────────────────────────────────
    # A five-color/domain board-state WIN (Coalition Victory: control a land of
    # each basic type AND a creature of each color -> you win). The reusable
    # game-state is a STATE-EVENT `domain` (ADR 0024 Home-3), mirroring `poisoned`:
    # the enablers PRODUCE the state and the wincon CONSUMES it. Both sides are
    # authored SYNTH attrs (no clean otag), so grade 'empty' (closure = itself,
    # like monarch-maker/rad-counter): produced_by = `color-fixing` (union of
    # oracle:'mana of any color' + rainbow-land, 709 fixers), triggers =
    # `five-color-win` (alternate-win-condition AND oracle:'each color' = Coalition
    # Victory + Call the Spirit Dragons). The domain -> game-end WIN rule-edge lives
    # in combo_win.py (EVENT_EDGE_CLASSES['domain'] = ['altwin::domain']); it is
    # walk-inert until a domain fid route exists (none today, mirrors poison). The
    # enabler->win link is the strategy-graph event edge color-fixing -> domain ->
    # five-color-win — the point of ADR 0024.
    'domain': {
        'triggers': {
            'seeds': ['five-color-win'],
            'grade': 'empty',
        },
        'produced_by': {
            'seeds': ['color-fixing'],
            'grade': 'empty',
        },
    },
}


# ---------------------------------------------------------------------------
# Taxonomy loader
# ---------------------------------------------------------------------------


def _taxonomy_path() -> Path:
    """Resolve the oracle_tags.parquet path, respecting ``MAKE_MAGIC_DATA_DIR``."""
    base = _os.environ.get('MAKE_MAGIC_DATA_DIR')
    root = Path(base) if base else (Path.home() / '.local' / 'share' / 'make-magic')
    return root / 'raw' / 'oracle_tags.parquet'


@lru_cache(maxsize=1)
def load_taxonomy() -> dict[str, dict[str, Any]]:
    """Load the otag taxonomy and build ``slug -> {children: [slug, ...], ...}``.

    Returns a dict keyed by slug.  Each value has:
    - ``children``: list[str] — child slugs
    - ``parents``: list[str] — parent slugs
    - ``description``: str — tag description (for audit)

    Uses DuckDB to read the parquet in-process.  Cached.
    """
    import duckdb

    path = _taxonomy_path()
    if not path.exists():
        raise FileNotFoundError(f'oracle_tags.parquet not found at {path}; hydrate with `collection hydrate-lake`')

    con = duckdb.connect(':memory:')
    try:
        rows = con.execute(
            f'SELECT CAST(id AS VARCHAR) AS id, slug, '
            f"COALESCE(description, '') AS description, "
            f'parent_ids, child_ids '
            f"FROM read_parquet('{path}')"
        ).fetchall()
    finally:
        con.close()

    # Build id→slug for cross-referencing UUIDs in parent_ids/child_ids.
    id_to_slug: dict[str, str] = {}
    for uid, slug, _desc, _pids, _cids in rows:
        id_to_slug[uid] = slug

    taxonomy: dict[str, dict[str, Any]] = {}
    for _uid, slug, desc, parent_ids, child_ids in rows:
        parents = [id_to_slug[str(p)] for p in (parent_ids or []) if str(p) in id_to_slug]
        children = [id_to_slug[str(c)] for c in (child_ids or []) if str(c) in id_to_slug]
        taxonomy[slug] = {
            'children': children,
            'parents': parents,
            'description': desc,
        }

    return taxonomy


# ---------------------------------------------------------------------------
# Descendant closure
# ---------------------------------------------------------------------------


def descendant_closure(slug: str, taxonomy: dict[str, dict[str, Any]]) -> frozenset[str]:
    """All descendants of *slug* (including itself) via BFS over children.

    If *slug* is not in the taxonomy, returns ``frozenset({slug})`` — the slug
    itself is still included so enumerated slugs not in the taxonomy (e.g.
    synthetic attrs) are not silently dropped.
    """
    if slug not in taxonomy:
        return frozenset({slug})

    visited: set[str] = set()
    queue: deque[str] = deque([slug])
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for child in taxonomy.get(current, {}).get('children', []):
            if child not in visited:
                queue.append(child)
    return frozenset(visited)


# ---------------------------------------------------------------------------
# Mechanic-exclusivity validator (task 6.1b) — the seed rule as ENFORCED code
#
# The seed for each event side must be *mechanic-exclusive*: climbing to
# a higher taxonomy concept must not admit a different mechanic's cards.  We
# measure this against the CARD population (oracle_ids) each slug reaches, which
# is ancestor-closed in the otag taxonomy (verified 0 violations in the
# build-design pass), so ``cards(slug)`` == all cards carrying the concept.
# ---------------------------------------------------------------------------

#: An ancestor is kept only while its closure stays >= this fraction within the
#: side's mechanic population.  ``ramp`` drops to 0.56 (mana) / 0.28 (land).
EXCLUSIVITY_PRECISION_MIN: float = 0.6

#: A child branch is "heavy" (a candidate distinct mechanic) at >= this many
#: cards; smaller tails are treated as same-mechanic refinements / noise.
EXCLUSIVITY_BRANCH_FLOOR: int = 50

#: A heavy child below this precision vs the mechanic is a FOREIGN branch — its
#: presence means the parent spans a second mechanic (the structural signal).
EXCLUSIVITY_BRANCH_PRECISION_MIN: float = 0.5


class ExclusivityResult(NamedTuple):
    """Outcome of :func:`is_mechanic_exclusive`."""

    ok: bool
    precision: float
    reason: str


@lru_cache(maxsize=1)
def _load_direct_cards() -> dict[str, frozenset[str]]:
    """Load ``slug -> frozenset(oracle_id)`` of DIRECTLY-tagged cards.

    Reads the ``taggings`` column of the otag parquet.  Direct only; the
    ancestor-closed card set for a slug is the union over its descendant closure
    (see :func:`card_set`).
    """
    import duckdb

    path = _taxonomy_path()
    if not path.exists():
        raise FileNotFoundError(f'oracle_tags.parquet not found at {path}; hydrate with `collection hydrate-lake`')

    con = duckdb.connect(':memory:')
    try:
        rows = con.execute(f"SELECT slug, taggings FROM read_parquet('{path}')").fetchall()
    finally:
        con.close()

    out: dict[str, frozenset[str]] = {}
    for slug, taggings in rows:
        oids = {str(r['oracle_id']) for r in (taggings or [])}
        out[slug] = frozenset(oids)
    return out


@cache
def card_set(slug: str) -> frozenset[str]:
    """All oracle_ids carrying *slug* = union of direct cards over its closure.

    A slug not in the taxonomy (synthetic attr) reaches only its own direct
    cards, if any, else the empty set.
    """
    taxonomy = load_taxonomy()
    direct = _load_direct_cards()
    cards: set[str] = set()
    for desc in descendant_closure(slug, taxonomy):
        cards |= direct.get(desc, frozenset())
    return frozenset(cards)


def mechanic_population(seeds: list[str] | tuple[str, ...]) -> frozenset[str]:
    """The card population a side's *seeds* establish as its mechanic.

    = union of the ancestor-closed card sets of every seed.  This is the
    ground-truth ``M`` the exclusivity check measures candidates against.
    """
    pop: set[str] = set()
    for a in seeds:
        pop |= card_set(a)
    return frozenset(pop)


def is_mechanic_exclusive(
    node: str,
    mechanic: frozenset[str],
    *,
    prec_min: float = EXCLUSIVITY_PRECISION_MIN,
    branch_floor: int = EXCLUSIVITY_BRANCH_FLOOR,
    branch_prec_min: float = EXCLUSIVITY_BRANCH_PRECISION_MIN,
) -> ExclusivityResult:
    """Is *node* exclusive to *mechanic* (so it may be a seed / be climbed to)?

    GENERIC two-part check, no per-event logic:

    * **(b) precision** — the fraction of ``node``'s cards inside ``mechanic``
      must be ``>= prec_min``.  A node that drags in a foreign mechanic dilutes.
    * **(a) foreign heavy child** — ``node`` must have no child whose closure is
      ``>= branch_floor`` cards yet mostly (precision ``< branch_prec_min``)
      outside ``mechanic``.  Such a child is a distinct sibling mechanic.

    Fails fast on the first signal.  A node with no cards (synthetic leaf) is
    trivially exclusive.
    """
    taxonomy = load_taxonomy()
    cards = card_set(node)
    if not cards:
        return ExclusivityResult(True, 1.0, 'no-cards (synthetic/leaf)')

    precision = len(cards & mechanic) / len(cards)
    if precision < prec_min:
        return ExclusivityResult(False, precision, f'precision {precision:.3f} < {prec_min}')

    for child in taxonomy.get(node, {}).get('children', []):
        child_cards = card_set(child)
        if len(child_cards) < branch_floor:
            continue
        child_prec = len(child_cards & mechanic) / len(child_cards)
        if child_prec < branch_prec_min:
            return ExclusivityResult(
                False,
                precision,
                f'foreign heavy child {child!r} ({len(child_cards)} cards, precision {child_prec:.3f})',
            )

    return ExclusivityResult(True, precision, 'ok')


def proper_ancestors(slug: str, taxonomy: dict[str, dict[str, Any]]) -> frozenset[str]:
    """All strict ancestors of *slug* via BFS over parents (excludes *slug*)."""
    if slug not in taxonomy:
        return frozenset()
    visited: set[str] = set()
    queue: deque[str] = deque(taxonomy[slug].get('parents', []))
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for parent in taxonomy.get(current, {}).get('parents', []):
            if parent not in visited:
                queue.append(parent)
    return frozenset(visited)


def refused_climb_targets(seeds: list[str]) -> dict[str, ExclusivityResult]:
    """Ancestors of *seeds* the rule REFUSES (the cross-mechanic umbrellas).

    For every seed, every proper ancestor that is NOT itself a seed and is
    NOT mechanic-exclusive against the side's mechanic.  ``ramp`` appears here
    for both mana-produced and land-enters — the generic auto-exclusion proof.
    """
    taxonomy = load_taxonomy()
    mechanic = mechanic_population(seeds)
    seed_set = set(seeds)
    refused: dict[str, ExclusivityResult] = {}
    for seed in seeds:
        for ancestor in proper_ancestors(seed, taxonomy):
            if ancestor in seed_set or ancestor in refused:
                continue
            res = is_mechanic_exclusive(ancestor, mechanic)
            if not res.ok:
                refused[ancestor] = res
    return refused


#: A single seed covering >= this fraction of a disjoint foreign event-side's
#: seed set is a cross-mechanic umbrella (it IS the other mechanic).
UMBRELLA_COVERAGE_MIN: float = 0.5

#: The foreign event-side must have at least this many seeds for the umbrella
#: check to apply (trivially small seed sets are not a meaningful mechanic).
UMBRELLA_FOREIGN_MIN_ANCHORS: int = 3


def validate_seed_map(
    seed_map: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Enforce the exclusivity rule over *seed_map*.  Returns violations.

    Hard invariants (empty list == clean):

    1. **Ancestor-closure** — every non-synthetic seed resolves to a
       non-empty descendant closure (no dangling slug).
    2. **Output purity** — the refused cross-mechanic umbrella ancestors (e.g.
       ``ramp``, discovered generically via :func:`refused_climb_targets`) never
       appear in the side's derived closure-union output.
    3. **Cross-mechanic umbrella** (NON-TAUTOLOGICAL) — for each seed A, if
       A's descendant-closure covers >= :data:`UMBRELLA_COVERAGE_MIN` of a
       *disjoint* foreign event-side's seed set (where the foreign side has >=
       :data:`UMBRELLA_FOREIGN_MIN_ANCHORS`), A is flagged as a cross-mechanic
       umbrella.  "Disjoint" = the two sides share no direct seeds; related
       events (sacrifice→death) naturally share seeds and are not compared.
       This check is non-tautological because a poisoned seed like ``ramp``
       whose closure spans ``mana-producer``/``ritual`` (50% of
       ``mana-produced.produced_by``) AND ``land-ramp``/``extra-land``/
       ``sneak-land`` (50% of ``land-enters.produced_by``) is caught, while
       legitimate same-mechanic seeds whose closures reach into related events
       (via shared seeds) are not compared.  A ``ramp`` hand-added as a seed is
       a reviewable data error now CAUGHT at validation time.

    Parameters
    ----------
    seed_map
        Override for testing; defaults to the module-level :data:`SEED_MAP`.
    """
    if seed_map is None:
        seed_map = SEED_MAP
    taxonomy = load_taxonomy()
    violations: list[str] = []

    # Pre-compute seed sets for the umbrella check.
    seed_sets: dict[tuple[str, str], set[str]] = {}
    for ev, entry in seed_map.items():
        for s in ('produced_by', 'triggers'):
            seed_sets[(ev, s)] = set(entry[s]['seeds'])

    for event_name, entry in seed_map.items():
        for side in ('produced_by', 'triggers'):
            seeds = entry[side]['seeds']
            if not seeds:
                continue

            # (1) ancestor-closure: closure non-empty for every seed.
            for seed in seeds:
                if not descendant_closure(seed, taxonomy):
                    violations.append(f'{event_name}.{side}: seed {seed!r} has empty closure')

            # (2) output purity: no refused umbrella ancestor leaks into the
            # derived closure-union output.
            output: set[str] = set()
            for seed in seeds:
                output |= descendant_closure(seed, taxonomy)
            for umbrella in refused_climb_targets(seeds):
                if umbrella in output:
                    violations.append(
                        f'{event_name}.{side}: cross-mechanic umbrella {umbrella!r} leaked into the derived output'
                    )

            # (3) Cross-mechanic umbrella check (NON-TAUTOLOGICAL).
            own_seeds = set(seeds)
            for seed in seeds:
                closure = descendant_closure(seed, taxonomy)
                for (e2, s2), seeds2 in seed_sets.items():
                    if e2 == event_name or s2 != side:
                        continue
                    # Only compare disjoint event-sides (no shared seeds).
                    if own_seeds & seeds2:
                        continue
                    if len(seeds2) < UMBRELLA_FOREIGN_MIN_ANCHORS:
                        continue
                    overlap = closure & seeds2
                    coverage = len(overlap) / len(seeds2)
                    if coverage >= UMBRELLA_COVERAGE_MIN:
                        violations.append(
                            f'{event_name}.{side}: seed {seed!r} is a '
                            f'cross-mechanic umbrella — its closure covers '
                            f'{len(overlap)}/{len(seeds2)} '
                            f'({coverage:.0%}) of {e2}.{s2} seeds'
                        )

    return violations


# ---------------------------------------------------------------------------
# Derived event adjacency
# ---------------------------------------------------------------------------


def derive_card_events(
    events: dict[str, dict],
) -> dict[str, dict]:
    """Compute derived ``produced_by``/``triggers`` from seeds + taxonomy closure.

    For each event side, the derived list = sorted union of descendant-closures
    of all seeds for that side.  The committed enumerated lists are a subset
    of the derived lists (no loss) because the seed map includes both the
    build-design structural seeds AND the existing enumerated slugs.

    The returned dict has the same shape as *events* — every event key is
    present with the same ``rule``/``abbrev``/``polarity``/``scope`` fields.
    Only ``produced_by`` and ``triggers`` lists are replaced.

    Events not in :data:`SEED_MAP` are passed through unchanged.
    """
    taxonomy = load_taxonomy()

    derived: dict[str, dict] = {}
    for event_name, ev in events.items():
        entry = SEED_MAP.get(event_name)
        if entry is None:
            # Not in seed map — pass through unchanged.
            derived[event_name] = dict(ev)
            continue

        # Start with a copy of the original event (preserving rule, abbrev, etc.)
        new_ev = dict(ev)

        for side in ('produced_by', 'triggers'):
            seeds = entry[side]['seeds']
            if not seeds:
                # Empty seed set — keep the original list (terminal/inert events).
                continue

            # Expand each seed via descendant-closure and union.
            expanded: set[str] = set()
            for seed in seeds:
                expanded |= descendant_closure(seed, taxonomy)

            new_ev[side] = sorted(expanded)

        derived[event_name] = new_ev

    return derived


# ---------------------------------------------------------------------------
# Authoritative at catalog load (task 6.5 flip — ADR 0022)
# ---------------------------------------------------------------------------
#
# The derived adjacency is now the DEFAULT, applied where the catalog is loaded
# (``assets.load_assets`` and ``engine_hydration._load_events``) via
# :func:`derive_card_events`.  The former ``SHADOW_DERIVED_ADJACENCY`` flag
# and its ``active_events`` gate are retired — there is no longer an enumerated
# path to toggle; the hand-enumerated catalog lists are the frozen seed that the
# derivation expands over the taxonomy.


# ---------------------------------------------------------------------------
# CI-enforced seed-map validation (``MAKE_MAGIC_VALIDATE_ANCHORS=1``)
# ---------------------------------------------------------------------------
#
# Full validation (card-set loads from parquet + leave-one-out exclusivity) is
# too expensive for every import (~0.5 s + I/O + DuckDB).  It runs at module
# load when the env flag is set, which CI MUST set.  The fail-fast ensures a
# poisoned seed (e.g. ``ramp`` hand-listed as a seed) is CAUGHT, not
# silently derived.

if _os.environ.get('MAKE_MAGIC_VALIDATE_ANCHORS') == '1':
    _violations = validate_seed_map()
    if _violations:
        raise RuntimeError(
            f'SEED_MAP validation failed ({len(_violations)} violations):\n'
            + '\n'.join(f'  - {v}' for v in _violations)
        )
