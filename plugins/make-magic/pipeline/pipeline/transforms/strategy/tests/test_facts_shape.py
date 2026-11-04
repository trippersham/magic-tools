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

    # degree axes carry the seven frozen-baseline axes
    assert set(facts['degree_axes']) == {
        'counterspells', 'removal', 'ramp', 'tutor', 'draw', 'stax', 'protection'}

    # every strategy exposes its weighted quantity + member cards + requires_met
    for arch, st in facts['strategies'].items():
        assert {'weighted_quantity', 'n_member_cards', 'requires_met', 'member_cards',
                'contributing_attrs', 'is_identity'} <= set(st), arch
        assert st['n_member_cards'] == len(st['member_cards'])

    # tribes block is always exposed (a fact for every deck, not only typal ones)
    assert 'top' in facts['tribes']
    assert 'ranked' in facts['tribes']


def test_compute_facts_then_name_deck_matches_resolve(load_deck, combos):
    # the facts layer and the naming layer compose into the same record resolve() emits
    deck = load_deck('gauntlet__kess-dissident-mage')
    ctx = compute_facts(deck, combos)
    named = name_deck(ctx)
    rec = resolve(deck, combos)
    assert named['named_strategy'] == rec['named_strategy']
    assert ctx.facts == rec['facts']  # same facts graph (resolve recomputes a fresh, equal object)
    assert named['confidence'] == rec['confidence']
