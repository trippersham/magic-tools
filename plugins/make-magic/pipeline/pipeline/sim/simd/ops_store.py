"""Phase A2.3 — the crash-safe ``ops.duckdb`` operational store (single-writer).

The operational counterpart to the lake's :mod:`pipeline.sim.store` cache: a DuckDB file that
holds enough committed state to REPLAY a run on restart — the universe of tasks, every completed
game's result, the per-task attempt log, and the persistent quarantine latch. It

**Run scoping.** One ops file may hold MANY runs; every row carries a ``run_id`` (the ``simd_runs``
manifest table records each run + its config fingerprint) and every query filters by it, so two
runs sharing a file never see each other's tasks/results/quarantine and one run's committed rows
can never prematurely "drain" another. Each task row also stores an immutable ``fingerprint`` (deck
basenames + driver classpath/fqcn + format); re-registering a task_id under a CHANGED fingerprint is
refused (mixed-universe resume). A pre-run_id (legacy) file is migrated on open: its rows are
rebuilt under the ``legacy`` run so historical fixtures/endurance resumes keep reading. It
REPLACES the per-subject JSONL done-set (:func:`pipeline.sim.game_queue.save_done_set`), which
checkpointed only at subject boundaries and therefore lost the whole in-flight subject on every
crash. Here every completed game is committed the instant it drains, so a mid-subject restart
loses ZERO games.

Schema (keyed by the deterministic task fingerprint = ``task_id``; the ``(subject, opponent,
piloting)`` cell is carried denormalized so coverage replays without re-parsing ids):

* ``simd_tasks``      — the task registry (the run's universe): one row per task_id + its cell.
* ``simd_attempts``   — append-only dispatch log: one row per (task_id, attempt) with the outcome
  (``dispatch`` / ``result`` / ``error`` / ``requeue`` / ``quarantine``). Drives the attempt-cap
  and survives restart so a task resumes with its remaining budget, not a fresh one.
* ``simd_results``    — one row per COMPLETED game (terminal success): the done-set replacement.
* ``simd_quarantine`` — one row per poison-latched task (terminal failure): persisted so a
  restart NEVER re-attempts it.

Coverage is a pure function of ``simd_tasks`` + ``simd_results`` + ``simd_quarantine`` and is
DERIVED on read (:class:`~pipeline.sim.simd.scheduler.SimdRunResult`) rather than stored — a
materialized coverage row could diverge from the base tables it summarizes, so it is never
written (see the phase note in the workpad).

**Single-writer discipline (DuckDB requirement + the design's "single owning thread/lock").**
One long-lived connection guarded by one :class:`threading.Lock`; every mutation is one
autocommitted statement (DuckDB autocommit = one durable transaction per ``execute``), so a
crash between statements leaves a consistent prefix — never a torn row. The store is opened by
:class:`~pipeline.sim.simd.engine.run_games_simd` per run and reconstructed from the same file on
restart to replay committed state.
"""

from __future__ import annotations

import hashlib
import json
import os.path
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import duckdb

from pipeline.sim.game_protocol import GameResult

if TYPE_CHECKING:
    import os
    from collections.abc import Iterable

    from pipeline.sim.game_tasks import GameTask

__all__ = ('OpsStore',)

#: The run_id assigned to rows migrated in from a pre-run_id (legacy) ops file — historical data
#: that predates run scoping. Opening a legacy store under this run replays those rows unchanged.
LEGACY_RUN_ID = 'legacy'

#: The run_id used when a caller opens a store without naming one (single-run files + the direct
#: round-trip tests). A production run always passes an explicit config-derived run_id.
DEFAULT_RUN_ID = 'default'

_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS simd_runs (
    run_id     TEXT PRIMARY KEY,
    created_at TIMESTAMP,
    config     TEXT
)
"""

_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS simd_tasks (
    run_id      TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    opponent    TEXT NOT NULL,
    piloting    TEXT NOT NULL,
    fmt         TEXT NOT NULL,
    fingerprint TEXT,
    PRIMARY KEY (run_id, task_id)
)
"""

_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS simd_attempts (
    run_id     TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    outcome    TEXT NOT NULL,
    detail     TEXT,
    created_at TIMESTAMP,
    PRIMARY KEY (run_id, task_id, attempt, outcome)
)
"""

_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS simd_results (
    run_id     TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    winner     TEXT NOT NULL,
    kill_turn  INTEGER,
    ms         INTEGER NOT NULL,
    markers    TEXT,
    log_path   TEXT,
    reason     TEXT,
    end_cause  TEXT,
    created_at TIMESTAMP,
    PRIMARY KEY (run_id, task_id)
)
"""

