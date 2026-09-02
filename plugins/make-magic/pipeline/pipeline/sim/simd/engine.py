"""Phase A2 — the simd engine: wire the :class:`OpsStore` + :class:`SimdScheduler` to the
persistent :class:`~pipeline.sim.worker_pool.WorkerPool` (the performance core).

:func:`run_games_simd` is the new live entry point (A3 routes production through it and retires
``run_games``). It owns the ``ops.duckdb`` operational store for the run, drives a fair
round-robin scheduler over a pool of persistent workers, and resumes from committed state on
restart — so ONE broken game (or a whole poison subject) can no longer gum the batch: it fails
fast, latches into a persisted quarantine, and the pool keeps draining every other subject.

The bailout gate (A1) is opt-in via ``bailout_floor_ms`` (0 disables it; production passes
``BAILOUT_HARD_FLOOR_MS``) — the new path can arm it exactly like ``run_games``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from pipeline.sim.simd.ops_store import DEFAULT_RUN_ID, OpsStore
from pipeline.sim.simd.preflight import BootFailure
from pipeline.sim.simd.reaper import (
    SingletonLock,
    reap_orphan_tree,
    record_worker_pgid,
    write_pidfile,
)
from pipeline.sim.simd.scheduler import SimdRunResult, SimdScheduler
from pipeline.sim.worker_pool import WorkerPool

if TYPE_CHECKING:
    import os
    from collections.abc import Callable

    from pipeline.sim.game_tasks import GameTask
    from pipeline.sim.monitor import ResourceMonitor
    from pipeline.sim.simd.circuit_breaker import CrashLoopBreaker
    from pipeline.sim.simd.governor import DiskGovernor

__all__ = ('BootFailure', 'SimdRunResult', 'run_games_simd')


def _config_fingerprint(fmt: str, attempt_cap: int, topup_cap: int, bailout_floor_ms: int) -> str:
    """A stable, short hash of the run's science config — the default run_id. Two invocations that
    agree on these knobs (regardless of task-set size) share a run and resume each other; a changed
    knob yields a distinct run scoped to its own rows in a shared ops file."""
    import hashlib
    import json

    blob = json.dumps(
        {'fmt': fmt, 'attempt_cap': attempt_cap, 'topup_cap': topup_cap, 'bailout_floor_ms': bailout_floor_ms},
        sort_keys=True,
        separators=(',', ':'),
    )
    return 'run-' + hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def run_games_simd(
    tasks: list[GameTask],
    *,
    worker_cmd: list[str],
    ops_db_path: str | os.PathLike[str],
    workers: int | None = None,
    stall_timeout_s: float,
    attempt_cap: int = 2,
    topup_cap: int = 2,
    monitor: ResourceMonitor | None = None,
    bailout_floor_ms: int = 0,
    env_for_worker: Callable[[int], dict[str, str]] | None = None,
    cond_poll_s: float = 0.05,
    join_timeout_s: float | None = None,
    preflight: Callable[[], None] | None = None,
    breaker: CrashLoopBreaker | None = None,
    boot_deadline_s: float | None = None,
    boot_backoff_base_s: float = 0.0,
    disk_governor: DiskGovernor | None = None,
    singleton_lock_path: str | os.PathLike[str] | None = None,
    pidfile_path: str | os.PathLike[str] | None = None,
    own_pgroup: bool = False,
    run_id: str | None = None,
) -> SimdRunResult:
    """Run every game in ``tasks`` on a fair, crash-safe simd pool; return the coverage tally.

    ``ops_db_path`` is the run's dedicated ``ops.duckdb`` (NOT the shared lake db) — the crash-safe
    operational store. On construction the scheduler seeds its done/quarantine/attempt state from
    that file, so pointing a fresh call at an existing store RESUMES the run: committed games are
    never re-run and quarantined cells are never re-attempted. ``attempt_cap`` (K, default 2) is
    the per-task dispatch budget before a persistent quarantine latch. ``topup_cap`` (default 2) is
    the per-cell top-up multiplier: a non-decisive game (bailout/timeout) enqueues a fresh
    REPLACEMENT task for its cell so completeness ("``needed`` DECISIVE games per cell") is
    reachable, bounded at ``topup_cap * needed`` replacements so an all-non-decisive cell still
    terminates (flagged exhausted). ``bailout_floor_ms`` arms
    the A1 plausibility gate (0 = disabled). ``env_for_worker`` injects per-worker env (the test
    fake-worker hooks; production driver env in A3).

    A2b RESILIENCE (all opt-in — the legacy A2a call is byte-identical with them unset):

    * ``preflight`` runs ONCE before ANY staging/spawn (e.g. version-gate Java). It raises
      :class:`BootFailure` on a deterministic config failure so the run aborts with ZERO workers
      and ZERO staging dirs — never the silent crash-loop that flooded staging.
    * ``breaker`` + ``boot_deadline_s`` + ``boot_backoff_base_s`` arm the pool's crash-loop
      circuit breaker: ``N`` pre-``READY`` worker deaths trip it, and the run raises
      :class:`BootFailure` instead of respawning unboundedly (so staging stays bounded).
    * ``disk_governor`` applies immediate soft/hard disk floors to admission; a HARD breach reaps
      in-flight workers and drains the pool for a RESUMABLE exit.
    * ``singleton_lock_path`` takes an ``flock`` so a 2nd concurrent simd launch refuses;
      ``pidfile_path`` reaps a crashed predecessor's orphan JVM tree BEFORE this run starts and
      records this run's own pid/pgid. ``own_pgroup`` makes this process a group leader first (so
      a future reaper can killpg its whole tree) — production sets it; library/test callers leave
      it False so the caller's own process group is untouched.
    """
    if workers is None:
        from pipeline.sim.governor import derive_pool_size

        workers = derive_pool_size()

    # Run scoping: an ops.duckdb file may hold many runs, each isolated by ``run_id``. A caller that
    # keeps distinct science in one file passes an explicit ``run_id``; the default single-run file
    # uses :data:`DEFAULT_RUN_ID`. The run's SCIENCE CONFIG (format + attempt/top-up caps + bailout
    # floor) is fingerprinted into the ``simd_runs`` manifest row: resuming the SAME run_id with a
    # CHANGED config is refused loudly by :class:`OpsStore` (mixed-universe resume), while an expanded
    # task universe under an unchanged config resumes cleanly. Per-task fingerprint binding (in
    # ``register_tasks``) independently refuses a changed deck/driver under a reused task id.
    fmt = tasks[0].fmt if tasks else 'commander'
    config_fingerprint = _config_fingerprint(fmt, attempt_cap, topup_cap, bailout_floor_ms)
    resolved_run_id = run_id if run_id is not None else DEFAULT_RUN_ID

    # 1. PREFLIGHT — fail LOUD before any staging/spawn (zero workers, zero staging on failure).
    if preflight is not None:
        preflight()

    with contextlib.ExitStack() as stack:
        # 2. SINGLETON — a 2nd concurrent simd launch refuses (raises AlreadyRunning).
        if singleton_lock_path is not None:
            stack.enter_context(SingletonLock(singleton_lock_path))
        # 3. ORPHAN REAP — a crashed predecessor's whole JVM tree is killed before we start, then
        #    we record our own pid/pgid so OUR successor can reap us if we crash.
        on_spawn: Callable[[int], None] | None = None
        if pidfile_path is not None:
            reap_orphan_tree(pidfile_path)
            write_pidfile(pidfile_path, setpgrp=own_pgroup)
            # Workers spawn in their OWN sessions (start_new_session), outside our group — record
            # each worker pgid so our successor's reaper can killpg the whole worker tree if we
            # crash, instead of leaking JVMs until stdin-EOF self-exit (F-2).
            on_spawn = lambda pid: record_worker_pgid(pidfile_path, pid)  # noqa: E731

        with OpsStore(ops_db_path, run_id=resolved_run_id, config_fingerprint=config_fingerprint) as ops:
            ops.register_tasks(tasks)
            sched = SimdScheduler(
                tasks,
                ops=ops,
                attempt_cap=attempt_cap,
                topup_cap=topup_cap,
                monitor=monitor,
                bailout_floor_ms=bailout_floor_ms,
                cond_poll_s=cond_poll_s,
                disk_governor=disk_governor,
            )

            # A full restart: everything already terminal in the store — nothing left to run.
            if sched._all_terminal_locked():
                return sched.result()

            if monitor is not None:
                monitor.start()
            pool = WorkerPool(
                worker_cmd,
                workers=workers,
                next_task=sched.next_task,
                on_result=sched.on_result,
                requeue=sched.requeue,
                stall_timeout_s=stall_timeout_s,
                env_for_worker=env_for_worker,
                breaker=breaker,
                boot_deadline_s=boot_deadline_s,
                boot_backoff_base_s=boot_backoff_base_s,
                on_spawn=on_spawn,
            )
            try:
                pool.start()
                pool.join(timeout=join_timeout_s)
            finally:
                pool.close()
                if monitor is not None:
                    monitor.stop()

            # The crash-loop breaker tripped: abort LOUD rather than report a half-empty tally as
            # if the run merely under-filled — the config can never make progress.
            if pool.boot_failed.is_set() or (breaker is not None and breaker.tripped):
                raise BootFailure(
                    f'crash-loop circuit breaker tripped after {breaker.total_pre_ready_deaths if breaker else "?"} '
                    'pre-READY worker deaths — workers are dying before they come up (broken '
                    'classpath / incompatible runtime / unsatisfiable config). Aborting instead '
                    'of respawning into an unbounded staging flood.'
                )
            return sched.result()
