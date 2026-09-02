"""Phase A2b — simd RESILIENCE layer: boot self-check + crash-loop circuit breaker + resource
governance + flock/killpg orphan reaping. These are the failing-first integration gates for the
five real-corpus failures this phase prevents (NO real JVM — the fake echo-worker drives every
boot/death scenario via real subprocesses; structural assertions only):

1. **Missing/wrong Java → fatal preflight** — the run raises ``BootFailure`` with ZERO workers and
   ZERO staging dirs (never the silent per-worker crash that flooded staging).
2. **Crash-loop circuit breaker** — workers that die BEFORE ``READY`` trip the breaker within N
   spawns; the run aborts and total spawns stay BOUNDED (the 33k-staging-dir flood cannot recur).
3. **Disk floor admission** — a probe below the HARD floor halts admission, reaps in-flight, and
   exits RESUMABLE (no wedge-paused-forever).
4. **Orphan reaping + singleton** — a SIGTERM-ignoring child tree is reaped TERM→KILL; a stale
   pidfile for a dead PID reaps only the matching tree; a 2nd simd flock launch refuses.
5. **Staging GC correctness** — a dead-PID ``xmage-worker-<pid>`` dir is GC'd while a LIVE
   worker's dir survives even under a 0s mid-run cutoff (proves the PID-parse fix).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.sim import runner as runner_mod
from pipeline.sim.game_tasks import SeatSpec, build_game_tasks
from pipeline.sim.simd.circuit_breaker import CrashLoopBreaker
from pipeline.sim.simd.engine import run_games_simd
from pipeline.sim.simd.governor import Admission, DiskGovernor, stable_pool_size
from pipeline.sim.simd.ops_store import OpsStore
from pipeline.sim.simd.preflight import (
    BootFailure,
    java_major_version,
    preflight_java,
)
from pipeline.sim.simd.reaper import (
    AlreadyRunning,
    SingletonLock,
    _read_worker_records,
    reap_orphan_tree,
    record_worker_pgid,
    write_pidfile,
)
from pipeline.sim.simd.scheduler import SimdScheduler

_FAKE = str(Path(__file__).parent / 'fake_echo_worker.py')


def _cmd() -> list[str]:
    return [sys.executable, _FAKE]


def _subjects(names: list[str]) -> list[SeatSpec]:
    return [SeatSpec(deck_path=f'/d/{n}.dck', driver=None) for n in names]


# ===================================================================================== #
# A2.4 — boot self-check: java version parsing + version gate (unit).
# ===================================================================================== #


@pytest.mark.parametrize(
    ('banner', 'expected'),
    [
        ('openjdk version "21.0.3" 2024-04-16', 21),
        ('java version "1.8.0_401"', 8),
        ('openjdk version "17.0.9" 2023-10-17', 17),
        ('openjdk version "24" 2025-03-18', 24),
        ('no version here', None),
    ],
)
def test_java_major_version_parse(banner: str, expected: int | None) -> None:
    assert java_major_version(banner) == expected


def test_preflight_java_missing_binary_raises_bootfailure() -> None:
    def probe(_java: Path) -> str:
        raise FileNotFoundError('no such file')

    with pytest.raises(BootFailure, match='not found or not executable'):
        preflight_java('/nonexistent/java', min_major=21, probe=probe)


def test_preflight_java_too_old_raises_bootfailure() -> None:
    with pytest.raises(BootFailure, match='too old'):
        preflight_java('/usr/bin/java', min_major=21, probe=lambda _j: 'openjdk version "11.0.1"')


def test_preflight_java_unparseable_raises_bootfailure() -> None:
    with pytest.raises(BootFailure, match='could not parse'):
        preflight_java('/usr/bin/java', min_major=21, probe=lambda _j: 'garbage output')


def test_preflight_java_ok_returns_path() -> None:
    got = preflight_java('/opt/jre/bin/java', min_major=21, probe=lambda _j: 'openjdk version "21.0.3"')
    assert got == Path('/opt/jre/bin/java')


# ===================================================================================== #
# GATE 1 — missing/wrong Java → fatal preflight: ZERO workers, ZERO staging.
# ===================================================================================== #


def test_gate1_bad_java_preflight_aborts_before_any_spawn(tmp_path, monkeypatch) -> None:
    db = tmp_path / 'ops.duckdb'
    staging = tmp_path / 'staging'
    monkeypatch.setattr(runner_mod, 'staging_root', lambda: staging)
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')
    spawnlog = tmp_path / 'spawns.txt'

    def bad_preflight() -> None:
        preflight_java('/nonexistent/java', min_major=21, probe=lambda _j: (_ for _ in ()).throw(FileNotFoundError()))

    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_SPAWNLOG': str(spawnlog)}

    with pytest.raises(BootFailure):
        run_games_simd(
            tasks, worker_cmd=_cmd(), workers=2, stall_timeout_s=30.0, ops_db_path=db,
            preflight=bad_preflight, env_for_worker=env_for, join_timeout_s=30.0,
        )
    # ZERO workers ever spawned ...
    assert not spawnlog.exists(), 'a worker spawned despite a failed preflight'
    # ... and ZERO staging dirs created.
    assert not staging.exists() or list(staging.iterdir()) == []


# ===================================================================================== #
# A2.4 — crash-loop breaker (unit).
# ===================================================================================== #


def test_crash_loop_breaker_trips_at_threshold_in_window() -> None:
    clock = {'t': 0.0}
    b = CrashLoopBreaker(max_pre_ready_deaths=3, window_s=10.0, clock=lambda: clock['t'])
    assert not b.record_pre_ready_death()
    assert not b.record_pre_ready_death()
    assert b.record_pre_ready_death()  # 3rd within window → trip.
    assert b.tripped


def test_crash_loop_breaker_prunes_outside_window() -> None:
    clock = {'t': 0.0}
    b = CrashLoopBreaker(max_pre_ready_deaths=3, window_s=10.0, clock=lambda: clock['t'])
    b.record_pre_ready_death()
    clock['t'] = 20.0  # far outside the window — the earlier death ages out.
    b.record_pre_ready_death()
    b.record_pre_ready_death()
    assert not b.tripped  # only 2 within the live window.


def test_crash_loop_breaker_ready_clears_window() -> None:
    b = CrashLoopBreaker(max_pre_ready_deaths=2, window_s=1000.0)
    b.record_pre_ready_death()
    b.record_ready()  # a successful boot resets the pending pre-READY tally.
    assert not b.record_pre_ready_death()
    assert not b.tripped


# ===================================================================================== #
# GATE 2 — crash-loop breaker trips, run aborts, staging/spawn stays BOUNDED.
# ===================================================================================== #


def test_gate2_crashloop_breaker_bounds_spawns_no_flood(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    spawnlog = tmp_path / 'spawns.txt'
    tasks = build_game_tasks(_subjects(['sa', 'sb']), _subjects(['oa']), 2, fmt='commander')
    breaker = CrashLoopBreaker(max_pre_ready_deaths=5, window_s=1000.0)

    def env_for(_idx: int) -> dict[str, str]:
        # Every worker dies BEFORE emitting READY → a deterministic boot failure.
        return {'FAKE_EXIT_BEFORE_READY': '1', 'FAKE_SPAWNLOG': str(spawnlog)}

    with pytest.raises(BootFailure, match='circuit breaker'):
        run_games_simd(
            tasks, worker_cmd=_cmd(), workers=2, stall_timeout_s=30.0, ops_db_path=db,
            breaker=breaker, env_for_worker=env_for, join_timeout_s=60.0,
        )
    assert breaker.tripped
    spawns = spawnlog.read_text().split() if spawnlog.exists() else []
    # BOUNDED: at most initial pool (2) + breaker budget (5) + a small race margin — NOT a flood.
    assert len(spawns) <= 12, f'crash-loop was not bounded: {len(spawns)} spawns'


# ===================================================================================== #
# A2.5 — resource governance (unit + gate 3).
# ===================================================================================== #


def test_stable_pool_size_is_deterministic_from_probes() -> None:
    # No ratchet: pure function of injected probes, computed once.
    assert stable_pool_size(cores=8, free_mem_gib=16.0, per_jvm_gib=2.0, hard_cap=6) == 6
    assert stable_pool_size(cores=4, free_mem_gib=16.0, per_jvm_gib=2.0, hard_cap=6) == 2
    assert stable_pool_size(cores=8, free_mem_gib=2.0, per_jvm_gib=2.0, hard_cap=6) == 1


def test_disk_governor_soft_and_hard_thresholds() -> None:
    # M2: check() is a PURE decision — no reaper, no process side effects on any thread.
    # OK above soft.
    g = DiskGovernor(soft_floor_gib=10.0, hard_floor_gib=5.0, disk_probe=lambda _p: 20.0)
    assert g.check() is Admission.OK
    # PAUSE between hard and soft.
    g = DiskGovernor(soft_floor_gib=10.0, hard_floor_gib=5.0, disk_probe=lambda _p: 7.0)
    assert g.check() is Admission.PAUSE
    assert not g.halted
    # HALT below hard, and STAYS halted.
    g = DiskGovernor(soft_floor_gib=10.0, hard_floor_gib=5.0, disk_probe=lambda _p: 1.0)
    assert g.check() is Admission.HALT
    assert g.halted
    assert g.check() is Admission.HALT  # stays halted (resumable exit, not oscillating).


def test_disk_governor_check_has_no_reaper_param() -> None:
    """M2 regression: the governor takes no process ``reaper`` — reaping is the engine/pool's job,
    never a pipe-touching call from the scheduler thread."""
    import inspect

    params = inspect.signature(DiskGovernor.__init__).parameters
    assert 'reaper' not in params


def test_gate3_disk_hard_floor_halts_admission(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')
    gov = DiskGovernor(soft_floor_gib=10.0, hard_floor_gib=5.0, disk_probe=lambda _p: 1.0)
    with OpsStore(db) as ops:
        ops.register_tasks(tasks)
        sched = SimdScheduler(tasks, ops=ops, disk_governor=gov, cond_poll_s=0.01)
        # Admission HALTS immediately: next_task drains (None) rather than wedging, and exposes
        # disk_halted so the engine reaps in-flight through the pool (never the scheduler thread).
        assert sched.next_task() is None
        assert sched.disk_halted
    assert gov.halted


def test_gate3_disk_halt_requeue_is_non_charging(tmp_path) -> None:
    """M3: a worker reaped because the disk HALTED is a NON-ATTEMPT — its in-flight task is neither
    charged toward the attempt cap nor quarantined (healthy task, failed disk), left non-terminal
    for a resumable re-run."""
    db = tmp_path / 'ops.duckdb'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')
    # Probe stays OK for the first admission, then drops below the hard floor.
    seq = iter([20.0, 1.0])
    gov = DiskGovernor(soft_floor_gib=10.0, hard_floor_gib=5.0, disk_probe=lambda _p: next(seq, 1.0))
    with OpsStore(db) as ops:
        ops.register_tasks(tasks)
        sched = SimdScheduler(tasks, ops=ops, disk_governor=gov, cond_poll_s=0.01, attempt_cap=1)
        task = sched.next_task()  # admit one (disk OK)
        assert task is not None
        assert sched.next_task() is None  # now halted
        assert sched.disk_halted
        # The reaped worker returns its in-flight task. With attempt_cap=1 the NORMAL path would
        # quarantine on the first requeue; the disk-HALT guard must NOT.
        sched.requeue(task)
        assert task.task_id not in sched.result().quarantined


def test_gate3_engine_halt_reap_neutralizes_dispatch_charge(tmp_path) -> None:
    """Sol HIGH 4 / Fable M3 on the ENGINE path: a task admitted just before a disk HARD-floor HALT
    is reaped by ``pool.close()``. Because ``close()`` sets ``_closing`` (suppressing requeue), the
    engine must hand the in-flight task to the scheduler's NON-CHARGING halt-reap so its persisted
    ``dispatch`` attempt is neutralized — otherwise the charge survives (attempts=1) and erodes the
    resume budget of a healthy task. After the run the ops store must show ZERO dispatch attempts:
    the task retains its FULL budget on resume."""
    db = tmp_path / 'ops.duckdb'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')
    # Probe OK for the first admission, then below the hard floor for the next check → HALT with a
    # task already in flight (the worker that got it goes silent and holds it through the halt).
    seq = iter([20.0, 1.0])
    gov = DiskGovernor(soft_floor_gib=10.0, hard_floor_gib=5.0, disk_probe=lambda _p: next(seq, 1.0))

    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_SILENT': '1'}  # accept the task, then never emit RESULT (held in flight).

    res = run_games_simd(
        tasks, worker_cmd=_cmd(), workers=2, stall_timeout_s=30.0, ops_db_path=db,
        disk_governor=gov, attempt_cap=1, env_for_worker=env_for, join_timeout_s=30.0,
    )
    assert not res.complete  # incomplete but RETURNED (resumable, no wedge).
    assert gov.halted
    # The one in-flight task's dispatch attempt was neutralized — no charged attempts persist, so a
    # resume restores the FULL attempt budget (with attempt_cap=1 a surviving charge would quarantine
    # the healthy task on its very first resume dispatch).
    with OpsStore(db) as ops:
        assert ops.load_attempts() == {}, ops.load_attempts()


def test_gate3_engine_normal_close_still_charges_dispatch(tmp_path) -> None:
    """Guard the halt-reap is HALT-ONLY: a normal (non-disk) close must NOT neutralize a real
    dispatch. A poison subject that quarantines charges its attempts as before."""
    db = tmp_path / 'ops.duckdb'
    poison = '/d/sa.dck'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 1, fmt='commander')

    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_DIE_ON_SUBJECT': poison}

    run_games_simd(
        tasks, worker_cmd=_cmd(), workers=1, stall_timeout_s=30.0, ops_db_path=db,
        attempt_cap=2, env_for_worker=env_for, join_timeout_s=60.0,
    )
    with OpsStore(db) as ops:
        # No disk halt → real dispatch charges are retained (the poison task burned its cap).
        assert any(v > 0 for v in ops.load_attempts().values()), ops.load_attempts()


def test_gate3_engine_resumable_exit_under_disk_halt(tmp_path) -> None:
    """A HARD disk breach from the start → the run drains and exits RESUMABLE (incomplete, not
    wedged); its ops.duckdb holds whatever committed so a later relaunch can resume. The engine
    reaps in-flight through the pool's own close() (M2), not the scheduler thread."""
    db = tmp_path / 'ops.duckdb'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 2, fmt='commander')
    gov = DiskGovernor(soft_floor_gib=10.0, hard_floor_gib=5.0, disk_probe=lambda _p: 1.0)
    res = run_games_simd(
        tasks, worker_cmd=_cmd(), workers=2, stall_timeout_s=30.0, ops_db_path=db,
        disk_governor=gov, join_timeout_s=30.0,
    )
    assert not res.complete  # nothing admitted → incomplete, but the call RETURNED (no wedge).
    assert gov.halted


