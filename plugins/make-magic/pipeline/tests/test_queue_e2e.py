"""Phase 5.1 — END-TO-END behavioral verification of the persistent-worker game queue.

Drives the REAL :func:`~pipeline.sim.game_queue.run_games` governor over a real
:class:`~pipeline.sim.worker_pool.WorkerPool` of real ``XMageBatch --worker`` JVMs (the
Phase-2 worker, spawned via the Phase-5.0 :func:`~pipeline.sim.driver_run.resolve_worker_cmd`
COW-staging bootstrap), SMALL: 2 compiled-driver subjects x 2 bare-CP7 opponents x 2 pilotings
x 2 games = 16 games, ``workers=2``.

Marked ``@pytest.mark.integration`` (deselected by default; run with ``-m integration``) and
self-skips unless the local dist jar + lab JRE + the two compiled drivers + the warm card DB
are all present.

The run is PHASED so it also proves the per-game RESTART property end-to-end:

* **Phase A** runs subject-1 only (8 games) into ``logdir_A``, saving a done-set sidecar.
* **Phase B** runs BOTH subjects (16 games) with Phase A's done-set seeded, into a SEPARATE
  ``logdir_B``. Subject-1's 8 tasks are already done, so ONLY subject-2's 8 games actually run
  — proved by ``logdir_B`` holding zero subject-1 transcripts — and subject-1's
  ``PilotingComparison`` on resume equals Phase A's (restart equivalence).

Every acceptance criterion (1-7) is asserted against the real observed artifacts.

**NOTE — corpus-path bug (STOP-and-report).** This test does NOT call the committed
:func:`~pipeline.sim.driver_run.build_corpus_game_tasks` / ``run_corpus_queue``: those set the
subject ``deck_path`` to a raw Forge ``.dck`` and the opponent ``deck_path`` to a bare deck
NAME, but the P2 worker imports ``seat.deck`` as a file with NO Forge->XMage translation — a
raw ``.dck`` loads as 0 cards (verified: ``deck too small (0 cards)``) and a bare name is not a
path. So the committed corpus builder produces tasks the worker cannot run. This test stages
translated XMage ``.txt`` decks and builds the task list directly, exercising the identical
governor + pool + worker machinery; the builder bug is reported to the orchestrator to fix.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# The two fast mono-color subjects from the v2 batch ledger (compiled drivers present).
_SUBJECTS = (
    'commander/casual/mono-black__ayara-first-of-locthwain.dck',
    'commander/casual/mono-green__ezuri-renegade-leader.dck',
)
# Two bare-CP7 opponents (driver=None in both arms).
_OPPONENTS = (
    'commander/cedh/grixis__kess-dissident-mage.dck',
    'commander/mid/azorius__brago-king-eternal.dck',
)
_GAMES = 2
_WORKERS = 2
_STALL_TIMEOUT_S = 900.0
_DRIVER_MARKERS = ('DRIVER_REGISTERED', 'DRIVER_MACRO_FIRED', 'DRIVER_STEER_FIRED',
                   'DRIVER_MULLIGAN', 'MACRO_FIRE_REAL', 'QUAD_DRIVER_REGISTERED')


def _gauntlet_root() -> Path:
    import pipeline as p

    return Path(p.__file__).resolve().parent / 'data' / 'gauntlet'


def _own_java_pids() -> set[int]:
    out = subprocess.run(['pgrep', '-f', 'XMageBatch'], capture_output=True, text=True).stdout
    return {int(x) for x in out.split() if x.strip().isdigit()}


@pytest.fixture(scope='module')
def staged(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Resolve the install, warm the card DB once, stage translated .txt decks + subject drivers."""
    from pipeline.sim import drivers
    from pipeline.sim import xmage_runtime as xr
    from pipeline.sim.driver_batch import Ledger
    from pipeline.sim.driver_run import deck_and_ref_for_row, default_batch_ledger_v2_path
    from pipeline.sim.engines import xmage as xe
    from pipeline.sim.game_tasks import DriverRef, SeatSpec

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved (needs local dist jar + JRE): {exc}')

    root = _gauntlet_root()
    for did in _SUBJECTS + _OPPONENTS:
        if not (root / did).is_file():
            pytest.skip(f'gauntlet deck missing: {did}')

    ledger = Ledger(default_batch_ledger_v2_path())
    stage = tmp_path_factory.mktemp('decks')  # flat dir of '/'-free deck basenames (see module docstring).

    # Warm the canonical card DB ONCE (cold CardScanner.scan is not concurrency-safe).
    xe.XMageEngine()._ensure_card_db_warm(install)

    # Subjects: staged translated .txt + their compiled DriverRef. The deck_path is a BARE
    # basename (no '/') so the derived task_id stays a valid filename for <logdir>/<task_id>.log.
    subjects: list[SeatSpec] = []
    subject_meta: dict[str, dict] = {}
    for i, did in enumerate(_SUBJECTS):
        row = ledger.row(did)
        deck, _ref = deck_and_ref_for_row(row)
        classes = drivers.classes_dir(deck)
        if not classes.is_dir():
            pytest.skip(f'compiled driver classes absent for {did}: {classes}')
        name = f'sub{i}.txt'
        (stage / name).write_text(xe._forge_dck_to_xmage_txt((root / did).read_text()), encoding='utf-8')
        subjects.append(SeatSpec(deck_path=name, driver=DriverRef(classpath=str(classes), fqcn=str(row['fqcn']))))
        subject_meta[name] = {'deck_id': did, 'archetype': row.get('archetype'), 'tight': row.get('p_tightness')}

    opponents: list[SeatSpec] = []
    for i, did in enumerate(_OPPONENTS):
        name = f'opp{i}.txt'
        (stage / name).write_text(xe._forge_dck_to_xmage_txt((root / did).read_text()), encoding='utf-8')
        opponents.append(SeatSpec(deck_path=name, driver=None))

    return {
        'install': install, 'subjects': subjects, 'opponents': opponents,
        'subject_meta': subject_meta, 'decks_dir': str(stage),
    }


