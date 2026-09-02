"""Terminal-cause gate on the scheduler — an INVALID game (a decisive claim with NO legal
terminal cause) is excluded from W/L AND triggers a top-up exactly like a non-decisive game, and is
flagged distinctly in coverage. Drives :class:`SimdScheduler` directly (no JVM, no subprocess).
"""

from __future__ import annotations

from pipeline.sim.game_protocol import GameResult
from pipeline.sim.game_tasks import build_game_tasks
from pipeline.sim.simd.ops_store import OpsStore
from pipeline.sim.simd.scheduler import SimdScheduler


def _seat(name):
    from pipeline.sim.game_tasks import SeatSpec

    return SeatSpec(deck_path=f'/d/{name}.dck', driver=None)


def _invalid(task_id: str) -> GameResult:
    # A game that CLAIMS a winner but carries no legal terminal cause (macro-game-over) → INVALID.
    return GameResult(
        task_id=task_id, winner='a', kill_turn=1, ms=22, markers=['end_cause=macro_game_over'],
        log_path=None, end_cause='macro_game_over',
    )


def _decisive(task_id: str) -> GameResult:
    return GameResult(
        task_id=task_id, winner='a', kill_turn=8, ms=60000, markers=['end_cause=lethal_damage'],
        log_path=None, end_cause='lethal_damage',
    )


def test_invalid_result_triggers_topup_and_is_flagged(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    # One cell needing one decisive game (driven) + its baseline sibling.
    tasks = build_game_tasks([_seat('sa')], [_seat('oa')], 1, fmt='commander')
    with OpsStore(db) as ops:
        ops.register_tasks(tasks)
        sched = SimdScheduler(tasks, ops=ops, attempt_cap=2, topup_cap=2, bailout_floor_ms=2000)

        # Dispatch + feed an INVALID result for the driven original.
        driven = next(t for t in tasks if t.task_id.split('|')[2] == 'driven')
        # Drain the fair-scheduler until the driven original is dispatched (its in-flight is now
        # balanced for the on_result decrement); every other dispatched task resolves decisive.
        got = None
        for _ in range(10):
            got = sched.next_task()
            if got is not None and got.task_id == driven.task_id:
                break
            # dispatch other tasks' results as decisive so the loop reaches the driven one.
            if got is not None:
                sched.on_result(_decisive(got.task_id))
        assert got is not None and got.task_id == driven.task_id
        sched.on_result(_invalid(driven.task_id))

        res = sched.result()
        cell = (driven.task_id.split('|')[0], driven.task_id.split('|')[1], 'driven')
        # INVALID is excluded from W/L (ok did not advance) AND flagged distinctly.
        assert cell in res.invalid_cells, res.invalid_cells
        assert res.fast_games == 0  # an INVALID game is NOT counted a fast decisive kill.
        # A top-up replacement was enqueued for the short cell — draining it yields a topup- task.
        drained = []
        for _ in range(5):
            t = sched.next_task()
            if t is None:
                break
            drained.append(t.task_id)
            sched.on_result(_decisive(t.task_id))
        assert any(t.split('|')[3].startswith('topup-') and t.split('|')[:3] == list(cell) for t in drained), drained
        # After the decisive top-up the cell completes.
        ok, need = sched.result().cells[cell]
        assert ok == need == 1


def test_ops_store_roundtrips_end_cause(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    tid = 'sa|oa|driven|0'
    with OpsStore(db) as ops:
        ops.record_result(_decisive(tid))
    with OpsStore(db) as ops:  # reopen — the additive migration + read tolerate the new column.
        loaded = ops.load_results()
    assert loaded[tid].end_cause == 'lethal_damage'
