"""Phase A1 — correctness-core unit tests (pure logic, no JVM, no pool).

Covers the three data-integrity fixes:

* A1.1 completeness — a cell filled only with FAILED/BAILOUT tasks must NOT report complete,
  yet its subject must still FINALIZE (aggregated + excluded from W/L, surfaced in coverage).
* A1.2 strict outcome validation + non-decisive exclusion — an unknown ``winner`` is a parse
  FAILURE (not a silent draw default); a ``reason``-set game credits neither seat.
* A1.3 bailout/plausibility gate — an implausibly fast game (< hard floor) is non-decisive
  ``reason=bailout``, never a win.
"""

from __future__ import annotations

import pytest

from pipeline.sim.game_protocol import GameResult, ProtocolError, parse_line
from pipeline.sim.game_queue import (
    BAILOUT_HARD_FLOOR_MS,
    _Governor,
    bailout_reason,
)
from pipeline.sim.game_tasks import GameTask, SeatSpec


def _seat(name: str) -> SeatSpec:
    return SeatSpec(deck_path=f'/d/{name}.dck', driver=None)


def _task(subject: str, opp: str, piloting: str, game: int) -> GameTask:
    return GameTask(
        task_id=f'{subject}|{opp}|{piloting}|{game}',
        fmt='commander',
        seat_a=_seat(subject),
        seat_b=_seat(opp),
    )


def _result(tid: str, winner: str, *, ms: int = 60000, reason: str | None = None) -> GameResult:
    return GameResult(task_id=tid, winner=winner, kill_turn=8, ms=ms, markers=[], log_path=None, reason=reason)


# --------------------------------------------------------------------------- #
# A1.2 — strict outcome validation.
# --------------------------------------------------------------------------- #


def test_unknown_winner_raises() -> None:
    with pytest.raises(ProtocolError):
        parse_line('RESULT {"id": "x|y|driven|0", "winner": "ZZZ", "ms": 60000}')


def test_missing_winner_raises() -> None:
    with pytest.raises(ProtocolError):
        parse_line('RESULT {"id": "x|y|driven|0", "ms": 60000}')


@pytest.mark.parametrize('winner', ['A', 'a', 'B', 'b', 'DRAW', 'draw', 'none', 'NONE'])
def test_known_winners_parse(winner: str) -> None:
    msg = parse_line(f'RESULT {{"id": "x|y|driven|0", "winner": "{winner}", "ms": 60000}}')
    assert isinstance(msg, GameResult)
    assert msg.winner == winner


# --------------------------------------------------------------------------- #
# A1.3 — bailout / plausibility gate (pure function).
# --------------------------------------------------------------------------- #


def test_bailout_reason_hard_floor() -> None:
    assert bailout_reason(_result('t', 'A', ms=1500)) == 'bailout'
    assert bailout_reason(_result('t', 'A', ms=19)) == 'bailout'


def test_bailout_reason_real_game_ok() -> None:
    assert bailout_reason(_result('t', 'A', ms=30000)) is None
    assert bailout_reason(_result('t', 'A', ms=BAILOUT_HARD_FLOOR_MS)) is None


def test_bailout_reason_marker() -> None:
    r = GameResult(
        task_id='t', winner='A', kill_turn=2, ms=60000, markers=['concede'], log_path=None
    )
    assert bailout_reason(r) == 'bailout'


# --------------------------------------------------------------------------- #
# A1.2 — non-decisive exclusion through the governor tally.
# --------------------------------------------------------------------------- #


