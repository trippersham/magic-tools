"""Phase A2 — the simd engine CORE: fair round-robin scheduling, per-cell attempt-cap +
persistent quarantine, and the crash-safe ``ops.duckdb`` operational store (NO JVM — the fake
echo-worker drives every scenario via real subprocesses; structural assertions only).

The THREE integration tests below are the acceptance gate (design A2.1/A2.2/A2.3):

1. **Fairness** — a subject whose every game fails forever cannot prevent the OTHER subjects
   from getting workers and completing (the bug that wedged a real corpus for hours).
2. **Persistent quarantine across restart** — a cell that burns its attempt-cap is quarantined
   AND persisted to ``ops.duckdb``; a fresh engine reconstructed from the store does NOT
   re-attempt it (even with a now-healthy worker).
3. **Crash-safe resume, zero progress loss** — run partway, tear the engine down mid-subject,
   reconstruct from ``ops.duckdb`` → every already-completed game is retained (never re-run) and
   the in-flight subject resumes from where it stopped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pipeline.sim.game_protocol import GameResult
from pipeline.sim.game_tasks import SeatSpec, build_game_tasks, cell_key
from pipeline.sim.simd.engine import SimdRunResult, run_games_simd
from pipeline.sim.simd.ops_store import OpsStore
from pipeline.sim.simd.scheduler import SimdScheduler

_FAKE = str(Path(__file__).parent / 'fake_echo_worker.py')


def _cmd() -> list[str]:
    return [sys.executable, _FAKE]


def _seat(name: str) -> SeatSpec:
    return SeatSpec(deck_path=f'/d/{name}.dck', driver=None)


def _sid(name: str) -> str:
    return f'/d/{name}.dck'


def _subjects(names: list[str]) -> list[SeatSpec]:
    return [_seat(n) for n in names]


# --------------------------------------------------------------------------- #
# A2.3 — ops.duckdb operational store: round-trip + reconstruct.
# --------------------------------------------------------------------------- #


def test_ops_store_result_roundtrip_survives_reconstruct(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')
    with OpsStore(db) as ops:
        ops.register_tasks(tasks)
        res = GameResult(task_id=tasks[0].task_id, winner='a', kill_turn=3, ms=60000, markers=[], log_path=None)
        ops.record_result(res)

    # Reconstruct a brand-new store from the same file: the committed result replays.
    with OpsStore(db) as ops2:
        loaded = ops2.load_results()
        assert set(loaded) == {tasks[0].task_id}
        assert loaded[tasks[0].task_id].winner == 'a'
        assert loaded[tasks[0].task_id].ms == 60000
        assert set(ops2.load_tasks()) == {t.task_id for t in tasks}


def test_ops_store_quarantine_persists(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 1, fmt='commander')
    tid = tasks[0].task_id
    s, o, p = cell_key(tasks[0])
    with OpsStore(db) as ops:
        ops.register_tasks(tasks)
        ops.quarantine(tid, s, o, p, attempts=2, reason='poison')
    with OpsStore(db) as ops2:
        assert ops2.load_quarantine() == {tid}


# --------------------------------------------------------------------------- #
# Sol BLOCKER 1 — a SHRUNK/CHANGED task universe under a reused run_id is REFUSED,
# never silently reconstructed as top-ups (which reported a mixed run "complete").
# --------------------------------------------------------------------------- #


def test_scheduler_refuses_shrunk_universe_with_absent_original(tmp_path) -> None:
    """Sol's changed-``--games`` repro at the store level: a 2-game universe is committed, then a
    1-game universe (the ``|1`` originals now ABSENT) is resumed under the SAME run_id. The absent
    originals must NOT be guessed to be top-ups (which retained stale rows and reported complete) —
    the mismatch is refused loudly so a fresh run_id (production's content-complete id) is required.
    """
    db = tmp_path / 'ops.duckdb'
    big = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')  # ids …|0, …|1
    with OpsStore(db) as ops:
        ops.register_tasks(big)
        small = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 1, fmt='commander')  # only …|0
        with pytest.raises(ValueError, match='absent from the supplied task universe'):
            SimdScheduler(small, ops=ops)


def test_scheduler_resumes_stored_topups_but_not_absent_originals(tmp_path) -> None:
    """A stored TOP-UP absent from the supplied universe IS reconstructed (top-ups are minted at
    runtime, never by build_game_tasks); only absent ORIGINALS are refused. Proves the guard keys on
    the explicit ``topup-`` id, not merely on presence in the supplied list."""
    db = tmp_path / 'ops.duckdb'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 1, fmt='commander')
    subject, opp, pil = cell_key(tasks[0])
    topup_id = '|'.join((subject, opp, pil, 'topup-0'))
    topup = SeatSpec(deck_path=_sid('sa'), driver=None)  # any seat; id carries the cell
    from pipeline.sim.game_tasks import GameTask

    stored_topup = GameTask(task_id=topup_id, fmt='commander', seat_a=topup, seat_b=_seat('oa'))
    with OpsStore(db) as ops:
        ops.register_tasks([*tasks, stored_topup])  # commit the original + a runtime top-up
        # Resume with ONLY the originals: the stored top-up is reconstructed, no refusal.
        sched = SimdScheduler(tasks, ops=ops)
        assert topup_id in sched.result().task_ids


# --------------------------------------------------------------------------- #
# A2.1 — fair round-robin: no subject gets a 2nd worker while another has 0.
# --------------------------------------------------------------------------- #


def test_scheduler_round_robin_one_per_subject_before_seconds(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    subs = _subjects(['sa', 'sb', 'sc'])
    tasks = build_game_tasks(subs, _subjects(['oa']), 3, fmt='commander')
    with OpsStore(db) as ops:
        ops.register_tasks(tasks)
        sched = SimdScheduler(tasks, ops=ops, attempt_cap=2, cond_poll_s=0.01)
        # Pull 3 tasks WITHOUT completing any (simulate 3 workers going busy at once).
        first_three = [sched.next_task() for _ in range(3)]
        subjects_seen = {t.task_id.split('|', 1)[0] for t in first_three}
        # Every subject must have been given a slot before any subject gets a second worker.
        assert subjects_seen == {_sid('sa'), _sid('sb'), _sid('sc')}, subjects_seen


# --------------------------------------------------------------------------- #
# INTEGRATION 1 — FAIRNESS: a poison subject cannot starve the others.
# --------------------------------------------------------------------------- #


def test_integration_fairness_poison_subject_cannot_starve_others(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    # sa is poison: EVERY game of subject sa fails forever (worker dies, no RESULT).
    poison = _sid('sa')
    subjects = _subjects(['sa', 'sb', 'sc'])
    field = _subjects(['oa', 'ob'])
    tasks = build_game_tasks(subjects, field, 2, fmt='commander')

    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_DIE_ON_SUBJECT': poison}

    res = run_games_simd(
        tasks,
        worker_cmd=_cmd(),
        workers=3,
        stall_timeout_s=30.0,
        ops_db_path=db,
        attempt_cap=2,
        env_for_worker=env_for,
        join_timeout_s=60.0,
    )
    assert isinstance(res, SimdRunResult)
    # The run TERMINATED (did not wedge): every task is terminal (done or quarantined).
    # The two HEALTHY subjects fully completed every cell ...
    for subj in (_sid('sb'), _sid('sc')):
        for opp in (_sid('oa'), _sid('ob')):
            for pil in ('driven', 'baseline'):
                ok, needed = res.cells[(subj, opp, pil)]
                assert ok == needed == 2, (subj, opp, pil, ok, needed)
    # ... and the POISON subject's tasks are all quarantined (fast-failed, surfaced not silent).
    poison_tasks = {t.task_id for t in tasks if t.task_id.split('|', 1)[0] == poison}
    assert res.quarantined == poison_tasks, res.quarantined
    # No poison cell is falsely "complete".
    for opp in (_sid('oa'), _sid('ob')):
        for pil in ('driven', 'baseline'):
            ok, needed = res.cells[(poison, opp, pil)]
            assert ok == 0 and needed == 2


# --------------------------------------------------------------------------- #
# INTEGRATION 2 — PERSISTENT QUARANTINE: reconstruct → quarantined cell not re-attempted.
# --------------------------------------------------------------------------- #


def test_integration_quarantine_persists_across_restart(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    poison = _sid('sa')
    subjects = _subjects(['sa', 'sb'])
    field = _subjects(['oa'])
    tasks = build_game_tasks(subjects, field, 1, fmt='commander')

    def env_poison(_idx: int) -> dict[str, str]:
        return {'FAKE_DIE_ON_SUBJECT': poison}

    res1 = run_games_simd(
        tasks,
        worker_cmd=_cmd(),
        workers=2,
        stall_timeout_s=30.0,
        ops_db_path=db,
        attempt_cap=2,
        env_for_worker=env_poison,
        join_timeout_s=60.0,
    )
    poison_tasks = {t.task_id for t in tasks if t.task_id.split('|', 1)[0] == poison}
    assert res1.quarantined == poison_tasks

    # Reconstruct: quarantine replays from ops.duckdb.
    with OpsStore(db) as ops2:
        assert ops2.load_quarantine() == poison_tasks
        attempts_before = ops2.load_attempts()

    # A SECOND run over the SAME task list + SAME ops db, now with a fully HEALTHY worker (no
    # poison env). The quarantined poison tasks must NOT be re-attempted; sb is already done.
    runlog = tmp_path / 'runlog.txt'

    def env_runlog(_idx: int) -> dict[str, str]:
        return {'FAKE_RUNLOG': str(runlog)}

    res2 = run_games_simd(
        tasks,
        worker_cmd=_cmd(),
        workers=2,
        stall_timeout_s=30.0,
        ops_db_path=db,
        attempt_cap=2,
        env_for_worker=env_runlog,
        join_timeout_s=60.0,
    )
    # Zero games ran in the restart (sb was already done; sa stays quarantined).
    ran = runlog.read_text().split() if runlog.exists() else []
    assert ran == [], f'restart re-ran games (quarantine or done-set not honored): {ran}'
    assert res2.quarantined == poison_tasks
    # No new attempts were logged for the quarantined tasks.
    with OpsStore(db) as ops3:
        attempts_after = ops3.load_attempts()
    for tid in poison_tasks:
        assert attempts_after.get(tid, 0) == attempts_before.get(tid, 0), tid


# --------------------------------------------------------------------------- #
# INTEGRATION 3 — CRASH-SAFE RESUME: mid-subject restart loses ZERO completed games.
# --------------------------------------------------------------------------- #


def test_integration_crash_safe_resume_zero_loss(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    runlog = tmp_path / 'runlog.txt'

    def env_runlog(_idx: int) -> dict[str, str]:
        return {'FAKE_RUNLOG': str(runlog)}

    # -- Phase 1: run a PARTIAL slice — subject sa against ONE opponent (oa) only, both games.
    #    This commits some but NOT all of sa's cells (sa is left mid-subject). ------------------
    phase1_tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')
    res1 = run_games_simd(
        phase1_tasks,
        worker_cmd=_cmd(),
        workers=2,
        stall_timeout_s=30.0,
        ops_db_path=db,
        attempt_cap=2,
        env_for_worker=env_runlog,
        join_timeout_s=60.0,
    )
    committed = set(res1.results)
    assert committed, 'phase 1 committed nothing'
    ran_phase1 = runlog.read_text().split()
    assert sorted(ran_phase1) == sorted(committed)

    # -- "Crash": the engine object is gone; ops.duckdb holds the committed state on disk. ------
    with OpsStore(db) as ops:
        assert set(ops.load_results()) == committed

    # -- Phase 2: reconstruct from ops.duckdb over the FULL task universe (sa+sb x oa+ob). The
    #    already-committed sa/oa cells MUST NOT re-run; everything else runs to completion. -----
    full_tasks = build_game_tasks(_subjects(['sa', 'sb']), _subjects(['oa', 'ob']), 2, fmt='commander')
    res2 = run_games_simd(
        full_tasks,
        worker_cmd=_cmd(),
        workers=2,
        stall_timeout_s=30.0,
        ops_db_path=db,
        attempt_cap=2,
        env_for_worker=env_runlog,
        join_timeout_s=60.0,
    )
    ran_total = runlog.read_text().split()
    # ZERO completed game was ever re-run: no task_id appears twice across the two phases.
    assert len(ran_total) == len(set(ran_total)), (
        f'a committed game was RE-RUN on resume: {[t for t in ran_total if ran_total.count(t) > 1]}'
    )
    # The committed phase-1 games did NOT re-run in phase 2.
    ran_phase2 = ran_total[len(ran_phase1) :]
    assert committed.isdisjoint(set(ran_phase2)), 'a committed game re-ran in phase 2'
    # The full run is now complete — every cell of every subject filled, none lost.
    assert res2.complete, res2.incomplete_cells
    for (subj, opp, pil), (ok, needed) in res2.cells.items():
        assert ok == needed == 2, (subj, opp, pil, ok, needed)
    # And the committed games are RETAINED in the final result (not re-derived, not dropped).
    assert committed <= set(res2.results)
