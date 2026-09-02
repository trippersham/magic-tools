"""Terminal-cause gate on the scheduler — a concession (``end_cause=concede``) is a rules-legal
loss (CR 104.3a), so it fills its cell slot as DECISIVE and must NEVER be topped up. XMage's CP7 AI
concedes hopeless positions on ordinary games; treating that as invalid data would wrongly exclude +
top-up a common legitimate ending. Drives :class:`SimdScheduler` directly (no JVM, no subprocess).
"""

from __future__ import annotations

from pipeline.sim.game_protocol import GameResult
from pipeline.sim.game_tasks import SeatSpec, build_game_tasks
from pipeline.sim.simd.ops_store import OpsStore
from pipeline.sim.simd.scheduler import SimdScheduler


def _seat(name: str) -> SeatSpec:
    return SeatSpec(deck_path=f'/d/{name}.dck', driver=None)


def _concede(task_id: str) -> GameResult:
    # A concession: the loser conceded, the winner (seat A) is credited normally.
    return GameResult(
        task_id=task_id, winner='a', kill_turn=6, ms=60000, markers=['end_cause=concede'],
        log_path=None, end_cause='concede',
    )


def _decisive(task_id: str) -> GameResult:
    return GameResult(
        task_id=task_id, winner='a', kill_turn=8, ms=60000, markers=['end_cause=lethal_damage'],
        log_path=None, end_cause='lethal_damage',
    )


def test_concede_fills_cell_and_is_not_topped_up(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    # One cell needing exactly one decisive game (driven) + its baseline sibling.
    tasks = build_game_tasks([_seat('sa')], [_seat('oa')], 1, fmt='commander')
    with OpsStore(db) as ops:
        ops.register_tasks(tasks)
        sched = SimdScheduler(tasks, ops=ops, attempt_cap=2, topup_cap=2, bailout_floor_ms=2000)

        driven = next(t for t in tasks if t.task_id.split('|')[2] == 'driven')
        # Drain the fair scheduler until the driven original is dispatched; resolve every other
        # dispatched task decisively so the loop reaches the driven one.
        got = None
        for _ in range(10):
            got = sched.next_task()
            if got is not None and got.task_id == driven.task_id:
                break
            if got is not None:
                sched.on_result(_decisive(got.task_id))
        assert got is not None and got.task_id == driven.task_id
        sched.on_result(_concede(driven.task_id))

        cell = (driven.task_id.split('|')[0], driven.task_id.split('|')[1], 'driven')
        res = sched.result()
        # The concede FILLED the cell decisively (ok advanced) — the cell is complete, never short.
        ok, need = res.cells[cell]
        assert ok == need == 1, (ok, need)
        assert cell not in res.incomplete_cells
        # It is credited in W/L (not excluded like a bailout) and is NOT flagged invalid.
        assert cell not in res.invalid_cells
        # Surfaced as a concession in the coverage breakdown (a data-quality signal).
        assert res.concede_games == 1

        # Draining further enqueues NO top-up for the concede cell (it is already filled).
        drained = []
        for _ in range(5):
            t = sched.next_task()
            if t is None:
                break
            drained.append(t.task_id)
            sched.on_result(_decisive(t.task_id))
        assert not any(
            t.split('|')[3].startswith('topup-') and t.split('|')[:3] == list(cell) for t in drained
        ), drained
