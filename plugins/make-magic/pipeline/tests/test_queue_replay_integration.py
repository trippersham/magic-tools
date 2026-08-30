"""Phase A1 behavioral gate — REPLAY the real 2265-game done-set through the governor
aggregation and prove the data-integrity fixes reproduce the audit.

The corpus lives at ``~/.local/share/make-magic/sim/driver_run_queue/done_set.jsonl``
(each row: ``id = subject|opponent|arm|game``, ``winner`` ∈ {A,B,DRAW,none}, ``kill_turn``,
``ms``, ``markers``). Marked ``@integration`` because it needs that real file; it SKIPS
cleanly when the file is absent.

Asserts:
  (a) pooled driven−baseline delta ≈ +0.095 (the audit's number) with the gate DISARMED;
  (b) games with ms < the hard floor are excluded as bailouts when the gate is ARMED;
  (c) no cell reports "complete" while ok < needed (bailout/failed fills don't count);
  (d) an unknown winner value raises (strict validation).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.sim.game_protocol import GameResult, ProtocolError, parse_line
from pipeline.sim.game_queue import BAILOUT_HARD_FLOOR_MS, _Governor
from pipeline.sim.game_tasks import GameTask, SeatSpec

pytestmark = pytest.mark.integration

_DONE_SET = (
    Path.home() / '.local' / 'share' / 'make-magic' / 'sim' / 'driver_run_queue' / 'done_set.jsonl'
)


def _load_rows() -> list[dict]:
    if not _DONE_SET.is_file():
        pytest.skip(f'real done-set absent: {_DONE_SET}')
    rows = [json.loads(line) for line in _DONE_SET.read_text().splitlines() if line.strip()]
    if not rows:
        pytest.skip('done-set present but empty')
    return rows


def _tasks_and_results(rows: list[dict]) -> tuple[list[GameTask], list[GameResult]]:
    tasks: list[GameTask] = []
    results: list[GameResult] = []
    for r in rows:
        tid = r['id']
        subject, opp, _arm, _g = tid.split('|')
        tasks.append(
            GameTask(
                task_id=tid,
                fmt='commander',
                seat_a=SeatSpec(deck_path=subject, driver=None),
                seat_b=SeatSpec(deck_path=opp, driver=None),
            )
        )
        results.append(
            GameResult(
                task_id=tid,
                winner=str(r['winner']),
                kill_turn=r.get('kill_turn'),
                ms=int(r.get('ms', 0)),
                markers=list(r.get('markers', [])),
                log_path=r.get('log'),
                reason=r.get('reason'),
            )
        )
    return tasks, results


def _replay(tasks: list[GameTask], results: list[GameResult], *, floor_ms: int) -> _Governor:
    gov = _Governor(
        tasks, retry_cap=2, monitor=None, done_set=None,
        on_subject_complete=None, cond_poll_s=0.0, bailout_floor_ms=floor_ms,
    )
    for res in results:
        gov.on_result(res)
    return gov


def _pooled(gov: _Governor) -> tuple[int, int, int, int]:
    """Pool driver/baseline (wins, decided) across every subject comparison."""
    dw = dd = cw = cd = 0
    for comp in gov.result().comparisons.values():
        for o in comp.per_opponent:
            dw += o.driver_wins
            dd += o.driver_decided
            cw += o.cp7_wins
            cd += o.cp7_decided
    return dw, dd, cw, cd


def _decisive_ab(rows: list[dict], *, min_ms: int) -> int:
    return sum(
        1
        for r in rows
        if str(r['winner']).strip().upper() in ('A', 'B') and int(r.get('ms', 0)) >= min_ms
    )


# --------------------------------------------------------------------------- #
# (a) audit reproduction — pooled delta ≈ +0.095, gate DISARMED.
# --------------------------------------------------------------------------- #


def test_replay_reproduces_audit_delta() -> None:
    rows = _load_rows()
    tasks, results = _tasks_and_results(rows)
    gov = _replay(tasks, results, floor_ms=0)
    dw, dd, cw, cd = _pooled(gov)
    delta = dw / dd - cw / cd
    # Every A/B game counts as decided (draws/timeouts excluded); pooled delta reproduces +0.095.
    assert dd + cd == _decisive_ab(rows, min_ms=0)
    assert delta == pytest.approx(0.095, abs=0.005)


# --------------------------------------------------------------------------- #
# (b) bailout exclusion — ms < hard floor drops out of the decided denominator.
# --------------------------------------------------------------------------- #


def test_replay_bailouts_excluded_when_gate_armed() -> None:
    rows = _load_rows()
    tasks, results = _tasks_and_results(rows)
    gov = _replay(tasks, results, floor_ms=BAILOUT_HARD_FLOOR_MS)
    dw, dd, cw, cd = _pooled(gov)
    # Decided pool now excludes every sub-floor game: it equals the A/B games with ms >= floor.
    assert dd + cd == _decisive_ab(rows, min_ms=BAILOUT_HARD_FLOOR_MS)
    # And that is strictly fewer than the disarmed pool (bailouts really were removed).
    assert dd + cd < _decisive_ab(rows, min_ms=0)


# --------------------------------------------------------------------------- #
# (c) no false-complete cell — ok < needed is never reported complete.
# --------------------------------------------------------------------------- #


def test_replay_no_false_complete_cell() -> None:
    rows = _load_rows()
    tasks, results = _tasks_and_results(rows)
    gov = _replay(tasks, results, floor_ms=BAILOUT_HARD_FLOOR_MS)
    res = gov.result()
    incomplete = set(res.incomplete_cells)
    # Invariant: every cell with ok < needed is flagged incomplete (none silently "complete").
    for cell, (ok, needed) in res.cells.items():
        if ok < needed:
            assert cell in incomplete, cell
    # A synthetic all-bailout cell must be surfaced as incomplete too (constructive proof of (c)).
    extra = GameTask(
        task_id='ZZ|ZZ|driven|0', fmt='commander',
        seat_a=SeatSpec(deck_path='ZZ', driver=None), seat_b=SeatSpec(deck_path='ZZ', driver=None),
    )
    gov2 = _Governor(
        [extra], retry_cap=2, monitor=None, done_set=None,
        on_subject_complete=None, cond_poll_s=0.0, bailout_floor_ms=BAILOUT_HARD_FLOOR_MS,
    )
    gov2.on_result(GameResult(task_id='ZZ|ZZ|driven|0', winner='A', kill_turn=1, ms=500, markers=[], log_path=None))
    assert ('ZZ', 'ZZ', 'driven') in gov2.result().incomplete_cells


# --------------------------------------------------------------------------- #
# (d) strict validation — an unknown winner in the wire codec raises.
# --------------------------------------------------------------------------- #


def test_unknown_winner_raises_on_wire() -> None:
    with pytest.raises(ProtocolError):
        parse_line('RESULT {"id": "s|o|driven|0", "winner": "WAT", "ms": 60000}')
