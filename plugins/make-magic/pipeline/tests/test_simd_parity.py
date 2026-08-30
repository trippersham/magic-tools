"""Phase A3 Step 2 — the CUTOVER PARITY GATE.

Replays a FIXED, deterministic frozen done-set through BOTH aggregation paths:

* OLD: the retired ``game_queue._Governor`` (seeded with the done-set on construction), whose
  per-subject ``PilotingComparison`` rows are the historical driver-study unit.
* NEW: the shared :func:`pipeline.sim.aggregate.aggregate_results` that the simd live path (A3)
  folds a ``SimdRunResult.results`` map through.

The two MUST produce identical decisive counts and Wilson intervals — that is the exit criterion
gating the deletion of the old stack. The frozen GOLDEN values are asserted directly too, so this
test keeps proving parity after the old ``_Governor`` is deleted (the ported-forward invariant).
"""

from __future__ import annotations

from pipeline.sim.aggregate import BAILOUT_HARD_FLOOR_MS, aggregate_results, bucket_table_from_comparisons
from pipeline.sim.core import wilson_ci
from pipeline.sim.game_protocol import GameResult
from pipeline.sim.game_tasks import SeatSpec, build_game_tasks

_FLOOR = BAILOUT_HARD_FLOOR_MS


def _subjects(names: list[str]) -> list[SeatSpec]:
    return [SeatSpec(deck_path=f'/d/{n}.dck', driver=None) for n in names]


def _sid(name: str) -> str:
    return f'/d/{name}.dck'


def _frozen_doneset() -> tuple[list, dict[str, GameResult]]:
    """A deterministic 2-subject x 2-opponent x 4-game done-set with a mix of decisive wins,
    draws, an unknown-winner ('none') non-decisive game, and a sub-floor bailout."""
    subjects = _subjects(['sa', 'sb'])
    field = _subjects(['oa', 'ob'])
    tasks = build_game_tasks(subjects, field, 4, fmt='commander')

    # Deterministic per-(cell) winner scripts. 'a' = subject win, 'b' = opp win, 'draw' = draw.
    # A sub-floor ms game (bailout) and a reason='timeout' game are non-decisive → excluded.
    _m = 60000  # a plausible real-game wall-clock (ms).
    scripts: dict[tuple[str, str], list[tuple[str, int, str | None]]] = {
        ('driven', _sid('oa')): [('a', _m, None), ('a', _m, None), ('a', _m, None), ('b', _m, None)],
        ('baseline', _sid('oa')): [('a', _m, None), ('b', _m, None), ('b', _m, None), ('b', _m, None)],
        ('driven', _sid('ob')): [('a', _m, None), ('a', _m, None), ('draw', _m, None), ('a', 500, None)],
        ('baseline', _sid('ob')): [('b', _m, None), ('b', _m, None), ('a', _m, None), ('none', _m, 'timeout')],
    }
    results: dict[str, GameResult] = {}
    # Index per (subject, opp, pil) into its script.
    idx: dict[tuple[str, str, str], int] = {}
    for t in tasks:
        s, o, p = t.task_id.split('|')[:3]
        # Only the first subject (sa) is fully scripted; sb re-uses the same scripts (deterministic).
        key = (p, o)
        i = idx.get((s, o, p), 0)
        idx[(s, o, p)] = i + 1
        winner, ms, reason = scripts[key][i]
        results[t.task_id] = GameResult(
            task_id=t.task_id, winner=winner, kill_turn=8, ms=ms, markers=[], log_path=None, reason=reason
        )
    return tasks, results


def test_old_vs_new_aggregation_parity() -> None:
    """OLD _Governor and NEW aggregate_results agree on every subject's comparison + buckets.

    The old ``game_queue._Governor`` is DELETED at the A3 cutover — this cross-check ran green as
    the exit gate (see the phase note) and now self-skips; :func:`test_frozen_golden_survives_deletion`
    carries the frozen golden forward so parity stays proven."""
    import pytest

    game_queue = pytest.importorskip('pipeline.sim.game_queue', reason='old stack deleted at A3 cutover')
    _Governor = game_queue._Governor

    tasks, results = _frozen_doneset()

    old_gov = _Governor(
        tasks,
        retry_cap=2,
        monitor=None,
        done_set=results,
        on_subject_complete=None,
        cond_poll_s=0.01,
        bailout_floor_ms=_FLOOR,
    )
    old = old_gov.result().comparisons

    new = aggregate_results(tasks, results, bailout_floor_ms=_FLOOR).comparisons()

    assert set(old) == set(new)
    for subject in old:
        o, n = old[subject], new[subject]
        assert o.winrate_driver == n.winrate_driver
        assert o.winrate_cp7 == n.winrate_cp7
        assert o.winrate_delta == n.winrate_delta
        assert o.winrate_driver_ci == n.winrate_driver_ci
        assert o.winrate_cp7_ci == n.winrate_cp7_ci
        assert len(o.per_opponent) == len(n.per_opponent)
        for op_o, op_n in zip(o.per_opponent, n.per_opponent, strict=True):
            assert (op_o.driver_wins, op_o.driver_decided) == (op_n.driver_wins, op_n.driver_decided)
            assert (op_o.cp7_wins, op_o.cp7_decided) == (op_n.cp7_wins, op_n.cp7_decided)
            assert op_o.winrate_driver_ci == op_n.winrate_driver_ci

    # Bucket table identical too (the study's reported unit).
    old_buckets = bucket_table_from_comparisons(old)
    new_buckets = bucket_table_from_comparisons(new)
    assert old_buckets == new_buckets


def test_frozen_golden_survives_deletion() -> None:
    """The GOLDEN aggregation of the frozen done-set — hard-coded so parity is proven even after
    the old _Governor is deleted (bailout + draw + unknown-winner all excluded from W/L)."""
    tasks, results = _frozen_doneset()
    comps = aggregate_results(tasks, results, bailout_floor_ms=_FLOOR).comparisons()

    for subject in (_sid('sa'), _sid('sb')):
        comp = comps[subject]
        by_opp = {o.opponent: o for o in comp.per_opponent}
        # oa driven: a,a,a,b → 3/4 ; baseline: a,b,b,b → 1/4.
        assert (by_opp[_sid('oa')].driver_wins, by_opp[_sid('oa')].driver_decided) == (3, 4)
        assert (by_opp[_sid('oa')].cp7_wins, by_opp[_sid('oa')].cp7_decided) == (1, 4)
        # ob driven: a,a,draw,bailout(500ms) → draw+bailout excluded → 2/2 decided.
        assert (by_opp[_sid('ob')].driver_wins, by_opp[_sid('ob')].driver_decided) == (2, 2)
        # ob baseline: b,b,a,timeout(none) → timeout excluded → 1/3 decided.
        assert (by_opp[_sid('ob')].cp7_wins, by_opp[_sid('ob')].cp7_decided) == (1, 3)
        # Pooled driver: (3+2)/(4+2)=5/6 ; pooled cp7: (1+1)/(4+3)=2/7.
        assert comp.winrate_driver_ci == wilson_ci(5, 6)
        assert comp.winrate_cp7_ci == wilson_ci(2, 7)
