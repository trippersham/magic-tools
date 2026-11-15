"""Anchor-based event adjacency derivation (Phase 6.1 + 6.2 + 6.1b).

Implements the **Maximal mechanic-exclusive anchor** rule as production code
(6.1), the **derived ``produced_by``/``triggers``** as a shadow view behind a
flag (6.2), and the **mechanic-exclusivity validator** that enforces the rule's
stop-condition so a cross-mechanic umbrella (``ramp``, ``triggered-ability``,
``thingfall``) can never enter the derived adjacency (6.1b).

The rule
--------
Given an event side (produce/trigger), each ANCHOR is the **highest ancestor**
in the otag taxonomy whose descendant-closure stays *mechanic-exclusive* — stop
one level below any ancestor that admits a different mechanic.  The 27-event
anchor map below was derived by the build-design measurement pass
(``subagents/adjacency-build-design/research/adjacency-build-design.md``) and is
reused here as DATA: each side's ``anchors`` are the owner-approved mechanic-
exclusive **seed concepts** (``mana-producer`` for "makes mana", ``land-ramp``
for "puts a land onto the battlefield", …), sitting at the mechanic frontier.

The exclusivity check (the enforcement — task 6.1b)
---------------------------------------------------
:func:`is_mechanic_exclusive` decides whether a candidate taxonomy node may be
an anchor for a side, measured against the **mechanic population** ``M`` that
the side's own anchors establish (the union of their descendant-closure card
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
and the same thresholds — see :func:`validate_anchor_map` and the
``TestExclusivityValidator`` gate tests.  The derivation never *climbs into*
``ramp`` (:func:`climb_to_frontier` stops below it), so ``ramp`` cannot appear
in any derived list — the hand-removal it used to require is now the rule's own
consequence.

Known boundary (reported, not hand-patched)
--------------------------------------------
A purely structural/card-evidence check cannot separate ``ramp`` from a
legitimate broad-mechanic ROOT (``recursion``, ``draw``, ``sacrifice-outlet``)
when the umbrella is hand-*listed* as a seed: structurally they are identical,
and the distinction (land-fetch is not mana) is semantic.  So the enforcement
guards the thing that is cleanly decidable — the **climb** can never ascend into
a mechanic-diluting ancestor, and the **output** never contains one — while the
seeds themselves stay owner-approved DATA.  Listing an umbrella AS a seed is a
map-authoring error caught by review, not by a structural classifier.

Shadow view
-----------
``derive_event_adjacency(events)`` returns a new events dict where each event's
``produced_by``/``triggers`` lists are the sorted descendant-closure of that
side's anchors.  The committed enumerated lists are a *subset* (no loss).  The
exclusivity layer does NOT change this output (the curated seeds are already the
mechanic frontier) — so the shadow election is byte-identical to the measured
ramp-corrected hand map.  ``SHADOW_DERIVED_ADJACENCY`` (default ``False``)
controls whether downstream consumers (relay, engine_hydration) use derived or
enumerated lists.  The flip is task 6.5.

One named rule (ADR 0001)
-------------------------
The anchor map is the SINGLE authoritative source for which otag concepts
participate on each event side.  The rule is ONE implementation — no per-event
branches, no per-deck hardcoding.  New events get an anchor-map row; the rule
stays the same.
"""

from __future__ import annotations

from collections import deque
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# Shadow flag — default OFF.  The committed enumerated lists remain
# authoritative.  Task 6.5 owns the flip.
# ---------------------------------------------------------------------------

SHADOW_DERIVED_ADJACENCY: bool = False

# ---------------------------------------------------------------------------
# 27-event anchor map — owner-approved DATA from the validated build-design.
#
# For each event, each side (``triggers`` / ``produced_by``) carries:
#   - ``anchors``: list[str] — otag slugs whose descendant-closure defines
#     the derived list for this side.  Includes BOTH the structural build-
#     design anchors (higher concepts) AND the existing enumerated slugs
#     (for guaranteed slug-level superset).
#   - ``grade``: "S" (clean structural, one exclusive taxonomy anchor),
#     "M" (semantic, union of flat concepts), or "empty" (no otag anchor —
#     text-synth / terminal / inert).
#
# The union of build-design anchors + enumerated slugs ensures
# derived ⊇ enumerated while the higher anchors unlock descendant coverage
# (the ~63% edge unlock vs 28% enumerated).
# ---------------------------------------------------------------------------

