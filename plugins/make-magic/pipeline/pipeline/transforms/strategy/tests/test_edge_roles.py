"""Tests for the curated EventEffect sidecar registry (P1).

Asserts that the 6 curated within-card edge-category attributes carry
(trigger_event, produced_event) roles aligned to the event catalog, with
source='curated' provenance. No held file is touched — roles live in the
sidecar ``edge_roles`` module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.transforms.strategy.machine_vocab import Provenance


# ---------------------------------------------------------------------------
# The 6 curated edge attrs (from SYNTH_ALWAYS_ATTRIBUTES in vocab.py)
# ---------------------------------------------------------------------------

EXPECTED_CURATED_EDGES: dict[str, tuple[str, str]] = {
    'draw-to-drain': ('draw-a-card', 'life-loss-drain'),
    'draw-to-gain': ('draw-a-card', 'lifegain'),
    'gain-to-drain': ('lifegain', 'life-loss-drain'),
    'drain-to-gain': ('life-loss-drain', 'lifegain'),
    'gain-to-draw': ('lifegain', 'draw-a-card'),
    'drain-to-draw': ('life-loss-drain', 'draw-a-card'),
}

# The 3 event slugs the 6 edges use — all must be in the event catalog.
REFERENCED_EVENTS = frozenset({'draw-a-card', 'lifegain', 'life-loss-drain'})


# ---------------------------------------------------------------------------
# Fixture: event catalog for cross-validation
# ---------------------------------------------------------------------------

@pytest.fixture()
def event_catalog() -> dict:
    catalog_path = (
        Path(__file__).resolve().parent.parent / 'data' / 'event_catalog_v4.json'
    )
    with open(catalog_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCuratedEdgeRoles:
    """The 6 curated edge attrs round-trip their roles + provenance."""

    def test_registry_has_exactly_six_entries(self) -> None:
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        assert len(CURATED_EDGE_ROLES) == 6

    def test_registry_names_match_synth_always(self) -> None:
        """The registry keys must be exactly the 6 edge attr names."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        assert set(CURATED_EDGE_ROLES.keys()) == set(EXPECTED_CURATED_EDGES.keys())

    @pytest.mark.parametrize(
        'attr_name,expected_trigger,expected_effect',
        [
            (name, trig, eff)
            for name, (trig, eff) in EXPECTED_CURATED_EDGES.items()
        ],
        ids=list(EXPECTED_CURATED_EDGES.keys()),
    )
    def test_role_round_trip(
        self, attr_name: str, expected_trigger: str, expected_effect: str,
    ) -> None:
        """Each attr's (trigger_event, produced_event) matches the expected mapping."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        role = CURATED_EDGE_ROLES[attr_name]
        assert role.trigger_event == expected_trigger
        assert role.produced_event == expected_effect

    def test_provenance_is_curated(self) -> None:
        """Every curated edge role carries source='curated'."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        for name, role in CURATED_EDGE_ROLES.items():
            assert role.provenance.source == 'curated', (
                f'{name}: expected source="curated", got {role.provenance.source!r}'
            )

    def test_provenance_type(self) -> None:
        """Provenance reuses the machine_vocab.Provenance dataclass (not a dup)."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        for role in CURATED_EDGE_ROLES.values():
            assert isinstance(role.provenance, Provenance)

    def test_referenced_events_exist_in_catalog(self, event_catalog: dict) -> None:
        """All trigger/produced slugs map to real events in event_catalog_v4."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        catalog_events = set(event_catalog['events'].keys())
        for name, role in CURATED_EDGE_ROLES.items():
            assert role.trigger_event in catalog_events, (
                f'{name}.trigger_event={role.trigger_event!r} not in catalog'
            )
            assert role.produced_event in catalog_events, (
                f'{name}.produced_event={role.produced_event!r} not in catalog'
            )

    def test_get_role_hit(self) -> None:
        """get_role returns the EventEffect for a known attr."""
        from pipeline.transforms.strategy.edge_roles import get_role

        role = get_role('draw-to-drain')
        assert role is not None
        assert role.trigger_event == 'draw-a-card'
        assert role.produced_event == 'life-loss-drain'

    def test_get_role_miss(self) -> None:
        """get_role returns None for an unknown attr (e.g. 'morph')."""
        from pipeline.transforms.strategy.edge_roles import get_role

        assert get_role('morph') is None
        assert get_role('rad-counter') is None

    def test_all_edge_attr_names(self) -> None:
        """all_edge_attr_names returns the frozenset of the 6 names."""
        from pipeline.transforms.strategy.edge_roles import all_edge_attr_names

        names = all_edge_attr_names()
        assert isinstance(names, frozenset)
        assert names == frozenset(EXPECTED_CURATED_EDGES.keys())


class TestCatalogAlignment:
    """Cross-validates that no event slug is invented outside the catalog."""

    def test_no_invented_events(self, event_catalog: dict) -> None:
        """Every event slug referenced by the registry exists in the catalog."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        catalog_events = set(event_catalog['events'].keys())
        referenced = set()
        for role in CURATED_EDGE_ROLES.values():
            referenced.add(role.trigger_event)
            referenced.add(role.produced_event)
        missing = referenced - catalog_events
        assert not missing, f'Event slug(s) missing from catalog: {missing}'

    def test_referenced_events_is_expected_set(self) -> None:
        """The 6 attrs reference exactly {draw-a-card, lifegain, life-loss-drain}."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        referenced = set()
        for role in CURATED_EDGE_ROLES.values():
            referenced.add(role.trigger_event)
            referenced.add(role.produced_event)
        assert referenced == REFERENCED_EVENTS
