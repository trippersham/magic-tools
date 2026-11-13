"""Tests for Phase 2A task 1: EdgeRole→EventEffect rename + additive guard scaffolding.

TDD red-phase: these tests import the RENAMED class/fields and assert the NEW
defaulted fields.  They MUST fail before the rename (ImportError / AttributeError)
and pass after.

Parity gate: the rename + additive fields must NOT alter any serialized/hashed
representation.  A separate parity-oracle run (not duplicated here) gates that.
"""

from __future__ import annotations

import pytest

from pipeline.transforms.strategy.machine_vocab import Provenance


# ────────────────────────────────────────────────────────────────────────────
# 1. EventEffect import + curated round-trip (renamed class + field)
# ────────────────────────────────────────────────────────────────────────────

class TestEventEffectRename:
    """The renamed EventEffect class exists and curated entries round-trip."""

    def test_eventeffect_importable(self) -> None:
        """EventEffect is importable from edge_roles."""
        from pipeline.transforms.strategy.edge_roles import EventEffect

        assert EventEffect is not None

    def test_curated_registry_uses_eventeffect(self) -> None:
        """CURATED_EDGE_ROLES values are EventEffect instances."""
        from pipeline.transforms.strategy.edge_roles import (
            CURATED_EDGE_ROLES,
            EventEffect,
        )

        for name, entry in CURATED_EDGE_ROLES.items():
            assert isinstance(entry, EventEffect), (
                f'{name}: expected EventEffect, got {type(entry).__name__}'
            )

    def test_produced_event_field_exists(self) -> None:
        """EventEffect has the renamed field 'produced_event' (was 'effect_event')."""
        from pipeline.transforms.strategy.edge_roles import EventEffect

        ee = EventEffect(
            trigger_event='draw-a-card',
            produced_event='life-loss-drain',
            provenance=Provenance(source='curated'),
        )
        assert ee.produced_event == 'life-loss-drain'

    @pytest.mark.parametrize(
        'attr_name,expected_trigger,expected_produced',
        [
            ('draw-to-drain', 'draw-a-card', 'life-loss-drain'),
            ('draw-to-gain', 'draw-a-card', 'lifegain'),
            ('gain-to-drain', 'lifegain', 'life-loss-drain'),
            ('drain-to-gain', 'life-loss-drain', 'lifegain'),
            ('gain-to-draw', 'lifegain', 'draw-a-card'),
            ('drain-to-draw', 'life-loss-drain', 'draw-a-card'),
        ],
        ids=[
            'draw-to-drain', 'draw-to-gain', 'gain-to-drain',
            'drain-to-gain', 'gain-to-draw', 'drain-to-draw',
        ],
    )
    def test_curated_round_trip_with_produced_event(
        self, attr_name: str, expected_trigger: str, expected_produced: str,
    ) -> None:
        """Every curated entry round-trips trigger_event + produced_event + provenance."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        entry = CURATED_EDGE_ROLES[attr_name]
        assert entry.trigger_event == expected_trigger
        assert entry.produced_event == expected_produced
        assert entry.provenance.source == 'curated'

    def test_get_role_returns_eventeffect_with_produced_event(self) -> None:
        """get_role returns an EventEffect with the 'produced_event' field."""
        from pipeline.transforms.strategy.edge_roles import EventEffect, get_role

        role = get_role('draw-to-drain')
        assert role is not None
        assert isinstance(role, EventEffect)
        assert role.produced_event == 'life-loss-drain'


# ────────────────────────────────────────────────────────────────────────────
# 2. Additive defaulted fields on EventEffect (scaffolding — inert)
# ────────────────────────────────────────────────────────────────────────────

class TestEventEffectDefaults:
    """New fields have inert defaults when only old fields are set."""

    def test_old_fields_only_gives_inert_defaults(self) -> None:
        """Constructing with only trigger/produced/provenance gives all defaults."""
        from pipeline.transforms.strategy.edge_roles import EventEffect

        ee = EventEffect(
            trigger_event='draw-a-card',
            produced_event='life-loss-drain',
            provenance=Provenance(source='curated'),
        )
        # Guard scaffolding — all at inert defaults
        assert ee.provider == 'card'
        assert ee.guard_characteristic_ref is None
        assert ee.guard_trigger_scope == 'any'
        assert ee.guard_identity == 'any'
        assert ee.guard_event_reachable_ref is None
        assert ee.guard_unresolved is False
        assert ee.magnitude is None
        assert ee.ability_key is None

    def test_curated_entries_all_have_inert_guard_defaults(self) -> None:
        """All 6 curated entries carry the inert guard defaults (scaffolding only)."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        for name, entry in CURATED_EDGE_ROLES.items():
            assert entry.provider == 'card', f'{name}: provider'
            assert entry.guard_characteristic_ref is None, f'{name}: guard_characteristic_ref'
            assert entry.guard_trigger_scope == 'any', f'{name}: guard_trigger_scope'
            assert entry.guard_identity == 'any', f'{name}: guard_identity'
            assert entry.guard_event_reachable_ref is None, f'{name}: guard_event_reachable_ref'
            assert entry.guard_unresolved is False, f'{name}: guard_unresolved'
            assert entry.magnitude is None, f'{name}: magnitude'
            assert entry.ability_key is None, f'{name}: ability_key'

    def test_eventeffect_is_frozen(self) -> None:
        """EventEffect remains a frozen dataclass."""
        from pipeline.transforms.strategy.edge_roles import EventEffect

        ee = EventEffect(
            trigger_event='draw-a-card',
            produced_event='life-loss-drain',
            provenance=Provenance(source='curated'),
        )
        with pytest.raises(AttributeError):
            ee.provider = 'rule'  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────────
