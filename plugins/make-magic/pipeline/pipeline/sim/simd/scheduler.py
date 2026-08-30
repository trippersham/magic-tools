"""Phase A2.1/A2.2 — the fair round-robin scheduler + per-cell attempt-cap & quarantine.

The simd replacement for :class:`pipeline.sim.game_queue._Governor`'s subject-major deque. It
owns the pool callbacks (``next_task`` / ``on_result`` / ``requeue``) and:

* **A2.1 fair round-robin.** :meth:`SimdScheduler.next_task` hands the next slot to the runnable
  subject holding the FEWEST in-flight workers (ties broken by a rotating round-robin pointer),
  so no subject gets a 2nd worker while another runnable subject still has 0. A poison subject
  therefore cannot monopolize the pool — the exact wedge that stalled a real corpus for hours
  under the old subject-major order (which drained one subject to completion before the next).
* **A2.2 attempt-cap + persistent quarantine.** Each dispatch of a task increments its attempt
  counter (logged to :class:`~pipeline.sim.simd.ops_store.OpsStore`). A task that fails (worker
  death / stall-reap / game ``ERROR``) after ``attempt_cap`` dispatches is QUARANTINED — a
  terminal poison latch PERSISTED to ``ops.duckdb`` so a restart never re-attempts it. This
  retires the ad-hoc ``/tmp/queue_quarantine.json`` + the 113/120 heuristic + mtime inference.
* **A2.3 crash-safe.** Every completed game is committed to ``ops.duckdb`` the instant it drains;
  the scheduler seeds its done/quarantine/attempt state from the store on construction, so a
  reconstructed scheduler replays committed progress and loses ZERO completed games.

All three pool callbacks run on pool-owned threads; every mutation is under ``self._cond``.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline.sim.game_protocol import GameError, GameResult
from pipeline.sim.game_queue import bailout_reason
from pipeline.sim.game_tasks import cell_key
from pipeline.sim.simd.governor import Admission

if TYPE_CHECKING:
    from pipeline.sim.game_tasks import GameTask
    from pipeline.sim.monitor import ResourceMonitor
    from pipeline.sim.simd.governor import DiskGovernor
    from pipeline.sim.simd.ops_store import OpsStore

log = logging.getLogger('make_magic.sim.simd.scheduler')

__all__ = ('SimdRunResult', 'SimdScheduler')


def _winner_bucket(winner: str) -> str:
    w = winner.strip().lower()
    if w in ('a', 'subject', 'player_a', 'playera'):
        return 'a'
    if w in ('b', 'opponent', 'player_b', 'playerb'):
        return 'b'
    return 'draw'


@dataclass(frozen=True)
class SimdRunResult:
    """The outcome of a simd run — coverage DERIVED from the ops store's committed state.

    ``results`` is ``{task_id: GameResult}`` for every completed game; ``quarantined`` is the set
    of terminally poison-latched task_ids. ``cells`` maps every ``(subject, opponent, piloting)``
    cell to ``(valid_fills, needed)`` — ``valid_fills`` counts only gate-passed decisive/undecided
    games, so a cell filled by bailouts/quarantine can never look "complete". ``incomplete_cells``
    is every cell with ``valid_fills < needed``; ``quarantined_cells`` is the subset left short
    specifically by a quarantined (or non-decisive) task. ``complete`` is true iff no cell is
    incomplete.
    """

    results: dict[str, GameResult]
    quarantined: set[str]
    cells: dict[tuple[str, str, str], tuple[int, int]]
    incomplete_cells: list[tuple[str, str, str]]
    quarantined_cells: list[tuple[str, str, str]]
    complete: bool


class _Cell:
    __slots__ = ('needed', 'nondecisive', 'ok', 'quarantined', 'wins_a', 'wins_b')

    def __init__(self) -> None:
        self.needed = 0
        self.ok = 0
        self.nondecisive = 0
        self.quarantined = 0
        self.wins_a = 0
        self.wins_b = 0


class SimdScheduler:
    """Fair round-robin scheduling + attempt-cap/quarantine over an :class:`OpsStore`."""

    def __init__(
        self,
        tasks: list[GameTask],
        *,
        ops: OpsStore,
        attempt_cap: int = 2,
        monitor: ResourceMonitor | None = None,
        bailout_floor_ms: int = 0,
        cond_poll_s: float = 0.05,
        disk_governor: DiskGovernor | None = None,
    ) -> None:
        self._ops = ops
        self._attempt_cap = attempt_cap
        self._monitor = monitor
        self._bailout_floor_ms = bailout_floor_ms
        self._cond_poll_s = cond_poll_s
        #: A2.5 immediate disk-floor admission control. A HARD breach halts admission (drains the
        #: pool) after reaping in-flight workers — a resumable exit, not a paused-forever wedge.
        self._disk_governor = disk_governor
        self._disk_halted = False

        self._cond = threading.Condition()
        self._task_by_id: dict[str, GameTask] = {t.task_id: t for t in tasks}

        # Seed committed state from the ops store (the replay contract).
        seeded_results = ops.load_results()
        seeded_quarantine = ops.load_quarantine()
        seeded_attempts = ops.load_attempts()

        # Seed the live result map with the replayed games so the final view retains them
        # (crash-safe resume: a committed game is never re-run AND never dropped).
        self._results: dict[str, GameResult] = dict(seeded_results)
        self._done: set[str] = set(seeded_results)
        self._quarantined: set[str] = set(seeded_quarantine)
        self._attempts: dict[str, int] = defaultdict(int, seeded_attempts)

        # Per-subject runnable queues (subject order preserved from build order) + in-flight count.
        self._subject_order: list[str] = []
        self._pending: dict[str, deque[GameTask]] = {}
        self._in_flight: dict[str, int] = defaultdict(int)
        self._cells: dict[tuple[str, str, str], _Cell] = {}
        self._rr = 0  # round-robin rotation pointer into _subject_order.

        for t in tasks:
            subject, opp, pil = cell_key(t)
            key = (subject, opp, pil)
            if subject not in self._pending:
                self._subject_order.append(subject)
                self._pending[subject] = deque()
            self._cells.setdefault(key, _Cell())
            self._cells[key].needed += 1

        # Fold seeded results into the cell tallies, then enqueue only the still-runnable tasks.
        for t in tasks:
            tid = t.task_id
            if tid in seeded_quarantine:
                self._apply_quarantine_tally(tid)
            elif tid in seeded_results:
                self._apply_result_tally(tid, seeded_results[tid])
            else:
                self._pending[cell_key(t)[0]].append(t)

    # ------------------------------------------------------------------ #
    # Pool callbacks.
    # ------------------------------------------------------------------ #

    def next_task(self) -> GameTask | None:
        """Fair round-robin pick, or ``None`` on true drain (every task terminal)."""
        with self._cond:
            while True:
                if self._all_terminal_locked():
                    return None
                if self._disk_governor is not None:
                    decision = self._disk_governor.check()
                    if decision is Admission.HALT:
                        # HARD floor: in-flight already reaped by the governor. Stop admitting →
                        # the pool drains → run exits RESUMABLE (committed state is in ops.duckdb).
                        self._disk_halted = True
                        return None
                    if decision is Admission.PAUSE:
                        self._cond.wait(timeout=self._cond_poll_s)
                        continue
                if self._monitor is not None and self._monitor.poll_pause():
                    self._cond.wait(timeout=self._cond_poll_s)
                    continue
                subject = self._pick_subject_locked()
                if subject is None:
                    # Nothing runnable this instant but tasks are still in flight / being
                    # requeued — wait to be woken rather than retiring this worker.
                    self._cond.wait(timeout=self._cond_poll_s)
                    continue
                task = self._pending[subject].popleft()
                tid = task.task_id
                if tid in self._done or tid in self._quarantined:
                    continue  # stale entry raced terminal — skip without dispatching.
                self._in_flight[subject] += 1
                self._attempts[tid] += 1
                self._ops.record_attempt(tid, self._attempts[tid], 'dispatch')
                return task

    def on_result(self, msg: GameResult | GameError) -> None:
        """Record one completed/errored game. Dedups by ``task_id``; ERROR → retry/quarantine."""
        with self._cond:
            tid = msg.task_id
            subject = self._subject_of(tid)
            if tid in self._done or tid in self._quarantined:
                return  # dedup: duplicate or late result for an already-terminal task.
            self._in_flight[subject] = max(0, self._in_flight[subject] - 1)
            if isinstance(msg, GameError):
                self._ops.record_attempt(tid, self._attempts[tid], 'error', msg.exc)
                self._retry_or_quarantine_locked(tid, reason=f'game ERROR: {msg.exc}')
            else:
                self._done.add(tid)
                self._results[tid] = msg
                self._ops.record_result(msg)
                self._ops.record_attempt(tid, self._attempts[tid], 'result')
                self._apply_result_tally(tid, msg)
            self._cond.notify_all()

    def requeue(self, task: GameTask) -> None:
        """A dead/reaped worker's in-flight task returns — retry or quarantine past the cap."""
        with self._cond:
            tid = task.task_id
            subject = self._subject_of(tid)
            if tid in self._done or tid in self._quarantined:
                return
            self._in_flight[subject] = max(0, self._in_flight[subject] - 1)
            self._ops.record_attempt(tid, self._attempts[tid], 'requeue')
            self._retry_or_quarantine_locked(tid, reason='worker died / stall-reaped')
            self._cond.notify_all()

    # ------------------------------------------------------------------ #
    # Internals (caller holds self._cond).
    # ------------------------------------------------------------------ #

    def _pick_subject_locked(self) -> str | None:
        """The runnable subject with the fewest in-flight workers (round-robin tie-break)."""
        candidates = [s for s in self._subject_order if self._pending[s]]
        if not candidates:
            return None
        min_if = min(self._in_flight[s] for s in candidates)
        n = len(self._subject_order)
        for step in range(n):
            s = self._subject_order[(self._rr + step) % n]
            if self._pending[s] and self._in_flight[s] == min_if:
                self._rr = (self._subject_order.index(s) + 1) % n
                return s
        return candidates[0]  # unreachable (a min always exists), kept for total-function safety.

    def _retry_or_quarantine_locked(self, tid: str, *, reason: str) -> None:
        if self._attempts[tid] >= self._attempt_cap:
            self._quarantine_locked(tid, reason)
        else:
            self._pending[self._subject_of(tid)].append(self._task_by_id[tid])

    def _quarantine_locked(self, tid: str, reason: str) -> None:
        self._quarantined.add(tid)
        subject, opp, pil = cell_key(self._task_by_id[tid])
        log.warning(
            'task %s QUARANTINED after %d attempt(s) (cap=%d) — %s; cell (%s,%s,%s) left under-filled',
            tid, self._attempts[tid], self._attempt_cap, reason, subject, opp, pil,
        )
        self._ops.quarantine(tid, subject, opp, pil, attempts=self._attempts[tid], reason=reason)
        self._cells[(subject, opp, pil)].quarantined += 1

    def _apply_result_tally(self, tid: str, res: GameResult) -> None:
        subject, opp, pil = cell_key(self._task_by_id[tid])
        cell = self._cells[(subject, opp, pil)]
        if not res.decisive or bailout_reason(res, hard_floor_ms=self._bailout_floor_ms) is not None:
            cell.nondecisive += 1
            return
        cell.ok += 1
        bucket = _winner_bucket(res.winner)
        if bucket == 'a':
            cell.wins_a += 1
        elif bucket == 'b':
            cell.wins_b += 1

    def _apply_quarantine_tally(self, tid: str) -> None:
        subject, opp, pil = cell_key(self._task_by_id[tid])
        self._cells[(subject, opp, pil)].quarantined += 1

    def _subject_of(self, tid: str) -> str:
        return cell_key(self._task_by_id[tid])[0]

    def _all_terminal_locked(self) -> bool:
        return (len(self._done) + len(self._quarantined)) >= len(self._task_by_id)

    # ------------------------------------------------------------------ #
    # Result extraction.
    # ------------------------------------------------------------------ #

    def result(self) -> SimdRunResult:
        with self._cond:
            cells = {k: (c.ok, c.needed) for k, c in self._cells.items()}
            incomplete = [k for k, c in self._cells.items() if c.ok < c.needed]
            quarantined_cells = [
                k for k, c in self._cells.items() if c.ok < c.needed and (c.quarantined or c.nondecisive)
            ]
            return SimdRunResult(
                results=dict(self._results),
                quarantined=set(self._quarantined),
                cells=cells,
                incomplete_cells=incomplete,
                quarantined_cells=quarantined_cells,
                complete=not incomplete,
            )
