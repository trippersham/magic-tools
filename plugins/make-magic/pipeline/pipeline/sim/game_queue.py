"""Phase 3 — the governor scheduler over the persistent-worker pool (pure Python, no JVM).

:func:`run_games` is the governor: it owns a thread-safe, subject-major task deque and the
per-cell completeness registry, drives a Phase-1 :class:`~pipeline.sim.worker_pool.WorkerPool`
via injected ``next_task`` / ``on_result`` / ``requeue`` callbacks, and folds the drained
per-game results into the *existing* driver-study aggregation (per-opponent Wilson-CI win-rate
deltas + the archetype/wincon bucket table).

Three responsibilities the pool deliberately leaves to the governor (design §5/§7):

* **Dedup by ``task_id`` (carry-forward).** The pool guarantees ``task_id`` fidelity but does
  NOT dedup: an at-least-once requeue race (a reaped worker whose RESULT was already observed)
  can deliver the same ``task_id`` twice. :meth:`_Governor.on_result` records a ``task_id`` only
  the FIRST time it completes; a later duplicate — or any result for an already-terminal task —
  is dropped, never double-counted.
* **Retry cap ``K`` (carry-forward MAJOR-2).** The pool requeues a dead worker's in-flight task
  unconditionally, so a poison task that kills every worker would loop forever. Each requeue (a
  worker death OR a game ``ERROR``) increments a per-``task_id`` counter; after ``retry_cap``
  requeues the task is marked **FAILED + logged (WARNING)** and NOT re-offered. A failed task is
  terminal — its cell stays under-filled and is surfaced in the result, never silently 100 %.
* **Monitor pause.** When a :class:`~pipeline.sim.monitor.ResourceMonitor` is supplied,
  ``next_task`` stops handing out work while ``poll_pause()`` is true (block-and-repoll) so the
  governor stops feeding under RAM/disk pressure; workers idle rather than retire.

**Restart** is per-game: a persisted done-set (task_ids, optionally with their
:class:`~pipeline.sim.game_protocol.GameResult`) filters the task list on start, so a re-run
resumes mid-subject. Persistence is keyed by ``task_id`` → a re-run overwrites its own slot,
never double-counting. :func:`save_done_set` / :func:`load_done_set` provide the sidecar I/O.

Aggregation REUSES the existing helpers verbatim — no Wilson/bucket math is reinvented here:
:func:`pipeline.sim.driver_compare._rates` (→ :func:`pipeline.sim.core.wilson_ci`),
:class:`~pipeline.sim.driver_compare.OpponentComparison` / ``PilotingComparison``, and
:func:`pipeline.sim.driver_run.aggregate_buckets` / ``write_bucket_table`` / ``DeckDelta``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline.sim.driver_compare import OpponentComparison, PilotingComparison, _rates
from pipeline.sim.game_protocol import GameError, GameResult
from pipeline.sim.game_tasks import cell_key
from pipeline.sim.worker_pool import WorkerPool

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from pipeline.sim.game_tasks import GameTask
    from pipeline.sim.monitor import ResourceMonitor

log = logging.getLogger('make_magic.sim.game_queue')

__all__ = (
    'RunGamesResult',
    'load_done_set',
    'run_games',
    'save_done_set',
)

#: A goldfish own-turn lens is not run on the queue (gauntlet) path — the ``PilotingComparison``
#: own-turn fields carry the harness's "never killed / not measured" sentinel + a warning.
_NO_GOLDFISH = -1.0


# --------------------------------------------------------------------------- #
# Result type.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunGamesResult:
    """The outcome of a :func:`run_games` governor run.

    ``done_results`` maps every completed ``task_id`` to its (first, deduped) ``GameResult`` —
    persist it via :func:`save_done_set` for a per-game restart. ``failed`` is the set of
    ``task_id``\\s that burned their retry budget (terminal, cell under-filled). ``comparisons``
    is the per-subject :class:`~pipeline.sim.driver_compare.PilotingComparison` built as each
    subject completed (incremental harvest). ``incomplete_cells`` lists any
    ``(subject, opponent, piloting)`` cell that did not reach its needed game count (only ever
    non-empty when tasks failed). ``complete`` is true iff every task ended done (none failed).
    """

    done_results: dict[str, GameResult]
    failed: set[str]
    comparisons: dict[str, PilotingComparison]
    cells: dict[tuple[str, str, str], tuple[int, int]]  # cell -> (done, needed)
    incomplete_cells: list[tuple[str, str, str]]
    complete: bool


# --------------------------------------------------------------------------- #
# Done-set persistence (per-game restart sidecar) — reuses the ledger atomic-write idiom.
# --------------------------------------------------------------------------- #


def save_done_set(path: str | os.PathLike[str], results: Mapping[str, GameResult]) -> None:
    """Atomically write the done-set sidecar: one JSON ``GameResult`` per line, keyed by id.

    Written temp-then-``os.replace`` so a crash mid-write never corrupts the sidecar (the same
    discipline as :class:`pipeline.sim.driver_batch.Ledger`)."""
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for tid, r in results.items():
        lines.append(
            json.dumps(
                {
                    'id': tid,
                    'winner': r.winner,
                    'kill_turn': r.kill_turn,
                    'ms': r.ms,
                    'markers': list(r.markers),
                    'log': r.log_path,
                    'reason': r.reason,
                },
                sort_keys=True,
            )
        )
    payload = '\n'.join(lines) + ('\n' if lines else '')
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='.doneset.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(payload)
        os.replace(tmp, p)
    except BaseException:
        from pathlib import Path as _P

        _P(tmp).unlink(missing_ok=True)
        raise


def load_done_set(path: str | os.PathLike[str]) -> dict[str, GameResult]:
    """Load a done-set sidecar → ``{task_id: GameResult}``. Missing file ⇒ empty (fresh run)."""
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, GameResult] = {}
    for line in p.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        body = json.loads(line)
        tid = str(body['id'])
        out[tid] = GameResult(
            task_id=tid,
            winner=str(body.get('winner', '')),
            kill_turn=body.get('kill_turn'),
            ms=int(body.get('ms', 0)),
            markers=list(body.get('markers', [])),
            log_path=body.get('log'),
            reason=body.get('reason'),
        )
    return out


# --------------------------------------------------------------------------- #
# The governor.
# --------------------------------------------------------------------------- #


def _winner_bucket(winner: str) -> str:
    """Normalise a ``GameResult.winner`` to ``'a'`` (subject) / ``'b'`` (opponent) / ``'draw'``.

    Draws are excluded from the Wilson denominator (the decided-games rule the existing
    aggregation uses); anything unrecognised — including ``'none'`` (a non-decisive
    wall-clock-timeout game, ``GameResult.reason == 'timeout'``) — is treated as a draw
    (no win credited), so an engine-defective livelock never folds into the W/L rate."""
    w = winner.strip().lower()
    if w in ('a', 'subject', 'player_a', 'playera'):
        return 'a'
    if w in ('b', 'opponent', 'player_b', 'playerb'):
        return 'b'
    return 'draw'


class _Cell:
    """One ``(subject, opponent, piloting)`` cell's live tally."""

    __slots__ = ('done', 'needed', 'wins_a', 'wins_b')

    def __init__(self, needed: int) -> None:
        self.needed = needed
        self.done = 0  # terminal tasks (done + failed) — for completeness.
        self.wins_a = 0
        self.wins_b = 0


