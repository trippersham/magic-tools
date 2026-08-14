"""TDD tests for the CRISPI per-card classifier (``transforms/crispi.classify_card``).

Phase 1 is ONLY per-card classification — no axis-ladder math. These tests lock:

  * the named-table path (rubric-named cards resolve to their exact tier),
  * the structured heuristic path for UNNAMED cards (symmetric/combat draw, CMC-band
    tutors) — conservatively, never landing an unnamed card in a premium tier,
  * interaction stack-point COMPOSITION across classes (free counterspell = 5),
  * that a resolved instant-speed removal reads as interaction + earns the instant credit,
  * graceful handling of an unresolved/inert card (no crash, no tiers).

All tests are marked so ``-k classify`` selects them. No network — pure data.
"""

from __future__ import annotations

import pytest

from pipeline.transforms.combo_detect import Combo
from pipeline.transforms.crispi import (
    CardTiers,
    classify_card,
    consistency_axis,
    consistency_totals,
    crispi_score,
    interaction_axis,
    interp,
    resilience_axis,
    snap_quarter,
    speed_axis,
)

# --------------------------------------------------------------------------- #
# Fixtures — minimal card dicts + an oracle_id -> slug-closure otag map.
#
# `classify_card(card, card_otag)` keys otags by str(oracle_id). A card with no
# oracle_id (or an id absent from the map) simply has an empty slug closure.
# --------------------------------------------------------------------------- #


def _card(
    name: str,
    *,
    oracle_id: str | None = None,
    oracle_text: str = '',
    type_line: str = '',
    cmc: float | None = 0.0,
    keywords: list[str] | None = None,
    produced_mana: list[str] | None = None,
    mana_cost: str = '',
    power: str | None = None,
) -> dict:
    return {
        'name': name,
        'oracle_id': oracle_id,
        'oracle_text': oracle_text,
        'type_line': type_line,
        'cmc': cmc,
        'keywords': keywords or [],
        'produced_mana': produced_mana,
        'mana_cost': mana_cost,
        'power': power,
    }


# --------------------------------------------------------------------------- #
# Named-table path — the rubric's alignment anchors.
# --------------------------------------------------------------------------- #


def test_classify_named_premium_tutor():
    c = _card('Demonic Tutor', type_line='Sorcery', cmc=2.0)
    t = classify_card(c, {})
    assert t.tutor is not None
    assert t.tutor == ('premium', 6)
    assert t.tutor_why


def test_classify_named_premium_asymmetric_draw():
    c = _card('Rhystic Study', type_line='Enchantment', cmc=3.0)
    t = classify_card(c, {})
    assert t.draw == ('premium-asymmetric', 5)


def test_classify_named_one_shot_draw():
    c = _card("Night's Whisper", type_line='Sorcery', cmc=2.0)
    t = classify_card(c, {})
    assert t.draw == ('one-shot', 2)


def test_classify_named_free_counterspell_composes():
    # Force of Will: named `free` (2) AND a counterspell (2) AND instant-speed (+1) = 5.
    c = _card(
        'Force of Will',
        type_line='Instant',
        cmc=5.0,
        oracle_text='Counter target spell.',
    )
    t = classify_card(c, {})
    assert t.is_interaction is True
    assert t.stack_points == 5


# --------------------------------------------------------------------------- #
# Heuristic path — UNNAMED cards. Conservative: never premium/burst.
# --------------------------------------------------------------------------- #


def test_classify_unnamed_symmetric_drawer_is_2():
    c = _card(
        'Fictional Symmetric Mine',
        oracle_id='sym-1',
        type_line='Artifact',
        cmc=2.0,
        oracle_text="At the beginning of each player's draw step, that player draws an additional card.",
    )
    otag = {'sym-1': {'card-advantage'}}
    t = classify_card(c, otag)
    assert t.draw is not None
    label, pts = t.draw
    assert pts == 2
    assert label == 'symmetric'


def test_classify_unnamed_combat_drawer_is_3():
    c = _card(
        'Fictional Combat Drawer',
        oracle_id='cmb-1',
        type_line='Creature — Beast',
        cmc=3.0,
        oracle_text='Whenever this creature attacks, draw a card.',
    )
    otag = {'cmb-1': {'card-advantage'}}
    t = classify_card(c, otag)
    assert t.draw is not None
    label, pts = t.draw
    assert pts == 3
    assert label == 'combat-conditioned'


def test_classify_unnamed_expensive_tutor_is_narrow_2():
    c = _card(
        'Fictional Big Tutor',
        oracle_id='tut-5',
        type_line='Sorcery',
        cmc=5.0,
        oracle_text='Search your library for a card and put it into your hand.',
    )
    otag = {'tut-5': {'tutor'}}
    t = classify_card(c, otag)
    assert t.tutor is not None
    label, pts = t.tutor
    assert pts == 2
    assert label == 'narrow'


def test_classify_unnamed_cheap_tutor_is_standard_not_premium():
    # CMC <=2 UNNAMED tutor is standard(4), NOT premium(6) — premium is named-only.
    c = _card(
        'Fictional Cheap Tutor',
        oracle_id='tut-2',
        type_line='Instant',
        cmc=2.0,
        oracle_text='Search your library for a card and put it into your hand.',
    )
    otag = {'tut-2': {'tutor'}}
    t = classify_card(c, otag)
    assert t.tutor == ('standard', 4)


def test_classify_unnamed_mid_tutor_is_standard_4():
    c = _card(
        'Fictional Mid Tutor',
        oracle_id='tut-3',
        type_line='Sorcery',
        cmc=3.0,
        oracle_text='Search your library for a card.',
    )
    otag = {'tut-3': {'tutor'}}
    t = classify_card(c, otag)
    assert t.tutor == ('standard', 4)


def test_classify_unnamed_never_burst_or_premium_asymmetric_draw():
    # An unnamed strong drawer must not reach burst(6) or premium-asymmetric(5).
    c = _card(
        'Fictional Big Draw',
        oracle_id='draw-x',
        type_line='Sorcery',
        cmc=4.0,
        oracle_text='Draw four cards.',
    )
    otag = {'draw-x': {'card-advantage'}}
    t = classify_card(c, otag)
    assert t.draw is not None
    _, pts = t.draw
    assert pts <= 4


# --------------------------------------------------------------------------- #
# Interaction — membership + instant-speed credit.
# --------------------------------------------------------------------------- #


def test_classify_resolved_instant_removal_is_interaction_with_credit():
    # Named premium-instant removal: is_interaction True + +1 instant credit.
    c = _card('Swords to Plowshares', type_line='Instant', cmc=1.0)
    t = classify_card(c, {})
    assert t.is_interaction is True
    assert t.stack_points == 1
    assert t.interaction_why


def test_classify_otag_removal_is_interaction():
    c = _card(
        'Fictional Removal',
        oracle_id='rem-1',
        type_line='Sorcery',
        cmc=3.0,
        oracle_text='Destroy target creature.',
    )
    otag = {'rem-1': {'removal'}}
    t = classify_card(c, otag)
    assert t.is_interaction is True


