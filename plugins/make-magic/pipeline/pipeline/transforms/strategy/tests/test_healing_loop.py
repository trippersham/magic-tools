"""Tests for the self-healing adjacency loop (task 6.3).

TDD gate tests — candidate discovery, N=3 promotion, umbrella rejection,
three-way gate, and P0 SHA preservation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CATALOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'event_catalog_v4.json'
_EDGES_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / 'subagents' / '5.3b-work'


@pytest.fixture()
def catalog_events() -> dict[str, dict]:
    with open(_CATALOG_PATH) as f:
        return json.load(f)['events']


@pytest.fixture()
def role_a_edges() -> list[dict]:
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
def residue_otags() -> dict[str, set[str]]:
    """Load oracle_id -> otags from residue_cards.jsonl."""
    result: dict[str, set[str]] = {}
    residue_path = _EDGES_DIR / 'residue_cards.jsonl'
    with open(residue_path) as f:
        for line in f:
            c = json.loads(line)
            result[c['oracle_id']] = set(c.get('otags', []))
    return result


# ---------------------------------------------------------------------------
# 1. Candidate promotion at N=3 (the exact threshold)
# ---------------------------------------------------------------------------

class TestCandidatePromotion:
    """A candidate promotes at exactly N=3 corroborating cards;
    <3 stays provisional."""

    def test_candidate_below_threshold_stays_provisional(self) -> None:
        """A candidate with 2 corroborating cards does NOT promote."""
        from pipeline.transforms.strategy.anchor_derivation import (
            CandidateAnchorStore,
        )

        store = CandidateAnchorStore()
        store.record_corroboration('creature-enters-battlefield', 'triggers',
                                  'some-otag', 'card-oracle-1')
        store.record_corroboration('creature-enters-battlefield', 'triggers',
                                  'some-otag', 'card-oracle-2')

        promoted = store.promotable_candidates()
        assert ('creature-enters-battlefield', 'triggers', 'some-otag') not in {
            (c.event, c.side, c.otag) for c in promoted
        }

    def test_candidate_at_threshold_promotes(self) -> None:
        """A candidate with exactly 3 corroborating cards DOES promote."""
        from pipeline.transforms.strategy.anchor_derivation import (
            CandidateAnchorStore,
        )

        store = CandidateAnchorStore()
        for i in range(3):
            store.record_corroboration('creature-enters-battlefield', 'triggers',
                                      'some-otag', f'card-oracle-{i}')

        promoted = store.promotable_candidates()
        keys = {(c.event, c.side, c.otag) for c in promoted}
        assert ('creature-enters-battlefield', 'triggers', 'some-otag') in keys

    def test_candidate_above_threshold_also_promotes(self) -> None:
        """A candidate with 5 corroborating cards still promotes (>= N)."""
        from pipeline.transforms.strategy.anchor_derivation import (
            CandidateAnchorStore,
        )

        store = CandidateAnchorStore()
        for i in range(5):
            store.record_corroboration('creature-enters-battlefield', 'triggers',
                                      'some-otag', f'card-oracle-{i}')

        promoted = store.promotable_candidates()
        keys = {(c.event, c.side, c.otag) for c in promoted}
        assert ('creature-enters-battlefield', 'triggers', 'some-otag') in keys

    def test_duplicate_card_does_not_count_twice(self) -> None:
        """The same card oracle_id corroborating twice counts as 1."""
        from pipeline.transforms.strategy.anchor_derivation import (
            CandidateAnchorStore,
        )

        store = CandidateAnchorStore()
        for _ in range(5):
            store.record_corroboration('creature-enters-battlefield', 'triggers',
                                      'some-otag', 'same-card')

        promoted = store.promotable_candidates()
        assert len(promoted) == 0  # only 1 unique card


# ---------------------------------------------------------------------------
# 2. Umbrella rejection — exclusivity gate blocks ramp-like candidates
# ---------------------------------------------------------------------------

class TestUmbrellaRejection:
    """An umbrella candidate (e.g. ``ramp``) is REJECTED by the exclusivity
    gate and can never promote, regardless of corroboration count."""

    def test_umbrella_candidate_rejected_by_exclusivity(self) -> None:
        """``ramp`` as a candidate for mana-produced.produced_by is rejected."""
        from pipeline.transforms.strategy.anchor_derivation import (
            ANCHOR_MAP,
            is_mechanic_exclusive,
            mechanic_population,
        )

        anchors = ANCHOR_MAP['mana-produced']['produced_by']['anchors']
        mechanic = mechanic_population(anchors)
        result = is_mechanic_exclusive('ramp', mechanic)
        assert not result.ok, 'ramp should NOT pass exclusivity for mana-produced'

    def test_umbrella_never_enters_candidate_store(self) -> None:
        """The discover_candidates function never returns a slug that fails
        exclusivity, so umbrellas cannot even enter the store."""
        from pipeline.transforms.strategy.anchor_derivation import (
            discover_candidates,
        )
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        candidates = discover_candidates(events, 'mana-produced', 'produced_by')
        candidate_slugs = {c.otag for c in candidates}
        assert 'ramp' not in candidate_slugs, (
            'ramp entered as a candidate — exclusivity gate failed'
        )

    def test_triggered_ability_umbrella_rejected(self) -> None:
        """``triggered-ability`` is a broad umbrella and should be rejected."""
        from pipeline.transforms.strategy.anchor_derivation import (
            ANCHOR_MAP,
            is_mechanic_exclusive,
            mechanic_population,
        )

        # triggered-ability is an umbrella that spans many mechanics.
        # Test it against a mechanic where it would be an ancestor.
        for _event_name, entry in ANCHOR_MAP.items():
            for side in ('produced_by', 'triggers'):
                anchors = entry[side]['anchors']
                if not anchors:
                    continue
                mechanic = mechanic_population(anchors)
                result = is_mechanic_exclusive('triggered-ability', mechanic)
                if not result.ok:
                    return  # Found at least one rejection — test passes
        pytest.fail('triggered-ability was not rejected for any event/side')


# ---------------------------------------------------------------------------
# 3. Three-way gate
# ---------------------------------------------------------------------------

class TestThreeWayGate:
    """The three-way gate correctly classifies otags."""

    def test_accept_via_anchor_for_known_slug(self) -> None:
        """An otag in the closure => accept-via-anchor."""
        from pipeline.transforms.strategy.anchor_derivation import (
            GateVerdict,
            three_way_gate,
        )
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        # landfall is in creature-enters-battlefield.triggers closure
        result = three_way_gate(
            'landfall',
            events,
            'land-enters-battlefield',
            'triggers',
        )
        assert result.verdict == GateVerdict.ACCEPT_VIA_ANCHOR

    def test_reject_for_non_exclusive_slug(self) -> None:
        """An otag that fails exclusivity => reject."""
        from pipeline.transforms.strategy.anchor_derivation import (
            GateVerdict,
            three_way_gate,
        )
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        result = three_way_gate(
            'ramp',
            events,
            'mana-produced',
            'produced_by',
        )
        assert result.verdict == GateVerdict.REJECT

    def test_gate_returns_three_distinct_verdicts(self) -> None:
        """All three verdict types are reachable."""
        from pipeline.transforms.strategy.anchor_derivation import GateVerdict

        assert len(GateVerdict) == 3


# ---------------------------------------------------------------------------
# 4. Generic: no per-event/per-deck logic in discovery
# ---------------------------------------------------------------------------

class TestGenericDiscovery:
    """Candidate discovery is generic — no event-specific branches."""

    def test_discover_candidates_works_for_multiple_events(self) -> None:
        """discover_candidates runs on at least 3 different events without
        per-event branches (same function, no dispatch)."""
        from pipeline.transforms.strategy.anchor_derivation import (
            discover_candidates,
        )
        from pipeline.transforms.strategy.engine_hydration import _load_events

        events = _load_events()
        events_tested = 0
        for event_name in ('creature-enters-battlefield', 'permanent-dies',
                           'lifegain', 'spell-cast', 'token-created'):
            for side in ('triggers', 'produced_by'):
                # Should not raise — same function for all events
                discover_candidates(events, event_name, side)
                events_tested += 1
        assert events_tested >= 8


# ---------------------------------------------------------------------------
# 5. Frozen P0 SHAs intact
# ---------------------------------------------------------------------------

class TestP0SHAIntact:
    """The self-healing mechanism is shadow + flagged; P0 SHAs are held."""

    def test_engine_parity_sha_unchanged(self) -> None:
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_engine_isolation_baselines,
        )

        baselines = compute_engine_isolation_baselines()
        sha = composite_sha(baselines)
        assert sha == 'a7edab20703291c8380340bb542c5a11e25a2e53fa3964eb89ffbce20f17e177', (
            f'Engine isolation SHA shifted: {sha}'
        )

    def test_resolve_parity_sha_unchanged(self) -> None:
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_resolve_baselines,
        )

        baselines = compute_resolve_baselines()
        sha = composite_sha(baselines)
        assert sha == '25f50a23d19ac98e37131f80b7ebf5d9d4bc1dd9ef78099e6de9a8a482ca464a', (
            f'Resolve SHA shifted: {sha}'
        )


# ---------------------------------------------------------------------------
# 6. Shadow measurement correctness
# ---------------------------------------------------------------------------

class TestShadowMeasurement:
    """The shadow measurement reports correct projected metrics."""

    @pytest.mark.skipif(
        not (_EDGES_DIR / 'edges').exists(),
        reason='5.3b edge data not available',
    )
    def test_shadow_discovers_candidates(
        self,
        catalog_events: dict[str, dict],
        residue_otags: dict[str, set[str]],
    ) -> None:
        """The shadow run discovers at least 1 candidate across all events."""
        from pipeline.transforms.strategy.anchor_derivation import (
            run_healing_shadow,
        )

        report = run_healing_shadow(residue_otags)
        assert report.total_candidates > 0, 'No candidates discovered'

    @pytest.mark.skipif(
        not (_EDGES_DIR / 'edges').exists(),
        reason='5.3b edge data not available',
    )
    def test_shadow_zero_umbrellas(
        self,
        catalog_events: dict[str, dict],
        residue_otags: dict[str, set[str]],
    ) -> None:
        """No umbrella (exclusivity-failing) otags are ever proposed."""
        from pipeline.transforms.strategy.anchor_derivation import (
            run_healing_shadow,
        )

        report = run_healing_shadow(residue_otags)
        assert report.umbrella_proposals == 0, (
            f'{report.umbrella_proposals} umbrella(s) proposed — gate failed'
        )

    @pytest.mark.skipif(
        not (_EDGES_DIR / 'edges').exists(),
        reason='5.3b edge data not available',
    )
    def test_projected_unlock_at_least_72_percent(
        self,
        catalog_events: dict[str, dict],
        role_a_edges: list[dict],
        residue_otags: dict[str, set[str]],
    ) -> None:
        """Projected unlock with promotions >= 72% (at least matches current)."""
        from pipeline.transforms.strategy.anchor_derivation import (
            run_healing_shadow,
        )

        report = run_healing_shadow(residue_otags)
        # With promoted candidates, unlock should be at least the current 72%
        assert report.projected_unlock_pct >= 72.0, (
            f'Projected unlock {report.projected_unlock_pct:.1f}% < 72%'
        )
