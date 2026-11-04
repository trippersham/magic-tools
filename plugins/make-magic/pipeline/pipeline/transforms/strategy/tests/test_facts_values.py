"""Facts VALUE assertions (review finding #7).

The facts graph is the non-negotiable guarantee, so these tests pin CONTENT, not
just keys: the answered-by slug pools, the degree-axis readings for a known
control deck vs a known non-control deck, support quantities, the power profile,
and the core-combo surface. Values are taken from the committed golden fixtures.
"""

from __future__ import annotations

import pytest

from pipeline.transforms.strategy import resolve
from pipeline.transforms.strategy.assets import ANSWERS_MAP


# ---------------------------------------------------------------- answered_by
def test_answered_by_graveyard_deck_maps_to_hate_graveyard_family(load_deck, combos):
    # Kess runs a graveyard/reanimator plan -> its directed answer pool is the
    # hate-graveyard family (verbatim from the committed ANSWERS_MAP).
    rec = resolve(load_deck('gauntlet__kess-dissident-mage'), combos)
    ab = rec['facts']['answered_by']
    assert 'graveyard/reanimator' in ab
    assert ab['graveyard/reanimator']['answer_slugs'] == [
        'hate-graveyard', 'single-minded-graveyard-hate', 'hate-graveyard-cast', 'sweeper-graveyard']
    assert ab['graveyard/reanimator']['answer_slugs'] == ANSWERS_MAP['graveyard/reanimator']


def test_answered_by_control_deck_maps_to_hate_counterspell(load_deck, combos):
    rec = resolve(load_deck('gauntlet__baral-chief-of-compliance'), combos)
    ab = rec['facts']['answered_by']
    assert ab['control/permission']['answer_slugs'] == ['hate-counterspell']


def test_answered_by_keys_are_only_requires_met_strategies(load_deck, combos):
    # a fact never claims an answer pool for a strategy that didn't fire
    rec = resolve(load_deck('owned__world-reclaimer'), combos)
    strategies = rec['facts']['strategies']
    for arch, entry in rec['facts']['answered_by'].items():
        assert strategies[arch]['requires_met'] is True, arch
        assert entry['answer_slugs'] == ANSWERS_MAP[arch]
        assert entry['n_in_deck_answers'] == len(entry['in_deck_answer_cards'])


# ------------------------------------------------------------- degree axes
def test_degree_axis_control_vs_noncontrol_counterspells(load_deck, combos):
    # Baral (mono-ish UB control) OVER-serves counterspells; Ozai (mono-R burn)
    # has none and is UNDER-served + color-gated out of blue permission.
    baral = resolve(load_deck('gauntlet__baral-chief-of-compliance'), combos)['facts']['degree_axes']
    ozai = resolve(load_deck('owned__ozai'), combos)['facts']['degree_axes']

    bcs = baral['counterspells']
    assert bcs['n'] == 26
    assert bcs['frac'] == pytest.approx(0.2737, abs=1e-4)
    assert bcs['over_served'] is True
    assert bcs['under_served'] is False
    assert bcs['color_gated'] is False  # blue deck -> not gated

    ocs = ozai['counterspells']
    assert ocs['n'] == 0
    assert ocs['frac'] == pytest.approx(0.0)
    assert ocs['over_served'] is False
    assert ocs['under_served'] is True
    assert ocs['color_gated'] is True  # no blue -> gated


def test_degree_axis_removal_inverts_and_reads_frac(load_deck, combos):
    # removal is an inverting axis (precon runs MORE than cEDH); assert the flag
    # and the measured fracs for both decks.
    baral = resolve(load_deck('gauntlet__baral-chief-of-compliance'), combos)['facts']['degree_axes']
    ozai = resolve(load_deck('owned__ozai'), combos)['facts']['degree_axes']
    assert baral['removal']['inverts'] is True
    assert baral['removal']['n'] == 5
    assert baral['removal']['frac'] == pytest.approx(0.0526, abs=1e-4)
    assert ozai['removal']['n'] == 14
    assert ozai['removal']['frac'] == pytest.approx(0.2258, abs=1e-4)


# ------------------------------------------------------------- support quantities
def test_support_quantities_values(load_deck, combos):
    baral = resolve(load_deck('gauntlet__baral-chief-of-compliance'), combos)['facts']['support_quantities']
    assert set(baral) == {'draw', 'ramp', 'removal'}
    assert baral['draw'] == {'weighted_quantity': 18.0, 'n_cards': 18}
    assert baral['removal'] == {'weighted_quantity': 6.0, 'n_cards': 5}

    ozai = resolve(load_deck('owned__ozai'), combos)['facts']['support_quantities']
    assert 'tutor' in ozai  # Ozai runs tutors; Baral does not surface a tutor node
    assert ozai['ramp'] == {'weighted_quantity': 32.0, 'n_cards': 22}


# ------------------------------------------------------------- power profile
def test_power_profile_values_control_vs_burn(load_deck, combos):
    baral = resolve(load_deck('gauntlet__baral-chief-of-compliance'), combos)['facts']['power_profile']
    assert baral['n_interaction'] == 28
    assert baral['removal_count'] == 5
    assert baral['removal_pct_instant'] == pytest.approx(1.0)  # every removal spell is instant
    assert baral['mean_efficiency'] == pytest.approx(0.437, abs=1e-3)

    ozai = resolve(load_deck('owned__ozai'), combos)['facts']['power_profile']
    assert ozai['removal_count'] == 14
    assert ozai['removal_pct_instant'] == pytest.approx(0.214, abs=1e-3)


# ------------------------------------------------------------- core combos
def test_core_combos_values_kess(load_deck, combos):
    rec = resolve(load_deck('gauntlet__kess-dissident-mage'), combos)
    cc = rec['facts']['core_combos']
    assert len(cc) == 6
    assert sum(1 for c in cc if c['win']) == 4
    assert rec['n_win_combos'] == 4
    # wins sort first, and each combo's piece count is self-consistent
    assert cc[0]['win'] is True
    assert any(c['label'] == 'Win the game' and c['n_pieces'] == 2 for c in cc)
    for c in cc:
        assert c['n_pieces'] == len(c['requires'])
        assert c['win_kind'] in {'predicate', 'mana-sink', 'non-win'}


def test_core_combos_ozai_has_predicate_and_mana_sink(load_deck, combos):
    # Ozai's win-combos come from BOTH a predicate win and a mana-sink promotion.
    # (mana-sink entries are promoted win-combos, so they carry win_kind=='mana-sink'
    # while their predicate `win` flag stays False.)
    rec = resolve(load_deck('owned__ozai'), combos)
    kinds = {c['win_kind'] for c in rec['facts']['core_combos']}
    assert {'predicate', 'mana-sink'} <= kinds
    assert rec['n_win_predicate'] >= 1
    assert rec['n_win_mana_sink'] >= 1
    assert rec['n_win_combos'] == rec['n_win_predicate'] + rec['n_win_mana_sink']
