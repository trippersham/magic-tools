"""Anchor-based event adjacency derivation (Phase 6.1 + 6.2).

Implements the **Maximal mechanic-exclusive anchor** rule as production code
(6.1) and the **derived ``produced_by``/``triggers``** as a shadow view behind a
flag (6.2).

The rule
--------
Given an event side (produce/trigger), each ANCHOR is the **highest ancestor**
in the otag taxonomy whose descendant-closure stays *mechanic-exclusive* — stop
one level below any ancestor that admits a different mechanic.  The 27-event
anchor map below was derived by the build-design measurement pass
(``research/adjacency-build-design.md``) and is reused here as DATA.

Shadow view
-----------
``derive_event_adjacency(events)`` returns a new events dict where each event's
``produced_by``/``triggers`` lists are expanded via the anchors' descendant-
closure in the taxonomy.  The committed enumerated lists are a *subset* of the
derived lists (no loss).  ``SHADOW_DERIVED_ADJACENCY`` (default ``False``)
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
from functools import lru_cache
from pathlib import Path
from typing import Any

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
            'anchors': [
                'extra-land', 'land-ramp', 'ramp',
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
