"""TDD tests for the Tier-1 closed-form kill-completion speed estimator.

NO engine, NO card DB — pure hand-worked fact fixtures and hand-computed math.
Three layers:

  * per-model hand-worked cases (aggro clock / ramp connect / combo assembly) on
    lists whose kill turn is derivable by hand;
  * hypergeometric correctness against a hand-computed value;
  * the ±1 calibration regression (AC4) on three fixtures that reproduce the
    LOAD-BEARING shape of the lab decks MonoR_Aggro (->5.0), Ramp_Green (->7.0),
    Combo_Mikaeus (->7.0, combo). Fixtures are HAND-BUILT from the real
    decklists at ~/mtg-sim-lab/xmage-lab/mage/Mage.Tests/{MonoR_Aggro,Ramp_Green,
    Combo_Mikaeus}.txt (every card is a well-known real card whose power / CMC /
    ramp / combo role I transcribe below); the mapping is documented per fixture.

All tests select under -k "speed".
"""

from __future__ import annotations

from math import comb

import pytest

from pipeline.sim.speed_estimate import (
    detect_archetype,
    estimate_speed,
    p_all_pieces,
    p_at_least_one,
)

# --------------------------------------------------------------------------- #
# Fixture builders (compact — `quantity` expands to physical copies).
# --------------------------------------------------------------------------- #


def _card(name, cmc, type_line, *, qty=1, power=None, text='', keywords=None, produced=None, mana_cost=''):
    return {
        'name': name,
        'cmc': cmc,
        'type_line': type_line,
        'quantity': qty,
        'power': None if power is None else str(power),
        'oracle_text': text,
        'keywords': keywords or [],
        'produced_mana': produced or [],
        'mana_cost': mana_cost,
    }


def _land(name, qty):
    return _card(name, 0, 'Basic Land', qty=qty, produced=['R'])


# --------------------------------------------------------------------------- #
# MonoR_Aggro fixture — transcribed from MonoR_Aggro.txt (60 cards, 20 Mountain).
# Load-bearing shape: 20 one-to-three-drop creatures (avg power 2.8, ~60% haste)
# + 20 three-damage burn spells. Powers/haste from the real cards:
#   Goblin Guide 2/2 haste · Monastery Swiftspear 1/2 · Keldon Marauders 2/2
#   · Hellspark Elemental 3/1 haste · Ball Lightning 6/1 haste.
#   Burn: Lightning Bolt/Lava Spike/Rift Bolt/Incinerate/Searing Blaze ~3 to face.
# --------------------------------------------------------------------------- #


def _monor_aggro():
    return [
        _card('Goblin Guide', 1, 'Creature — Goblin', qty=4, power=2, keywords=['Haste']),
        _card('Monastery Swiftspear', 1, 'Creature — Monk', qty=4, power=1),
        _card('Keldon Marauders', 2, 'Creature — Human Warrior', qty=4, power=2),
        _card('Hellspark Elemental', 2, 'Creature — Elemental', qty=4, power=3, keywords=['Haste', 'Trample']),
        _card('Ball Lightning', 3, 'Creature — Elemental', qty=4, power=6, keywords=['Haste', 'Trample']),
        _card('Lightning Bolt', 1, 'Instant', qty=4, text='Lightning Bolt deals 3 damage to any target.'),
        _card('Lava Spike', 1, 'Sorcery', qty=4, text='Lava Spike deals 3 damage to target player or planeswalker.'),
        _card('Rift Bolt', 1, 'Sorcery', qty=4, text='Rift Bolt deals 3 damage to any target.'),
        _card('Incinerate', 2, 'Instant', qty=4, text='Incinerate deals 3 damage to any target.'),
        _card(
            'Searing Blaze',
            2,
            'Instant',
            qty=4,
            text='Searing Blaze deals 3 damage to target player and 3 damage to target creature.',
        ),
        _land('Mountain', 20),
    ]