# ===================================================================================== #
# GATE 4 — singleton flock + crash-safe orphan-tree reaping.
# ===================================================================================== #


def test_cleanly_retired_worker_pgid_leaves_sidecar(tmp_path) -> None:
    """A worker that RETIRES NORMALLY has its pgid dropped from the reaper sidecar (on_retire),
    so a later crash-reaper never killpg's a pgid the OS has since recycled."""
    db = tmp_path / 'ops.duckdb'
    pidfile = tmp_path / 'simd.pid'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 1, fmt='commander')
    # One worker → one reader thread → its clean retire drops its own pgid from the sidecar with no
    # concurrent-writer contention. Without the engine's on_retire wiring the pgid would linger.
    res = run_games_simd(
        tasks, worker_cmd=_cmd(), workers=1, stall_timeout_s=30.0, ops_db_path=db,
        pidfile_path=pidfile, join_timeout_s=30.0,
    )
    assert res.complete
    assert _read_worker_records(pidfile) == []


def test_gate4_singleton_flock_refuses_second_launch(tmp_path) -> None:
    lock_path = tmp_path / 'simd.lock'
    first = SingletonLock(lock_path).acquire()
    try:
        with pytest.raises(AlreadyRunning):
            SingletonLock(lock_path).acquire()
    finally:
        first.release()
    # Released → a fresh acquire succeeds.
    second = SingletonLock(lock_path).acquire()
    second.release()


