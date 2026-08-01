"""Tests for the Tier-1 telemetry extractor (:mod:`pipeline.sim.telemetry`).

Every assertion is HAND-VERIFIED against the real captured verbose log
``tests/fixtures/forge/sim2.log`` (ONE full game of Constructed) — the only
fixture with per-turn ``Turn:``/``Land:``/``Life:``/``Damage:`` detail. The
compact ``empirical/*`` and ``sim30`` logs carry only ``Game Outcome:`` /
``Game Result:`` summary lines, so they exercise graceful degradation (winner
still derivable, per-turn curves empty).

No JVM is spawned — telemetry is a PURE parse of already-captured text.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.sim.telemetry import (
    GameFeatures,
    extract_game_features,
    extract_match_features,
    split_games,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'forge'


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


# --------------------------------------------------------------------------- #
# extract_game_features — hand-verified against sim2.log (full verbose game)
# --------------------------------------------------------------------------- #
#
# Ai(1)-RedTest = deck_a, Ai(2)-WhiteTest = deck_b. Hand trace:
#   * Mulligans (L35-38): both "mulliganed down to 6" -> 7-6 = 1 each.
#   * Turns (Turn: Turn N): 1(b) 2(a) 3(b) 4(a) 5(b) 6(a) 7(b) 8(a) 9(b); max 9.
#   * Lands per turn boundary (cumulative in play):
#       T1 b Plains          -> a=0 b=1
#       T2 a Mountain        -> a=1 b=1
#       T3 b Plains          -> a=1 b=2
#       T4 a Mountain        -> a=2 b=2
#       T5 b Plains          -> a=2 b=3
#       T6 a Mountain        -> a=3 b=3
#       T7 (no land)         -> a=3 b=3
#       T8 a Mountain        -> a=4 b=3
#       T9 (no land, ends)   -> a=4 b=3
#     lands_by_turn_a = [0,1,1,2,2,3,3,4,4]
#     lands_by_turn_b = [1,1,2,2,3,3,3,3,3]
#   * Life: only RedTest(a) takes damage: 20>18(T3) 18>12(T5) 12>4(T7) 4>0(T9).
#     WhiteTest(b) never appears in a Life line -> stays at 20 (Constructed
#     starting life, from the "of Constructed" header). Winner b final life = 20.
#   * Winner: "Game Result: ... Ai(2)-WhiteTest has won!" -> 'b'.
#   * kill_turn: loser(a) life hit 0 during Turn 9 -> 9.
#   * wincon: killing blow (L238/239) = "deals 2 combat damage to Ai(1)-RedTest"
#     -> 'combat'.
#   * game_length_ms: "ended in 1135 ms" -> 1135.


def test_sim2_winner_is_b() -> None:
    f = extract_game_features(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert f.winner == 'b'


def test_winner_name_with_spaces_and_parens() -> None:
    """Winner extraction handles a spaced/paren deck name (slot-keyed, not ``\\S+``)."""
    log = 'Game Result: Game 1 ended in 1780 ms. Ai(1)-UR Izzet (Chaos Sealed) has won!\n'
    f = extract_game_features(log, deck_a='UR Izzet (Chaos Sealed)', deck_b='Mono Red')
    assert f.winner == 'a'


def test_sim2_kill_turn_is_9() -> None:
    """The tracked ``Turn: Turn N`` counter, NOT Forge's ``Game Outcome: Turn 5``
    (which counts each player's turn separately)."""
    f = extract_game_features(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert f.kill_turn == 9


def test_sim2_win_margin_life_is_20() -> None:
    f = extract_game_features(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert f.win_margin_life == 20


def test_sim2_wincon_is_combat() -> None:
    f = extract_game_features(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert f.wincon == 'combat'


def test_sim2_mulligans() -> None:
    f = extract_game_features(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert f.mulligans_a == 1
    assert f.mulligans_b == 1


def test_sim2_game_length_ms() -> None:
    f = extract_game_features(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert f.game_length_ms == 1135


def test_sim2_land_curves() -> None:
    f = extract_game_features(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert f.lands_by_turn_a == [0, 1, 1, 2, 2, 3, 3, 4, 4]
    assert f.lands_by_turn_b == [1, 1, 2, 2, 3, 3, 3, 3, 3]


def test_sim2_returns_gamefeatures() -> None:
    f = extract_game_features(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert isinstance(f, GameFeatures)


# --------------------------------------------------------------------------- #
# wincon classification — mill (empty-library) from a real Game Outcome line
# --------------------------------------------------------------------------- #


def test_mill_wincon_from_empty_library_outcome() -> None:
    """A game whose loser 'has lost trying to draw cards from empty library'
    classifies as 'mill' even without per-turn detail."""
    game = (
        'Game Outcome: Turn 34\n'
        'Game Outcome: Ai(1)-RedTest has won because all opponents have lost\n'
        'Game Outcome: Ai(2)-WhiteTest has lost trying to draw cards from empty library\n'
        'Match Result: Ai(1)-RedTest: 3 Ai(2)-WhiteTest: 23 \n'
        '\n'
        'Game Result: Game 26 ended in 5186 ms. Ai(1)-RedTest has won!\n'
    )
    f = extract_game_features(game, deck_a='RedTest', deck_b='WhiteTest')
    assert f.winner == 'a'
    assert f.wincon == 'mill'
    assert f.game_length_ms == 5186


# --------------------------------------------------------------------------- #
# split_games — segment a multi-game verbose log
# --------------------------------------------------------------------------- #


def test_split_games_counts_ten_games_in_commander_run() -> None:
    """commander_run.log is a real 10-game match; one segment per 'Game Result'."""
    segments = split_games(_read('empirical/commander_run.log'))
    assert len(segments) == 10
    # Each segment ends with its own Game Result line.
    for i, seg in enumerate(segments, start=1):
        assert f'Game Result: Game {i} ended' in seg


def test_split_games_on_concatenated_single_games() -> None:
    single = _read('sim2.log')
    doubled = single + '\n' + single
    segments = split_games(doubled)
    assert len(segments) == 2
    assert all('Game Result: Game 1 ended in 1135 ms' in seg for seg in segments)


def test_split_games_single_game_returns_one_segment() -> None:
    segments = split_games(_read('sim2.log'))
    assert len(segments) == 1


def test_split_games_empty_returns_empty() -> None:
    assert split_games('') == []


# --------------------------------------------------------------------------- #
# extract_match_features — per-game features across a multi-game log
# --------------------------------------------------------------------------- #


def test_match_features_commander_run_ten_games() -> None:
    feats = extract_match_features(
        _read('empirical/commander_run.log'), deck_a='RedCmdr', deck_b='WhiteCmdr'
    )
    assert len(feats) == 10
    # Game 8 is the sole b win (Match Result flips at game 8 in the fixture).
    winners = [f.winner for f in feats]
    assert winners.count('a') == 9
    assert winners.count('b') == 1
    assert winners[7] == 'b'


# --------------------------------------------------------------------------- #
# graceful degradation — never raise on malformed / partial / empty input
# --------------------------------------------------------------------------- #


def test_garbage_log_degrades_to_empty_features() -> None:
    f = extract_game_features(
        'total nonsense\nno forge lines here\n', deck_a='RedTest', deck_b='WhiteTest'
    )
    assert f.winner == 'draw'  # no winner derivable -> neutral 'draw'
    assert f.kill_turn is None
    assert f.win_margin_life is None
    assert f.wincon is None
    assert f.mulligans_a == 0
    assert f.mulligans_b == 0
    assert f.game_length_ms is None
    assert f.lands_by_turn_a == []
    assert f.lands_by_turn_b == []


def test_empty_log_does_not_raise() -> None:
    f = extract_game_features('', deck_a='RedTest', deck_b='WhiteTest')
    assert f.winner == 'draw'
    assert f.lands_by_turn_a == []


def test_truncated_log_does_not_raise() -> None:
    """A log cut off mid-game (no Game Result / Damage) parses what it can and
    leaves un-derivable fields None/[] without raising."""
    truncated = '\n'.join(_read('sim2.log').splitlines()[:120])
    f = extract_game_features(truncated, deck_a='RedTest', deck_b='WhiteTest')
    assert f.winner == 'draw'
    assert f.kill_turn is None
    assert f.win_margin_life is None
    # Lands played up to the truncation point are still captured.
    assert f.lands_by_turn_a  # non-empty
    assert f.lands_by_turn_b


def test_match_features_empty_returns_empty() -> None:
    assert extract_match_features('', deck_a='A', deck_b='B') == []
