"""F4 margin gate as an INDEPENDENT discriminator (review finding #5).

On the real fixtures the SHARE gate alone separates Ozai (share >= 0.50, blocked)
from the cEDH combo decks (share < 0.50, flip), so the margin half of the
``primary_dominates`` guard never gets to be the deciding factor — a dense deck
with a close #2 (share >= 0.50 but margin < combo_primary_margin) is a gap no
fixture exercises.

These synthetic FactsContexts pin the margin gate directly: SHARE is held
constant at 0.55 in both cases, the override is held eligible (confidence LOW,
two win-combos present), and ONLY the #1-vs-#2 margin is moved across the 0.30
boundary. The verdict flips, which proves the margin condition — not the share
condition — is what decides the F4 block here.

This documents the shipped contract: share is the PRIMARY separator and margin
is a SECONDARY guard; a dense-but-not-dominant deck (share >= 0.50, margin <
0.30) is NOT protected and WILL flip to combo. That is no longer an uncaught
behavior.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from pipeline.transforms.strategy.config import DEFAULT_CONFIG
from pipeline.transforms.strategy.facts import FactsContext
from pipeline.transforms.strategy.naming import name_deck

_SHARE = 0.55  # identical in every case -> share can never be the discriminator


def _ctx(wq_top: float, wq_second: float) -> FactsContext:
    """A two-identity deck (burn #1, lifegain #2) with the override held eligible.

    ``identity_mass`` is pinned so the top identity owns exactly ``_SHARE`` of it,
    and a large filler node mass drags span (hence confidence) down to LOW so the
    win-combo override is eligible and the F4 guard is the ONLY thing in play.
    """
    cfg = DEFAULT_CONFIG
    strategies = {
        'burn': {'weighted_quantity': wq_top, 'n_member_cards': 5, 'requires_met': True,
                 'contributing_attrs': ['burn'], 'member_cards': ['a', 'b', 'c', 'd', 'e'],
                 'is_identity': True},
        'lifegain': {'weighted_quantity': wq_second, 'n_member_cards': 4, 'requires_met': True,
                     'contributing_attrs': ['lifegain'], 'member_cards': ['f', 'g', 'h', 'i'],
                     'is_identity': True},
    }
    arch_members = {'burn': {0, 1, 2, 3, 4}, 'lifegain': {5, 6, 7, 8}}  # disjoint
    adens: defaultdict[str, float] = defaultdict(float, {'burn': wq_top, 'lifegain': wq_second,
                                                         'filler': 300.0})
    cards = [{'name': c, 'role': None} for c in 'abcdefghi']
    win_combos = [{'card_names': [f'p{i}', f'q{i}'], 'win': True, 'mana': False,
                   'variant_id': f'v{i}', 'result': 'Win the game'} for i in range(2)]
    return FactsContext(
        deck={}, cards=cards, n_nonland=9, assets=None, cfg=cfg, facts={},  # type: ignore[arg-type]
        strategies=strategies, facts_only=set(), arch_members=arch_members,
        arch_has_cmd={'burn': False, 'lifegain': False},
        nodes={'burn', 'lifegain', 'filler'}, support=frozenset(), adens=adens, card_w={},
        identity_mass=wq_top / _SHARE, coverage=0.9, spell_sub=True, gy_sub=True, demotions={},
        cs_axis={'n': 0}, stax_axis={}, ramp_axis={'cedh_level': False}, protection_degree={},
        power={}, tribe_info={'top': None, 'ranked': []}, density_signals={}, cmd_attrs=set(),
        detected=[], win_combos=win_combos, predicate_wins=win_combos, mana_sink_wins=[],
    )


def test_margin_just_above_threshold_blocks_override_stays_primary():
    # share >= 0.50 AND margin >= 0.30 -> dense AND dominant -> F4 BLOCKS the flip
    rec = name_deck(_ctx(wq_top=100.0, wq_second=69.0))  # margin = 1 - 69/100 = 0.31
    sig = rec['confidence_signals']
    assert sig['identity_share'] >= DEFAULT_CONFIG.combo_primary_share
    assert sig['margin'] >= DEFAULT_CONFIG.combo_primary_margin
    assert sig['margin'] == pytest.approx(0.31)
    # the override WAS eligible (LOW residue, 2 win-combos) but the margin gate held
    assert rec['confidence'] == 'LOW'
    assert rec['combo_override_f4_blocked'] is True
    assert rec['combo_override_applied'] is False
    assert rec['named_strategy'] == 'burn'


def test_margin_just_below_threshold_does_not_block_flips_to_combo():
    # share >= 0.50 but margin < 0.30 -> dense but NOT dominant -> override FIRES
    rec = name_deck(_ctx(wq_top=100.0, wq_second=71.0))  # margin = 1 - 71/100 = 0.29
    sig = rec['confidence_signals']
    assert sig['identity_share'] >= DEFAULT_CONFIG.combo_primary_share  # SAME share...
    assert sig['margin'] < DEFAULT_CONFIG.combo_primary_margin          # ...only margin moved
    assert sig['margin'] == pytest.approx(0.29)
    assert rec['combo_override_f4_blocked'] is False
    assert rec['combo_override_applied'] is True
    assert rec['named_strategy'] == 'alt-wincon/combo'


def test_share_is_held_constant_so_margin_is_the_discriminator():
    # the two cases share an identical identity_share; the only moved input is the
    # margin -> the margin gate, not the share gate, is what flips the verdict
    above = name_deck(_ctx(100.0, 69.0))
    below = name_deck(_ctx(100.0, 71.0))
    assert above['confidence_signals']['identity_share'] == below['confidence_signals']['identity_share']
    assert above['named_strategy'] != below['named_strategy']