def test_classify_otag_counterspell_composes_instant_credit():
    # An unnamed hard counterspell (otag counterspell) at instant speed = 2 + 1 = 3.
    c = _card(
        'Fictional Counter',
        oracle_id='ctr-1',
        type_line='Instant',
        cmc=2.0,
        oracle_text='Counter target spell.',
    )
    otag = {'ctr-1': {'counterspell'}}
    t = classify_card(c, otag)
    assert t.is_interaction is True
    assert t.stack_points == 3


def test_classify_generic_instant_removal_earns_universal_stack_credit():
    # FIX A. Rubric ~194: "1 point — every interaction piece usable at INSTANT speed
    # (instant type or flash). This stacks with the classes above." A generic
    # (non-premium, non-counter) instant-speed removal spell must earn +1 stack, not 0.
    c = _card(
        'Beast Within',
        oracle_id='bw-1',
        type_line='Instant',
        cmc=3.0,
        oracle_text='Destroy target permanent.',
    )
    otag = {'bw-1': {'removal'}}
    t = classify_card(c, otag)
    assert t.is_interaction is True
    assert t.stack_points == 1  # universal instant credit, independent of premium/named


def test_classify_generic_sorcery_removal_earns_no_stack_credit():
    # FIX A (converse). Rubric ~194: "Sorcery-speed removal — however hard its scope —
    # earns 0: it can never answer a combo turn."
    c = _card(
        'Fictional Sorcery Kill',
        oracle_id='sk-1',
        type_line='Sorcery',
        cmc=3.0,
        oracle_text='Destroy target creature.',
    )
    otag = {'sk-1': {'removal'}}
    t = classify_card(c, otag)
    assert t.is_interaction is True
    assert t.stack_points == 0


def test_classify_effective_counterspell_bucket_earns_2():
    # FIX B. Rubric ~197: an otag `counterspells`-bucket card (an "effective
    # counterspell", e.g. Redirect Lightning) counts as a counterspell (+2) even
    # when it is not in INTERACTION_TIERS and its text has no "counter target".
    c = _card(
        'Redirect Lightning',
        oracle_id='rl-1',
        type_line='Sorcery',  # sorcery-speed so no instant credit muddies the +2
        cmc=3.0,
        oracle_text='Change the target of target spell or ability with a single target.',
    )
    otag = {'rl-1': {'counterspell'}}  # rolls up to the `counterspells` bucket
    t = classify_card(c, otag)
    assert t.is_interaction is True
    assert t.stack_points == 2  # effective counterspell +2, no instant (sorcery)


# --------------------------------------------------------------------------- #
# Resilience signals.
# --------------------------------------------------------------------------- #


def test_classify_big_creature_is_threat():
    c = _card(
        'Fictional Beater',
        oracle_id='beat-1',
        type_line='Creature — Giant',
        cmc=6.0,
        oracle_text='',
        power='7',
    )
    t = classify_card(c, {})
    assert t.is_threat is True


def test_classify_vanilla_beatstick_weighs_half():
    c = _card(
        'Fictional Vanilla',
        oracle_id='van-1',
        type_line='Creature — Ox',
        cmc=6.0,
        oracle_text='',
        power='6',
    )
    t = classify_card(c, {})
    assert t.is_threat is True
    assert t.threat_weight == 0.5


# --------------------------------------------------------------------------- #
# R3-1 — recursion detection (two-sided: tighten over-count, broaden under-count).
# --------------------------------------------------------------------------- #


def test_classify_land_animation_is_not_recursion():
    # R3-1 OVER-COUNT (Bumi). Land-animation / land-ramp reminder text ("target land
    # you control becomes a 0/0 creature ... it's still a land ... exile ... return")
    # must NOT trip recursion points — there is no graveyard/exile SOURCE recursion of
    # a creature/permanent here, only land animation.
    c = _card(
        'Fictional Earthbend',
        oracle_id='eb-1',
        type_line='Sorcery',
        oracle_text=(
            'target land you control becomes a 0/0 creature with haste that is still a land. '
            'put three +1/+1 counters on it. (if it would leave the battlefield, exile it '
            'instead, then return it to the battlefield.)'
        ),
    )
    t = classify_card(c, {})
    assert t.recursion_points == 0.0


def test_classify_land_ramp_return_is_not_recursion():
    # R3-1 OVER-COUNT. "Return that land to the battlefield" / "put a land onto the
    # battlefield" (land-ramp / animation) is NOT permanent recursion.
    c = _card(
        'Fictional Landramp',
        oracle_id='lr-1',
        type_line='Instant',
        oracle_text='return target land card from your graveyard to the battlefield tapped.',
    )
    t = classify_card(c, {})
    assert t.recursion_points == 0.0


def test_classify_unearth_permanent_is_recursion():
    # R3-1 UNDER-COUNT (Wakanda: Cityscape Leveler). The unearth keyword is a
    # permanent-recast recursion engine -> recursion points > 0.
    c = _card(
        'Fictional Unearther',
        oracle_id='ue-1',
        type_line='Artifact Creature — Construct',
        oracle_text='trample\nunearth {2}{R} (you may pay to return this from your graveyard.)',
        keywords=['Unearth'],
    )
    t = classify_card(c, {})
    assert t.recursion_points >= 1.0


def test_classify_reanimate_otag_is_recursion():
    # R3-1 UNDER-COUNT (Wakanda: Conduit of Worlds / Trading Post). A card whose otag
    # closure carries a (nonland) recursion/reanimation slug counts as recursion.
    c = _card('Fictional Reanimator', oracle_id='ra-1', type_line='Enchantment')
    otag = {'ra-1': {'recursion', 'reanimate', 'reanimate-nonland'}}
    t = classify_card(c, otag)
    assert t.recursion_points >= 1.5


def test_classify_land_recursion_otag_is_not_recursion():
    # R3-1. A card tagged ONLY as land-recursion (reanimate-land / recursion-land,
    # World Reclaimer's Aftermath Analyst class) is land-ramp, not the permanent
    # rebuild the combat rows measure.
    c = _card('Fictional Landbacker', oracle_id='lb-1', type_line='Creature — Druid')
    otag = {'lb-1': {'reanimate-land', 'recursion-land'}}
    t = classify_card(c, otag)
    assert t.recursion_points == 0.0


def test_classify_graveyard_creature_recursion_still_counts():
    # R3-1. A genuine graveyard->battlefield creature recursion (Loyal Retainers class)
    # is preserved after the tighten.
    c = _card(
        'Fictional Retainer',
        oracle_id='gr-1',
        type_line='Creature — Human',
        oracle_text='sacrifice this creature: return target creature card from your graveyard to the battlefield.',
    )
    t = classify_card(c, {})
    assert t.recursion_points >= 1.0


# --------------------------------------------------------------------------- #
# Graceful — unresolved / inert card.
# --------------------------------------------------------------------------- #


