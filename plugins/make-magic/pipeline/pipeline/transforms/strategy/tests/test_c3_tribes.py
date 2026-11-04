"""C3 — the facts-sidecar tribe nits: dominant-tribe winner + pseudo-subtype filter."""

from __future__ import annotations

from pipeline.transforms.strategy import resolve
from pipeline.transforms.strategy.tribes import NON_CREATURE_SUBTYPES, compute_tribes, creature_subtypes


def test_fantastic_four_tribe_is_dominant_hero_not_vote_winner_human(load_deck, combos):
    rec = resolve(load_deck('owned__the-fantastic-four'), combos)
    tribes = rec['facts']['tribes']
    # Hero overwhelmingly owns the creature base -> it is the dominant tribe...
    assert 'Hero' in tribes['dominant_tribes']
    # ...so the reported tribe is Hero, NOT the incidental co-printed Human
    assert rec['tribe'] == 'Hero'
    assert tribes['top'] == 'Hero'


def test_sokka_multi_dominant_keeps_highest_scoring_dominant(load_deck, combos):
    # Sokka has two dominant tribes (Ally, Human); the winner stays the
    # highest-scoring one (Ally), not clobbered by the C3 rule
    rec = resolve(load_deck('owned__sokka'), combos)
    assert rec['tribe'] == 'Ally'


def test_spiders_unaffected_no_dominant(load_deck, combos):
    # no dominant tribe -> C3a is a no-op, the raw vote winner (Spider) stands
    rec = resolve(load_deck('owned__spiders-play-with-their-food'), combos)
    assert rec['facts']['tribes']['dominant_tribes'] == []
    assert rec['tribe'] == 'Spider'


def test_pseudo_subtypes_filtered_from_creature_subtypes():
    # a dual-subtype type line must not leak the artifact/enchantment subtype
    assert creature_subtypes({'type_line': 'Artifact Creature — Clue Plant'}) == ['Plant']
    assert creature_subtypes({'type_line': 'Enchantment Creature — Aura Spirit'}) == ['Spirit']
    assert creature_subtypes({'type_line': 'Creature — Equipment'}) == []
    assert 'Clue' in NON_CREATURE_SUBTYPES
    assert 'Equipment' in NON_CREATURE_SUBTYPES


def test_pseudo_subtypes_never_win_tribe_vote():
    # even with heavy oracle-text references, a filtered pseudo-subtype cannot
    # register as a tribe (Clue would otherwise win on text votes)
    cards = [
        {'type_line': 'Artifact — Clue', 'oracle_text': 'investigate. Sacrifice this Clue: draw a card.'},
        {'type_line': 'Creature — Human Soldier', 'oracle_text': 'When this enters, investigate.',
         'role': 'commander'},
        {'type_line': 'Creature — Human Soldier', 'oracle_text': ''},
        {'type_line': 'Creature — Human Soldier', 'oracle_text': ''},
    ]
    info = compute_tribes(cards, sub_t=8, cmd_weight=5.0, typal_dom_share=0.70)
    assert all(d['tribe'] != 'Clue' for d in info['ranked'])
    assert info['top'] == 'Human'
