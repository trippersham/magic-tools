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

from pipeline.sim.aggregate import Validity, classify_validity, is_concede, is_fast_game
from pipeline.sim.game_protocol import GameError, GameResult
from pipeline.sim.game_tasks import GameTask, cell_key, starter_for_index
from pipeline.sim.simd.governor import Admission

if TYPE_CHECKING:
    from pipeline.sim.monitor import ResourceMonitor
    from pipeline.sim.simd.governor import DiskGovernor
    from pipeline.sim.simd.ops_store import OpsStore

log = logging.getLogger('make_magic.sim.simd.scheduler')

__all__ = ('SimdRunResult', 'SimdScheduler')


#: The game-index field prefix that marks a dynamically-created top-up replacement task
#: (``subject|opponent|piloting|topup-<n>``). Originals carry a plain integer index.
_TOPUP_PREFIX = 'topup-'


def _is_topup_id(task_id: str) -> bool:
    """True iff ``task_id`` is a dynamically-created top-up (its game-index field is ``topup-<n>``).

    Top-ups are minted at runtime (never by ``build_game_tasks``), so a stored top-up absent from a
    resume's supplied universe is legitimately reconstructed; a stored ORIGINAL absent from it is a
    universe mismatch (Sol BLOCKER 1) — the caller refuses rather than guessing it is a top-up."""
    parts = task_id.split('|')
    return len(parts) == 4 and parts[3].startswith(_TOPUP_PREFIX)


def _starter_of_id(task_id: str) -> str:
    """The deterministic first-turn seat for a task_id (original OR top-up).

    An original's 4th field is the integer game index; a top-up's is ``topup-<ordinal>``. Both use
    the SAME parity rule (:func:`starter_for_index`) so a reconstructed top-up on resume rebinds to
    the identical starter it was created with — even index/ordinal → A, odd → B. A malformed field
    falls back to the legacy 'A' (subject on the play), never raising in the resume path."""
    field = task_id.split('|')[3]
    ordinal = field[len(_TOPUP_PREFIX) :] if field.startswith(_TOPUP_PREFIX) else field
    try:
        return starter_for_index(int(ordinal))
    except ValueError:
        return 'A'


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
    #: Cells left short specifically because their bounded top-up budget was exhausted (every
    #: replacement game also resolved non-decisive). These are flagged DISTINCTLY — they are
    #: incomplete-by-exhaustion, never silently folded into ``complete``.
    exhausted_cells: list[tuple[str, str, str]]
    #: Cells that saw at least one INVALID game (a decisive claim with no legal terminal cause).
    #: Should be near-empty; a nonzero list is a loud data-integrity alarm surfaced in coverage.
    invalid_cells: list[tuple[str, str, str]]
    #: Total DECISIVE games under the fast-game floor (informational triage flag; no longer excluded).
    fast_games: int
    #: Total decisive concessions (``end_cause=concede``) — a rules-legal loss counted in W/L like any
    #: decisive cause, surfaced so concede-heavy matchups are visible (a data-quality signal).
    concede_games: int
    #: The ORIGINAL registered task universe (every task_id the scheduler was built with, plus any
    #: resumed top-ups) — passed into post-hoc aggregation so never-run tasks surface as incomplete
    #: coverage instead of silently vanishing from a results-only universe (Sol BLOCKER 1).
    task_ids: list[str]
    #: True IFF every cell reached ``ok >= needed`` — a genuine completeness predicate with NO
    #: exhaustion exception. An exhausted-by-cap cell is terminal-but-incomplete (surfaced in
    #: ``exhausted_cells``), so ``complete`` stays False and partial science can never be blessed.
    complete: bool


