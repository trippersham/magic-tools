"""Frozen vocabulary + event catalog + routing — the engine's committed data.

Loads the promoted data files (``data/attribute_vocab_v4.json``,
``data/event_catalog_v4.json``) and assembles, as a function of a
:class:`~.config.StrategyConfig`, every derived constant the facts/naming layers
need: the bucket set, payoff/support vocab, density thresholds, event anchors,
the EDHREC membership family, the compiled synth-attribute predicates, and the
config-bound :class:`~.vocab.Router`.

Where identity lives
--------------------
Routing is NOT written here. An attribute NAME becomes a strategy through the two
declarative maps in :mod:`.vocab` (``ATTRIBUTES`` for membership, ``ARCHETYPE_EDGES``
for the edge), compiled into a :class:`~.vocab.Router`;
``StrategyAssets.archetype_of_attr`` is a thin call into it. This module owns only
the *membership* families the facts layer unions onto each card — the buckets, the
payoff/anchor/enchantment-regex surfacing, and ``EDHREC_ATTR_SLUGS`` — plus the
compiled synth predicates (vehicle/energy/extra-turn/devotion/morph). Membership
and edge never mix.

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
from pipeline.transforms.strategy.query import Predicate
from pipeline.transforms.strategy.vocab import Router, build_router, compile_synth

DATA_DIR = Path(__file__).resolve().parent / 'data'
VOCAB_FILE = DATA_DIR / 'attribute_vocab_v4.json'
EVENTS_FILE = DATA_DIR / 'event_catalog_v4.json'
#: The T5.5 EDHREC NEW-NODE nameable label space. SINGLE SOURCE OF TRUTH: the
#: set is loaded from the committed ``labelspace.json`` (data-authoritative) so
#: the code and the realized label space can never silently diverge.
LABELSPACE_FILE = DATA_DIR / 'labelspace.json'

# --- static control vocab (independent of config) ---------------------------
#: The spellslinger "on-cast payoff" children, read by the facts substantiveness
#: guard (``cast-trigger-you``/``-other`` = magecraft/prowess count; the bare
#: ``cast-trigger`` parent and ``-self`` are a per-card body, not a payoff).
_CAST_CHILDREN = frozenset({
    'cast-trigger-you', 'cast-trigger-self', 'cast-trigger-other',
    'second-spell-matters', 'storm-count-matters', 'prowess-like',
})
#: The enchantment-family surfacing regex: any card otag containing one of these
#: substrings is surfaced (as itself) onto the card as a density attribute.
_ENCH_ATTR_RE = re.compile(r'(enchant|aura|constellation|shrine|saga)')

# EDHREC membership family: the in-lake otag slugs the facts layer surfaces onto a
# card (as themselves) when ``cfg.edhrec_labels`` is on, so the EDHREC identities
# can see them. This is MEMBERSHIP surfacing, distinct from routing (which the
# vocab owns); it is kept explicit because some members (e.g. ``crew``,
# ``poisonous``) are neither payoff nor event-anchor slugs and would otherwise
# never surface.
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
    #: Compiled routing: attribute NAME -> strategy, from the vocab's two maps.
    router: Router
    #: Compiled synth-attribute predicates (name, predicate), card-field queries.
    synth_predicates: tuple[tuple[str, Predicate], ...]
    spell_re: re.Pattern
    art_re: re.Pattern
    ench_re: re.Pattern
    spell_t: int
    art_t: int
    sub_t: int
    cast_children: frozenset[str] = _CAST_CHILDREN
    ench_attr_re: re.Pattern = _ENCH_ATTR_RE

    def archetype_of_attr(self, a: str) -> str | None:
        """Deterministic attr-name -> identity archetype: one call into the vocab Router."""
        return self.router.route(a)


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

    arch_newbuckets = 'new_buckets' in enabled
    arch_anthem_deconf = 'deconf_anthem' in enabled
    router = build_router(
        edhrec=cfg.edhrec_labels, newbuckets=arch_newbuckets, anthem_deconf=arch_anthem_deconf)
    synth_predicates = tuple(compile_synth(edhrec=cfg.edhrec_labels))

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
        arch_newbuckets=arch_newbuckets,
        arch_anthem_deconf=arch_anthem_deconf,
        router=router,
        synth_predicates=synth_predicates,
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
