"""Tests for anchor-based adjacency derivation (Phase 6.1+6.2).

TDD gate tests — the anchor rule re-derives the committed adjacency, the
shadow stays off by default (P0 SHAs unchanged), and the derived lists are
a monotonic add over the enumerated lists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CATALOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'event_catalog_v4.json'


@pytest.fixture()
def catalog_events() -> dict[str, dict]:
    with open(_CATALOG_PATH) as f:
        return json.load(f)['events']


# ---------------------------------------------------------------------------
# 1. Mana-produced soundness (THE KEY GATE — write first, implement second)
# ---------------------------------------------------------------------------

class TestManaProducedSoundness:
    """The anchor rule re-derives mana-produced: mana-producer IN, ramp OUT.

    If derived produced_by re-admits ``ramp``, the rule is wrong → STOP.
    """

    def test_mana_produced_derived_includes_committed_producers(self) -> None:
        """Derived produced_by ⊇ committed produced_by for mana-produced."""
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        derived = derive_event_adjacency(events)

        committed = set(events['mana-produced']['produced_by'])
        derived_pb = set(derived['mana-produced']['produced_by'])

        assert committed <= derived_pb, (
            f'Derived produced_by lost committed slugs: {committed - derived_pb}'
        )

    def test_mana_produced_derived_excludes_ramp(self) -> None:
        """Derived produced_by does NOT contain ``ramp``."""
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        derived = derive_event_adjacency(events)

        assert 'ramp' not in derived['mana-produced']['produced_by'], (
            'ramp re-admitted to mana-produced derived produced_by — the rule is WRONG'
        )

    def test_mana_produced_derived_includes_mana_dork_descendants(self) -> None:
        """Derived produced_by includes mana-producer descendants (mana-dork etc)."""
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        derived = derive_event_adjacency(events)
        derived_pb = set(derived['mana-produced']['produced_by'])

        # mana-dork is a child of mana-producer (anchor), so it must appear
        assert 'mana-dork' in derived_pb
        assert 'mana-rock' in derived_pb

    def test_mana_produced_anchors_match_adr0020(self) -> None:
        """The produce-side anchors for mana-produced match ADR 0020's committed set."""
        from pipeline.transforms.strategy.anchor_derivation import ANCHOR_MAP

        anchors = set(ANCHOR_MAP['mana-produced']['produced_by']['anchors'])
        expected = {'adds-multiple-mana', 'mana-producer', 'repeatable-treasures', 'ritual'}
        assert anchors == expected, (
            f'Anchor mismatch: got {anchors}, expected {expected}'
        )


# ---------------------------------------------------------------------------
# 2. Anchor map completeness + grades
# ---------------------------------------------------------------------------

class TestAnchorMapCompleteness:
    """The 27-event anchor map covers every catalog event."""

    def test_every_event_has_anchor_entry(self, catalog_events: dict[str, dict]) -> None:
        from pipeline.transforms.strategy.anchor_derivation import ANCHOR_MAP

        for event_name in catalog_events:
            assert event_name in ANCHOR_MAP, (
                f'Event {event_name!r} missing from ANCHOR_MAP'
            )

    def test_anchor_map_has_27_events(self) -> None:
        from pipeline.transforms.strategy.anchor_derivation import ANCHOR_MAP
        assert len(ANCHOR_MAP) == 27

    def test_each_entry_has_both_sides_and_grade(self) -> None:
        from pipeline.transforms.strategy.anchor_derivation import ANCHOR_MAP

        for event_name, entry in ANCHOR_MAP.items():
            assert 'triggers' in entry, f'{event_name}: missing triggers'
            assert 'produced_by' in entry, f'{event_name}: missing produced_by'
            for side in ('triggers', 'produced_by'):
                side_data = entry[side]
                assert 'anchors' in side_data, f'{event_name}.{side}: missing anchors'
                assert 'grade' in side_data, f'{event_name}.{side}: missing grade'
                assert side_data['grade'] in ('S', 'M', 'empty'), (
                    f'{event_name}.{side}: invalid grade {side_data["grade"]!r}'
                )

    def test_grade_tally_matches_build_design(self) -> None:
        """15 S-bearing, 8 fully-M, 4 ambiguous/empty.

        The build-design notes "16 + mana-prod T-adjacent" — mana-produced's
        T side is graded M (multi-concept: mana-sink + mana-increaser) so the
        formal tally is 15/8/4.  The build-design's "+T-adjacent" is a
        borderline note, not a grade upgrade.
        """
        from pipeline.transforms.strategy.anchor_derivation import ANCHOR_MAP

        has_s = 0
        fully_m = 0
        has_empty = 0
        for entry in ANCHOR_MAP.values():
            t_grade = entry['triggers']['grade']
            p_grade = entry['produced_by']['grade']
            if t_grade == 'S' or p_grade == 'S':
                has_s += 1
            elif t_grade == 'empty' or p_grade == 'empty':
                has_empty += 1
            else:
                fully_m += 1

        assert has_s == 15, f'Expected 15 S-bearing events, got {has_s}'
        assert fully_m == 8, f'Expected 8 fully-M events, got {fully_m}'
        assert has_empty == 4, f'Expected 4 ambiguous/empty events, got {has_empty}'


