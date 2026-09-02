"""Phase 1.3 — WorkerPool concurrency core, tested with a FAKE echo-worker (NO JVM).

Every test spawns real subprocesses (the ``fake_echo_worker.py`` script) and asserts
structural outcomes: all tasks drain, backpressure load-balances, a dead/stalled worker's
in-flight task is requeued + a replacement spawns, and no fake procs linger after close.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from pathlib import Path

from pipeline.sim import runner
from pipeline.sim.game_protocol import GameResult
from pipeline.sim.game_tasks import GameTask, SeatSpec
from pipeline.sim.worker_pool import WorkerPool

_FAKE = str(Path(__file__).parent / 'fake_echo_worker.py')


def _worker_cmd(env: dict[str, str] | None = None) -> list[str]:
    # env is applied by the pool via env_for_worker; here we just return the base argv.
    return [sys.executable, _FAKE]


def _task(i: int) -> GameTask:
    return GameTask(
        task_id=f's|o|driven|{i}',
        fmt='commander',
        seat_a=SeatSpec(deck_path='/d/s.dck', driver=None),
        seat_b=SeatSpec(deck_path='/d/o.dck', driver=None),
    )


class _Harness:
    """A thread-safe task source + result sink shared with the pool."""

    def __init__(self, tasks: list[GameTask]) -> None:
        self._pending = deque(tasks)
        self._lock = threading.Lock()
        self.results: list[GameResult] = []
        self.result_ids: list[str] = []  # every id surfaced, incl. requeue duplicates.

    def next_task(self) -> GameTask | None:
        with self._lock:
            return self._pending.popleft() if self._pending else None

    def requeue(self, task: GameTask) -> None:
        with self._lock:
            self._pending.append(task)

    def on_result(self, msg: object) -> None:
        with self._lock:
            if isinstance(msg, GameResult):
                self.results.append(msg)
            # every message carries a task_id (fidelity contract for Phase-3 dedup).
            self.result_ids.append(msg.task_id)

    def unique_result_ids(self) -> set[str]:
        with self._lock:
            return {r.task_id for r in self.results}


def _no_fake_procs_linger() -> None:
    """Assert the pool left no registered procs and no fake_echo_worker children alive."""
    assert not runner._ACTIVE_PROCS, 'pool left procs registered in _ACTIVE_PROCS'


def test_drains_all_tasks_no_orphans() -> None:
    n = 12
    h = _Harness([_task(i) for i in range(n)])
    pool = WorkerPool(
        _worker_cmd(),
        workers=4,
        next_task=h.next_task,
        on_result=h.on_result,
        requeue=h.requeue,
        stall_timeout_s=30.0,
    )
    pool.start()
    pool.join(timeout=30.0)
    pool.close()

    assert h.unique_result_ids() == {f's|o|driven|{i}' for i in range(n)}
    _no_fake_procs_linger()


def test_backpressure_slow_worker_does_not_block_queue() -> None:
    """One fake sleeps a long time on its first task; the other fakes must drain the rest."""
    n = 10
    h = _Harness([_task(i) for i in range(n)])
    pool = WorkerPool(
        _worker_cmd(),
        workers=3,
        next_task=h.next_task,
        on_result=h.on_result,
        requeue=h.requeue,
        stall_timeout_s=60.0,
        # exactly one worker is slow-on-first-task; the others are fast.
        env_for_worker=lambda idx: {'FAKE_SLOW_FIRST': '1', 'FAKE_SLOW_MS': '4000'} if idx == 0 else {},
    )
    pool.start()

    # Before the slow worker could possibly finish (4s), the fast workers should have
    # drained everything except the one task the slow worker is holding.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if len(h.unique_result_ids()) >= n - 1:
            break
        time.sleep(0.05)
    assert len(h.unique_result_ids()) >= n - 1, 'slow worker blocked the queue'

    pool.join(timeout=30.0)
    pool.close()
    assert h.unique_result_ids() == {f's|o|driven|{i}' for i in range(n)}
    _no_fake_procs_linger()


def test_requeue_on_death_respawns_and_completes(tmp_path: Path) -> None:
    """A fake that dies mid-first-task → its in-flight task is requeued + a replacement
    spawns → the task eventually completes on a live worker."""
    n = 4
    h = _Harness([_task(i) for i in range(n)])
    marker = str(tmp_path / 'died.marker')
    pool = WorkerPool(
        _worker_cmd(),
        workers=2,
        next_task=h.next_task,
        on_result=h.on_result,
        requeue=h.requeue,
        stall_timeout_s=30.0,
        # One-shot: worker 0 dies once; its replacement (same idx env) runs clean via the marker.
        env_for_worker=lambda idx: (
            {'FAKE_DIE_ON_TASK': '1', 'FAKE_FAULT_ONCE_FILE': marker} if idx == 0 else {}
        ),
    )
    pool.start()
    pool.join(timeout=30.0)
    pool.close()

    # Every task completes despite worker 0 dying on its first task.
    assert h.unique_result_ids() == {f's|o|driven|{i}' for i in range(n)}
    _no_fake_procs_linger()


def test_stall_reap_requeues_and_completes(tmp_path: Path) -> None:
    """A fake that goes silent (no heartbeat/RESULT) past stall_timeout_s is reaped,
    its task requeued, and completed on another worker."""
    n = 4
    h = _Harness([_task(i) for i in range(n)])
    marker = str(tmp_path / 'silent.marker')
    pool = WorkerPool(
        _worker_cmd(),
        workers=2,
        next_task=h.next_task,
        on_result=h.on_result,
        requeue=h.requeue,
        stall_timeout_s=1.0,
        poll_s=0.1,
        # One-shot: worker 0 goes silent once; its replacement runs clean via the marker.
        env_for_worker=lambda idx: (
            {'FAKE_SILENT': '1', 'FAKE_FAULT_ONCE_FILE': marker} if idx == 0 else {}
        ),
    )
    pool.start()
    pool.join(timeout=30.0)
    pool.close()

    assert h.unique_result_ids() == {f's|o|driven|{i}' for i in range(n)}
    _no_fake_procs_linger()


def test_reap_mid_stream_no_reader_exception(tmp_path: Path) -> None:
    """MAJOR-1: reaping a worker whose reader thread is actively mid-stream must not raise
    inside the reader (single owner per pipe — no cross-thread communicate()). The reaped
    task is requeued + completed on a replacement, and no proc lingers."""
    n = 4
    h = _Harness([_task(i) for i in range(n)])
    marker = str(tmp_path / 'hb_silent.marker')

    reader_errors: list[tuple[str, BaseException]] = []
    prev_hook = threading.excepthook

    def _hook(args: threading.ExceptHookArgs) -> None:
        thread = args.thread
        if thread is not None and thread.name.endswith('-reader') and args.exc_value is not None:
            reader_errors.append((thread.name, args.exc_value))
        prev_hook(args)

    threading.excepthook = _hook
    try:
        pool = WorkerPool(
            _worker_cmd(),
            workers=2,
            next_task=h.next_task,
            on_result=h.on_result,
            requeue=h.requeue,
            stall_timeout_s=1.0,
            poll_s=0.1,
            # Worker 0 emits a heartbeat (reader now mid-stream) then goes silent → the
            # watchdog reaps it WHILE its reader is blocked reading stdout.
            env_for_worker=lambda idx: (
                {'FAKE_SILENT_AFTER_HEARTBEAT': '1', 'FAKE_FAULT_ONCE_FILE': marker} if idx == 0 else {}
            ),
        )
        pool.start()
        pool.join(timeout=30.0)
        pool.close()
    finally:
        threading.excepthook = prev_hook

    assert not reader_errors, f'reader thread raised on mid-stream reap: {reader_errors}'
    assert h.unique_result_ids() == {f's|o|driven|{i}' for i in range(n)}
    _no_fake_procs_linger()


def test_death_between_tasks_respawns_and_drains(tmp_path: Path) -> None:
    """MINOR-2: a worker that dies BETWEEN tasks (no in-flight task) must still trigger a
    respawn, else the single worker vanishes and the remaining tasks never drain."""
    n = 3
    h = _Harness([_task(i) for i in range(n)])
    marker = str(tmp_path / 'between.marker')

    spawn_calls: list[int] = []

    def _env(idx: int) -> dict[str, str]:
        spawn_calls.append(idx)  # one call per spawn (initial + every respawn).
        return {'FAKE_DIE_AFTER_RESULT': '1', 'FAKE_FAULT_ONCE_FILE': marker}

    pool = WorkerPool(
        _worker_cmd(),
        workers=1,  # a single worker: no respawn ⇒ pool shrinks to 0 ⇒ tasks stall.
        next_task=h.next_task,
        on_result=h.on_result,
        requeue=h.requeue,
        stall_timeout_s=30.0,
        env_for_worker=_env,
    )
    pool.start()
    pool.join(timeout=30.0)
    pool.close()

    # The only way all tasks drain with workers=1 is a replacement spawning after the
    # between-tasks death — proven both by completeness and by the extra spawn call.
    assert h.unique_result_ids() == {f's|o|driven|{i}' for i in range(n)}
    assert len(spawn_calls) >= 2, 'no replacement spawned after death between tasks'
    _no_fake_procs_linger()


def _protocol_failure_case(env_flag: str, tmp_path: Path, poll_s: float = 0.1) -> None:
    """Shared harness: worker 0 commits one protocol violation on its first task; the pool must
    kill+requeue (never silent-continue), and every task still drains on a replacement."""
    n = 4
    h = _Harness([_task(i) for i in range(n)])
    marker = str(tmp_path / f'{env_flag}.marker')
    pool = WorkerPool(
        _worker_cmd(),
        workers=2,
        next_task=h.next_task,
        on_result=h.on_result,
        requeue=h.requeue,
        stall_timeout_s=30.0,
        poll_s=poll_s,
        env_for_worker=lambda idx: ({env_flag: '1', 'FAKE_FAULT_ONCE_FILE': marker} if idx == 0 else {}),
    )
    pool.start()
    assert pool.join(timeout=30.0), f'{env_flag}: pool wedged — a lost in-flight task never requeued'
    pool.close()
    assert h.unique_result_ids() == {f's|o|driven|{i}' for i in range(n)}, env_flag
    _no_fake_procs_linger()


def test_malformed_result_in_flight_requeues_not_silent_continue(tmp_path: Path) -> None:
    """Sol HIGH 1: an unparseable line while a task is IN FLIGHT is a worker/task failure — the
    worker is killed, its task requeued through the cap, and the run does NOT wedge (the old code
    silently `continue`d, stranding the in-flight task forever)."""
    _protocol_failure_case('FAKE_BAD_RESULT', tmp_path)


def test_ready_while_task_in_flight_is_a_violation(tmp_path: Path) -> None:
    """A READY emitted while a task is still in flight is a protocol violation → kill + requeue,
    never a silent `in_flight` overwrite that loses the task's terminal."""
    _protocol_failure_case('FAKE_READY_WHILE_INFLIGHT', tmp_path)


