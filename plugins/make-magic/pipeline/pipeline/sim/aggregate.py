"""Phase A3 — the shared driver-study aggregation (extracted from the retired ``game_queue``).

The consolidation cutover (A3) deletes the old per-matchup runner + the ``game_queue`` governor,
but their *aggregation* is domain science that both the old and the new simd paths must produce
IDENTICALLY (the parity exit gate). This module is that single owner: the A1 plausibility gate
(:func:`bailout_reason` + its floors) and the per-subject Wilson-CI aggregation
(:class:`RunAggregator`) that folds a ``{task_id: GameResult}`` map into per-subject
:class:`~pipeline.sim.driver_compare.PilotingComparison` rows + the rule-8 bucket table.

It reinvents NO statistics — the per-opponent / pooled Wilson math is
:func:`pipeline.sim.driver_compare._rates` (→ :func:`pipeline.sim.core.wilson_ci`) verbatim, and
the bucket rollup is :func:`pipeline.sim.driver_run.aggregate_buckets`. The simd scheduler
(live tallying) and this module (post-hoc aggregation over the committed results) share the same
``_winner_bucket`` + ``bailout_reason`` logic, which is what makes old-vs-new parity exact.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from pipeline.sim.driver_compare import OpponentComparison, PilotingComparison, _rates

if TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Mapping

    from pipeline.sim.game_protocol import GameResult
    from pipeline.sim.game_tasks import GameTask

_SEP = '|'


def _cell_of(task_id: str) -> tuple[str, str, str]:
    """The ``(subject, opponent, piloting)`` cell key parsed from a ``task_id``."""
    parts = task_id.split(_SEP)
    if len(parts) != 4:
        msg = f'malformed task_id (expected 4 {_SEP!r}-fields): {task_id!r}'
        raise ValueError(msg)
    return parts[0], parts[1], parts[2]

__all__ = (
    'BAILOUT_HARD_FLOOR_MS',
    'BAILOUT_SUSPICIOUS_MS',
    'RunAggregator',
    'aggregate_results',
    'bailout_reason',
    'bucket_table_from_comparisons',
)

#: Wall-clock floor (ms) below which a reported game is physically impossible for a real
#: multi-turn Commander game (the audit found sub-2s "turn-12 wins" — engine bailouts scored as
#: fake wins). A game under this floor is a HARD DROP: marked non-decisive ``reason=bailout`` and
#: excluded from W/L/D (see :func:`bailout_reason`). Audit-calibrated.
BAILOUT_HARD_FLOOR_MS = 2000

#: Wall-clock floor (ms) below which a game is SUSPICIOUSLY fast — kept in W/L (decisive) but
#: logged so the coverage report can flag it. Not an exclusion (a genuinely fast game is possible).
BAILOUT_SUSPICIOUS_MS = 10000

#: Transcript markers that independently signal a non-decisive engine bailout (mulligan-to-
#: nothing / concede / engine error), regardless of wall-clock.
_BAILOUT_MARKERS = frozenset({'concede', 'conceded', 'mulligan_to_nothing', 'engine_error', 'engine-error'})

#: A goldfish own-turn lens is not run on the queue (gauntlet) path — the ``PilotingComparison``
#: own-turn fields carry the harness's "never killed / not measured" sentinel + a warning.
_NO_GOLDFISH = -1.0


def bailout_reason(result: GameResult, *, hard_floor_ms: int = BAILOUT_HARD_FLOOR_MS) -> str | None:
    """Return ``'bailout'`` for an implausibly-fast / engine-bailed game, else ``None``.

    A game is a bailout when its wall-clock is below ``hard_floor_ms`` OR it carries a
    concede/mulligan-to-nothing/engine-error marker. A bailout is non-decisive: it must NEVER be
    credited as a win (the fix for the audit's 24 % fake-win rate). Already-non-decisive games
    (``reason`` set) are left as-is (returns ``None`` — the caller keeps the existing reason)."""
    if result.reason is not None:
        return None
    if result.ms < hard_floor_ms:
        return 'bailout'
    lowered = {m.strip().lower() for m in result.markers}
    if lowered & _BAILOUT_MARKERS:
        return 'bailout'
    return None


def _winner_bucket(winner: str) -> str:
    """Normalise a ``GameResult.winner`` to ``'a'`` (subject) / ``'b'`` (opponent) / ``'draw'``.

    Draws are excluded from the Wilson denominator (the decided-games rule the existing
    aggregation uses); anything unrecognised — including ``'none'`` (a non-decisive
    wall-clock-timeout game) — is treated as a draw (no win credited)."""
    w = winner.strip().lower()
    if w in ('a', 'subject', 'player_a', 'playera'):
        return 'a'
    if w in ('b', 'opponent', 'player_b', 'playerb'):
        return 'b'
    return 'draw'


class _Cell:
    """One ``(subject, opponent, piloting)`` cell's tally (valid fills vs bad fills)."""

    __slots__ = ('failed', 'needed', 'nondecisive', 'ok', 'wins_a', 'wins_b')

    def __init__(self) -> None:
        self.needed = 0
        self.ok = 0  # valid terminal games (gate-passed) — the completeness numerator.
        self.nondecisive = 0  # bailout/timeout games: counted in coverage, excluded from W/L.
        self.failed = 0  # tasks that burned their budget / were quarantined (no result).
        self.wins_a = 0
        self.wins_b = 0


class RunAggregator:
    """Fold a run's game outcomes into per-subject Wilson comparisons + coverage.

    Pure aggregation (no scheduling, no threads): built from the task universe, fed each
    completed :class:`~pipeline.sim.game_protocol.GameResult` (and each failed/quarantined
    ``task_id``), then queried for the per-subject
    :class:`~pipeline.sim.driver_compare.PilotingComparison` rows and coverage. Reproduces the
    retired ``game_queue._Governor`` aggregation EXACTLY — the old-vs-new parity contract.
    """

    def __init__(self, tasks: Iterable[GameTask | str], *, bailout_floor_ms: int = 0) -> None:
        self._bailout_floor_ms = bailout_floor_ms
        self._cells: dict[tuple[str, str, str], _Cell] = {}
        self._subject_order: list[str] = []
        self._subject_opponents: dict[str, list[str]] = defaultdict(list)
        for t in tasks:
            task_id = t if isinstance(t, str) else t.task_id
            subject, opp, pil = _cell_of(task_id)
            key = (subject, opp, pil)
            if subject not in self._subject_order:
                self._subject_order.append(subject)
            self._cells.setdefault(key, _Cell())
            self._cells[key].needed += 1
            if opp not in self._subject_opponents[subject]:
                self._subject_opponents[subject].append(opp)

    def add_result(self, res: GameResult) -> None:
        """Tally one completed game (non-decisive/bailout → coverage only, credits neither seat)."""
        subject, opp, pil = _cell_of(res.task_id)
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

    def add_failed(self, task_id: str) -> None:
        """Record a task that burned its budget / was quarantined (leaves its cell under-filled)."""
        subject, opp, pil = _cell_of(task_id)
        self._cells[(subject, opp, pil)].failed += 1

    # -- comparisons (REUSES driver_compare helpers — no reinvented Wilson math) -- #

    def _aggregate_subject(self, subject: str) -> PilotingComparison:
        per_opponent: list[OpponentComparison] = []
        tot_dw = tot_dd = tot_cw = tot_cd = 0
        warnings: list[str] = []
        for opp in self._subject_opponents[subject]:
            driven = self._cells.get((subject, opp, 'driven')) or _Cell()
            baseline = self._cells.get((subject, opp, 'baseline')) or _Cell()
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
                if c.ok < c.needed:
                    warnings.append(
                        f'cell ({subject},{opp},{pil}) under-filled: {c.ok}/{c.needed} valid games '
                        f'({c.nondecisive} non-decisive, {c.failed} failed)'
                    )
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

    def comparisons(self) -> dict[str, PilotingComparison]:
        """``{subject: PilotingComparison}`` for every subject, in task-build order."""
        return {s: self._aggregate_subject(s) for s in self._subject_order}

    # -- coverage ---------------------------------------------------------- #

    def cells(self) -> dict[tuple[str, str, str], tuple[int, int]]:
        """``{cell: (valid_fills, needed)}`` — ``ok`` is the completeness numerator."""
        return {k: (c.ok, c.needed) for k, c in self._cells.items()}

    def incomplete_cells(self) -> list[tuple[str, str, str]]:
        return [k for k, c in self._cells.items() if c.ok < c.needed]

    def failed_cells(self) -> list[tuple[str, str, str]]:
        """Cells left under-filled specifically by failed/quarantined or non-decisive games."""
        return [k for k, c in self._cells.items() if c.ok < c.needed and (c.failed or c.nondecisive)]

    def complete(self) -> bool:
        return not self.incomplete_cells()


def aggregate_results(
    tasks: Iterable[GameTask],
    results: Mapping[str, GameResult],
    *,
    quarantined: Iterable[str] = (),
    bailout_floor_ms: int = 0,
) -> RunAggregator:
    """Build a :class:`RunAggregator` fed with every ``result`` + every quarantined task_id.

    The single entry point the simd live path (A3) uses to fold a
    :class:`~pipeline.sim.simd.scheduler.SimdRunResult`'s committed ``results`` into per-subject
    comparisons + coverage. ``quarantined`` marks the poison-latched tasks so their cells surface
    as failed (never falsely complete)."""
    agg = RunAggregator(tasks, bailout_floor_ms=bailout_floor_ms)
    for res in results.values():
        agg.add_result(res)
    for tid in quarantined:
        if tid not in results:
            agg.add_failed(tid)
    return agg


def bucket_table_from_comparisons(
    comparisons: Mapping[str, PilotingComparison],
    *,
    out_dir: str | os.PathLike[str] | None = None,
    archetypes: Mapping[str, str] | None = None,
    tightness: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Fold per-subject comparisons into the rule-8 bucket table (REUSES
    :func:`pipeline.sim.driver_run.aggregate_buckets`). ``archetypes`` / ``tightness`` map
    subject → tag; when ``out_dir`` is given the ``buckets.json`` + ``.md`` pair is written."""
    from pipeline.sim.driver_run import DeckDelta, aggregate_buckets, write_bucket_table

    arche = archetypes or {}
    tight = tightness or {}
    deltas: list[DeckDelta] = []
    for subject, comp in comparisons.items():
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