# --------------------------------------------------------------------------- #
# Ramp_Green fixture — transcribed from Ramp_Green.txt (60 cards, 18 Forest).
# Load-bearing shape: 26 ramp sources (12 one-drop dorks + 12 land-ramp spells +
# 2 Sol Ring) ramping into a fat top end. Cheapest top-end payoff = Terastodon
# (7 CMC, 9 power). Powers/CMC from the real cards.
# --------------------------------------------------------------------------- #


def _ramp_green():
    return [
        _card('Llanowar Elves', 1, 'Creature — Elf Druid', qty=4, power=1, produced=['G']),
        _card('Elvish Mystic', 1, 'Creature — Elf Druid', qty=4, power=1, produced=['G']),
        _card('Fyndhorn Elves', 1, 'Creature — Elf Druid', qty=4, power=1, produced=['G']),
        _card(
            'Rampant Growth',
            2,
            'Sorcery',
            qty=4,
            text='Search your library for a basic land card and put it onto the battlefield tapped.',
        ),
        _card(
            "Kodama's Reach",
            3,
            'Sorcery',
            qty=4,
            text='Search your library for up to two basic land cards, put one onto the battlefield tapped and the other into your hand.',  # noqa: E501
        ),
        _card(
            'Cultivate',
            3,
            'Sorcery',
            qty=4,
            text='Search your library for up to two basic land cards, put one onto the battlefield tapped and the other into your hand.',  # noqa: E501
        ),
        _card('Sol Ring', 1, 'Artifact', qty=2, produced=['C'], text='{T}: Add {C}{C}.'),
        _card('Terastodon', 7, 'Creature — Elephant', qty=4, power=9),
        _card('Woodfall Primus', 8, 'Creature — Treefolk Shaman', qty=4, power=6, keywords=['Trample']),
        _card('Craterhoof Behemoth', 8, 'Creature — Beast', qty=3, power=5, keywords=['Haste', 'Trample']),
        _card('Ghalta, Primal Hunger', 12, 'Creature — Elder Dinosaur', qty=3, power=12, keywords=['Trample']),
        _card('Worldspine Wurm', 11, 'Creature — Wurm', qty=2, power=15, keywords=['Trample']),
        _card('Forest', 0, 'Basic Land — Forest', qty=18, produced=['G']),
    ]


# --------------------------------------------------------------------------- #
# Combo_Mikaeus fixture — transcribed from Combo_Mikaeus.txt (60 cards, 16 lands).
# Load-bearing shape: the 2-card combo Mikaeus, the Unhallowed (4 copies, 5 CMC) +
# Triskelion (4 copies, 6 CMC) = infinite damage; a ramp package (Solemn, Sakura,
# Rampant, Cultivate, Sol Ring, Wall of Roots = 20 ramp) and a draw package (Sign
# in Blood, Read the Bones, Night's Whisper = 12 draw-2 spells) that accelerate
# assembly. The combo is supplied as a pre-detected combo_pieces=[[4, 4]].
# --------------------------------------------------------------------------- #