def test_classify_inert_card_no_crash_no_tiers():
    c = _card('Unresolved Card', oracle_id=None, type_line='', cmc=None)
    t = classify_card(c, {})
    assert t.draw is None
    assert t.tutor is None
    assert t.is_interaction is False
    assert t.stack_points == 0
    assert t.is_threat is False


# =========================================================================== #
# Phase 2 — reusable core: snap_quarter + interp (ladder).
# =========================================================================== #


def test_ladder_snap_quarter_rounds_to_nearest_quarter():
    assert snap_quarter(6.24) == 6.25
    assert snap_quarter(6.26) == 6.25
    assert snap_quarter(6.30) == 6.25
    assert snap_quarter(6.40) == 6.5


def test_ladder_snap_quarter_midpoint_rounds_up():
    # An exact eighth (halfway between two quarters) rounds UP (rubric: midpoints up).
    assert snap_quarter(6.125) == 6.25
    assert snap_quarter(0.125) == 1.0  # rounds up to 0.25 then clamps to 1.0
    assert snap_quarter(5.375) == 5.5


def test_ladder_snap_quarter_clamps_1_to_10():
    assert snap_quarter(0.0) == 1.0
    assert snap_quarter(-3.0) == 1.0
    assert snap_quarter(11.0) == 10.0
    assert snap_quarter(9.99) == 10.0


def test_ladder_interp_on_anchor_reads_anchor():
    anchors = [(0.0, 3.5), (12.0, 4.5), (24.0, 6.25), (68.0, 10.0)]
    assert interp(0.0, anchors) == 3.5
    assert interp(12.0, anchors) == 4.5
    assert interp(24.0, anchors) == 6.25
    assert interp(68.0, anchors) == 10.0


def test_ladder_interp_between_anchors_is_linear():
    anchors = [(0.0, 4.0), (10.0, 6.0)]
    assert interp(5.0, anchors) == 5.0
    assert interp(2.5, anchors) == 4.5


def test_ladder_interp_clamps_open_top_and_bottom():
    anchors = [(0.0, 3.5), (68.0, 10.0)]
    assert interp(-5.0, anchors) == 3.5  # below first
    assert interp(200.0, anchors) == 10.0  # 68+ open top


def test_ladder_tutor_anchor_values():
    # Transcribed anchors: 0->3.5 · 12->4.5 · 20->5.5 · 24->6.25 · 32->7 · 44->8 · 56->9 · 68+->10.
    from pipeline.transforms.crispi import _TUTOR_LADDER

    expected = {0: 3.5, 12: 4.5, 20: 5.5, 24: 6.25, 32: 7.0, 44: 8.0, 56: 9.0, 68: 10.0}
    for x, y in expected.items():
        assert interp(x, _TUTOR_LADDER) == y


# =========================================================================== #
# Phase 2 — Consistency axis.
# =========================================================================== #

# Neutral mana_facts: a healthy 36-land base, no penalty.
_HEALTHY_MANA = {'land_count': 36, 'rock_count': 0, 'dork_count': 0, 'avg_cmc': 3.0}


def _draw_card(name: str, label: str, pts: int) -> CardTiers:
    return CardTiers(name=name, draw=(label, pts))


def _tutor_card(name: str, label: str, pts: int, *, graveyard_gated: bool = False) -> CardTiers:
    return CardTiers(name=name, tutor=(label, pts), graveyard_gated=graveyard_gated)


def test_consistency_joint_min_binds_on_empty_tutors():
    # Strong DRAW column (many burst/premium engines) but ZERO tutors -> the tutorless
    # tutor column (reads 3.5) binds the whole axis LOW despite great draw.
    draw = [_draw_card(f'Burst{i}', 'burst', 6) for i in range(12)]  # 72 draw pts -> row 10
    axis = consistency_axis(draw, None, _HEALTHY_MANA)
    # tutor total 0 -> 3.5; min(10, 3.5) = 3.5.
    assert axis.value == pytest.approx(3.5)
    assert 'tutor' in axis.rationale.lower()


def test_consistency_premium_gate_blocks_9_10_without_two_premium():
    # A tutor column that WOULD read 9-10 by volume but has <2 premium tutors caps at 8.
    # 12 standard tutors (48 pts) -> ladder reads ~8.36; with only 0 premium it caps at 8.
    tutors = [_tutor_card(f'Std{i}', 'standard', 4) for i in range(14)]  # 56 pts -> 9.0
    draw = [_draw_card(f'D{i}', 'burst', 6) for i in range(12)]  # draw row 10 (won't bind)
    axis = consistency_axis(tutors + draw, None, _HEALTHY_MANA)
    # tutor row would be 9.0 (56 pts) but 0 premium -> capped 8.0; min(10, 8) = 8.
    assert axis.value == pytest.approx(8.0)
    assert 'premium gate' in axis.rationale.lower()


def test_consistency_premium_gate_allows_9_with_two_premium():
    # Same 56 tutor pts but TWO premium tutors present -> gate passes, tutor row 9.
    tutors = [_tutor_card('Demonic', 'premium', 6), _tutor_card('Vampiric', 'premium', 6)]
    tutors += [_tutor_card(f'Std{i}', 'standard', 4) for i in range(11)]  # 12+44 = 56 pts
    draw = [_draw_card(f'D{i}', 'burst', 6) for i in range(12)]
    axis = consistency_axis(tutors + draw, None, _HEALTHY_MANA)
    assert axis.value == pytest.approx(9.0)


def test_consistency_graveyard_tutor_zeroed_without_recursion():
    # 3 graveyard-gated tutors + no recursion package -> they score 0 -> tutorless -> 3.5.
    gy = [_tutor_card(f'Entomb{i}', 'graveyard-tutor', 4, graveyard_gated=True) for i in range(3)]
    draw = [_draw_card(f'D{i}', 'burst', 6) for i in range(6)]  # row 8 draw (36 pts)
    axis = consistency_axis(gy + draw, None, _HEALTHY_MANA)
    # gy tutors score 0 -> tutorless column 3.5 binds below the row-8 draw column.
    assert axis.value == pytest.approx(3.5)


def test_consistency_graveyard_tutor_counts_with_recursion_package():
    # Same gy tutors, but 3 recursion cards present -> they score their 4 pts each (12).
    gy = [_tutor_card(f'Entomb{i}', 'graveyard-tutor', 4, graveyard_gated=True) for i in range(3)]
    recursion = [CardTiers(name=f'Regrow{i}', recursion_points=1.0) for i in range(3)]
    draw = [_draw_card(f'D{i}', 'burst', 6) for i in range(6)]
    axis = consistency_axis(gy + recursion + draw, None, _HEALTHY_MANA)
    # tutor 12 pts -> 4.5; draw 36 -> 8; min = 4.5. (Proves gy tutors now counted.)
    assert axis.value == pytest.approx(4.5)


