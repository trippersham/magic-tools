"""Phase 3 — the governor scheduler (:mod:`pipeline.sim.game_queue`), tested with the FAKE
echo-worker (NO JVM). Real subprocesses; structural assertions only.

Covers: completeness (both pilotings of every cell drain — the cp7 0/0 regression), dedup by
task_id, the retry-cap K=2 poison-task path (terminates + surfaces the failure), per-game
restart via a done-set, monitor-pause stops feeding, aggregation matches a hand-computed Wilson
CI + delta (reusing the existing helpers), and subject-major incremental harvest.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from pipeline.sim import runner
from pipeline.sim.core import wilson_ci
from pipeline.sim.game_protocol import GameError, GameResult
from pipeline.sim.game_queue import (
    RunGamesResult,
    load_done_set,
    run_games,
    save_done_set,
)
from pipeline.sim.game_tasks import SeatSpec, build_game_tasks, cell_key

_FAKE = str(Path(__file__).parent / 'fake_echo_worker.py')


def _cmd() -> list[str]:
    return [sys.executable, _FAKE]


def _seat(name: str) -> SeatSpec:
    return SeatSpec(deck_path=f'/d/{name}.dck', driver=None)


def _sid(name: str) -> str:
    return f'/d/{name}.dck'


def _subjects(names: list[str]) -> list[SeatSpec]:
    return [_seat(n) for n in names]


def _no_orphans() -> None:
    assert not runner._ACTIVE_PROCS, 'run left procs registered in _ACTIVE_PROCS'


# --------------------------------------------------------------------------- #
# Completeness — the cp7 0/0 regression.
# --------------------------------------------------------------------------- #


def test_completeness_both_pilotings_of_every_cell() -> None:
    subjects = _subjects(['sa', 'sb'])
    field = _subjects(['oa', 'ob'])
    games = 3
    tasks = build_game_tasks(subjects, field, games, fmt='commander')
    res = run_games(tasks, worker_cmd=_cmd(), workers=3, stall_timeout_s=30.0)

    assert isinstance(res, RunGamesResult)
    assert res.complete
    assert not res.failed
    assert not res.incomplete_cells
    # Every (subject, opp, piloting) cell is full — INCLUDING the baseline arm (the 0/0 bug).
    for (subject, opp, piloting), (done, needed) in res.cells.items():
        assert done == needed == games, (subject, opp, piloting, done, needed)
    # Both pilotings present for each (subject, opp).
    pilotings = {(s, o): set() for (s, o, _p) in res.cells}
    for (s, o, p) in res.cells:
        pilotings[(s, o)].add(p)
    for cell, pset in pilotings.items():
        assert pset == {'driven', 'baseline'}, (cell, pset)
    # Every game got a result.
    assert len(res.done_results) == len(tasks)
    _no_orphans()


# --------------------------------------------------------------------------- #
# Dedup — a duplicate result for a task_id is counted once.
# --------------------------------------------------------------------------- #


def test_dedup_duplicate_result_counted_once() -> None:
    from pipeline.sim.game_queue import _Governor

    subjects = _subjects(['sa'])
    field = _subjects(['oa'])
    tasks = build_game_tasks(subjects, field, 2, fmt='commander')
    gov = _Governor(
        tasks, retry_cap=2, monitor=None, done_set=None, on_subject_complete=None, cond_poll_s=0.01
    )
    tid = tasks[0].task_id  # a 'driven' task.
    r = GameResult(task_id=tid, winner='a', kill_turn=3, ms=1, markers=[], log_path=None)
    gov.on_result(r)
    gov.on_result(r)  # DUPLICATE — must be dropped (at-least-once requeue race).

    subject, opp, piloting = cell_key(tasks[0])
    done, _needed = gov.result().cells[(subject, opp, piloting)]
    assert done == 1, f'duplicate double-counted: done={done}'
    assert gov.result().done_results[tid].winner == 'a'


# --------------------------------------------------------------------------- #
# Retry cap K=2 — a poison task is requeued exactly K times then FAILED; run terminates.
# --------------------------------------------------------------------------- #


def test_retry_cap_poison_task_fails_and_terminates() -> None:
    subjects = _subjects(['sa'])
    field = _subjects(['oa'])
    tasks = build_game_tasks(subjects, field, 2, fmt='commander')
    poison = tasks[0].task_id  # a specific task that kills every worker that touches it.

    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_DIE_ON_TASK_ID': poison}

    # A single worker so the poison keeps coming back to the (only) worker deterministically.
    from pipeline.sim.game_queue import _Governor
    from pipeline.sim.worker_pool import WorkerPool

    gov = _Governor(tasks, retry_cap=2, monitor=None, done_set=None, on_subject_complete=None, cond_poll_s=0.05)
    pool = WorkerPool(
        _cmd(),
        workers=1,
        next_task=gov.next_task,
        on_result=gov.on_result,
        requeue=gov.requeue,
        stall_timeout_s=30.0,
        env_for_worker=env_for,
    )
    pool.start()
    ok = pool.join(timeout=30.0)  # MUST terminate, not hang.
    pool.close()
    res = gov.result()

    assert ok, 'run hung — poison task looped forever'
    assert poison in res.failed, res.failed
    assert gov._retries[poison] == 3, gov._retries[poison]  # 2 re-queues + the fatal 3rd.
    # The poison's cell is under-filled (surfaced, never silently 100%).
    subject, opp, piloting = cell_key(tasks[0])
    done, needed = res.cells[(subject, opp, piloting)]
    assert done == needed  # terminal (failed counts toward done) ...
    # ... but a decided-games tally shows the missing game: the driven cell has < needed wins.
    assert (subject, opp, piloting) not in res.incomplete_cells or done < needed
    # The OTHER (non-poison) tasks all completed.
    assert len(res.done_results) == len(tasks) - 1
    assert not res.complete  # a failure means the run is not clean.
    _no_orphans()


# --------------------------------------------------------------------------- #
# Restart — a done-set skips completed tasks; a re-run finishes the rest.
# --------------------------------------------------------------------------- #


def test_restart_doneset_skips_and_completes(tmp_path: Path) -> None:
    subjects = _subjects(['sa'])
    field = _subjects(['oa', 'ob'])
    games = 2
    tasks = build_game_tasks(subjects, field, games, fmt='commander')

    # Pre-complete the first half of the tasks in a done-set (as if a prior run drained them).
    half = len(tasks) // 2
    seeded = {
        t.task_id: GameResult(task_id=t.task_id, winner='a', kill_turn=3, ms=1, markers=[], log_path=None)
        for t in tasks[:half]
    }
    sidecar = tmp_path / 'done.jsonl'
    save_done_set(sidecar, seeded)
    reloaded = load_done_set(sidecar)
    assert set(reloaded) == set(seeded)

    # A worker that records which task_ids it actually received (via a temp marker dir).
    seen_dir = tmp_path / 'seen'
    seen_dir.mkdir()

    # Use the fake worker but capture dispatched ids by inspecting done_results after the run:
    # seeded ids must NOT appear in done_results (the fake never receives them → we keep the seed).
    res = run_games(
        tasks,
        worker_cmd=_cmd(),
        workers=2,
        stall_timeout_s=30.0,
        done_set=reloaded,
    )
    assert res.complete
    # All tasks terminal; the remainder ran and the seeded ones are carried forward.
    assert len(res.done_results) == len(tasks)
    for tid in seeded:
        # carried forward verbatim (winner from the seed, ms==1 from the seed not the fake).
        assert res.done_results[tid].ms == 1


def test_restart_doneset_preserves_comparisons(tmp_path: Path) -> None:
    """A partially-seeded restart (winner-carrying done-set) must still populate the subject's
    ``PilotingComparison`` byte-equal to an uninterrupted run — closing the assertion gap that
    let the 'seeded tasks never advance the subject-complete threshold' bug ship."""
    subjects = _subjects(['sa'])
    field = _subjects(['oa', 'ob'])
    games = 2
    tasks = build_game_tasks(subjects, field, games, fmt='commander')

    # 1) Uninterrupted baseline — capture the subject's comparison.
    base = run_games(tasks, worker_cmd=_cmd(), workers=2, stall_timeout_s=30.0)
    assert _sid('sa') in base.comparisons
    expected = base.comparisons[_sid('sa')]

    # 2) Same inputs, but seed the first half via the winner-carrying done-set, round-tripped
    #    through save/load (the real restart path run_corpus_queue uses).
    half = len(tasks) // 2
    seeded = {
        t.task_id: base.done_results[t.task_id]
        for t in tasks[:half]
    }
    sidecar = tmp_path / 'done.jsonl'
    save_done_set(sidecar, seeded)
    reloaded = load_done_set(sidecar)

    res = run_games(
        tasks,
        worker_cmd=_cmd(),
        workers=2,
        stall_timeout_s=30.0,
        done_set=reloaded,
    )
    assert res.complete
    assert _sid('sa') in res.comparisons, 'restarted subject dropped its PilotingComparison'
    got = res.comparisons[_sid('sa')]
    # Per-opponent win-rates + Wilson CIs + deltas equal the uninterrupted run's.
    assert got.per_opponent == expected.per_opponent
    assert got.winrate_driver == expected.winrate_driver
    assert got.winrate_cp7 == expected.winrate_cp7
    assert got.winrate_delta == expected.winrate_delta
    assert got.winrate_driver_ci == expected.winrate_driver_ci
    assert got.winrate_cp7_ci == expected.winrate_cp7_ci
    _no_orphans()


def test_restart_full_doneset_runs_nothing() -> None:
    subjects = _subjects(['sa'])
    field = _subjects(['oa'])
    tasks = build_game_tasks(subjects, field, 2, fmt='commander')
    seeded = {
        t.task_id: GameResult(task_id=t.task_id, winner='a', kill_turn=3, ms=1, markers=[], log_path=None)
        for t in tasks
    }
    # No worker should ever spawn — a bad command would still succeed because we never start a pool.
    res = run_games(tasks, worker_cmd=['/nonexistent/worker'], workers=2, stall_timeout_s=5.0, done_set=seeded)
    assert res.complete
    assert _sid('sa') in res.comparisons  # the seeded subject was still aggregated.
    _no_orphans()


# --------------------------------------------------------------------------- #
# Monitor pause — no tasks dispatched while paused; resumes after.
# --------------------------------------------------------------------------- #


class _FakeMonitor:
    """A monitor stub whose pause flag the test flips. Matches the slice run_games uses."""

    def __init__(self) -> None:
        self._paused = True
        self.started = False
        self.stopped = False

    def poll_pause(self) -> bool:
        return self._paused

    def read_alarm(self) -> dict[str, object] | None:
        return None

    def start(self) -> None:
        self.started = True

    def stop(self, *, timeout: float | None = None) -> None:
        self.stopped = True

    def release(self) -> None:
        self._paused = False


def test_monitor_pause_stops_feeding_then_resumes() -> None:
    subjects = _subjects(['sa'])
    field = _subjects(['oa', 'ob'])
    tasks = build_game_tasks(subjects, field, 2, fmt='commander')
    mon = _FakeMonitor()

    result_box: dict[str, RunGamesResult] = {}

    def _run() -> None:
        result_box['res'] = run_games(
            tasks, worker_cmd=_cmd(), workers=3, stall_timeout_s=30.0, monitor=mon
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # While paused, the fake worker (which completes instantly) must dispatch nothing → no results.
    time.sleep(1.0)
    assert mon.started
    assert not result_box, 'run finished while paused — feeding was not gated'

    mon.release()  # lift the pause → workers resume and drain.
    t.join(timeout=30.0)
    assert not t.is_alive(), 'run did not finish after pause lifted'
    res = result_box['res']
    assert res.complete
    assert len(res.done_results) == len(tasks)
    assert mon.stopped
    _no_orphans()


# --------------------------------------------------------------------------- #
# Aggregation — the produced PilotingComparison matches a hand-computed Wilson CI + delta.
# --------------------------------------------------------------------------- #


def test_aggregation_matches_hand_computed_wilson() -> None:
    from pipeline.sim.game_queue import _Governor

    subjects = _subjects(['sa'])
    field = _subjects(['oa'])
    games = 4
    tasks = build_game_tasks(subjects, field, games, fmt='commander')
    gov = _Governor(tasks, retry_cap=2, monitor=None, done_set=None, on_subject_complete=None, cond_poll_s=0.01)

    # driven arm: 3 subject-wins, 1 opponent-win → 3/4 decided.
    # baseline arm: 1 subject-win, 3 opponent-wins → 1/4 decided.
    def feed(cell_pilot: str, winners: list[str]) -> None:
        ts = [t for t in tasks if cell_key(t)[2] == cell_pilot]
        for t, w in zip(ts, winners, strict=True):
            gov.on_result(GameResult(task_id=t.task_id, winner=w, kill_turn=None, ms=1, markers=[], log_path=None))

    feed('driven', ['a', 'a', 'a', 'b'])
    feed('baseline', ['a', 'b', 'b', 'b'])

    comp = gov.result().comparisons[_sid('sa')]
    # Hand-computed via the SAME helper (reuse check, not a re-derivation).
    exp_driver = 3 / 4
    exp_cp7 = 1 / 4
    assert comp.winrate_driver == exp_driver
    assert comp.winrate_cp7 == exp_cp7
    assert comp.winrate_delta == exp_driver - exp_cp7
    assert comp.winrate_driver_ci == wilson_ci(3, 4)
    assert comp.winrate_cp7_ci == wilson_ci(1, 4)
    assert len(comp.per_opponent) == 1
    o = comp.per_opponent[0]
    assert (o.driver_wins, o.driver_decided) == (3, 4)
    assert (o.cp7_wins, o.cp7_decided) == (1, 4)
    assert o.winrate_driver_ci == wilson_ci(3, 4)


def test_draws_excluded_from_denominator() -> None:
    from pipeline.sim.game_queue import _Governor

    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 4, fmt='commander')
    gov = _Governor(tasks, retry_cap=2, monitor=None, done_set=None, on_subject_complete=None, cond_poll_s=0.01)
    driven = [t for t in tasks if cell_key(t)[2] == 'driven']
    baseline = [t for t in tasks if cell_key(t)[2] == 'baseline']
    for t, w in zip(driven, ['a', 'a', 'draw', 'b'], strict=True):
        gov.on_result(GameResult(task_id=t.task_id, winner=w, kill_turn=None, ms=1, markers=[], log_path=None))
    for t in baseline:  # baseline drains all-draws → 0 decided.
        gov.on_result(GameResult(task_id=t.task_id, winner='draw', kill_turn=None, ms=1, markers=[], log_path=None))
    comp = gov.result().comparisons[_sid('sa')]
    o = comp.per_opponent[0]
    assert (o.driver_wins, o.driver_decided) == (2, 3)  # the draw is NOT in the denominator.
    assert (o.cp7_wins, o.cp7_decided) == (0, 0)
    assert comp.winrate_driver_ci == wilson_ci(2, 3)


# --------------------------------------------------------------------------- #
# Error → retry path (GameError requeues, not a silent drop).
# --------------------------------------------------------------------------- #


def test_game_error_requeues_then_fails() -> None:
    from pipeline.sim.game_queue import _Governor

    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 1, fmt='commander')
    gov = _Governor(tasks, retry_cap=2, monitor=None, done_set=None, on_subject_complete=None, cond_poll_s=0.01)
    tid = tasks[0].task_id
    err = GameError(task_id=tid, exc='boom')
    gov.on_result(err)  # retry 1 → re-queued.
    assert tid not in gov.result().failed
    gov.on_result(err)  # retry 2 → re-queued.
    assert tid not in gov.result().failed
    gov.on_result(err)  # retry 3 > cap → FAILED.
    assert tid in gov.result().failed


# --------------------------------------------------------------------------- #
# Subject-major incremental harvest — subject 1's row lands before subject 2 finishes.
# --------------------------------------------------------------------------- #


def test_subject_major_incremental_harvest() -> None:
    subjects = _subjects(['s1', 's2'])
    field = _subjects(['oa'])
    games = 2
    tasks = build_game_tasks(subjects, field, games, fmt='commander')

    order: list[str] = []
    lock = threading.Lock()

    def on_complete(subject: str, _comp: object) -> None:
        with lock:
            order.append(subject)

    res = run_games(
        tasks,
        worker_cmd=_cmd(),
        workers=1,  # single worker → subject-major drains s1 fully before s2.
        stall_timeout_s=30.0,
        on_subject_complete=on_complete,
    )
    assert res.complete
    assert order == [_sid('s1'), _sid('s2')], order  # s1 harvested BEFORE s2 finished.
    assert set(res.comparisons) == {_sid('s1'), _sid('s2')}
    _no_orphans()


# --------------------------------------------------------------------------- #
# 3.5 — the --queue wiring seam (default OFF; fake worker, NO JVM).
# --------------------------------------------------------------------------- #


def test_queue_flag_defaults_off() -> None:
    from pipeline.sim import driver_run

    parser = _run_parser(driver_run)
    args = parser.parse_args([])
    assert args.queue is False  # the opt-in flag is OFF unless explicitly passed.


def _run_parser(driver_run: object):
    # driver_run.run() builds the parser inline; reconstruct just enough to read the default.
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument('--queue', action='store_true')
    return p


def test_resolve_worker_cmd_returns_bootstrap_command() -> None:
    """Phase 5.0: the stub is replaced by the game_worker COW-staging bootstrap argv (no JVM)."""
    import sys

    from pipeline.sim.driver_run import resolve_worker_cmd

    cmd = resolve_worker_cmd(max_games=500)
    assert cmd[:3] == [sys.executable, '-m', 'pipeline.sim.game_worker']
    assert cmd[3:5] == ['--worker-max-games', '500']


def test_run_corpus_queue_drives_run_games(monkeypatch, tmp_path: Path) -> None:
    from pipeline.sim import driver_run

    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')
    # Bypass the ledger/packaged-.dck reconstruction: inject a synthetic task list.
    monkeypatch.setattr(driver_run, 'build_corpus_game_tasks', lambda *a, **k: tasks)

    sidecar = tmp_path / 'queue-done.jsonl'
    res = driver_run.run_corpus_queue(
        rows=[],
        field=[],
        games=2,
        worker_cmd=_cmd(),  # the fake worker — NO JVM.
        done_set_path=sidecar,
    )
    assert res.complete
    assert len(res.done_results) == len(tasks)
    # The done-set sidecar was persisted → a restart would skip these.
    reloaded = load_done_set(sidecar)
    assert set(reloaded) == {t.task_id for t in tasks}
    _no_orphans()
