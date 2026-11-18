"""Tests for engine_report.py (Phase PC -- on-demand engine identification).

All tests are **hermetic**: no live network, no DuckDB lake, no real machine
store.  The Queza relay fixture provides the deck; ``tmp_path`` and frozen
snapshots provide the machine store when needed.

Test organisation:

- ``TestIdentifyEnginesQuezaProvisional``  — happy path: PROVISIONAL with unauthored cards
- ``TestIdentifyEnginesQuezaResolved``     — frozen snapshot covers unauthored_cards -> RESOLVED
- ``TestIdentifyEnginesEmptyDeck``         — empty deck edge case
- ``TestNamedEngineCycle``                 — cycle detection + characterization
- ``TestNamedEngineStandalone``            — non-cycle engines
- ``TestFindSCCs``                         — Tarjan's SCC unit tests
- ``TestDefaultPathParity``                — byte-parity gate (curated SHAs unchanged)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.transforms.strategy.assets import EVENTS_FILE
from pipeline.transforms.strategy.engine_report import (
    CompletenessState,
    EngineReport,
    _characterize_engines,
    _find_sccs,
    identify_engines,
)
from pipeline.transforms.strategy.machine_vocab import (
    CardAttribute,
    Provenance,
    ProvisionalAttributeStore,
    empty_store,
)
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
EXPECTED_ENGINE_SHA = '84b9617efcf0ec7481ee167874cb6802b4762ce494a2a8a72085785ddda7441a'
EXPECTED_RESOLVE_SHA = '5a43fc56ce9fd371f8a4729e7cbc11a7855ec08185fac81fe35873c57dfad889'

# Event catalog (with co-located abbrevs) for _characterize_engines unit calls.
CATALOG_EVENTS = json.loads(EVENTS_FILE.read_text())['events']


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def relay_deck() -> dict:
    path = RELAY_DECKS / 'relay__queza-lichs-mastery.json'
    return json.loads(path.read_text())


def _sample_provenance(**overrides: object) -> Provenance:
    defaults = {
        'source': 'haiku',
        'model_id': 'claude-3-5-haiku-20241022',
        'authored_at': '2026-11-06T10:00:00Z',
        'source_card_name': 'Test Card',
        'source_card_oracle_id': 'test-oracle-id',
        'deck_context': 'test-deck',
        'scryfall_query': 'otag:test',
        'scryfall_resolved_count': 1,
        'verified_at': '2026-11-06T10:00:05Z',
    }
    defaults.update(overrides)
    return Provenance(**defaults)  # type: ignore[arg-type]


def _machine_attr(
    name: str,
    query: str,
    trigger_event: str = '',
    produced_event: str = '',
    **prov_overrides: object,
) -> CardAttribute:
    return CardAttribute(
        name=name,
        query=query,
        trigger_event=trigger_event,
        produced_event=produced_event,
        provenance=_sample_provenance(**prov_overrides),
    )


def _covering_store() -> ProvisionalAttributeStore:
    """Machine store whose edge queries cover ALL 7 unauthored cards in the
    relay fixture, so ``unauthored_cards()`` returns empty -> RESOLVED.

    Unauthored cards and their engine-relevant otags:
    - Vilis, Broker of Blood: lifegain-matters
    - Archangel of Thune: lifegain-matters
    - Well of Lost Dreams: lifegain-matters
    - Trudge Garden: lifegain-matters
    - Cleric of Life's Bond: lifegain-matters
    - Rhox Faithmender: lifegain-increaser (but NOT lifegain-matters)
    - Erebos's Intervention: lifegain + removal (but NOT lifegain-matters)
    """
    return ProvisionalAttributeStore(
        attributes=[
            _machine_attr(
                name='test-lifegain-matters-edge',
                query='otag:lifegain-matters',
                trigger_event='lifegain',
                produced_event='draw-a-card',
                source_card_oracle_id='cover-1',
            ),
            _machine_attr(
                name='test-lifegain-increaser-edge',
                query='otag:lifegain-increaser',
                trigger_event='lifegain',
                produced_event='lifegain',
                source_card_oracle_id='cover-2',
            ),
            _machine_attr(
                name='test-removal-lifegain-edge',
                query='otag:lifegain AND otag:removal',
                trigger_event='lifegain',
                produced_event='creature-death',
                source_card_oracle_id='cover-3',
            ),
        ]
    )


# =====================================================================
# TestIdentifyEnginesQuezaProvisional
# =====================================================================


class TestIdentifyEnginesQuezaProvisional:
    """Queza fixture + empty store -> PROVISIONAL with unauthored cards."""

    def test_returns_engine_report(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=empty_store())
        assert isinstance(report, EngineReport)

    def test_state_is_provisional(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=empty_store())
        assert report.state is CompletenessState.PROVISIONAL

    def test_dark_cards_present(self, relay_deck: dict) -> None:
        """All 7 uncovered engine-relevant cards appear in dark_cards."""
        report = identify_engines(relay_deck, store=empty_store())
        expected_dark = {
            'Vilis, Broker of Blood',
            'Archangel of Thune',
            'Well of Lost Dreams',
            'Trudge Garden',
            'Rhox Faithmender',
            "Erebos's Intervention",
            "Cleric of Life's Bond",
        }
        assert set(report.dark_cards) == expected_dark

    def test_dark_cards_sorted(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=empty_store())
        assert report.dark_cards == sorted(report.dark_cards)

    def test_engines_nonempty(self, relay_deck: dict) -> None:
        """The relay-augmented walk finds engines."""
        report = identify_engines(relay_deck, store=empty_store())
        assert len(report.engines) > 0

    def test_draw_gain_cycle_found(self, relay_deck: dict) -> None:
        """The draw-a-card <-> lifegain cycle is detected and named."""
        report = identify_engines(relay_deck, store=empty_store())
        cycle_engines = [e for e in report.engines if e.kind == 'cycle']
        assert len(cycle_engines) >= 1

        cycle = cycle_engines[0]
        assert 'draw-a-card' in cycle.events
        assert 'lifegain' in cycle.events

    def test_cycle_has_relay_hubs(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=empty_store())
        cycle = next(e for e in report.engines if e.kind == 'cycle')
        assert 'draw-to-gain' in cycle.hub_attrs
        assert 'gain-to-draw' in cycle.hub_attrs

    def test_cycle_has_loop_members(self, relay_deck: dict) -> None:
        """The cycle's loop_members include the relay hubs and their consumers."""
        report = identify_engines(relay_deck, store=empty_store())
        cycle = next(e for e in report.engines if e.kind == 'cycle')
        assert 'draw-to-gain' in cycle.loop_members
        assert 'gain-to-draw' in cycle.loop_members

    def test_cycle_has_forward_chains(self, relay_deck: dict) -> None:
        """The cycle's forward_chains show the bidirectional event feeding."""
        report = identify_engines(relay_deck, store=empty_store())
        cycle = next(e for e in report.engines if e.kind == 'cycle')
        chain_set = set(cycle.forward_chains)
        assert ('draw-a-card', 'lifegain') in chain_set
        assert ('lifegain', 'draw-a-card') in chain_set

    def test_cycle_name_contains_event_abbreviations(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=empty_store())
        cycle = next(e for e in report.engines if e.kind == 'cycle')
        assert 'draw' in cycle.name
        assert 'gain' in cycle.name
        assert 'cycle' in cycle.name

    def test_cycle_description_includes_hubs_and_events(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=empty_store())
        cycle = next(e for e in report.engines if e.kind == 'cycle')
        assert 'draw-to-gain' in cycle.description
        assert 'gain-to-draw' in cycle.description
        assert 'draw-a-card' in cycle.description
        assert 'lifegain' in cycle.description

    def test_cycle_weight_positive(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=empty_store())
        cycle = next(e for e in report.engines if e.kind == 'cycle')
        assert cycle.weight > 0

    def test_commander_loop_present(self, relay_deck: dict) -> None:
        """The commander-anchored loop probe is included."""
        report = identify_engines(relay_deck, store=empty_store())
        assert report.commander_loop is not None
        assert 'Queza, Augur of Agonies' in report.commander_loop['commander']

    def test_raw_isolation_present(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=empty_store())
        assert 'engines' in report.raw_isolation
        assert 'relay_hubs' in report.raw_isolation
        assert 'forward_chains' in report.raw_isolation