_QUARANTINE_DDL = """
CREATE TABLE IF NOT EXISTS simd_quarantine (
    run_id     TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    subject    TEXT NOT NULL,
    opponent   TEXT NOT NULL,
    piloting   TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    reason     TEXT,
    created_at TIMESTAMP,
    PRIMARY KEY (run_id, task_id)
)
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _driver_ident(seat: object) -> tuple[str, str] | None:
    drv = getattr(seat, 'driver', None)
    if drv is None:
        return None
    return (drv.classpath, drv.fqcn)


def task_fingerprint(t: GameTask) -> str:
    """A stable hash binding a task_id to its immutable science: deck basenames + driver
    classpath/fqcn + format. Staging dirs are relocated every run, so only the deck BASENAME is
    fingerprinted (the full path is not stable across resumes). Re-registering a task_id whose
    fingerprint changed means the underlying deck/driver/config was swapped under a reused id —
    :meth:`OpsStore.register_tasks` refuses that mixed-universe resume.
    """
    payload = {
        'fmt': t.fmt,
        'a_deck': os.path.basename(t.seat_a.deck_path),
        'a_driver': _driver_ident(t.seat_a),
        'b_deck': os.path.basename(t.seat_b.deck_path),
        'b_driver': _driver_ident(t.seat_b),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


class OpsStore:
    """A single-writer, crash-safe operational store backing one simd run.

    Open on a dedicated ``ops.duckdb`` path (NOT the shared lake db). Reconstructing a store on
    the same path after a crash replays every committed row — that is the resume contract.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        run_id: str = DEFAULT_RUN_ID,
        config_fingerprint: str | None = None,
    ) -> None:
        from pathlib import Path

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id
        self._lock = threading.Lock()
        self._conn = duckdb.connect(str(p))
        with self._lock:
            self._migrate_legacy_locked()
            self._conn.execute(_RUNS_DDL)
            self._conn.execute(_TASKS_DDL)
            self._conn.execute(_ATTEMPTS_DDL)
            self._conn.execute(_RESULTS_DDL)
            self._conn.execute(_QUARANTINE_DDL)
            self._open_run_locked(run_id, config_fingerprint)

    def _open_run_locked(self, run_id: str, config_fingerprint: str | None) -> None:
        """Create this run's manifest row, or verify a resume against the stored config fingerprint.

        Resuming a run_id whose stored config fingerprint differs from ``config_fingerprint`` is a
        mixed-universe resume (``--games``/attempt-cap/bailout/format changed under the same run) and
        is REFUSED loudly. A stored ``NULL`` fingerprint (a row created by a bare open) is adopted by
        the first real fingerprint; a ``None`` incoming fingerprint never triggers the guard.
        """
        row = self._conn.execute(
            'SELECT config FROM simd_runs WHERE run_id = ?', [run_id]
        ).fetchone()
        if row is None:
            self._conn.execute(
                'INSERT INTO simd_runs (run_id, created_at, config) VALUES (?, ?, ?) '
                'ON CONFLICT (run_id) DO NOTHING',
                [run_id, _now(), config_fingerprint],
            )
            return
        stored = row[0]
        if config_fingerprint is None:
            return  # a bare open (inspection/round-trip) — never re-fingerprints or guards.
        if stored is None:
            self._conn.execute(
                'UPDATE simd_runs SET config = ? WHERE run_id = ?', [config_fingerprint, run_id]
            )
            return
        if stored != config_fingerprint:
            msg = (
                f'run {run_id!r} resumed with a CHANGED config fingerprint (stored {stored!r}, '
                f'incoming {config_fingerprint!r}): the format / attempt-cap / top-up-cap / bailout '
                'floor changed under the same run — mixed-universe resume refused. Use a fresh run_id '
                '(or ops file) for a changed run configuration.'
            )
            raise ValueError(msg)

    # -- migration --------------------------------------------------------- #

    def _table_columns_locked(self, table: str) -> set[str]:
        rows = self._conn.execute(
            'SELECT column_name FROM information_schema.columns WHERE table_name = ?', [table]
        ).fetchall()
        return {r[0] for r in rows}

    def _migrate_legacy_locked(self) -> None:
        """Backfill a pre-run_id ops file: rebuild each table with a ``run_id`` (+ ``fingerprint``
        on tasks) and stamp every existing row with :data:`LEGACY_RUN_ID`, so historical fixtures
        and endurance-run resumes keep reading. A file that already has run_id (or is brand new) is
        left untouched — every rebuild is guarded by the column's absence.
        """
        cols = self._table_columns_locked('simd_tasks')
        if not cols or 'run_id' in cols:
            return  # brand-new file, or already migrated — nothing to backfill.

        legacy = LEGACY_RUN_ID
        # simd_results may predate the end_cause column; add it before copying.
        self._conn.execute('ALTER TABLE simd_results ADD COLUMN IF NOT EXISTS end_cause TEXT')

        self._conn.execute('BEGIN TRANSACTION')
        try:
            self._conn.execute('ALTER TABLE simd_tasks RENAME TO simd_tasks_legacy')
            self._conn.execute(_TASKS_DDL)
            self._conn.execute(
                'INSERT INTO simd_tasks (run_id, task_id, subject, opponent, piloting, fmt, fingerprint) '
                f"SELECT '{legacy}', task_id, subject, opponent, piloting, fmt, NULL FROM simd_tasks_legacy"
            )
            self._conn.execute('DROP TABLE simd_tasks_legacy')

            self._conn.execute('ALTER TABLE simd_results RENAME TO simd_results_legacy')
            self._conn.execute(_RESULTS_DDL)
            self._conn.execute(
                'INSERT INTO simd_results '
                '(run_id, task_id, winner, kill_turn, ms, markers, log_path, reason, end_cause, created_at) '
                f"SELECT '{legacy}', task_id, winner, kill_turn, ms, markers, log_path, reason, end_cause, "
                'created_at FROM simd_results_legacy'
            )
            self._conn.execute('DROP TABLE simd_results_legacy')

            self._conn.execute('ALTER TABLE simd_quarantine RENAME TO simd_quarantine_legacy')
            self._conn.execute(_QUARANTINE_DDL)
            self._conn.execute(
                'INSERT INTO simd_quarantine '
                '(run_id, task_id, subject, opponent, piloting, attempts, reason, created_at) '
                f"SELECT '{legacy}', task_id, subject, opponent, piloting, attempts, reason, created_at "
                'FROM simd_quarantine_legacy'
            )
            self._conn.execute('DROP TABLE simd_quarantine_legacy')

            self._conn.execute('ALTER TABLE simd_attempts RENAME TO simd_attempts_legacy')
            self._conn.execute(_ATTEMPTS_DDL)
            self._conn.execute(
                'INSERT INTO simd_attempts (run_id, task_id, attempt, outcome, detail, created_at) '
                f"SELECT '{legacy}', task_id, attempt, outcome, detail, created_at FROM simd_attempts_legacy"
            )
            self._conn.execute('DROP TABLE simd_attempts_legacy')
            self._conn.execute('COMMIT')
        except Exception:
            self._conn.execute('ROLLBACK')
            raise

    # -- lifecycle --------------------------------------------------------- #

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> OpsStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writes (each is one autocommitted statement) ---------------------- #

    def register_tasks(self, tasks: Iterable[GameTask]) -> None:
        """Record the run's task universe, binding each task_id to an immutable fingerprint.

        Idempotent for an unchanged task (same fingerprint → no-op). A task_id re-registered with a
        DIFFERENT fingerprint (deck/driver/format swapped under a reused id) raises loudly — a
        mixed-universe resume is refused, never silently adopted as the old task.
        """
        from pipeline.sim.game_tasks import cell_key

        with self._lock:
            for t in tasks:
                subject, opp, pil = cell_key(t)
                fp = task_fingerprint(t)
                prior = self._conn.execute(
                    'SELECT fingerprint FROM simd_tasks WHERE run_id = ? AND task_id = ?',
                    [self._run_id, t.task_id],
                ).fetchone()
                if prior is not None and prior[0] is not None and prior[0] != fp:
                    msg = (
                        f'task {t.task_id!r} re-registered under run {self._run_id!r} with a CHANGED '
                        f'fingerprint (stored {prior[0][:12]}…, incoming {fp[:12]}…): the deck/driver/'
                        'format bound to this id changed — mixed-universe resume refused. Use a fresh '
                        'run_id (or ops file) for a changed task universe.'
                    )
                    raise ValueError(msg)
                self._conn.execute(
                    'INSERT INTO simd_tasks (run_id, task_id, subject, opponent, piloting, fmt, fingerprint) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (run_id, task_id) DO NOTHING',
                    [self._run_id, t.task_id, subject, opp, pil, t.fmt, fp],
                )

    def record_attempt(self, task_id: str, attempt: int, outcome: str, detail: str | None = None) -> None:
        """Append one dispatch-log row (``ON CONFLICT DO NOTHING`` — replay-safe)."""
        with self._lock:
            self._conn.execute(
                'INSERT INTO simd_attempts (run_id, task_id, attempt, outcome, detail, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING',
                [self._run_id, task_id, attempt, outcome, detail, _now()],
            )

    def record_result(self, res: GameResult) -> None:
        """Commit one completed game as ONE atomic upsert (single statement → a crash mid-write can
        never erase the previously committed terminal row; the old DELETE+INSERT pair could)."""
        with self._lock:
            self._conn.execute(
                'INSERT INTO simd_results '
                '(run_id, task_id, winner, kill_turn, ms, markers, log_path, reason, end_cause, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT (run_id, task_id) DO UPDATE SET '
                'winner = excluded.winner, kill_turn = excluded.kill_turn, ms = excluded.ms, '
                'markers = excluded.markers, log_path = excluded.log_path, reason = excluded.reason, '
                'end_cause = excluded.end_cause, created_at = excluded.created_at',
                [
                    self._run_id,
                    res.task_id,
                    res.winner,
                    res.kill_turn,
                    res.ms,
                    json.dumps(list(res.markers)),
                    res.log_path,
                    res.reason,
                    res.end_cause,
                    _now(),
                ],
            )

    def quarantine(
        self, task_id: str, subject: str, opponent: str, piloting: str, *, attempts: int, reason: str | None
    ) -> None:
        """Persist a poison-latched task as ONE atomic upsert so a restart never re-attempts it (and
        a crash mid-write can never erase an already-committed quarantine latch)."""
        with self._lock:
            self._conn.execute(
                'INSERT INTO simd_quarantine '
                '(run_id, task_id, subject, opponent, piloting, attempts, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT (run_id, task_id) DO UPDATE SET '
                'subject = excluded.subject, opponent = excluded.opponent, piloting = excluded.piloting, '
                'attempts = excluded.attempts, reason = excluded.reason, created_at = excluded.created_at',
                [self._run_id, task_id, subject, opponent, piloting, attempts, reason, _now()],
            )

    # -- reads (replay) ---------------------------------------------------- #

    def load_tasks(self) -> dict[str, tuple[str, str, str, str]]:
        """``{task_id: (subject, opponent, piloting, fmt)}`` — the registered universe."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT task_id, subject, opponent, piloting, fmt FROM simd_tasks WHERE run_id = ?',
                [self._run_id],
            ).fetchall()
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}

    def load_results(self) -> dict[str, GameResult]:
        """``{task_id: GameResult}`` for every committed game — the done-set replacement."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT task_id, winner, kill_turn, ms, markers, log_path, reason, end_cause '
                'FROM simd_results WHERE run_id = ?',
                [self._run_id],
            ).fetchall()
        out: dict[str, GameResult] = {}
        for r in rows:
            out[r[0]] = GameResult(
                task_id=r[0],
                winner=r[1],
                kill_turn=r[2],
                ms=int(r[3]),
                markers=list(json.loads(r[4])) if r[4] else [],
                log_path=r[5],
                reason=r[6],
                end_cause=r[7],
            )
        return out

    def load_quarantine(self) -> set[str]:
        """The set of terminally quarantined task_ids (skipped on restart)."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT task_id FROM simd_quarantine WHERE run_id = ?', [self._run_id]
            ).fetchall()
        return {r[0] for r in rows}

    def load_attempts(self) -> dict[str, int]:
        """``{task_id: dispatch_count}`` — the restored per-task attempt budget.

        Counts ``dispatch`` rows only (each real hand-out of the task), so a resumed task keeps
        the attempts it already burned instead of restarting its cap from zero.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, count(*) FROM simd_attempts "
                "WHERE outcome = 'dispatch' AND run_id = ? GROUP BY task_id",
                [self._run_id],
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}