def test_reason_set_game_credits_neither_seat() -> None:
    # One driven cell, 2 games: one decided (A wins), one reason=timeout (winner claims A).
    tasks = [_task('S', 'O', 'driven', 0), _task('S', 'O', 'driven', 1),
             _task('S', 'O', 'baseline', 0), _task('S', 'O', 'baseline', 1)]
    gov = _Governor(
        tasks, retry_cap=2, monitor=None, done_set=None,
        on_subject_complete=None, cond_poll_s=0.0,
    )
    gov.on_result(_result('S|O|driven|0', 'A'))
    gov.on_result(_result('S|O|driven|1', 'A', reason='timeout'))  # non-decisive
    gov.on_result(_result('S|O|baseline|0', 'B'))
    gov.on_result(_result('S|O|baseline|1', 'B'))
    res = gov.result()
    comp = res.comparisons['S']
    opp = comp.per_opponent[0]
    # driven: only the ONE decided game counts (1 win / 1 decided), the timeout is excluded.
    assert opp.driver_wins == 1
    assert opp.driver_decided == 1
    # the timeout game is surfaced in coverage: its cell is NOT complete (1 ok / 2 needed).
    assert ('S', 'O', 'driven') in res.incomplete_cells


# --------------------------------------------------------------------------- #
# A1.3 — bailout excluded from W/L when the gate is armed.
# --------------------------------------------------------------------------- #


def test_bailout_game_excluded_when_gate_armed() -> None:
    tasks = [_task('S', 'O', 'driven', 0), _task('S', 'O', 'baseline', 0)]
    gov = _Governor(
        tasks, retry_cap=2, monitor=None, done_set=None,
        on_subject_complete=None, cond_poll_s=0.0,
        bailout_floor_ms=BAILOUT_HARD_FLOOR_MS,
    )
    gov.on_result(_result('S|O|driven|0', 'A', ms=800))  # implausibly fast → bailout
    gov.on_result(_result('S|O|baseline|0', 'B', ms=60000))
    res = gov.result()
    comp = res.comparisons['S']
    opp = comp.per_opponent[0]
    assert opp.driver_decided == 0  # the bailout credited nothing
    assert ('S', 'O', 'driven') in res.incomplete_cells


# --------------------------------------------------------------------------- #
# A1.1 — completeness: an all-failed / all-bailout cell finalizes but is not complete.
# --------------------------------------------------------------------------- #


def test_all_failed_cell_finalizes_but_incomplete() -> None:
    from pipeline.sim.game_protocol import GameError

    tasks = [_task('S', 'O', 'driven', 0), _task('S', 'O', 'baseline', 0)]
    gov = _Governor(
        tasks, retry_cap=1, monitor=None, done_set=None,
        on_subject_complete=None, cond_poll_s=0.0,
    )
    # Drive the driven task past its retry cap → FAILED (terminal).
    gov.on_result(GameError(task_id='S|O|driven|0', exc='boom'))
    gov.on_result(GameError(task_id='S|O|driven|0', exc='boom'))
    gov.on_result(_result('S|O|baseline|0', 'B'))
    res = gov.result()
    # Subject FINALIZED (aggregated) despite the failed cell.
    assert 'S' in res.comparisons
    # The failed cell is flagged incomplete (0 ok / 1 needed) and NOT reported complete.
    assert ('S', 'O', 'driven') in res.incomplete_cells
    assert ('S', 'O', 'driven') in res.failed_cells
    assert not res.complete
    ok, needed = res.cells[('S', 'O', 'driven')]
    assert ok == 0 and needed == 1


def test_all_bailout_cell_not_complete() -> None:
    tasks = [_task('S', 'O', 'driven', 0), _task('S', 'O', 'baseline', 0)]
    gov = _Governor(
        tasks, retry_cap=2, monitor=None, done_set=None,
        on_subject_complete=None, cond_poll_s=0.0,
        bailout_floor_ms=BAILOUT_HARD_FLOOR_MS,
    )
    gov.on_result(_result('S|O|driven|0', 'A', ms=500))  # bailout
    gov.on_result(_result('S|O|baseline|0', 'B', ms=60000))
    res = gov.result()
    # the driven cell's ONLY fill is a bailout → must NOT report complete.
    assert ('S', 'O', 'driven') in res.incomplete_cells
    assert not res.complete
