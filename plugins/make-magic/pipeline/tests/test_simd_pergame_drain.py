"""Per-game durable transcript/feature drain (simd) — failing-first TDD.

A game's historical transcript/feature record must be durable the MOMENT its result commits,
never dependent on a clean run exit (the post-mortem: end-of-run ``ingest_queue_transcripts``
lost 1,369 games across three generation deaths). These tests prove:

1. ``OpsStore.drain_game`` upserts run-scoped ``simd_game_logs`` + ``simd_game_features`` rows and
   deletes the transcript file on success (continuous disk reclaim + GC ingest-guard).
2. A MISSING/unreadable transcript at drain time yields a FLAGGED row, never a silent skip.
3. The scheduler drains EACH game the instant its result commits — mid-run inspection sees the
   rows with no dependency on run exit; a SIGTERM mid-run loses zero committed transcripts.
4. ``rollup_to_lake`` re-keys the durable ops record onto JOINABLE lake matchup rows (the (c)
   keying-bug fix) and is idempotent.

PURE PYTHON — NO JVM (hand-authored transcript fixtures + fake results).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from pipeline import store
from pipeline.sim import driver_run as dr
from pipeline.sim import store as sim_store
from pipeline.sim.game_protocol import GameResult
from pipeline.sim.game_tasks import SeatSpec, build_game_tasks
from pipeline.sim.simd.ops_store import OpsStore
from pipeline.sim.simd.scheduler import SimdScheduler

_FIX = Path(__file__).parent / 'fixtures' / 'transcripts'
DRIVEN_COMBO_WIN = (_FIX / 'driven_combo_win.log').read_text()
DRIVERLESS_LOSS = (_FIX / 'driverless_loss.log').read_text()


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding='utf-8')
    return p


def _result(task_id: str, log_path: str | None, *, winner: str = 'a') -> GameResult:
    return GameResult(
        task_id=task_id,
        winner=winner,
        kill_turn=3,
        ms=8421,
        markers=[],
        log_path=log_path,
    )


# ===================================================================================== #
# 1. OpsStore.drain_game — per-game durable persistence + file cleanup.
# ===================================================================================== #


def test_drain_game_persists_features_and_logs_and_deletes_file(tmp_path: Path) -> None:
    ops_path = tmp_path / 'ops.duckdb'
    log = _write(tmp_path, 'g0.log', DRIVEN_COMBO_WIN)
    tid = 's_x|o_y|driven|0'
    with OpsStore(ops_path, run_id='r1') as ops:
        assert ops.drain_game(_result(tid, str(log))) is True
        logs = ops.load_game_logs()
        feats = ops.load_game_features()
    assert tid in logs
    subject, opp, pil, gidx, raw_log, missing = logs[tid]
    assert (subject, opp, pil, gidx) == ('s_x', 'o_y', 'driven', '0')
    assert missing is False
    assert raw_log is not None and 'MACRO_FIRE_REAL' in raw_log
    f = feats[tid]
    assert f['winner'] == 'a'
    assert f['wincon'] == 'combo'
    assert f['fired_turn'] == 5
    assert f['missing_transcript'] is False
    # Success → the transcript FILE is reclaimed continuously (no dependency on run exit).
    assert not log.exists()


def test_drain_missing_transcript_flags_row_not_silent(tmp_path: Path) -> None:
    ops_path = tmp_path / 'ops.duckdb'
    tid = 's_x|o_y|driven|1'
    with OpsStore(ops_path, run_id='r1') as ops:
        # log_path None AND a nonexistent path both flag, never skip.
        assert ops.drain_game(_result(tid, None, winner='b')) is False
        logs = ops.load_game_logs()
        feats = ops.load_game_features()
    assert tid in logs, 'a missing transcript must be RECORDED, never silently dropped'
    assert logs[tid][5] is True  # missing_transcript
    assert logs[tid][4] is None  # no raw_log
    assert feats[tid]['missing_transcript'] is True
    assert feats[tid]['winner'] == 'b'  # winner carried from the result


def test_drain_unreadable_transcript_is_kept_and_flagged(tmp_path: Path) -> None:
    ops_path = tmp_path / 'ops.duckdb'
    missing_path = tmp_path / 'gone.log'  # never created
    tid = 's_x|o_y|driven|2'
    with OpsStore(ops_path, run_id='r1') as ops:
        assert ops.drain_game(_result(tid, str(missing_path))) is False
        assert ops.load_game_features()[tid]['missing_transcript'] is True


# ===================================================================================== #
# 2. Scheduler wiring — every committed game is drained the instant its result commits.
# ===================================================================================== #


def _tasks() -> list:
    subjects = [SeatSpec(deck_path='s_x.txt', driver=None)]
    opponents = [SeatSpec(deck_path='o_y.txt', driver=None)]
    return build_game_tasks(subjects, opponents, 2, fmt='commander')


def test_scheduler_drains_each_game_at_result_commit(tmp_path: Path) -> None:
    ops_path = tmp_path / 'ops.duckdb'
    tasks = _tasks()
    with OpsStore(ops_path, run_id='r1') as ops:
        ops.register_tasks(tasks)
        sched = SimdScheduler(tasks, ops=ops, bailout_floor_ms=0)
        t0 = tasks[0]
        log0 = _write(tmp_path, 'a.log', DRIVEN_COMBO_WIN)
        sched.on_result(_result(t0.task_id, str(log0)))
        # The moment the FIRST result commits — before any run exit — its transcript is durable.
        feats = ops.load_game_features()
        assert t0.task_id in feats
        assert feats[t0.task_id]['fired_turn'] == 5
        assert not log0.exists()  # drained + reclaimed
        # A missing transcript on the second game is flagged, run continues.
        t1 = tasks[1]
        sched.on_result(_result(t1.task_id, None))
        assert ops.load_game_features()[t1.task_id]['missing_transcript'] is True


# ===================================================================================== #
# 3. SIGTERM mid-run: teardown runs, exits nonzero, zero transcript loss (main-thread).
# ===================================================================================== #

_TERM_DRIVER = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path
    from pipeline.sim import driver_run as dr
    from pipeline.sim import runner as runner_mod
    import pipeline.sim.simd.engine as engine_mod
    from pipeline.sim.simd.ops_store import OpsStore
    from pipeline.sim.game_protocol import GameResult

    tmp = Path(sys.argv[1])
    ops_db = tmp / 'q' / 'ops.duckdb'
    fix = Path(sys.argv[2])
    text = fix.read_text()

    # Isolate staging under tmp (never the live data dir) so the sweep assertion is deterministic.
    staging = tmp / 'staging'; staging.mkdir(parents=True, exist_ok=True)
    runner_mod.staging_root = lambda: staging

    def fake_build(rows, field, *, games, stage_dir, data_dir=None, thin_rows=()):
        (Path(stage_dir) / 's_x.txt').write_text('99 Forest\\n')
        return []

    def fake_simd(tasks, *, ops_db_path, run_id, **kw):
        # Commit + per-game-drain two games into the SAME ops file/run the queue owns, then
        # deliver SIGTERM mid-run (before any clean return).
        log0 = tmp / 'g0.log'; log0.write_text(text)
        with OpsStore(ops_db_path, run_id=run_id) as ops:
            for i in range(2):
                p = tmp / f'g{i}.log'; p.write_text(text)
                ops.drain_game(GameResult(task_id=f's_x|o_y|driven|{i}', winner='a',
                    kill_turn=3, ms=8421, markers=[], log_path=str(p)))
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError('SIGTERM should have unwound before this line')

    dr.build_corpus_game_tasks = fake_build
    dr.rollup_to_lake = lambda *a, **k: 0
    engine_mod.run_games_simd = fake_simd

    try:
        dr.run_corpus_queue(rows=[{'deck_id': 'd'}], field=[], games=2,
                            ops_db_path=ops_db, worker_cmd=['x'])
    except SystemExit as e:
        # Report the leaked corpus-* staging dirs (must be none) + the exit code back to the parent.
        leaked = sorted(p.name for p in staging.glob('corpus-*'))
        print('LEAKED', leaked)
        print('EXITCODE', e.code)
        sys.exit(int(e.code or 0))
    print('NO_SYSTEMEXIT'); sys.exit(99)
    """
)