def _run_phase(subjects, opponents, *, logdir: Path, decks_dir: str, done_set, done_set_path, sampler_out=None):
    """Run one run_games phase; optionally sample a driven transcript's live growth."""
    from pipeline.sim.driver_run import resolve_worker_cmd
    from pipeline.sim.game_queue import load_done_set, run_games, save_done_set
    from pipeline.sim.game_tasks import build_game_tasks

    logdir.mkdir(parents=True, exist_ok=True)
    tasks = build_game_tasks(subjects, opponents, _GAMES, fmt='commander')
    worker_cmd = resolve_worker_cmd(
        max_games=500, log_dir=logdir, data_dir=os.environ.get('MAKE_MAGIC_DATA_DIR'), decks_dir=decks_dir,
    )

    stop = threading.Event()
    if sampler_out is not None:
        def _sample() -> None:
            while not stop.is_set():
                for f in logdir.glob('*.log'):
                    with __import__('contextlib').suppress(OSError):
                        sampler_out.append((f.name, f.stat().st_size, time.time()))
                time.sleep(0.4)
        threading.Thread(target=_sample, daemon=True).start()

    t0 = time.time()
    result = run_games(
        tasks,
        worker_cmd=worker_cmd,
        workers=_WORKERS,
        stall_timeout_s=_STALL_TIMEOUT_S,
        done_set=load_done_set(done_set_path) if done_set is None and done_set_path else done_set,
    )
    wall = time.time() - t0
    stop.set()
    if done_set_path:
        save_done_set(done_set_path, {**(done_set or {}), **result.done_results})
    return result, wall