def test_result_id_mismatch_is_a_violation(tmp_path: Path) -> None:
    """A RESULT whose id does not match the worker's assigned task must NOT clear in_flight —
    the mismatch is a failure that requeues the real task."""
    _protocol_failure_case('FAKE_WRONG_ID_RESULT', tmp_path)


def test_on_retire_called_for_normally_drained_workers() -> None:
    """A worker that drains cleanly fires on_retire (so the caller can drop it from the orphan
    sidecar); a reused pid must never linger as a reaper target."""
    n = 4
    h = _Harness([_task(i) for i in range(n)])
    retired: list[int] = []
    pool = WorkerPool(
        _worker_cmd(),
        workers=2,
        next_task=h.next_task,
        on_result=h.on_result,
        requeue=h.requeue,
        stall_timeout_s=30.0,
        on_retire=retired.append,
    )
    pool.start()
    pool.join(timeout=30.0)
    pool.close()
    assert len(retired) >= 2, f'clean-drained workers did not fire on_retire: {retired}'
    _no_fake_procs_linger()


def test_task_id_on_every_result() -> None:
    """Dedup is Phase 3; here we only guarantee task_id fidelity on every surfaced result."""
    n = 6
    h = _Harness([_task(i) for i in range(n)])
    pool = WorkerPool(
        _worker_cmd(),
        workers=3,
        next_task=h.next_task,
        on_result=h.on_result,
        requeue=h.requeue,
        stall_timeout_s=30.0,
    )
    pool.start()
    pool.join(timeout=30.0)
    pool.close()

    assert all(rid for rid in h.result_ids)  # no empty/None ids
    assert set(h.result_ids) == {f's|o|driven|{i}' for i in range(n)}
    _no_fake_procs_linger()


def test_close_leaves_zero_live_workers_deterministically() -> None:
    """Regression (orphan-at-drain race): close() must not return while a worker process is still
    dying. A SIGKILL'd worker takes a beat to terminate; close() now proc.wait()s each worker, so
    zero remain alive on return. Stressed over several iterations to catch the intermittent race."""
    import subprocess as _sp

    for _ in range(6):
        h = _Harness([_task(i) for i in range(6)])
        pool = WorkerPool(
            _worker_cmd(),
            workers=3,
            next_task=h.next_task,
            on_result=h.on_result,
            requeue=h.requeue,
            stall_timeout_s=30.0,
        )
        pool.start()
        pool.join(timeout=30.0)
        pids = [w.proc.pid for w in pool._workers]
        pool.close(grace_s=5.0)
        # Every worker process must be dead the instant close() returns — no lingering JVM/fake.
        for pid in pids:
            alive = _sp.run(['kill', '-0', str(pid)], capture_output=True).returncode == 0
            assert not alive, f'worker pid {pid} still alive after close() — orphan-at-drain race'
        _no_fake_procs_linger()