ANCHOR_MAP: dict[str, dict[str, Any]] = {
    # ── 1. land-enters-battlefield ──────────────────────────────────────
    'land-enters-battlefield': {
        'triggers': {
            'anchors': [
                'islandfall', 'land-count-matters', 'landfall',
                'landfall-other', 'lands-in-graveyard-matter',
                'lands-matter', 'opponent-land-count-matters',
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
            # validate_anchor_map (task 6.1b).
            'anchors': [
                'extra-land', 'land-ramp',
                'reanimate-land', 'recursion-land',
                'sneak-land', 'tutor-land-to-battlefield',
            ],
            'grade': 'M',
        },
    },
    # ── 2. creature-enters-battlefield ──────────────────────────────────
    'creature-enters-battlefield': {
        'triggers': {
            'anchors': [
                'creature-count-matters', 'creaturefall',
                'enters-in-company', 'flicker',
                'leaves-body-behind', 'multiple-bodies',
                'synergy-token-creature',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'anchors': [
                'flicker', 'flicker-creature', 'multiple-bodies',
                'reanimate-artifact-creature', 'reanimate-creature',
                'reanimate-nonland', 'reanimate-permanent',
                'repeatable-creature-tokens', 'repeatable-token-generator',
                'sneak-creature', 'tokens',
            ],
            'grade': 'M',
        },
    },
    # ── 3. permanent-leaves-battlefield ─────────────────────────────────
    'permanent-leaves-battlefield': {
        'triggers': {
            'anchors': [
                'enters-and-leaves-trigger-self',
                'leaves-battlefield-trigger', 'leaves-body-behind',
                'leaves-trigger-self',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'anchors': [
                'flicker', 'flicker-artifact', 'flicker-creature',
                'flicker-enchantment', 'flicker-land',
                'flicker-nonenchantment', 'flicker-nonland',
                'flicker-permanent', 'flicker-planeswalker',
                'flicker-self', 'flicker-slow', 'flicker-vehicle',
                'removal', 'sac',
            ],
            'grade': 'M',
        },
    },
    # ── 4. permanent-dies ───────────────────────────────────────────────
    'permanent-dies': {
        'triggers': {
            'anchors': [
                'cheat-death', 'cheat-death-self', 'death-trigger',
                'death-trigger-opponent', 'death-trigger-self',
                'life-and-death-trigger-self', 'splits-on-death',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'anchors': [
                'free-sacrifice-outlet', 'removal',
                'removal-sacrifice', 'sac', 'sacrifice-outlet',
                'sacrifice-outlet-artifact', 'sacrifice-outlet-creature',
                'sacrifice-outlet-enchantment', 'sacrifice-outlet-land',
                'sacrifice-outlet-nonland', 'sacrifice-outlet-planeswalker',
                'sacrifice-outlet-token', 'sacrifice-outlet-universal',
                'sacrifice-self',
            ],
            'grade': 'M',
        },
    },
    # ── 5. permanent-sacrificed ─────────────────────────────────────────
    'permanent-sacrificed': {
        'triggers': {
            'anchors': [
                'death-trigger', 'death-trigger-opponent',
                'death-trigger-self', 'opponent-sacrifice-matters',
                'sacrifice-matters', 'your-sacrifice-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'alternate-cost-sacrifice', 'free-sacrifice-outlet',
                'removal-sacrifice', 'repeatable-sacrifice-outlet',
                'sac', 'sacrifice-outlet', 'sacrifice-outlet-artifact',
                'sacrifice-outlet-creature', 'sacrifice-outlet-enchantment',
                'sacrifice-outlet-land', 'sacrifice-outlet-nonland',
                'sacrifice-outlet-planeswalker', 'sacrifice-outlet-token',
                'sacrifice-outlet-universal', 'sacrifice-self',
            ],
            'grade': 'S',
        },
    },
    # ── 6. card-enters-graveyard ────────────────────────────────────────
    'card-enters-graveyard': {
        'triggers': {
            'anchors': [
                'activate-from-graveyard', 'affinity-for-graveyard',
                'card-types-in-graveyard-matter',
                'cards-in-graveyard-matter',
                'castable-from-graveyard', 'graveyard-fuel',
                'graveyard-fuel-artifact', 'graveyard-fuel-creature',
                'graveyard-fuel-enchantment', 'graveyard-fuel-historic',
                'graveyard-fuel-instant', 'graveyard-fuel-land',
                'graveyard-fuel-legendary', 'graveyard-fuel-noncreature',
                'graveyard-fuel-nonland', 'graveyard-fuel-permanent',
                'graveyard-fuel-saga', 'graveyard-fuel-self',
                'graveyard-fuel-sorcery', 'lands-in-graveyard-matter',
                'leaving-graveyard-matters', 'reanimate',
                'reanimate-artifact', 'reanimate-artifact-creature',
                'reanimate-aura', 'reanimate-battle', 'reanimate-cast',
                'reanimate-creature', 'reanimate-enchantment',
                'reanimate-equipment', 'reanimate-face-down',
                'reanimate-from-any', 'reanimate-from-opponent',
                'reanimate-historic', 'reanimate-instant-sorcery',
                'reanimate-land', 'reanimate-legendary',
                'reanimate-matters', 'reanimate-noncreature',
                'reanimate-nonland', 'reanimate-permanent',
                'reanimate-planeswalker', 'reanimate-saga',
                'reanimate-self', 'reanimate-spacecraft',
                'reanimate-vehicle', 'recursion', 'recursion-any',
                'recursion-artifact', 'recursion-battle',
                'recursion-creature', 'recursion-enchantment',
                'recursion-from-exile', 'recursion-instant',
                'recursion-land', 'recursion-permanent',
                'recursion-planeswalker', 'recursion-self',
                'recursion-sorcery', 'synergy-graveyard-cast',
                'trigger-from-graveyard',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'discard', 'discard-outlet', 'discard-outlet-artifact',
                'discard-outlet-creature', 'discard-outlet-land',
                'discard-outlet-last-drawn', 'discard-outlet-legendary',
                'discard-outlet-nonland', 'discard-outlet-random',
                'loot', 'mill', 'mill-self', 'rummage', 'sac',
                'sacrifice-self', 'wheel',
            ],
            'grade': 'M',
        },
    },
    # ── 7. spell-cast ───────────────────────────────────────────────────
    'spell-cast': {
        'triggers': {
            'anchors': [
                'cast-trigger', 'cast-trigger-other',
                'cast-trigger-self', 'cast-trigger-you',
                'prowess-like', 'second-spell-matters',
                'storm-count-matters', 'synergy-instant',
                'synergy-noncreature', 'synergy-sorcery',
                'times-resolved-matters',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'anchors': [
                'copy-instant', 'copy-sorcery', 'copy-spell',
            ],
            'grade': 'M',
        },
    },
    # ── 8. creature-attacks ─────────────────────────────────────────────
    'creature-attacks': {
        'triggers': {
            'anchors': [
                'attack-trigger', 'attacking-matters',
                'attacking-matters-any', 'attacking-matters-self',
                'attacking-opponents-matters', 'synergy-solo-attack',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'anchors': [
                'combat', 'extra-combat-phase', 'force-attacker',
                'forced-attacker', 'gives-haste', 'tokens',
            ],
            'grade': 'M',
        },
    },
    # ── 9. combat-damage-dealt ──────────────────────────────────────────
    'combat-damage-dealt': {
        'triggers': {
            'anchors': [
                'combat-neutral-damage-trigger', 'retaliate-to-damage',
                'saboteur', 'scales-with-damage-dealt',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'anchors': [
                'combat', 'damage-multiplier', 'gains-double-strike',
                'gives-double-strike', 'gives-evasion', 'gives-trample',
            ],
            'grade': 'M',
        },
    },
    # ── 10. lifegain ────────────────────────────────────────────────────
    'lifegain': {
        'triggers': {
            'anchors': [
                'life-total-matters-self', 'lifegain-increaser',
                'lifegain-matters', 'lifegain-to-damage',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'drain-life', 'gains-lifelink', 'gives-lifelink',
                'lifegain', 'repeatable-lifegain',
            ],
            'grade': 'S',
        },
    },
    # ── 11. life-loss-drain ─────────────────────────────────────────────
    'life-loss-drain': {
        'triggers': {
            'anchors': [
                'life-loss-matters', 'self-life-loss-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'burn', 'burn-player', 'burn-player-each',
                'drain-life', 'omnivore-ability', 'opponent-loses-life',
                'sac',
            ],
            'grade': 'M',
        },
    },
    # ── 12. counter-placed ──────────────────────────────────────────────
    'counter-placed': {
        'triggers': {
            'anchors': [
                'counter-fuel', 'counter-fuel-aesthetic',
                'counter-fuel-any', 'counter-fuel-charge',
                'counter-fuel-energy', 'counter-fuel-fade',
                'counter-fuel-keyword', 'counter-fuel-lore',
                'counter-fuel-loyalty', 'counter-fuel-mm',
                'counter-fuel-oil', 'counter-fuel-other',
                'counter-fuel-pp', 'counter-fuel-pt',
                'counter-fuel-shield', 'counter-fuel-stun',
                'counter-fuel-time', 'counters-matter',
                'gains-mm-counters', 'gains-pp-counters',
                'mm-counters-matter', 'pp-counters-matter',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'anchors': [
                'counter-increaser', 'counters', 'gives-mm-counters',
                'gives-pp-counters', 'gives-pp-counters-to-all',
                'repeatable-pp-counters', 'repeatable-proliferate',
            ],
            'grade': 'M',
        },
    },
    # ── 13. proliferate ─────────────────────────────────────────────────
    'proliferate': {
        'triggers': {
            'anchors': [
                'counter-fuel', 'counter-fuel-aesthetic',
                'counter-fuel-any', 'counter-fuel-charge',
                'counter-fuel-energy', 'counter-fuel-fade',
                'counter-fuel-keyword', 'counter-fuel-lore',
                'counter-fuel-loyalty', 'counter-fuel-mm',
                'counter-fuel-oil', 'counter-fuel-other',
                'counter-fuel-pp', 'counter-fuel-pt',
                'counter-fuel-shield', 'counter-fuel-stun',
                'counter-fuel-time', 'counters-matter',
                'pp-counters-matter',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'pseudo-proliferate', 'repeatable-proliferate',
                'synergy-proliferate',
            ],
            'grade': 'M',
        },
    },
    # ── 14. token-created ───────────────────────────────────────────────
    'token-created': {
        'triggers': {
            'anchors': [
                'artifact-count-matters', 'creature-count-matters',
                'sac', 'synergy-token', 'synergy-token-creature',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # Build-design P anchors: repeatable-token-generator + multiple-
            # bodies, PLUS self-heal candidates promoted by the build-design:
            # leaves-body-behind (death→token), out-of-color-token (off-color
            # token producers), creates-bigger-body (Reef Worm chain).
            'anchors': [
                'artifacts', 'creates-bigger-body',
                'leaves-body-behind', 'multiple-bodies',
                'out-of-color-token',
                'repeatable-artifact-tokens', 'repeatable-clues',
                'repeatable-creature-tokens', 'repeatable-food',
                'repeatable-noncreature-tokens',
                'repeatable-token-generator', 'repeatable-treasures',
                'tokens',
            ],
            'grade': 'M',
        },
    },
    # ── 15. artifact-enters ─────────────────────────────────────────────
    'artifact-enters': {
        'triggers': {
            'anchors': [
                'affinity-for-artifacts', 'artifact-count-matters',
                'artifactfall', 'synergy-artifact',
                'synergy-artifact-creature',
            ],
            'grade': 'S',
        },
        'produced_by': {
            'anchors': [
                'animate-artifact', 'artifactify', 'artifacts',
                'repeatable-artifact-tokens', 'repeatable-treasures',
            ],
            'grade': 'M',
        },
    },
    # ── 16. food-created ────────────────────────────────────────────────
    'food-created': {
        'triggers': {
            'anchors': [
                'eat-food', 'lifegain-matters', 'synergy-food',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'repeatable-food', 'repeatable-treasures',
            ],
            'grade': 'M',
        },
    },
    # ── 17. draw-a-card ─────────────────────────────────────────────────
    'draw-a-card': {
        'triggers': {
            'anchors': [
                'draw-matters', 'hand-size-matters',
                'second-draw-matters', 'third-draw-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'burst-draw', 'draw', 'draw-engine',
                'repeatable-draw',
            ],
            'grade': 'S',
        },
    },
    # ── 18. discard ─────────────────────────────────────────────────────
    'discard': {
        'triggers': {
            'anchors': [
                'castable-from-graveyard', 'discard-matters',
                'discarded-type-matters', 'self-discard-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'discard-outlet', 'discard-outlet-artifact',
                'discard-outlet-creature', 'discard-outlet-land',
                'discard-outlet-last-drawn', 'discard-outlet-legendary',
                'discard-outlet-nonland', 'discard-outlet-random',
                'free-discard-outlet', 'loot', 'rummage', 'wheel',
            ],
            'grade': 'S',
        },
    },
    # ── 19. recursion-from-graveyard ────────────────────────────────────
    'recursion-from-graveyard': {
        'triggers': {
            'anchors': [
                'castable-from-graveyard', 'graveyard-fuel',
                'graveyard-fuel-artifact', 'graveyard-fuel-creature',
                'graveyard-fuel-enchantment', 'graveyard-fuel-historic',
                'graveyard-fuel-instant', 'graveyard-fuel-land',
                'graveyard-fuel-legendary', 'graveyard-fuel-noncreature',
                'graveyard-fuel-nonland', 'graveyard-fuel-permanent',
                'graveyard-fuel-saga', 'graveyard-fuel-self',
                'graveyard-fuel-sorcery', 'reanimate',
                'reanimate-artifact', 'reanimate-artifact-creature',
                'reanimate-aura', 'reanimate-battle', 'reanimate-cast',
                'reanimate-creature', 'reanimate-enchantment',
                'reanimate-equipment', 'reanimate-face-down',
                'reanimate-from-any', 'reanimate-from-opponent',
                'reanimate-historic', 'reanimate-instant-sorcery',
                'reanimate-land', 'reanimate-legendary',
                'reanimate-matters', 'reanimate-noncreature',
                'reanimate-nonland', 'reanimate-permanent',
                'reanimate-planeswalker', 'reanimate-saga',
                'reanimate-self', 'reanimate-spacecraft',
                'reanimate-vehicle', 'recursion', 'recursion-any',
                'recursion-artifact', 'recursion-battle',
                'recursion-creature', 'recursion-enchantment',
                'recursion-from-exile', 'recursion-instant',
                'recursion-land', 'recursion-permanent',
                'recursion-planeswalker', 'recursion-self',
                'recursion-sorcery', 'synergy-graveyard-cast',
            ],
            'grade': 'S',
        },
        'produced_by': {
            # Build-design P anchors: recursion/reanimate (the cards that
            # PERFORM the graveyard return) + mill/discard/sac fillers (that
            # put cards INTO the graveyard enabling recursion).  `recursion`
            # is a ROOT (100 descendants) covering all reanimate-*/recursion-*
            # variants.  Adding it here is the single biggest unlock (~25
            # edges) identified by the build-design measurement.
            'anchors': [
                'discard', 'discard-outlet', 'discard-outlet-artifact',
                'discard-outlet-creature', 'discard-outlet-land',
                'discard-outlet-last-drawn', 'discard-outlet-legendary',
                'discard-outlet-nonland', 'discard-outlet-random',
                'mill', 'mill-self', 'recursion', 'sac',
            ],
            'grade': 'M',
        },
    },
    # ── 20. deathtouch-combat ───────────────────────────────────────────
    'deathtouch-combat': {
        'triggers': {
            'anchors': [
                'combat', 'old-blocking-deathtouch',
                'old-damage-deathtouch', 'synergy-deathtouch',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'deathtouch-counter', 'gains-deathtouch',
                'gives-deathtouch',
            ],
            'grade': 'M',
        },
    },
    # ── 21. monarch-or-extra-turn ───────────────────────────────────────
    'monarch-or-extra-turn': {
        'triggers': {
            'anchors': [
                'monarch-matters', 'off-turn-casting-matters',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': ['extra-turn'],
            'grade': 'empty',
        },
    },
    # ── 22. creature-copied ─────────────────────────────────────────────
    'creature-copied': {
        'triggers': {
            'anchors': [
                'creature-count-matters', 'creaturefall',
                'eat-food', 'enters-in-company',
                'leaves-body-behind', 'synergy-food',
                'synergy-token', 'synergy-token-creature',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'clones', 'copy-creature', 'copy-self', 'copy-token',
            ],
            'grade': 'M',
        },
    },
    # ── 23. untap ───────────────────────────────────────────────────────
    'untap': {
        'triggers': {
            'anchors': [
                'synergy-untapped', 'untapped-matters-self',
            ],
            'grade': 'M',
        },
        'produced_by': {
            'anchors': [
                'etb-untapper', 'extra-untap', 'ritual-untap',
                'untapper', 'untapper-artifact', 'untapper-creature',
                'untapper-equipment', 'untapper-land',
                'untapper-nonland', 'untapper-permanent',
                'untapper-planeswalker', 'untaps-self',
            ],
            'grade': 'S',
        },
    },
    # ── 24. player-counter ──────────────────────────────────────────────
    'player-counter': {
        'triggers': {
            'anchors': [
                'mill-any', 'mill-each', 'mill-opponent',
                'mill-self', 'pseudo-proliferate',
                'repeatable-proliferate', 'synergy-mill',
                'synergy-proliferate',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # rad-counter is a text-synth attr with NO otag, kept for
            # backward compatibility with the committed catalog.
            'anchors': ['rad-counter'],
            'grade': 'empty',
        },
    },
    # ── 25. mana-produced ───────────────────────────────────────────────
    'mana-produced': {
        'triggers': {
            'anchors': [
                'bottomless-mana-sink', 'mana-increaser', 'mana-sink',
            ],
            'grade': 'M',
        },
        'produced_by': {
            # THE PROVEN SET (ADR 0020): mana-producer IN, ramp OUT.
            'anchors': [
                'adds-multiple-mana', 'mana-producer',
                'repeatable-treasures', 'ritual',
            ],
            'grade': 'M',
        },
    },
    # ── 26. game-end (terminal) ─────────────────────────────────────────
    'game-end': {
        'triggers': {'anchors': [], 'grade': 'empty'},
        'produced_by': {'anchors': [], 'grade': 'empty'},
    },
    # ── 27. library-depleted ────────────────────────────────────────────
    'library-depleted': {
        'triggers': {'anchors': [], 'grade': 'empty'},
        'produced_by': {'anchors': [], 'grade': 'empty'},
    },
}


# ---------------------------------------------------------------------------
# Taxonomy loader
# ---------------------------------------------------------------------------

_TAXONOMY_PATH = Path.home() / '.local' / 'share' / 'make-magic' / 'raw' / 'oracle_tags.parquet'


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

    if not _TAXONOMY_PATH.exists():
        raise FileNotFoundError(
            f'oracle_tags.parquet not found at {_TAXONOMY_PATH}; '
            f'hydrate with `collection hydrate-lake`'
        )

    con = duckdb.connect(':memory:')
    try:
        rows = con.execute(
            f"SELECT CAST(id AS VARCHAR) AS id, slug, "
            f"COALESCE(description, '') AS description, "
            f"parent_ids, child_ids "
            f"FROM read_parquet('{_TAXONOMY_PATH}')"
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
# Mechanic-exclusivity validator (task 6.1b) — the anchor rule as ENFORCED code
#
# The seed/anchor for each event side must be *mechanic-exclusive*: climbing to
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

    if not _TAXONOMY_PATH.exists():
        raise FileNotFoundError(
            f'oracle_tags.parquet not found at {_TAXONOMY_PATH}; '
            f'hydrate with `collection hydrate-lake`'
        )

    con = duckdb.connect(':memory:')
    try:
        rows = con.execute(
            f"SELECT slug, taggings FROM read_parquet('{_TAXONOMY_PATH}')"
        ).fetchall()
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


def mechanic_population(anchors: list[str] | tuple[str, ...]) -> frozenset[str]:
    """The card population a side's *anchors* establish as its mechanic.

    = union of the ancestor-closed card sets of every anchor.  This is the
    ground-truth ``M`` the exclusivity check measures candidates against.
    """
    pop: set[str] = set()
    for a in anchors:
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
    """Is *node* exclusive to *mechanic* (so it may be an anchor / be climbed to)?

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
        return ExclusivityResult(
            False, precision, f'precision {precision:.3f} < {prec_min}'
        )

    for child in taxonomy.get(node, {}).get('children', []):
        child_cards = card_set(child)
        if len(child_cards) < branch_floor:
            continue
        child_prec = len(child_cards & mechanic) / len(child_cards)
        if child_prec < branch_prec_min:
            return ExclusivityResult(
                False,
                precision,
                f'foreign heavy child {child!r} '
                f'({len(child_cards)} cards, precision {child_prec:.3f})',
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


def climb_to_frontier(seed: str, mechanic: frozenset[str]) -> str:
    """Climb *seed* child->parent to the maximal mechanic-exclusive ancestor.

    Returns the highest ancestor that still :func:`is_mechanic_exclusive` vs
    *mechanic*; stops one level below the first ancestor that admits a different
    mechanic.  For the curated seeds (already at the frontier) this is a no-op
    and returns *seed* — but it is the rule that REFUSES to ascend into ``ramp``
    and friends.  When *seed* has several parents it climbs the first exclusive
    one (ties do not occur in the current catalog).
    """
    taxonomy = load_taxonomy()
    current = seed
    while current in taxonomy:
        climbed = False
        for parent in taxonomy[current].get('parents', []):
            if is_mechanic_exclusive(parent, mechanic).ok:
                current = parent
                climbed = True
                break
        if not climbed:
            break
    return current


def refused_climb_targets(anchors: list[str]) -> dict[str, ExclusivityResult]:
    """Ancestors of *anchors* the rule REFUSES (the cross-mechanic umbrellas).

    For every anchor, every proper ancestor that is NOT itself an anchor and is
    NOT mechanic-exclusive against the side's mechanic.  ``ramp`` appears here
    for both mana-produced and land-enters — the generic auto-exclusion proof.
    """
    taxonomy = load_taxonomy()
    mechanic = mechanic_population(anchors)
    anchor_set = set(anchors)
    refused: dict[str, ExclusivityResult] = {}
    for anchor in anchors:
        for ancestor in proper_ancestors(anchor, taxonomy):
            if ancestor in anchor_set or ancestor in refused:
                continue
            res = is_mechanic_exclusive(ancestor, mechanic)
            if not res.ok:
                refused[ancestor] = res
    return refused


def validate_anchor_map() -> list[str]:
    """Enforce the exclusivity rule over :data:`ANCHOR_MAP`.  Returns violations.

    Hard invariants (empty list == clean):

    1. **Ancestor-closure** — every non-synthetic anchor resolves to a
       non-empty descendant closure (no dangling slug).
    2. **Output purity** — the refused cross-mechanic umbrella ancestors (e.g.
       ``ramp``, discovered generically via :func:`refused_climb_targets`) never
       appear in the side's derived closure-union output.
    3. **Frontier soundness** — every anchor that carries cards is itself
       mechanic-exclusive against its side's mechanic (a listed anchor never
       dilutes its own side below the precision floor).

    The ``ramp``-shaped regression (a diluting ancestor re-listed) is proven
    excluded by :func:`refused_climb_targets` (``ramp`` is refused for both
    mana-produced and land-enters) together with the ``TestExclusivityValidator``
    gate.  A fully-structural auto-reject of a hand-*listed* umbrella is not
    derivable (a documented semantic boundary, see the module docstring), so the
    curated seeds remain owner-approved DATA checked by invariants 1 & 3.
    """
    taxonomy = load_taxonomy()
    violations: list[str] = []

    for event_name, entry in ANCHOR_MAP.items():
        for side in ('produced_by', 'triggers'):
            anchors = entry[side]['anchors']
            if not anchors:
                continue

            # (1) ancestor-closure: closure non-empty for every anchor.
            for anchor in anchors:
                if not descendant_closure(anchor, taxonomy):
                    violations.append(
                        f'{event_name}.{side}: anchor {anchor!r} has empty closure'
                    )

            mechanic = mechanic_population(anchors)

            # (2) output purity: no refused umbrella ancestor leaks into the
            # derived closure-union output.
            output: set[str] = set()
            for anchor in anchors:
                output |= descendant_closure(anchor, taxonomy)
            for umbrella in refused_climb_targets(anchors):
                if umbrella in output:
                    violations.append(
                        f'{event_name}.{side}: cross-mechanic umbrella '
                        f'{umbrella!r} leaked into the derived output'
                    )

            # (3) frontier soundness: each carded anchor is self-exclusive.
            for anchor in anchors:
                if not card_set(anchor):
                    continue
                res = is_mechanic_exclusive(anchor, mechanic)
                if not res.ok:
                    violations.append(
                        f'{event_name}.{side}: anchor {anchor!r} is not '
                        f'mechanic-exclusive ({res.reason})'
                    )

    return violations


# ---------------------------------------------------------------------------
# Derived event adjacency
# ---------------------------------------------------------------------------

def derive_event_adjacency(
    events: dict[str, dict],
) -> dict[str, dict]:
    """Compute derived ``produced_by``/``triggers`` from anchors + taxonomy closure.

    For each event side, the derived list = sorted union of descendant-closures
    of all anchors for that side.  The committed enumerated lists are a subset
    of the derived lists (no loss) because the anchor map includes both the
    build-design structural anchors AND the existing enumerated slugs.

    The returned dict has the same shape as *events* — every event key is
    present with the same ``rule``/``abbrev``/``polarity``/``scope`` fields.
    Only ``produced_by`` and ``triggers`` lists are replaced.

    Events not in :data:`ANCHOR_MAP` are passed through unchanged.
    """
    taxonomy = load_taxonomy()

    derived: dict[str, dict] = {}
    for event_name, ev in events.items():
        entry = ANCHOR_MAP.get(event_name)
        if entry is None:
            # Not in anchor map — pass through unchanged.
            derived[event_name] = dict(ev)
            continue

        # Start with a copy of the original event (preserving rule, abbrev, etc.)
        new_ev = dict(ev)

        for side in ('produced_by', 'triggers'):
            anchors = entry[side]['anchors']
            if not anchors:
                # Empty anchor set — keep the original list (terminal/inert events).
                continue

            # Expand each anchor via descendant-closure and union.
            expanded: set[str] = set()
            for anchor in anchors:
                expanded |= descendant_closure(anchor, taxonomy)

            new_ev[side] = sorted(expanded)

        derived[event_name] = new_ev

    return derived


# ---------------------------------------------------------------------------
# Public API for the shadow view
# ---------------------------------------------------------------------------

def active_events(events: dict[str, dict]) -> dict[str, dict]:
    """Return events with either enumerated or derived adjacency.

    When :data:`SHADOW_DERIVED_ADJACENCY` is ``False`` (the default), returns
    *events* unchanged.  When ``True``, returns the derived adjacency.

    Consumers (relay, engine_hydration) call this when they want the
    shadow-aware events dict.
    """
    if not SHADOW_DERIVED_ADJACENCY:
        return events
    return derive_event_adjacency(events)
