"""Blocker 2 (Sol review) — the ops store must be run-scoped, fingerprint-bound, and
crash-atomic. These tests pin the four guarantees the unscoped store violated:

1. **Fingerprint binding** — re-registering a task_id whose underlying deck/driver changed is
   REFUSED loudly (a mixed-universe resume can never silently adopt the old task's identity).
2. **Run scoping** — two runs sharing one ops.duckdb file never see each other's tasks/results/
   quarantine, and one run's committed rows can never prematurely "drain" (declare terminal) the
   other.
3. **Legacy migration** — a pre-run_id ops file still loads: its rows are backfilled under the
   ``legacy`` run so historical fixtures/resumes keep working.
4. **Crash atomicity** — a terminal write is a single atomic upsert; a crash injected mid-write
   can never erase a previously committed terminal row.
"""

from __future__ import annotations

import duckdb
import pytest

from pipeline.sim.game_protocol import GameResult
from pipeline.sim.game_tasks import DriverRef, SeatSpec, build_game_tasks
from pipeline.sim.simd.ops_store import OpsStore


def _tasks(sub: str, opp: str, games: int, *, driver: DriverRef | None = None):
    subjects = [SeatSpec(deck_path=f'/d/{sub}.dck', driver=driver)]
    field = [SeatSpec(deck_path=f'/d/{opp}.dck', driver=None)]
    return build_game_tasks(subjects, field, games, fmt='commander')


def _result(task_id: str, winner: str = 'a', ms: int = 60000) -> GameResult:
    return GameResult(task_id=task_id, winner=winner, kill_turn=3, ms=ms, markers=[], log_path=None)


class _CrashOn:
    """A connection proxy that raises on the FIRST write statement touching ``table`` (a mid-write
    crash), then delegates every call to the real connection. DuckDB's C connection object is
    read-only, so we wrap it rather than monkeypatch its ``execute``."""

    def __init__(self, real: object, table: str) -> None:
        self.real = real
        self._table = table
        self._armed = True

    def execute(self, sql: str, *a: object, **k: object):
        if self._armed and self._table in sql and ('INSERT' in sql or 'UPDATE' in sql):
            self._armed = False
            raise RuntimeError('injected crash mid-write')
        return self.real.execute(sql, *a, **k)

    def __getattr__(self, name: str):
        return getattr(self.real, name)


# --------------------------------------------------------------------------- #
# 1. FINGERPRINT BINDING
# --------------------------------------------------------------------------- #


