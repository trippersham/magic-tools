#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Unit tests for the F4 combo-hijack heuristic in roleb_measure.py.

Tests the tightened F4 rule:
  - A winning combo aligned to the dominant archetype is R1-correct (NOT flagged).
  - F4 flags ONLY when (a) no winning combo backs the name, or (b) the naming
    layer's own F4 gate blocked the combo override (misalignment signal).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the scripts directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
from roleb_measure import classify_engine


def _make_deck_input(
    *,
    top_strategy: str = 'aristocrats/sacrifice',
    top_share: float = 0.60,
    n_win_combos: int = 5,
    n_nonwin_combos: int = 2,
    combo_override_blocked: bool = False,
    combo_identity: str | None = None,
) -> dict:
    """Build a minimal deck_input dict for testing classify_engine."""
    combos = []
    for i in range(n_win_combos):
        combos.append(
            {
                'variant_id': f'win-{i}',
                'label': f'Infinite loop variant {i}',
                'result': 'Infinite death triggers',
                'requires': ['Card A', 'Card B'],
                'n_pieces': 2,
                'win': True,
                'win_kind': 'card',
            }
        )
    for i in range(n_nonwin_combos):
        combos.append(
            {
                'variant_id': f'nonwin-{i}',
                'label': f'Near-infinite variant {i}',
                'result': 'Near-infinite tokens',
                'requires': ['Card C', 'Card D'],
                'n_pieces': 2,
                'win': False,
                'win_kind': None,
            }
        )
    return {
        'archetype_identity': [
            {'strategy': top_strategy, 'weighted_quantity': 30, 'share': top_share},
        ],
        'detected_combos': combos,
        'naming_resolution': {
            'named_strategy': top_strategy,
            'combo_override_applied': combo_identity is not None,
            'combo_identity': combo_identity,
            'pre_combo_name': top_strategy,
            'confidence': 'HIGH',
            'combo_override_blocked': combo_override_blocked,
        },
        'commanders': [{'name': 'Test Commander', 'oracle_text': ''}],
        'elected': [],
    }


# ---------- R1: aligned winning combo = NOT flagged ----------


def test_aligned_winning_combo_no_f4():
    """A deck with combo-in-strategy + dominant archetype + winning combos
    + no F4 block is the R1 case: combo IS the engine.  Must NOT be flagged."""
    engine = {
        'strategy': 'Aristocrats Sacrifice Combo',
        'win_condition': 'Infinite death triggers via sacrifice loops',
        'key_pieces': ['Viscera Seer', 'Blood Artist'],
        'confidence': 'HIGH',
    }
    deck_input = _make_deck_input(
        top_strategy='aristocrats/sacrifice',
        top_share=0.60,
        n_win_combos=5,
    )
    result = classify_engine('test__korvold', engine, deck_input)
    f4_flags = [f for f in result['flags'] if 'F4' in f]
    assert f4_flags == [], f'R1 aligned combo should NOT be F4-flagged, got: {f4_flags}'


def test_aligned_graveyard_combo_no_f4():
    """Graveyard/reanimator deck with combo (Karador-like): R1, not flagged."""
    engine = {
        'strategy': 'Graveyard Reanimation Combo',
        'win_condition': 'Recursion loop',
        'key_pieces': ['Karmic Guide', 'Reveillark'],
        'confidence': 'HIGH',
    }
    deck_input = _make_deck_input(
        top_strategy='graveyard/reanimator',
        top_share=0.69,
        n_win_combos=7,
    )
    result = classify_engine('test__karador', engine, deck_input)
    f4_flags = [f for f in result['flags'] if 'F4' in f]
    assert f4_flags == [], f'Graveyard-combo R1 should NOT be F4-flagged, got: {f4_flags}'


def test_combo_with_override_applied_not_blocked():
    """combo_override_applied=True but f4_blocked=False: aligned combo.
    Should NOT be flagged (Ghave-like)."""
    engine = {
        'strategy': '+1/+1 Counter Combo Engine',
        'win_condition': 'Infinite counters',
        'key_pieces': ['Ghave', 'Good-Fortune Unicorn'],
        'confidence': 'HIGH',
    }
    deck_input = _make_deck_input(
        top_strategy='+1/+1 counters',
        top_share=0.56,
        n_win_combos=12,
        combo_identity='Infinite +1/+1 counters combo',
    )
    result = classify_engine('test__ghave', engine, deck_input)
    f4_flags = [f for f in result['flags'] if 'F4' in f]
    assert f4_flags == [], f'Override-applied non-blocked combo is R1, got: {f4_flags}'


