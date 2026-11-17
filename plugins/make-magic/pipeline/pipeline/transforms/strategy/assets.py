"""Frozen vocabulary + event catalog + strategy distillation — the engine's committed data.

Loads the promoted data files (``data/attribute_catalog.json``,
``data/event_catalog.json``) and assembles, as a function of a
:class:`~.config.StrategyConfig`, every derived constant the facts/naming layers
need: the unified ``card_attributes`` membership matcher (one compiled predicate
per bucket/payoff CardAttribute), the ``support`` role set, density thresholds,
event anchors, the EDHREC membership family, the compiled synth-attribute
predicates, and the config-bound :class:`~.vocab.Router`.

Where identity lives
--------------------
Distillation is NOT written here. An attribute NAME becomes a strategy through the two
declarative maps in :mod:`.vocab` (``ATTRIBUTES`` for membership, ``ARCHETYPE_EDGES``
for the edge), compiled into a :class:`~.vocab.Router`;
``StrategyAssets.archetype_of_attr`` is a thin call into it. This module owns only
the *membership* families the facts layer unions onto each card — the unified
``card_attributes`` query matcher (buckets + payoffs), the anchor/enchantment-slug
surfacing, and ``EDHREC_ATTR_SLUGS`` — plus the compiled synth predicates
(vehicle/energy/extra-turn/devotion/morph). Membership and edge never mix.

``load_assets(cfg)`` is memoized per config so repeated ``resolve`` calls reuse
one immutable :class:`StrategyAssets`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pipeline.transforms.strategy.config import DEFAULT_CONFIG, StrategyConfig
from pipeline.transforms.strategy.query import Predicate, compile_query
from pipeline.transforms.strategy.vocab import (
    BASE_IDENTITIES,
    EDHREC_IDENTITIES,
    Router,
    build_router,
    compile_synth,
)

DATA_DIR = Path(__file__).resolve().parent / 'data'
VOCAB_FILE = DATA_DIR / 'attribute_catalog.json'
EVENTS_FILE = DATA_DIR / 'event_catalog.json'
#: The T5.5 EDHREC NEW-NODE nameable label space. SINGLE SOURCE OF TRUTH: the
#: set is loaded from the committed ``labelspace.json`` (data-authoritative) so
#: the code and the realized label space can never silently diverge.
LABELSPACE_FILE = DATA_DIR / 'labelspace.json'

# --- static control vocab (independent of config) ---------------------------
#: The enchantment-family surfacing SET: the exact otag slugs the facts layer
#: surfaces (as themselves) onto a card so the enchantment identities can distill
#: them. SINGLE SOURCE OF TRUTH: the literal ``otag:<slug>`` atoms named by the
#: vocab's ``enchantress`` (EDHREC tier-1) and ``enchantments`` (base) identity
#: queries, extracted here so the surfacing can never diverge from what those two
#: identities actually look for. Replaces the old broad substring regex, which
#: over-matched cycle/removal/hate-adjacent slugs 4.6x and missed ``auto-attach``.
_OTAG_SLUG_RE = re.compile(r'otag:([a-z0-9-]+)')


def _ench_attr_slugs() -> frozenset[str]:
    queries = dict(EDHREC_IDENTITIES) | dict(BASE_IDENTITIES)
    slugs: set[str] = set()
    for identity in ('enchantress', 'enchantments'):
        slugs.update(_OTAG_SLUG_RE.findall(queries[identity]))
    return frozenset(slugs)


ENCH_ATTR_SLUGS = _ench_attr_slugs()

# EDHREC membership family: the in-lake otag slugs the facts layer surfaces onto a
# card (as themselves) when ``cfg.edhrec_labels`` is on, so the EDHREC identities
# can see them. This is MEMBERSHIP surfacing, distinct from distillation (which the
# vocab owns); it is kept explicit because some members (e.g. ``crew``,
# ``poisonous``) are neither payoff nor event-anchor slugs and would otherwise
# never surface.
_MILL_OPP_SLUGS = frozenset({'mill-opponent', 'mill-any', 'synergy-mill'})
_PW_SLUGS = frozenset({'synergy-planeswalker', 'recursion-planeswalker', 'protects-planeswalker', 'superfriends'})
_DISCARD_SLUGS = frozenset(
    {
        'discard-matters',
        'self-discard-matters',
        'wheel',
        'wheel-one-sided',
        'wheel-symmetrical',
        'wheel-symmetrical-optional',
        'miniwheel',
    }
)
_TREASURE_SLUGS = frozenset(
    {
        'repeatable-treasures',
        'synergy-treasure',
        'repeatable-clues',
        'synergy-clue',
        'repeatable-blood',
        'synergy-blood',
        'repeatable-food',
        'synergy-food',
        'eat-food',
        'food',
    }
)
_VEHICLE_SLUGS = frozenset(
    {'synergy-vehicle', 'crew', 'bring-your-own-crew', 'alternative-crewing', 'animate-vehicle', 'crewless-vehicle'}
)
_ENERGY_SLUGS = frozenset({'energy-generator', 'counter-fuel-energy', 'energy-increaser', 'synergy-energy'})
_EXTRATURN_SLUGS = frozenset({'extra-turn', 'extra-upkeep'})
_POISON_SLUGS = frozenset(
    {'synergy-poison', 'poison-opponents', 'poison-mechanics', 'poisonous', 'synergy-toxic', 'synergy-infect'}
)
_THEFT_WIDE = frozenset(
    {
        'theft',
        'theft-creature',
        'theft-artifact',
        'theft-land',
        'theft-planeswalker',
        'theft-spell',
        'theft-cast',
        'nightveil-theft',
    }
)
EDHREC_ATTR_SLUGS = (
    _MILL_OPP_SLUGS
    | _PW_SLUGS
    | _DISCARD_SLUGS
    | _TREASURE_SLUGS
    | _VEHICLE_SLUGS
    | _ENERGY_SLUGS
    | _EXTRATURN_SLUGS
    | _POISON_SLUGS
    | _THEFT_WIDE
)
EDHREC_NEW_LABELS: frozenset[str] = frozenset(json.loads(LABELSPACE_FILE.read_text())['new_nameable_labels_added'])

# T5 correction B: directed hate-* answers map (FACTS only; scoring deferred).
ANSWERS_MAP: dict[str, list[str]] = {
    'graveyard/reanimator': [
        'hate-graveyard',
        'single-minded-graveyard-hate',
        'hate-graveyard-cast',
        'sweeper-graveyard',
    ],
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
    return (
        a == 'graveyard'
        or a.startswith(('recursion', 'reanimate'))
        or a
        in (
            'cards-in-graveyard-matter',
            'castable-from-graveyard',
            'graveyard-fuel',
            'graveyard-fuel-self',
            'activate-from-graveyard',
            'mill-self',
        )
    )


def is_land(card: dict) -> bool:
    tl = card.get('type_line') or ''
    return 'Land' in tl and 'Creature' not in tl


@dataclass(frozen=True)
class StrategyAssets:
    """Immutable, config-bound bundle of the engine's committed vocab + strategy distillation."""

    cfg: StrategyConfig
    #: Bucket name -> its member otag slug-list, reconstructed from the unified
    #: ``card_attributes`` query map. Retained for the Phase-A combo-edge query
    #: builder (:func:`.combo_edges._attr_query`), which expands a bucket attribute
    #: to the OR of its slugs; membership itself flows through ``card_attr_preds``.
    buckets: dict[str, list[str]]
    #: The bucket-origin attribute names (the ``card_attributes`` entries that are
    #: buckets, not single-otag payoffs). The ``event_anchors`` split subtracts
    #: exactly this set, so a bucket NAME that coincides with an event ref is not
    #: double-surfaced as an anchor.
    bucket_names: frozenset[str]
    #: The ONE membership matcher: compiled ``(name, predicate)`` for every
    #: CardAttribute (buckets + payoffs) in the unified ``card_attributes`` map. A
    #: card carries ``name`` iff ``predicate`` matches its otag-closure card view —
    #: the single resolution path that replaced the separate bucket/payoff tests.
    card_attr_preds: tuple[tuple[str, Predicate], ...]
    support: frozenset[str]
    density: dict
    events: dict
    event_anchors: frozenset[str]
    #: Compiled ``otag:tutor-land`` predicate (ancestor-closed: card otags carry the
    #: exact parent, so exact membership of ``tutor-land`` matches the whole family).
    tutor_land_pred: Predicate
    #: Compiled distillation: attribute NAME -> strategy, from the vocab's two maps.
    router: Router
    #: Compiled synth-attribute predicates (name, predicate), card-field queries.
    synth_predicates: tuple[tuple[str, Predicate], ...]
    spell_re: re.Pattern
    art_re: re.Pattern
    ench_re: re.Pattern
    spell_t: int
    art_t: int
    ench_t: int
    sub_t: int

    def archetype_of_attr(self, a: str) -> str | None:
        """Deterministic attr-name -> identity archetype: one call into the vocab Router."""
        return self.router.route(a)


