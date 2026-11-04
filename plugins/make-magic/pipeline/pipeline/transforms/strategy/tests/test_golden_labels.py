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


def test_abzan_armor_is_toughness_matters(load_deck, combos):
    # Felothar (assign-damage-by-toughness + Walls-attack) is a toughness-matters
    # deck. Before the vocab add it shattered across lifegain/anthem/counters and
    # read "diffuse"; the dedicated archetype recovers the real identity.
    rec = resolve(load_deck('gauntlet__abzanarmor_2025'), combos)
    assert rec['named_strategy'] == 'toughness-matters'
    assert rec['strategy_identity'][0]['strategy'] == 'toughness-matters'


def test_faceless_menace_is_morph(load_deck, combos):
    # Kadena morph deck. Morph is a KEYWORD (no clean otag), so detection is
    # synthesized from the keyword — the archetype must see the full body of
    # face-down creatures, not just the handful of flip-trigger payoffs.
    rec = resolve(load_deck('gauntlet__facelessmenace_sultai_2019'), combos)
    assert rec['named_strategy'] == 'morph'
    assert rec['facts']['strategies']['morph']['n_member_cards'] >= 12


def test_welcoming_vampire_goodstuff_abstains(load_deck, combos):
    rec = resolve(load_deck('owned__welcoming-vampire'), combos)
    assert rec['abstain'] is True
    assert rec['named_strategy'] is None
    # a goodstuff deck: nothing but support axes -> honest abstain (or low-conf)
    assert rec['abstain_reason'] in {'only-support-axis', 'no-coherent-engine', 'low-coverage'}
    # an abstaining deck has no identity set
    assert rec['strategy_identity'] == []


def test_grandlarceny_strategy_identity_is_ranked_set(load_deck, combos):
    # the single-label collapse hid the real #2 axis; the set surfaces it.
    rec = resolve(load_deck('gauntlet__grandlarceny_2024'), combos)
    sid = [s['strategy'] for s in rec['strategy_identity']]
    assert sid[0] == 'theft'          # leads with the headline
    assert 'mill' in sid              # ...and surfaces the real second axis
    assert len(sid) <= 3              # capped at max_identity
    wqs = [s['weighted_quantity'] for s in rec['strategy_identity']]
    assert wqs == sorted(wqs, reverse=True)  # ranked descending


def test_strategy_identity_headline_matches_named_strategy(load_deck, combos):
    # for a non-combo, non-abstaining deck, strategy_identity[0] IS the headline
    # (named_strategy is kept as a back-compat alias of the set's leader).
    for fx, label in [('gauntlet__grandlarceny_2024', 'theft'),
                      ('gauntlet__tuvasa-the-sunlit', 'enchantress'),
                      ('gauntlet__baral-chief-of-compliance', 'control/permission')]:
        rec = resolve(load_deck(fx), combos)
        assert rec['named_strategy'] == label
        assert rec['strategy_identity'][0]['strategy'] == label


def test_kess_combo_keeps_strategic_identity_set(load_deck, combos):
    # combo is a separate axis: the override sets named_strategy + combo_identity,
    # but strategy_identity still reports the deck's strategic composition.
    rec = resolve(load_deck('gauntlet__kess-dissident-mage'), combos)
    assert rec['named_strategy'] == 'alt-wincon/combo'
    assert rec['combo_override_applied'] is True
    assert 'alt-wincon/combo' not in [s['strategy'] for s in rec['strategy_identity']]