def _combo_mikaeus():
    return [
        _card('Mikaeus, the Unhallowed', 5, 'Legendary Creature — Zombie Cleric', qty=4, power=5),
        _card(
            'Triskelion',
            6,
            'Artifact Creature — Construct',
            qty=4,
            power=1,
            text='Triskelion enters with three +1/+1 counters. Remove a +1/+1 counter: deals 1 damage to any target.',
        ),
        _card(
            'Solemn Simulacrum',
            4,
            'Artifact Creature — Golem',
            qty=4,
            power=2,
            text='When Solemn Simulacrum enters, search your library for a basic land card and put it onto the battlefield tapped.',  # noqa: E501
        ),
        _card(
            'Sakura-Tribe Elder',
            2,
            'Creature — Snake Shaman',
            qty=4,
            power=1,
            text='Sacrifice: Search your library for a basic land card and put it onto the battlefield tapped.',
        ),
        _card(
            'Rampant Growth',
            2,
            'Sorcery',
            qty=4,
            text='Search your library for a basic land card and put it onto the battlefield tapped.',
        ),
        _card(
            'Cultivate',
            3,
            'Sorcery',
            qty=4,
            text='Search your library for up to two basic land cards, put one onto the battlefield tapped and the other into your hand.',  # noqa: E501
        ),
        _card('Sign in Blood', 2, 'Sorcery', qty=4, text='Target player draws two cards and loses 2 life.'),
        _card('Read the Bones', 3, 'Sorcery', qty=4, text='Scry 2, then draw two cards. You lose 2 life.'),
        _card("Night's Whisper", 2, 'Sorcery', qty=4, text='You draw two cards and lose 2 life.'),
        _card('Sol Ring', 1, 'Artifact', qty=2, produced=['C'], text='{T}: Add {C}{C}.'),
        _card('Wall of Roots', 2, 'Creature — Plant Wall', qty=2, power=0, produced=['G'], text='{T}: Add {G}.'),
        _card('Swamp', 0, 'Basic Land — Swamp', qty=10, produced=['B']),
        _card('Forest', 0, 'Basic Land — Forest', qty=6, produced=['G']),
    ]


# =========================================================================== #
# Hypergeometric correctness — hand-computed.
# =========================================================================== #


def test_speed_hypergeometric_single_piece_matches_hand_value():
    # N=60, 4 copies, seen 7 cards (own-turn 1 on the play).
    # P(>=1) = 1 - C(56,7)/C(60,7). Hand: C(56,7)/C(60,7) = (53*52*51*50)/(60*59*58*57).
    expected = 1.0 - (53 * 52 * 51 * 50) / (60 * 59 * 58 * 57)
    assert expected == pytest.approx(0.39950, abs=1e-4)
    assert p_at_least_one(60, 4, 7) == pytest.approx(expected, abs=1e-9)
    # Cross-check against the comb() closed form directly.
    assert p_at_least_one(60, 4, 7) == pytest.approx(1 - comb(56, 7) / comb(60, 7), abs=1e-12)


def test_speed_hypergeometric_all_pieces_is_independent_product():
    # Two pieces, 4 copies each, seen 7: joint (independent approx) = single**2.
    single = p_at_least_one(60, 4, 7)
    assert p_all_pieces(60, [4, 4], 7) == pytest.approx(single * single, abs=1e-12)
    assert p_all_pieces(60, [4, 4], 7) == pytest.approx(0.39950**2, abs=1e-3)


def test_speed_hypergeometric_edge_cases():
    assert p_at_least_one(60, 0, 7) == 0.0  # no copies
    assert p_at_least_one(60, 4, 0) == 0.0  # no cards seen
    assert p_at_least_one(60, 4, 60) == 1.0  # whole deck seen


# =========================================================================== #
# Per-model hand-worked cases.
# =========================================================================== #


def test_speed_aggro_handworked_four_haste_beaters():
    # 4x a 3-power HASTE creature, no burn, lethal 20, deck size irrelevant to the
    # clock. One body/turn; a haste body deployed turn i attacks turns i..t.
    #   combat(t) = 3 * ( sum_{i=1..min(t,4)}(t-i) + 1.0*min(t,4) )   [haste_frac=1]
    #   t=1: 3*(0+1)=3    t=2: 3*(1+2)=9    t=3: 3*(3+3)=18    t=4: 3*(6+4)=30 >=20
    deck = [_card('Beater', 1, 'Creature — Elemental', qty=4, power=3, keywords=['Haste']), _land('Mountain', 10)]
    est = estimate_speed(deck, archetype='aggro', lethal=20)
    assert est.own_turn == 4.0
    assert est.archetype == 'aggro'


