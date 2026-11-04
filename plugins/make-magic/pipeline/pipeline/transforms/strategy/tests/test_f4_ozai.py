"""F4 — the Ozai over-fire fix: a soft mana-sink promotion must not override a
dense, dominant proactive plan, while real cEDH combo residues still flip.
"""

from __future__ import annotations

from pipeline.transforms.strategy import resolve
from pipeline.transforms.strategy.config import DEFAULT_CONFIG


def test_ozai_stays_burn_not_combo(load_deck, combos):
    rec = resolve(load_deck('owned__ozai'), combos)
    assert rec['named_strategy'] == 'burn'
    assert rec['combo_override_applied'] is False
    # the override WAS otherwise eligible (win-combos present, residue LOW) but was
    # blocked by the dense-primary guard
    assert rec['n_win_combos'] >= DEFAULT_CONFIG.combo_win_min
    assert rec['combo_override_f4_blocked'] is True
    assert rec['pre_combo_name'] == 'burn'


def test_ozai_primary_is_dense_and_dominant(load_deck, combos):
    rec = resolve(load_deck('owned__ozai'), combos)
    sig = rec['confidence_signals']
    # burn owns a majority of the identity mass and clearly beats #2 — the exact
    # signal the guard keys on
    assert sig['identity_share'] >= DEFAULT_CONFIG.combo_primary_share
    assert sig['margin'] >= DEFAULT_CONFIG.combo_primary_margin


def test_cedh_combo_decks_still_override(load_deck, combos):
    # the three canonical cEDH combo decks have scattered (share < 0.50) residues,
    # so F4 does NOT block them
    for slug in ('gauntlet__kess-dissident-mage',
                 'gauntlet__urza-lord-high-artificer',
                 'gauntlet__krrik-son-of-yawgmoth'):
        rec = resolve(load_deck(slug), combos)
        assert rec['named_strategy'] == 'alt-wincon/combo', slug
        assert rec['combo_override_applied'] is True, slug
        assert rec['combo_override_f4_blocked'] is False, slug


def test_f4_guard_is_configurable_off(load_deck, combos):
    # with the guard relaxed (share threshold > 1), Ozai flips back to combo,
    # proving the ONLY thing holding it to burn is the F4 guard
    cfg = DEFAULT_CONFIG.with_overrides(combo_primary_share=2.0)
    rec = resolve(load_deck('owned__ozai'), combos, cfg=cfg)
    assert rec['named_strategy'] == 'alt-wincon/combo'
    assert rec['combo_override_applied'] is True