def test_gate4_reaper_term_then_kill_sequence_injected() -> None:
    """The reaper escalates SIGTERM → grace → SIGKILL against a group that survives TERM."""
    signals: list[int] = []
    members = {'alive': True}

    def fake_killpg(_pgid: int, sig: int) -> None:
        if sig == signal.SIGKILL:
            members['alive'] = False
        signals.append(sig)

    # A pidfile whose leader is dead but whose group still has members.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        pf = Path(d) / 'pid'
        dead = subprocess.Popen([sys.executable, '-c', ''])
        dead.wait()
        pf.write_text(f'{dead.pid} {dead.pid}\n')  # leader dead; we fake the group as live.

        import pipeline.sim.simd.reaper as reaper_mod

        # Force "group has members" until SIGKILL flips it.
        orig = reaper_mod._pgid_has_members
        reaper_mod._pgid_has_members = lambda _pgid: members['alive']  # type: ignore[assignment]
        try:
            reaped = reap_orphan_tree(pf, grace_s=0.2, poll_s=0.01, killpg=fake_killpg, sleep=lambda _s: None)
        finally:
            reaper_mod._pgid_has_members = orig  # type: ignore[assignment]

    assert signals[0] == signal.SIGTERM
    assert signal.SIGKILL in signals  # escalated to KILL after TERM did not clear the group.
    assert reaped == dead.pid