# =====================================================================
# TestIdentifyEnginesQuezaResolved
# =====================================================================


class TestIdentifyEnginesQuezaResolved:
    """Queza fixture + covering store -> RESOLVED (unauthored_cards empty)."""

    def test_state_is_resolved(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=_covering_store())
        assert report.state is CompletenessState.RESOLVED

    def test_dark_cards_empty(self, relay_deck: dict) -> None:
        report = identify_engines(relay_deck, store=_covering_store())
        assert report.dark_cards == []

    def test_engines_still_found(self, relay_deck: dict) -> None:
        """RESOLVED does not mean no engines -- the walk still runs."""
        report = identify_engines(relay_deck, store=_covering_store())
        assert len(report.engines) > 0

    def test_cycle_still_present(self, relay_deck: dict) -> None:
        """The draw-gain cycle is detected regardless of honesty state."""
        report = identify_engines(relay_deck, store=_covering_store())
        cycle_engines = [e for e in report.engines if e.kind == 'cycle']
        assert len(cycle_engines) >= 1
        cycle = cycle_engines[0]
        assert 'draw-a-card' in cycle.events
        assert 'lifegain' in cycle.events


# =====================================================================
# TestIdentifyEnginesEmptyDeck
# =====================================================================


class TestIdentifyEnginesEmptyDeck:
    """Empty deck or deck with no engine-relevant cards."""

    def test_empty_cards_raises(self) -> None:
        """Deck must have 'cards' key."""
        with pytest.raises(ValueError, match='cards'):
            identify_engines({}, store=empty_store())

    def test_all_lands_produces_empty_engines(self) -> None:
        """A deck of only basic lands has no engines."""
        deck = {
            'cards': [
                {
                    'name': 'Island',
                    'role': None,
                    'oracle_text': '',
                    'otags': [],
                    'keywords': [],
                    'type_line': 'Basic Land — Island',
                    'color_identity': ['U'],
                    'mana_cost': '',
                },
                {
                    'name': 'Swamp',
                    'role': None,
                    'oracle_text': '',
                    'otags': [],
                    'keywords': [],
                    'type_line': 'Basic Land — Swamp',
                    'color_identity': ['B'],
                    'mana_cost': '',
                },
                {
                    'name': 'Plains',
                    'role': None,
                    'oracle_text': '',
                    'otags': [],
                    'keywords': [],
                    'type_line': 'Basic Land — Plains',
                    'color_identity': ['W'],
                    'mana_cost': '',
                },
            ]
        }
        report = identify_engines(deck, store=empty_store())
        assert report.engines == []
        # No engine-relevant cards -> unauthored_cards empty -> RESOLVED.
        assert report.state is CompletenessState.RESOLVED
        assert report.dark_cards == []


