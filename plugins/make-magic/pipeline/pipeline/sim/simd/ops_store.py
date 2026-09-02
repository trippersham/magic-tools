"""Phase A2.3 — the crash-safe ``ops.duckdb`` operational store (single-writer).

The operational counterpart to the lake's :mod:`pipeline.sim.store` cache: a **per-run** DuckDB
file that holds enough committed state to REPLAY a run on restart — the universe of tasks, every
completed game's result, the per-task attempt log, and the persistent quarantine latch. It
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

import json
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

_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS simd_tasks (
    task_id  TEXT PRIMARY KEY,
    subject  TEXT NOT NULL,
    opponent TEXT NOT NULL,
    piloting TEXT NOT NULL,
    fmt      TEXT NOT NULL
)
"""

_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS simd_attempts (
    task_id    TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    outcome    TEXT NOT NULL,
    detail     TEXT,
    created_at TIMESTAMP,
    PRIMARY KEY (task_id, attempt, outcome)
)
"""

_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS simd_results (
    task_id    TEXT PRIMARY KEY,
    winner     TEXT NOT NULL,
    kill_turn  INTEGER,
    ms         INTEGER NOT NULL,
    markers    TEXT,
    log_path   TEXT,
    reason     TEXT,
    end_cause  TEXT,
    created_at TIMESTAMP
)
"""

#: Additive migration for a store created before the terminal-cause gate: add ``end_cause`` if the
#: table predates it. DuckDB tolerates ``ADD COLUMN IF NOT EXISTS``; old rows read back as ``None``
#: (a legacy row the classifier routes through the ms-floor fallback).
_RESULTS_MIGRATE = 'ALTER TABLE simd_results ADD COLUMN IF NOT EXISTS end_cause TEXT'

_QUARANTINE_DDL = """
CREATE TABLE IF NOT EXISTS simd_quarantine (
    task_id    TEXT PRIMARY KEY,
    subject    TEXT NOT NULL,
    opponent   TEXT NOT NULL,
    piloting   TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    reason     TEXT,
    created_at TIMESTAMP
)
"""


def _now() -> datetime:
    return datetime.now(UTC)


class OpsStore:
    """A single-writer, crash-safe operational store backing one simd run.

    Open on a dedicated ``ops.duckdb`` path (NOT the shared lake db). Reconstructing a store on
    the same path after a crash replays every committed row — that is the resume contract.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        from pathlib import Path

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = duckdb.connect(str(p))
        with self._lock:
            self._conn.execute(_TASKS_DDL)
            self._conn.execute(_ATTEMPTS_DDL)
            self._conn.execute(_RESULTS_DDL)
            self._conn.execute(_RESULTS_MIGRATE)
            self._conn.execute(_QUARANTINE_DDL)

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
        """Record the run's task universe (idempotent — re-registering a task is a no-op)."""
        from pipeline.sim.game_tasks import cell_key

        with self._lock:
            for t in tasks:
                subject, opp, pil = cell_key(t)
                self._conn.execute(
                    'INSERT INTO simd_tasks (task_id, subject, opponent, piloting, fmt) '
                    'VALUES (?, ?, ?, ?, ?) ON CONFLICT (task_id) DO NOTHING',
                    [t.task_id, subject, opp, pil, t.fmt],
                )

    def record_attempt(self, task_id: str, attempt: int, outcome: str, detail: str | None = None) -> None:
        """Append one dispatch-log row (``ON CONFLICT DO NOTHING`` — replay-safe)."""
        with self._lock:
            self._conn.execute(
                'INSERT INTO simd_attempts (task_id, attempt, outcome, detail, created_at) '
                'VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING',
                [task_id, attempt, outcome, detail, _now()],
            )

    def record_result(self, res: GameResult) -> None:
        """Commit one completed game (upsert by task_id → re-recording never dups a row)."""
        with self._lock:
            self._conn.execute('DELETE FROM simd_results WHERE task_id = ?', [res.task_id])
            self._conn.execute(
                'INSERT INTO simd_results '
                '(task_id, winner, kill_turn, ms, markers, log_path, reason, end_cause, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
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
        """Persist a poison-latched task (upsert by task_id) so a restart never re-attempts it."""
        with self._lock:
            self._conn.execute('DELETE FROM simd_quarantine WHERE task_id = ?', [task_id])
            self._conn.execute(
                'INSERT INTO simd_quarantine '
                '(task_id, subject, opponent, piloting, attempts, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                [task_id, subject, opponent, piloting, attempts, reason, _now()],
            )

    # -- reads (replay) ---------------------------------------------------- #

    def load_tasks(self) -> dict[str, tuple[str, str, str, str]]:
        """``{task_id: (subject, opponent, piloting, fmt)}`` — the registered universe."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT task_id, subject, opponent, piloting, fmt FROM simd_tasks'
            ).fetchall()
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}

    def load_results(self) -> dict[str, GameResult]:
        """``{task_id: GameResult}`` for every committed game — the done-set replacement."""
        with self._lock:
            rows = self._conn.execute(
                'SELECT task_id, winner, kill_turn, ms, markers, log_path, reason, end_cause FROM simd_results'
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
            rows = self._conn.execute('SELECT task_id FROM simd_quarantine').fetchall()
        return {r[0] for r in rows}

    def load_attempts(self) -> dict[str, int]:
        """``{task_id: dispatch_count}`` — the restored per-task attempt budget.

        Counts ``dispatch`` rows only (each real hand-out of the task), so a resumed task keeps
        the attempts it already burned instead of restarting its cap from zero.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, count(*) FROM simd_attempts WHERE outcome = 'dispatch' GROUP BY task_id"
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}