# ---------- F4 violation: no winning combo ----------


def test_no_winning_combo_flags_f4():
    """Deck names combo in strategy but has NO winning combo.
    Condition (a): must be flagged."""
    engine = {
        'strategy': 'Lifegain Combo Engine',
        'win_condition': 'Drain opponents',
        'key_pieces': ['Karlov'],
        'confidence': 'MED',
    }
    deck_input = _make_deck_input(
        top_strategy='lifegain',
        top_share=0.80,
        n_win_combos=0,
        n_nonwin_combos=3,
    )
    result = classify_engine('test__no-win-combo', engine, deck_input)
    f4_flags = [f for f in result['flags'] if 'F4' in f]
    assert len(f4_flags) == 1, f'Combo with no winning combo should be F4-flagged, got: {f4_flags}'
    assert 'F4_COMBO_HIJACK_SUSPECT' in f4_flags[0]


# ---------- F4 violation: naming layer detected misalignment ----------


def test_f4_blocked_flags_hijack():
    """Naming layer's combo_override_blocked=True signals misalignment.
    Condition (b): must be flagged even with winning combos."""
    engine = {
        'strategy': 'Burn Assault Combo',
        'win_condition': 'Infinite combat phases',
        'key_pieces': ['Aggravated Assault', 'Savage Ventmaw'],
        'confidence': 'MED',
    }
    deck_input = _make_deck_input(
        top_strategy='burn',
        top_share=0.51,
        n_win_combos=4,
        combo_override_blocked=True,
    )
    result = classify_engine('test__ozai-like', engine, deck_input)
    f4_flags = [f for f in result['flags'] if 'F4' in f]
    assert len(f4_flags) == 1, f'F4-blocked combo should be flagged as hijack, got: {f4_flags}'
    assert 'F4_COMBO_HIJACK_SUSPECT' in f4_flags[0]


# ---------- Edge cases ----------


def test_no_combo_in_strategy_no_flag():
    """Strategy does NOT mention combo at all: no F4 check fires."""
    engine = {
        'strategy': 'Lifegain Drain Engine',
        'win_condition': 'Drain opponents',
        'key_pieces': ['Karlov'],
        'confidence': 'HIGH',
    }
    deck_input = _make_deck_input(
        top_strategy='lifegain',
        top_share=0.80,
        n_win_combos=10,
    )
    result = classify_engine('test__no-combo-word', engine, deck_input)
    f4_flags = [f for f in result['flags'] if 'F4' in f]
    assert f4_flags == [], f'No combo in strategy should not trigger F4, got: {f4_flags}'


def test_low_share_no_flag():
    """Dominant archetype share < 0.50: F4 heuristic does not apply."""
    engine = {
        'strategy': 'Artifact Combo',
        'win_condition': 'Infinite mana',
        'key_pieces': ['Rings of Brighthearth'],
        'confidence': 'MED',
    }
    deck_input = _make_deck_input(
        top_strategy='artifacts',
        top_share=0.40,
        n_win_combos=5,
    )
    result = classify_engine('test__low-share', engine, deck_input)
    f4_flags = [f for f in result['flags'] if 'F4' in f]
    assert f4_flags == [], f'Low-share archetype should not trigger F4, got: {f4_flags}'


def test_no_deck_input_no_flag():
    """No deck_input available: F4 heuristic is skipped entirely."""
    engine = {
        'strategy': 'Sacrifice Combo Loop',
        'win_condition': 'Infinite death triggers',
        'key_pieces': ['Blood Artist'],
        'confidence': 'HIGH',
    }
    result = classify_engine('test__no-input', engine, None)
    f4_flags = [f for f in result['flags'] if 'F4' in f]
    assert f4_flags == [], f'No deck_input should skip F4, got: {f4_flags}'


def run_tests() -> None:
    """Simple test runner — no pytest dependency needed."""
    tests = [
        test_aligned_winning_combo_no_f4,
        test_aligned_graveyard_combo_no_f4,
        test_combo_with_override_applied_not_blocked,
        test_no_winning_combo_flags_f4,
        test_f4_blocked_flags_hijack,
        test_no_combo_in_strategy_no_flag,
        test_low_share_no_flag,
        test_no_deck_input_no_flag,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
            passed += 1
        except AssertionError as e:
            print(f'  FAIL  {t.__name__}: {e}')
            failed += 1
        except Exception as e:
            print(f'  ERROR {t.__name__}: {e}')
            failed += 1

    print(f'\n{passed} passed, {failed} failed')
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    run_tests()
