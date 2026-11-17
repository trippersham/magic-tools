"""Tests for seed-based adjacency derivation (Phase 6.1+6.2).

TDD gate tests — the seed rule re-derives the committed adjacency, the
shadow stays off by default (P0 SHAs unchanged), and the derived lists are
a monotonic add over the enumerated lists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.transforms.strategy.tests.conftest import LAKE_AVAILABLE

_skip_no_lake = pytest.mark.skipif(not LAKE_AVAILABLE, reason='lake parquets not available')

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


@_skip_no_lake
class TestManaProducedSoundness:
    """The seed rule re-derives mana-produced: mana-producer IN, ramp OUT.

    If derived produced_by re-admits ``ramp``, the rule is wrong → STOP.
    """

    def test_mana_produced_derived_includes_committed_producers(self) -> None:
        """Derived produced_by ⊇ committed produced_by for mana-produced."""
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        derived = derive_event_adjacency(events)

        committed = set(events['mana-produced']['produced_by'])
        derived_pb = set(derived['mana-produced']['produced_by'])

        assert committed <= derived_pb, f'Derived produced_by lost committed slugs: {committed - derived_pb}'

    def test_mana_produced_derived_excludes_ramp(self) -> None:
        """Derived produced_by does NOT contain ``ramp``."""
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        derived = derive_event_adjacency(events)

        assert 'ramp' not in derived['mana-produced']['produced_by'], (
            'ramp re-admitted to mana-produced derived produced_by — the rule is WRONG'
        )

    def test_mana_produced_derived_includes_mana_dork_descendants(self) -> None:
        """Derived produced_by includes mana-producer descendants (mana-dork etc)."""
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        derived = derive_event_adjacency(events)
        derived_pb = set(derived['mana-produced']['produced_by'])

        # mana-dork is a child of mana-producer (seed), so it must appear
        assert 'mana-dork' in derived_pb
        assert 'mana-rock' in derived_pb

    def test_mana_produced_anchors_match_adr0020(self) -> None:
        """The produce-side seeds for mana-produced match ADR 0020's committed set."""
        from pipeline.transforms.strategy.adjacency import SEED_MAP

        seeds = set(SEED_MAP['mana-produced']['produced_by']['seeds'])
        expected = {'adds-multiple-mana', 'mana-producer', 'repeatable-treasures', 'ritual'}
        assert seeds == expected, f'Seed mismatch: got {seeds}, expected {expected}'


# ---------------------------------------------------------------------------
# 2. Seed map completeness + grades
# ---------------------------------------------------------------------------


class TestAnchorMapCompleteness:
    """The 27-event seed map covers every catalog event."""

    def test_every_event_has_anchor_entry(self, catalog_events: dict[str, dict]) -> None:
        from pipeline.transforms.strategy.adjacency import SEED_MAP

        for event_name in catalog_events:
            assert event_name in SEED_MAP, f'Event {event_name!r} missing from SEED_MAP'

    def test_anchor_map_has_27_events(self) -> None:
        from pipeline.transforms.strategy.adjacency import SEED_MAP

        assert len(SEED_MAP) == 27

    def test_each_entry_has_both_sides_and_grade(self) -> None:
        from pipeline.transforms.strategy.adjacency import SEED_MAP

        for event_name, entry in SEED_MAP.items():
            assert 'triggers' in entry, f'{event_name}: missing triggers'
            assert 'produced_by' in entry, f'{event_name}: missing produced_by'
            for side in ('triggers', 'produced_by'):
                side_data = entry[side]
                assert 'seeds' in side_data, f'{event_name}.{side}: missing seeds'
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
        from pipeline.transforms.strategy.adjacency import SEED_MAP

        has_s = 0
        fully_m = 0
        has_empty = 0
        for entry in SEED_MAP.values():
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