# ---------------------------------------------------------------------------
# 3. Ancestor-closure: 0 violations
# ---------------------------------------------------------------------------

class TestAncestorClosure:
    """Taxonomy anchors exist; synthetic catalog slugs are known exceptions."""

    #: Slugs in the committed event catalog that are NOT Scryfall otags —
    #: synthetic bucket names or shorthand.  They have no taxonomy entry and
    #: their descendant-closure is just themselves (backward-compat passthrough).
    _SYNTHETIC_CATALOG_SLUGS = frozenset({
        'artifacts', 'clones', 'combat', 'counters',
        'rad-counter', 'sac', 'tokens',
    })

    def test_all_non_synthetic_anchors_exist_in_taxonomy(self) -> None:
        from pipeline.transforms.strategy.anchor_derivation import (
            ANCHOR_MAP,
            load_taxonomy,
        )

        taxonomy = load_taxonomy()
        missing: list[str] = []
        for event_name, entry in ANCHOR_MAP.items():
            for side in ('triggers', 'produced_by'):
                for slug in entry[side]['anchors']:
                    if slug not in taxonomy and slug not in self._SYNTHETIC_CATALOG_SLUGS:
                        missing.append(f'{event_name}.{side}: {slug}')

        assert not missing, f'Anchors missing from taxonomy (and not synthetic): {missing}'

    def test_synthetic_slugs_not_in_taxonomy(self) -> None:
        """Synthetic slugs are indeed absent from the taxonomy (guard)."""
        from pipeline.transforms.strategy.anchor_derivation import load_taxonomy

        taxonomy = load_taxonomy()
        for slug in self._SYNTHETIC_CATALOG_SLUGS:
            assert slug not in taxonomy, (
                f'Synthetic slug {slug!r} now EXISTS in taxonomy — '
                f'review whether it should stay in _SYNTHETIC_CATALOG_SLUGS'
            )

    def test_descendant_closure_terminates(self) -> None:
        """Descendant closure on every anchor terminates (no cycles)."""
        from pipeline.transforms.strategy.anchor_derivation import (
            ANCHOR_MAP,
            descendant_closure,
            load_taxonomy,
        )

        taxonomy = load_taxonomy()
        for _event_name, entry in ANCHOR_MAP.items():
            for side in ('triggers', 'produced_by'):
                for slug in entry[side]['anchors']:
                    # Should not raise or loop forever
                    result = descendant_closure(slug, taxonomy)
                    assert slug in result, f'{slug} not in its own closure'


# ---------------------------------------------------------------------------
# 4. Derived ⊇ enumerated (slug-level superset, per event)
# ---------------------------------------------------------------------------

class TestDerivedSupersetOfEnumerated:
    """Derived lists ⊇ enumerated for every event (no loss)."""

    def test_derived_produced_by_superset(self, catalog_events: dict[str, dict]) -> None:
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        for event_name, ev in catalog_events.items():
            enum_pb = set(ev['produced_by'])
            derived_pb = set(derived[event_name]['produced_by'])
            missing = enum_pb - derived_pb
            assert not missing, (
                f'{event_name}: derived produced_by lost enumerated slugs: {missing}'
            )

    def test_derived_triggers_superset(self, catalog_events: dict[str, dict]) -> None:
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        for event_name, ev in catalog_events.items():
            enum_t = set(ev['triggers'])
            derived_t = set(derived[event_name]['triggers'])
            missing = enum_t - derived_t
            assert not missing, (
                f'{event_name}: derived triggers lost enumerated slugs: {missing}'
            )


# ---------------------------------------------------------------------------
# 5. Unlock on 116 role-A edges (~63% target)
# ---------------------------------------------------------------------------

# Walk up from tests/ -> strategy/ -> transforms/ -> pipeline/ -> pipeline(root)/ -> subagents/
_EDGES_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / 'subagents' / '5.3b-work'


