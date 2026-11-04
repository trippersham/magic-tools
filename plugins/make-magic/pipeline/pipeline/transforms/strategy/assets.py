"""Frozen vocabulary + event catalog + routing — the engine's committed data.

Loads the promoted data files (``data/attribute_vocab_v4.json``,
``data/event_catalog_v4.json``) and assembles, as a function of a
:class:`~.config.StrategyConfig`, every derived constant the facts/naming layers
need: the bucket set, payoff/support vocab, density thresholds, event anchors,
the T5.5 EDHREC new-node families, and the deterministic
``attr → identity archetype`` router.

``load_assets(cfg)`` is memoized per config so repeated ``resolve`` calls reuse
one immutable :class:`StrategyAssets`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pipeline.transforms.strategy.config import DEFAULT_CONFIG, REFINED_GROUPS, StrategyConfig

DATA_DIR = Path(__file__).resolve().parent / 'data'
VOCAB_FILE = DATA_DIR / 'attribute_vocab_v4.json'
EVENTS_FILE = DATA_DIR / 'event_catalog_v4.json'
#: The T5.5 EDHREC NEW-NODE nameable label space. SINGLE SOURCE OF TRUTH: the
#: set is loaded from the committed ``labelspace.json`` (data-authoritative) so
#: the code and the realized label space can never silently diverge.
LABELSPACE_FILE = DATA_DIR / 'labelspace.json'

# --- static control vocab (independent of config) ---------------------------
_CAST_CHILDREN = frozenset({
    'cast-trigger-you', 'cast-trigger-self', 'cast-trigger-other',
    'second-spell-matters', 'storm-count-matters', 'prowess-like',
})
_ENCH_ATTR_RE = re.compile(r'(enchant|aura|constellation|shrine|saga)')

# T5.5 EDHREC NEW-NODE otag families (all existing in-lake slugs).
_MILL_OPP_SLUGS = frozenset({'mill-opponent', 'mill-any', 'synergy-mill'})
_PW_SLUGS = frozenset({'synergy-planeswalker', 'recursion-planeswalker', 'protects-planeswalker', 'superfriends'})
_DISCARD_SLUGS = frozenset({'discard-matters', 'self-discard-matters', 'wheel', 'wheel-one-sided',
                            'wheel-symmetrical', 'wheel-symmetrical-optional', 'miniwheel'})
_TREASURE_SLUGS = frozenset({'repeatable-treasures', 'synergy-treasure', 'repeatable-clues', 'synergy-clue',
                             'repeatable-blood', 'synergy-blood', 'repeatable-food', 'synergy-food', 'eat-food',
                             'food'})
_VEHICLE_SLUGS = frozenset({'synergy-vehicle', 'crew', 'bring-your-own-crew', 'alternative-crewing',
                            'animate-vehicle', 'crewless-vehicle'})
_ENERGY_SLUGS = frozenset({'energy-generator', 'counter-fuel-energy', 'energy-increaser', 'synergy-energy'})
_EXTRATURN_SLUGS = frozenset({'extra-turn', 'extra-upkeep'})
_POISON_SLUGS = frozenset({'synergy-poison', 'poison-opponents', 'poison-mechanics', 'poisonous',
                           'synergy-toxic', 'synergy-infect'})
_THEFT_WIDE = frozenset({'theft', 'theft-creature', 'theft-artifact', 'theft-land', 'theft-planeswalker',
                         'theft-spell', 'theft-cast', 'nightveil-theft'})
EDHREC_ATTR_SLUGS = (_MILL_OPP_SLUGS | _PW_SLUGS | _DISCARD_SLUGS | _TREASURE_SLUGS
                     | _VEHICLE_SLUGS | _ENERGY_SLUGS | _EXTRATURN_SLUGS | _POISON_SLUGS | _THEFT_WIDE)
EDHREC_NEW_LABELS: frozenset[str] = frozenset(
    json.loads(LABELSPACE_FILE.read_text())['new_nameable_labels_added']
)
_ENCH_IDENTITY = frozenset({
    'synergy-enchantment', 'synergy-aura', 'synergy-shrine', 'synergy-enchantment-creature',
    'enchantmentfall', 'enchantment-engine', 'constellation', 'repeatable-enchantment-tokens',
    'repeatable-aura-tokens', 'affinity-for-enchantments', 'creature-type-enchantress',
    'quick-enchant', 'auto-attach', 'cast-enchantment-trigger', 'enchantment-cost-reducer',
})

# T5 correction B: directed hate-* answers map (FACTS only; scoring deferred).
ANSWERS_MAP: dict[str, list[str]] = {
    'graveyard/reanimator': ['hate-graveyard', 'single-minded-graveyard-hate',
                             'hate-graveyard-cast', 'sweeper-graveyard'],
    'artifacts': ['hate-artifact', 'hate-artifact-creature', 'counterspell-artifact'],
    'enchantments': ['hate-enchantment', 'hate-aura', 'counterspell-enchantment'],
    'voltron/equipment': ['hate-aura', 'hate-enchantment'],
    'lands-matter': ['mass-land-denial', 'hate-nonbasic-land'],
    'tokens/go-wide': ['sweeper', 'sweeper-one-sided', 'hate-token'],
    'anthem/go-wide': ['sweeper', 'sweeper-one-sided', 'hate-token'],
    'aristocrats/sacrifice': ['sweeper', 'sweeper-one-sided'],
    'typal/tribal': ['hate-typal', 'sweeper', 'sweeper-one-sided'],
    'control/permission': ['hate-counterspell'],
    'theft': ['hate-theft'],
    '+1/+1 counters': ['hate-counters'],
    'lifegain': ['hate-lifegain'],
    'spellslinger': ['hate-storm'],
}
ANSWERS_MAP_ALL_SLUGS = frozenset(s for sl in ANSWERS_MAP.values() for s in sl)


def is_graveyard_attr(a: str) -> bool:
    return (a == 'graveyard' or a.startswith(('recursion', 'reanimate')) or a in (
        'cards-in-graveyard-matter', 'castable-from-graveyard', 'graveyard-fuel',
        'graveyard-fuel-self', 'activate-from-graveyard', 'mill-self'))


def is_land(card: dict) -> bool:
    tl = card.get('type_line') or ''
    return 'Land' in tl and 'Creature' not in tl


@dataclass(frozen=True)
class StrategyAssets:
    """Immutable, config-bound bundle of the engine's committed vocab + routing."""

    cfg: StrategyConfig
    buckets: dict[str, list[str]]
    bucket_names: frozenset[str]
    payoff_slugs: frozenset[str]
    support: frozenset[str]
    density: dict
    events: dict
    event_anchors: frozenset[str]
    tutor_land_slugs: frozenset[str]
    new_bucket_names: frozenset[str]
    arch_newbuckets: bool
    arch_anthem_deconf: bool
    food_set: frozenset[str]
    mm_set: frozenset[str]
    theft_set: frozenset[str]
    anthem_deconf_set: frozenset[str]
    spell_re: re.Pattern
    art_re: re.Pattern
    ench_re: re.Pattern
    spell_t: int
    art_t: int
    sub_t: int
    cast_children: frozenset[str] = _CAST_CHILDREN
    ench_attr_re: re.Pattern = _ENCH_ATTR_RE

    # ---------------------------------------------------- routing (T5.5 + v4)
    def edhrec_route(self, a: str) -> str | None:
        """Tier-1 EDHREC NEW-NODE router, checked before the coarse v4 router."""
        if a == 'devotion-signal':
            return 'devotion'
        if a in _VEHICLE_SLUGS or a == 'vehicle-type':
            return 'vehicles'
        if a in _ENERGY_SLUGS or a == 'energy-text':
            return 'energy'
        if a in _EXTRATURN_SLUGS or a == 'extra-turn-text':
            return 'extra-turns'
        if a in _MILL_OPP_SLUGS:
            return 'mill'
        if a in _PW_SLUGS:
            return 'planeswalkers'
        if a in _DISCARD_SLUGS:
            return 'discard/wheels'
        if a in _TREASURE_SLUGS:
            return 'treasure/food'
        if a in _THEFT_WIDE:
            return 'theft'
        return None

    def archetype_of_attr(self, a: str) -> str | None:
        """Deterministic attr → identity archetype router (v4 surface + T5.5)."""
        if self.cfg.edhrec_labels:
            r = self.edhrec_route(a)
            if r is not None:
                return r
            if a in _POISON_SLUGS:
                return '-1/-1 counters'
            if a in _ENCH_IDENTITY:
                return 'enchantress'
        if self.arch_newbuckets:
            if a in self.food_set:
                return 'food'
            if a in self.mm_set:
                return '-1/-1 counters'
            if a in self.theft_set:
                return 'theft'
        if self.arch_anthem_deconf and a in self.anthem_deconf_set:
            return 'anthem/go-wide'
        if a.startswith('gives-'):
            return None  # granted keyword = payload mechanic, not an identity
        if a.startswith('typal') or a == 'changeling':
            return 'typal/tribal'
        if a in ('tokens', 'token-increaser') or a.startswith(('repeatable-token', 'repeatable-creature-token',
                                                               'repeatable-noncreature-token', 'synergy-token')):
            return 'tokens/go-wide'
        if a == 'counters' or 'pp-counters' in a or a in ('counters-matter', 'counter-increaser') or a == 'proliferate':
            return '+1/+1 counters'
        if a == 'sac' or a.startswith('sacrifice-outlet') or a in ('your-sacrifice-matters', 'sacrifice-matters',
                                                                    'death-trigger', 'death-trigger-self',
                                                                    'free-sacrifice-outlet', 'sacrifice-self'):
            return 'aristocrats/sacrifice'
        if a in ('landfall', 'lands-matter', 'land-count-matters', 'extra-land') or a in ('recursion-land',
                                                                                          'reanimate-land'):
            return 'lands-matter'
        if a == 'graveyard' or a.startswith(('recursion', 'reanimate')) or a in (
                'cards-in-graveyard-matter', 'castable-from-graveyard', 'graveyard-fuel', 'graveyard-fuel-self',
                'activate-from-graveyard', 'mill-self'):
            return 'graveyard/reanimator'
        if a == 'spellslinger' or a.startswith('cast-trigger') or a in (
                'second-spell-matters', 'storm-count-matters', 'synergy-instant', 'synergy-sorcery',
                'synergy-noncreature', 'prowess-like', 'copy-instant', 'copy-sorcery'):
            return 'spellslinger'
        if a == 'artifacts' or a.startswith('synergy-artifact') or a in ('artifact-count-matters',
                                                                         'affinity-for-artifacts',
                                                                         'repeatable-treasures', 'artifactfall'):
            return 'artifacts'
        if a == 'flicker' or a.startswith('flicker'):
            return 'blink/flicker'
        if a in ('equipment', 'auras') or a.startswith(('synergy-equipment', 'synergy-aura')) or a in (
                'quick-equip', 'auto-equip'):
            return 'voltron/equipment'
        if a == 'lifegain' or a.startswith('lifegain') or a in ('repeatable-lifegain', 'gives-lifelink'):
            return 'lifegain'
        if a == 'stax' or a.startswith('tax') or a == 'cast-tax':
            return 'stax/prison'
        if a in ('deathtouch-combat',) or 'deathtouch' in a:
            return 'deathtouch'
        if a in ('counterspells', 'counterspell'):
            return 'control/permission'
        if a == 'anthem' or a in ('power-boost-to-all', 'toughness-boost-to-all'):
            return 'anthem/go-wide'
        if a in ('extra_combat', 'extra-combat-phase'):
            return 'extra-combat'
        if a == 'burn' or a in ('burn-player', 'opponent-loses-life', 'drain-life'):
            return 'burn'
        if a == 'combat' or a.startswith('attacking-matters') or a in ('attack-trigger',):
            return 'combat/aggro'
        if a in ('wincon', 'alternate-win-condition'):
            return 'alt-wincon/combo'
        if self.ench_attr_re.search(a):
            return 'enchantments'
        return None