@_skip_no_lake
class TestAncestorClosure:
    """Taxonomy seeds exist; synthetic catalog slugs are known exceptions."""

    #: Slugs in the committed event catalog that are NOT Scryfall otags —
    #: synthetic bucket names or shorthand.  They have no taxonomy entry and
    #: their descendant-closure is just themselves (backward-compat passthrough).
    _SYNTHETIC_CATALOG_SLUGS = frozenset(
        {
            'artifacts',
            'clones',
            'combat',
            'counters',
            'rad-counter',
            'sac',
            'tokens',
        }
    )

    def test_all_non_synthetic_anchors_exist_in_taxonomy(self) -> None:
        from pipeline.transforms.strategy.adjacency import (
            SEED_MAP,
            load_taxonomy,
        )

        taxonomy = load_taxonomy()
        missing: list[str] = []
        for event_name, entry in SEED_MAP.items():
            for side in ('triggers', 'produced_by'):
                for slug in entry[side]['seeds']:
                    if slug not in taxonomy and slug not in self._SYNTHETIC_CATALOG_SLUGS:
                        missing.append(f'{event_name}.{side}: {slug}')

        assert not missing, f'Anchors missing from taxonomy (and not synthetic): {missing}'

    def test_synthetic_slugs_not_in_taxonomy(self) -> None:
        """Synthetic slugs are indeed absent from the taxonomy (guard)."""
        from pipeline.transforms.strategy.adjacency import load_taxonomy

        taxonomy = load_taxonomy()
        for slug in self._SYNTHETIC_CATALOG_SLUGS:
            assert slug not in taxonomy, (
                f'Synthetic slug {slug!r} now EXISTS in taxonomy — '
                f'review whether it should stay in _SYNTHETIC_CATALOG_SLUGS'
            )

    def test_descendant_closure_terminates(self) -> None:
        """Descendant closure on every seed terminates (no cycles)."""
        from pipeline.transforms.strategy.adjacency import (
            SEED_MAP,
            descendant_closure,
            load_taxonomy,
        )

        taxonomy = load_taxonomy()
        for _event_name, entry in SEED_MAP.items():
            for side in ('triggers', 'produced_by'):
                for slug in entry[side]['seeds']:
                    # Should not raise or loop forever
                    result = descendant_closure(slug, taxonomy)
                    assert slug in result, f'{slug} not in its own closure'


# ---------------------------------------------------------------------------
# 4. Derived ⊇ enumerated (slug-level superset, per event)
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestDerivedSupersetOfEnumerated:
    """Derived lists ⊇ enumerated for every event, except slugs the exclusivity
    rule refuses as cross-mechanic umbrellas.

    The committed ``event_catalog_v4.json`` (ADR 0012 byte-parity) still lists
    ``ramp`` in ``land-enters-battlefield.produced_by``.  The seed rule now
    REFUSES ``ramp`` there (it dilutes the land mechanic), so the single
    documented superset exception is exactly the rule's refused set — not a
    hardcode.  When the catalog is re-based (task 6.5 flip), the exception
    disappears on its own.
    """

    def test_derived_produced_by_superset(self, catalog_events: dict[str, dict]) -> None:
        from pipeline.transforms.strategy.adjacency import (
            SEED_MAP,
            derive_event_adjacency,
            refused_climb_targets,
        )

        derived = derive_event_adjacency(catalog_events)
        for event_name, ev in catalog_events.items():
            enum_pb = set(ev['produced_by'])
            derived_pb = set(derived[event_name]['produced_by'])
            # The exclusivity rule may refuse enumerated slugs that are
            # cross-mechanic umbrellas (e.g. `ramp`); those are allowed drops.
            seeds = SEED_MAP.get(event_name, {}).get('produced_by', {}).get('seeds', [])
            refused = set(refused_climb_targets(seeds)) if seeds else set()
            missing = (enum_pb - derived_pb) - refused
            assert not missing, f'{event_name}: derived produced_by lost enumerated slugs: {missing}'

    def test_derived_triggers_superset(self, catalog_events: dict[str, dict]) -> None:
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        for event_name, ev in catalog_events.items():
            enum_t = set(ev['triggers'])
            derived_t = set(derived[event_name]['triggers'])
            missing = enum_t - derived_t
            assert not missing, f'{event_name}: derived triggers lost enumerated slugs: {missing}'


# ---------------------------------------------------------------------------
# 5. Unlock on 116 edge-author edges (~63% target)
# ---------------------------------------------------------------------------

# Walk up from tests/ -> strategy/ -> transforms/ -> pipeline/ -> pipeline(root)/ -> subagents/
_EDGES_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / 'subagents' / '5.3b-work'