def test_sigterm_midrun_tears_down_nonzero_and_keeps_transcripts(tmp_path: Path) -> None:
    """A SIGTERM mid-run unwinds through teardown (staging swept, flock released), exits NONZERO,
    and every committed game's transcript survives in ops.duckdb (per-game drain → no loss)."""
    driver = _write(tmp_path, 'drv.py', _TERM_DRIVER)
    env = {**os.environ, 'MAKE_MAGIC_DATA_DIR': str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, str(driver), str(tmp_path), str(_FIX / 'driven_combo_win.log')],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1, f'expected nonzero TERM exit; stdout={proc.stdout} stderr={proc.stderr}'
    # Teardown ran: no leaked corpus-* staging dirs (reported by the subprocess over its own tmp staging).
    assert 'LEAKED []' in proc.stdout, f'staging not swept on TERM; stdout={proc.stdout} stderr={proc.stderr}'
    # Zero transcript loss: ops.duckdb holds both committed games' drained rows.
    ops_db = tmp_path / 'q' / 'ops.duckdb'
    import duckdb

    conn = duckdb.connect(str(ops_db), read_only=True)
    try:
        n = conn.execute('SELECT count(*) FROM simd_game_logs').fetchone()[0]
    finally:
        conn.close()
    assert n == 2, 'both committed games must survive the TERM in ops.duckdb'