def test_consistency_mana_penalty_lowers_score():
    # A degenerate mana base (20 lands, no rocks, avg cmc 3 -> target 36, deficit 16 -> -2).
    tutors = [_tutor_card(f'Std{i}', 'standard', 4) for i in range(8)]  # 32 pts -> row 7
    draw = [_draw_card(f'D{i}', 'burst', 6) for i in range(6)]  # row 8
    starved = {'land_count': 20, 'rock_count': 0, 'dork_count': 0, 'avg_cmc': 3.0}
    axis = consistency_axis(tutors + draw, None, starved)
    # base min(8, 7) = 7; -2 mana penalty -> 5.0.
    assert axis.value == pytest.approx(5.0)
    assert 'mana base' in axis.rationale.lower()


def test_tribal_bonus_excludes_broad_combat_bucket():
    # FIX D. Rubric ~127: "The cards must be interchangeable — a pile of different win
    # conditions is variety, not redundancy... Interaction suites and tutor/draw
    # packages never qualify." A pile of 10 heterogeneous `combat` cards (different
    # payoffs, not interchangeable copies) must NOT earn the redundancy bonus.
    from pipeline.transforms.crispi import _tribal_bonus

    combat = [CardTiers(name=f'Combat{i}', tags=frozenset({'combat'})) for i in range(10)]
    bonus, notes = _tribal_bonus(combat)
    assert bonus == 0.0
    assert notes == []


def test_tribal_bonus_fires_on_literal_copies():
    # FIX D. Rubric ~127: "copies count (25x Persistent Petitioners is one role filled
    # 25 times)." Literal repeated identical card names ARE interchangeable redundancy.
    petitioners = [CardTiers(name='Persistent Petitioners', tags=frozenset({'mill'})) for _ in range(10)]
    from pipeline.transforms.crispi import _tribal_bonus

    bonus, notes = _tribal_bonus(petitioners)
    assert bonus >= 5.0
    assert notes


def test_tribal_bonus_fires_on_narrow_typal_role():
    # FIX D. A narrow single typal role (a real tribe, not an umbrella plan bucket)
    # of 10 members earns the redundancy bonus.
    typal = [CardTiers(name=f'Goblin{i}', tags=frozenset({'typal'})) for i in range(10)]
    from pipeline.transforms.crispi import _tribal_bonus

    bonus, notes = _tribal_bonus(typal)
    assert bonus >= 5.0
    assert notes


def test_tribal_bonus_fires_on_land_ramp_single_role():
    # FIX D-REWORK. Rubric L127: "redundant copies of one effect are virtual tutors...
    # 10+ cards filling a single critical role add +10." DeckCheck's World Reclaimer
    # analysis credits "8+ land-ramp spells = massive redundancy role." We key on the
    # NARROW `land-ramp` otag slug (interchangeable copies of one effect) -> +10 tutor.
    from pipeline.transforms.crispi import _tribal_bonus

    ramp = [CardTiers(name=f'Ramp{i}', role_slugs=frozenset({'land-ramp'})) for i in range(10)]
    bonus, notes = _tribal_bonus(ramp)
    assert bonus == pytest.approx(10.0)
    assert notes


def test_tribal_bonus_does_not_fire_on_token_pile_variety():
    # FIX D-FINAL. A "token generator" pile is functionally HETEROGENEOUS (mana tokens
    # vs bodies vs an anthem-that-upgrades-tokens vs a copy engine) — the rubric's
    # "variety, not redundancy." ``repeatable-token-generator`` was tried as a
    # redundancy role but REMOVED (it over-credited Wakanda's Consistency); such a pile
    # must earn 0. A real single tribe still qualifies via the ``typal`` bucket.
    from pipeline.transforms.crispi import _tribal_bonus

    tokens = [CardTiers(name=f'Tok{i}', role_slugs=frozenset({'repeatable-token-generator'})) for i in range(7)]
    bonus, _notes = _tribal_bonus(tokens)
    assert bonus == pytest.approx(0.0)


def test_tribal_bonus_does_not_fire_on_sac_pile_variety():
    # FIX D-FINAL. Same as tokens: a sacrifice-outlet pile is variety, not one
    # interchangeable role; ``repeatable-sacrifice-outlet`` was removed from the
    # redundancy slugs -> 0. Only ``land-ramp`` (validated) + a real ``typal`` tribe
    # + literal name-copies qualify.
    from pipeline.transforms.crispi import _tribal_bonus

    sac = [CardTiers(name=f'Sac{i}', role_slugs=frozenset({'repeatable-sacrifice-outlet'})) for i in range(10)]
    bonus, _notes = _tribal_bonus(sac)
    assert bonus == pytest.approx(0.0)


def test_tribal_bonus_excludes_broad_ramp_bucket_variety():
    # FIX D-REWORK. The rubric keys redundancy on interchangeable COPIES of one effect,
    # not the broad plan bucket. A pile of DIFFERENT mana rocks (the broad `ramp`
    # bucket, no shared narrow `land-ramp` slug) is variety, not redundancy -> 0.
    # (This is exactly Wakanda's mana-rock pile, which must NOT earn the World-Reclaimer
    # land-ramp bonus.)
    from pipeline.transforms.crispi import _tribal_bonus

    rocks = [CardTiers(name=f'Rock{i}', tags=frozenset({'ramp'})) for i in range(12)]
    bonus, notes = _tribal_bonus(rocks)
    assert bonus == 0.0
    assert notes == []


def test_tribal_bonus_excludes_interaction_and_draw_and_tutor_piles():
    # FIX D-REWORK. Rubric L127: "Interaction suites and tutor/draw packages never
    # qualify." Piles of removal / counterspells / draw / tutor cards are NOT
    # interchangeable single-effect redundancy, however numerous.
    from pipeline.transforms.crispi import _tribal_bonus

    for barred in ('removal', 'counterspells', 'draw', 'tutor'):
        pile = [CardTiers(name=f'{barred}{i}', tags=frozenset({barred})) for i in range(12)]
        bonus, notes = _tribal_bonus(pile)
        assert bonus == 0.0, f'{barred} pile must not earn redundancy'
        assert notes == []


def test_tribal_bonus_excludes_heterogeneous_wincon_bucket():
    # FIX D-REWORK. Rubric L127: "a pile of DIFFERENT win conditions is variety, not
    # redundancy." The `wincon` (and `combat`) buckets are heterogeneous variety.
    from pipeline.transforms.crispi import _tribal_bonus

    wincons = [CardTiers(name=f'Win{i}', tags=frozenset({'wincon'})) for i in range(12)]
    bonus, notes = _tribal_bonus(wincons)
    assert bonus == 0.0
    assert notes == []


