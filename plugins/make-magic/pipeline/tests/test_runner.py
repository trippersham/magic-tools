"""Tests for the single-matchup runner (:mod:`pipeline.sim.runner`).

The OFFLINE tests parse REAL captured Forge logs (``tests/fixtures/forge/``) to
prove the win-tally + ``Game Result`` dedupe logic and the exit-0 deck-load
failure detection — no JVM is spawned. ONE gated ``@pytest.mark.forge`` test
spawns exactly ONE real Forge JVM (deselected by default via the ``forge``
marker) to prove the end-to-end invoke + parse against the live install.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from pipeline.sim.forge_runtime import ENV_FORGE_HOME, ENV_JAVA, ForgeInstall, resolve
from pipeline.sim.runner import (
    ForgeError,
    MatchResult,
    is_clockout_segment,
    parse_match_log,
    run_matchup,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'forge'


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


# --------------------------------------------------------------------------- #
# parse_match_log — win tally + Game Result dedupe
# --------------------------------------------------------------------------- #


def test_parse_single_game_log() -> None:
    result = parse_match_log(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert result.games == 1
    # WhiteTest is Ai(2) = deck_b in this log.
    assert result.wins_a == 0
    assert result.wins_b == 1
    assert result.draws == 0
    assert len(result.per_game) == 1
    assert result.per_game[0].winner == 'b'
    assert result.per_game[0].elapsed_ms == 1135


def test_parse_thirty_game_log_dedupes_game_result_lines() -> None:
    """The 30-game log ALSO prints 30 'Game Outcome:' twins; count only
    'Game Result:' -> exactly 30 results, correct split (24 b / 6 a)."""
    result = parse_match_log(_read('sim30.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert result.games == 30
    assert result.wins_a == 6  # RedTest = Ai(1)
    assert result.wins_b == 24  # WhiteTest = Ai(2)
    assert result.draws == 0
    assert result.wins_a + result.wins_b + result.draws == 30
    assert len(result.per_game) == 30


def test_parse_empirical_40card_run() -> None:
    result = parse_match_log(_read('empirical/40card_run.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert result.games == 30
    assert result.wins_a == 3
    assert result.wins_b == 27
    assert result.wins_a + result.wins_b + result.draws == 30


def test_parse_empirical_commander_run() -> None:
    result = parse_match_log(_read('empirical/commander_run.log'), deck_a='RedCmdr', deck_b='WhiteCmdr')
    assert result.games == 10
    assert result.wins_a == 9
    assert result.wins_b == 1
    assert result.wins_a + result.wins_b + result.draws == 10


def test_parse_records_raw_log_and_elapsed_ms() -> None:
    raw = _read('sim30.log')
    result = parse_match_log(raw, deck_a='RedTest', deck_b='WhiteTest')
    assert result.raw_log == raw
    # per-game elapsed_ms mirrors the ms captured in each Game Result line.
    assert result.per_game[0].elapsed_ms == 1420
    assert all(g.elapsed_ms > 0 for g in result.per_game)


def test_parse_deck_name_with_spaces_and_parens() -> None:
    """A real Airtable deck name (spaces + parens) parses — winner is slot-keyed.

    Regression: the winner regex was ``\\S+`` and stopped at the first space, so a
    name like ``UR Izzet (Chaos Sealed)`` produced an 'unparseable Game Result'
    error. Only the ``Ai(k)`` slot drives attribution, so the name span must be
    allowed to contain spaces/parens.
    """
    log = (
        'Simulation mode\n'
        'Game Result: Game 1 ended in 1780 ms. Ai(1)-UR Izzet (Chaos Sealed) has won!\n'
        'Game Result: Game 2 ended in 900 ms. Ai(2)-Mono Red (Aggro!) has won!\n'
    )
    result = parse_match_log(log, deck_a='UR Izzet (Chaos Sealed)', deck_b='Mono Red (Aggro!)')
    assert (result.wins_a, result.wins_b, result.draws) == (1, 1, 0)
    assert [g.winner for g in result.per_game] == ['a', 'b']


# --------------------------------------------------------------------------- #
# CLOCKOUT — a draw-clock timeout must NOT tally as a fabricated win (B1b)
# --------------------------------------------------------------------------- #


def test_clockout_log_is_non_decisive_not_a_win() -> None:
    """The captured 2-game clockout fixture (real harness, ``-c 2``) tallies as
    0 wins / 0 losses / 2 draws — NOT the fabricated ``Ai(2) has won!`` the
    ``Game Result`` terminator claims.

    Regression: the parser counted the bogus ``has won!`` terminator, turning
    every clocked-out game into a decisive win (~40-60% of a live run at the old
    30s clock). Each clockout segment carries ``Stopping slow match as draw`` +
    the DUAL ``has won because all opponents have lost`` — the non-decision signal.
    """
    result = parse_match_log(_read('clockout.log'), deck_a='AzoriusMid', deck_b='IzzetMid')
    assert (result.wins_a, result.wins_b, result.draws) == (0, 0, 2)
    assert result.games == 2
    assert [g.winner for g in result.per_game] == ['draw', 'draw']


def test_is_clockout_segment_detects_marker_and_dual_win() -> None:
    """A segment is a clockout on EITHER the explicit marker OR the dual-win pair."""
    marker_only = 'HANDLOG …\nStopping slow match as draw\nGame Result: Game 1 ended in 5 ms. Ai(1)-X has won!'
    assert is_clockout_segment(marker_only)
    dual_win = (
        'Game Outcome: Ai(1)-X has won because all opponents have lost\n'
        'Game Outcome: Ai(2)-Y has won because all opponents have lost\n'
        'Game Result: Game 1 ended in 5 ms. Ai(2)-Y has won!'
    )
    assert is_clockout_segment(dual_win)
    # A NORMAL decisive game (one winner, one loser) is NOT a clockout.
    normal = (
        'Game Outcome: Ai(1)-X has won because all opponents have lost\n'
        'Game Outcome: Ai(2)-Y has lost because life total reached 0\n'
        'Game Result: Game 1 ended in 5 ms. Ai(1)-X has won!'
    )
    assert not is_clockout_segment(normal)


def test_mixed_clockout_and_real_win_only_counts_the_real_game() -> None:
    """One clocked-out game + one genuine win -> 1 win, 1 draw (the clockout)."""
    log = (
        'Game Outcome: Ai(1)-X has won because all opponents have lost\n'
        'Game Outcome: Ai(2)-Y has lost because life total reached 0\n'
        'Game Result: Game 1 ended in 24000 ms. Ai(1)-X has won!\n'
        'Stopping slow match as draw\n'
        'Game Outcome: Ai(1)-X has won because all opponents have lost\n'
        'Game Outcome: Ai(2)-Y has won because all opponents have lost\n'
        'Game Result: Game 2 ended in 2000 ms. Ai(2)-Y has won!\n'
    )
    result = parse_match_log(log, deck_a='X', deck_b='Y')
    assert (result.wins_a, result.wins_b, result.draws) == (1, 0, 1)
    assert [g.winner for g in result.per_game] == ['a', 'draw']


# --------------------------------------------------------------------------- #
# GENUINE draw — the "ended in a Draw! Took <ms> ms." terminator (M4)
# --------------------------------------------------------------------------- #


def test_genuine_draw_line_parses_as_draw_no_error() -> None:
    """The real harness draw terminator (``SimAIMatch.java:219``) is a DRAW, and a
    single-game match with it has ``games == 1`` (no spurious ForgeError)."""
    log = 'Simulation mode\nGame Result: Game 1 ended in a Draw! Took 5000 ms.\n'
    result = parse_match_log(log, deck_a='X', deck_b='Y')
    assert (result.wins_a, result.wins_b, result.draws) == (0, 0, 1)
    assert result.games == 1
    assert result.per_game[0].winner == 'draw'
    assert result.per_game[0].elapsed_ms == 5000


def test_genuine_draw_mixed_with_a_win() -> None:
    """A genuine draw alongside a decisive game tallies both correctly."""
    log = 'Game Result: Game 1 ended in 1000 ms. Ai(1)-X has won!\nGame Result: Game 2 ended in a Draw! Took 2000 ms.\n'
    result = parse_match_log(log, deck_a='X', deck_b='Y')
    assert (result.wins_a, result.wins_b, result.draws) == (1, 0, 1)
    assert [g.winner for g in result.per_game] == ['a', 'draw']


# --------------------------------------------------------------------------- #
# deck-load failure — exit 0 but "Could not load deck" -> ForgeError
# --------------------------------------------------------------------------- #


def test_deck_load_failure_raises_forge_error() -> None:
    with pytest.raises(ForgeError, match='load'):
        parse_match_log(_read('could_not_load.log'), deck_a='RedTest', deck_b='Nonexistent')


def test_zero_results_without_load_marker_raises() -> None:
    with pytest.raises(ForgeError):
        parse_match_log('Simulation mode\nnothing happened\n', deck_a='A', deck_b='B')


# --------------------------------------------------------------------------- #
# MatchResult shape
# --------------------------------------------------------------------------- #


def test_match_result_is_frozen() -> None:
    result = parse_match_log(_read('sim2.log'), deck_a='RedTest', deck_b='WhiteTest')
    assert isinstance(result, MatchResult)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.wins_a = 99  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# GATED: one REAL Forge match (spawns exactly ONE JVM). Deselected by default.
# --------------------------------------------------------------------------- #


@pytest.mark.forge
def test_run_one_real_match() -> None:
    """Spawn exactly ONE Forge JVM and parse a winner.

    Requires MAKE_MAGIC_FORGE_HOME + MAKE_MAGIC_JAVA pointing at the existing
    install. Uses the RedTest/WhiteTest decks already in the Forge profile
    (rendered into ``install.decks_dir`` by ``run_matchup``).
    """
    if not (os.getenv(ENV_FORGE_HOME) and os.getenv(ENV_JAVA)):
        pytest.skip(f'set {ENV_FORGE_HOME} + {ENV_JAVA} to run the gated Forge match')

    install: ForgeInstall = resolve()
    # The two decks ship in the Forge profile's constructed dir; read their text.
    profile_decks = Path.home() / 'Library' / 'Application Support' / 'Forge' / 'decks' / 'constructed'
    deck_a = (profile_decks / 'RedTest.dck').read_text()
    deck_b = (profile_decks / 'WhiteTest.dck').read_text()

    result = run_matchup(
        install,
        deck_a=('RedTest', deck_a),
        deck_b=('WhiteTest', deck_b),
        n=1,
        seed=42,
        fmt='constructed',
        timeout_s=180,
    )
    assert result.games == 1
    assert result.wins_a + result.wins_b + result.draws == 1
    assert result.per_game[0].winner in ('a', 'b', 'draw')
    print(
        f'\n[forge] one real match: {result.wins_a}-{result.wins_b}, '
        f'winner={result.per_game[0].winner}, {result.per_game[0].elapsed_ms} ms'
    )
