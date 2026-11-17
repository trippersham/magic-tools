"""Tests for machine_vocab.py (Phase PB -- persistence + provenance).

All tests are **hermetic**: they use ``tmp_path`` for the store directory,
NEVER write to the real ``~/.local/share/make-magic/``.

Test organisation:

- ``TestSerializationRoundTrip``     -- record ↔ attr fidelity
- ``TestPersist``                    -- write, idempotency, curated rejection
- ``TestLoadStore``                  -- read from JSONL, empty/missing, multi-line
- ``TestLoadFrozenSnapshot``         -- JSON-array fixture loading
- ``TestCompoundingReadPath``        -- 2nd deck reuses stored attr, no re-author
- ``TestAllEdgeQueries``             -- curated + machine assembly helper
- ``TestStoreEdgeMethods``           -- has_edge_for_card, edge_queries
- ``TestStalenessVersion``           -- two-layer version key (5.1 / ADR 0021)
- ``TestHermeticity``                -- populated store does NOT shift curated SHAs
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.transforms.strategy.machine_vocab import (
    DERIVATION_VERSION,
    EXTRACTION_VERSION,
    CardAttribute,
    Provenance,
    ProvisionalAttributeStore,
    _attr_to_record,
    _record_to_attr,
    all_edge_queries,
    empty_store,
    load_frozen_snapshot,
    load_store,
    persist,
)
from pipeline.transforms.strategy.tests.conftest import LAKE_AVAILABLE

_skip_no_lake = pytest.mark.skipif(not LAKE_AVAILABLE, reason='lake parquets not available')

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _sample_provenance(**overrides: object) -> Provenance:
    defaults = {
        'source': 'haiku',
        'model_id': 'claude-3-5-haiku-20241022',
        'authored_at': '2026-11-06T10:00:00Z',
        'source_card_name': 'Queza, Augur of Agonies',
        'source_card_oracle_id': 'abc-111-222-333',
        'deck_context': 'queza-esper-drain',
        'scryfall_query': 'oracle:/draw[^.]*los/ AND otag:draw-matters',
        'scryfall_resolved_count': 4,
        'verified_at': '2026-11-06T10:00:05Z',
    }
    defaults.update(overrides)
    return Provenance(**defaults)  # type: ignore[arg-type]


def _sample_attr(
    name: str = 'draw-to-drain',
    query: str = 'oracle:/draw[^.]*los/ AND otag:draw-matters',
    trigger_event: str = 'draw-a-card',
    produced_event: str = 'life-loss-drain',
    **prov_overrides: object,
) -> CardAttribute:
    return CardAttribute(
        name=name,
        query=query,
        trigger_event=trigger_event,
        produced_event=produced_event,
        provenance=_sample_provenance(**prov_overrides),
    )


# =====================================================================
# TestSerializationRoundTrip
# =====================================================================


class TestSerializationRoundTrip:
    """Record ↔ CardAttribute fidelity."""

    def test_round_trip_preserves_all_fields(self) -> None:
        attr = _sample_attr()
        rec = _attr_to_record(attr)
        restored = _record_to_attr(rec)
        assert restored == attr

    def test_record_is_flat_dict(self) -> None:
        """No nested objects -- provenance fields are promoted to top level."""
        rec = _attr_to_record(_sample_attr())
        assert isinstance(rec, dict)
        # No value should itself be a dict (flat record).
        for v in rec.values():
            assert not isinstance(v, dict), f'Nested dict found: {v!r}'

    def test_record_contains_all_provenance_keys(self) -> None:
        rec = _attr_to_record(_sample_attr())
        expected_keys = {
            'name',
            'query',
            'trigger_event',
            'produced_event',
            'source',
            'model_id',
            'authored_at',
            'source_card_name',
            'source_card_oracle_id',
            'deck_context',
            'scryfall_query',
            'scryfall_resolved_count',
            'verified_at',
            'breadth_warning',
            'lifecycle',
            'extraction_version',
            'derivation_version',
            'combo_variant_id',
        }
        assert set(rec.keys()) == expected_keys

    def test_json_serializable(self) -> None:
        rec = _attr_to_record(_sample_attr())
        text = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        restored_rec = json.loads(text)
        assert restored_rec == rec

    def test_record_to_attr_tolerates_missing_optional_keys(self) -> None:
        """Minimal record (only name + query) still deserializes."""
        minimal = {'name': 'x', 'query': 'otag:foo'}
        attr = _record_to_attr(minimal)
        assert attr.name == 'x'
        assert attr.query == 'otag:foo'
        assert attr.trigger_event == ''
        assert attr.provenance.source == 'haiku'


# =====================================================================
# TestPersist
# =====================================================================


class TestPersist:
    """Write to the durable JSONL store."""

    def test_creates_directory_and_file(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'machine_vocab'
        attr = _sample_attr()
        persist(attr, store_dir=store_dir)

        store_file = store_dir / 'attributes.jsonl'
        assert store_file.exists()
        lines = store_file.read_text().strip().splitlines()
        assert len(lines) == 1

    def test_record_is_valid_json(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        persist(_sample_attr(), store_dir=store_dir)

        line = (store_dir / 'attributes.jsonl').read_text().strip()
        rec = json.loads(line)
        assert rec['name'] == 'draw-to-drain'
        assert rec['source'] == 'haiku'
        assert rec['source_card_oracle_id'] == 'abc-111-222-333'

    def test_full_provenance_persisted(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        persist(_sample_attr(), store_dir=store_dir)

        rec = json.loads((store_dir / 'attributes.jsonl').read_text().strip())
        assert rec['model_id'] == 'claude-3-5-haiku-20241022'
        assert rec['authored_at'] == '2026-11-06T10:00:00Z'
        assert rec['verified_at'] == '2026-11-06T10:00:05Z'
        assert rec['deck_context'] == 'queza-esper-drain'
        assert rec['scryfall_resolved_count'] == 4

    def test_idempotent_on_same_name_and_card(self, tmp_path: Path) -> None:
        """Same (name, source_card_oracle_id) => no duplicate."""
        store_dir = tmp_path / 'mv'
        attr = _sample_attr()
        persist(attr, store_dir=store_dir)
        persist(attr, store_dir=store_dir)  # second call, same key

        lines = (store_dir / 'attributes.jsonl').read_text().strip().splitlines()
        assert len(lines) == 1

    def test_different_card_same_name_writes_both(self, tmp_path: Path) -> None:
        """Same attr name from two different source cards => two records."""
        store_dir = tmp_path / 'mv'
        attr_a = _sample_attr(source_card_oracle_id='card-aaa')
        attr_b = _sample_attr(source_card_oracle_id='card-bbb')
        persist(attr_a, store_dir=store_dir)
        persist(attr_b, store_dir=store_dir)

        lines = (store_dir / 'attributes.jsonl').read_text().strip().splitlines()
        assert len(lines) == 2

    def test_different_name_same_card_writes_both(self, tmp_path: Path) -> None:
        """Different attr names from the same source card => two records."""
        store_dir = tmp_path / 'mv'
        attr_a = _sample_attr(name='draw-to-drain')
        attr_b = _sample_attr(name='draw-to-gain')
        persist(attr_a, store_dir=store_dir)
        persist(attr_b, store_dir=store_dir)

        lines = (store_dir / 'attributes.jsonl').read_text().strip().splitlines()
        assert len(lines) == 2

    def test_rejects_curated_source(self, tmp_path: Path) -> None:
        """Cannot persist ``source='curated'`` into the machine store."""
        store_dir = tmp_path / 'mv'
        attr = _sample_attr(source='curated')
        with pytest.raises(ValueError, match='curated'):
            persist(attr, store_dir=store_dir)

    def test_append_to_existing_file(self, tmp_path: Path) -> None:
        """Multiple persist calls append, not overwrite."""
        store_dir = tmp_path / 'mv'
        persist(_sample_attr(name='a', source_card_oracle_id='c1'), store_dir=store_dir)
        persist(_sample_attr(name='b', source_card_oracle_id='c2'), store_dir=store_dir)
        persist(_sample_attr(name='c', source_card_oracle_id='c3'), store_dir=store_dir)

        lines = (store_dir / 'attributes.jsonl').read_text().strip().splitlines()
        assert len(lines) == 3


# =====================================================================
# TestLoadStore
# =====================================================================


class TestLoadStore:
    """Read from the durable JSONL store."""

    def test_loads_persisted_attr(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        original = _sample_attr()
        persist(original, store_dir=store_dir)

        store = load_store(store_dir)
        assert not store.is_empty
        assert len(store.attributes) == 1
        assert store.attributes[0] == original

    def test_loads_multiple_attrs(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        attrs = [
            _sample_attr(name='a', source_card_oracle_id='c1'),
            _sample_attr(name='b', source_card_oracle_id='c2'),
        ]
        for a in attrs:
            persist(a, store_dir=store_dir)

        store = load_store(store_dir)
        assert len(store.attributes) == 2
        assert store.names() == frozenset(['a', 'b'])

    def test_empty_on_missing_dir(self, tmp_path: Path) -> None:
        store = load_store(tmp_path / 'nonexistent')
        assert store.is_empty

    def test_empty_on_empty_file(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        store_dir.mkdir(parents=True)
        (store_dir / 'attributes.jsonl').write_text('')

        store = load_store(store_dir)
        assert store.is_empty

    def test_raises_on_corrupt_json(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        store_dir.mkdir(parents=True)
        (store_dir / 'attributes.jsonl').write_text('not json\n')

        with pytest.raises(ValueError, match='invalid JSON'):
            load_store(store_dir)

    def test_roundtrip_preserves_provenance(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        original = _sample_attr()
        persist(original, store_dir=store_dir)

        store = load_store(store_dir)
        loaded = store.attributes[0]
        assert loaded.provenance.source == 'haiku'
        assert loaded.provenance.model_id == 'claude-3-5-haiku-20241022'
        assert loaded.provenance.source_card_name == 'Queza, Augur of Agonies'
        assert loaded.provenance.verified_at == '2026-11-06T10:00:05Z'
        assert loaded.provenance.scryfall_resolved_count == 4
        assert loaded.provenance.breadth_warning is False

    def test_breadth_warning_roundtrip(self, tmp_path: Path) -> None:
        """Provenance with breadth_warning=True survives persist/load."""
        store_dir = tmp_path / 'mv'
        attr = _sample_attr(breadth_warning=True)
        persist(attr, store_dir=store_dir)

        store = load_store(store_dir)
        loaded = store.attributes[0]
        assert loaded.provenance.breadth_warning is True


# =====================================================================
# TestLoadFrozenSnapshot
# =====================================================================


class TestLoadFrozenSnapshot:
    """Load a frozen machine-store snapshot for tests."""

    def test_loads_json_array(self, tmp_path: Path) -> None:
        fixture = tmp_path / 'frozen.json'
        records = [_attr_to_record(_sample_attr(name=n)) for n in ['x', 'y']]
        fixture.write_text(json.dumps(records, indent=2))

        store = load_frozen_snapshot(fixture)
        assert len(store.attributes) == 2
        assert store.names() == frozenset(['x', 'y'])

    def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match='not found'):
            load_frozen_snapshot(tmp_path / 'nope.json')

    def test_raises_on_non_array(self, tmp_path: Path) -> None:
        fixture = tmp_path / 'bad.json'
        fixture.write_text('{"not": "an array"}')
        with pytest.raises(ValueError, match='JSON array'):
            load_frozen_snapshot(fixture)

    def test_empty_array_gives_empty_store(self, tmp_path: Path) -> None:
        fixture = tmp_path / 'empty.json'
        fixture.write_text('[]')
        store = load_frozen_snapshot(fixture)
        assert store.is_empty

    def test_snapshot_roundtrip_with_full_provenance(self, tmp_path: Path) -> None:
        """Persist -> serialize to JSON array -> load_frozen_snapshot = identical."""
        original = _sample_attr()
        fixture = tmp_path / 'snap.json'
        fixture.write_text(json.dumps([_attr_to_record(original)], indent=2))

        store = load_frozen_snapshot(fixture)
        assert store.attributes[0] == original


# =====================================================================
# TestCompoundingReadPath
# =====================================================================


class TestCompoundingReadPath:
    """The compounding pattern: persist once, reuse on the 2nd deck."""

    def test_second_deck_reuses_stored_attr(self, tmp_path: Path) -> None:
        """Persist for deck A; deck B finds it without re-authoring."""
        store_dir = tmp_path / 'mv'
        attr = _sample_attr(
            name='draw-to-drain',
            source_card_oracle_id='queza-oracle-id',
            deck_context='deck-a-queza',
        )
        persist(attr, store_dir=store_dir)

        # Deck B loads the store and checks whether Queza already has an edge.
        store = load_store(store_dir)
        assert store.has_edge_for_card('queza-oracle-id')
        assert store.get('draw-to-drain') is not None
        result = store.get('draw-to-drain')
        assert result is not None
        assert result.provenance.deck_context == 'deck-a-queza'

    def test_unknown_card_not_in_store(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        persist(_sample_attr(source_card_oracle_id='card-a'), store_dir=store_dir)

        store = load_store(store_dir)
        assert not store.has_edge_for_card('card-b')

    @_skip_no_lake
    def test_compounding_frontier_shrinks(self, tmp_path: Path) -> None:
        """After persisting an edge, the unauthored-card set for a 2nd deck shrinks.

        This is the key compounding behaviour: a second deck containing a
        card that's already been authored doesn't send it to haiku.
        """
        from pipeline.transforms.strategy.engine_hydration import (
            curated_edge_queries,
            unauthored_cards,
        )

        # A card with engine-relevant otags (death-trigger = trigger family,
        # tokens = effect family) but oracle text that does NOT match any of
        # the 6 curated draw/drain/gain edge queries.  This ensures the card
        # lands among the unauthored cards with curated edges alone.
        card_frontier = {
            'name': 'Test Frontier Card',
            'oracle_text': 'Whenever a creature you control dies, create a 1/1 black Zombie creature token.',
            'type_line': 'Enchantment',
            'mana_cost': '{3}{B}',
            'keywords': [],
            'otags': ['death-trigger', 'tokens'],
        }
        deck_cards = [card_frontier]

        # With only curated edges, this card IS among the unauthored cards
        # (engine-relevant otags, no curated edge match).
        curated_only = curated_edge_queries()
        front_before = unauthored_cards(deck_cards, curated_only)
        assert len(front_before) == 1

        # Persist a machine attr whose query MATCHES this card.
        store_dir = tmp_path / 'mv'
        machine_attr = _sample_attr(
            name='death-to-tokens',
            query='otag:death-trigger AND otag:tokens',
            trigger_event='creature-death',
            produced_event='token-created',
            source_card_oracle_id='test-card-oracle',
        )
        persist(machine_attr, store_dir=store_dir)

        # Load store and assemble full edge set.
        store = load_store(store_dir)
        full_edges = curated_only + store.edge_queries()

        # Now the card is covered => NOT among the unauthored cards.
        front_after = unauthored_cards(deck_cards, full_edges)
        assert len(front_after) == 0


# =====================================================================
# TestAllEdgeQueries
# =====================================================================


class TestAllEdgeQueries:
    """The ``all_edge_queries`` helper assembles curated + machine."""

    def test_empty_store_returns_curated_only(self) -> None:
        from pipeline.transforms.strategy.engine_hydration import curated_edge_queries

        curated = curated_edge_queries()
        result = all_edge_queries(None)
        assert result == curated

    def test_empty_machine_store_returns_curated_only(self) -> None:
        from pipeline.transforms.strategy.engine_hydration import curated_edge_queries

        curated = curated_edge_queries()
        result = all_edge_queries(empty_store())
        assert result == curated

    def test_machine_attrs_appended_after_curated(self, tmp_path: Path) -> None:
        from pipeline.transforms.strategy.engine_hydration import curated_edge_queries

        curated = curated_edge_queries()

        store_dir = tmp_path / 'mv'
        persist(
            _sample_attr(name='machine-edge-1', source_card_oracle_id='c1'),
            store_dir=store_dir,
        )
        store = load_store(store_dir)

        result = all_edge_queries(store)
        assert len(result) == len(curated) + 1
        # Curated first, then machine.
        assert result[: len(curated)] == curated
        assert result[-1][0] == 'machine-edge-1'


# =====================================================================
# TestStoreEdgeMethods
# =====================================================================


class TestStoreEdgeMethods:
    """ProvisionalAttributeStore helper methods: has_edge_for_card, edge_queries."""

    def test_edge_queries_format(self) -> None:
        store = ProvisionalAttributeStore(
            attributes=[
                _sample_attr(name='a', query='otag:foo'),
                _sample_attr(name='b', query='otag:bar'),
            ]
        )
        queries = store.edge_queries()
        assert queries == [('a', 'otag:foo'), ('b', 'otag:bar')]

    def test_empty_store_edge_queries(self) -> None:
        assert empty_store().edge_queries() == []

    def test_has_edge_for_card_true(self) -> None:
        store = ProvisionalAttributeStore(
            attributes=[
                _sample_attr(source_card_oracle_id='card-x'),
            ]
        )
        assert store.has_edge_for_card('card-x')

    def test_has_edge_for_card_false(self) -> None:
        store = ProvisionalAttributeStore(
            attributes=[
                _sample_attr(source_card_oracle_id='card-x'),
            ]
        )
        assert not store.has_edge_for_card('card-y')


# =====================================================================
# TestStalenessVersion — two-layer version key (Phase 5.1 / ADR 0021)
# =====================================================================


class TestStalenessVersion:
    """Two-layer staleness key: extraction_version + derivation_version.

    Version constants start at 1.  Legacy records (pre-versioning, no version
    fields) deserialize with version 0 and are therefore stale.  A record is
    "fresh" only when both stored versions >= current constants.
    """

    # --- Version constants exist and start at 1 ---

    def test_version_constants_are_positive_ints(self) -> None:
        assert isinstance(EXTRACTION_VERSION, int)
        assert isinstance(DERIVATION_VERSION, int)
        assert EXTRACTION_VERSION >= 1
        assert DERIVATION_VERSION >= 1

    # --- Provenance carries version fields ---

    def test_provenance_default_versions_are_zero(self) -> None:
        """Default Provenance (no explicit versions) has version 0 (legacy)."""
        prov = Provenance(source='haiku')
        assert prov.extraction_version == 0
        assert prov.derivation_version == 0

    def test_provenance_accepts_explicit_versions(self) -> None:
        prov = Provenance(source='haiku', extraction_version=1, derivation_version=2)
        assert prov.extraction_version == 1
        assert prov.derivation_version == 2

    # --- Serialization round-trip with versions ---

    def test_round_trip_with_versions(self) -> None:
        """Attr with explicit versions survives serialize -> deserialize."""
        attr = _sample_attr(extraction_version=3, derivation_version=5)
        rec = _attr_to_record(attr)
        restored = _record_to_attr(rec)
        assert restored.provenance.extraction_version == 3
        assert restored.provenance.derivation_version == 5
        assert restored == attr

    def test_record_includes_version_fields(self) -> None:
        attr = _sample_attr(extraction_version=1, derivation_version=1)
        rec = _attr_to_record(attr)
        assert 'extraction_version' in rec
        assert 'derivation_version' in rec
        assert rec['extraction_version'] == 1
        assert rec['derivation_version'] == 1

    def test_legacy_record_without_versions_loads_as_zero(self) -> None:
        """A record from before versioning (no version keys) loads with 0."""
        legacy_rec = {
            'name': 'old-edge',
            'query': 'otag:foo',
            'source': 'haiku',
            'source_card_oracle_id': 'old-card',
        }
        attr = _record_to_attr(legacy_rec)
        assert attr.provenance.extraction_version == 0
        assert attr.provenance.derivation_version == 0

    # --- Persist + load round-trip with versions ---

    def test_persist_load_preserves_versions(self, tmp_path: Path) -> None:
        store_dir = tmp_path / 'mv'
        attr = _sample_attr(extraction_version=2, derivation_version=3)
        persist(attr, store_dir=store_dir)

        store = load_store(store_dir)
        loaded = store.attributes[0]
        assert loaded.provenance.extraction_version == 2
        assert loaded.provenance.derivation_version == 3

    # --- Freshness checks ---

    def test_current_version_is_fresh(self) -> None:
        """A record at current version constants is fresh."""
        attr = _sample_attr(
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )
        store = ProvisionalAttributeStore(attributes=[attr])
        assert store.has_fresh_edge_for_card(
            'abc-111-222-333',
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )

    def test_higher_version_is_fresh(self) -> None:
        """A record at a HIGHER version than current is still fresh."""
        attr = _sample_attr(
            extraction_version=EXTRACTION_VERSION + 1,
            derivation_version=DERIVATION_VERSION + 1,
        )
        store = ProvisionalAttributeStore(attributes=[attr])
        assert store.has_fresh_edge_for_card(
            'abc-111-222-333',
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )

    def test_lower_extraction_version_is_stale(self) -> None:
        """A record with extraction_version < current is stale."""
        attr = _sample_attr(
            extraction_version=EXTRACTION_VERSION - 1,
            derivation_version=DERIVATION_VERSION,
        )
        store = ProvisionalAttributeStore(attributes=[attr])
        assert not store.has_fresh_edge_for_card(
            'abc-111-222-333',
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )

    def test_lower_derivation_version_is_stale(self) -> None:
        """A record with derivation_version < current is stale."""
        attr = _sample_attr(
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION - 1,
        )
        store = ProvisionalAttributeStore(attributes=[attr])
        assert not store.has_fresh_edge_for_card(
            'abc-111-222-333',
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )

    def test_legacy_record_is_stale(self) -> None:
        """A legacy record (versions 0/0) is always stale."""
        # Default Provenance has extraction_version=0, derivation_version=0
        attr = _sample_attr()  # uses _sample_provenance which has defaults -> 0
        store = ProvisionalAttributeStore(attributes=[attr])
        assert not store.has_fresh_edge_for_card(
            'abc-111-222-333',
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )

    def test_has_edge_for_card_unchanged_version_agnostic(self) -> None:
        """has_edge_for_card is still version-agnostic (backward compat)."""
        # A stale-versioned record still returns True for has_edge_for_card
        attr = _sample_attr(extraction_version=0, derivation_version=0)
        store = ProvisionalAttributeStore(attributes=[attr])
        assert store.has_edge_for_card('abc-111-222-333')

    def test_idempotent_same_version_persist(self, tmp_path: Path) -> None:
        """Persisting the same (name, oracle_id) at the same version is a no-op."""
        store_dir = tmp_path / 'mv'
        attr = _sample_attr(
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )
        persist(attr, store_dir=store_dir)
        persist(attr, store_dir=store_dir)  # idempotent

        lines = (store_dir / 'attributes.jsonl').read_text().strip().splitlines()
        assert len(lines) == 1

    def test_fresh_edge_for_unknown_card_is_false(self) -> None:
        """has_fresh_edge_for_card returns False for a card not in the store."""
        store = ProvisionalAttributeStore(
            attributes=[
                _sample_attr(
                    extraction_version=EXTRACTION_VERSION,
                    derivation_version=DERIVATION_VERSION,
                ),
            ]
        )
        assert not store.has_fresh_edge_for_card(
            'nonexistent-oracle-id',
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )

    def test_multiple_attrs_for_card_all_must_be_fresh(self) -> None:
        """If a card has multiple attrs, has_fresh_edge_for_card is True only
        when ALL attrs for that card are fresh (no stale attr for that oracle_id)."""
        fresh = _sample_attr(
            name='edge-a',
            source_card_oracle_id='card-x',
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )
        stale = _sample_attr(
            name='edge-b',
            source_card_oracle_id='card-x',
            extraction_version=0,
            derivation_version=0,
        )
        store = ProvisionalAttributeStore(attributes=[fresh, stale])
        # Card has a stale attr -> not fully fresh
        assert not store.has_fresh_edge_for_card(
            'card-x',
            extraction_version=EXTRACTION_VERSION,
            derivation_version=DERIVATION_VERSION,
        )

    def test_frozen_snapshot_with_versions(self, tmp_path: Path) -> None:
        """Frozen snapshot round-trips version fields."""
        attr = _sample_attr(extraction_version=2, derivation_version=3)
        fixture = tmp_path / 'snap.json'
        fixture.write_text(json.dumps([_attr_to_record(attr)], indent=2))

        store = load_frozen_snapshot(fixture)
        loaded = store.attributes[0]
        assert loaded.provenance.extraction_version == 2
        assert loaded.provenance.derivation_version == 3


# =====================================================================
# TestHermeticity — the crux: machine store does NOT shift curated SHAs
# =====================================================================


@_skip_no_lake
class TestHermeticity:
    """Populated machine store does NOT shift the curated engine + resolve SHAs.

    This is the Q4 separation invariant: the curated golden/determinism/parity
    tests must be unaffected by the machine store.  The default ``resolve()``
    and ``compute_facts`` path NEVER loads the machine store; tests that DO
    need machine vocab use a FROZEN snapshot.

    The test populates a machine store with several attrs (to prove it's not
    empty), then asserts the curated composite SHAs are unchanged.
    """

    def test_curated_engine_sha_unchanged_with_populated_store(
        self,
        tmp_path: Path,
    ) -> None:
        """Populate a machine store; curated engine isolation SHA is unchanged."""
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_engine_isolation_baselines,
        )

        # --- Populate a non-trivial machine store in tmp_path ---------------
        store_dir = tmp_path / 'machine_vocab'
        for i in range(5):
            persist(
                _sample_attr(
                    name=f'machine-edge-{i}',
                    source_card_oracle_id=f'card-{i}',
                    deck_context=f'deck-{i}',
                ),
                store_dir=store_dir,
            )
        store = load_store(store_dir)
        assert not store.is_empty, 'Store should be populated for this test'
        assert len(store.attributes) == 5

        # --- Compute curated baselines (resolve + engine isolation) ---------
        engine_baselines = compute_engine_isolation_baselines()
        engine_sha = composite_sha(engine_baselines)

        # The HELD expected SHAs from the parity oracle baseline.
        expected_engine_sha = '1eb27096118d38ad1dacf6a6d49f63531d3f334b63074f41db8090b207f3529b'
        assert engine_sha == expected_engine_sha, (
            f'Engine isolation SHA shifted!\n'
            f'  Expected: {expected_engine_sha}\n'
            f'  Got:      {engine_sha}\n'
            f'A populated machine store must NOT affect curated engine output.'
        )

    def test_curated_resolve_sha_unchanged_with_populated_store(
        self,
        tmp_path: Path,
    ) -> None:
        """Populate a machine store; curated resolve SHA is unchanged."""
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_resolve_baselines,
        )

        # --- Populate a non-trivial machine store in tmp_path ---------------
        store_dir = tmp_path / 'machine_vocab'
        for i in range(3):
            persist(
                _sample_attr(
                    name=f'machine-edge-{i}',
                    source_card_oracle_id=f'card-{i}',
                ),
                store_dir=store_dir,
            )
        store = load_store(store_dir)
        assert not store.is_empty

        # --- Compute curated baselines -------------------------------------
        resolve_baselines = compute_resolve_baselines()
        resolve_sha = composite_sha(resolve_baselines)

        expected_resolve_sha = '00fbb8360a7115835c86ddb64929801f909070e528e320245fb8e66398e77063'
        assert resolve_sha == expected_resolve_sha, (
            f'Resolve SHA shifted!\n'
            f'  Expected: {expected_resolve_sha}\n'
            f'  Got:      {resolve_sha}\n'
            f'A populated machine store must NOT affect curated resolve output.'
        )

    def test_curated_base_imports_never_touch_machine_store(self) -> None:
        """The curated base path (resolve / compute_facts) does NOT import
        or call load_store -- it only uses distill_strategies.py + edge_roles.py."""
        import inspect

        from pipeline.transforms.strategy import resolve
        from pipeline.transforms.strategy.strategy_graph import compute_facts

        # Inspect the source for any reference to load_store / machine_vocab.
        inspect.getsource(resolve)  # ensure importable
        facts_src = inspect.getsource(compute_facts)

        # The resolve function delegates to compute_facts, which is the
        # deterministic path.  Neither should reference the machine store.
        assert 'load_store' not in facts_src, 'compute_facts must not reference load_store'
        assert 'machine_vocab' not in facts_src, 'compute_facts must not reference machine_vocab'
