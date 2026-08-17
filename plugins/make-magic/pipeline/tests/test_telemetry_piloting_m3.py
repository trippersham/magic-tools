"""M3 regression: own-turn removal affordability off the POST-untap land count.

``GameEventTurnBegan`` fires PRE-untap, so the ``turnstart`` HANDLOG snapshot's
``UR_untapped_lands`` is the leftover-tapped count from prior turns — it read 0
for whole games and hid EVERY own-turn removal opportunity (the candidate looked
perennially manaless on its own turn). The harness now also emits
``UR_total_lands`` (untap-invariant: every land untaps at the untap step), and an
``event=turnend`` snapshot (tapped = total - untapped = mana spent that turn).
:func:`extract_piloting` uses ``total_lands`` (+ a land drop) as the candidate's
own-turn available mana, while opponent-turn instant-speed interaction still pays
only from currently-untapped mana.

These are pure parses of synthetic HANDLOG text (no JVM).
"""

from __future__ import annotations

import re

from pipeline.sim.telemetry import extract_piloting

_REMOVAL = {'Doom Blade'}
_INTERACTION = set(_REMOVAL)
_COSTS = {'Doom Blade': 2}
_LANDS = {'Swamp'}

# Candidate Ai(1) on its OWN turn: holds an affordable removal spell (Doom Blade,
# mv 2) with a legal target (opp_creatures=1), THREE lands in play — but they read
# as pre-untap tapped, so UR_untapped_lands=0. UR_total_lands=3 is the real
# post-untap mana. A turnend snapshot follows (tapped = 3 - 0 = all three spent).
_OWN_TURN_HELD = (
    'HANDLOG turn=1 event=turnstart active=Ai(1)-Test '
    'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=0 UR_total_lands=3 '
    'opp_creatures=1 UR_life=20 opp_life=20\n'
    'HANDLOG turn=1 event=turnend active=Ai(1)-Test '
    'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=0 UR_total_lands=3 '
    'opp_creatures=1 UR_life=20 opp_life=20\n'
    'HANDLOG turn=1 event=gameend '
    'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=0 UR_total_lands=3 '
    'opp_creatures=1 UR_life=20 opp_life=20\n'
)


def _profile(log: str):
    return extract_piloting(
        log,
        candidate_slot='a',
        counters=set(),
        removal=_REMOVAL,
        interaction=_INTERACTION,
        costs=_COSTS,
        lands=_LANDS,
    )


def test_own_turn_opportunity_seen_via_total_lands() -> None:
    # The fix: total_lands=3 (>= mv 2) reveals the opportunity the pre-untap
    # untapped=0 count hid. It holds but never casts -> 1 opp, 0 casts.
    p = _profile(_OWN_TURN_HELD)
    assert p.removal_opps == 1
    assert p.removal_casts == 0


def test_legacy_log_without_total_lands_falls_back_and_undercounts() -> None:
    # Strip UR_total_lands -> a pre-M3 harness log. The parser falls back to the
    # pre-untap untapped=0 estimate (+1 land drop = 1 < mv 2), so the opportunity
    # is NOT seen. This documents both the original bug and the compat fallback.
    legacy = re.sub(r' UR_total_lands=\d+', '', _OWN_TURN_HELD)
    p = _profile(legacy)
    assert p.removal_opps == 0


def test_own_turn_conversion_counts_the_cast() -> None:
    # Same setup but the candidate actually casts Doom Blade this turn -> 1/1.
    log = _OWN_TURN_HELD.replace(
        'HANDLOG turn=1 event=turnend',
        'HANDLOG turn=1 event=cast kind=spell castBy=Ai(1)-Test source=Doom Blade '
        'UR_hand=[Swamp] UR_untapped_lands=1 UR_total_lands=3 '
        'opp_creatures=1 UR_life=20 opp_life=20\n'
        'HANDLOG turn=1 event=turnend',
    )
    p = _profile(log)
    assert p.removal_opps == 1
    assert p.removal_casts == 1


def test_opponent_turn_uses_untapped_not_total_lands() -> None:
    # On the OPPONENT's turn only instant-speed interaction is possible, paid from
    # currently-untapped mana. total_lands=3 must NOT be used here: untapped=0 means
    # tapped out -> cannot respond -> NO opportunity (even though total_lands >= mv).
    opp_turn = (
        'HANDLOG turn=1 event=turnstart active=Ai(2)-Foe '
        'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=0 UR_total_lands=3 '
        'opp_creatures=1 UR_life=20 opp_life=20\n'
        'HANDLOG turn=1 event=gameend '
        'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=0 UR_total_lands=3 '
        'opp_creatures=1 UR_life=20 opp_life=20\n'
    )
    assert _profile(opp_turn).removal_opps == 0


def test_opponent_turn_instant_speed_seen_when_mana_up() -> None:
    # Same opp turn but the candidate has mana UP (untapped=2 >= mv 2) -> a real
    # instant-speed opportunity, independent of total_lands.
    opp_turn = (
        'HANDLOG turn=1 event=turnstart active=Ai(2)-Foe '
        'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=2 UR_total_lands=3 '
        'opp_creatures=1 UR_life=20 opp_life=20\n'
        'HANDLOG turn=1 event=gameend '
        'UR_hand=[Doom Blade;Swamp] UR_untapped_lands=2 UR_total_lands=3 '
        'opp_creatures=1 UR_life=20 opp_life=20\n'
    )
    assert _profile(opp_turn).removal_opps == 1