# ===================================================================================== #
# 4. rollup_to_lake — the (c) keying fix + idempotency.
# ===================================================================================== #


@pytest.fixture()
def _lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'lake'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _seed_ops(ops_path: Path) -> None:
    """Drain three games (2 originals + 1 top-up) for one cell into ops."""
    with OpsStore(ops_path, run_id='r1') as ops:
        ops.drain_game(_result('s_x|o_y|driven|0', None, winner='a'))  # flagged, still counts W/L
        ops.drain_game(_result('s_x|o_y|driven|1', None, winner='b'))
        ops.drain_game(_result('s_x|o_y|driven|topup-0', None, winner='a'))


def test_rollup_writes_joinable_lake_rows(_lake: Path, tmp_path: Path) -> None:
    """THE (c) FIX: queue games land in the lake JOINABLE to a sim_matchups parent (the old
    cell-string key created no parent row, so every ``JOIN sim_matchups`` matched nothing)."""
    ops_path = tmp_path / 'ops.duckdb'
    _seed_ops(ops_path)
    n = dr.rollup_to_lake(ops_path, run_id='r1')
    assert n == 3
    db = sim_store._db_path(None)
    with store.connect(db) as conn:
        # The JOIN the retired ingest broke now resolves for every rolled-in game.
        joined = conn.execute(
            'SELECT count(*) FROM sim_game_features f JOIN sim_matchups m USING (matchup_key)'
        ).fetchone()[0]
        assert joined == 3
        wins_a, wins_b, draws = conn.execute('SELECT wins_a, wins_b, draws FROM sim_matchups').fetchone()
        assert (wins_a, wins_b, draws) == (2, 1, 0)
        # top-up index offset keeps originals + top-ups distinct on the grain.
        indices = sorted(r[0] for r in conn.execute('SELECT game_index FROM sim_game_features').fetchall())
        assert indices == [0, 1, dr._TOPUP_INDEX_BASE]


def test_rollup_is_idempotent(_lake: Path, tmp_path: Path) -> None:
    ops_path = tmp_path / 'ops.duckdb'
    _seed_ops(ops_path)
    dr.rollup_to_lake(ops_path, run_id='r1')
    dr.rollup_to_lake(ops_path, run_id='r1')  # re-run must not duplicate.
    db = sim_store._db_path(None)
    with store.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM sim_game_features').fetchone()[0] == 3
        assert conn.execute('SELECT count(*) FROM sim_matchups').fetchone()[0] == 1


def test_rollup_all_runs_when_run_id_omitted(_lake: Path, tmp_path: Path) -> None:
    ops_path = tmp_path / 'ops.duckdb'
    with OpsStore(ops_path, run_id='rA') as ops:
        ops.drain_game(_result('s_a|o_y|driven|0', None))
    with OpsStore(ops_path, run_id='rB') as ops:
        ops.drain_game(_result('s_b|o_y|driven|0', None))
    n = dr.rollup_to_lake(ops_path)  # no run_id → every run in the file.
    assert n == 2
    db = sim_store._db_path(None)
    with store.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM sim_matchups').fetchone()[0] == 2