# =====================================================================
# TestNamedEngineCycle
# =====================================================================


class TestNamedEngineCycle:
    """Cycle detection and characterization via _characterize_engines."""

    def test_bidirectional_chains_produce_cycle(self) -> None:
        """Two events with bidirectional forward_chains -> one cycle engine."""
        iso = {
            'engines': [
                {
                    'event': 'draw-a-card',
                    'interior_node': 'gain-to-draw',
                    'enabler': 'gain-to-draw',
                    'kind': 'recursive-engine',
                    'producers': ['gain-to-draw'],
                    'consumers': ['draw-to-gain'],
                    'payoff_archetypes': [],
                    'mass': 10.0,
                },
                {
                    'event': 'lifegain',
                    'interior_node': 'draw-to-gain',
                    'enabler': 'draw-to-gain',
                    'kind': 'recursive-engine',
                    'producers': ['draw-to-gain'],
                    'consumers': ['gain-to-draw'],
                    'payoff_archetypes': [],
                    'mass': 8.0,
                },
            ],
            'forward_chains': [['draw-a-card', 'lifegain'], ['lifegain', 'draw-a-card']],
            'relay_hubs': ['draw-to-gain', 'gain-to-draw'],
            'commander_loop': None,
            'determinism': 'forward-engine-isolated',
        }
        named = _characterize_engines(iso, CATALOG_EVENTS)
        cycles = [e for e in named if e.kind == 'cycle']
        assert len(cycles) == 1

        cycle = cycles[0]
        assert set(cycle.events) == {'draw-a-card', 'lifegain'}
        assert 'draw-to-gain' in cycle.hub_attrs
        assert 'gain-to-draw' in cycle.hub_attrs
        assert cycle.weight == 18.0
        assert 'draw' in cycle.name and 'gain' in cycle.name
        assert 'cycle' in cycle.name

    def test_no_chains_produces_no_cycles(self) -> None:
        """Without forward_chains, no cycle grouping occurs."""
        iso = {
            'engines': [
                {
                    'event': 'lifegain',
                    'interior_node': None,
                    'enabler': 'lifegain',
                    'kind': 'static-payoff',
                    'producers': ['lifegain'],
                    'consumers': ['lifegain-matters'],
                    'payoff_archetypes': [],
                    'mass': 5.0,
                },
            ],
            'forward_chains': [],
            'relay_hubs': [],
            'commander_loop': None,
            'determinism': 'loop-or-flat-no-forward-hub',
        }
        named = _characterize_engines(iso, CATALOG_EVENTS)
        assert all(e.kind != 'cycle' for e in named)
        assert len(named) == 1
        assert named[0].kind == 'static-payoff'

    def test_empty_engines_produces_empty_list(self) -> None:
        iso = {
            'engines': [],
            'forward_chains': [],
            'relay_hubs': [],
            'commander_loop': None,
            'determinism': 'loop-or-flat-no-forward-hub',
        }
        assert _characterize_engines(iso, CATALOG_EVENTS) == []

    def test_unidirectional_chain_not_a_cycle(self) -> None:
        """A -> B without B -> A is NOT a cycle."""
        iso = {
            'engines': [
                {
                    'event': 'A',
                    'interior_node': 'h1',
                    'enabler': 'h1',
                    'kind': 'recursive-engine',
                    'producers': ['h1'],
                    'consumers': ['c1'],
                    'payoff_archetypes': [],
                    'mass': 5.0,
                },
                {
                    'event': 'B',
                    'interior_node': 'h2',
                    'enabler': 'h2',
                    'kind': 'recursive-engine',
                    'producers': ['h2'],
                    'consumers': ['c2'],
                    'payoff_archetypes': [],
                    'mass': 3.0,
                },
            ],
            'forward_chains': [['A', 'B']],
            'relay_hubs': ['h1', 'h2'],
            'commander_loop': None,
            'determinism': 'forward-engine-isolated',
        }
        named = _characterize_engines(iso, CATALOG_EVENTS)
        cycles = [e for e in named if e.kind == 'cycle']
        assert len(cycles) == 0
        assert len(named) == 2


