"""Golden regression labels — the validated per-deck verdicts.

These encode the behavior the T1→T5.5 study validated: the label (or tribe, or
abstain) the engine must produce for representative decks across the gauntlet +
owned corpus.
"""

from __future__ import annotations

from pipeline.transforms.strategy import resolve


def test_sokka_tribe_is_ally(load_deck, combos):
    rec = resolve(load_deck('owned__sokka'), combos)
    assert rec['tribe'] == 'Ally'


def test_tuvasa_is_enchantress(load_deck, combos):
    rec = resolve(load_deck('gauntlet__tuvasa-the-sunlit'), combos)
    assert rec['named_strategy'] == 'enchantress'


def test_grandlarceny_is_theft(load_deck, combos):
    rec = resolve(load_deck('gauntlet__grandlarceny_2024'), combos)
    assert rec['named_strategy'] == 'theft'


def test_kess_is_combo(load_deck, combos):
    rec = resolve(load_deck('gauntlet__kess-dissident-mage'), combos)
    assert rec['named_strategy'] == 'alt-wincon/combo'
    assert rec['combo_override_applied'] is True


def test_urza_is_combo(load_deck, combos):
    rec = resolve(load_deck('gauntlet__urza-lord-high-artificer'), combos)
    assert rec['named_strategy'] == 'alt-wincon/combo'
    assert rec['combo_override_applied'] is True


def test_krrik_is_combo(load_deck, combos):
    rec = resolve(load_deck('gauntlet__krrik-son-of-yawgmoth'), combos)
    assert rec['named_strategy'] == 'alt-wincon/combo'
    assert rec['combo_override_applied'] is True


def test_world_reclaimer_lands_with_fused_sacrifice_engine(load_deck, combos):
    rec = resolve(load_deck('owned__world-reclaimer'), combos)
    assert rec['named_strategy'] == 'lands-matter'
    strategies = rec['facts']['strategies']
    # the aristocrats/sacrifice plan is a real, requires-met strategy...
    assert strategies['aristocrats/sacrifice']['requires_met'] is True
    # ...and the top convergent engine FUSES lands + sacrifice (shared hubs)
    top_engine = rec['facts']['engines']['convergent_event_components'][0]
    assert 'lands-matter' in top_engine['archetypes']
    assert 'aristocrats/sacrifice' in top_engine['archetypes']


def test_baral_is_control(load_deck, combos):
    rec = resolve(load_deck('gauntlet__baral-chief-of-compliance'), combos)
    assert rec['named_strategy'] == 'control/permission'
    assert rec['confidence'] == 'HIGH'
    assert rec['control_cs_gate_met'] is True


def test_welcoming_vampire_goodstuff_abstains(load_deck, combos):
    rec = resolve(load_deck('owned__welcoming-vampire'), combos)
    assert rec['abstain'] is True
    assert rec['named_strategy'] is None
    # a goodstuff deck: nothing but support axes -> honest abstain (or low-conf)
    assert rec['abstain_reason'] in {'only-support-axis', 'no-coherent-engine', 'low-coverage'}