@_skip_no_lake
class TestUnlockOnRoleAEdges:
    """Derived-adjacency corroboration on the 116 edge-author edges exceeds 63%."""

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
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)

        n_edges = len(role_a_edges)
        assert n_edges == 116, f'Expected 116 edges, got {n_edges}'

        enum_corr = sum(1 for e in role_a_edges if self._corroborates(e, catalog_events, residue_otags))
        derived_corr = sum(1 for e in role_a_edges if self._corroborates(e, derived, residue_otags))

        enum_pct = enum_corr / n_edges * 100
        derived_pct = derived_corr / n_edges * 100

        assert derived_corr >= enum_corr, f'Derived ({derived_corr}) lost edges vs enumerated ({enum_corr})'
        assert derived_pct >= 63.0, (
            f'Derived unlock {derived_pct:.1f}% < 63% target ({derived_corr}/{n_edges}; enumerated was {enum_pct:.1f}%)'
        )


# ---------------------------------------------------------------------------
# 6. P0 SHA re-baselined (derived adjacency authoritative — ADR 0022, task 6.5)
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestP0SHAUnchanged:
    """The re-baselined parity SHAs (derived adjacency + rank-don't-cut now the
    default path; the ``SHADOW_DERIVED_ADJACENCY`` flag is retired)."""

    def test_adjacency_is_derived_at_load(self) -> None:
        # The flip is authoritative: the live catalog's event adjacency equals the
        # derived closure-union, not the frozen enumerated lists (ADR 0022).
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency
        from pipeline.transforms.strategy.assets import load_assets

        events = load_assets().events
        derived = derive_event_adjacency(events)
        for ev in events:
            assert sorted(events[ev]['produced_by']) == sorted(derived[ev]['produced_by'])
            assert sorted(events[ev]['triggers']) == sorted(derived[ev]['triggers'])

    def test_engine_parity_sha_unchanged(self) -> None:
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_engine_isolation_baselines,
        )

        baselines = compute_engine_isolation_baselines()
        sha = composite_sha(baselines)
        assert sha == '7a73aba98b9fb877a30cc7ac97649d7d8935a673750d40a35b0c0be383095442', (
            f'Engine isolation SHA shifted: {sha}'
        )

    def test_resolve_parity_sha_unchanged(self) -> None:
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_resolve_baselines,
        )

        baselines = compute_resolve_baselines()
        sha = composite_sha(baselines)
        assert sha == '1e54293cd148f2fd01ce5fc59834361dbb082da8d692ecc8048537237691a59a', f'Resolve SHA shifted: {sha}'


# ---------------------------------------------------------------------------
# 6. Derived adjacency shape / invariants
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestDerivedAdjacencyShape:
    """Derived events dict has the same shape as the original."""

    def test_derived_preserves_all_event_keys(
        self,
        catalog_events: dict[str, dict],
    ) -> None:
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        assert set(derived.keys()) == set(catalog_events.keys())

    def test_derived_preserves_rule_field(
        self,
        catalog_events: dict[str, dict],
    ) -> None:
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        for event_name in catalog_events:
            assert derived[event_name]['rule'] == catalog_events[event_name]['rule']

    def test_derived_lists_are_sorted(
        self,
        catalog_events: dict[str, dict],
    ) -> None:
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency

        derived = derive_event_adjacency(catalog_events)
        for event_name, ev in derived.items():
            assert ev['produced_by'] == sorted(ev['produced_by']), f'{event_name}: derived produced_by not sorted'
            assert ev['triggers'] == sorted(ev['triggers']), f'{event_name}: derived triggers not sorted'


# ---------------------------------------------------------------------------
# 7. Exclusivity validator — the seed rule as ENFORCED code (task 6.1b)
# ---------------------------------------------------------------------------


