"""F5 — a combo win-override that fires over a DENSE competing primary must carry
MED confidence (flag the ambiguity), while one over a genuinely weak residue
keeps HIGH.

The F4 guard only BLOCKS the flip when the pre-combo primary is dense AND
dominant (share >= combo_primary_share AND margin >= combo_primary_margin). A
dense-but-not-dominant deck (share >= 0.50 but a close #2, margin < 0.30) slips
past the gate and flips to combo — that flip is acceptable, but a HIGH-confidence
combo label on a contested primary hides the real ambiguity (the T7 finding on
gruul__roxanne 0.523 / abzan__tayam 0.545 / owned Blight Curse 0.607).

These synthetic FactsContexts hold the override eligible (confidence LOW, two
win-combos) and move ONLY the identity_share across the combo_primary_share
boundary, pinning that:

* dense competing primary (share >= 0.50, margin < 0.30) -> combo at **MED**;
* weak residue (share < 0.50) -> combo at **HIGH** (Kess/Urza/K'rrik unchanged).
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from pipeline.transforms.strategy.config import DEFAULT_CONFIG
from pipeline.transforms.strategy.facts import FactsContext
from pipeline.transforms.strategy.naming import name_deck


def _ctx(share: float, wq_top: float, wq_second: float) -> FactsContext:
    """Two-identity deck (burn #1, lifegain #2) with the override held eligible.

    ``identity_mass`` is pinned so burn owns exactly ``share`` of it, and a large
    filler node mass drags span (hence confidence) to LOW so the win-combo
    override is eligible. Only ``share`` and the #1-vs-#2 margin vary.
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
    arch_members = {'burn': {0, 1, 2, 3, 4}, 'lifegain': {5, 6, 7, 8}}
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
        identity_mass=wq_top / share, coverage=0.9, spell_sub=True, gy_sub=True, demotions={},
        cs_axis={'n': 0}, stax_axis={}, ramp_axis={'cedh_level': False}, protection_degree={},
        power={}, tribe_info={'top': None, 'ranked': []}, density_signals={}, cmd_attrs=set(),
        detected=[], win_combos=win_combos, predicate_wins=win_combos, mana_sink_wins=[],
    )


def test_dense_competing_primary_combo_is_med_not_high():
    # share 0.55 >= combo_primary_share, margin 0.29 < combo_primary_margin ->
    # dense-but-not-dominant -> flips to combo, but at MED to flag the ambiguity
    rec = name_deck(_ctx(share=0.55, wq_top=100.0, wq_second=71.0))
    sig = rec['confidence_signals']
    assert sig['identity_share'] >= DEFAULT_CONFIG.combo_primary_share
    assert sig['margin'] < DEFAULT_CONFIG.combo_primary_margin
    assert rec['named_strategy'] == 'alt-wincon/combo'
    assert rec['combo_override_applied'] is True
    assert rec['combo_override_f4_blocked'] is False
    assert rec['combo_override_dense_primary'] is True
    assert rec['confidence'] == 'MED'


def test_weak_residue_combo_keeps_high():
    # share 0.45 < combo_primary_share -> not a contested primary -> combo at HIGH
    # (the Kess/Urza/K'rrik regime is unchanged)
    rec = name_deck(_ctx(share=0.45, wq_top=100.0, wq_second=71.0))
    sig = rec['confidence_signals']
    assert sig['identity_share'] < DEFAULT_CONFIG.combo_primary_share
    assert rec['named_strategy'] == 'alt-wincon/combo'
    assert rec['combo_override_applied'] is True
    assert rec['combo_override_dense_primary'] is False
    assert rec['confidence'] == 'HIGH'


def test_share_is_the_only_mover_between_med_and_high():
    # same margin regime; only identity_share crosses the threshold -> the share
    # gate, not anything else, is what demotes the combo label's confidence
    dense = name_deck(_ctx(share=0.55, wq_top=100.0, wq_second=71.0))
    weak = name_deck(_ctx(share=0.45, wq_top=100.0, wq_second=71.0))
    assert dense['named_strategy'] == weak['named_strategy'] == 'alt-wincon/combo'
    assert dense['confidence'] == 'MED'
    assert weak['confidence'] == 'HIGH'


@pytest.mark.parametrize('share', [0.50, 0.523, 0.545, 0.607])
def test_known_dense_combo_shares_are_med(share: float):
    # the exact T7 shares (Roxanne 0.523, Tayam 0.545, Blight Curse 0.607, and the
    # 0.50 boundary) all land at MED, never HIGH
    rec = name_deck(_ctx(share=share, wq_top=100.0, wq_second=71.0))
    assert rec['named_strategy'] == 'alt-wincon/combo'
    assert rec['confidence'] == 'MED'