def test_gate4_reaper_kills_real_sigterm_ignoring_tree(tmp_path) -> None:
    """End-to-end: a real child in its own process group that IGNORES SIGTERM is reaped (KILL)."""
    pf = tmp_path / 'pid'
    # A dead leader pid to make the reaper act (a live leader would be left alone).
    dead = subprocess.Popen([sys.executable, '-c', ''])
    dead.wait()
    # A real orphan: own session (pid == pgid), ignores SIGTERM, sleeps.
    child = subprocess.Popen(
        [sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],
        start_new_session=True,
    )
    # A control child in a DIFFERENT group, NOT referenced by the pidfile — must survive.
    control = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    try:
        pf.write_text(f'{dead.pid} {child.pid}\n')  # group == the child's own pgid.
        reaped = reap_orphan_tree(pf, grace_s=0.5, poll_s=0.02)
        assert reaped == child.pid
        # The SIGTERM-ignoring child was escalated to SIGKILL and is now dead.
        assert child.wait(timeout=5) is not None
        # The unrelated control tree is untouched.
        assert control.poll() is None
        assert not pf.exists()  # handled pidfile is cleared.
    finally:
        for p in (child, control):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_gate4_reaper_kills_start_new_session_worker_tree(tmp_path) -> None:
    """F-2: a crashed leader's workers live in their OWN process groups (``start_new_session``).

    The leader's own group is empty on crash, so the old ``killpg(leader_pgid)`` reaper never
    reached the workers — cleanup fell back to slow stdin-EOF self-exit. The reaper must reap the
    RECORDED worker pgids: a real child in its own session, referenced only via
    ``record_worker_pgid``, is KILLED on the next startup (zero survivors), NOT left to self-exit.
    """
    pf = tmp_path / 'pid'
    # A dead leader whose OWN process group has no members (its child is in a separate session).
    dead = subprocess.Popen([sys.executable, '-c', ''])
    dead.wait()
    # A worker exactly like WorkerPool spawns: own session/group (pgid == pid), ignores SIGTERM.
    worker = subprocess.Popen(
        [sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],
        start_new_session=True,
    )
    # A control worker, own group, NOT recorded — must survive (own-lineage only, never broad kill).
    control = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    try:
        # Leader pidfile records only the (empty) leader group — reproducing the F-2 gap.
        pf.write_text(f'{dead.pid} {dead.pid}\n')
        record_worker_pgid(pf, worker.pid)  # the reparented worker's own pgid.
        reaped = reap_orphan_tree(pf, grace_s=0.5, poll_s=0.02)
        assert reaped is not None
        # The recorded worker group was actually reaped (SIGTERM-ignoring → escalated to SIGKILL).
        assert worker.wait(timeout=5) is not None
        assert control.poll() is None  # the unrecorded control worker is untouched.
        assert not pf.exists()  # pidfile + its worker sidecar are cleared once handled.
    finally:
        for p in (worker, control):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_gate4_reaper_leaves_live_leader_alone(tmp_path) -> None:
    """A pidfile whose LEADER pid is still alive is NOT reaped — the flock, not the reaper, owns
    the live-concurrent-owner case."""
    pf = tmp_path / 'pid'
    pf.write_text(f'{os.getpid()} {os.getpgrp()}\n')  # our own live pid as the leader.
    assert reap_orphan_tree(pf) is None
    assert pf.exists()  # untouched — a live owner's pidfile is left in place.