def _enabled_groups(cfg: StrategyConfig, all_groups: frozenset[str]) -> set[str]:
    g = set(cfg.enabled_groups)
    if 'refined' in g:  # allow a "refined" sentinel to expand
        g.discard('refined')
        g |= set(REFINED_GROUPS)
    return g & all_groups


@cache
def _load_assets_cached(cfg: StrategyConfig) -> StrategyAssets:
    vocab = json.loads(VOCAB_FILE.read_text())
    base_buckets: dict[str, list[str]] = vocab['_v4_base_buckets']
    v4_groups: dict[str, dict[str, list[str]]] = vocab['_v4_groups']
    new_bucket_names = frozenset(vocab['_v4_new_bucket_names'])

    enabled = _enabled_groups(cfg, frozenset(v4_groups))

    buckets: dict[str, list[str]] = {b: list(sl) for b, sl in base_buckets.items()}
    for g in enabled:
        for b, slugs in v4_groups.get(g, {}).items():
            buckets.setdefault(b, [])
            for s in slugs:
                if s not in buckets[b]:
                    buckets[b].append(s)
    buckets = {b: sl for b, sl in buckets.items() if sl or b not in new_bucket_names}

    # narrow go-tall anthem payoff augmentation (deterministic)
    if 'anthem' in buckets:
        for s in sorted(cfg.anthem_payoff):
            if s not in buckets['anthem']:
                buckets['anthem'].append(s)

    routing = vocab['_v4_archetype_routing']
    food_set = frozenset(routing['_anchor_food']) | {'food'}
    mm_set = frozenset(routing['_anchor_mm']) | {'mm_counters'}
    theft_set = frozenset(routing['_anchor_theft']) | {'theft'}
    anthem_deconf_set = frozenset(routing['_anchor_anthem_deconf'])

    events = json.loads(EVENTS_FILE.read_text())['events']
    # tutor-land producer injection (idempotent on v4, matches the prototype's
    # "dual"/"dual_land"-gated widening of the landfall event producers)
    if 'dual' in enabled or 'dual_land' in enabled:
        le = events['land-enters-battlefield']
        if 'tutor-land-to-battlefield' not in le['produced_by']:
            le['produced_by'] = sorted(set(le['produced_by']) | {'tutor-land-to-battlefield'})

    support = frozenset(vocab['support_buckets'])
    density = vocab['ranking_only_density']
    bucket_names = frozenset(buckets)
    payoff_slugs = frozenset(vocab['payoff_attributes'].keys())

    tutor_land_slugs = frozenset(
        s for sl in v4_groups.get('ramp_tutor', {}).values() for s in sl if s.startswith('tutor-land')
    )

    event_anchors: set[str] = set()
    for ev in events.values():
        for ref in ev['produced_by'] + ev['triggered_by']:
            if ref not in bucket_names:
                event_anchors.add(ref)

    return StrategyAssets(
        cfg=cfg,
        buckets=buckets,
        bucket_names=bucket_names,
        payoff_slugs=payoff_slugs,
        support=support,
        density=density,
        events=events,
        event_anchors=frozenset(event_anchors),
        tutor_land_slugs=tutor_land_slugs,
        new_bucket_names=new_bucket_names,
        arch_newbuckets='new_buckets' in enabled,
        arch_anthem_deconf='deconf_anthem' in enabled,
        food_set=food_set,
        mm_set=mm_set,
        theft_set=theft_set,
        anthem_deconf_set=anthem_deconf_set,
        spell_re=re.compile(density['spell-density']['regex']),
        art_re=re.compile(density['artifact-density']['regex']),
        ench_re=re.compile(r'\bEnchantment\b'),
        spell_t=int(density['spell-density']['threshold_cards']),
        art_t=int(density['artifact-density']['threshold_cards']),
        sub_t=int(density['creature-type-density']['threshold_cards']),
    )


def load_assets(cfg: StrategyConfig | None = None) -> StrategyAssets:
    """Load (memoized) the config-bound committed vocab + routing bundle."""
    return _load_assets_cached(cfg or DEFAULT_CONFIG)