def _bucket_slugs(query: str) -> list[str]:
    """The ordered otag slugs of a bucket's OR-of-``otag:`` query.

    A bucket's ``card_attributes`` value is ``' OR '.join('otag:'+s for s in slugs)``
    by construction, so splitting it back recovers the exact member slug-list for
    the combo-edge query builder. Membership never routes through this — it is the
    slug *list*, not a matcher.
    """
    return [atom.removeprefix('otag:') for atom in query.split(' OR ')]


@cache
def _load_assets_cached(cfg: StrategyConfig) -> StrategyAssets:
    vocab = json.loads(VOCAB_FILE.read_text())
    # The unified CardAttribute membership model (ADR 0009): one ``card_attributes``
    # map (name -> otag-query) for every bucket + payoff, matched by one compiled
    # predicate pass. ``bucket_attributes`` names the bucket-origin subset (the
    # multi-slug aggregates), which the Phase-A combo-edge builder expands to slugs
    # and which the event-anchor split subtracts. The group-merge mechanism is gone:
    # the group slugs are baked into each bucket's query (ADR 0006).
    card_attr_queries: dict[str, str] = vocab['card_attributes']
    bucket_name_list: list[str] = vocab['bucket_attributes']
    buckets: dict[str, list[str]] = {name: _bucket_slugs(card_attr_queries[name]) for name in bucket_name_list}

    # Adjacency is DERIVED at catalog load (ADR 0012/0022): the catalog's
    # hand-enumerated ``produced_by``/``triggers`` are a frozen subset; the
    # authoritative lists are the descendant-closure of each event side's
    # owner-approved anchors (the taxonomy view), replaced in place here so every
    # downstream reader (relay, facts, combo_edges, substantiveness) sees the
    # derived view. One named rule: ``derive_event_adjacency`` (adjacency).
    #
    # Graceful offline fallback: when the taxonomy parquet is absent (CI / no
    # lake), the committed hand-enumerated adjacency is used.  Tests that
    # specifically verify derived adjacency gate on ``LAKE_AVAILABLE`` in the
    # strategy conftest.
    raw_events = json.loads(EVENTS_FILE.read_text())['events']
    try:
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency

        events = derive_event_adjacency(raw_events)
    except FileNotFoundError:
        # No lake (offline CI / unhydrated): fall back to the committed
        # hand-enumerated adjacency. Observable, not silent — a production run
        # must hydrate the lake to get the derived view (ADR 0022).
        logging.getLogger(__name__).warning(
            'oracle-tag taxonomy absent; using committed enumerated event '
            'adjacency (derived adjacency disabled — hydrate the lake for '
            'production). ADR 0022.'
        )
        events = raw_events

    support = frozenset(vocab['support_attributes'])
    density = vocab['ranking_only_density']
    bucket_names = frozenset(bucket_name_list)
    # The ONE matcher: compile every CardAttribute query once (buckets + payoffs).
    card_attr_preds = tuple((name, compile_query(query)) for name, query in card_attr_queries.items())

    tutor_land_pred = compile_query('otag:tutor-land')

    event_anchors: set[str] = set()
    for ev in events.values():
        for ref in ev['produced_by'] + ev['triggers']:
            if ref not in bucket_names:
                event_anchors.add(ref)

    router = build_router()
    synth_predicates = tuple(compile_synth())

    return StrategyAssets(
        cfg=cfg,
        buckets=buckets,
        bucket_names=bucket_names,
        card_attr_preds=card_attr_preds,
        support=support,
        density=density,
        events=events,
        event_anchors=frozenset(event_anchors),
        tutor_land_pred=tutor_land_pred,
        router=router,
        synth_predicates=synth_predicates,
        spell_re=re.compile(density['spell-density']['regex']),
        art_re=re.compile(density['artifact-density']['regex']),
        ench_re=re.compile(density['enchantment-density']['regex']),
        spell_t=int(density['spell-density']['threshold_cards']),
        art_t=int(density['artifact-density']['threshold_cards']),
        ench_t=int(density['enchantment-density']['threshold_cards']),
        sub_t=int(density['creature-type-density']['threshold_cards']),
    )


def load_assets(cfg: StrategyConfig | None = None) -> StrategyAssets:
    """Load (memoized) the config-bound committed vocab + strategy distillation bundle."""
    return _load_assets_cached(cfg or DEFAULT_CONFIG)