def test_commander_engine_class_activated_battlefield_tutor_is_access():
    # R3-2. Rubric L125: an activated/triggered battlefield-tutor commander (Sisay,
    # Nick Fury: "put a ... card from your library onto the battlefield") is an ACCESS
    # engine, even without a named tutor tier.
    from pipeline.transforms.crispi import _commander_engine_class

    commander = classify_card(
        _card(
            'Fictional Sisay',
            oracle_id='sis-1',
            type_line='Legendary Creature — Legend',
            oracle_text=(
                '{w}{u}{b}{r}{g}: look at the top seven cards of your library. you may put a '
                'creature card from among them onto the battlefield. put the rest on the bottom.'
            ),
        ),
        {},
    )
    assert _commander_engine_class(commander) == 'access'


def test_commander_engine_class_battlefield_tutor_via_otag_is_access():
    # R3-2. The `tutor-to-battlefield` / `impulse-onto-battlefield` / `sneak-from-library`
    # otag on a repeatable/activated ability marks an access engine.
    from pipeline.transforms.crispi import _commander_engine_class

    commander = classify_card(
        _card(
            'Fictional Fury',
            oracle_id='fury-1',
            type_line='Legendary Creature — Hero',
            oracle_text='{5}: put a card from among them onto the battlefield.',
        ),
        {'fury-1': {'activated-ability', 'impulse-onto-battlefield', 'sneak-from-library'}},
    )
    assert _commander_engine_class(commander) == 'access'


def test_activated_battlefield_tutor_commander_lifts_lower_column():
    # R3-2. An access commander lifts whichever Consistency column reads lower (up to
    # +2 rows, <=9). A low-tutor deck with an access commander lifts the tutor column.
    commander = classify_card(
        _card(
            'Fictional Sisay',
            oracle_id='sis-2',
            type_line='Legendary Creature — Legend',
            oracle_text='{1}: put a creature card from your library onto the battlefield.',
        ),
        {},
    )
    draw = [_draw_card(f'D{i}', 'burst', 6) for i in range(8)]  # strong draw column
    tutors = [_tutor_card(f'T{i}', 'standard', 4) for i in range(3)]  # thin tutor column
    with_cmd = consistency_axis(tutors + draw, commander, _HEALTHY_MANA)
    without = consistency_axis(tutors + draw, None, _HEALTHY_MANA)
    assert with_cmd.value > without.value
    assert 'access command-zone engine' in with_cmd.rationale.lower()


def test_consistency_commander_tutor_bonus_lifts_tutor_column():
    # A tutor commander adds +5 to the tutor total.
    commander = CardTiers(name='Cmdr Tutor', tutor=('premium', 6))
    tutors = [_tutor_card(f'Std{i}', 'standard', 4) for i in range(4)]  # 16 pts + 5 = 21 -> ~5.6
    draw = [_draw_card(f'D{i}', 'burst', 6) for i in range(6)]  # row 8
    with_cmd = consistency_axis(tutors + draw, commander, _HEALTHY_MANA)
    without = consistency_axis(tutors + draw, None, _HEALTHY_MANA)
    assert with_cmd.value > without.value


# =========================================================================== #
# Phase 2 — Interaction axis.
# =========================================================================== #


def _interaction_card(name: str, stack: int, *, tags: frozenset[str] = frozenset({'removal'})) -> CardTiers:
    return CardTiers(name=name, is_interaction=True, stack_points=stack, tags=tags)


def test_interaction_count_and_stack_ladders_combine():
    # 14 interaction cards (count row 6.25) each a counterspell+instant (3 stack pts) =
    # 42 stack pts -> stack row interpolates between 38(9) and 45(9.5). Weakest binds:
    # count 6.25 vs stack ~9.29 -> 6.25.
    cards = [_interaction_card(f'Ctr{i}', 3, tags=frozenset({'counterspells'})) for i in range(14)]
    axis = interaction_axis(cards, counterspell_count=14)
    assert axis.value == pytest.approx(6.25)


def test_interaction_stack_ladder_binds_when_count_high():
    # Many cards (count high) but all sorcery-speed (0 stack pts) -> stack row 3.5 binds.
    cards = [_interaction_card(f'Rem{i}', 0) for i in range(26)]  # count row 10
    axis = interaction_axis(cards, counterspell_count=0)
    assert axis.value == pytest.approx(3.5)
    assert 'stack' in axis.rationale.lower()


def test_interaction_symmetric_wipe_cap_at_7():
    # A suite that would read 8.5+ but relies on 3 symmetric board wipes -> capped 7.
    cards = [_interaction_card(f'Ctr{i}', 3, tags=frozenset({'counterspells'})) for i in range(24)]
    axis = interaction_axis(cards, symmetric_wipe_count=3, counterspell_count=24)
    assert axis.value == pytest.approx(7.0)
    assert 'symmetric' in axis.rationale.lower()


def test_interaction_symmetric_wipe_cap_not_triggered_below_three():
    cards = [_interaction_card(f'Ctr{i}', 3, tags=frozenset({'counterspells'})) for i in range(24)]
    axis = interaction_axis(cards, symmetric_wipe_count=2, counterspell_count=24)
    assert axis.value > 7.0


def test_interaction_scope_gate_binds_creature_only_to_7():
    # A dense, stack-heavy suite that can ONLY answer creatures caps at 7 despite high
    # count/stack rows.
    cards = [_interaction_card(f'Kill{i}', 3, tags=frozenset({'removal'})) for i in range(26)]
    axis = interaction_axis(
        cards,
        scope_creature=True,
        scope_artifact=False,
        scope_enchantment=False,
        counterspell_count=0,
    )
    assert axis.value == pytest.approx(7.0)
    assert 'creature-only' in axis.rationale.lower()


def test_interaction_full_scope_permits_high_rows():
    cards = [_interaction_card(f'X{i}', 3, tags=frozenset({'counterspells'})) for i in range(26)]
    axis = interaction_axis(
        cards,
        scope_creature=True,
        scope_artifact=True,
        scope_enchantment=True,
        counterspell_count=26,
    )
    assert axis.value == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# Phase 3 — Speed axis (fundamental-turn table + half-steps + coupling cap).
#
# Selected by `-k speed`. Consistency is passed in so the coupling cap
# (Speed>=9 AND Consistency<=7 -> cap 8) can be applied.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('turn', 'score'),
    [
        (1.0, 10.0),  # T1-2 -> 10
        (2.0, 10.0),  # T1-2 -> 10
        (3.0, 9.0),  # T3 -> 9
        (4.0, 8.0),  # T4 -> 8
        (5.0, 7.0),  # T5 -> 7
        (6.0, 6.0),  # T6 -> 6
        (7.0, 5.0),  # T7 -> 5
        (8.5, 4.0),  # T8-9 band -> 4 (midpoint anchor)
        (10.5, 3.0),  # T10-11 band -> 3
        (12.5, 2.0),  # T12-13 band -> 2
        (14.0, 1.0),  # T14+ -> 1
        (20.0, 1.0),  # clamps at 1
    ],
)
def test_speed_table_maps_each_turn_row(turn, score):
    # Consistency high enough that the coupling cap never engages.
    axis = speed_axis(turn, consistency_value=10.0)
    assert axis.value == pytest.approx(score)


