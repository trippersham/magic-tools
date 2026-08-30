"""Phase 1.3 — the WorkerPool: W long-lived worker subprocesses + READY-driven pull.

The concurrency core of the persistent-worker queue, de-risked entirely in Python with a
fake echo-worker (no JVM). The governor's task-ordering / aggregation is Phase 3; the pool
does only the transport: given an injected ``next_task()`` source it feeds workers on their
``READY`` signal, collects results via ``on_result``, and survives worker faults by
**requeuing the dead worker's in-flight task** (via ``requeue``) and respawning a replacement.

Process discipline mirrors the existing harness (see :mod:`pipeline.sim.runner` /
:func:`pipeline.sim.engines.xmage._run_with_watchdog`):

* every worker is launched with ``start_new_session=True`` and registered in
  :data:`runner._ACTIVE_PROCS` so the existing emergency reaper covers it;
* a per-worker stall clock (reset on every ``GAME``/``RESULT``) reaps a silent worker
  own-lineage via :func:`runner._kill_process_group` — which flows into the requeue+respawn
  path exactly like an unexpected death.

Injected callbacks (all called from pool-owned threads; each must be thread-safe):

* ``next_task() -> GameTask | None`` — the next task to run, or ``None`` when drained.
* ``on_result(msg: GameResult | GameError) -> None`` — one completed/failed game
  (both carry ``task_id`` — the fidelity the Phase-3 governor dedups on).
* ``requeue(task: GameTask) -> None`` — return a dead/reaped worker's in-flight task.

Single-owner pipe discipline (MAJOR-1): the watchdog/``close`` reaper only *signals* the
group (:meth:`WorkerPool._signal_kill_group` — ``killpg`` without any pipe read); the
worker's OWN reader thread then hits EOF naturally and performs the ``proc.wait()`` +
cleanup in its ``finally``. Exactly one thread ever touches a given worker's stdout, so
there is no cross-thread ``communicate()`` racing the reader (that is why the reaper here
does NOT call :func:`runner._kill_process_group`, which reads the pipe).

Phase-3 concerns (out of scope here — the governor owns them):

* **No retry cap / poison-pill termination.** ``WorkerPool`` requeues a dead worker's
  in-flight task unconditionally, so a task that deterministically kills *every* worker
  loops forever. The governor (Phase 3) MUST implement the retry-counter (design §7, K=2)
  inside its ``requeue`` callback and stop re-offering a task that has burned its budget.
* **MINOR-1: watchdog reap is a TOCTOU / at-least-once seam.** A worker may finish a game
  and the watchdog may still reap it just before the RESULT is observed, so a task can run
  twice; Phase-3 dedup (on ``task_id``) absorbs the duplicate.
* **MINOR-4: lenient RESULT parsing.** :func:`game_protocol.parse_line` fills RESULT
  defaults leniently; Phase-3 aggregation may want stricter field validation.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from pipeline.sim import runner
from pipeline.sim.game_protocol import (
    GameError,
    GameResult,
    Heartbeat,
    ProtocolError,
    Ready,
    encode_task,
    parse_line,
)
from pipeline.sim.game_tasks import GameTask

if TYPE_CHECKING:
    from collections.abc import Callable

    from pipeline.sim.simd.circuit_breaker import CrashLoopBreaker as _CrashLoopBreaker

log = logging.getLogger('make_magic.sim.worker_pool')

__all__ = ('WorkerPool',)


class _Worker:
    """One live worker subprocess + its reader thread + stall bookkeeping."""

    __slots__ = (
        'idx', 'in_flight', 'last_progress', 'lock', 'proc', 'reader', 'retiring',
        'saw_ready', 'spawned_at',
    )

    def __init__(self, idx: int, proc: subprocess.Popen[str]) -> None:
        self.idx = idx
        self.proc = proc
        self.reader: threading.Thread | None = None
        self.in_flight: GameTask | None = None
        self.last_progress: float = time.monotonic()
        self.retiring = False  # got a `None` task → draining out cleanly (EOF is expected).
        self.saw_ready = False  # reached its first READY → a mid-game death is a retry, not a boot fail.
        self.spawned_at: float = time.monotonic()  # for the pre-READY boot deadline.
        self.lock = threading.Lock()


class WorkerPool:
    """Owns W worker subprocesses and does READY-driven pull scheduling with fault recovery."""

    def __init__(
        self,
        worker_cmd: list[str],
        *,
        workers: int,
        next_task: Callable[[], GameTask | None],
        on_result: Callable[[GameResult | GameError], None],
        requeue: Callable[[GameTask], None],
        stall_timeout_s: float,
        poll_s: float = 1.0,
        env_for_worker: Callable[[int], dict[str, str]] | None = None,
        breaker: _CrashLoopBreaker | None = None,
        boot_deadline_s: float | None = None,
        boot_backoff_base_s: float = 0.0,
        boot_backoff_cap_s: float = 10.0,
    ) -> None:
        self._cmd = list(worker_cmd)
        self._n = workers
        self._next_task = next_task
        self._on_result = on_result
        self._requeue = requeue
        self._stall_timeout_s = stall_timeout_s
        self._poll_s = poll_s
        self._env_for_worker = env_for_worker
        #: A2.4 crash-loop breaker (pre-READY death → boot fail). ``None`` = legacy behavior
        #: (respawn every death unconditionally — the old game_queue path A3 will retire).
        self._breaker = breaker
        #: A worker that has not emitted READY within this many seconds is presumed a hung boot
        #: and reaped (→ a pre-READY death that feeds the breaker). ``None`` disables the deadline.
        self._boot_deadline_s = boot_deadline_s
        self._boot_backoff_base_s = boot_backoff_base_s
        self._boot_backoff_cap_s = boot_backoff_cap_s

        self._closing = threading.Event()
        self._state_lock = threading.Lock()
        self._workers: list[_Worker] = []
        self._live = 0  # workers not yet retired (drained or gone-for-good).
        self._retired = threading.Condition(self._state_lock)
        self._watchdog: threading.Thread | None = None
        #: Set when the breaker trips: the pool stops respawning and unblocks join(); the engine
        #: raises a BootFailure. Distinct from a clean drain (which also unblocks join).
        self.boot_failed = threading.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Spawn the initial W workers + the stall watchdog."""
        with self._state_lock:
            for idx in range(self._n):
                self._spawn_locked(idx)
        self._watchdog = threading.Thread(target=self._watch, name='worker-pool-watchdog', daemon=True)
        self._watchdog.start()

    def join(self, timeout: float | None = None) -> bool:
        """Block until every worker has retired (the task source is drained).

        Returns ``True`` if all workers retired within ``timeout``, else ``False``.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._state_lock:
            while self._live > 0:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._retired.wait(timeout=remaining)
            return True

    def close(self, grace_s: float = 5.0) -> None:
        """Stop feeding, let in-flight games finish (bounded), then reap + join. No orphans."""
        self._closing.set()
        deadline = time.monotonic() + grace_s
        # Give in-flight workers a bounded window to finish and exit on their own.
        with self._state_lock:
            while self._live > 0 and time.monotonic() < deadline:
                self._retired.wait(timeout=max(0.0, deadline - time.monotonic()))
            workers = list(self._workers)
        # Reap anything still alive (own-lineage), then join reader threads.
        # Signal-only: the worker's own reader thread owns draining + wait() (MAJOR-1).
        for w in workers:
            if w.proc.poll() is None:
                self._signal_kill_group(w.proc)
        for w in workers:
            if w.reader is not None:
                w.reader.join(timeout=grace_s)
        # Deterministically reap each worker PROCESS before returning — a SIGKILL'd JVM takes a
        # beat to actually terminate, and the reader thread's drain may return before the process
        # is dead. proc.wait() only reaps the exit status (it does NOT read the pipe, so it is safe
        # alongside the reader-owns-drain discipline); this closes the "orphan at drain" race where
        # close() returned while a worker JVM was still dying. A worker that refuses to die after a
        # re-kill is left to the ResourceMonitor reaper (logged), never silently leaked.
        for w in workers:
            try:
                w.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self._signal_kill_group(w.proc)  # re-kill the stubborn group, then wait once more.
                try:
                    w.proc.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    log.warning('worker pid %s survived close() — left to the reaper', w.proc.pid)
        if self._watchdog is not None:
            self._watchdog.join(timeout=self._poll_s * 2)

    # ------------------------------------------------------------------ #
    # Spawning
    # ------------------------------------------------------------------ #
    def _spawn_locked(self, idx: int) -> None:
        """Launch a worker for slot ``idx`` and start its reader. Caller holds ``_state_lock``."""
        env = dict(os.environ)
        if self._env_for_worker is not None:
            env.update(self._env_for_worker(idx))
        proc = subprocess.Popen(
            self._cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        runner._register_active(proc)
        worker = _Worker(idx, proc)
        worker.reader = threading.Thread(
            target=self._read_loop, args=(worker,), name=f'worker-{idx}-reader', daemon=True
        )
        self._workers.append(worker)
        self._live += 1
        worker.reader.start()

    # ------------------------------------------------------------------ #
    # Per-worker reader
    # ------------------------------------------------------------------ #
    def _read_loop(self, worker: _Worker) -> None:
        assert worker.proc.stdout is not None
        try:
            for raw in worker.proc.stdout:
                line = raw.rstrip('\n')
                if not line.strip():
                    continue
                try:
                    msg = parse_line(line)
                except ProtocolError:
                    continue  # ignore stray/non-protocol output (JVM noise, banners).
                self._handle(worker, msg)
        finally:
            worker.proc.wait()
            self._on_worker_exit(worker)

    def _handle(self, worker: _Worker, msg: object) -> None:
        if isinstance(msg, Ready):
            if not worker.saw_ready:
                worker.saw_ready = True  # first READY: this worker booted OK.
                if self._breaker is not None:
                    self._breaker.record_ready()
            self._feed(worker)
        elif isinstance(msg, Heartbeat):
            with worker.lock:
                worker.last_progress = time.monotonic()
        elif isinstance(msg, (GameResult, GameError)):
            with worker.lock:
                worker.in_flight = None
                worker.last_progress = time.monotonic()
            self._on_result(msg)

    def _feed(self, worker: _Worker) -> None:
        """On READY: pull the next task and write it, or close stdin to drain the worker out."""
        if self._closing.is_set():
            self._begin_retire(worker)
            return
        task = self._next_task()
        if task is None:
            self._begin_retire(worker)
            return
        with worker.lock:
            worker.in_flight = task
            worker.last_progress = time.monotonic()
        try:
            assert worker.proc.stdin is not None
            worker.proc.stdin.write(encode_task(task) + '\n')
            worker.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            # Worker died between READY and our write; the reader EOF path requeues in_flight.
            pass

    def _begin_retire(self, worker: _Worker) -> None:
        with worker.lock:
            worker.retiring = True
        try:
            if worker.proc.stdin is not None:
                worker.proc.stdin.close()  # EOF → the fake/real worker exits its loop.
        except (BrokenPipeError, ValueError, OSError):
            pass

    def _on_worker_exit(self, worker: _Worker) -> None:
        """Reader saw EOF/exit: requeue+respawn on ANY unexpected death; else retire cleanly."""
        runner._unregister_active(worker.proc)
        with worker.lock:
            in_flight = worker.in_flight
            worker.in_flight = None
            retiring = worker.retiring
            saw_ready = worker.saw_ready
        # Unexpected = not a deliberate retire (close()) and not a clean drain (None task).
        # Respawn on ANY such death — whether or not a task was in flight — so a crash
        # BETWEEN tasks (in_flight is None) does not silently shrink the pool (MINOR-2).
        if not retiring and not self._closing.is_set():
            # A2.4: a PRE-READY death (the worker died before ever signalling it was up) is a
            # DETERMINISTIC boot failure, not a transient mid-game fault. Feed it to the
            # crash-loop breaker; if it trips, STOP respawning (bounding total spawns — and thus
            # staging dirs) and unblock join so the engine can raise a BootFailure.
            if self._breaker is not None and not saw_ready:
                if self._breaker.record_pre_ready_death():
                    self.boot_failed.set()
                    self._closing.set()
                    with self._state_lock:
                        if worker in self._workers:
                            self._workers.remove(worker)
                        self._live -= 1
                        self._retired.notify_all()
                    return
                self._boot_backoff(self._breaker.total_pre_ready_deaths)
            if in_flight is not None:
                self._requeue(in_flight)  # only requeue when there was actually a task.
            with self._state_lock:
                self._workers.remove(worker)
                self._live -= 1  # retire the dead worker ...
                self._spawn_locked(worker.idx)  # ... its replacement re-adds one (net steady).
            return
        # Clean drain or shutdown: this worker is gone for good.
        with self._state_lock:
            if worker in self._workers:
                self._workers.remove(worker)
            self._live -= 1
            self._retired.notify_all()

    # ------------------------------------------------------------------ #
    # Stall watchdog
    # ------------------------------------------------------------------ #
    def _watch(self) -> None:
        """Reap any worker silent past ``stall_timeout_s`` while holding a task (own-lineage)."""
        while not self._closing.is_set():
            now = time.monotonic()
            with self._state_lock:
                workers = list(self._workers)
            for w in workers:
                with w.lock:
                    stalled = (
                        w.in_flight is not None
                        and not w.retiring
                        and (now - w.last_progress) > self._stall_timeout_s
                    )
                    # A2.4 boot deadline: a worker that has not reached READY within the deadline
                    # is a hung boot. Reap it (→ a pre-READY death that feeds the breaker) so a
                    # worker that spawns but never comes up cannot silently hold a pool slot.
                    boot_hung = (
                        self._boot_deadline_s is not None
                        and not w.saw_ready
                        and not w.retiring
                        and w.in_flight is None
                        and (now - w.spawned_at) > self._boot_deadline_s
                    )
                if (stalled or boot_hung) and w.proc.poll() is None:
                    # Signal-only kill → the reader thread hits EOF with in_flight set,
                    # wait()s the proc itself, then requeues + respawns (MAJOR-1: single
                    # owner per pipe — no cross-thread communicate() racing the reader).
                    self._signal_kill_group(w.proc)
            time.sleep(self._poll_s)

    def _boot_backoff(self, consecutive_deaths: int) -> None:
        """Exponential backoff between boot respawns so a crash-loop does not spin hot.

        ``base * 2**(deaths-1)`` capped at ``boot_backoff_cap_s``; a zero base (the test default)
        is a no-op so the fast loop stays fast. Runs on the dying reader thread, before its
        replacement is spawned.
        """
        base = self._boot_backoff_base_s
        if base <= 0 or consecutive_deaths <= 0:
            return
        delay = min(self._boot_backoff_cap_s, base * (2 ** (consecutive_deaths - 1)))
        time.sleep(delay)

    @staticmethod
    def _signal_kill_group(proc: subprocess.Popen[str]) -> None:
        """SIGKILL the worker's process group WITHOUT reading its pipe.

        Deliberately unlike :func:`runner._kill_process_group` (which calls
        ``communicate()`` and would read stdout from this thread while the worker's
        reader thread is still blocked on the same ``BufferedReader``). Here we only
        signal; the worker's own reader thread owns draining to EOF and ``proc.wait()``.
        """
        if hasattr(os, 'killpg'):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.kill()  # belt-and-braces for the direct child; still no pipe read.