@_skip_no_lake
class TestExclusivityValidator:
    """``ramp`` is auto-excluded from mana-produced AND land-enters by the
    generic mechanic-exclusivity rule — no per-event special-casing.

    The rule: a seed/climb-target X is admitted to an event side only if
    ``is_mechanic_exclusive(X, M)`` holds, where ``M`` is the card population
    the side's own seeds establish.  ``ramp`` fails this for every side whose
    mechanic it would dilute — the same predicate, same thresholds, for both
    events.  This is the generic proof the exclusion is enforced, not the
    hand-removal it replaces.
    """

    def test_is_mechanic_exclusive_rejects_ramp_both_events_generic(self) -> None:
        """``ramp`` is non-exclusive for mana-produced AND land-enters.

        Found by iterating every event/side (no ``if event == ...``): for every
        side where ``ramp`` is a proper ancestor of a listed seed, the SAME
        predicate must classify ``ramp`` as non-exclusive.  Both historically
        relevant sides must be discovered and must fail.
        """
        from pipeline.transforms.strategy.adjacency import (
            SEED_MAP,
            is_mechanic_exclusive,
            load_taxonomy,
            mechanic_population,
            proper_ancestors,
        )

        taxonomy = load_taxonomy()
        ramp_sides: list[tuple[str, str]] = []
        for event_name, entry in SEED_MAP.items():
            for side in ('produced_by', 'triggers'):
                seeds = entry[side]['seeds']
                if not seeds:
                    continue
                reaches_ramp = any('ramp' in proper_ancestors(a, taxonomy) for a in seeds)
                if not reaches_ramp:
                    continue
                m = mechanic_population(seeds)
                res = is_mechanic_exclusive('ramp', m)
                assert not res.ok, (
                    f'{event_name}.{side}: ramp passed exclusivity (precision={res.precision:.3f}) — the rule is WRONG'
                )
                ramp_sides.append((event_name, side))

        # The two historically relevant sides MUST be among those discovered.
        assert ('mana-produced', 'produced_by') in ramp_sides
        assert ('land-enters-battlefield', 'produced_by') in ramp_sides

    def test_ramp_absent_from_every_derived_output(self) -> None:
        """Output purity: ``ramp`` is in NO derived produced_by/triggers list."""
        from pipeline.transforms.strategy.adjacency import (
            derive_event_adjacency,
        )
        from pipeline.transforms.strategy.engine_hydration import _load_events

        derived = derive_event_adjacency(_load_events())
        for event_name, ev in derived.items():
            assert 'ramp' not in ev['produced_by'], f'{event_name}: ramp leaked into derived produced_by'
            assert 'ramp' not in ev['triggers'], f'{event_name}: ramp leaked into derived triggers'

    def test_mana_producer_is_mechanic_exclusive(self) -> None:
        """mana-producer passes exclusivity for mana-produced (the kept seed)."""
        from pipeline.transforms.strategy.adjacency import (
            SEED_MAP,
            is_mechanic_exclusive,
            mechanic_population,
        )

        seeds = SEED_MAP['mana-produced']['produced_by']['seeds']
        m = mechanic_population(seeds)
        assert is_mechanic_exclusive('mana-producer', m).ok, (
            'mana-producer wrongly flagged non-exclusive for mana-produced'
        )

    def test_validate_anchor_map_clean(self) -> None:
        """The committed seed map passes the exclusivity validator (0 violations)."""
        from pipeline.transforms.strategy.adjacency import validate_seed_map

        violations = validate_seed_map()
        assert violations == [], f'Seed-map exclusivity violations: {violations}'

    def test_derived_output_byte_identical_to_enumerated_expansion(
        self,
        catalog_events: dict[str, dict],
    ) -> None:
        """The exclusivity layer does NOT change the derived output.

        derive_event_adjacency still returns the closure-union of the curated
        seeds, so the shadow election is byte-identical to the measured
        (ramp-corrected) hand map — the re-confirmed shadow numbers carry over.
        """
        from pipeline.transforms.strategy.adjacency import (
            SEED_MAP,
            derive_event_adjacency,
            descendant_closure,
            load_taxonomy,
        )

        taxonomy = load_taxonomy()
        derived = derive_event_adjacency(catalog_events)
        for event_name, entry in SEED_MAP.items():
            for side in ('produced_by', 'triggers'):
                seeds = entry[side]['seeds']
                if not seeds:
                    continue
                expected: set[str] = set()
                for a in seeds:
                    expected |= descendant_closure(a, taxonomy)
                assert set(derived[event_name][side]) == expected, (
                    f'{event_name}.{side}: derived output drifted from the curated closure-union'
                )

    def test_ramp_precision_margins(self) -> None:
        """The empirical margins: ramp precision < PREC_MIN for both events.

        Confirms the measured 0.56 (mana) / 0.28 (land) over-capture figures
        that motivated the exclusion.
        """
        from pipeline.transforms.strategy.adjacency import (
            EXCLUSIVITY_PRECISION_MIN,
            SEED_MAP,
            is_mechanic_exclusive,
            mechanic_population,
        )

        for event_name in ('mana-produced', 'land-enters-battlefield'):
            seeds = SEED_MAP[event_name]['produced_by']['seeds']
            m = mechanic_population(seeds)
            res = is_mechanic_exclusive('ramp', m)
            assert res.precision < EXCLUSIVITY_PRECISION_MIN, (
                f'{event_name}: ramp precision {res.precision:.3f} >= {EXCLUSIVITY_PRECISION_MIN}'
            )

    def test_ramp_as_seed_caught_by_validator(self) -> None:
        """Adding ``ramp`` as a seed to land-enters.produced_by or
        mana-produced.produced_by MUST make ``validate_seed_map()`` return a
        violation.

        This is the NON-TAUTOLOGICAL acceptance test: the leave-one-out
        exclusivity check catches a cross-mechanic umbrella hand-listed as a
        seed — the validator is no longer a tautology.
        """
        import copy

        from pipeline.transforms.strategy.adjacency import (
            SEED_MAP,
            validate_seed_map,
        )

        for event_name in ('land-enters-battlefield', 'mana-produced'):
            poisoned = copy.deepcopy(SEED_MAP)
            poisoned[event_name]['produced_by']['seeds'].append('ramp')
            violations = validate_seed_map(seed_map=poisoned)
            ramp_violations = [v for v in violations if 'ramp' in v]
            assert ramp_violations, (
                f'Adding ramp as a seed to {event_name}.produced_by did NOT '
                f'trigger a violation — the validator is tautological. '
                f'All violations: {violations}'
            )

    def test_climb_to_frontier_removed(self) -> None:
        """``climb_to_frontier`` is deleted (was dead code with zero callers)."""
        import pipeline.transforms.strategy.adjacency as ad

        assert not hasattr(ad, 'climb_to_frontier'), (
            'climb_to_frontier still exists — it was dead code and should be deleted'
        )


