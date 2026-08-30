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

from typing import TYPE_CHECKING

from pipeline.sim.simd.ops_store import OpsStore
from pipeline.sim.simd.scheduler import SimdRunResult, SimdScheduler
from pipeline.sim.worker_pool import WorkerPool

if TYPE_CHECKING:
    import os
    from collections.abc import Callable

    from pipeline.sim.game_tasks import GameTask
    from pipeline.sim.monitor import ResourceMonitor

__all__ = ('SimdRunResult', 'run_games_simd')


def run_games_simd(
    tasks: list[GameTask],
    *,
    worker_cmd: list[str],
    ops_db_path: str | os.PathLike[str],
    workers: int | None = None,
    stall_timeout_s: float,
    attempt_cap: int = 2,
    monitor: ResourceMonitor | None = None,
    bailout_floor_ms: int = 0,
    env_for_worker: Callable[[int], dict[str, str]] | None = None,
    cond_poll_s: float = 0.05,
    join_timeout_s: float | None = None,
) -> SimdRunResult:
    """Run every game in ``tasks`` on a fair, crash-safe simd pool; return the coverage tally.

    ``ops_db_path`` is the run's dedicated ``ops.duckdb`` (NOT the shared lake db) — the crash-safe
    operational store. On construction the scheduler seeds its done/quarantine/attempt state from
    that file, so pointing a fresh call at an existing store RESUMES the run: committed games are
    never re-run and quarantined cells are never re-attempted. ``attempt_cap`` (K, default 2) is
    the per-task dispatch budget before a persistent quarantine latch. ``bailout_floor_ms`` arms
    the A1 plausibility gate (0 = disabled). ``env_for_worker`` injects per-worker env (the test
    fake-worker hooks; production driver env in A3).
    """
    if workers is None:
        from pipeline.sim.governor import derive_pool_size

        workers = derive_pool_size()

    with OpsStore(ops_db_path) as ops:
        ops.register_tasks(tasks)
        sched = SimdScheduler(
            tasks,
            ops=ops,
            attempt_cap=attempt_cap,
            monitor=monitor,
            bailout_floor_ms=bailout_floor_ms,
            cond_poll_s=cond_poll_s,
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
        )
        try:
            pool.start()
            pool.join(timeout=join_timeout_s)
        finally:
            pool.close()
            if monitor is not None:
                monitor.stop()
        return sched.result()