def test_write_pidfile_records_pid_and_pgid(tmp_path) -> None:
    pf = tmp_path / 'pid'
    rec = write_pidfile(pf, setpgrp=False)  # setpgrp=False: never detach the test runner's group.
    assert rec.pid == os.getpid()
    first_two = pf.read_text().split()[:2]
    assert first_two == [str(rec.pid), str(rec.pgid)]


# ===================================================================================== #
# Sol HIGH 3 — reaper process IDENTITY (PID reuse can't be reaped on integers alone).
# ===================================================================================== #


def test_write_pidfile_records_start_token_and_nonce(tmp_path) -> None:
    pf = tmp_path / 'pid'
    rec = write_pidfile(pf, setpgrp=False, nonce='run-xyz')
    fields = pf.read_text().split()
    assert fields[0] == str(rec.pid)
    assert fields[1] == str(rec.pgid)
    assert rec.token  # a non-empty per-process start token was recorded ...
    assert fields[2] == rec.token  # ... and persisted to the pidfile.
    assert fields[3] == 'run-xyz'


def test_write_pidfile_raises_when_setpgrp_fails(tmp_path, monkeypatch) -> None:
    """FAIL LOUD: if os.setpgrp() fails, write_pidfile must RAISE — never silently record the
    caller's inherited process group (a later reaper would then killpg a broader group)."""
    import pipeline.sim.simd.reaper as reaper_mod

    def boom() -> None:
        raise OSError('setpgrp denied')

    monkeypatch.setattr(reaper_mod.os, 'setpgrp', boom)
    with pytest.raises(OSError, match='setpgrp'):
        write_pidfile(tmp_path / 'pid', setpgrp=True)