# ---------------------------------------------------------------------------
# 8. event_admitted gate test (Gate-2 test gap)
# ---------------------------------------------------------------------------


class TestEventAdmittedGate:
    """``event_admitted`` gates the mana-produced engine unit into the mass
    election based on whether the command zone seeds mana production.

    A mana commander (commander carries a producer_anchor ref like
    ``mana-producer``) passes; a non-mana commander whose mana unit lacks the
    seed is refused.
    """

    def test_mana_commander_admitted(self) -> None:
        """A commander with a mana-production attr admits mana-produced."""
        from pipeline.transforms.strategy.assets import load_assets
        from pipeline.transforms.strategy.substantiveness import event_admitted

        assets = load_assets()
        # A commander carrying 'mana-producer' (or 'ramp' via extra_refs)
        # seeds mana-produced election candidacy.
        cmd_attrs_mana = {'mana-producer', 'lifegain-matters'}
        assert event_admitted('mana-produced', cmd_attrs_mana, assets), (
            'A mana-producer commander should admit mana-produced'
        )

    def test_ramp_commander_admitted(self) -> None:
        """A commander carrying 'ramp' (via extra_refs) still seeds mana."""
        from pipeline.transforms.strategy.assets import load_assets
        from pipeline.transforms.strategy.substantiveness import event_admitted

        assets = load_assets()
        # 'ramp' is in extra_refs for mana-produced (seed-side-only).
        cmd_attrs_ramp = {'ramp', 'landfall'}
        assert event_admitted('mana-produced', cmd_attrs_ramp, assets), (
            'A ramp-only commander (e.g. Derevi) should still admit mana-produced via the extra_refs seed'
        )

    def test_non_mana_commander_refused(self) -> None:
        """A commander with no mana-production attrs refuses mana-produced."""
        from pipeline.transforms.strategy.assets import load_assets
        from pipeline.transforms.strategy.substantiveness import event_admitted

        assets = load_assets()
        # A pure combat commander carries no mana-production ref.
        cmd_attrs_combat = {'attack-trigger', 'saboteur', 'gives-haste'}
        assert not event_admitted('mana-produced', cmd_attrs_combat, assets), (
            'A non-mana commander should refuse mana-produced election candidacy'
        )

    def test_non_gated_event_always_admitted(self) -> None:
        """Events without a substantiveness bar are always admitted."""
        from pipeline.transforms.strategy.assets import load_assets
        from pipeline.transforms.strategy.substantiveness import event_admitted

        assets = load_assets()
        # creature-enters-battlefield has no EVENT_SUBSTANTIVENESS entry.
        cmd_attrs_empty: set[str] = set()
        assert event_admitted('creature-enters-battlefield', cmd_attrs_empty, assets), (
            'An ungated event should always be admitted'
        )