# 3. Provenance.lifecycle field
# ────────────────────────────────────────────────────────────────────────────

class TestProvenanceLifecycle:
    """Provenance carries a 'lifecycle' field (provisional|verified)."""

    def test_lifecycle_field_exists(self) -> None:
        """Provenance has a 'lifecycle' attribute."""
        prov = Provenance(source='curated')
        assert hasattr(prov, 'lifecycle')

    def test_curated_default_is_verified(self) -> None:
        """Default lifecycle is 'verified' (curated provenance)."""
        prov = Provenance(source='curated')
        assert prov.lifecycle == 'verified'

    def test_haiku_default_lifecycle_is_provisional(self) -> None:
        """CardAttribute's default provenance carries lifecycle='provisional'."""
        from pipeline.transforms.strategy.machine_vocab import CardAttribute

        ma = CardAttribute(name='test', query='otag:test')
        assert ma.provenance.lifecycle == 'provisional'

    def test_explicit_lifecycle_overrides_default(self) -> None:
        """Lifecycle can be set explicitly."""
        prov = Provenance(source='haiku', lifecycle='verified')
        assert prov.lifecycle == 'verified'

    def test_curated_edge_roles_provenance_lifecycle(self) -> None:
        """All curated edge roles carry lifecycle='verified'."""
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES

        for name, entry in CURATED_EDGE_ROLES.items():
            assert entry.provenance.lifecycle == 'verified', (
                f'{name}: expected lifecycle="verified"'
            )


# ────────────────────────────────────────────────────────────────────────────
# 4. ProvisionalAttributeStore inertness (empty store behavior unchanged)
# ────────────────────────────────────────────────────────────────────────────

class TestProvisionalAttributeStoreInertness:
    """An empty ProvisionalAttributeStore is still inert after the scaffolding additions."""

    def test_empty_store_is_empty(self) -> None:
        from pipeline.transforms.strategy.machine_vocab import empty_store

        store = empty_store()
        assert store.is_empty is True

    def test_empty_store_names_empty(self) -> None:
        from pipeline.transforms.strategy.machine_vocab import empty_store

        assert empty_store().names() == frozenset()

    def test_empty_store_edge_queries_empty(self) -> None:
        from pipeline.transforms.strategy.machine_vocab import empty_store

        assert empty_store().edge_queries() == []

    def test_empty_store_get_returns_none(self) -> None:
        from pipeline.transforms.strategy.machine_vocab import empty_store

        assert empty_store().get('anything') is None


# ────────────────────────────────────────────────────────────────────────────
# 5. CardAttribute uses produced_event field name
# ────────────────────────────────────────────────────────────────────────────

class TestCardAttributeProducedEvent:
    """CardAttribute uses the renamed 'produced_event' field."""

    def test_produced_event_field(self) -> None:
        from pipeline.transforms.strategy.machine_vocab import CardAttribute

        ma = CardAttribute(
            name='test-edge',
            query='otag:test',
            trigger_event='draw-a-card',
            produced_event='life-loss-drain',
        )
        assert ma.produced_event == 'life-loss-drain'
        assert ma.trigger_event == 'draw-a-card'