def test_reaper_skips_reused_pid_with_mismatched_start_token(tmp_path) -> None:
    """PID reuse: the pidfile records a leader pid + start token; that pid is DEAD but its integer
    is now reused by an UNRELATED live process (a different start token). The reaper must NOT
    signal it — identity (pid AND start-token), not the bare integer, gates the kill."""
    pf = tmp_path / 'pid'
    # A live stand-in for "the unrelated process that reused the pid": our own process. Record its
    # pid but a DELIBERATELY WRONG start token, as if a now-dead predecessor had that pid earlier.
    reused_pid = os.getpid()
    pf.write_text(f'{reused_pid} {reused_pid} STALE_TOKEN_FROM_DEAD_LEADER run-old\n')

    signals: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        signals.append((pgid, sig))

    reaped = reap_orphan_tree(pf, grace_s=0.1, poll_s=0.01, killpg=fake_killpg, sleep=lambda _s: None)
    assert reaped is None, 'reaped a reused/mismatched pid — identity check failed'
    assert signals == [], f'signalled an unrelated reused pid: {signals}'


def test_engine_records_worker_sidecar_with_start_token(tmp_path, monkeypatch) -> None:
    """Sol HIGH 3 on the ENGINE path: the production worker-spawn callback must record each worker
    with its per-process START TOKEN (not a bare pgid). A bare record is admitted by group-number
    liveness alone, so a reused pgid held by an unrelated process could be signalled. Spy the
    record calls the engine makes and assert every one carries a non-``None`` token."""
    import pipeline.sim.simd.engine as engine_mod

    recorded: list[tuple[int, str | None]] = []
    real = engine_mod.record_worker_pgid

    def spy(pidfile: object, pgid: int, *, token: str | None = None) -> None:
        recorded.append((pgid, token))
        real(pidfile, pgid, token=token)

    monkeypatch.setattr(engine_mod, 'record_worker_pgid', spy)
    db = tmp_path / 'ops.duckdb'
    pidfile = tmp_path / 'simd.pid'
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), 1, fmt='commander')
    run_games_simd(
        tasks, worker_cmd=_cmd(), workers=1, stall_timeout_s=30.0, ops_db_path=db,
        pidfile_path=pidfile, join_timeout_s=30.0,
    )
    assert recorded, 'the engine recorded no worker sidecar entry'
    assert all(token is not None for _pgid, token in recorded), \
        f'a worker was recorded with a BARE pgid (no start token): {recorded}'