class _Cell:
    __slots__ = (
        'concede',
        'fast',
        'invalid',
        'needed',
        'nondecisive',
        'ok',
        'quarantined',
        'topups',
        'wins_a',
        'wins_b',
    )

    def __init__(self) -> None:
        self.needed = 0
        self.ok = 0
        self.nondecisive = 0
        #: Subset of nondecisive: a decisive claim with NO legal terminal cause (macro-game-over) or
        #: an ``unknown`` cause — INVALID data. Topped up like a non-decisive game AND flagged loudly.
        self.invalid = 0
        #: Informational: DECISIVE games under the fast-game floor (a genuinely fast kill, no longer
        #: excluded by the retired ms proxy).
        self.fast = 0
        #: Subset of ok: decisive concessions (``end_cause=concede``) — a rules-legal loss counted in
        #: W/L, surfaced separately so concede-heavy matchups are visible (a data-quality signal).
        self.concede = 0
        self.quarantined = 0
        #: Top-up replacement tasks EVER created for this cell (originals excluded). Bounds the
        #: per-cell top-up budget and seeds the next replacement's game-index (``topup-<n>``).
        self.topups = 0
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
        topup_cap: int = 2,
        monitor: ResourceMonitor | None = None,
        bailout_floor_ms: int = 0,
        cond_poll_s: float = 0.05,
        disk_governor: DiskGovernor | None = None,
    ) -> None:
        self._ops = ops
        self._attempt_cap = attempt_cap
        #: Per-cell top-up multiplier: a cell needing N decisive games gets at most ``topup_cap * N``
        #: replacement tasks before it is declared exhausted. Default 2x keeps a pathological
        #: all-non-decisive cell bounded (terminates) while giving realistic bailout rates ample
        #: headroom to reach completeness.
        self._topup_cap = topup_cap
        self._monitor = monitor
        self._bailout_floor_ms = bailout_floor_ms
        self._cond_poll_s = cond_poll_s
        #: A2.5 immediate disk-floor admission control. A HARD breach halts admission; the ENGINE
        #: observes :attr:`disk_halted` and reaps in-flight workers through the pool's own
        #: signal-kill drain (M2) — a resumable exit, not a paused-forever wedge.
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
        #: Per-cell seat template (any original task's seats) — the blueprint a top-up clones so a
        #: replacement game runs the identical (subject, opponent, arm) matchup.
        self._cell_template: dict[tuple[str, str, str], GameTask] = {}
        self._rr = 0  # round-robin rotation pointer into _subject_order.

        for t in tasks:
            subject, opp, pil = cell_key(t)
            key = (subject, opp, pil)
            if subject not in self._pending:
                self._subject_order.append(subject)
                self._pending[subject] = deque()
            if key not in self._cells:
                self._cells[key] = _Cell()
                self._cell_template[key] = t
            self._cells[key].needed += 1

        # Resume: reconstruct prior top-up tasks committed to ops.duckdb but ABSENT from the passed
        # task universe (top-ups are created dynamically, never by build_game_tasks). Each is rebuilt
        # from its cell's seat template so a still-pending replacement can re-run identically.
        #
        # A stored task absent from the supplied universe is reconstructed ONLY if it is an explicit
        # top-up (``…|topup-<n>`` id). A stored ORIGINAL absent from the universe is NEVER guessed to
        # be a top-up (Sol BLOCKER 1): originals are DETERMINISTIC, so an absent one means the task
        # universe SHRANK/CHANGED under a reused run_id (e.g. ``--games`` reduced, field changed) —
        # refuse loudly instead of silently retaining stale rows and reporting the run complete. In
        # production every universe change already yields a fresh content-complete run_id (so those
        # stale rows are invisible under the new id); this guard is the store-level backstop.
        for tid, (subject, opp, pil, fmt) in ops.load_tasks().items():
            if tid in self._task_by_id:
                continue  # an original — already registered.
            if not _is_topup_id(tid):
                msg = (
                    f'stored task {tid!r} is absent from the supplied task universe and is NOT a '
                    'top-up: an original task vanished, so the universe shrank/changed under a reused '
                    'run_id (games reduced / field changed / deck removed) — mixed-universe resume '
                    'refused. Use a fresh run_id (or ops file) for a changed task universe.'
                )
                raise ValueError(msg)
            key = (subject, opp, pil)
            template = self._cell_template.get(key)
            if template is None:
                continue  # a top-up for a cell not in this run's universe — skip (out of scope).
            self._task_by_id[tid] = GameTask(
                task_id=tid,
                fmt=fmt,
                seat_a=template.seat_a,
                seat_b=template.seat_b,
                starter=_starter_of_id(tid),
            )
            self._cells[key].topups += 1

        # Fold seeded results into the cell tallies, then enqueue only the still-runnable tasks
        # (originals AND reconstructed top-ups).
        for tid, t in self._task_by_id.items():
            if tid in seeded_quarantine:
                self._apply_quarantine_tally(tid)
            elif tid in seeded_results:
                self._apply_result_tally(tid, seeded_results[tid])
            else:
                self._pending[cell_key(t)[0]].append(t)

        # Reconcile top-ups over the seeded state: any cell left short by committed non-decisive
        # games (endurance resume) enqueues exactly the missing replacements now — bounded, and
        # never double-counting top-ups already present from a prior run.
        for key in self._cells:
            self._ensure_topups_locked(key)

    @property
    def disk_halted(self) -> bool:
        """True once a disk HARD-floor breach has latched admission off (engine reaps + exits)."""
        with self._cond:
            return self._disk_halted

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
                # PER-GAME durable drain (A2.3 parity for the transcript record): parse + persist
                # this game's transcript/features into ops.duckdb the instant its result commits,
                # then delete the transcript file. Non-fatal — a drain hiccup must never break the
                # run (the file is KEPT on failure so a later re-drain can recover it).
                try:
                    self._ops.drain_game(msg)
                except Exception:  # pragma: no cover - drain is defensive/total; belt-and-suspenders.
                    log.warning('per-game transcript drain failed for %s (non-fatal)', tid, exc_info=True)
                self._apply_result_tally(tid, msg)
                # A non-decisive commit consumed a slot without incrementing ``ok`` — enqueue a
                # bounded replacement so the cell can still reach completeness (no-op for a
                # decisive result, which already advanced ``ok``).
                self._ensure_topups_locked(cell_key(self._task_by_id[tid]))
            self._cond.notify_all()

    def requeue(self, task: GameTask) -> None:
        """A dead/reaped worker's in-flight task returns — retry or quarantine past the cap.

        M3: a task whose worker was reaped BECAUSE the disk HARD floor halted the run is a
        NON-ATTEMPT — it must not be charged toward the attempt cap nor quarantined (the task is
        healthy; only the disk failed). It is left NON-TERMINAL (dropped from in-flight, not
        re-enqueued into this dying run) so a later resume re-runs it cleanly. The normal pool
        shutdown path (``close()`` → ``_closing`` set) does not requeue at all; this guard covers
        the race where a worker dies on its own between the HALT latch and the engine's reap.
        """
        with self._cond:
            tid = task.task_id
            subject = self._subject_of(tid)
            if tid in self._done or tid in self._quarantined:
                return
            self._in_flight[subject] = max(0, self._in_flight[subject] - 1)
            if self._disk_halted:
                # Disk-HALT reap: not the task's fault. Don't debit the attempt budget / quarantine;
                # leave it non-terminal for a resumable re-run.
                self._cond.notify_all()
                return
            self._ops.record_attempt(tid, self._attempts[tid], 'requeue')
            self._retry_or_quarantine_locked(tid, reason='worker died / stall-reaped')
            self._cond.notify_all()

    def on_halt_reap(self, task: GameTask) -> None:
        """A disk-HALT reap of an IN-FLIGHT task: persistently NON-CHARGING (Sol HIGH 4 / Fable M3).

        The engine drives this (via :meth:`WorkerPool.close`) for every still-in-flight task when a
        disk HARD-floor breach halted the run. Unlike :meth:`requeue`, this is reached even on the
        normal ``close()`` shutdown path (where ``_closing`` suppresses requeue entirely), so it is
        the ONLY place the persisted dispatch charge is undone. The task's ``dispatch`` attempt row
        is DELETED from the ops log so a resume restores its FULL attempt budget — the task is
        healthy; only the disk failed. The task is left NON-TERMINAL (not requeued into this dying
        run, not quarantined) for a clean re-run on resume. Idempotent against an already-terminal
        task (a game that completed during the close grace window)."""
        with self._cond:
            tid = task.task_id
            if tid in self._done or tid in self._quarantined:
                return  # completed (or latched) during the close grace window — nothing to undo.
            subject = self._subject_of(tid)
            self._in_flight[subject] = max(0, self._in_flight[subject] - 1)
            attempt = self._attempts.get(tid, 0)
            if attempt > 0:
                self._ops.neutralize_dispatch(tid, attempt)
                self._attempts[tid] = attempt - 1
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
            tid,
            self._attempts[tid],
            self._attempt_cap,
            reason,
            subject,
            opp,
            pil,
        )
        self._ops.quarantine(tid, subject, opp, pil, attempts=self._attempts[tid], reason=reason)
        self._cells[(subject, opp, pil)].quarantined += 1

    def _ensure_topups_locked(self, key: tuple[str, str, str]) -> None:
        """Enqueue any missing top-up replacements for one cell (idempotent, bounded by the cap).

        The cell wants one replacement per committed non-decisive game (each consumed a slot
        without a decisive result), capped at ``topup_cap * needed``. Quarantined failures are the
        attempt-cap path's concern and are NOT compensated here. Idempotent: it only tops the cell
        up to ``min(nondecisive, cap)`` replacements, so a resume never double-enqueues a top-up a
        prior run already created.
        """
        cell = self._cells[key]
        if cell.ok >= cell.needed:
            return  # already complete — no replacement needed.
        cap = self._topup_cap * cell.needed
        wanted = min(cell.nondecisive, cap)
        while cell.topups < wanted:
            self._enqueue_topup_locked(key)

    def _enqueue_topup_locked(self, key: tuple[str, str, str]) -> None:
        """Create + enqueue one replacement task for ``key`` (registered to ops for resume)."""
        subject, opp, pil = key
        cell = self._cells[key]
        template = self._cell_template[key]
        new_id = '|'.join((subject, opp, pil, f'topup-{cell.topups}'))
        task = GameTask(
            task_id=new_id,
            fmt=template.fmt,
            seat_a=template.seat_a,
            seat_b=template.seat_b,
            # Top-ups continue the cell's alternation by TOP-UP ORDINAL parity (cell.topups): even
            # ordinal → A, odd → B, matching the original games' index-parity schedule.
            starter=starter_for_index(cell.topups),
        )
        cell.topups += 1
        self._task_by_id[new_id] = task
        self._pending[subject].append(task)
        self._ops.register_tasks([task])
        log.debug(
            'cell (%s,%s,%s) topped up → %s (topups=%d, cap=%d)',
            subject,
            opp,
            pil,
            new_id,
            cell.topups,
            self._topup_cap * cell.needed,
        )

    def _apply_result_tally(self, tid: str, res: GameResult) -> None:
        subject, opp, pil = cell_key(self._task_by_id[tid])
        cell = self._cells[(subject, opp, pil)]
        validity = classify_validity(res, hard_floor_ms=self._bailout_floor_ms)
        if validity is not Validity.DECISIVE:
            # timeout / legacy-bailout / INVALID — excluded from W/L and topped up (the caller's
            # _ensure_topups_locked fires off this same nondecisive increment). INVALID is flagged.
            cell.nondecisive += 1
            if validity is Validity.INVALID:
                cell.invalid += 1
                subj, o, p = cell_key(self._task_by_id[tid])
                log.warning(
                    'task %s INVALID (end_cause=%r, winner=%r) — decisive claim with no legal '
                    'terminal cause; excluded + topped up; cell (%s,%s,%s) flagged',
                    tid,
                    res.end_cause,
                    res.winner,
                    subj,
                    o,
                    p,
                )
            return
        cell.ok += 1
        if is_fast_game(res):
            cell.fast += 1
        if is_concede(res):
            cell.concede += 1
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
        # Set INCLUSION over the CURRENT-run task universe — never a cardinality compare. A stale
        # done/quarantine id from another universe can no longer make the run look drained; the run
        # is terminal iff every live task id is itself done or quarantined.
        return all(tid in self._done or tid in self._quarantined for tid in self._task_by_id)

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
            # A cell is exhausted-by-cap iff it is still short AND its top-up budget is spent — every
            # replacement it was allowed also failed to resolve decisively. Flagged distinctly so the
            # caller can tell "terminated by top-up cap" from "still running", but it is STILL
            # incomplete: it does NOT get subtracted from the completeness predicate (Sol BLOCKER 1 —
            # ``complete`` means all cells ok>=needed, no exhaustion exception).
            exhausted = [
                k for k, c in self._cells.items() if c.ok < c.needed and c.topups >= self._topup_cap * c.needed
            ]
            invalid_cells = [k for k, c in self._cells.items() if c.invalid]
            fast_games = sum(c.fast for c in self._cells.values())
            concede_games = sum(c.concede for c in self._cells.values())
            return SimdRunResult(
                results=dict(self._results),
                quarantined=set(self._quarantined),
                cells=cells,
                incomplete_cells=incomplete,
                quarantined_cells=quarantined_cells,
                exhausted_cells=exhausted,
                invalid_cells=invalid_cells,
                fast_games=fast_games,
                concede_games=concede_games,
                task_ids=list(self._task_by_id),
                complete=not incomplete,
            )