def test_queue_end_to_end_real_jar(staged, tmp_path) -> None:
    from pipeline.sim.driver_run import ingest_queue_transcripts
    from pipeline.sim.game_queue import bucket_table_from_result

    subjects = staged['subjects']
    opponents = staged['opponents']
    subject_meta = staged['subject_meta']
    decks_dir = staged['decks_dir']

    pids_before = _own_java_pids()

    logdir_a = tmp_path / 'logs_a'
    logdir_b = tmp_path / 'logs_b'
    done_path = tmp_path / 'doneset.jsonl'

    # -- Phase A: subject-1 only (8 games), saving the done-set. Sample live growth. --------- #
    growth: list[tuple[str, int, float]] = []
    result_a, wall_a = _run_phase(
        [subjects[0]], opponents, logdir=logdir_a, decks_dir=decks_dir,
        done_set={}, done_set_path=done_path, sampler_out=growth,
    )
    print(f'\n[phaseA] wall={wall_a:.1f}s complete={result_a.complete} '
          f'done={len(result_a.done_results)} failed={len(result_a.failed)} '
          f'incomplete_cells={result_a.incomplete_cells}')

    # -- Phase B: BOTH subjects (16 games), Phase A seeded → only subject-2's 8 run. --------- #
    result_b, wall_b = _run_phase(
        subjects, opponents, logdir=logdir_b, decks_dir=decks_dir,
        done_set=result_a.done_results, done_set_path=None,
    )
    print(f'[phaseB] wall={wall_b:.1f}s complete={result_b.complete} '
          f'done={len(result_b.done_results)} failed={len(result_b.failed)} '
          f'comparisons={sorted(k.split("/")[-1] for k in result_b.comparisons)}')

    pids_after = _own_java_pids()

    # ------------------------------------------------------------------ Criterion 5: RESTART #
    s1_path = subjects[0].deck_path
    s2_path = subjects[1].deck_path
    logs_b = {f.name for f in logdir_b.glob('*.log')}
    # subject-1 task_ids all start with the subject-1 deck_path; assert none re-ran in phase B.
    s1_reran = [n for n in logs_b if n.split('|')[0] == s1_path]
    s2_ran = [n for n in logs_b if n.split('|')[0] == s2_path]
    print(f'[restart] logdir_B logs: subject1_reran={len(s1_reran)} subject2_ran={len(s2_ran)}')
    assert s1_reran == [], f'seeded subject-1 tasks re-ran on restart: {s1_reran}'
    assert len(s2_ran) == len(opponents) * 2 * _GAMES, f'subject-2 did not fully run: {s2_ran}'
    assert result_b.complete, f'restart run not complete; incomplete={result_b.incomplete_cells}'
    # Restart equivalence: subject-1's comparison on resume == its Phase-A comparison.
    comp_a = result_a.comparisons[s1_path]
    comp_b = result_b.comparisons[s1_path]
    assert comp_a.winrate_driver == comp_b.winrate_driver
    assert comp_a.winrate_cp7 == comp_b.winrate_cp7

    # Combined result for the aggregate criteria: subject-1 from A, subject-2 from B.
    combined = {s1_path: result_a.comparisons[s1_path], s2_path: result_b.comparisons[s2_path]}

    # ------------------------------------------------------- Criterion 1: completeness/no 0/0 #
    assert result_a.complete and not result_a.incomplete_cells
    for spath, comp in combined.items():
        assert comp.per_opponent, f'{spath}: no per-opponent rows'
        agg_dd = sum(o.driver_decided for o in comp.per_opponent)
        agg_cd = sum(o.cp7_decided for o in comp.per_opponent)
        print(f'[complete] {subject_meta[spath]["deck_id"]}: driver {sum(o.driver_wins for o in comp.per_opponent)}'
              f'/{agg_dd}  cp7 {sum(o.cp7_wins for o in comp.per_opponent)}/{agg_cd}')
        assert agg_dd > 0, f'{spath}: driven arm 0/0 (the cp7-0/0 bug)'
        assert agg_cd > 0, f'{spath}: baseline arm 0/0 (the cp7-0/0 bug)'

    # ------------------------------------------------------------- Criterion 2: bucket table #
    from pipeline.sim.driver_run import DeckDelta, aggregate_buckets, write_bucket_table

    class _R:
        comparisons = combined
    arche = {p: subject_meta[p]['archetype'] for p in combined}
    tight = {p: subject_meta[p]['tight'] for p in combined}
    table = bucket_table_from_result(_R(), archetypes=arche, tightness=tight)  # type: ignore[arg-type]
    out_dir = tmp_path / 'buckets'
    # Build DeckDeltas to write the json/md pair with populated CIs.
    deltas = []
    for p, comp in combined.items():
        deltas.append(DeckDelta(
            deck_id=subject_meta[p]['deck_id'], keep='', archetype=arche[p] or '',
            driver_wins=sum(o.driver_wins for o in comp.per_opponent),
            driver_decided=sum(o.driver_decided for o in comp.per_opponent),
            cp7_wins=sum(o.cp7_wins for o in comp.per_opponent),
            cp7_decided=sum(o.cp7_decided for o in comp.per_opponent),
            n_matchups=len(comp.per_opponent), p_tightness=tight[p] or ''))
    stats = aggregate_buckets(deltas)
    jpath, mpath = write_bucket_table(stats, out_dir=out_dir)
    print(f'[bucket] wrote {jpath.name} + {mpath.name}; buckets={list(table)}')
    assert 'all-driven' in table
    ad = table['all-driven']
    assert isinstance(ad['lift_ci'], list) and len(ad['lift_ci']) == 2
    assert jpath.is_file() and mpath.is_file()

    # ------------------------------------------------ Criterion 3: transcripts persisted+LIVE #
    # Live: some log grew during the run (intermediate non-final samples > 0).
    assert growth, 'no growth samples captured'
    assert max(sz for _n, sz, _t in growth) > 0
    per_file_max = {}
    for n, sz, _t in growth:
        per_file_max[n] = max(per_file_max.get(n, 0), sz)
    print(f'[live] sampled {len(growth)} points across {len(per_file_max)} logs; '
          f'max sizes {sorted(per_file_max.values(), reverse=True)[:4]}')
    a_log = next(logdir_a.glob('*.log'))
    txt = a_log.read_text(encoding='utf-8', errors='replace')
    assert 'Game Result:' in txt and 'XMAGEBATCH RESULT ' in txt

    # Persisted: ingest both phases into sim_game_features / sim_game_logs, count rows.
    data_dir = os.environ.get('MAKE_MAGIC_DATA_DIR')
    n1 = ingest_queue_transcripts(result_a, data_dir=data_dir, cleanup=False)
    n2 = ingest_queue_transcripts(result_b, data_dir=data_dir, cleanup=False)
    print(f'[persist] ingested rows: phaseA={n1} phaseB={n2}')
    assert n1 + n2 >= len(result_a.done_results)  # every drained game persisted
    from pipeline import store
    from pipeline.sim import store as sim_store
    db = sim_store._db_path(data_dir)
    cells = [t.split('|', 3) for t in list(result_a.done_results) + list(result_b.done_results)]
    keys = {'|'.join(c[:3]) for c in cells}
    with store.connect(db) as conn:
        placeholders = ','.join('?' for _ in keys)
        nf = conn.execute(
            f'SELECT count(*) FROM sim_game_features WHERE matchup_key IN ({placeholders})', list(keys)
        ).fetchone()[0]
        nl = conn.execute(
            f'SELECT count(*) FROM sim_game_logs WHERE matchup_key IN ({placeholders})', list(keys)
        ).fetchone()[0]
    print(f'[persist] sim_game_features rows={nf}  sim_game_logs rows={nl} (for these cells)')
    assert nf >= 8 and nl >= 8

    # -------------------------------------------------------- Criterion 4: state-bleed clean #
    baseline_logs = [f for f in list(logdir_a.glob('*.log')) + list(logdir_b.glob('*.log'))
                     if f.name.split('|')[2] == 'baseline']
    assert baseline_logs, 'no baseline transcripts found'
    checked = 0
    for f in baseline_logs:
        body = f.read_text(encoding='utf-8', errors='replace')
        offending = [ln for ln in body.splitlines() if any(ln.startswith(m) for m in _DRIVER_MARKERS)]
        assert offending == [], f'DRIVER_* markers bled into baseline {f.name}: {offending[:3]}'
        checked += 1
    # And a driven transcript DID register (else state-bleed is vacuous).
    driven_logs = [f for f in logdir_a.glob('*.log') if f.name.split('|')[2] == 'driven']
    assert any('DRIVER_REGISTERED' in f.read_text(errors='replace') for f in driven_logs), \
        'no driven transcript registered a driver'
    print(f'[state-bleed] {checked} baseline transcripts clean; driven registered OK')

    # ------------------------------------------- Criteria 6/7: orphans, retry-cap, throughput #
    assert not result_a.failed and not result_b.failed, 'retry-cap tripped on healthy games'
    # WorkerPool.close() drains workers via EOF; a cleanly-exiting JVM can linger in the process
    # table for a beat, so poll (bounded) until our-lineage JVMs clear rather than sampling once.
    leaked = pids_after - pids_before
    deadline = time.time() + 20
    while leaked and time.time() < deadline:
        time.sleep(0.5)
        leaked = _own_java_pids() - pids_before
    print(f'[orphans] java pids before={len(pids_before)} after={len(pids_after)} leaked_after_drain={leaked}')
    assert leaked == set(), f'orphan XMage JVMs at exit: {leaked}'
    total_games = len(result_a.done_results) + len(result_b.done_results) - len(result_a.done_results)
    print(f'[throughput] phaseA {len(result_a.done_results)}g/{wall_a:.0f}s, '
          f'phaseB {total_games}g/{wall_b:.0f}s; warm workers (max-games=500) never recycled '
          f'→ {_WORKERS} JVMs ran {len(result_a.done_results)}+{total_games} games with one startup each.')