# =====================================================================
# TestNamedEngineStandalone
# =====================================================================


class TestNamedEngineStandalone:
    """Non-cycle engines: recursive-engine and static-payoff."""

    def test_standalone_recursive_engine(self) -> None:
        iso = {
            'engines': [
                {
                    'event': 'draw-a-card',
                    'interior_node': 'my-hub',
                    'enabler': 'my-hub',
                    'kind': 'recursive-engine',
                    'producers': ['my-hub', 'p2'],
                    'consumers': ['c1', 'c2'],
                    'payoff_archetypes': [],
                    'mass': 12.0,
                },
            ],
            'forward_chains': [],
            'relay_hubs': ['my-hub'],
            'commander_loop': None,
            'determinism': 'forward-engine-isolated',
        }
        named = _characterize_engines(iso, CATALOG_EVENTS)
        assert len(named) == 1
        eng = named[0]
        assert eng.kind == 'recursive-engine'
        assert eng.name == 'draw engine'
        assert eng.hub_attrs == ['my-hub']
        assert set(eng.loop_members) == {'my-hub', 'p2', 'c1', 'c2'}
        assert eng.events == ['draw-a-card']
        assert eng.weight == 12.0

    def test_standalone_static_payoff(self) -> None:
        iso = {
            'engines': [
                {
                    'event': 'lifegain',
                    'interior_node': None,
                    'enabler': 'lifegain',
                    'kind': 'static-payoff',
                    'producers': ['lifegain'],
                    'consumers': ['lifegain-matters'],
                    'payoff_archetypes': [],
                    'mass': 5.0,
                },
            ],
            'forward_chains': [],
            'relay_hubs': [],
            'commander_loop': None,
            'determinism': 'loop-or-flat-no-forward-hub',
        }
        named = _characterize_engines(iso, CATALOG_EVENTS)
        assert len(named) == 1
        eng = named[0]
        assert eng.kind == 'static-payoff'
        assert eng.name == 'gain payoff'
        assert eng.hub_attrs == []
        assert eng.weight == 5.0

    def test_engines_sorted_by_weight_descending(self) -> None:
        iso = {
            'engines': [
                {
                    'event': 'A',
                    'interior_node': None,
                    'enabler': 'a',
                    'kind': 'static-payoff',
                    'producers': ['a'],
                    'consumers': ['b'],
                    'payoff_archetypes': [],
                    'mass': 3.0,
                },
                {
                    'event': 'B',
                    'interior_node': None,
                    'enabler': 'c',
                    'kind': 'static-payoff',
                    'producers': ['c'],
                    'consumers': ['d'],
                    'payoff_archetypes': [],
                    'mass': 10.0,
                },
            ],
            'forward_chains': [],
            'relay_hubs': [],
            'commander_loop': None,
            'determinism': 'loop-or-flat-no-forward-hub',
        }
        named = _characterize_engines(iso, CATALOG_EVENTS)
        assert named[0].weight >= named[1].weight


