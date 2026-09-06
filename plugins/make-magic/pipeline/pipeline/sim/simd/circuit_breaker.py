"""Phase A2.4 — a pool-wide crash-loop circuit breaker keyed on PRE-``READY`` worker death.

Two worker-death classes must be told apart:

* a **mid-game death** (the worker booted, emitted ``READY``, took a task, then died) — a
  TRANSIENT fault → the task is requeued/retried and the worker respawned (the pool's normal
  fault recovery);
* a **pre-``READY`` death** (the worker died before it ever signalled it was up) — a
  DETERMINISTIC boot failure (broken classpath, missing/incompatible JVM, a config the run can
  never satisfy). Respawning into it is an unbounded crash-loop that spawns a fresh worker — and,
  in production, stages a fresh card-DB copy — on every death. That is exactly what flooded 33k
  staging dirs to 99 % disk.

:class:`CrashLoopBreaker` counts pre-``READY`` deaths in a sliding time window; ``N`` within ``T``
seconds trips it. Once tripped the pool STOPS respawning and the run aborts with a
:class:`~pipeline.sim.simd.preflight.BootFailure`, so the total number of workers ever spawned
(and therefore staging dirs created) is bounded by ``initial_pool + N``.

Thread-safe (the pool calls it from worker-owned threads); the clock is injectable for tests.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ('CrashLoopBreaker',)


class CrashLoopBreaker:
    """Trip after ``max_pre_ready_deaths`` pre-``READY`` worker deaths within ``window_s``."""

    def __init__(
        self,
        *,
        max_pre_ready_deaths: int = 5,
        window_s: float = 120.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_pre_ready_deaths < 1:
            msg = f'max_pre_ready_deaths must be >= 1 (got {max_pre_ready_deaths})'
            raise ValueError(msg)
        self._max = max_pre_ready_deaths
        self._window_s = window_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._deaths: deque[float] = deque()
        self._tripped = False
        self._total_pre_ready_deaths = 0

    def record_ready(self) -> None:
        """A worker reached ``READY`` — a successful boot. Clears the pending pre-``READY`` window
        so an EARLIER flurry of boot failures that the run recovered from cannot later trip the
        breaker on an unrelated mid-run blip."""
        with self._lock:
            self._deaths.clear()

    def record_pre_ready_death(self) -> bool:
        """Record one pre-``READY`` worker death; return ``True`` iff the breaker is now tripped."""
        with self._lock:
            now = self._clock()
            self._total_pre_ready_deaths += 1
            self._deaths.append(now)
            cutoff = now - self._window_s
            while self._deaths and self._deaths[0] < cutoff:
                self._deaths.popleft()
            if len(self._deaths) >= self._max:
                self._tripped = True
            return self._tripped

    @property
    def tripped(self) -> bool:
        """Whether the crash-loop threshold has been crossed (terminal — never resets)."""
        with self._lock:
            return self._tripped

    @property
    def total_pre_ready_deaths(self) -> int:
        """Count of pre-``READY`` deaths seen over the run's whole life (for diagnostics)."""
        with self._lock:
            return self._total_pre_ready_deaths