def test_register_same_task_same_fingerprint_is_noop(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    tasks = _tasks('sa', 'oa', 2)
    with OpsStore(db, run_id='r1') as ops:
        ops.register_tasks(tasks)
        ops.register_tasks(tasks)  # idempotent re-register — must not raise.
        assert set(ops.load_tasks()) == {t.task_id for t in tasks}


def test_register_changed_fingerprint_refused(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    with OpsStore(db, run_id='r1') as ops:
        ops.register_tasks(_tasks('sa', 'oa', 1))
        # Same task_ids (same deck paths → same ids) but the subject now carries a DRIVER: the
        # underlying science changed. A silent ON CONFLICT DO NOTHING would adopt the old row.
        driven = _tasks('sa', 'oa', 1, driver=DriverRef(classpath='/cp', fqcn='X'))
        with pytest.raises(ValueError, match='fingerprint'):
            ops.register_tasks(driven)


# --------------------------------------------------------------------------- #
# 2. RUN SCOPING — two runs, one file, zero cross-contamination.
# --------------------------------------------------------------------------- #


def test_two_runs_one_file_isolated(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    t1 = _tasks('sa', 'oa', 2)
    t2 = _tasks('sb', 'ob', 2)
    with OpsStore(db, run_id='r1') as ops1:
        ops1.register_tasks(t1)
        ops1.record_result(_result(t1[0].task_id))
    with OpsStore(db, run_id='r2') as ops2:
        ops2.register_tasks(t2)
        # r2 sees ONLY its own universe — none of r1's tasks or results bled in.
        assert set(ops2.load_tasks()) == {t.task_id for t in t2}
        assert ops2.load_results() == {}
        ops2.record_result(_result(t2[0].task_id))
    # And back in r1, r2's writes are invisible.
    with OpsStore(db, run_id='r1') as ops1b:
        assert set(ops1b.load_tasks()) == {t.task_id for t in t1}
        assert set(ops1b.load_results()) == {t1[0].task_id}


def test_run_row_recorded(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    with OpsStore(db, run_id='r1', config_fingerprint='cfg-abc') as ops:
        ops.register_tasks(_tasks('sa', 'oa', 1))
    con = duckdb.connect(str(db))
    rows = con.execute('SELECT run_id, config FROM simd_runs').fetchall()
    con.close()
    assert ('r1', 'cfg-abc') in [(r[0], r[1]) for r in rows]


def test_changed_config_fingerprint_resume_refused(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    with OpsStore(db, run_id='r1', config_fingerprint='cfg-v1') as ops:
        ops.register_tasks(_tasks('sa', 'oa', 1))
    # Reopening the SAME run with a DIFFERENT config fingerprint (e.g. --games / attempt-cap changed)
    # is a mixed-universe resume and must be refused loudly.
    with pytest.raises(ValueError, match='config fingerprint'):
        OpsStore(db, run_id='r1', config_fingerprint='cfg-v2')
    # A bare open (config_fingerprint=None) never triggers the guard — inspection stays possible.
    with OpsStore(db, run_id='r1') as ops2:
        assert set(ops2.load_tasks()) == {t.task_id for t in _tasks('sa', 'oa', 1)}


# --------------------------------------------------------------------------- #
# 3. LEGACY MIGRATION — a pre-run_id file loads under the ``legacy`` run.
# --------------------------------------------------------------------------- #


def test_starter_persists_and_round_trips(tmp_path) -> None:
    """A committed result's ``starter`` (who took the first turn) survives the store round-trip."""
    db = tmp_path / 'ops.duckdb'
    tasks = _tasks('sa', 'oa', 2)
    with OpsStore(db, run_id='r1') as ops:
        ops.register_tasks(tasks)
        ops.record_result(
            GameResult(
                task_id=tasks[0].task_id,
                winner='a',
                kill_turn=3,
                ms=60000,
                markers=['starter=B'],
                log_path=None,
                starter='B',
            )
        )
    with OpsStore(db, run_id='r1') as ops2:
        loaded = ops2.load_results()
        assert loaded[tasks[0].task_id].starter == 'B'


def test_pre_starter_runid_file_gains_column(tmp_path) -> None:
    """A run_id-scoped results table predating the starter column gains it on open (additive,
    migration-tolerant): existing rows read back with starter=None, new writes persist it."""
    db = tmp_path / 'ops.duckdb'
    con = duckdb.connect(str(db))
    # A modern run_id schema but WITHOUT the starter column (the pre-work shape).
    con.execute(
        'CREATE TABLE simd_results (run_id TEXT NOT NULL, task_id TEXT NOT NULL, winner TEXT NOT NULL, '
        'kill_turn INTEGER, ms INTEGER NOT NULL, markers TEXT, log_path TEXT, reason TEXT, '
        'end_cause TEXT, created_at TIMESTAMP, PRIMARY KEY (run_id, task_id))'
    )
    con.execute("INSERT INTO simd_results (run_id, task_id, winner, ms) VALUES ('r1', 'sa|oa|driven|0', 'a', 60000)")
    con.close()

    with OpsStore(db, run_id='r1') as ops:
        loaded = ops.load_results()
        assert loaded['sa|oa|driven|0'].starter is None  # pre-existing row: unrecorded starter.
        ops.record_result(
            GameResult(
                task_id='sa|oa|driven|1',
                winner='b',
                kill_turn=3,
                ms=60000,
                markers=['starter=A'],
                log_path=None,
                starter='A',
            )
        )
        assert ops.load_results()['sa|oa|driven|1'].starter == 'A'


def test_legacy_file_loads_under_legacy_run(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    # Hand-build the OLD schema (no run_id anywhere) and commit a task + a result.
    con = duckdb.connect(str(db))
    con.execute(
        'CREATE TABLE simd_tasks (task_id TEXT PRIMARY KEY, subject TEXT NOT NULL, '
        'opponent TEXT NOT NULL, piloting TEXT NOT NULL, fmt TEXT NOT NULL)'
    )
    con.execute(
        'CREATE TABLE simd_results (task_id TEXT PRIMARY KEY, winner TEXT NOT NULL, '
        'kill_turn INTEGER, ms INTEGER NOT NULL, markers TEXT, log_path TEXT, reason TEXT, '
        'end_cause TEXT, created_at TIMESTAMP)'
    )
    con.execute(
        'CREATE TABLE simd_quarantine (task_id TEXT PRIMARY KEY, subject TEXT NOT NULL, '
        'opponent TEXT NOT NULL, piloting TEXT NOT NULL, attempts INTEGER NOT NULL, '
        'reason TEXT, created_at TIMESTAMP)'
    )
    con.execute(
        'CREATE TABLE simd_attempts (task_id TEXT NOT NULL, attempt INTEGER NOT NULL, '
        'outcome TEXT NOT NULL, detail TEXT, created_at TIMESTAMP, '
        'PRIMARY KEY (task_id, attempt, outcome))'
    )
    con.execute("INSERT INTO simd_tasks VALUES ('sa|oa|driven|0', 'sa', 'oa', 'driven', 'commander')")
    con.execute("INSERT INTO simd_results (task_id, winner, ms) VALUES ('sa|oa|driven|0', 'a', 60000)")
    con.close()

    # Open with the new store under the legacy run: the historical rows read back.
    with OpsStore(db, run_id='legacy') as ops:
        assert set(ops.load_tasks()) == {'sa|oa|driven|0'}
        assert set(ops.load_results()) == {'sa|oa|driven|0'}

    # A DIFFERENT run in the same (now-migrated) file does NOT see the legacy rows.
    with OpsStore(db, run_id='fresh') as ops2:
        assert ops2.load_tasks() == {}
        assert ops2.load_results() == {}


# --------------------------------------------------------------------------- #
# 4. CRASH ATOMICITY — a mid-write crash cannot erase a committed terminal row.
# --------------------------------------------------------------------------- #


def test_result_upsert_atomic_no_row_loss_on_crash(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    tasks = _tasks('sa', 'oa', 1)
    tid = tasks[0].task_id
    with OpsStore(db, run_id='r1') as ops:
        ops.register_tasks(tasks)
        ops.record_result(_result(tid, winner='a', ms=60000))  # committed terminal row v1.

        # Inject a crash DURING the second write. With a single atomic upsert the failed
        # statement rolls back and v1 survives; the old DELETE+INSERT would have already
        # erased v1 by the time the INSERT blows up.
        ops._conn = _CrashOn(ops._conn, 'simd_results')
        with pytest.raises(RuntimeError, match='injected crash'):
            ops.record_result(_result(tid, winner='b', ms=1))
        ops._conn = ops._conn.real

        # v1 is intact — the committed terminal row was NOT lost.
        loaded = ops.load_results()
        assert set(loaded) == {tid}
        assert loaded[tid].winner == 'a'
        assert loaded[tid].ms == 60000


def test_quarantine_upsert_atomic_no_row_loss_on_crash(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    tasks = _tasks('sa', 'oa', 1)
    tid = tasks[0].task_id
    with OpsStore(db, run_id='r1') as ops:
        ops.register_tasks(tasks)
        ops.quarantine(tid, 'sa', 'oa', 'driven', attempts=2, reason='poison-v1')
        ops._conn = _CrashOn(ops._conn, 'simd_quarantine')
        with pytest.raises(RuntimeError, match='injected crash'):
            ops.quarantine(tid, 'sa', 'oa', 'driven', attempts=3, reason='poison-v2')
        ops._conn = ops._conn.real
        assert ops.load_quarantine() == {tid}