def test_reaper_skips_reused_worker_pgid_with_mismatched_token(tmp_path) -> None:
    """The other half of HIGH 3: a tokenized WORKER sidecar entry whose pgid is now held by an
    unrelated LIVE process (mismatched start token) is never signalled — identity, not the bare
    pgid, gates the kill. Extends the reviewer's untokened-reused-worker simulation to the tokened
    record the engine now writes."""
    pf = tmp_path / 'pid'
    dead = subprocess.Popen([sys.executable, '-c', ''])
    dead.wait()  # a dead leader so the reaper acts.
    pf.write_text(f'{dead.pid} {dead.pid}\n')
    # Record a worker pgid == our OWN live pid, but with a STALE token (as if a now-dead worker held
    # it and its pid was recycled by us). The reaper must refuse to signal this live stranger.
    record_worker_pgid(pf, os.getpid(), token='STALE_WORKER_TOKEN_FROM_DEAD_WORKER')

    signals: list[tuple[int, int]] = []
    reaped = reap_orphan_tree(
        pf, grace_s=0.1, poll_s=0.01, killpg=lambda g, s: signals.append((g, s)), sleep=lambda _s: None
    )
    assert signals == [], f'signalled a reused worker pgid despite a mismatched token: {signals}'
    assert reaped is None


def test_reaper_leaves_live_leader_with_matching_token_alone(tmp_path) -> None:
    """A pidfile whose leader pid is alive AND whose recorded start token still matches is a
    healthy live owner — not reaped (the flock owns concurrency)."""
    pf = tmp_path / 'pid'
    write_pidfile(pf, setpgrp=False, nonce='run-live')  # our own live process, real token.
    signals: list[int] = []
    reaped = reap_orphan_tree(
        pf, grace_s=0.1, poll_s=0.01, killpg=lambda _g, s: signals.append(s), sleep=lambda _s: None
    )
    assert reaped is None
    assert signals == []
    assert pf.exists()


def test_remove_worker_pgid_drops_entry_on_normal_retire(tmp_path) -> None:
    """A worker that retires normally is removed from the sidecar, so a later reaper never targets
    its (possibly reused) group."""
    from pipeline.sim.simd.reaper import _read_worker_pgids, remove_worker_pgid

    pf = tmp_path / 'pid'
    record_worker_pgid(pf, 4321)
    record_worker_pgid(pf, 8765)
    assert set(_read_worker_pgids(pf)) == {4321, 8765}
    remove_worker_pgid(pf, 4321)
    assert set(_read_worker_pgids(pf)) == {8765}


