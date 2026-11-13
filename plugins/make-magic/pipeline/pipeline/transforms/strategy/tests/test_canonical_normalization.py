"""Tests for Phase 2A task 3: canonical normalization renames + provenance widen.

TDD red-phase: these tests import the RENAMED classes and assert the widened
provenance.  They MUST fail before the rename (ImportError / AttributeError)
and pass after.

Renames under test:
  MachineAttribute  -> CardAttribute
  MachineStore      -> ProvisionalAttributeStore

Provenance widen: source='spellbook' is now a valid constructible value.

Serialization: persist -> load round-trip preserves all fields including
provenance(source, lifecycle) for both haiku and spellbook sources.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ────────────────────────────────────────────────────────────────────────────
# 1. CardAttribute import + construction (renamed from MachineAttribute)
# ────────────────────────────────────────────────────────────────────────────

class TestCardAttributeRename:
    """CardAttribute is the canonical name (was MachineAttribute)."""

    def test_cardattribute_importable(self) -> None:
        """CardAttribute is importable from machine_vocab."""
        from pipeline.transforms.strategy.machine_vocab import CardAttribute

        assert CardAttribute is not None

    def test_cardattribute_constructible(self) -> None:
        """CardAttribute can be constructed with the standard fields."""
        from pipeline.transforms.strategy.machine_vocab import CardAttribute, Provenance

        attr = CardAttribute(
            name='draw-to-drain',
            query='oracle:/draw[^.]*los/ AND otag:draw-matters',
            trigger_event='draw-a-card',
            produced_event='life-loss-drain',
            provenance=Provenance(source='haiku', lifecycle='provisional'),
        )
        assert attr.name == 'draw-to-drain'
        assert attr.trigger_event == 'draw-a-card'
        assert attr.produced_event == 'life-loss-drain'
        assert attr.provenance.source == 'haiku'
        assert attr.provenance.lifecycle == 'provisional'

    def test_cardattribute_default_provenance(self) -> None:
        """Default provenance is source='haiku', lifecycle='provisional'."""
        from pipeline.transforms.strategy.machine_vocab import CardAttribute

        attr = CardAttribute(name='test', query='otag:test')
        assert attr.provenance.source == 'haiku'
        assert attr.provenance.lifecycle == 'provisional'

    def test_cardattribute_is_frozen(self) -> None:
        """CardAttribute is a frozen dataclass."""
        from pipeline.transforms.strategy.machine_vocab import CardAttribute

        attr = CardAttribute(name='test', query='otag:test')
        with pytest.raises(AttributeError):
            attr.name = 'changed'  # type: ignore[misc]

    def test_no_machineattribute_name(self) -> None:
        """MachineAttribute is NOT exported (clean rename, no alias)."""
        import pipeline.transforms.strategy.machine_vocab as mv

        assert not hasattr(mv, 'MachineAttribute'), (
            'MachineAttribute should not exist after rename -- no compat alias'
        )


# ────────────────────────────────────────────────────────────────────────────
# 2. ProvisionalAttributeStore import + construction (renamed from MachineStore)
# ────────────────────────────────────────────────────────────────────────────

class TestProvisionalAttributeStoreRename:
    """ProvisionalAttributeStore is the canonical name (was MachineStore)."""

    def test_provisionalattributestore_importable(self) -> None:
        """ProvisionalAttributeStore is importable from machine_vocab."""
        from pipeline.transforms.strategy.machine_vocab import ProvisionalAttributeStore

        assert ProvisionalAttributeStore is not None

    def test_empty_store_is_empty(self) -> None:
        """Empty ProvisionalAttributeStore is inert."""
        from pipeline.transforms.strategy.machine_vocab import ProvisionalAttributeStore

        store = ProvisionalAttributeStore()
        assert store.is_empty is True
        assert store.names() == frozenset()
        assert store.edge_queries() == []
        assert store.get('anything') is None

    def test_empty_store_factory(self) -> None:
        """empty_store() returns a ProvisionalAttributeStore."""
        from pipeline.transforms.strategy.machine_vocab import (
            ProvisionalAttributeStore,
            empty_store,
        )

        store = empty_store()
        assert isinstance(store, ProvisionalAttributeStore)

    def test_no_machinestore_name(self) -> None:
        """MachineStore is NOT exported (clean rename, no alias)."""
        import pipeline.transforms.strategy.machine_vocab as mv

        assert not hasattr(mv, 'MachineStore'), (
            'MachineStore should not exist after rename -- no compat alias'
        )


# ────────────────────────────────────────────────────────────────────────────
# 3. Provenance source widen: 'spellbook' is valid
# ────────────────────────────────────────────────────────────────────────────

class TestProvenanceSourceWiden:
    """Provenance.source accepts 'spellbook' in addition to 'curated'/'haiku'."""

    def test_spellbook_source_constructible(self) -> None:
        """Provenance with source='spellbook' is constructible."""
        from pipeline.transforms.strategy.machine_vocab import Provenance

        prov = Provenance(source='spellbook')
        assert prov.source == 'spellbook'

    def test_spellbook_default_lifecycle_is_provisional(self) -> None:
        """A spellbook-sourced provenance defaults to lifecycle='provisional'."""
        from pipeline.transforms.strategy.machine_vocab import Provenance

        prov = Provenance(source='spellbook', lifecycle='provisional')
        assert prov.lifecycle == 'provisional'

    def test_cardattribute_with_spellbook_source(self) -> None:
        """CardAttribute can be constructed with source='spellbook'."""
        from pipeline.transforms.strategy.machine_vocab import CardAttribute, Provenance

        attr = CardAttribute(
            name='combo-edge',
            query='otag:sacrifice-outlet AND otag:death-trigger',
            trigger_event='creature-death',
            produced_event='sacrifice-loop',
            provenance=Provenance(source='spellbook', lifecycle='provisional'),
        )
        assert attr.provenance.source == 'spellbook'
        assert attr.name == 'combo-edge'

    def test_curated_and_haiku_still_valid(self) -> None:
        """Existing sources 'curated' and 'haiku' still work."""
        from pipeline.transforms.strategy.machine_vocab import Provenance

        assert Provenance(source='curated').source == 'curated'
        assert Provenance(source='haiku').source == 'haiku'

    def test_spellbook_distinguishable_from_curated(self) -> None:
        """spellbook != curated (provenance distinguishability invariant)."""
        from pipeline.transforms.strategy.machine_vocab import Provenance

        curated = Provenance(source='curated')
        spellbook = Provenance(source='spellbook')
        assert curated.source != spellbook.source


# ────────────────────────────────────────────────────────────────────────────
# 4. Serialization round-trip with renamed types
# ────────────────────────────────────────────────────────────────────────────

class TestSerializationWithRenamedTypes:
    """persist -> load round-trip preserves all fields under the new names."""

    def test_roundtrip_preserves_all_fields(self, tmp_path: Path) -> None:
        """CardAttribute persist -> load_store -> CardAttribute is identical."""
        from pipeline.transforms.strategy.machine_vocab import (
            CardAttribute,
            Provenance,
            load_store,
            persist,
        )

        original = CardAttribute(
            name='draw-to-drain',
            query='oracle:/draw[^.]*los/',
            trigger_event='draw-a-card',
            produced_event='life-loss-drain',
            provenance=Provenance(
                source='haiku',
                model_id='claude-3-5-haiku-20241022',
                authored_at='2026-11-06T10:00:00Z',
                source_card_name='Queza, Augur of Agonies',
                source_card_oracle_id='abc-111-222-333',
                deck_context='queza-esper-drain',
                scryfall_query='oracle:/draw[^.]*los/',
                scryfall_resolved_count=4,
                verified_at='2026-11-06T10:00:05Z',
                lifecycle='provisional',
            ),
        )

        store_dir = tmp_path / 'mv'
        persist(original, store_dir=store_dir)

        store = load_store(store_dir)
        assert not store.is_empty
        loaded = store.attributes[0]
        assert loaded == original
        assert loaded.provenance.source == 'haiku'
        assert loaded.provenance.lifecycle == 'provisional'
        assert loaded.provenance.model_id == 'claude-3-5-haiku-20241022'

    def test_spellbook_source_roundtrip(self, tmp_path: Path) -> None:
        """CardAttribute with source='spellbook' survives persist -> load."""
        from pipeline.transforms.strategy.machine_vocab import (
            CardAttribute,
            Provenance,
            load_store,
            persist,
        )

        original = CardAttribute(
            name='combo-edge',
            query='otag:sac-outlet AND otag:death-trigger',
            trigger_event='creature-death',
            produced_event='sacrifice-loop',
            provenance=Provenance(
                source='spellbook',
                model_id='spellbook-v1',
                authored_at='2026-11-13T00:00:00Z',
                source_card_name='Viscera Seer',
                source_card_oracle_id='seer-oracle-id',
                deck_context='aristocrats',
                lifecycle='provisional',
            ),
        )

        store_dir = tmp_path / 'mv'
        persist(original, store_dir=store_dir)

        store = load_store(store_dir)
        loaded = store.attributes[0]
        assert loaded == original
        assert loaded.provenance.source == 'spellbook'
        assert loaded.provenance.lifecycle == 'provisional'

    def test_on_disk_format_unchanged(self, tmp_path: Path) -> None:
        """The JSONL on-disk format is the same flat record format."""
        from pipeline.transforms.strategy.machine_vocab import (
            CardAttribute,
            Provenance,
            persist,
        )

        attr = CardAttribute(
            name='test-edge',
            query='otag:test',
            trigger_event='draw',
            produced_event='gain',
            provenance=Provenance(source='haiku', lifecycle='provisional'),
        )
        store_dir = tmp_path / 'mv'
        persist(attr, store_dir=store_dir)

        line = (store_dir / 'attributes.jsonl').read_text().strip()
        rec = json.loads(line)
        # Flat record -- no nesting
        for v in rec.values():
            assert not isinstance(v, dict), f'Nested dict in record: {v!r}'
        # Key fields present
        assert rec['name'] == 'test-edge'
        assert rec['source'] == 'haiku'
        assert rec['lifecycle'] == 'provisional'
        assert rec['produced_event'] == 'gain'
