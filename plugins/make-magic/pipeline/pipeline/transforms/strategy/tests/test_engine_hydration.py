"""Tests for engine_hydration.py (Phase PA).

All tests are **hermetic**: no live network, no DuckDB lake access.  The
fixture search client (``make_fixture_search_client``) runs compiled query
predicates against in-memory card lists.

Test organisation:

- ``TestEngineRelevantOtags`` — catalog-derived otag universes
- ``TestCuratedEdgeQueries``  — curated edge-query helper
- ``TestFrontier``            — frontier computation on the Queza relay fixture
- ``TestVerifyPasteability``  — check (a): RE2 guard
- ``TestVerifyResolution``    — check (b): resolves + expected hit
- ``TestVerifyCorroboration`` — check (c): over-capture oracle
- ``TestVerifyEndToEnd``      — full-gate pass / reject
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.transforms.strategy.engine_hydration import (
    CORROBORATION_THRESHOLD,
    SYNTH_BREADTH_WARN,
    VerificationResult,
    curated_edge_queries,
    engine_relevant_otags,
    frontier,
    make_fixture_search_client,
    verify,
)

FIX = Path(__file__).resolve().parent / 'fixtures'


def _load_relay_fixture() -> dict:
    return json.loads(
        (FIX / 'relay_decks' / 'relay__queza-lichs-mastery.json').read_text()
    )


# ---- Synthetic card builders for verify tests -----------------------------

def _card(
    name: str, oracle_text: str, otags: list[str],
    *, keywords: list[str] | None = None,
    type_line: str = 'Creature', mana_cost: str = '{1}',
) -> dict:
    return {
        'name': name,
        'oracle_text': oracle_text,
        'otags': otags,
        'keywords': keywords or [],
        'type_line': type_line,
        'mana_cost': mana_cost,
    }


# =====================================================================
# TestEngineRelevantOtags
# =====================================================================

class TestEngineRelevantOtags:
    """Catalog-derived TRIGGER and EFFECT otag universes."""

    def test_returns_nonempty_frozensets(self) -> None:
        trigger, effect = engine_relevant_otags()
        assert isinstance(trigger, frozenset) and len(trigger) > 0
        assert isinstance(effect, frozenset) and len(effect) > 0

    def test_known_trigger_otags_present(self) -> None:
        trigger, _ = engine_relevant_otags()
        assert 'draw-matters' in trigger
        assert 'lifegain-matters' in trigger
        assert 'life-loss-matters' in trigger
        assert 'landfall' in trigger
        assert 'death-trigger' in trigger

    def test_known_effect_otags_present(self) -> None:
        _, effect = engine_relevant_otags()
        assert 'lifegain' in effect
        assert 'draw' in effect
        assert 'tokens' in effect
        assert 'drain-life' in effect
        # `ramp` is the cross-mechanic umbrella (admits mana-producer/ritual) and
        # is DELIBERATELY NOT an effect otag under derived adjacency (ADR 0022):
        # the mechanic-exclusive producer of a land-enter is `land-ramp`, not the
        # generic `ramp` the enumerated catalog over-counted.
        assert 'ramp' not in effect
        assert 'land-ramp' in effect

    def test_some_otags_in_both(self) -> None:
        """Some otags (like ``sac``) can be both trigger and effect."""
        trigger, effect = engine_relevant_otags()
        # `sac` is a produced_by for several events and a triggers entry for
        # token-created, so it appears in both.
        assert 'sac' in (trigger & effect)


# =====================================================================
# TestCuratedEdgeQueries
# =====================================================================

class TestCuratedEdgeQueries:

    def test_returns_six_queries(self) -> None:
        queries = curated_edge_queries()
        assert len(queries) == 6

    def test_all_names_match_edge_roles(self) -> None:
        from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES
        queries = curated_edge_queries()
        names = {n for n, _ in queries}
        assert names == set(CURATED_EDGE_ROLES.keys())

    def test_all_queries_compile(self) -> None:
        """Every curated edge query must compile without error."""
        from pipeline.transforms.strategy.query import compile_query
        for name, query in curated_edge_queries():
            compile_query(query)  # raises on failure


# =====================================================================
# TestFrontier
# =====================================================================

class TestFrontier:
    """Frontier computation on the Queza relay fixture."""

    def test_covered_cards_excluded(self) -> None:
        """Cards matching a curated edge query are NOT in the frontier."""
        fixture = _load_relay_fixture()
        edges = curated_edge_queries()
        result = frontier(fixture['cards'], edges)
        names = {c['name'] for c in result}

        # These match curated edge queries (draw-to-drain, draw-to-gain,
        # gain-to-draw, gain-to-drain, drain-to-gain):
        assert 'Queza, Augur of Agonies' not in names
        assert "Lich's Mastery" not in names
        assert 'Sanguine Bond' not in names
        assert 'Exquisite Blood' not in names
        assert 'Drogskol Reaver' not in names
        assert 'Gain-Draw Relay Filler' not in names

    def test_uncovered_engine_relevant_cards_included(self) -> None:
        """Engine-relevant cards NOT matching any edge query are in the frontier."""
        fixture = _load_relay_fixture()
        edges = curated_edge_queries()
        result = frontier(fixture['cards'], edges)
        names = {c['name'] for c in result}

        # These have engine-relevant otags but no curated edge query matches:
        assert 'Vilis, Broker of Blood' in names
        assert 'Archangel of Thune' in names
        assert 'Well of Lost Dreams' in names
        assert 'Trudge Garden' in names
        assert 'Rhox Faithmender' in names
        assert "Erebos's Intervention" in names
        assert "Cleric of Life's Bond" in names

    def test_lands_excluded(self) -> None:
        """Basic lands have no engine-relevant otags, so they never appear."""
        fixture = _load_relay_fixture()
        edges = curated_edge_queries()
        result = frontier(fixture['cards'], edges)
        names = {c['name'] for c in result}

        assert 'Island' not in names
        assert 'Swamp' not in names
        assert 'Plains' not in names

    def test_empty_edges_all_relevant_in_frontier(self) -> None:
        """With no authored edges, every engine-relevant card is uncovered."""
        fixture = _load_relay_fixture()
        result = frontier(fixture['cards'], [])
        names = {c['name'] for c in result}

        # Every card with engine-relevant otags should appear.
        assert 'Queza, Augur of Agonies' in names
        assert "Lich's Mastery" in names
        assert 'Exquisite Blood' in names
        # Lands still excluded.
        assert 'Island' not in names

    def test_empty_deck_returns_empty(self) -> None:
        assert frontier([], curated_edge_queries()) == []

    def test_preserves_deck_order(self) -> None:
        """Frontier cards appear in the same order as the input deck."""
        fixture = _load_relay_fixture()
        edges = curated_edge_queries()
        result = frontier(fixture['cards'], edges)

        # Collect the original indices of frontier cards.
        frontier_names = [c['name'] for c in result]
        deck_names = [c['name'] for c in fixture['cards']]
        indices = [deck_names.index(n) for n in frontier_names]
        assert indices == sorted(indices)


# =====================================================================
# TestVerifyPasteability
# =====================================================================

class TestVerifyPasteability:
    """Check (a): RE2 pasteability guard."""

    def test_valid_regex_passes(self) -> None:
        query = 'oracle:/gain[^.]*life[^.]*draw/ AND otag:lifegain-matters'
        client = make_fixture_search_client([
            _card('C', 'Whenever you gain life, draw a card.',
                  ['lifegain-matters', 'draw', 'lifegain']),
        ])
        result = verify(query, ('lifegain', 'draw-a-card'), search_client=client)
        assert result.pasteability_ok is True

    def test_lookahead_rejected(self) -> None:
        query = 'oracle:/(?=.*draw)gain/'
        client = make_fixture_search_client([])
        result = verify(query, ('lifegain', 'draw-a-card'), search_client=client)
        assert result.passed is False
        assert result.pasteability_ok is False
        assert 'lookaround' in (result.error or '').lower()

    def test_bare_space_rejected(self) -> None:
        query = 'oracle:/gain life draw/'
        client = make_fixture_search_client([])
        result = verify(query, ('lifegain', 'draw-a-card'), search_client=client)
        assert result.passed is False
        assert result.pasteability_ok is False

    def test_backreference_rejected(self) -> None:
        query = r'oracle:/(\w)\1/'
        client = make_fixture_search_client([])
        result = verify(query, ('lifegain', 'draw-a-card'), search_client=client)
        assert result.passed is False
        assert result.pasteability_ok is False


# =====================================================================
# TestVerifyResolution
# =====================================================================

class TestVerifyResolution:
    """Check (b): resolves + expected-hit."""

    def test_zero_results_fails(self) -> None:
        query = 'oracle:/xyznonexistent/'
        client = make_fixture_search_client([
            _card('A', 'Flying', ['flying']),
        ])
        result = verify(query, ('lifegain', 'draw-a-card'), search_client=client)
        assert result.passed is False
        assert result.resolves is False
        assert result.hit_count == 0

    def test_resolves_nonempty(self) -> None:
        query = 'otag:lifegain-matters'
        client = make_fixture_search_client([
            _card('C', 'Whenever you gain life, draw a card.',
                  ['lifegain-matters', 'draw', 'lifegain']),
        ])
        result = verify(query, ('lifegain', 'draw-a-card'), search_client=client)
        assert result.resolves is True
        assert result.hit_count == 1

    def test_expected_card_present(self) -> None:
        query = 'otag:life-loss-matters AND otag:lifegain'
        client = make_fixture_search_client([
            _card('Exquisite Blood',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain', 'repeatable-lifegain']),
        ])
        result = verify(
            query, ('life-loss-drain', 'lifegain'),
            expected_card_names=['Exquisite Blood'],
            search_client=client,
        )
        assert result.expected_cards_hit is True

    def test_expected_card_missing_fails(self) -> None:
        query = 'otag:lifegain-matters'
        client = make_fixture_search_client([
            _card('Some Card', 'Whenever you gain life, X.',
                  ['lifegain-matters']),
        ])
        result = verify(
            query, ('lifegain', 'draw-a-card'),
            expected_card_names=["Lich's Mastery"],
            search_client=client,
        )
        assert result.expected_cards_hit is False
        assert result.passed is False


# =====================================================================
# TestVerifyCorroboration
# =====================================================================

class TestVerifyCorroboration:
    """Check (c): deterministic over-capture oracle."""

    def test_full_corroboration_passes(self) -> None:
        """All captured cards have both trigger and effect role otags."""
        query = ('oracle:/opponent[^.]*los[a-z]*[^.]*gain/ '
                 'AND otag:life-loss-matters AND otag:lifegain')
        role = ('life-loss-drain', 'lifegain')
        cards = [
            _card('Exquisite Blood',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain', 'repeatable-lifegain']),
            _card('Irrelevant', 'Flying', []),
        ]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client)

        assert result.corroboration_rate == 1.0
        assert len(result.failing_cards) == 0
        assert result.passed is True

    def test_overcapture_rejects(self) -> None:
        """A bare regex without otag constraints captures many false positives."""
        query = 'oracle:/los[^.]*gain/'
        role = ('life-loss-drain', 'lifegain')
        cards = [
            # Legitimate: both trigger and effect otags
            _card('Exquisite Blood',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain', 'repeatable-lifegain']),
            # FP: burn-player has no trigger otag for life-loss-drain
            _card('Drain Spell',
                  'Target player loses 2 life and you gain 2 life.',
                  ['burn-player']),
            # FP: wrong otags entirely
            _card('Combat Trick',
                  'Target creature loses all abilities and gains trample.',
                  ['gives-trample']),
            # FP: generic life payment
            _card('Life Exchange',
                  'As an additional cost, each player loses 1 life and gains a counter.',
                  ['counters']),
        ]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client)

        # 4 cards captured, only Exquisite Blood corroborates: rate = 0.25
        assert result.hit_count == 4
        assert result.corroboration_rate == pytest.approx(0.25)
        assert result.corroboration_rate < CORROBORATION_THRESHOLD
        assert result.passed is False
        assert len(result.failing_cards) == 3

    def test_threshold_boundary_passes_at_exactly_threshold(self) -> None:
        """Exactly at threshold (>=) passes."""
        query = 'oracle:/los[^.]*gain/'
        role = ('life-loss-drain', 'lifegain')
        cards = [
            # Corroborates
            _card('Exquisite Blood',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain']),
            # Does NOT corroborate
            _card('FP', 'loses stuff and gains stuff.',
                  ['burn-player']),
        ]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client, threshold=0.5)

        # 2 cards, 1 corroborates: rate = 0.5 >= 0.5 -> passes
        assert result.corroboration_rate == pytest.approx(0.5)
        assert result.passed is True

    def test_threshold_boundary_fails_below(self) -> None:
        """Just below threshold fails."""
        query = 'oracle:/los[^.]*gain/'
        role = ('life-loss-drain', 'lifegain')
        cards = [
            _card('Exquisite Blood',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain']),
            _card('FP1', 'Target player loses stuff and gains stuff.',
                  ['burn-player']),
            _card('FP2', 'Another loses foo and gains bar.',
                  ['tokens']),
        ]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client, threshold=0.5)

        # 3 cards, 1 corroborates: rate = 0.333 < 0.5 -> fails
        assert result.corroboration_rate < 0.5
        assert result.passed is False


# =====================================================================
# TestVerifyEndToEnd
# =====================================================================

class TestVerifyEndToEnd:
    """Full gate: pass and reject end-to-end on the Queza fixture."""

    def test_curated_drain_to_gain_passes(self) -> None:
        """The curated ``drain-to-gain`` query passes all three checks."""
        query = ('oracle:/opponent[^.]*los[a-z]*[^.]*gain/ '
                 'AND otag:life-loss-matters AND otag:lifegain')
        role = ('life-loss-drain', 'lifegain')
        fixture = _load_relay_fixture()
        client = make_fixture_search_client(fixture['cards'])

        result = verify(
            query, role,
            expected_card_names=['Exquisite Blood'],
            search_client=client,
        )
        assert result.passed is True
        assert result.pasteability_ok is True
        assert result.resolves is True
        assert result.expected_cards_hit is True
        assert result.corroboration_rate == 1.0
        assert result.substrate == 'fixture'

    def test_curated_gain_to_draw_passes(self) -> None:
        """The curated ``gain-to-draw`` query passes all three checks."""
        query = 'oracle:/gain[^.]*life[^.]*draw/ AND otag:lifegain-matters'
        role = ('lifegain', 'draw-a-card')
        fixture = _load_relay_fixture()
        client = make_fixture_search_client(fixture['cards'])

        result = verify(
            query, role,
            expected_card_names=["Lich's Mastery"],
            search_client=client,
        )
        assert result.passed is True
        assert result.hit_count >= 1
        # All captured cards should have lifegain-matters (trigger) AND
        # a draw-a-card producer otag:
        assert result.corroboration_rate >= CORROBORATION_THRESHOLD

    def test_bare_regex_overcapture_rejects(self) -> None:
        """A bare ``oracle:/los[^.]*gain/`` without the opponent anchor is rejected."""
        query = 'oracle:/los[^.]*gain/'
        role = ('life-loss-drain', 'lifegain')

        # Build a card pool with a mix of legitimate and FP cards.
        cards = [
            # Legitimate drain-to-gain
            _card('Exquisite Blood',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain', 'repeatable-lifegain']),
            # FP: self-loss (no opponent anchor), has self-life-loss-matters
            # which IS in triggers, but missing a produced_by otag
            _card('Transcendence-like',
                  'Whenever you lose life, you gain that much life instead.',
                  ['self-life-loss-matters']),
            # FP: generic burn, no trigger otags
            _card('Drain',
                  'Target player loses 3 life and you gain 3 life.',
                  ['burn-player']),
            # FP: ability-gain word sense
            _card('Ability Granter',
                  'Target creature loses all abilities and gains flying.',
                  ['gives-evasion']),
            # FP: cost payment
            _card('Dark Ritual Variant',
                  'As a cost, you lose 2 life and gain two mana.',
                  ['ritual']),
        ]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client)

        assert result.passed is False
        assert result.pasteability_ok is True
        assert result.resolves is True
        assert result.corroboration_rate < CORROBORATION_THRESHOLD

    def test_client_substrate_recorded(self) -> None:
        query = 'otag:lifegain'
        role = ('lifegain', 'draw-a-card')
        client = make_fixture_search_client(
            [_card('C', 'Gain life.', ['lifegain', 'draw'])],
            substrate_label='test-lake',
        )
        result = verify(query, role, search_client=client)
        assert result.substrate == 'test-lake'


# =====================================================================
# TestVerifySynthBypass
# =====================================================================

class TestVerifySynthBypass:
    """Synth gate bypass (``decision='synth'``).

    A synth is a mechanic with no otag.  For synth decisions, corroboration
    (check c) is bypassed in favour of a text-anchor specificity check:
    the query must contain at least one ``oracle:`` or ``type:`` text
    anchor (regex ``/.../`` or literal ``'...'``).  Edge decisions
    (``decision='edge'``, the default) ALWAYS run corroboration.
    """

    def test_synth_literal_query_passes(self) -> None:
        """decision='synth' + oracle literal query -> bypass passes."""
        query = "oracle:'number of counters on'"
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card('Jadzia Dax, Living Legacy',
                  'At the beginning of your first main phase, put a +1/+1 counter on '
                  'Jadzia Dax, then add an amount of {G} equal to the number of '
                  'counters on her.',
                  []),  # no otags (unreleased)
        ]
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            expected_card_names=['Jadzia Dax, Living Legacy'],
            search_client=client,
            decision='synth',
        )
        assert result.passed is True
        assert result.synth_bypass is True
        assert result.corroboration_rate == -1.0  # sentinel: bypass

    def test_synth_regex_query_passes(self) -> None:
        """decision='synth' + oracle regex query -> bypass passes."""
        query = "oracle:/number[^.]*counters[^.]*on/"
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card('Jadzia Dax', 'number of counters on her.', []),
        ]
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            expected_card_names=['Jadzia Dax'],
            search_client=client,
            decision='synth',
        )
        assert result.passed is True
        assert result.synth_bypass is True
        assert result.corroboration_rate == -1.0

    def test_synth_no_text_anchor_fails(self) -> None:
        """decision='synth' but query has no oracle/type atom -> fails specificity."""
        query = 'otag:counters-matter'
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card('Jadzia Dax', 'number of counters on her.',
                  ['counters-matter']),
        ]
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            search_client=client,
            decision='synth',
        )
        assert result.passed is False
        assert result.synth_bypass is True
        assert 'text anchor' in (result.error or '')

    def test_synth_zero_hits_fails(self) -> None:
        """decision='synth' but query resolves to 0 cards -> fails resolution."""
        query = "oracle:'xyznonexistent'"
        role = ('counter-placed', 'mana-from-counters')
        cards = [_card('A', 'Flying', [])]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client, decision='synth')
        assert result.passed is False
        assert result.resolves is False

    def test_synth_expected_card_missing_fails(self) -> None:
        """decision='synth' + expected card not hit -> fails."""
        query = "oracle:'number of counters on'"
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card('Other Card', 'number of counters on it.', []),
        ]
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            expected_card_names=['Jadzia Dax'],
            search_client=client,
            decision='synth',
        )
        assert result.passed is False
        assert result.expected_cards_hit is False

    def test_edge_decision_no_otags_does_NOT_bypass(self) -> None:
        """decision='edge' (default) with no-otag cards does NOT bypass.

        Edge decisions always run corroboration.  With no otags on captured
        cards and a novel event, the corroboration rate will be 0.0 and the
        query will be rejected.
        """
        query = "oracle:'number of counters on'"
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card('Jadzia Dax', 'number of counters on her.', []),
        ]
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            search_client=client,
            decision='edge',
        )
        # Edge decision runs corroboration; with no otags it rates 0.0 -> fail
        assert result.synth_bypass is False
        assert result.corroboration_rate == 0.0
        assert result.passed is False

    def test_edge_normal_otag_path_unchanged(self) -> None:
        """Normal edge decision with otag cards runs corroboration as before."""
        query = ('oracle:/opponent[^.]*los[a-z]*[^.]*gain/ '
                 'AND otag:life-loss-matters AND otag:lifegain')
        role = ('life-loss-drain', 'lifegain')
        cards = [
            _card('Exquisite Blood',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain', 'repeatable-lifegain']),
        ]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client)
        assert result.synth_bypass is False
        assert result.corroboration_rate == 1.0
        assert result.passed is True


# =====================================================================
# TestSynthBreadthWarning
# =====================================================================

class TestSynthBreadthWarning:
    """Soft over-breadth gate for synth verification.

    A synth that passes all hard checks (RE2-pasteable, resolves, expected-hit,
    text-anchor) but captures > SYNTH_BREADTH_WARN cards gets
    ``breadth_warning=True`` and ``passed=False`` — not a terminal pass.
    """

    def test_breadth_warn_constant_is_100(self) -> None:
        assert SYNTH_BREADTH_WARN == 100

    def test_synth_within_breadth_passes_clean(self) -> None:
        """Synth with <= 100 hits passes clean, breadth_warning=False."""
        query = "oracle:'number of counters on'"
        role = ('counter-placed', 'mana-from-counters')
        # 3 cards — well within threshold.
        cards = [
            _card(f'Card {i}', 'number of counters on it.', [])
            for i in range(3)
        ]
        cards[0] = _card('Source Card', 'number of counters on her.', [])
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            expected_card_names=['Source Card'],
            search_client=client,
            decision='synth',
        )
        assert result.passed is True
        assert result.breadth_warning is False
        assert result.synth_bypass is True

    def test_synth_exactly_at_breadth_threshold_passes(self) -> None:
        """Exactly 100 hits passes clean (breadth_warning=False)."""
        query = "oracle:'counter'"
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card(f'Card {i}', f'Put a counter on target.', [])
            for i in range(100)
        ]
        cards[0] = _card('Source Card', 'Put a counter on it.', [])
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            expected_card_names=['Source Card'],
            search_client=client,
            decision='synth',
        )
        assert result.hit_count == 100
        assert result.passed is True
        assert result.breadth_warning is False

    def test_synth_just_above_breadth_threshold_warns(self) -> None:
        """101 hits triggers breadth_warning=True, passed=False."""
        query = "oracle:'counter'"
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card(f'Card {i}', f'Put a counter on target.', [])
            for i in range(101)
        ]
        cards[0] = _card('Source Card', 'Put a counter on it.', [])
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            expected_card_names=['Source Card'],
            search_client=client,
            decision='synth',
        )
        assert result.hit_count == 101
        assert result.passed is False
        assert result.breadth_warning is True
        # Hard-check booleans still true.
        assert result.pasteability_ok is True
        assert result.resolves is True
        assert result.expected_cards_hit is True
        assert result.synth_bypass is True
        assert result.corroboration_rate == -1.0  # still sentinel
        assert 'breadth' in (result.error or '').lower()

    def test_synth_large_hit_count_warns(self) -> None:
        """A very broad synth (>100 hits) with source present still warns."""
        query = "oracle:'counter'"
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card(f'Card {i}', f'Add a counter to something.', [])
            for i in range(250)
        ]
        cards[0] = _card('Source Card', 'Put a counter on it.', [])
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            expected_card_names=['Source Card'],
            search_client=client,
            decision='synth',
        )
        assert result.hit_count == 250
        assert result.passed is False
        assert result.breadth_warning is True

    def test_synth_over_breadth_but_fails_hard_check_no_warning(self) -> None:
        """If hard checks fail, breadth_warning stays False (hard failure first)."""
        # No text anchor (fails specificity check).
        query = 'otag:counters-matter'
        role = ('counter-placed', 'mana-from-counters')
        cards = [
            _card(f'Card {i}', 'some text', ['counters-matter'])
            for i in range(200)
        ]
        client = make_fixture_search_client(cards)
        result = verify(
            query, role,
            search_client=client,
            decision='synth',
        )
        assert result.passed is False
        # Hard check failed -> breadth_warning not set.
        assert result.breadth_warning is False

    def test_edge_decision_never_sets_breadth_warning(self) -> None:
        """Edge decision (default) never sets breadth_warning, regardless of hit count."""
        query = ('oracle:/opponent[^.]*los[a-z]*[^.]*gain/ '
                 'AND otag:life-loss-matters AND otag:lifegain')
        role = ('life-loss-drain', 'lifegain')
        # Build a large pool with lots of corroborating cards.
        cards = [
            _card(f'Card {i}',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain'])
            for i in range(150)
        ]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client, decision='edge')
        # Edge path does NOT touch breadth_warning.
        assert result.breadth_warning is False
        assert result.passed is True  # all corroborate

    def test_edge_path_unchanged_regression(self) -> None:
        """Edge path output is byte-identical to pre-breadth-warning behavior.

        The full corroboration check (check c) runs for edge decisions exactly
        as before.  breadth_warning is irrelevant; synth_bypass is False.
        """
        query = ('oracle:/opponent[^.]*los[a-z]*[^.]*gain/ '
                 'AND otag:life-loss-matters AND otag:lifegain')
        role = ('life-loss-drain', 'lifegain')
        cards = [
            _card('Exquisite Blood',
                  'Whenever an opponent loses life, you gain that much life.',
                  ['life-loss-matters', 'lifegain', 'repeatable-lifegain']),
        ]
        client = make_fixture_search_client(cards)
        result = verify(query, role, search_client=client, decision='edge')
        assert result.passed is True
        assert result.synth_bypass is False
        assert result.breadth_warning is False
        assert result.corroboration_rate == 1.0
        assert result.hit_count == 1
