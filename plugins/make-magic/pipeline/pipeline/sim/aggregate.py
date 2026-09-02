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
from enum import Enum
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
    'Validity',
    'aggregate_results',
    'bailout_reason',
    'bucket_table_from_comparisons',
    'classify_validity',
    'is_concede',
    'is_fast_game',
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


class Validity(Enum):
    """The terminal-cause validity bucket a game's outcome falls into.

    Supersedes the ``ms<2000`` wall-clock proxy as the correctness predicate: time is *triage*, not
    correctness (all 76 sub-2s games in the endurance corpus had assignable terminal causes — real
    fast kills + unpaid-Pact rule losses the floor wrongly excluded, and synthetic macro wins a
    legal cause rejects). See the three-model bailout synthesis.
    """

    #: A legitimate decided or drawn outcome (lethal/commander-damage/deck-out/poison/rule loss, or
    #: a real draw) — a VALID cell fill counted toward completeness; the winner credits the seat
    #: (a draw credits neither). The fast real kills + unpaid-Pact losses come BACK into the data.
    DECISIVE = 'decisive'
    #: Excluded from W/L and TOPPED UP: an engine-defective livelock (``timeout``) or a refused
    #: driver (``driver-rejected``) — no legal terminal event, so the cell needs a replacement.
    NONDECISIVE = 'nondecisive'
    #: Excluded, topped up, AND flagged loudly: a decisive claim with NO legal terminal cause
    #: (macro-game-over) or an ``unknown``/unclassifiable cause on a game that claims a winner. Must
    #: be near-zero; a nonzero count is a data-integrity alarm (a driver fabricating a win).
    INVALID = 'invalid'


#: Terminal causes that are legitimate decided/drawn outcomes → a VALID cell fill (``DECISIVE``).
#: ``draw_game`` is a valid fill whose winner (``DRAW``/``none``) credits neither seat — excluded
#: from the W/L denominator exactly like today, but NOT topped up (the cell is filled).
#: ``concede`` is a rules-legal loss (CR 104.3a — the conceder is the loser, the winner credited
#: normally); XMage's CP7 AI concedes hopeless positions on ordinary games, so a concession is a
#: decisive cell fill (NOT invalid data), tracked separately as a data-quality signal.
_DECISIVE_CAUSES = frozenset(
    {
        'lethal_damage',
        'commander_damage',
        'draw_empty_library',
        'poison',
        'rule_loss',
        'state_loss',
        'draw_game',
        'concede',
    }
)

#: The terminal cause emitted for a concession (a rules-legal loss). Decisive (in ``_DECISIVE_CAUSES``)
#: yet surfaced in coverage: a matchup that concedes a lot is a data-quality signal worth monitoring.
_CONCEDE_CAUSE = 'concede'

#: Terminal causes that are non-decisive (excluded from W/L + a top-up dispatched).
_NONDECISIVE_CAUSES = frozenset({'timeout'})


