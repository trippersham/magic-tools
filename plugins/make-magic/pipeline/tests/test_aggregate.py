"""Phase A1/A3 — the shared aggregation correctness core (ported from the retired
``test_queue_correctness`` when ``game_queue._Governor`` was deleted at the A3 cutover).

The A1 data-integrity invariants now live against :mod:`pipeline.sim.aggregate` — the single
owner both the old and the simd paths shared:

* A1.1 completeness — a cell filled only with FAILED/BAILOUT tasks must NOT report complete,
  yet its subject must still FINALIZE (aggregated + excluded from W/L, surfaced in coverage).
* A1.2 strict outcome validation + non-decisive exclusion — an unknown ``winner`` is a parse
  FAILURE (not a silent draw default); a ``reason``-set game credits neither seat.
* A1.3 bailout/plausibility gate — an implausibly fast game (< hard floor) is non-decisive
  ``reason=bailout``, never a win.
"""

from __future__ import annotations

import pytest

from pipeline.sim.aggregate import BAILOUT_HARD_FLOOR_MS, RunAggregator, bailout_reason
from pipeline.sim.core import wilson_ci
from pipeline.sim.game_protocol import GameResult, ProtocolError, parse_line


def _result(tid: str, winner: str, *, ms: int = 60000, reason: str | None = None) -> GameResult:
    return GameResult(task_id=tid, winner=winner, kill_turn=8, ms=ms, markers=[], log_path=None, reason=reason)


def _ids(*ids: str) -> list[str]:
    return list(ids)


# --------------------------------------------------------------------------- #
# A1.2 — strict outcome validation (game_protocol codec, unchanged at cutover).
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
    r = GameResult(task_id='t', winner='A', kill_turn=2, ms=60000, markers=['concede'], log_path=None)
    assert bailout_reason(r) == 'bailout'


# --------------------------------------------------------------------------- #
# A1.2 — non-decisive exclusion through the aggregator tally.
# --------------------------------------------------------------------------- #


def test_reason_set_game_credits_neither_seat() -> None:
    ids = _ids('S|O|driven|0', 'S|O|driven|1', 'S|O|baseline|0', 'S|O|baseline|1')
    agg = RunAggregator(ids)
    agg.add_result(_result('S|O|driven|0', 'A'))
    agg.add_result(_result('S|O|driven|1', 'A', reason='timeout'))  # non-decisive
    agg.add_result(_result('S|O|baseline|0', 'B'))
    agg.add_result(_result('S|O|baseline|1', 'B'))
    comp = agg.comparisons()['S']
    opp = comp.per_opponent[0]
    # driven: only the ONE decided game counts (1 win / 1 decided), the timeout is excluded.
    assert opp.driver_wins == 1
    assert opp.driver_decided == 1
    # the timeout game is surfaced in coverage: its cell is NOT complete (1 ok / 2 needed).
    assert ('S', 'O', 'driven') in agg.incomplete_cells()


def test_concede_is_decisive_credited_and_surfaced() -> None:
    # A concession (end_cause=concede) is a rules-legal loss: DECISIVE, credited to the winner, and
    # counted in the concede breakdown so concede-heavy matchups are visible.
    ids = _ids('S|O|driven|0', 'S|O|baseline|0')
    agg = RunAggregator(ids)
    agg.add_result(GameResult(
        task_id='S|O|driven|0', winner='A', kill_turn=6, ms=60000,
        markers=['end_cause=concede'], log_path=None, end_cause='concede',
    ))
    agg.add_result(_result('S|O|baseline|0', 'B'))
    opp = agg.comparisons()['S'].per_opponent[0]
    # The concede is credited to the winner (seat A) exactly like any decisive cause.
    assert (opp.driver_wins, opp.driver_decided) == (1, 1)
    # It FILLS its cell (no top-up owed) — the driven cell is complete, never incomplete.
    assert ('S', 'O', 'driven') not in agg.incomplete_cells()
    ok, needed = agg.cells()[('S', 'O', 'driven')]
    assert ok == needed == 1
    # And it is surfaced in the cause breakdown (the data-quality signal).
    assert agg.concede_count() == 1
    assert ('S', 'O', 'driven') in agg.concede_cells()


def test_draws_excluded_from_denominator() -> None:
    ids = _ids(*(f'S|O|driven|{i}' for i in range(4)), *(f'S|O|baseline|{i}' for i in range(4)))
    agg = RunAggregator(ids)
    for i, w in enumerate(['a', 'a', 'draw', 'b']):
        agg.add_result(_result(f'S|O|driven|{i}', w))
    for i in range(4):
        agg.add_result(_result(f'S|O|baseline|{i}', 'draw'))
    opp = agg.comparisons()['S'].per_opponent[0]
    assert (opp.driver_wins, opp.driver_decided) == (2, 3)  # the draw is NOT in the denominator.
    assert (opp.cp7_wins, opp.cp7_decided) == (0, 0)
    assert opp.winrate_driver_ci == wilson_ci(2, 3)


# --------------------------------------------------------------------------- #
# A1.3 — bailout excluded from W/L when the gate is armed.
# --------------------------------------------------------------------------- #


def test_bailout_game_excluded_when_gate_armed() -> None:
    agg = RunAggregator(_ids('S|O|driven|0', 'S|O|baseline|0'), bailout_floor_ms=BAILOUT_HARD_FLOOR_MS)
    agg.add_result(_result('S|O|driven|0', 'A', ms=800))  # implausibly fast → bailout
    agg.add_result(_result('S|O|baseline|0', 'B', ms=60000))
    opp = agg.comparisons()['S'].per_opponent[0]
    assert opp.driver_decided == 0  # the bailout credited nothing
    assert ('S', 'O', 'driven') in agg.incomplete_cells()


# --------------------------------------------------------------------------- #
# A1.1 — completeness: an all-failed / all-bailout cell finalizes but is not complete.
# --------------------------------------------------------------------------- #


def test_all_failed_cell_finalizes_but_incomplete() -> None:
    agg = RunAggregator(_ids('S|O|driven|0', 'S|O|baseline|0'))
    agg.add_failed('S|O|driven|0')  # burned its budget / quarantined — terminal, no result.
    agg.add_result(_result('S|O|baseline|0', 'B'))
    # Subject FINALIZED (aggregated) despite the failed cell.
    assert 'S' in agg.comparisons()
    # The failed cell is flagged incomplete (0 ok / 1 needed) and NOT reported complete.
    assert ('S', 'O', 'driven') in agg.incomplete_cells()
    assert ('S', 'O', 'driven') in agg.failed_cells()
    assert not agg.complete()
    ok, needed = agg.cells()[('S', 'O', 'driven')]
    assert ok == 0 and needed == 1


def test_all_bailout_cell_not_complete() -> None:
    agg = RunAggregator(_ids('S|O|driven|0', 'S|O|baseline|0'), bailout_floor_ms=BAILOUT_HARD_FLOOR_MS)
    agg.add_result(_result('S|O|driven|0', 'A', ms=500))  # bailout
    agg.add_result(_result('S|O|baseline|0', 'B', ms=60000))
    assert ('S', 'O', 'driven') in agg.incomplete_cells()
    assert not agg.complete()
