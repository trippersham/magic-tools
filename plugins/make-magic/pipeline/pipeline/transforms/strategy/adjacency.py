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
one level below any ancestor that admits a different mechanic.  The 27-event
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
``derive_event_adjacency(events)`` returns a new events dict where each event's
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

import enum
import os as _os
from collections import deque
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# 27-event seed map — owner-approved DATA from the validated build-design.
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
                'repeatable-creature-tokens',
                'repeatable-token-generator',
                'sneak-creature',
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
            'seeds': [
                'free-sacrifice-outlet',
                'removal',
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
                'sac',
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
            'seeds': [
                'copy-instant',
                'copy-sorcery',
                'copy-spell',
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
            'seeds': [
                'combat',
                'damage-multiplier',
                'gains-double-strike',
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
            'seeds': [
                'burn',
                'burn-player',
                'burn-player-each',
                'drain-life',
                'omnivore-ability',
                'opponent-loses-life',
                'sac',
            ],
            'grade': 'M',
        },
    },
    # ── 12. counter-placed ──────────────────────────────────────────────
    'counter-placed': {
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
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'pseudo-proliferate',
                'repeatable-proliferate',
                'synergy-proliferate',
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
            'seeds': [
                'animate-artifact',
                'artifactify',
                'artifacts',
                'repeatable-artifact-tokens',
                'repeatable-treasures',
            ],
            'grade': 'M',
        },
    },
    # ── 16. food-created ────────────────────────────────────────────────
    'food-created': {
        'triggers': {
            'seeds': [
                'eat-food',
                'lifegain-matters',
                'synergy-food',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'repeatable-food',
                'repeatable-treasures',
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
    'recursion-from-graveyard': {
        'triggers': {
            'seeds': [
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
            ],
            'grade': 'S',
        },
        'produced_by': {
            # Build-design P seeds: recursion/reanimate (the cards that
            # PERFORM the graveyard return) + mill/discard/sac fillers (that
            # put cards INTO the graveyard enabling recursion).  `recursion`
            # is a ROOT (100 descendants) covering all reanimate-*/recursion-*
            # variants.  Adding it here is the single biggest unlock (~25
            # edges) identified by the build-design measurement.
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
                'mill',
                'mill-self',
                'recursion',
                'sac',
            ],
            'grade': 'M',
        },
    },
    # ── 20. deathtouch-combat ───────────────────────────────────────────
    'deathtouch-combat': {
        'triggers': {
            'seeds': [
                'combat',
                'old-blocking-deathtouch',
                'old-damage-deathtouch',
                'synergy-deathtouch',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'deathtouch-counter',
                'gains-deathtouch',
                'gives-deathtouch',
            ],
            'grade': 'M',
        },
    },
    # ── 21. monarch-or-extra-turn ───────────────────────────────────────
    'monarch-or-extra-turn': {
        'triggers': {
            'seeds': [
                'monarch-matters',
                'off-turn-casting-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': ['extra-turn'],
            'grade': 'empty',
        },
    },
    # ── 22. creature-copied ─────────────────────────────────────────────
    'creature-copied': {
        'triggers': {
            'seeds': [
                'creature-count-matters',
                'creaturefall',
                'eat-food',
                'enters-in-company',
                'leaves-body-behind',
                'synergy-food',
                'synergy-token',
                'synergy-token-creature',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'seeds': [
                'clones',
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
            'seeds': [
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
            'seeds': ['rad-counter'],
            'grade': 'empty',
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
    'game-end': {
        'triggers': {'seeds': [], 'grade': 'empty'},
        'produced_by': {'seeds': [], 'grade': 'empty'},
    },
    # ── 27. library-depleted ────────────────────────────────────────────
    'library-depleted': {
        'triggers': {'seeds': [], 'grade': 'empty'},
        'produced_by': {'seeds': [], 'grade': 'empty'},
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


def derive_event_adjacency(
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
# :func:`derive_event_adjacency`.  The former ``SHADOW_DERIVED_ADJACENCY`` flag
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


# ---------------------------------------------------------------------------
# Seed-discovery (event-candidate evaluation) loop (task 6.3) — candidate discovery + N=3 promotion
# ---------------------------------------------------------------------------
#
# The mechanism below lets the concept-anchored adjacency EXTEND ITSELF from
# evidence.  When an otag appears on corroborated cards for an event side but
# is NOT yet in the side's seed closure, it becomes a **candidate seed** —
# provided it passes the SAME ``is_mechanic_exclusive`` gate as the seeds
# (so umbrellas like ``ramp`` can never re-enter).  A candidate promotes to
# the seed set when >= N=3 independent cards corroborate it.
#
# The mechanism is SHADOW (the promotion is behind a flag, owner-gated) — the
# authoritative derived adjacency is unchanged by this code.
# ---------------------------------------------------------------------------

#: Promotion threshold: a candidate must be corroborated by this many
#: independent cards (distinct oracle_ids) before it is eligible for promotion.
PROMOTION_THRESHOLD: int = 3


class GateVerdict(enum.Enum):
    """Three-way corroboration gate outcomes."""

    #: The otag is in the current seed closure — accept immediately.
    ACCEPT_VIA_SEED = 'accept-via-seed'

    #: The otag is on-mechanic and passes exclusivity, but is not yet in
    #: the closure — accept provisionally and propose as a candidate seed.
    ACCEPT_PROVISIONAL = 'accept-provisional'

    #: The otag fails exclusivity or is genuinely loose — reject.
    REJECT = 'reject'


@dataclass(frozen=True)
class GateResult:
    """Outcome of the three-way gate for a single otag."""

    verdict: GateVerdict
    otag: str
    reason: str


@dataclass(frozen=True)
class CandidateSeed:
    """A candidate seed otag discovered from evidence."""

    event: str
    side: str  # 'triggers' | 'produced_by'
    otag: str
    corroborating_cards: frozenset[str]  # oracle_ids

    @property
    def corroboration_count(self) -> int:
        return len(self.corroborating_cards)


class CandidateSeedStore:
    """In-memory store for candidate seeds with corroboration tracking.

    Records candidate otags and the independent cards that corroborate them.
    A candidate promotes when its corroboration count reaches
    :data:`PROMOTION_THRESHOLD`.  Reuses the ADR-0008 provisional->verified
    lifecycle concept.
    """

    def __init__(self) -> None:
        # Key: (event, side, otag) -> set of oracle_ids
        self._candidates: dict[tuple[str, str, str], set[str]] = {}

    def record_corroboration(
        self,
        event: str,
        side: str,
        otag: str,
        card_oracle_id: str,
    ) -> None:
        """Record that *card_oracle_id* corroborates *otag* for *event.side*."""
        key = (event, side, otag)
        if key not in self._candidates:
            self._candidates[key] = set()
        self._candidates[key].add(card_oracle_id)

    def promotable_candidates(
        self,
        threshold: int = PROMOTION_THRESHOLD,
    ) -> list[CandidateSeed]:
        """Return candidates with >= *threshold* independent corroborating cards."""
        result: list[CandidateSeed] = []
        for (event, side, otag), cards in sorted(self._candidates.items()):
            if len(cards) >= threshold:
                result.append(
                    CandidateSeed(
                        event=event,
                        side=side,
                        otag=otag,
                        corroborating_cards=frozenset(cards),
                    )
                )
        return result

    def all_candidates(self) -> list[CandidateSeed]:
        """Return ALL candidates regardless of corroboration count."""
        result: list[CandidateSeed] = []
        for (event, side, otag), cards in sorted(self._candidates.items()):
            result.append(
                CandidateSeed(
                    event=event,
                    side=side,
                    otag=otag,
                    corroborating_cards=frozenset(cards),
                )
            )
        return result

    @property
    def total_candidates(self) -> int:
        return len(self._candidates)


def _closure_for_side(event_name: str, side: str) -> frozenset[str]:
    """Compute the descendant-closure of all seeds for *event_name.side*."""
    taxonomy = load_taxonomy()
    entry = SEED_MAP.get(event_name)
    if entry is None:
        return frozenset()
    seeds = entry[side].get('seeds', [])
    closure: set[str] = set()
    for seed in seeds:
        closure |= descendant_closure(seed, taxonomy)
    return frozenset(closure)


def three_way_gate(
    otag: str,
    events: dict[str, dict],
    event_name: str,
    side: str,
) -> GateResult:
    """Classify *otag* via the three-way corroboration gate.

    - **accept-via-seed**: otag is already in the side's seed closure.
    - **accept-provisional**: otag passes ``is_mechanic_exclusive`` against
      the side's mechanic but is not yet in the closure — propose as
      candidate seed.
    - **reject**: otag fails exclusivity or has no cards (genuinely loose).

    GENERIC: no per-event branches, no hand-listed umbrellas.
    """
    closure = _closure_for_side(event_name, side)

    # (1) Already in closure — immediate accept.
    if otag in closure:
        return GateResult(
            verdict=GateVerdict.ACCEPT_VIA_SEED,
            otag=otag,
            reason='in seed closure',
        )

    # (2) Check mechanic exclusivity.
    entry = SEED_MAP.get(event_name)
    if entry is None:
        return GateResult(
            verdict=GateVerdict.REJECT,
            otag=otag,
            reason=f'event {event_name!r} not in SEED_MAP',
        )

    seeds = entry[side].get('seeds', [])
    if not seeds:
        return GateResult(
            verdict=GateVerdict.REJECT,
            otag=otag,
            reason=f'{event_name}.{side} has no seeds',
        )

    mechanic = mechanic_population(seeds)
    excl = is_mechanic_exclusive(otag, mechanic)

    if excl.ok:
        return GateResult(
            verdict=GateVerdict.ACCEPT_PROVISIONAL,
            otag=otag,
            reason=f'exclusive (precision={excl.precision:.3f}) but not in closure',
        )

    return GateResult(
        verdict=GateVerdict.REJECT,
        otag=otag,
        reason=f'not exclusive: {excl.reason}',
    )


def discover_candidates(
    events: dict[str, dict],
    event_name: str,
    side: str,
    otag_evidence: dict[str, set[str]] | None = None,
) -> list[CandidateSeed]:
    """Discover candidate seed otags for *event_name.side* from evidence.

    For each card in *otag_evidence* (oracle_id -> set of otags), if the card
    carries at least one otag in the current closure (i.e. it is a corroborated
    card for this event/side), collect all its OTHER otags that are:

    - NOT already in the closure
    - In the taxonomy (not synthetic)
    - Mechanic-exclusive for the side's mechanic

    Returns candidate seeds with their corroborating cards.

    GENERIC: no per-event branches, no hand-listed candidates or blocklists.

    Parameters
    ----------
    events
        The derived events dict (from ``derive_event_adjacency``).
    event_name
        Event to discover candidates for.
    side
        ``'triggers'`` or ``'produced_by'``.
    otag_evidence
        ``{oracle_id: {otag, ...}}`` evidence from the corpus.  If ``None``,
        loads the default residue_cards.jsonl.
    """
    closure = _closure_for_side(event_name, side)
    if not closure:
        return []

    entry = SEED_MAP.get(event_name)
    if entry is None:
        return []

    seeds = entry[side].get('seeds', [])
    if not seeds:
        return []

    mechanic = mechanic_population(seeds)
    taxonomy = load_taxonomy()

    if otag_evidence is None:
        otag_evidence = _load_default_evidence()

    # Accumulate: candidate_otag -> set of corroborating oracle_ids
    candidates: dict[str, set[str]] = {}

    for oracle_id, card_otags in otag_evidence.items():
        # Is this card corroborated for this event/side?
        if not (card_otags & closure):
            continue

        # Collect otags NOT in closure, IN taxonomy, AND exclusive
        for otag in card_otags:
            if otag in closure:
                continue
            if otag not in taxonomy:
                continue
            # Check exclusivity (cached)
            if not _cached_exclusivity_check(otag, mechanic):
                continue

            if otag not in candidates:
                candidates[otag] = set()
            candidates[otag].add(oracle_id)

    return [
        CandidateSeed(
            event=event_name,
            side=side,
            otag=otag,
            corroborating_cards=frozenset(cards),
        )
        for otag, cards in sorted(candidates.items())
    ]


@cache
def _cached_exclusivity_check(otag: str, mechanic: frozenset[str]) -> bool:
    """Cached wrapper: is *otag* mechanic-exclusive for *mechanic*?"""
    return is_mechanic_exclusive(otag, mechanic).ok


def _load_default_evidence() -> dict[str, set[str]]:
    """Load oracle_id -> set(otags) from the default residue_cards.jsonl."""
    import json as _json

    evidence_path = (
        Path(__file__).resolve().parent.parent.parent.parent / 'subagents' / '5.3b-work' / 'residue_cards.jsonl'
    )
    result: dict[str, set[str]] = {}
    if not evidence_path.exists():
        return result
    with evidence_path.open() as f:
        for line in f:
            c = _json.loads(line)
            result[c['oracle_id']] = set(c.get('otags', []))
    return result


# ---------------------------------------------------------------------------
# Shadow measurement (task 6.3) — what would the seed-discovery loop discover?
# ---------------------------------------------------------------------------


@dataclass
class HealingShadowReport:
    """Results of running the seed-discovery loop in shadow mode."""

    #: Total candidate otags discovered across all events/sides.
    total_candidates: int = 0

    #: Candidates that would promote at N=3.
    promotable_count: int = 0

    #: Number of umbrella-like otags that were PROPOSED (should be 0).
    umbrella_proposals: int = 0

    #: Projected corroboration percentage with promoted candidates.
    projected_unlock_pct: float = 0.0

    #: Current corroboration percentage (baseline).
    current_unlock_pct: float = 0.0

    #: Per-event candidate details.
    per_event: dict[str, dict[str, Any]] = field(default_factory=dict)

    #: The promotable candidates themselves.
    promotable: list[CandidateSeed] = field(default_factory=list)

    #: All candidates (for the research report).
    all_candidates: list[CandidateSeed] = field(default_factory=list)

    #: Extended adjacency slugs (what promotion would add).
    projected_new_slugs: dict[str, dict[str, set[str]]] = field(default_factory=dict)


def run_healing_shadow(
    otag_evidence: dict[str, set[str]],
) -> HealingShadowReport:
    """Run the event-candidate discovery + promotion in SHADOW mode.

    Does NOT modify the authoritative adjacency.  Returns a report of what
    the seed-discovery loop would discover, promote, and unlock.

    Parameters
    ----------
    otag_evidence
        ``{oracle_id: {otag, ...}}`` evidence from the corpus residue.
    """
    import json as _json

    from pipeline.transforms.strategy.engine_hydration import _load_events

    events = _load_events()
    store = CandidateSeedStore()
    report = HealingShadowReport()

    # Phase 1: Discover candidates for every event/side
    for event_name, entry in SEED_MAP.items():
        event_report: dict[str, Any] = {}
        for side in ('triggers', 'produced_by'):
            seeds = entry[side].get('seeds', [])
            if not seeds:
                event_report[side] = {'candidates': 0, 'promotable': 0, 'slugs': []}
                continue

            candidates = discover_candidates(events, event_name, side, otag_evidence)
            for cand in candidates:
                for card in cand.corroborating_cards:
                    store.record_corroboration(event_name, side, cand.otag, card)

            event_report[side] = {
                'candidates': len(candidates),
                'slugs': [c.otag for c in candidates],
            }

        report.per_event[event_name] = event_report

    # Phase 2: Identify promotable candidates
    promotable = store.promotable_candidates()
    all_cands = store.all_candidates()

    report.total_candidates = store.total_candidates
    report.promotable_count = len(promotable)
    report.promotable = promotable
    report.all_candidates = all_cands

    # Phase 3: Project adjacency extension
    taxonomy = load_taxonomy()
    new_slugs: dict[str, dict[str, set[str]]] = {}
    for cand in promotable:
        closure = _closure_for_side(cand.event, cand.side)
        added = descendant_closure(cand.otag, taxonomy) - closure
        if added:
            if cand.event not in new_slugs:
                new_slugs[cand.event] = {}
            if cand.side not in new_slugs[cand.event]:
                new_slugs[cand.event][cand.side] = set()
            new_slugs[cand.event][cand.side] |= added

    report.projected_new_slugs = new_slugs

    # Phase 4: Project unlock on edge-author edges
    edges_dir = Path(__file__).resolve().parent.parent.parent.parent / 'subagents' / '5.3b-work'

    role_a_edges: list[dict] = []
    import glob as _glob

    for f in sorted(_glob.glob(str(edges_dir / 'edges' / 'chunk-*-edges.jsonl'))):
        with open(f) as fh:
            for line in fh:
                e = _json.loads(line)
                if e.get('trigger_event') and e.get('produced_event'):
                    role_a_edges.append(e)

    if not role_a_edges:
        return report

    def _corroborates(edge: dict, ev_dict: dict[str, dict]) -> bool:
        otags = otag_evidence.get(edge['oracle_id'], set())
        t_list = set(ev_dict.get(edge['trigger_event'], {}).get('triggers', []))
        p_list = set(ev_dict.get(edge['produced_event'], {}).get('produced_by', []))
        return bool(otags & t_list) and bool(otags & p_list)

    n_edges = len(role_a_edges)
    current_corr = sum(1 for e in role_a_edges if _corroborates(e, events))
    report.current_unlock_pct = current_corr / n_edges * 100

    # Build projected events with promoted candidates added to the closure
    projected_events = _build_projected_events(events, promotable)
    projected_corr = sum(1 for e in role_a_edges if _corroborates(e, projected_events))
    report.projected_unlock_pct = projected_corr / n_edges * 100

    return report


def _build_projected_events(
    events: dict[str, dict],
    promotable: list[CandidateSeed],
) -> dict[str, dict]:
    """Build a projected events dict with promoted candidates in the adjacency.

    This is SHADOW-ONLY — the authoritative adjacency is not modified.
    """
    taxonomy = load_taxonomy()
    projected: dict[str, dict] = {}

    for event_name, ev in events.items():
        projected[event_name] = dict(ev)

    for cand in promotable:
        if cand.event not in projected:
            continue
        ev = dict(projected[cand.event])
        current_list = set(ev.get(cand.side, []))
        # Add the promoted candidate's descendant closure
        current_list |= descendant_closure(cand.otag, taxonomy)
        ev[cand.side] = sorted(current_list)
        projected[cand.event] = ev

    return projected