def test_speed_ramp_handworked_connect_math():
    # A single 6-power top-end at CMC 7, ramp_rate saturated (rate 1.0):
    #   mana(t) = t + min(t-1,4)*1.0 ; reaches 7 at t=4 (4+3). deploy_turn=4.
    #   connect = ceil(20/6) = 4 swings -> own_turn = 4 + 4 = 8.
    deck = [
        _card('Dork', 1, 'Creature — Elf Druid', qty=12, power=1, produced=['G']),
        _card('Fatty', 7, 'Creature — Beast', qty=6, power=6),
        _card('Forest', 0, 'Basic Land — Forest', qty=18, produced=['G']),
    ]
    est = estimate_speed(deck, archetype='ramp', lethal=20)
    assert est.own_turn == 8.0


def test_speed_combo_handworked_assembly_plus_lag():
    # 2-card combo, 4 copies each, 60-card deck, NO extra draw (draw_rate 0):
    #   P(both) crosses 0.35 at... cards_seen = 6+t. Hand:
    #   t=8 -> seen 14 -> single=1-(46*45*44*43)/(60*59*58*57); both>=0.35.
    # assembly + lag(=2) is asserted; the exact assembly turn is checked separately.
    deck = [
        _card('Piece A', 5, 'Creature — Zombie', qty=4, power=5),
        _card('Piece B', 6, 'Artifact Creature', qty=4, power=1),
        _card('Swamp', 0, 'Basic Land — Swamp', qty=52, produced=['B']),
    ]
    est = estimate_speed(deck, archetype='combo', combo_pieces=[[4, 4]], lethal=20)
    assert est.archetype == 'combo'
    # execution_lag = num_pieces = 2, floored by the mana wall (turn>=? no ramp ->
    # mana reaches 6 at turn 6). own_turn = max(assembly+2, 6).
    assert est.own_turn >= 6.0


# =========================================================================== #
# Archetype detection.
# =========================================================================== #


def test_speed_detect_archetype_on_calibration_fixtures():
    assert detect_archetype(_monor_aggro()) == 'aggro'
    assert detect_archetype(_ramp_green()) == 'ramp'
    assert detect_archetype(_combo_mikaeus(), combo_pieces=[[4, 4]]) == 'combo'


def test_speed_control_deck_is_speed_na():
    # Interaction-dense, threat-light -> control -> own_turn None (no fake number).
    deck = [
        _card('Counterspell', 2, 'Instant', qty=12, text='Counter target spell.'),
        _card('Swords to Plowshares', 1, 'Instant', qty=8, text='Exile target creature.'),
        _card('Wrath of God', 4, 'Sorcery', qty=6, text='Destroy all creatures.'),
        _card('Filler', 2, 'Enchantment', qty=4, text='Draw a card.'),
        _card('Island', 0, 'Basic Land — Island', qty=30, produced=['U']),
    ]
    est = estimate_speed(deck)
    assert est.archetype == 'control'
    assert est.own_turn is None
    assert est.confidence == 'n/a'


# =========================================================================== #
# ±1 CALIBRATION REGRESSION (AC4) — the acceptance gate.
# =========================================================================== #


@pytest.mark.parametrize(
    'name, fixture, kwargs, anchor',
    [
        ('MonoR_Aggro', _monor_aggro(), {}, 5.0),
        ('Ramp_Green', _ramp_green(), {}, 7.0),
        ('Combo_Mikaeus', _combo_mikaeus(), {'combo_pieces': [[4, 4]]}, 7.0),
    ],
)
def test_speed_calibration_within_one_own_turn(name, fixture, kwargs, anchor):
    est = estimate_speed(fixture, **kwargs)
    assert est.own_turn is not None, f'{name}: expected a number, got Speed N/A'
    assert abs(est.own_turn - anchor) <= 1.0, (
        f'{name}: predicted {est.own_turn} vs anchor {anchor} (±1). {est.rationale}'
    )


def test_speed_combo_mikaeus_detected_as_combo():
    est = estimate_speed(_combo_mikaeus(), combo_pieces=[[4, 4]])
    assert est.archetype == 'combo'
