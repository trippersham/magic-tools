"""Phase A2 (top-up dispatch) — non-decisive games get bounded REPLACEMENT tasks so a cell can
actually reach ``ok >= needed`` decisive games (NO JVM — the fake echo-worker drives every
scenario via real subprocesses; structural assertions only).

The gap this closes: a game that resolves NON-DECISIVE (``reason=bailout`` / ``timeout``) consumes
its task slot without incrementing ``ok``. With a fixed task universe nothing replaces it, so any
cell holding a bailout can NEVER reach completeness. Top-up dispatch enqueues a fresh replacement
task for the same cell on every non-decisive commit, bounded by a per-cell cap so a pathological
all-bailout cell still TERMINATES (flagged exhausted, never silently "complete").

The FOUR integration gates below are the acceptance contract:

1. **Top-up completes a short cell** — 1 bailout + decisive games → the cell reaches ok=needed via
   exactly one top-up; total tasks = needed+1; ``complete=True``.
2. **Bounded termination** — an all-bailout cell terminates at the top-up cap, is flagged
   exhausted (never "complete"), and the run ENDS while the other cells complete (no infinite loop).
3. **Resume top-up** — an ops.duckdb carrying a short cell (endurance state) reconstructs → the
   engine enqueues exactly the missing top-ups, re-runs ZERO decisive games, and the cell completes.
4. **Fairness preserved** — a bailout-heavy subject (spawning many top-ups) cannot starve the
   other subjects; they all complete and the run terminates.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pipeline.sim.game_protocol import GameResult
from pipeline.sim.game_tasks import SeatSpec, build_game_tasks
from pipeline.sim.simd.engine import SimdRunResult, run_games_simd
from pipeline.sim.simd.ops_store import OpsStore

_FAKE = str(Path(__file__).parent / 'fake_echo_worker.py')


def _cmd() -> list[str]:
    return [sys.executable, _FAKE]


def _seat(name: str) -> SeatSpec:
    return SeatSpec(deck_path=f'/d/{name}.dck', driver=None)


def _sid(name: str) -> str:
    return f'/d/{name}.dck'


def _subjects(names: list[str]) -> list[SeatSpec]:
    return [_seat(n) for n in names]


def _win(task_id: str) -> GameResult:
    return GameResult(task_id=task_id, winner='a', kill_turn=3, ms=60000, markers=[], log_path=None)


# --------------------------------------------------------------------------- #
# GATE 1 — a single bailout is replaced by a top-up; the cell completes.
# --------------------------------------------------------------------------- #


def test_integration_topup_completes_short_cell(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    runlog = tmp_path / 'runlog.txt'
    needed = 3
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), needed, fmt='commander')

    # game_index 0 of EVERY cell bails once; every other game (and the top-up) is decisive.
    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_BAILOUT_ON_INDEX': '0', 'FAKE_RUNLOG': str(runlog)}

    res = run_games_simd(
        tasks, worker_cmd=_cmd(), workers=2, stall_timeout_s=30.0,
        ops_db_path=db, attempt_cap=2, env_for_worker=env_for, join_timeout_s=60.0,
    )
    assert isinstance(res, SimdRunResult)
    assert res.complete, res.incomplete_cells
    assert not res.exhausted_cells
    # Every cell reached exactly ``needed`` DECISIVE games ...
    for (subj, opp, pil), (ok, need) in res.cells.items():
        assert ok == need == needed, (subj, opp, pil, ok, need)
    # ... and each of the two cells ran needed+1 games (the one bailout + its replacement).
    ran = runlog.read_text().split()
    assert len(ran) == 2 * (needed + 1), ran
    # A top-up task_id carries the ``topup-`` game-index marker and parses to a real cell.
    topups = [t for t in res.results if t.split('|')[3].startswith('topup-')]
    assert len(topups) == 2, topups
    for tid in topups:
        # A top-up id stays 4-field so cell_key / _cell_of parse it to the original cell.
        assert tid.count('|') == 3
        assert tid.split('|')[:3] in ([_sid('sa'), _sid('oa'), 'driven'], [_sid('sa'), _sid('oa'), 'baseline'])


# --------------------------------------------------------------------------- #
# GATE 2 — an all-bailout cell terminates at the cap (exhausted), others complete.
# --------------------------------------------------------------------------- #


def test_integration_all_bailout_cell_terminates_at_cap(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    needed = 2
    topup_cap = 2  # per-cell cap = topup_cap * needed = 4 replacements max.
    # sa bails on EVERY game (originals + top-ups); sb is healthy and must fully complete.
    tasks = build_game_tasks(_subjects(['sa', 'sb']), _subjects(['oa']), needed, fmt='commander')

    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_BAILOUT_ON_SUBJECT': _sid('sa')}

    res = run_games_simd(
        tasks, worker_cmd=_cmd(), workers=2, stall_timeout_s=30.0,
        ops_db_path=db, attempt_cap=2, topup_cap=topup_cap,
        env_for_worker=env_for, join_timeout_s=60.0,
    )
    # The run TERMINATED (no infinite top-up loop): the healthy subject sb completed every cell.
    for opp in (_sid('oa'),):
        for pil in ('driven', 'baseline'):
            ok, need = res.cells[(_sid('sb'), opp, pil)]
            assert ok == need == needed, (opp, pil, ok, need)
    # sa's cells are EXHAUSTED (capped), flagged distinctly, and never falsely "complete".
    sa_cells = [(_sid('sa'), _sid('oa'), pil) for pil in ('driven', 'baseline')]
    for cell in sa_cells:
        ok, need = res.cells[cell]
        assert ok == 0 and need == needed, cell
        assert cell in res.exhausted_cells, cell
    # ``complete`` means EVERY cell reached ok>=needed — no exhaustion exception. An exhausted cell
    # is terminal-but-incomplete: the run TERMINATES (bounded top-up) yet reports complete=False, and
    # the exhausted cells are surfaced DISTINCTLY so partial science can never be blessed as complete.
    assert not res.complete
    assert set(res.incomplete_cells) >= set(sa_cells)
    assert res.exhausted_cells and set(res.exhausted_cells) == set(sa_cells)
    # Bounded: each exhausted cell ran at most needed + cap*needed tasks.
    for cell in sa_cells:
        ran_in_cell = [
            t for t in res.results
            if (t.split('|')[0], t.split('|')[1], t.split('|')[2]) == cell
        ]
        assert len(ran_in_cell) <= needed + topup_cap * needed, (cell, len(ran_in_cell))
        assert len(ran_in_cell) == needed + topup_cap * needed, (cell, len(ran_in_cell))


# --------------------------------------------------------------------------- #
# GATE 3 — resume: an ops.duckdb with a short cell enqueues exactly the missing top-ups.
# --------------------------------------------------------------------------- #


def test_integration_resume_tops_up_short_cell_zero_rerun(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    runlog = tmp_path / 'runlog.txt'
    needed = 3
    tasks = build_game_tasks(_subjects(['sa']), _subjects(['oa']), needed, fmt='commander')

    # Build an ops.duckdb by hand that SIMULATES a torn-down endurance run: the ``driven`` cell is
    # short by one (2 decisive + 1 bailout); the ``baseline`` cell is fully decisive.
    driven = [t for t in tasks if t.task_id.split('|')[2] == 'driven']
    baseline = [t for t in tasks if t.task_id.split('|')[2] == 'baseline']
    decisive_ids: set[str] = set()
    with OpsStore(db) as ops:
        ops.register_tasks(tasks)
        for t in baseline:
            ops.record_result(_win(t.task_id))
            decisive_ids.add(t.task_id)
        for t in driven[:-1]:
            ops.record_result(_win(t.task_id))
            decisive_ids.add(t.task_id)
        # The last driven game bailed (non-decisive) — the cell is short by exactly one.
        ops.record_result(GameResult(
            task_id=driven[-1].task_id, winner='none', kill_turn=None, ms=60000,
            markers=[], log_path=None, reason='bailout',
        ))

    # Reconstruct over the same ops.duckdb with a HEALTHY worker: it must enqueue exactly the one
    # missing top-up, re-run ZERO decisive games, and complete the run.
    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_RUNLOG': str(runlog)}

    res = run_games_simd(
        tasks, worker_cmd=_cmd(), workers=2, stall_timeout_s=30.0,
        ops_db_path=db, attempt_cap=2, env_for_worker=env_for, join_timeout_s=60.0,
    )
    assert res.complete, res.incomplete_cells
    # Every cell now holds ``needed`` decisive games.
    for (subj, opp, pil), (ok, need) in res.cells.items():
        assert ok == need == needed, (subj, opp, pil, ok, need)
    # ZERO decisive games were re-run: the runlog holds only freshly-run top-up ids.
    ran = runlog.read_text().split() if runlog.exists() else []
    assert decisive_ids.isdisjoint(set(ran)), f'a committed decisive game re-ran: {set(ran) & decisive_ids}'
    # Exactly one top-up ran (the single missing decisive game for the driven cell).
    assert len(ran) == 1, ran
    assert ran[0].split('|')[3].startswith('topup-'), ran
    assert ran[0].split('|')[:3] == [_sid('sa'), _sid('oa'), 'driven'], ran


# --------------------------------------------------------------------------- #
# GATE 4 — fairness: a bailout-heavy subject cannot starve the others.
# --------------------------------------------------------------------------- #


def test_integration_topup_fairness_bailout_subject_cannot_starve(tmp_path) -> None:
    db = tmp_path / 'ops.duckdb'
    needed = 2
    # sa bails on every game → spawns top-ups up to the cap; sb, sc are healthy.
    subjects = _subjects(['sa', 'sb', 'sc'])
    field = _subjects(['oa', 'ob'])
    tasks = build_game_tasks(subjects, field, needed, fmt='commander')

    def env_for(_idx: int) -> dict[str, str]:
        return {'FAKE_BAILOUT_ON_SUBJECT': _sid('sa')}

    res = run_games_simd(
        tasks, worker_cmd=_cmd(), workers=3, stall_timeout_s=30.0,
        ops_db_path=db, attempt_cap=2, topup_cap=2, env_for_worker=env_for, join_timeout_s=90.0,
    )
    # Both HEALTHY subjects completed EVERY cell despite sa's top-up storm (no starvation).
    for subj in (_sid('sb'), _sid('sc')):
        for opp in (_sid('oa'), _sid('ob')):
            for pil in ('driven', 'baseline'):
                ok, need = res.cells[(subj, opp, pil)]
                assert ok == need == needed, (subj, opp, pil, ok, need)
    # sa's cells are all exhausted (bounded) and the run terminated.
    for opp in (_sid('oa'), _sid('ob')):
        for pil in ('driven', 'baseline'):
            cell = (_sid('sa'), opp, pil)
            assert cell in res.exhausted_cells, cell