def classify_validity(result: GameResult, *, hard_floor_ms: int = BAILOUT_HARD_FLOOR_MS) -> Validity:
    """Bucket one game by its terminal cause — the correctness predicate replacing the ms floor.

    * A ``None`` ``end_cause`` is a LEGACY row predating the terminal-cause jar — fall back to the
      OLD ms-floor heuristic (:func:`bailout_reason`) so replaying legacy data still works
      (backward compat: the real-2265 replay reproduces its exact expectations). The legacy path is
      LEFT EXACTLY AS-IS (reason → non-decisive, else ms-floor) — the cross-field invariants below
      apply only to the NEW terminal-cause path so the locked replay is untouched.
    * NEW terminal-cause path — enforce cross-field (cause ↔ winner ↔ reason) consistency, so a
      worker/driver misbehaving on ONE field can never slip a fabricated credit past the classifier:
      a credited winner (``A``/``B``) REQUIRES a decisive cause and no ``reason``; a ``draw_game``
      credits neither seat; a non-decisive ``reason``/``timeout`` must credit no winner. A known
      decisive cause with a credited winner → ``DECISIVE``; ``timeout`` (no credit) → ``NONDECISIVE``;
      any inconsistency, unknown cause, or macro-game-over → ``INVALID`` (a loud data-integrity alarm).
    """
    if result.end_cause is None:
        # Legacy row: no terminal cause emitted — the OLD ms-floor heuristic still governs it, byte
        # for byte (no cross-field change), so the frozen 2265-game replay reproduces exactly.
        if result.reason is not None:
            return Validity.NONDECISIVE
        if bailout_reason(result, hard_floor_ms=hard_floor_ms) is not None:
            return Validity.NONDECISIVE
        return Validity.DECISIVE

    credited = _winner_bucket(result.winner) in ('a', 'b')
    if result.reason is not None:
        # A non-decisive reason (timeout / driver-rejected) must credit NO winner; a credited winner
        # paired with a non-decisive reason is a cross-field contradiction → INVALID.
        return Validity.INVALID if credited else Validity.NONDECISIVE
    c = result.end_cause.strip().lower()
    if c in _NONDECISIVE_CAUSES:
        # timeout: non-decisive, must credit no winner.
        return Validity.INVALID if credited else Validity.NONDECISIVE
    if c in _DECISIVE_CAUSES:
        if c == 'draw_game':
            # A draw credits neither seat; a credited winner on a draw cause → INVALID.
            return Validity.INVALID if credited else Validity.DECISIVE
        # Every other decisive cause REQUIRES a credited winner (a decisive cause with winner=none is
        # a silent draw the classifier would otherwise fold in with no credit).
        return Validity.DECISIVE if credited else Validity.INVALID
    # A decisive claim with no legal terminal cause (macro-game-over) or an unknown cause.
    return Validity.INVALID


def is_concede(result: GameResult) -> bool:
    """True for a concession (``end_cause=concede``) — a decisive game surfaced as a data-quality flag.

    A concession is a rules-legal loss (CR 104.3a) counted in W/L like any decisive cause; this flag
    only makes concede-heavy matchups visible in coverage (an AI that concedes a lot is a signal)."""
    cause = result.end_cause
    return cause is not None and cause.strip().lower() == _CONCEDE_CAUSE


