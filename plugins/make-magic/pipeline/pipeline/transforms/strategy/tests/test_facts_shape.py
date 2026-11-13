"""Facts-object shape + exposure: the deterministic facts graph is well-formed."""

from __future__ import annotations

import pytest

from pipeline.transforms.strategy import compute_facts, name_deck, resolve
from pipeline.transforms.strategy.tests.conftest import GOLDEN_SLUGS

_FACTS_KEYS = {
    'attributes_present', 'density_signals', 'strategies', 'core_combos', 'combo_elevation',
    'answered_by', 'support_quantities', 'degree_axes', 'power_profile', 'protection_degree',
    'engines', 'edges', 'lands_enabler', 'tribes', 'coverage', 'n_nodes',
}
_TOP_KEYS = {
    'name', 'bracket', 'bundle', 'source', 'commanders', 'commander_hint', 'n_cards', 'n_nonland',
    'coverage', 'density_signals', 'n_nodes', 'cmd_attrs', 'named_strategy', 'tribe', 'abstain',
    'confidence', 'confidence_signals', 'strategies', 'facts', 'combo_override_applied',
    'n_detected_combos', 'n_win_combos',
}


@pytest.mark.parametrize('slug', sorted(GOLDEN_SLUGS))
def test_record_and_facts_shape(slug, load_deck, combos):
    rec = resolve(load_deck(slug), combos)
    assert set(rec) >= _TOP_KEYS, f'{slug} missing top-level keys: {_TOP_KEYS - set(rec)}'
    facts = rec['facts']
    assert set(facts) >= _FACTS_KEYS, f'{slug} missing facts keys: {_FACTS_KEYS - set(facts)}'

    # degree axes carry the frozen-baseline axes (evasion is measure-only: a degree
    # axis that is never consumed by the naming layer)
    assert set(facts['degree_axes']) == {
        'counterspells', 'removal', 'ramp', 'tutor', 'draw', 'stax', 'protection', 'evasion'}

    # every strategy exposes its weighted quantity + member cards + requires_met
    for arch, st in facts['strategies'].items():
        assert {'weighted_quantity', 'n_member_cards', 'requires_met', 'member_cards',
                'contributing_attrs', 'is_identity'} <= set(st), arch
        assert st['n_member_cards'] == len(st['member_cards'])

    # tribes block is always exposed (a fact for every deck, not only typal ones)
    assert 'top' in facts['tribes']
    assert 'ranked' in facts['tribes']


def test_evasion_is_a_measure_only_degree_axis(load_deck, combos):
    # evasion is a universal combat MEANS (like ramp/draw): a degree axis surfaced
    # as a fact + over/under-served flag, inverted (precon creature-heavy > cEDH
    # combo-light), and NEVER a label — the naming layer does not consume it.
    from pipeline.transforms.strategy import vocab
    assert 'evasion' not in set(vocab.ARCHETYPE_EDGES.values())  # not in the label space

    baral = resolve(load_deck('gauntlet__baral-chief-of-compliance'), combos)  # control = evasion-light
    ev = baral['facts']['degree_axes']['evasion']
    assert ev['inverts'] is True
    assert {'n', 'over_served', 'under_served'} <= set(ev)
    # #69 fix: with correct card data Baral's evasion count shifts just past the
    # under-served threshold → neither flag fires (neutral evasion).
    assert baral['evasion_under_served'] is False and baral['evasion_over_served'] is False
    assert baral['evasion_count'] == ev['n']
    assert baral['named_strategy'] != 'evasion'


def test_compute_facts_then_name_deck_matches_resolve(load_deck, combos):
    # the facts layer and the naming layer compose into the same record resolve() emits
    deck = load_deck('gauntlet__kess-dissident-mage')
    ctx = compute_facts(deck, combos)
    named = name_deck(ctx)
    rec = resolve(deck, combos)
    assert named['named_strategy'] == rec['named_strategy']
    assert ctx.facts == rec['facts']  # same facts graph (resolve recomputes a fresh, equal object)
    assert named['confidence'] == rec['confidence']