class TestUnlockOnRoleAEdges:
    """Derived-adjacency corroboration on the 116 role-A edges exceeds 63%."""

    @pytest.fixture()
    def role_a_edges(self) -> list[dict]:
        """Load both-sided edges from the 5.3b chunks."""
        import glob

        edges: list[dict] = []
        for f in sorted(glob.glob(str(_EDGES_DIR / 'edges' / 'chunk-*-edges.jsonl'))):
            with open(f) as fh:
                for line in fh:
                    e = json.loads(line)
                    if e.get('trigger_event') and e.get('produced_event'):
                        edges.append(e)
        return edges

    @pytest.fixture()
    def residue_otags(self) -> dict[str, set[str]]:
        """Load oracle_id -> otags from residue_cards.jsonl."""
        result: dict[str, set[str]] = {}
        residue_path = _EDGES_DIR / 'residue_cards.jsonl'
        with open(residue_path) as f:
            for line in f:
                c = json.loads(line)
                result[c['oracle_id']] = set(c.get('otags', []))
        return result

    def _corroborates(
        self,
        edge: dict,
        ev_dict: dict[str, dict],
        otag_map: dict[str, set[str]],
    ) -> bool:
        otags = otag_map.get(edge['oracle_id'], set())
        t_list = set(ev_dict.get(edge['trigger_event'], {}).get('triggers', []))
        p_list = set(ev_dict.get(edge['produced_event'], {}).get('produced_by', []))
        return bool(otags & t_list) and bool(otags & p_list)

    @pytest.mark.skipif(
        not (_EDGES_DIR / 'edges').exists(),
        reason='5.3b edge data not available',
    )
    def test_derived_unlock_exceeds_63_percent(
        self,
        catalog_events: dict[str, dict],
        role_a_edges: list[dict],
        residue_otags: dict[str, set[str]],
    ) -> None:
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)

        n_edges = len(role_a_edges)
        assert n_edges == 116, f'Expected 116 edges, got {n_edges}'

        enum_corr = sum(
            1 for e in role_a_edges
            if self._corroborates(e, catalog_events, residue_otags)
        )
        derived_corr = sum(
            1 for e in role_a_edges
            if self._corroborates(e, derived, residue_otags)
        )

        enum_pct = enum_corr / n_edges * 100
        derived_pct = derived_corr / n_edges * 100

        assert derived_corr >= enum_corr, (
            f'Derived ({derived_corr}) lost edges vs enumerated ({enum_corr})'
        )
        assert derived_pct >= 63.0, (
            f'Derived unlock {derived_pct:.1f}% < 63% target '
            f'({derived_corr}/{n_edges}; enumerated was {enum_pct:.1f}%)'
        )


# ---------------------------------------------------------------------------
# 6. P0 SHA unchanged (shadow OFF by default)
# ---------------------------------------------------------------------------

class TestP0SHAUnchanged:
    """With shadow OFF (default), the P0 parity SHAs are unchanged."""

    def test_shadow_flag_is_off(self) -> None:
        from pipeline.transforms.strategy.anchor_derivation import SHADOW_DERIVED_ADJACENCY
        assert SHADOW_DERIVED_ADJACENCY is False

    def test_engine_parity_sha_unchanged(self) -> None:
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_engine_isolation_baselines,
        )

        baselines = compute_engine_isolation_baselines()
        sha = composite_sha(baselines)
        assert sha == '987f2660f04421720f5e0b3162a17d313fb309a476dad482554b8f376bdf181c', (
            f'Engine isolation SHA shifted: {sha}'
        )

    def test_resolve_parity_sha_unchanged(self) -> None:
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_resolve_baselines,
        )

        baselines = compute_resolve_baselines()
        sha = composite_sha(baselines)
        assert sha == '85752bb9361e03e4fce0eeb11bf4a989d45cc4ec839840f7975877072a3f5a1c', (
            f'Resolve SHA shifted: {sha}'
        )


# ---------------------------------------------------------------------------
# 6. Derived adjacency shape / invariants
# ---------------------------------------------------------------------------

class TestDerivedAdjacencyShape:
    """Derived events dict has the same shape as the original."""

    def test_derived_preserves_all_event_keys(
        self, catalog_events: dict[str, dict],
    ) -> None:
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        assert set(derived.keys()) == set(catalog_events.keys())

    def test_derived_preserves_rule_field(
        self, catalog_events: dict[str, dict],
    ) -> None:
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        for event_name in catalog_events:
            assert derived[event_name]['rule'] == catalog_events[event_name]['rule']

    def test_derived_lists_are_sorted(
        self, catalog_events: dict[str, dict],
    ) -> None:
        from pipeline.transforms.strategy.anchor_derivation import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        for event_name, ev in derived.items():
            assert ev['produced_by'] == sorted(ev['produced_by']), (
                f'{event_name}: derived produced_by not sorted'
            )
            assert ev['triggers'] == sorted(ev['triggers']), (
                f'{event_name}: derived triggers not sorted'
            )