def is_fast_game(result: GameResult) -> bool:
    """True for a suspiciously-fast (< hard floor) game — an INFORMATIONAL triage flag only.

    No longer a correctness predicate (that is :func:`classify_validity`); surfaced in coverage so a
    genuinely-fast decisive kill is visible without being excluded."""
    return result.ms < BAILOUT_HARD_FLOOR_MS


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
    """One ``(subject, opponent, piloting)`` cell's tally (valid fills vs bad fills).

    The ``*_sa`` / ``*_sb`` / ``*_su`` counters split the decisive W/L by WHO TOOK THE FIRST TURN
    (``sa`` = subject seat A started, ``sb`` = opponent seat B started, ``su`` = starter unknown /
    a legacy row) so the first-player effect is measurable per arm.
    """

    __slots__ = (
        'concede', 'failed', 'fast', 'invalid', 'needed', 'nondecisive', 'ok',
        'wins_a', 'wins_a_sa', 'wins_a_sb', 'wins_a_su', 'wins_b', 'wins_b_sa', 'wins_b_sb', 'wins_b_su',
    )

    def __init__(self) -> None:
        self.needed = 0
        self.ok = 0  # valid terminal games (cause-classified DECISIVE) — the completeness numerator.
        self.nondecisive = 0  # timeout/legacy-bailout/INVALID games: in coverage, excluded from W/L.
        self.invalid = 0  # subset of nondecisive: a decisive claim with NO legal cause (loud alarm).
        self.fast = 0  # informational: DECISIVE games under the fast-game floor (a genuinely fast kill).
        self.concede = 0  # subset of ok: decisive concessions (data-quality signal, still counted in W/L).
        self.failed = 0  # tasks that burned their budget / were quarantined (no result).
        self.wins_a = 0
        self.wins_b = 0
        # Decisive W/L split by starter seat: A-started (sa), B-started (sb), unknown/legacy (su).
        self.wins_a_sa = 0
        self.wins_a_sb = 0
        self.wins_a_su = 0
        self.wins_b_sa = 0
        self.wins_b_sb = 0
        self.wins_b_su = 0


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
        validity = classify_validity(res, hard_floor_ms=self._bailout_floor_ms)
        if validity is not Validity.DECISIVE:
            cell.nondecisive += 1
            if validity is Validity.INVALID:
                cell.invalid += 1
            return
        cell.ok += 1
        if is_fast_game(res):
            cell.fast += 1
        if is_concede(res):
            cell.concede += 1
        bucket = _winner_bucket(res.winner)
        # Which starter-seat suffix this game contributes to (sa/sb/su): 'A' → sa, 'B' → sb,
        # None/legacy → su. Kept separate so a legacy (unmarked) game never pollutes the A/B split.
        st = (res.starter or '').strip().upper()
        suffix = 'sa' if st == 'A' else 'sb' if st == 'B' else 'su'
        if bucket == 'a':
            cell.wins_a += 1
            setattr(cell, f'wins_a_{suffix}', getattr(cell, f'wins_a_{suffix}') + 1)
        elif bucket == 'b':
            cell.wins_b += 1
            setattr(cell, f'wins_b_{suffix}', getattr(cell, f'wins_b_{suffix}') + 1)

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

    def invalid_cells(self) -> list[tuple[str, str, str]]:
        """Cells that saw at least one INVALID game (a decisive claim with no legal terminal cause).

        Should be near-empty; a nonzero list is a loud data-integrity alarm (a driver fabricating a
        win, or an ``unknown`` cause on a game claiming a winner) — surfaced in coverage."""
        return [k for k, c in self._cells.items() if c.invalid]

    def invalid_count(self) -> int:
        """Total INVALID games across all cells (the coverage alarm counter)."""
        return sum(c.invalid for c in self._cells.values())

    def fast_count(self) -> int:
        """Total DECISIVE games under the fast-game floor (informational triage flag)."""
        return sum(c.fast for c in self._cells.values())

    def concede_count(self) -> int:
        """Total decisive concessions across all cells (a data-quality signal in the cause breakdown).

        Concessions are counted in W/L like any decisive cause; this surfaces concede-heavy matchups
        (an AI that concedes a lot is worth monitoring) without excluding them."""
        return sum(c.concede for c in self._cells.values())

    def concede_cells(self) -> list[tuple[str, str, str]]:
        """Cells that saw at least one decisive concession — surfaced so concede-heavy matchups show."""
        return [k for k, c in self._cells.items() if c.concede]

    def starter_split(self) -> dict[str, dict[str, dict[str, float | int]]]:
        """Per-arm first-player split so the first-player-seat effect is measurable.

        Returns ``{piloting: {'A': stats, 'B': stats, 'unknown': stats}}`` where ``piloting`` is
        the arm (``driven`` / ``baseline``), each key is WHO TOOK THE FIRST TURN, and ``stats`` is
        ``{'subject_wins', 'opponent_wins', 'decided', 'winrate'}`` — the SUBJECT's decisive win
        count / rate among games that seat started. ``'unknown'`` collects legacy (unmarked) games.
        A first-player advantage shows as ``A``'s subject winrate exceeding ``B``'s within an arm;
        because both arms share an index-matched starter schedule, the two arms are comparable.
        """
        # suffix → the starter bucket it feeds in the output.
        buckets = (('sa', 'A'), ('sb', 'B'), ('su', 'unknown'))
        out: dict[str, dict[str, dict[str, float | int]]] = {}
        for (_subject, _opp, pil), cell in self._cells.items():
            arm = out.setdefault(
                pil, {label: {'subject_wins': 0, 'opponent_wins': 0, 'decided': 0} for _s, label in buckets}
            )
            for suffix, label in buckets:
                sw = getattr(cell, f'wins_a_{suffix}')
                ow = getattr(cell, f'wins_b_{suffix}')
                arm[label]['subject_wins'] += sw
                arm[label]['opponent_wins'] += ow
                arm[label]['decided'] += sw + ow
        for arm in out.values():
            for stats in arm.values():
                decided = stats['decided']
                stats['winrate'] = (stats['subject_wins'] / decided) if decided else 0.0
        return out

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