class _Governor:
    """The thread-safe scheduling + tallying core behind :func:`run_games`."""

    def __init__(
        self,
        tasks: list[GameTask],
        *,
        retry_cap: int,
        monitor: ResourceMonitor | None,
        done_set: Mapping[str, GameResult] | set[str] | frozenset[str] | None,
        on_subject_complete: Callable[[str, PilotingComparison], None] | None,
        cond_poll_s: float,
        done_set_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._retry_cap = retry_cap
        self._monitor = monitor
        self._on_subject_complete = on_subject_complete
        self._cond_poll_s = cond_poll_s
        # Incremental durability: when set, the accumulated done-set is atomically re-saved after
        # each subject crosses terminal (see _advance_subject_locked / _flush_checkpoint_if_pending)
        # so a crash mid-run keeps the finished subjects instead of losing all progress.
        self._done_set_path = done_set_path
        # A consistent snapshot of self._results taken under the lock at a subject-complete point,
        # written to disk OUTSIDE the lock by the pool-callback thread that staged it.
        self._checkpoint_snapshot: dict[str, GameResult] | None = None

        self._cond = threading.Condition()
        self._task_by_id: dict[str, GameTask] = {t.task_id: t for t in tasks}
        self._retries: dict[str, int] = defaultdict(int)
        self._done: set[str] = set()
        self._failed: set[str] = set()
        self._results: dict[str, GameResult] = {}
        self._comparisons: dict[str, PilotingComparison] = {}
        # Subjects whose completeness threshold was crossed WHILE seeding the done-set (a
        # fully-seeded subject on restart). Aggregated in-place but their on_subject_complete
        # callback is deferred to run_games (fired once, outside the lock, after construction).
        self._seeded_complete: list[str] = []

        # Completeness registry: cells keyed by (subject, opp, piloting).
        self._cells: dict[tuple[str, str, str], _Cell] = {}
        # Subject bookkeeping (order preserved from build_game_tasks; subject-major).
        self._subject_order: list[str] = []
        self._subject_opponents: dict[str, list[str]] = defaultdict(list)
        self._subject_total: dict[str, int] = defaultdict(int)
        self._subject_terminal: dict[str, int] = defaultdict(int)
        self._subject_of: dict[str, str] = {}  # task_id -> subject

        for t in tasks:
            subject, opp, piloting = cell_key(t)
            key = (subject, opp, piloting)
            if subject not in self._subject_total:
                self._subject_order.append(subject)
            if key not in self._cells:
                self._cells[key] = _Cell(0)
            self._cells[key].needed += 1
            if opp not in self._subject_opponents[subject]:
                self._subject_opponents[subject].append(opp)
            self._subject_total[subject] += 1
            self._subject_of[t.task_id] = subject

        # Restart: pre-seed the done-set. A mapping carries the winners (correct aggregation on
        # resume); a bare set only marks completion (winners unknown → excluded from tallies).
        seed_results: Mapping[str, GameResult] | None = done_set if isinstance(done_set, dict) else None
        seed_ids: Iterable[str] = done_set.keys() if isinstance(done_set, dict) else (done_set or ())
        self._pending: deque[GameTask] = deque()
        for t in tasks:
            tid = t.task_id
            if tid in seed_ids:
                res = seed_results.get(tid) if seed_results is not None else None
                self._mark_done(tid, res, seeded=True)
            else:
                self._pending.append(t)

    # -- pool callbacks ---------------------------------------------------- #

    def next_task(self) -> GameTask | None:
        """Pop the next task (subject-major), block while paused, or ``None`` when all terminal.

        Returns ``None`` ONLY on true drain (every task terminal) so the pool retires workers at
        completion — never while a task is still in flight/requeued (that would shrink the pool
        and wedge the run). Under monitor pause it blocks (no dispatch) until the flag clears."""
        with self._cond:
            while True:
                if self._all_terminal_locked():
                    return None
                if self._monitor is not None and self._monitor.poll_pause():
                    self._cond.wait(timeout=self._cond_poll_s)
                    continue
                if self._pending:
                    task = self._pending.popleft()
                    # A duplicate RESULT can win a TOCTOU race and mark a requeued task terminal
                    # while its stale entry still sits in _pending; skip it rather than dispatch a
                    # wasted game (dedup would drop the RESULT anyway — this just saves the work).
                    if task.task_id in self._done or task.task_id in self._failed:
                        continue
                    return task
                # Nothing to hand out but not all terminal (tasks in flight / being requeued):
                # wait to be woken by a result/requeue rather than retiring this worker.
                self._cond.wait(timeout=self._cond_poll_s)

    def on_result(self, msg: GameResult | GameError) -> None:
        """Record one completed/errored game. Dedups by ``task_id``; ERROR → retry path."""
        with self._cond:
            tid = msg.task_id
            if tid in self._done or tid in self._failed:
                pass  # dedup: a duplicate or a late result for an already-terminal task.
            elif isinstance(msg, GameError):
                log.info('task %s ERROR: %s', tid, msg.exc)
                self._retry_locked(tid, reason=f'game ERROR: {msg.exc}')
                self._cond.notify_all()
            else:
                self._mark_done(tid, msg)
                self._cond.notify_all()
        # A subject may have crossed terminal above → persist the checkpoint OUTSIDE the lock so
        # file I/O never blocks next_task dispatch (the snapshot was taken under the lock).
        self._flush_checkpoint_if_pending()

    def requeue(self, task: GameTask) -> None:
        """Return a dead/reaped worker's in-flight task — with the retry cap enforced."""
        with self._cond:
            tid = task.task_id
            if tid in self._done or tid in self._failed:
                pass  # already terminal (RESULT won the TOCTOU race) — drop the requeue.
            else:
                # A requeue past the cap FAILS the task → its subject can cross terminal here.
                self._retry_locked(tid, reason='worker died / stall-reaped')
                self._cond.notify_all()
        self._flush_checkpoint_if_pending()

    # -- internals (caller holds self._cond) ------------------------------- #

    def _retry_locked(self, tid: str, *, reason: str) -> None:
        self._retries[tid] += 1
        if self._retries[tid] > self._retry_cap:
            self._fail_locked(tid, reason)
        else:
            self._pending.append(self._task_by_id[tid])

    def _fail_locked(self, tid: str, reason: str) -> None:
        self._failed.add(tid)
        log.warning(
            'task %s FAILED after %d requeue(s) (retry_cap=%d) — %s; cell left under-filled',
            tid,
            self._retries[tid] - 1,
            self._retry_cap,
            reason,
        )
        subject, opp, piloting = self._task_by_id[tid].task_id.split('|')[:3]
        self._cells[(subject, opp, piloting)].done += 1
        self._advance_subject_locked(self._subject_of[tid])

    def _mark_done(self, tid: str, res: GameResult | None, *, seeded: bool = False) -> None:
        self._done.add(tid)
        if res is not None:
            self._results[tid] = res
        subject, opp, piloting = self._task_by_id[tid].task_id.split('|')[:3]
        cell = self._cells[(subject, opp, piloting)]
        cell.done += 1
        if res is not None:  # a seeded (winner-less) task counts for completeness only.
            bucket = _winner_bucket(res.winner)
            if bucket == 'a':
                cell.wins_a += 1
            elif bucket == 'b':
                cell.wins_b += 1
        self._advance_subject_locked(subject, seeded=seeded)

    def _advance_subject_locked(self, subject: str, *, seeded: bool = False) -> None:
        self._subject_terminal[subject] += 1
        if self._subject_terminal[subject] >= self._subject_total[subject]:
            comp = self._aggregate_subject_locked(subject)
            self._comparisons[subject] = comp
            # A subject fully covered by the done-set crosses its threshold during seeding: still
            # aggregate once (so its comparison is never dropped — the restart regression), but
            # DEFER the user callback to run_games. A threshold crossed by a drained (non-seeded)
            # result fires the callback inline for the subject-major incremental harvest.
            if seeded:
                self._seeded_complete.append(subject)
            else:
                # Stage a consistent done-set snapshot (taken here, under the lock) for the
                # calling pool-callback thread to write to disk after it releases _cond.
                if self._done_set_path is not None:
                    self._checkpoint_snapshot = dict(self._results)
                if self._on_subject_complete is not None:
                    self._on_subject_complete(subject, comp)

    def _flush_checkpoint_if_pending(self) -> None:
        """Write the staged done-set snapshot (if any) OUTSIDE the lock. Atomically swaps the
        snapshot slot under a brief lock so exactly one thread writes each staged snapshot; a
        later snapshot supersedes an unwritten earlier one (results only grow → nothing lost)."""
        if self._done_set_path is None:
            return
        with self._cond:
            snap = self._checkpoint_snapshot
            self._checkpoint_snapshot = None
        if snap is not None:
            save_done_set(self._done_set_path, snap)

    def _all_terminal_locked(self) -> bool:
        return (len(self._done) + len(self._failed)) >= len(self._task_by_id)

    # -- aggregation (REUSES driver_compare helpers — no reinvented Wilson math) -- #

    def _aggregate_subject_locked(self, subject: str) -> PilotingComparison:
        """Build one subject's ``PilotingComparison`` from its cell tallies (per-opponent Wilson
        CIs + deltas). Wilson/rate math is :func:`driver_compare._rates` verbatim."""
        per_opponent: list[OpponentComparison] = []
        tot_dw = tot_dd = tot_cw = tot_cd = 0
        warnings: list[str] = []
        for opp in self._subject_opponents[subject]:
            driven = self._cells[(subject, opp, 'driven')]
            baseline = self._cells[(subject, opp, 'baseline')]
            dw, dd = driven.wins_a, driven.wins_a + driven.wins_b
            cw, cd = baseline.wins_a, baseline.wins_a + baseline.wins_b
            tot_dw += dw
            tot_dd += dd
            tot_cw += cw
            tot_cd += cd
            d_rate, d_ci = _rates(dw, dd)
            c_rate, c_ci = _rates(cw, cd)
            per_opponent.append(
                OpponentComparison(
                    opponent=opp,
                    winrate_driver=d_rate,
                    winrate_cp7=c_rate,
                    winrate_delta=d_rate - c_rate,
                    driver_wins=dw,
                    driver_decided=dd,
                    cp7_wins=cw,
                    cp7_decided=cd,
                    winrate_driver_ci=d_ci,
                    winrate_cp7_ci=c_ci,
                )
            )
            for pil, c in (('driven', driven), ('baseline', baseline)):
                if c.done < c.needed:
                    warnings.append(f'cell ({subject},{opp},{pil}) under-filled: {c.done}/{c.needed} games')
        d_rate, d_ci = _rates(tot_dw, tot_dd)
        c_rate, c_ci = _rates(tot_cw, tot_cd)
        warnings.append('own-turn (goldfish) lens not run on the queue path (winrate lens only)')
        return PilotingComparison(
            candidate=subject,
            fmt='commander',
            games=max((c.needed for c in self._cells.values()), default=0),
            gate_mode_used='queue',
            fqcn='',
            own_turn_driver=_NO_GOLDFISH,
            own_turn_cp7=_NO_GOLDFISH,
            own_turn_delta=0.0,
            winrate_driver=d_rate,
            winrate_cp7=c_rate,
            winrate_delta=d_rate - c_rate,
            winrate_driver_ci=d_ci,
            winrate_cp7_ci=c_ci,
            per_opponent=tuple(per_opponent),
            warnings=tuple(warnings),
        )

    # -- result extraction ------------------------------------------------- #

    def result(self) -> RunGamesResult:
        with self._cond:
            cells = {k: (c.done, c.needed) for k, c in self._cells.items()}
            incomplete = [k for k, c in self._cells.items() if c.done < c.needed]
            return RunGamesResult(
                done_results=dict(self._results),
                failed=set(self._failed),
                comparisons=dict(self._comparisons),
                cells=cells,
                incomplete_cells=incomplete,
                complete=not self._failed and not incomplete,
            )


def run_games(
    tasks: list[GameTask],
    *,
    worker_cmd: list[str],
    workers: int | None = None,
    stall_timeout_s: float,
    done_set: Mapping[str, GameResult] | set[str] | frozenset[str] | None = None,
    done_set_path: str | os.PathLike[str] | None = None,
    monitor: ResourceMonitor | None = None,
    retry_cap: int = 2,
    on_subject_complete: Callable[[str, PilotingComparison], None] | None = None,
    cond_poll_s: float = 0.05,
    join_timeout_s: float | None = None,
) -> RunGamesResult:
    """Run every game in ``tasks`` across a persistent :class:`WorkerPool`; return the tallies.

    Owns a subject-major task deque + per-cell completeness registry, dedups by ``task_id``,
    enforces the ``retry_cap`` (K=2) poison-task cap, honours a ``monitor`` pause, and resumes
    from ``done_set`` (a ``{task_id: GameResult}`` mapping restores winners for aggregation; a
    bare ``set`` skips-only). Aggregates each subject on completion (incremental harvest) into a
    :class:`~pipeline.sim.driver_compare.PilotingComparison` via the existing Wilson helpers.

    ``workers`` defaults to :func:`pipeline.sim.governor.derive_pool_size`. The run returns when
    every task is terminal (done or failed). ``worker_cmd`` is the persistent worker (the fake
    echo worker in tests; ``XMageBatch --worker`` in production).

    When ``done_set_path`` is set the accumulated done-set is atomically re-saved after EACH
    subject completes (per-subject incremental checkpointing — ≤ one subject's in-flight games
    lost on a crash), plus a final flush before returning. The snapshot is taken under the lock
    but written OUTSIDE it so file I/O never stalls ``next_task`` dispatch.

    NOTE: the inline (drained-result) ``on_subject_complete`` harvest fires while the governor
    holds ``_cond`` — keep the callback FAST (the Wilson math it wraps is cheap), since a slow
    callback briefly blocks ``next_task`` dispatch. Callbacks for subjects fully covered by the
    restart done-set are fired here, outside the lock, before the pool starts."""
    if workers is None:
        from pipeline.sim.governor import derive_pool_size

        workers = derive_pool_size()

    gov = _Governor(
        tasks,
        retry_cap=retry_cap,
        monitor=monitor,
        done_set=done_set,
        on_subject_complete=on_subject_complete,
        cond_poll_s=cond_poll_s,
        done_set_path=done_set_path,
    )

    # Fire the callbacks deferred while seeding the done-set (subjects fully covered by the
    # restart set aggregated during construction). Run OUTSIDE the lock — no pool is dispatching
    # yet, so this cannot stall next_task, and the callback is free to be slow.
    if on_subject_complete is not None:
        for subject in gov._seeded_complete:
            on_subject_complete(subject, gov._comparisons[subject])

    # Nothing left to run (a full restart) — every subject already aggregated during seeding.
    if gov._all_terminal_locked():
        res = gov.result()
        if done_set_path is not None:
            save_done_set(done_set_path, res.done_results)
        return res

    if monitor is not None:
        monitor.start()
    pool = WorkerPool(
        worker_cmd,
        workers=workers,
        next_task=gov.next_task,
        on_result=gov.on_result,
        requeue=gov.requeue,
        stall_timeout_s=stall_timeout_s,
    )
    try:
        pool.start()
        pool.join(timeout=join_timeout_s)
    finally:
        pool.close()
        if monitor is not None:
            monitor.stop()
    res = gov.result()
    if done_set_path is not None:  # final flush (last-subject completion already checkpointed).
        save_done_set(done_set_path, res.done_results)
    return res


# --------------------------------------------------------------------------- #
# Bucket rollup — a thin wrapper folding subject comparisons into the existing buckets.
# --------------------------------------------------------------------------- #


def bucket_table_from_result(
    result: RunGamesResult,
    *,
    out_dir: str | os.PathLike[str] | None = None,
    archetypes: Mapping[str, str] | None = None,
    tightness: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Fold the per-subject comparisons into the rule-8 bucket table (REUSES
    :func:`driver_run.aggregate_buckets`). ``archetypes`` / ``tightness`` map subject → tag;
    when ``out_dir`` is given the ``buckets.json`` + ``.md`` pair is written."""
    from pipeline.sim.driver_run import DeckDelta, aggregate_buckets, write_bucket_table

    arche = archetypes or {}
    tight = tightness or {}
    deltas: list[DeckDelta] = []
    for subject, comp in result.comparisons.items():
        dw = dd = cw = cd = 0
        for o in comp.per_opponent:
            dw += o.driver_wins
            dd += o.driver_decided
            cw += o.cp7_wins
            cd += o.cp7_decided
        deltas.append(
            DeckDelta(
                deck_id=subject,
                keep='',
                archetype=arche.get(subject, ''),
                driver_wins=dw,
                driver_decided=dd,
                cp7_wins=cw,
                cp7_decided=cd,
                n_matchups=len(comp.per_opponent),
                p_tightness=tight.get(subject, ''),
            )
        )
    stats = aggregate_buckets(deltas)
    if out_dir is not None:
        write_bucket_table(stats, out_dir=out_dir)
    return {b: s.as_dict() for b, s in stats.items()}
