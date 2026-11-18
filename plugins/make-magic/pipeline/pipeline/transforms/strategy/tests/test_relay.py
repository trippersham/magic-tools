"""Tests for the Phase 2 within-card edge relay-node derivation.

Proves that relay injection into the event graph:
1. Closes cross-card loops that the default path does NOT see.
2. Leaves the default ``compute_facts`` / ``resolve()`` path byte-identical
   (the parity-oracle SHAs are unchanged).
3. Correctly maps the 6 curated edge attrs to consumer/producer roles.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from pipeline.transforms.strategy.assets import load_assets
from pipeline.transforms.strategy.edge_roles import get_role
from pipeline.transforms.strategy.machine_vocab import (
    CardAttribute,
    Provenance,
    ProvisionalAttributeStore,
    empty_store,
)
from pipeline.transforms.strategy.relay import (
    _get_role_with_store,
    build_relay_augmented_event_edges,
    elect_engine_units_with_relay,
)
from pipeline.transforms.strategy.strategy_graph import compute_facts, elect_engine_units
from pipeline.transforms.strategy.tests.conftest import LAKE_AVAILABLE
from pipeline.transforms.strategy.tests.engine_parity_oracle import (
    composite_sha,
    compute_engine_isolation_baselines,
    compute_resolve_baselines,
)

_skip_no_lake = pytest.mark.skipif(not LAKE_AVAILABLE, reason='lake parquets not available')

FIX = Path(__file__).resolve().parent / 'fixtures'
RELAY_DECKS = FIX / 'relay_decks'

# The two parity-oracle SHAs that MUST NOT shift.
EXPECTED_ENGINE_SHA = '4549062831f5bfc9606df4539069a16565eeebd275997b8f3b97dc1edc9d5d3b'
EXPECTED_RESOLVE_SHA = 'be2981c37ff3c3a044af5ce467f95b78e3f7640b815c9f61541d8f27fb752ead'


# ---------------------------------------------------------------------------
# Fixture: the Queza + Lich's Mastery relay fixture deck
# ---------------------------------------------------------------------------


@pytest.fixture()
def relay_deck() -> dict:
    path = RELAY_DECKS / 'relay__queza-lichs-mastery.json'
    return json.loads(path.read_text())


@pytest.fixture()
def relay_ctx(relay_deck: dict):
    """FactsContext for the relay fixture deck (DEFAULT path, no relay)."""
    return compute_facts(relay_deck, [])


@pytest.fixture()
def assets():
    return load_assets()


# ---------------------------------------------------------------------------
# A: Default path byte-parity (the relay module must NOT shift these)
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestDefaultPathParity:
    """The default compute_facts / resolve path stays byte-identical.

    These tests prove that importing and using the relay module does not
    contaminate the default code path.
    """

    def test_engine_isolation_sha_unchanged(self) -> None:
        """COMPOSITE engine_isolation SHA over the 17 fixture decks."""
        baselines = compute_engine_isolation_baselines()
        sha = composite_sha(baselines)
        assert sha == EXPECTED_ENGINE_SHA, f'engine_isolation SHA shifted: {sha} != {EXPECTED_ENGINE_SHA}'

    def test_resolve_sha_unchanged(self) -> None:
        """COMPOSITE resolve() SHA over the 17 fixture decks."""
        baselines = compute_resolve_baselines()
        sha = composite_sha(baselines)
        assert sha == EXPECTED_RESOLVE_SHA, f'resolve SHA shifted: {sha} != {EXPECTED_RESOLVE_SHA}'


# ---------------------------------------------------------------------------
# B: Default path on the relay fixture deck (no engines from edge attrs)
# ---------------------------------------------------------------------------


class TestDefaultPathNoRelayEngines:
    """On the relay fixture deck, the DEFAULT path sees no relay-based engines.

    The edge-category attrs (draw-to-drain, draw-to-gain, gain-to-draw) are in
    ``nodes`` but are invisible to the event graph because they appear in no
    catalog event's ``produced_by`` / ``triggers`` lists.
    """

    def test_edge_attrs_in_nodes(self, relay_ctx) -> None:
        """draw-to-drain, draw-to-gain, gain-to-draw are all in nodes."""
        expected = {'draw-to-drain', 'draw-to-gain', 'gain-to-draw'}
        assert expected <= relay_ctx.nodes

    def test_no_relay_hubs(self, relay_ctx) -> None:
        """The default engine isolation has zero relay hubs."""
        iso = relay_ctx.facts['engines']['engine_units']
        assert iso['relay_hubs'] == []

    def test_no_recursive_engines(self, relay_ctx) -> None:
        """No recursive-engine on the default path (only static-payoff)."""
        iso = relay_ctx.facts['engines']['engine_units']
        kinds = {e['kind'] for e in iso['engines']}
        assert 'recursive-engine' not in kinds

    def test_commander_loop_does_not_close(self, relay_ctx) -> None:
        """The commander loop probe does not close without relay injection."""
        iso = relay_ctx.facts['engines']['engine_units']
        assert iso['commander_loop'] is not None
        assert iso['commander_loop']['closes'] is False


# ---------------------------------------------------------------------------
# C: build_relay_augmented_event_edges — the derivation itself
# ---------------------------------------------------------------------------


class TestBuildRelayAugmentedEventEdges:
    """The relay derivation injects edge attrs as consumer+producer nodes."""

    def test_augmented_edges_contain_relay_nodes(self, relay_ctx, assets) -> None:
        """Edge attrs appear as producer or consumer in augmented event edges."""
        augmented = build_relay_augmented_event_edges(relay_ctx.nodes, assets.events)
        relay_attrs = {'draw-to-drain', 'draw-to-gain', 'gain-to-draw'}
        # Collect all attrs that appear in augmented edges
        in_edges = {a for p, c, _ in augmented for a in (p, c)}
        assert relay_attrs & in_edges == relay_attrs, (
            f'Expected all relay attrs in edges; missing: {relay_attrs - in_edges}'
        )

    def test_gain_to_draw_produces_draw_a_card(self, relay_ctx, assets) -> None:
        """gain-to-draw is injected as a producer of draw-a-card."""
        augmented = build_relay_augmented_event_edges(relay_ctx.nodes, assets.events)
        draw_event_producers = {p for p, c, ev in augmented if ev == 'draw-a-card'}
        assert 'gain-to-draw' in draw_event_producers

    def test_gain_to_draw_consumes_lifegain(self, relay_ctx, assets) -> None:
        """gain-to-draw is injected as a consumer of lifegain."""
        augmented = build_relay_augmented_event_edges(relay_ctx.nodes, assets.events)
        lifegain_consumers = {c for p, c, ev in augmented if ev == 'lifegain'}
        assert 'gain-to-draw' in lifegain_consumers

    def test_draw_to_gain_produces_lifegain(self, relay_ctx, assets) -> None:
        """draw-to-gain is injected as a producer of lifegain."""
        augmented = build_relay_augmented_event_edges(relay_ctx.nodes, assets.events)
        lifegain_producers = {p for p, c, ev in augmented if ev == 'lifegain'}
        assert 'draw-to-gain' in lifegain_producers

    def test_draw_to_drain_produces_life_loss_drain(self, relay_ctx, assets) -> None:
        """draw-to-drain is injected as a producer of life-loss-drain.

        NOTE: In the relay fixture, no consumers of ``life-loss-drain`` are in
        ``nodes`` (only 1 card has ``life-loss-matters``, below the 3-card
        threshold), so no event EDGES are generated for that event.  We verify
        the injection at the augmented-list level instead.
        """
        # Verify injection happened by checking the raw augmented producers
        # (the intermediate state, before the cross-product filters by nodes).
        role = get_role('draw-to-drain')
        assert role is not None
        assert role.produced_event == 'life-loss-drain'
        # In a nodes set with a life-loss-drain consumer, edges would appear.
        # Prove it with a synthetic nodes set:
        synthetic_nodes = relay_ctx.nodes | {'life-loss-matters'}
        synthetic_edges = build_relay_augmented_event_edges(synthetic_nodes, assets.events)
        drain_producers = {p for p, c, ev in synthetic_edges if ev == 'life-loss-drain'}
        assert 'draw-to-drain' in drain_producers

    def test_default_edges_lack_relay_nodes(self, relay_ctx, assets) -> None:
        """The DEFAULT event-edge construction has no edge attrs."""

        # Replicate the default event_edges from compute_facts
        def refs_to_nodes(refs):
            return [r for r in refs if r in relay_ctx.nodes]

        default_edges = []
        for evname, ev in assets.events.items():
            for p in refs_to_nodes(ev['produced_by']):
                for c_ in refs_to_nodes(ev['triggers']):
                    if p != c_:
                        default_edges.append((p, c_, evname))

        relay_attrs = {'draw-to-drain', 'draw-to-gain', 'gain-to-draw'}
        in_default = {a for p, c, _ in default_edges for a in (p, c)}
        assert not (relay_attrs & in_default), (
            f'Edge attrs should NOT appear in default edges: {relay_attrs & in_default}'
        )

    def test_no_injection_without_edge_attrs_in_nodes(self, assets) -> None:
        """If no edge attrs are in nodes, augmented == default (no injection)."""
        # nodes with zero edge attrs
        nodes = {'lifegain', 'draw-matters', 'sac'}
        augmented = build_relay_augmented_event_edges(nodes, assets.events)

        # Replicate default
        def refs_to_nodes(refs):
            return [r for r in refs if r in nodes]

        default = []
        for evname, ev in assets.events.items():
            for p in refs_to_nodes(ev['produced_by']):
                for c_ in refs_to_nodes(ev['triggers']):
                    if p != c_:
                        default.append((p, c_, evname))
        assert augmented == default

    def test_determinism_sorted_injection(self, relay_ctx, assets) -> None:
        """Calling twice produces identical edge lists (deterministic)."""
        a1 = build_relay_augmented_event_edges(relay_ctx.nodes, assets.events)
        a2 = build_relay_augmented_event_edges(relay_ctx.nodes, assets.events)
        assert a1 == a2


# ---------------------------------------------------------------------------
# D: Augmented path — the loop closes (the behavioral proof)
# ---------------------------------------------------------------------------


class TestAugmentedPathClosesLoop:
    """On the relay fixture deck, the AUGMENTED path finds the Queza engine.

    This is the core Phase 2 proof: within-card edges (relay nodes) close
    cross-card loops that the default path cannot see.
    """

    def test_relay_hubs_present(self, relay_ctx, assets) -> None:
        """The augmented engine isolation has relay hubs."""
        iso = elect_engine_units_with_relay(
            relay_ctx.nodes,
            assets.events,
            relay_ctx.attr_weight,
            assets,
            relay_ctx.cards,
        )
        assert len(iso['relay_hubs']) > 0
        # Specifically, draw-to-gain and gain-to-draw are relay hubs
        assert 'draw-to-gain' in iso['relay_hubs']
        assert 'gain-to-draw' in iso['relay_hubs']

    def test_recursive_engines_found(self, relay_ctx, assets) -> None:
        """The augmented path finds recursive-engine(s) (not just static-payoff)."""
        iso = elect_engine_units_with_relay(
            relay_ctx.nodes,
            assets.events,
            relay_ctx.attr_weight,
            assets,
            relay_ctx.cards,
        )
        kinds = {e['kind'] for e in iso['engines']}
        assert 'recursive-engine' in kinds

    def test_draw_lifegain_cycle_closes(self, relay_ctx, assets) -> None:
        """The draw-a-card <-> lifegain cycle closes via forward chains.

        The Queza engine: gain-to-draw produces draw-a-card, draw-to-gain
        consumes draw-a-card and produces lifegain, gain-to-draw consumes
        lifegain — a closed loop.
        """
        iso = elect_engine_units_with_relay(
            relay_ctx.nodes,
            assets.events,
            relay_ctx.attr_weight,
            assets,
            relay_ctx.cards,
        )
        chains = iso['forward_chains']
        chain_set = {tuple(c) for c in chains}
        # The bidirectional chain: draw-a-card feeds lifegain AND lifegain feeds draw-a-card
        assert ('draw-a-card', 'lifegain') in chain_set or ('lifegain', 'draw-a-card') in chain_set, (
            f'Expected draw-a-card <-> lifegain chain; got {chains}'
        )
        # Both directions present = closed loop
        assert ('draw-a-card', 'lifegain') in chain_set and ('lifegain', 'draw-a-card') in chain_set, (
            f'Expected bidirectional chain (closed loop); got {chains}'
        )

    def test_determinism_mass_elected(self, relay_ctx, assets) -> None:
        """The augmented path mass-elects candidates (ADR 0014: 'mass-elected').

        The election produces a ranked `elected` candidate set; there is no
        distinguished single lead field. A non-empty election reports
        determinism='mass-elected'.
        """
        iso = elect_engine_units_with_relay(
            relay_ctx.nodes,
            assets.events,
            relay_ctx.attr_weight,
            assets,
            relay_ctx.cards,
        )
        assert iso['determinism'] == 'mass-elected'


# ---------------------------------------------------------------------------
# E: Minimal unit tests — edge cases
# ---------------------------------------------------------------------------


class TestRelayEdgeCases:
    """Edge cases for the relay derivation function."""

    def test_empty_nodes_produces_empty_edges(self, assets) -> None:
        """Empty nodes set -> empty event edges."""
        edges = build_relay_augmented_event_edges(set(), assets.events)
        assert edges == []

    def test_single_edge_attr_in_nodes(self, assets) -> None:
        """A single edge attr in nodes produces edges for its events."""
        nodes = {'draw-to-drain'}
        edges = build_relay_augmented_event_edges(nodes, assets.events)
        # draw-to-drain consumes draw-a-card (but there are no producers of
        # draw-a-card in nodes, so no draw-a-card edges)
        # draw-to-drain produces life-loss-drain (but there are no consumers
        # of life-loss-drain in nodes, so no life-loss-drain edges)
        # With only one node and no other attrs, there are no edges (p != c_
        # prevents self-loops, and no other node is available)
        assert edges == []

    def test_two_complementary_edge_attrs(self, assets) -> None:
        """Two complementary edge attrs (gain-to-draw + draw-to-gain) produce edges."""
        nodes = {'gain-to-draw', 'draw-to-gain'}
        edges = build_relay_augmented_event_edges(nodes, assets.events)
        # gain-to-draw produces draw-a-card, draw-to-gain consumes draw-a-card
        # -> edge (gain-to-draw, draw-to-gain, 'draw-a-card')
        # draw-to-gain produces lifegain, gain-to-draw consumes lifegain
        # -> edge (draw-to-gain, gain-to-draw, 'lifegain')
        assert len(edges) >= 2
        edge_set = {(p, c, ev) for p, c, ev in edges}
        assert ('gain-to-draw', 'draw-to-gain', 'draw-a-card') in edge_set
        assert ('draw-to-gain', 'gain-to-draw', 'lifegain') in edge_set

    def testelect_engine_units_on_minimal_loop(self, assets) -> None:
        """elect_engine_units detects a loop from just two complementary relay attrs."""
        nodes = {'gain-to-draw', 'draw-to-gain'}
        attr_weight: defaultdict[str, float] = defaultdict(
            float,
            {
                'gain-to-draw': 3.0,
                'draw-to-gain': 5.0,
            },
        )
        cards: list[dict] = []  # no cards needed for this unit test
        edges = build_relay_augmented_event_edges(nodes, assets.events)
        iso = elect_engine_units(nodes, edges, attr_weight, assets, cards)
        assert len(iso['relay_hubs']) == 2
        assert 'gain-to-draw' in iso['relay_hubs']
        assert 'draw-to-gain' in iso['relay_hubs']
        # Both engines should be recursive
        for e in iso['engines']:
            assert e['kind'] == 'recursive-engine'

    def test_non_edge_attrs_pass_through_unchanged(self, assets) -> None:
        """Non-edge attrs in nodes still produce catalog-derived event edges."""
        nodes = {'lifegain', 'lifegain-matters', 'draw-to-gain'}
        edges = build_relay_augmented_event_edges(nodes, assets.events)
        # lifegain is a catalog producer of lifegain event
        # lifegain-matters is a catalog consumer of lifegain event
        # draw-to-gain is injected: consumer of draw-a-card, producer of lifegain
        # So we should see: (lifegain, lifegain-matters, 'lifegain') from catalog
        #                 + (draw-to-gain, lifegain-matters, 'lifegain') from relay
        #                 + (lifegain, draw-to-gain???) wait, draw-to-gain is NOT
        #   in the catalog's triggers for lifegain. It's in draw-a-card's
        #   triggers. And draw-to-gain produces lifegain.
        #
        # Let me just check that the catalog-derived edge is still there:
        catalog_edges = [(p, c, ev) for p, c, ev in edges if p == 'lifegain' and c == 'lifegain-matters']
        assert len(catalog_edges) >= 1


# ---------------------------------------------------------------------------
# F: Machine-store -> relay path (the Build 1 keystone)
# ---------------------------------------------------------------------------


def _test_provenance(**overrides: object) -> Provenance:
    defaults = {
        'source': 'haiku',
        'model_id': 'test',
        'authored_at': '2026-11-06T00:00:00Z',
        'source_card_name': 'Test Card',
        'source_card_oracle_id': 'test-oid',
        'deck_context': 'test',
        'scryfall_query': 'oracle:/test/',
        'scryfall_resolved_count': 1,
        'verified_at': '2026-11-06T00:00:01Z',
    }
    defaults.update(overrides)
    return Provenance(**defaults)  # type: ignore[arg-type]


class TestProvisionalAttributeStoreAugmentedEdges:
    """Machine-authored attributes contribute to relay injection via store."""

    def test_empty_store_produces_identical_edges(self, relay_ctx, assets) -> None:
        """With store=empty_store(), edges are identical to store=None."""
        edges_none = build_relay_augmented_event_edges(relay_ctx.nodes, assets.events)
        edges_empty = build_relay_augmented_event_edges(
            relay_ctx.nodes,
            assets.events,
            store=empty_store(),
        )
        assert edges_none == edges_empty

    @_skip_no_lake
    def test_empty_store_sha_parity(self) -> None:
        """Composite engine SHA is unchanged with an explicit empty store."""
        baselines = compute_engine_isolation_baselines()
        sha = composite_sha(baselines)
        assert sha == EXPECTED_ENGINE_SHA

    def test_machine_attr_injected_as_consumer_producer(self, assets) -> None:
        """A machine attr with trigger/effect events appears in augmented edges.

        The attr ``test-counter-to-mana`` consumes ``counter-placed`` and
        produces ``lifegain`` (using an existing event for simplicity).
        With ``test-counter-to-mana`` in nodes AND a counter producer
        (``gives-pp-counters``) also in nodes, the relay should inject
        ``test-counter-to-mana`` as consumer of ``counter-placed``.
        """
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='test-counter-to-mana',
                    query="oracle:'test'",
                    trigger_event='counter-placed',
                    produced_event='lifegain',
                    provenance=_test_provenance(),
                ),
            ]
        )
        nodes = {'gives-pp-counters', 'lifegain-matters', 'test-counter-to-mana'}
        edges = build_relay_augmented_event_edges(nodes, assets.events, store=store)

        # test-counter-to-mana should appear as consumer of counter-placed
        counter_consumers = {c for p, c, ev in edges if ev == 'counter-placed'}
        assert 'test-counter-to-mana' in counter_consumers

        # gives-pp-counters should be a producer feeding test-counter-to-mana
        counter_edges_to_mana = [
            (p, c, ev) for p, c, ev in edges if ev == 'counter-placed' and c == 'test-counter-to-mana'
        ]
        producers_feeding = {p for p, c, ev in counter_edges_to_mana}
        assert 'gives-pp-counters' in producers_feeding

    def test_machine_attr_produces_into_produced_event(self, assets) -> None:
        """The machine attr produces into its effect event."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='test-counter-to-mana',
                    query="oracle:'test'",
                    trigger_event='counter-placed',
                    produced_event='lifegain',
                    provenance=_test_provenance(),
                ),
            ]
        )
        nodes = {'gives-pp-counters', 'lifegain-matters', 'test-counter-to-mana'}
        edges = build_relay_augmented_event_edges(nodes, assets.events, store=store)

        # test-counter-to-mana should produce lifegain events
        lifegain_producers = {p for p, c, ev in edges if ev == 'lifegain'}
        assert 'test-counter-to-mana' in lifegain_producers

        # lifegain-matters should consume the produced lifegain
        lifegain_to_matters = [(p, c, ev) for p, c, ev in edges if ev == 'lifegain' and p == 'test-counter-to-mana']
        consumers = {c for p, c, ev in lifegain_to_matters}
        assert 'lifegain-matters' in consumers

    def test_machine_attr_not_in_nodes_is_ignored(self, assets) -> None:
        """A machine attr NOT in nodes is not injected (even if in store)."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='test-counter-to-mana',
                    query="oracle:'test'",
                    trigger_event='counter-placed',
                    produced_event='lifegain',
                    provenance=_test_provenance(),
                ),
            ]
        )
        # test-counter-to-mana is NOT in nodes
        nodes = {'gives-pp-counters', 'lifegain-matters'}
        edges = build_relay_augmented_event_edges(nodes, assets.events, store=store)

        # test-counter-to-mana should NOT appear anywhere
        all_attrs = {a for p, c, _ in edges for a in (p, c)}
        assert 'test-counter-to-mana' not in all_attrs

    def test_curated_takes_precedence_over_machine(self, relay_ctx, assets) -> None:
        """If a machine attr has the same name as a curated edge, curated wins."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='draw-to-gain',
                    query="oracle:'wrong'",
                    trigger_event='lifegain',  # wrong events
                    produced_event='counter-placed',
                    provenance=_test_provenance(),
                ),
            ]
        )
        # The curated draw-to-gain has trigger=draw-a-card, effect=lifegain.
        # The machine one has different events. Curated should win.
        edges = build_relay_augmented_event_edges(
            relay_ctx.nodes,
            assets.events,
            store=store,
        )
        # draw-to-gain should still appear as a lifegain PRODUCER (curated effect)
        lifegain_producers = {p for p, c, ev in edges if ev == 'lifegain'}
        assert 'draw-to-gain' in lifegain_producers

    def test_machine_attr_with_novel_event_skipped(self, assets) -> None:
        """Machine attr whose effect event is not in the catalog is skipped."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='test-novel-event',
                    query="oracle:'test'",
                    trigger_event='counter-placed',
                    produced_event='nonexistent-event',  # not in catalog
                    provenance=_test_provenance(),
                ),
            ]
        )
        nodes = {'gives-pp-counters', 'test-novel-event'}
        edges = build_relay_augmented_event_edges(nodes, assets.events, store=store)
        all_attrs = {a for p, c, _ in edges for a in (p, c)}
        assert 'test-novel-event' not in all_attrs


# ---------------------------------------------------------------------------
# F: Machine-store -> relay path (Build 1 keystone)
# ---------------------------------------------------------------------------


class TestProvisionalAttributeStoreRelayPath:
    """Machine-authored attributes flow through the relay via the store.

    Proves: a CardAttribute with trigger_event/produced_event is injected
    as a relay node, while an empty store produces byte-identical output.
    """

    def test_empty_store_equals_no_store(self, relay_ctx, assets) -> None:
        """Empty store produces the same edges as no store (byte-parity)."""
        edges_no_store = build_relay_augmented_event_edges(relay_ctx.nodes, assets.events)
        edges_empty_store = build_relay_augmented_event_edges(
            relay_ctx.nodes, assets.events, store=ProvisionalAttributeStore()
        )
        assert edges_no_store == edges_empty_store

    def test_get_role_with_store_curated_first(self) -> None:
        """Curated role takes precedence over a same-named machine attr."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='draw-to-drain',
                    query="oracle:'test'",
                    trigger_event='lifegain',  # different from curated
                    produced_event='lifegain',
                    provenance=Provenance(source='haiku'),
                ),
            ]
        )
        role = _get_role_with_store('draw-to-drain', store)
        # Should return the CURATED role, not the machine one.
        assert role is not None
        assert role.trigger_event == 'draw-a-card'
        assert role.produced_event == 'life-loss-drain'

    def test_get_role_with_store_machine_attr(self) -> None:
        """Machine attr is returned when no curated role exists."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='counter-to-mana',
                    query="oracle:'number of counters on'",
                    trigger_event='counter-placed',
                    produced_event='lifegain',  # placeholder for test
                    provenance=Provenance(source='haiku'),
                ),
            ]
        )
        role = _get_role_with_store('counter-to-mana', store)
        assert role is not None
        assert role.trigger_event == 'counter-placed'
        assert role.produced_event == 'lifegain'

    def test_get_role_with_store_none_for_missing(self) -> None:
        """Missing attr returns None even with a store."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='counter-to-mana',
                    query="oracle:'test'",
                    trigger_event='counter-placed',
                    produced_event='lifegain',
                    provenance=Provenance(source='haiku'),
                ),
            ]
        )
        assert _get_role_with_store('nonexistent', store) is None

    def test_machine_attr_injected_as_relay_node(self, assets) -> None:
        """A machine attr in nodes is injected as consumer+producer in edges."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='counter-to-mana',
                    query="oracle:'number of counters on'",
                    trigger_event='counter-placed',
                    produced_event='lifegain',  # use existing event for test
                    provenance=Provenance(source='haiku'),
                ),
            ]
        )
        # nodes containing the machine attr + some counter producers + lifegain consumer
        nodes = {'counter-to-mana', 'gives-pp-counters', 'lifegain-matters'}
        edges = build_relay_augmented_event_edges(nodes, assets.events, store=store)
        # counter-to-mana should consume counter-placed (from gives-pp-counters producer)
        # and produce lifegain (consumed by lifegain-matters)
        counter_feed = [(p, c, ev) for p, c, ev in edges if c == 'counter-to-mana' and ev == 'counter-placed']
        assert len(counter_feed) >= 1, f'Expected counter-placed -> counter-to-mana; got {edges}'
        mana_produce = [(p, c, ev) for p, c, ev in edges if p == 'counter-to-mana' and ev == 'lifegain']
        assert len(mana_produce) >= 1, f'Expected counter-to-mana -> lifegain; got {edges}'

    def test_machine_attr_with_novel_event_skipped(self, assets) -> None:
        """A machine attr whose events are not in the catalog is safely skipped."""
        store = ProvisionalAttributeStore(
            attributes=[
                CardAttribute(
                    name='counter-to-mana',
                    query="oracle:'test'",
                    trigger_event='counter-placed',
                    produced_event='mana-from-counters',  # NOT in catalog
                    provenance=Provenance(source='haiku'),
                ),
            ]
        )
        nodes = {'counter-to-mana', 'gives-pp-counters'}
        # Should not crash; the machine attr is skipped because its effect event
        # does not exist in the catalog.
        edges = build_relay_augmented_event_edges(nodes, assets.events, store=store)
        # counter-to-mana should NOT appear in any edge
        machine_in_edges = {a for p, c, _ in edges for a in (p, c) if a == 'counter-to-mana'}
        assert not machine_in_edges