def test_concurrent_worker_pgid_mutations_lose_nothing(tmp_path) -> None:
    """Concurrent record/remove from many threads must not lose any survivor pgid.

    ``remove_worker_pgid`` does a read-modify-write of the sidecar; without serialization a churn
    thread can read the sidecar, then have a survivor thread append a pgid, then write its stale
    snapshot back — silently dropping the just-appended survivor (an orphan pgid that is then never
    reaped). This exercises heavy record/remove interleave and asserts every survivor survives.
    Pre-fix this fails flakily; the module-level lock makes it deterministic.
    """
    import threading

    from pipeline.sim.simd.reaper import _read_worker_pgids, record_worker_pgid, remove_worker_pgid

    for _ in range(40):
        pf = tmp_path / 'pid'
        _worker_sidecar = pf.with_suffix(pf.suffix + '.workers')
        _worker_sidecar.unlink(missing_ok=True)

        n_survivors = 16
        survivors = list(range(1000, 1000 + n_survivors))
        churn_pgids = list(range(9000, 9000 + 8))
        start = threading.Barrier(n_survivors + len(churn_pgids))

        def survive(g: int, pf: Path = pf, start: threading.Barrier = start) -> None:
            start.wait()
            record_worker_pgid(pf, g)

        def churn(g: int, pf: Path = pf, start: threading.Barrier = start) -> None:
            start.wait()
            for _ in range(20):
                record_worker_pgid(pf, g)
                remove_worker_pgid(pf, g)

        threads = [threading.Thread(target=survive, args=(g,)) for g in survivors]
        threads += [threading.Thread(target=churn, args=(g,)) for g in churn_pgids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        got = set(_read_worker_pgids(pf))
        assert set(survivors) <= got, f'lost survivor pgids: {set(survivors) - got}'


# ===================================================================================== #
# GATE 5 — staging GC correctness: dead xmage-worker dir GC'd, live worker dir survives.
# ===================================================================================== #


def test_gate5_staging_owner_pid_parses_two_word_prefix() -> None:
    # The bug: 'worker' was mis-read as the owner field. Now the FIRST digit field is the pid.
    assert runner_mod._staging_owner_pid('xmage-worker-4321-abcd') == 4321
    assert runner_mod._staging_owner_pid('xmage-solo-999-xy') == 999
    assert runner_mod._staging_owner_pid('run-12345-abc') == 12345
    assert runner_mod._staging_owner_pid('xmage-77-zz') == 77
    assert runner_mod._staging_owner_pid('run-legacy') is None


def test_gate5_dead_worker_dir_gcd_live_survives_under_zero_cutoff(tmp_path, monkeypatch) -> None:
    root = tmp_path / 'staging'
    root.mkdir()
    monkeypatch.setattr(runner_mod, 'staging_root', lambda: root)

    dead = subprocess.Popen([sys.executable, '-c', ''])
    dead.wait()  # reaped → dead.pid is gone.
    dead_dir = root / f'xmage-worker-{dead.pid}-abc'
    live_dir = root / f'xmage-worker-{os.getpid()}-def'  # our own live pid == a live worker.
    for d in (dead_dir, live_dir):
        d.mkdir()
        (d / 'db').write_text('x')

    # Aggressive MID-RUN GC: a 0s cutoff. Before the PID-parse fix this GC'd the live dir too
    # (its owner mis-parsed as unknown → fell to the age gate). Now the live owner is protected.
    reaped = runner_mod.reap_stale_staging(max_age_s=0.0)

    assert not dead_dir.exists(), 'dead-worker staging dir was not GC-ed'
    assert live_dir.exists(), 'LIVE worker staging dir was wrongly GC-ed (PID-parse regression)'
    assert reaped == 1