def test_speed_half_step_between_rows():
    # Rubric: turn 3.5 straddles T3(9) and T4(8) -> 8.5;
    # turn 4.5 straddles T4(8) and T5(7) -> 7.5.
    assert speed_axis(3.5, consistency_value=10.0).value == pytest.approx(8.5)
    assert speed_axis(4.5, consistency_value=10.0).value == pytest.approx(7.5)


def test_speed_consistency_coupling_caps_at_8():
    # Speed 9 (turn 3) with Consistency 6 (<=7) -> capped at 8.
    axis = speed_axis(3.0, consistency_value=6.0)
    assert axis.value == pytest.approx(8.0)
    assert 'cap' in axis.rationale.lower()


def test_speed_coupling_not_applied_below_9():
    # Speed 8 (turn 4) with low Consistency -> no cap (cap only fires for Speed>=9).
    axis = speed_axis(4.0, consistency_value=3.0)
    assert axis.value == pytest.approx(8.0)


def test_speed_coupling_not_applied_with_high_consistency():
    # Speed 10 (turn 2) with Consistency 8 (>7) -> no cap.
    axis = speed_axis(2.0, consistency_value=8.0)
    assert axis.value == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# Phase 3 — Resilience axis. Selected by `-k resilience`.
#
# Synthetic `CardTiers` builders + fake `Combo` match lists exercise each path
# (combo layering, combat rows, answer-density, stax/Voltron caps) and both
# penalties (engine exposure, commander dependence).
# --------------------------------------------------------------------------- #


def _combo(variant_id: str, cards: tuple[str, ...]) -> Combo:
    return Combo(variant_id=variant_id, card_names=cards, card_oracle_ids=(), result='Infinite mana; win')


def _threat(name: str, *, weight: float = 1.0) -> CardTiers:
    return CardTiers(name=name, is_threat=True, threat_weight=weight)


def _recur(name: str, pts: float) -> CardTiers:
    return CardTiers(name=name, recursion_points=pts)


def _protect(name: str) -> CardTiers:
    return CardTiers(name=name, is_protection=True, protection_points=1.0)


def test_resilience_two_distinct_combos_high_tutor_reads_9():
    combos = [_combo('a', ('Thassa', 'Kiki')), _combo('b', ('Dramatic', 'Scepter'))]
    axis = resilience_axis(
        [],
        tutor_points=30.0,  # >=24 -> full credit
        combos=combos,
        commander_dependence='low',
    )
    assert axis.value == pytest.approx(9.0)


def test_resilience_same_combos_low_tutor_assembly_discounted():
    # Same 2 distinct combos but tutor_points < 12 -> x0.5 -> 0.5 lines read 3.5.
    combos = [_combo('a', ('Thassa', 'Kiki')), _combo('b', ('Dramatic', 'Scepter'))]
    axis = resilience_axis(
        [],
        tutor_points=4.0,
        combos=combos,
        commander_dependence='low',
    )
    assert axis.value < 9.0
    assert axis.value == pytest.approx(3.5)


def test_resilience_lone_combo_with_strong_combat_backup_reads_7():
    # R3-3 (World Reclaimer). Rubric row 7 = "1 primary combo + 1 backup". A single
    # assembled combo line PLUS an independent combat path reading >=5 is a real
    # backup -> release the lone-combo cap from 6 to 7.
    combos = [_combo('a', ('Thassa', 'Kiki'))]
    # A combat board that reads exactly row 5 (8+ threats, 2+ protection, 4+ recursion).
    threats = [_threat(f'T{i}') for i in range(8)]
    protection = [_protect(f'P{i}') for i in range(2)]
    engines = [_recur(f'E{i}', 2.0) for i in range(2)]  # 4 recursion pts
    axis = resilience_axis(
        threats + protection + engines,
        tutor_points=30.0,  # full assembly credit so combo line is a full line
        combos=combos,
        commander_dependence='low',
    )
    assert axis.value == pytest.approx(7.0)


def test_resilience_lone_combo_weak_combat_stays_6():
    # R3-3 guard. A single combo line with a WEAK combat path (below row 5) stays
    # capped at 6 (rubric: "a lone combo line ... caps at 6").
    combos = [_combo('a', ('Thassa', 'Kiki'))]
    threats = [_threat(f'T{i}') for i in range(3)]  # row 3.5 combat only
    axis = resilience_axis(
        threats,
        tutor_points=30.0,
        combos=combos,
        commander_dependence='low',
    )
    assert axis.value == pytest.approx(6.0)


def test_resilience_no_combo_strong_combat_not_lifted_to_7_via_backup():
    # R3-3 guard. A comboless deck must NOT reach 7 through this path. A strong combat
    # board with NO combo line reads its own combat row (<=6 without full structure),
    # never the row-7 "1 primary + 1 backup".
    threats = [_threat(f'T{i}') for i in range(8)]
    protection = [_protect(f'P{i}') for i in range(2)]
    engines = [_recur(f'E{i}', 2.0) for i in range(2)]  # row 5 combat
    axis = resilience_axis(
        threats + protection + engines,
        tutor_points=30.0,
        combos=[],  # no combo line
        commander_dependence='low',
    )
    assert axis.value < 7.0


def test_resilience_combat_row_9_structural_requirement():
    # Row 9: 12+ effective threats, 8+ protection, 12+ recursion pts w/ 4+ rebuild
    # engines, 3+ board-level protection.
    threats = [_threat(f'T{i}') for i in range(12)]
    protection = [_protect(f'P{i}') for i in range(8)]
    board = [
        CardTiers(name=f'Board{i}', is_protection=True, protection_points=1.0, is_board_protection=True)
        for i in range(3)
    ]
    engines = [_recur(f'E{i}', 2.0) for i in range(6)]  # 12 pts, 6 engines (>=4)
    axis = resilience_axis(
        threats + protection + board + engines,
        tutor_points=0.0,
        combos=[],
        commander_dependence='low',
    )
    assert axis.value >= 9.0


def test_resilience_combat_row_6_structural_requirement():
    # Row 6: 10+ threats, 4+ protection, 8+ recursion pts w/ 2+ rebuild engines,
    # 1+ board-level protection.
    threats = [_threat(f'T{i}') for i in range(10)]
    protection = [_protect(f'P{i}') for i in range(3)]
    board = [CardTiers(name='Board', is_protection=True, protection_points=1.0, is_board_protection=True)]
    engines = [_recur(f'E{i}', 2.0) for i in range(4)]  # 8 pts, 4 engines
    axis = resilience_axis(
        threats + protection + board + engines,
        tutor_points=0.0,
        combos=[],
        commander_dependence='low',
    )
    assert axis.value == pytest.approx(6.0)


def test_resilience_answer_density_caps_at_7():
    # 16+ interaction, 8+ counters, 40+ draw -> row 7 (cap 7).
    interaction = [CardTiers(name=f'I{i}', is_interaction=True, stack_points=(2 if i < 8 else 0)) for i in range(16)]
    axis = resilience_axis(
        interaction,
        tutor_points=0.0,
        combos=[],
        commander_dependence='low',
        draw_points=40.0,
        counterspell_count=8,
    )
    assert axis.value == pytest.approx(7.0)


