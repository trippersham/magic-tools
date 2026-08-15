"""Tests for the HANDLOG piloting extractor (:func:`extract_piloting`).

The metric conditions on *opportunity* — a counter/removal only "should" fire on
a turn the candidate holds an affordable answer with a legal target — so a low
conversion rate implicates the PILOT (AI), not the DECK. The parser is a PURE
port of the lab reference ``variant_gauntlet/analyze_forge_variant.py``.

The exact assertion values are locked against the real 3-game fixture
``tests/fixtures/forge/handlog_azorius.log`` (UR Izzet, ``Ai(1)``) using the UR
classification below. They were derived by re-running the reference
``analyze_game`` logic over the same log:

  * counter_opps = 7, counter_casts = 0
      All 7 opportunities are in game 3, where the candidate holds an affordable
      ``Reasonable Doubt`` (mv 2) across 7 opponent spell-casts and never counters
      (it is the card left STRANDED at gameend). Games 1-2 hold no affordable
      counter when the opponent casts, so 0 opportunities there.
  * removal_opps = 2, removal_casts = 2
      One turn in game 2 and one in game 3 where the candidate holds affordable
      burn AND ``opp_creatures >= 1``; it fires removal on both (2/2).
  * stranded total = 1 (``Reasonable Doubt`` in game 3), so
      interaction_stranded_per_game = 1/3.

No JVM is spawned — this is a pure parse of already-captured HANDLOG text.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.sim.telemetry import PilotingProfile, extract_piloting

FIXTURES = Path(__file__).parent / 'fixtures' / 'forge'

# --------------------------------------------------------------------------- #
# UR Izzet classification (the deck the fixture pilots as Ai(1)).
# Sudden Setback is a TUCK, not a counter -> excluded from `counters`.
# Reasonable Doubt is the sole true counter. Removal = the castable-instant burn
# suite. `interaction` = counters | removal (what "stranded" counts).
# --------------------------------------------------------------------------- #
_COUNTERS = {'Reasonable Doubt'}
_REMOVAL = {'Burst Lightning', 'Sear', 'Bombard', 'Unsubtle Mockery', 'Synchronized Spellcraft'}
_INTERACTION = _COUNTERS | _REMOVAL
_COSTS = {
    'Reasonable Doubt': 2,
    'Burst Lightning': 1,
    'Sear': 2,
    'Bombard': 3,
    'Unsubtle Mockery': 3,
    'Synchronized Spellcraft': 5,
}
_LANDS = {'Island', 'Mountain', 'Spectacle Summit'}


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(errors='replace')


def _profile() -> PilotingProfile:
    return extract_piloting(
        _read('handlog_azorius.log'),
        candidate_slot='a',
        counters=_COUNTERS,
        removal=_REMOVAL,
        interaction=_INTERACTION,
        costs=_COSTS,
        lands=_LANDS,
    )


# --------------------------------------------------------------------------- #
# Locked exact counts (hand-derived from the reference logic over the fixture).
# --------------------------------------------------------------------------- #


def test_games_split_on_gameend() -> None:
    assert _profile().games == 3


def test_counter_opportunities_and_casts() -> None:
    p = _profile()
    assert p.counter_opps == 7
    assert p.counter_casts == 0


def test_removal_opportunities_and_casts() -> None:
    p = _profile()
    assert p.removal_opps == 2
    assert p.removal_casts == 2


def test_stranded_interaction_per_game() -> None:
    # Exactly one interaction card (Reasonable Doubt) stranded, across 3 games.
    assert _profile().interaction_stranded_per_game == 1 / 3


# --------------------------------------------------------------------------- #
# Rate + Wilson CI derivation from the locked counts.
# --------------------------------------------------------------------------- #


def test_counter_fire_is_zero_with_ci() -> None:
    p = _profile()
    assert p.counter_fire == 0.0  # 0/7 conversions.
    assert p.counter_ci is not None
    lo, hi = p.counter_ci
    assert lo < 1e-9  # 0/7 lower bound is ~0 (float epsilon from the Wilson center).
    assert 0.0 < hi < 0.5  # small n, one-sided upper bound.


def test_removal_fire_is_perfect_with_ci() -> None:
    p = _profile()
    assert p.removal_fire == 1.0  # 2/2 conversions.
    assert p.removal_ci is not None
    lo, hi = p.removal_ci
    assert hi == 1.0
    assert 0.0 < lo < 1.0


# --------------------------------------------------------------------------- #
# The signal the feature exists to measure.
# --------------------------------------------------------------------------- #


def test_counter_holding_fixture_has_opportunities() -> None:
    assert _profile().counter_opps > 0


# --------------------------------------------------------------------------- #
# Hard contract: never raises; zeroed profile on empty / garbage input.
# --------------------------------------------------------------------------- #


def _empty_profile(log: str) -> PilotingProfile:
    return extract_piloting(
        log,
        counters=_COUNTERS,
        removal=_REMOVAL,
        interaction=_INTERACTION,
        costs=_COSTS,
        lands=_LANDS,
    )


def test_empty_log_is_zeroed_and_does_not_raise() -> None:
    p = _empty_profile('')
    assert p == PilotingProfile(
        counter_opps=0,
        counter_casts=0,
        counter_fire=None,
        counter_ci=None,
        removal_opps=0,
        removal_casts=0,
        removal_fire=None,
        removal_ci=None,
        interaction_stranded_per_game=0.0,
        games=0,
    )


def test_garbage_and_truncated_logs_do_not_raise() -> None:
    garbage = 'not a handlog\nHANDLOG turn=oops event=\nrandom bytes \x00\x01\n'
    truncated = 'HANDLOG turn=1 event=turnstart active=Ai(1)-UR UR_hand=[Island'  # cut mid-line
    for log in (garbage, truncated):
        p = _empty_profile(log)
        assert p.games == 0
        assert p.counter_opps == 0
        assert p.removal_opps == 0
        assert p.interaction_stranded_per_game == 0.0