# =====================================================================
# TestFindSCCs
# =====================================================================


class TestFindSCCs:
    """Tarjan's SCC detection on forward-chain graphs."""

    def test_bidirectional_pair(self) -> None:
        sccs = _find_sccs([['A', 'B'], ['B', 'A']])
        assert len(sccs) == 1
        assert sccs[0] == frozenset({'A', 'B'})

    def test_three_node_cycle(self) -> None:
        sccs = _find_sccs([['A', 'B'], ['B', 'C'], ['C', 'A']])
        assert len(sccs) == 1
        assert sccs[0] == frozenset({'A', 'B', 'C'})

    def test_no_cycle(self) -> None:
        sccs = _find_sccs([['A', 'B'], ['B', 'C']])
        assert sccs == []

    def test_empty_chains(self) -> None:
        assert _find_sccs([]) == []

    def test_single_node_not_scc(self) -> None:
        """A self-loop is not produced by forward_chains (p != c_ guard)."""
        sccs = _find_sccs([['A', 'B']])
        assert sccs == []

    def test_two_disjoint_cycles(self) -> None:
        sccs = _find_sccs(
            [
                ['A', 'B'],
                ['B', 'A'],
                ['C', 'D'],
                ['D', 'C'],
            ]
        )
        assert len(sccs) == 2
        scc_sets = set(sccs)
        assert frozenset({'A', 'B'}) in scc_sets
        assert frozenset({'C', 'D'}) in scc_sets

    def test_deterministic_output(self) -> None:
        """Same input -> same output (sorted iteration)."""
        chains = [['B', 'A'], ['A', 'B']]
        s1 = _find_sccs(chains)
        s2 = _find_sccs(chains)
        assert s1 == s2


# =====================================================================
# TestDefaultPathParity — the byte-parity gate
# =====================================================================


@_skip_no_lake
class TestDefaultPathParity:
    """Importing and using engine_report does NOT shift the curated SHAs.

    This is the hard gate: the default ``compute_facts`` / ``resolve()`` path
    must stay byte-identical.
    """

    def test_engine_isolation_sha_unchanged(self) -> None:
        baselines = compute_engine_isolation_baselines()
        sha = composite_sha(baselines)
        assert sha == EXPECTED_ENGINE_SHA, f'engine_isolation SHA shifted: {sha} != {EXPECTED_ENGINE_SHA}'

    def test_resolve_sha_unchanged(self) -> None:
        baselines = compute_resolve_baselines()
        sha = composite_sha(baselines)
        assert sha == EXPECTED_RESOLVE_SHA, f'resolve SHA shifted: {sha} != {EXPECTED_RESOLVE_SHA}'