def test_resilience_stax_cap_at_6():
    # A strong combat board that would read >6, capped to 6 by the stax archetype hint.
    threats = [_threat(f'T{i}') for i in range(12)]
    protection = [_protect(f'P{i}') for i in range(8)]
    board = [
        CardTiers(name=f'B{i}', is_protection=True, protection_points=1.0, is_board_protection=True) for i in range(3)
    ]
    engines = [_recur(f'E{i}', 2.0) for i in range(6)]
    axis = resilience_axis(
        threats + protection + board + engines,
        tutor_points=0.0,
        combos=[],
        commander_dependence='low',
        archetype_caps={'stax'},
    )
    assert axis.value == pytest.approx(6.0)


def test_resilience_voltron_cap_at_7():
    threats = [_threat(f'T{i}') for i in range(12)]
    protection = [_protect(f'P{i}') for i in range(8)]
    board = [
        CardTiers(name=f'B{i}', is_protection=True, protection_points=1.0, is_board_protection=True) for i in range(3)
    ]
    engines = [_recur(f'E{i}', 2.0) for i in range(6)]
    axis = resilience_axis(
        threats + protection + board + engines,
        tutor_points=0.0,
        combos=[],
        commander_dependence='low',
        archetype_caps={'voltron'},
    )
    assert axis.value == pytest.approx(7.0)


def test_resilience_commander_dependence_penalty_applied_last():
    combos = [_combo('a', ('Thassa', 'Kiki')), _combo('b', ('Dramatic', 'Scepter'))]
    base = resilience_axis([], tutor_points=30.0, combos=combos, commander_dependence='low')
    med = resilience_axis([], tutor_points=30.0, combos=combos, commander_dependence='med')
    high = resilience_axis([], tutor_points=30.0, combos=combos, commander_dependence='high')
    assert base.value == pytest.approx(9.0)
    assert med.value == pytest.approx(8.0)  # -1
    assert high.value == pytest.approx(7.0)  # -2


def test_resilience_engine_exposure_penalty():
    combos = [_combo('a', ('Thassa', 'Kiki')), _combo('b', ('Dramatic', 'Scepter'))]
    base = resilience_axis([], tutor_points=30.0, combos=combos, commander_dependence='low')
    exposed1 = resilience_axis([], tutor_points=30.0, combos=combos, commander_dependence='low', engine_exposure=-1)
    exposed2 = resilience_axis([], tutor_points=30.0, combos=combos, commander_dependence='low', engine_exposure=-2)
    assert base.value == pytest.approx(9.0)
    assert exposed1.value == pytest.approx(8.0)
    assert exposed2.value == pytest.approx(7.0)


def test_resilience_penalties_stack_and_clamp():
    combos = [_combo('a', ('Thassa', 'Kiki')), _combo('b', ('Dramatic', 'Scepter'))]
    axis = resilience_axis(
        [],
        tutor_points=30.0,
        combos=combos,
        commander_dependence='high',  # -2
        engine_exposure=-2,  # -2
    )
    assert axis.value == pytest.approx(5.0)  # 9 - 4


def test_resilience_no_threat_base_is_glass():
    axis = resilience_axis([], tutor_points=0.0, combos=[], commander_dependence='low')
    assert axis.value <= 2.5


# =========================================================================== #
# Phase 4 — top-level scorer `crispi_score` + `crispi_from_deck` bridge.
# Selected by `-k "score or determinism or from_deck"`.
# =========================================================================== #


def _score_cards() -> tuple[list[dict], dict[str, set[str]]]:
    """A small synthetic deck: a commander, a couple threats, some draw/removal.

    Hand-built card dicts + an oracle_id -> slug-closure otag map so the scorer
    exercises every axis without a Scryfall fetch or an otag lake.
    """
    cards = [
        _card(
            'Krenko, Mob Boss',
            oracle_id='cmd',
            type_line='Legendary Creature — Goblin',
            oracle_text='at the beginning of combat, create tokens',
            cmc=4.0,
            power='3',
        ),
        _card('Goblin Bushwhacker', oracle_id='t1', type_line='Creature — Goblin', cmc=1.0, power='2'),
        _card('Shivan Dragon', oracle_id='t2', type_line='Creature — Dragon', cmc=6.0, keywords=['Flying'], power='5'),
        _card(
            'Faithless Looting',
            oracle_id='d1',
            type_line='Sorcery',
            oracle_text='draw two cards, then discard two cards.',
            cmc=1.0,
        ),
        _card(
            'Lightning Bolt',
            oracle_id='r1',
            type_line='Instant',
            oracle_text='deals 3 damage to any target',
            cmc=1.0,
        ),
        _card('Mountain', type_line='Basic Land — Mountain', cmc=0.0),
    ]
    card_otag = {
        'd1': {'card-advantage'},
        'r1': {'removal'},
    }
    return cards, card_otag


def test_score_produces_valid_result_with_snapped_pi():
    cards, card_otag = _score_cards()
    result = crispi_score(
        cards,
        card_otag,
        fundamental_turn=6.0,
        commander_dependence='med',
    )
    from pipeline.contracts import CrispiResult

    assert isinstance(result, CrispiResult)
    # PI is the snapped mean of the four axis values.
    axes = [result.consistency.value, result.interaction.value, result.speed.value, result.resilience.value]
    assert result.performance_index == pytest.approx(round(snap_quarter(sum(axes) / 4.0), 2))
    # Bracket is Phase 6 — None here. Inputs echo the two judgement inputs.
    assert result.bracket is None
    assert result.inputs == {'fundamental_turn': 6.0, 'commander_dependence': 'med'}
    # Every axis in-range.
    for v in axes:
        assert 1.0 <= v <= 10.0


def test_score_is_deterministic_same_inputs_identical_dump():
    cards, card_otag = _score_cards()
    a = crispi_score(cards, card_otag, fundamental_turn=7.5, commander_dependence='high', computed_at='X')
    b = crispi_score(cards, card_otag, fundamental_turn=7.5, commander_dependence='high', computed_at='X')
    assert a.model_dump() == b.model_dump()


def test_score_is_pure_no_clock_default_computed_at_empty():
    cards, card_otag = _score_cards()
    result = crispi_score(cards, card_otag, fundamental_turn=8.0, commander_dependence='low')
    # No clock inside: default sentinel stamp.
    assert result.computed_at == ''
    # Two calls (excluding computed_at) are byte-identical.
    a = crispi_score(cards, card_otag, fundamental_turn=8.0, commander_dependence='low', computed_at='t1')
    b = crispi_score(cards, card_otag, fundamental_turn=8.0, commander_dependence='low', computed_at='t2')
    da, db = a.model_dump(), b.model_dump()
    da.pop('computed_at')
    db.pop('computed_at')
    assert da == db


def test_score_degrades_without_otag_layer():
    # Empty otag map: axes still compute from structured facts, never crash.
    cards, _ = _score_cards()
    result = crispi_score(cards, {}, fundamental_turn=6.0, commander_dependence='low')
    assert 1.0 <= result.performance_index <= 10.0


def test_score_shares_tutor_and_draw_totals_between_consistency_and_resilience():
    # The Consistency draw/tutor totals feeding Resilience come from the same
    # `consistency_totals` the axis uses — a single source of truth.
    cards, card_otag = _score_cards()
    nonland = [c for c in cards if 'Land' not in (c.get('type_line') or '')]
    classified = [classify_card(c, card_otag) for c in nonland]
    commander = classify_card(cards[0], card_otag)
    totals = consistency_totals(classified, commander)
    # The shared totals are floats the resilience axis consumes as tutor/draw points.
    assert isinstance(totals.tutor_total, float)
    assert isinstance(totals.draw_total, float)


def test_from_deck_builds_cards_with_power_toughness_and_commander_flag():
    # HARD requirement: the crispi bridge MUST include power/toughness/is_commander
    # in the built card dicts (the classifier's threat detection reads card['power']).
    import sys as _sys
    from pathlib import Path as _Path

    scripts_dir = _Path(__file__).resolve().parents[2] / 'scripts'
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    from deck_factsheet import _crispi_card_to_fields  # type: ignore[import-not-found]

    from pipeline.contracts import DeckCard

    commander = DeckCard(
        name='Krenko, Mob Boss',
        role='commander',
        power='3',
        toughness='3',
        type_line='Legendary Creature — Goblin',
        mana_value=4.0,
    )
    fields = _crispi_card_to_fields(commander)
    assert fields['power'] == '3'
    assert fields['toughness'] == '3'
    assert fields['is_commander'] is True

    maindeck = DeckCard(name='Lightning Bolt', type_line='Instant', mana_value=1.0)
    mfields = _crispi_card_to_fields(maindeck)
    assert mfields['is_commander'] is False
    # power/toughness keys are present even when null (non-creature).
    assert 'power' in mfields
    assert 'toughness' in mfields


def _load_deck_factsheet():
    import sys as _sys
    from pathlib import Path as _Path

    scripts_dir = _Path(__file__).resolve().parents[2] / 'scripts'
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    import deck_factsheet  # type: ignore[import-not-found]

    return deck_factsheet


def test_crispi_card_to_fields_carries_quantity():
    # FIX C. `_crispi_card_to_fields` must carry `quantity` so downstream mana counting
    # can count card COPIES (basics carry quantity 8-12), not deck ROWS.
    from pipeline.contracts import DeckCard

    df = _load_deck_factsheet()
    basic = DeckCard(name='Forest', type_line='Basic Land — Forest', mana_value=0.0, quantity=10)
    fields = df._crispi_card_to_fields(basic)
    assert fields['quantity'] == 10


def test_crispi_mana_facts_counts_basics_by_quantity():
    # FIX C. Rubric ~129 / DeckCheck: "the 41-land/10+ ramp base avoids any
    # mana-reliability penalty." A 40-land base built from a handful of ROWS whose
    # quantities sum to 40 must read ~40 effective sources (no phantom -2).
    from pipeline.contracts import DeckCard

    df = _load_deck_factsheet()
    # Two basic-land rows, quantity 20 each -> 40 lands.
    cards = [
        df._crispi_card_to_fields(
            DeckCard(name='Forest', type_line='Basic Land — Forest', mana_value=0.0, produced_mana=['G'], quantity=20)
        ),
        df._crispi_card_to_fields(
            DeckCard(
                name='Mountain', type_line='Basic Land — Mountain', mana_value=0.0, produced_mana=['R'], quantity=20
            )
        ),
    ]
    facts = df._crispi_mana_facts(cards, None)
    assert facts['land_count'] == 40

    from pipeline.transforms.crispi import _mana_penalty

    penalty, _notes = _mana_penalty(facts)
    assert penalty == 0.0  # a 40-land base trips no mana-reliability penalty


def test_crispi_mana_facts_counts_lands_as_color_producers():
    # FIX C. Fixing/basic LANDS are color sources; the per-color producer count must
    # include LAND produced_mana, not just nonland ramp. A deck whose colored pips are
    # carried by land sources must NOT read <10 producers per color and trip -1.
    from pipeline.contracts import DeckCard

    df = _load_deck_factsheet()
    cards = [
        # 12 green sources: a nonland spell providing the green pip pressure...
        df._crispi_card_to_fields(
            DeckCard(name='Green Spell', type_line='Sorcery', mana_value=2.0, mana_cost='{1}{G}', quantity=1)
        ),
        # ...backed by 12 Forests (a single row, quantity 12) that PRODUCE green.
        df._crispi_card_to_fields(
            DeckCard(name='Forest', type_line='Basic Land — Forest', mana_value=0.0, produced_mana=['G'], quantity=12)
        ),
    ]
    facts = df._crispi_mana_facts(cards, None)
    # Find the green pip-pressure entry: (color, share, producers).
    green = next(e for e in facts['pip_pressure'] if e[0] == 'G')
    assert green[2] >= 12  # lands count toward the producer total


# --------------------------------------------------------------------------- #
# FIX E — engine-exposure exempts creature/threat concentration.
# --------------------------------------------------------------------------- #


def test_engine_exposure_exempts_burn_creature_concentration():
    # FIX E. Rubric ~164: "Worst class only; creature concentration is exempt... cards
    # that fight as creature threats ... never count toward the artifact or enchantment
    # share ... the penalty measures engine dependence, not what your threats are made
    # of." A burn / creature-threat concentrated deck takes NO engine-exposure penalty.
    from pipeline.transforms.crispi import _engine_exposure

    cards = [_card(f'Burn{i}', oracle_id=f'b{i}', type_line='Instant', oracle_text='deal 3 damage') for i in range(20)]
    cards += [_card(f'Beater{i}', oracle_id=f'c{i}', type_line='Creature — Goblin', power='3') for i in range(20)]
    exposure = _engine_exposure(cards, {}, nonland_count=40, counterspell_count=0)
    assert exposure is None  # noncreature hoser share is ~0: these are threats


def test_engine_exposure_still_hits_artifact_engine_pile():
    # FIX E (converse). A genuinely hoser-answerable NONCREATURE artifact-engine pile
    # still takes the penalty (folds to a single Vandalblast).
    from pipeline.transforms.crispi import _engine_exposure

    cards = [
        _card(f'Artifact{i}', oracle_id=f'a{i}', type_line='Artifact', oracle_text='tap: add mana') for i in range(20)
    ]
    cards += [_card(f'Other{i}', oracle_id=f'o{i}', type_line='Sorcery', oracle_text='draw a card') for i in range(20)]
    exposure = _engine_exposure(cards, {}, nonland_count=40, counterspell_count=0)
    assert exposure is not None
    assert exposure < 0
