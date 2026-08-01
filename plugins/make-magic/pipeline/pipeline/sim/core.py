"""Sim core: cached matchup execution + ``simulate`` / ``compare`` over a gauntlet.

This is the integrating layer that ties every prior phase together:

  * :mod:`pipeline.sim.gauntlet` resolves the opponent set;
  * :mod:`pipeline.sim.store` is the content-addressed cache (a matchup already
    run is NEVER re-run unless ``force``);
  * :mod:`pipeline.sim.governor` runs the cache MISSES across a bounded,
    resource-safe pool of Forge JVMs;
  * :mod:`pipeline.sim.telemetry` turns each fresh verbose log into per-game
    :class:`~pipeline.sim.telemetry.GameFeatures`, which :func:`simulate`
    aggregates into a profile.

Three entry points:

  * :func:`run_cached_matchups` — read-through the cache, run the misses, store
    fresh results, return one :class:`MatchOutcome` per matchup (cached-or-fresh
    flagged).
  * :func:`simulate` — resolve a gauntlet, build one matchup per opponent, run
    them cached, and aggregate into a :class:`SimResult` (overall win-rate +
    **Wilson CI**, per-opponent breakdown, and an aggregate telemetry profile).
  * :func:`compare` — ``simulate`` two variants over the SAME gauntlet and diff
    their profiles.

**Variance is from sample size, not seed pairing.** Forge's ``-s`` is not a
reliably reproducible seed, so this layer makes NO common-random-numbers claim:
the Wilson CI on win-rate reflects the number of games, full stop.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

from pipeline.sim import forge_runtime
from pipeline.sim.gauntlet import resolve_gauntlet
from pipeline.sim.governor import MatchSpec, run_matchups
from pipeline.sim.runner import MatchResult, deck_to_dck
from pipeline.sim.store import (
    MatchupMeta,
    deck_hash,
    get_cached,
    matchup_key,
    store_matchup,
)
from pipeline.sim.telemetry import GameFeatures, extract_match_features

if TYPE_CHECKING:
    import os

    from pipeline.contracts import Deck
    from pipeline.sim.forge_runtime import ForgeInstall

__all__ = (
    'Comparison',
    'MatchOutcome',
    'OpponentResult',
    'SimResult',
    'TelemetryProfile',
    'compare',
    'forge_version',
    'run_cached_matchups',
    'simulate',
    'wilson_ci',
)

#: A candidate/variant deck: a domain ``Deck`` OR an already-rendered
#: ``(name, dck_text)`` pair.
DeckInput = Union['Deck', tuple[str, str]]

#: Wilson score z for a 95% two-sided interval.
_WILSON_Z = 1.959963984540054


def forge_version() -> str:
    """The pinned Forge version string folded into every cache key.

    A thin indirection over :data:`pipeline.sim.forge_runtime.FORGE_VERSION` so a
    version bump self-invalidates the cache (and so tests can patch it).
    """
    return forge_runtime.FORGE_VERSION


def wilson_ci(wins: int, n: int, *, z: float = _WILSON_Z) -> tuple[float, float]:
    """The Wilson score confidence interval for a win-rate ``wins/n``.

    Wilson (not the naive normal approximation) so the interval behaves at the 0/1
    boundaries and for small ``n``. Returns ``(lo, hi)`` clamped to ``[0, 1]``.
    ``n == 0`` returns the degenerate ``(0.0, 1.0)`` — no games, no information —
    rather than dividing by zero.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


# --------------------------------------------------------------------------- #
# Value types.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MatchOutcome:
    """One matchup's result: the candidate's win tally + per-game telemetry.

    ``wins`` / ``losses`` / ``draws`` are from the CANDIDATE's perspective (the
    candidate is always ``deck_a`` / Ai(1) in the spec). ``cached`` is True when
    this outcome was served from the store (0 Forge games), False when freshly
    run. ``features`` is the per-game telemetry (parsed fresh, or rehydrated from
    the cache).
    """

    opponent: str
    wins: int
    losses: int
    draws: int
    cached: bool
    features: list[GameFeatures]

    @property
    def games(self) -> int:
        """Total games in this matchup."""
        return self.wins + self.losses + self.draws


@dataclass(frozen=True)
class OpponentResult:
    """The candidate's record vs ONE opponent — the per-opponent breakdown row."""

    opponent: str
    wins: int
    losses: int
    draws: int
    games: int
    win_rate: float
    win_rate_ci: tuple[float, float]
    cached: bool


@dataclass(frozen=True)
class TelemetryProfile:
    """Aggregate telemetry over every game in a simulation (all opponents pooled).

    Every field summarizes the pooled :class:`~pipeline.sim.telemetry.GameFeatures`:
    kill-turn central tendency, a win-margin (winner's remaining life) summary, the
    wincon mix (a ``{wincon: count}`` map), and the mean per-turn land ramp curve.
    A field is ``None`` / empty when no game supplied the underlying signal.
    """

    games: int
    avg_kill_turn: float | None
    median_kill_turn: float | None
    avg_win_margin_life: float | None
    median_win_margin_life: float | None
    wincon_mix: dict[str, int]
    mean_ramp_curve: list[float]


@dataclass(frozen=True)
class SimResult:
    """The aggregated outcome of simulating a candidate against a gauntlet.

    Overall win-rate + **Wilson CI** over ALL games, the per-opponent breakdown,
    and a pooled telemetry :class:`TelemetryProfile`. ``cached_matchups`` /
    ``fresh_matchups`` count how many opponents were served from cache vs freshly
    run (0 fresh == a fully cached re-run).
    """

    candidate: str
    gauntlet_source: str
    fmt: str
    games_per_opponent: int
    total_games: int
    wins: int
    losses: int
    draws: int
    win_rate: float
    win_rate_ci: tuple[float, float]
    per_opponent: list[OpponentResult]
    profile: TelemetryProfile
    cached_matchups: int
    fresh_matchups: int


@dataclass(frozen=True)
class Comparison:
    """A diff of two :class:`SimResult` over the SAME gauntlet (A vs B).

    ``win_rate_delta`` is ``A.win_rate - B.win_rate``; ``metric_deltas`` diffs the
    two profiles' scalar metrics (A minus B, ``None`` when either side is
    ``None``). ``stronger`` names the higher-win-rate variant (``None`` on a tie).
    Per the module note, the confidence in a delta comes from each side's Wilson
    CI (sample size) — there is NO seed-paired CRN claim.
    """

    a: SimResult
    b: SimResult
    win_rate_delta: float
    metric_deltas: dict[str, float | None]
    stronger: str | None = None


# --------------------------------------------------------------------------- #
# Cached matchup execution.
# --------------------------------------------------------------------------- #


def _candidate_tally(result: MatchResult) -> tuple[int, int, int]:
    """(wins, losses, draws) from the CANDIDATE (deck_a / Ai(1)) perspective."""
    return result.wins_a, result.wins_b, result.draws


def run_cached_matchups(
    install: ForgeInstall | None,
    specs: list[MatchSpec],
    *,
    force: bool = False,
    data_dir: str | os.PathLike[str] | None = None,
    pool_size: int | None = None,
) -> list[MatchOutcome]:
    """Run ``specs`` through the content-addressed cache, returning one outcome each.

    For each spec a :func:`~pipeline.sim.store.matchup_key` is computed; unless
    ``force``, a :func:`~pipeline.sim.store.get_cached` hit is served with ZERO
    Forge games. The remaining MISSES are run together via
    :func:`~pipeline.sim.governor.run_matchups` (one bounded, resource-safe
    batch), each fresh log is parsed with
    :func:`~pipeline.sim.telemetry.extract_match_features` and persisted with
    :func:`~pipeline.sim.store.store_matchup`. Returns the outcomes in the SAME
    order as ``specs`` (candidate-perspective tally + telemetry, cached flagged).
    """
    version = forge_version()

    keys = [
        matchup_key(
            s.deck_a[1], s.deck_b[1], seed=s.seed, n_games=s.n, fmt=s.fmt, forge_version=version
        )
        for s in specs
    ]

    outcomes: dict[int, MatchOutcome] = {}
    misses: list[tuple[int, MatchSpec, str]] = []

    for idx, (spec, key) in enumerate(zip(specs, keys, strict=True)):
        cached = None if force else get_cached(key, data_dir=data_dir)
        if cached is not None:
            outcomes[idx] = MatchOutcome(
                opponent=spec.deck_b[0],
                wins=cached.wins_a,
                losses=cached.wins_b,
                draws=cached.draws,
                cached=True,
                features=cached.features,
            )
        else:
            misses.append((idx, spec, key))

    if misses:
        miss_specs = [spec for _, spec, _ in misses]
        pool = run_matchups(install, miss_specs, pool_size=pool_size)  # type: ignore[arg-type]

        # The governor returns results out of order and without a spec back-ref,
        # so pair each result to its spec by the opponent's (deck_a, deck_b) names
        # (unique per opponent within one simulate). A spec with no matching result
        # was a governor failure (deck-load/timeout) -> a zeroed outcome.
        remaining = list(pool.results)
        for miss_idx, spec, key in misses:
            match = _pop_matching_result(remaining, spec)
            if match is None:
                # A failed matchup (deck-load/timeout): record a zeroed outcome so
                # the aggregate still has a row rather than silently dropping it.
                outcomes[miss_idx] = MatchOutcome(
                    opponent=spec.deck_b[0], wins=0, losses=0, draws=0, cached=False, features=[]
                )
                continue
            features = extract_match_features(
                match.raw_log, deck_a=spec.deck_a[0], deck_b=spec.deck_b[0]
            )
            meta = MatchupMeta(
                deck_a_hash=deck_hash(spec.deck_a[1]),
                deck_b_hash=deck_hash(spec.deck_b[1]),
                seed=spec.seed,
                n_games=spec.n,
                format=spec.fmt,
                forge_version=version,
            )
            store_matchup(key, meta, match, features, data_dir=data_dir)
            wins, losses, draws = _candidate_tally(match)
            outcomes[miss_idx] = MatchOutcome(
                opponent=spec.deck_b[0],
                wins=wins,
                losses=losses,
                draws=draws,
                cached=False,
                features=features,
            )

    return [outcomes[i] for i in range(len(specs))]


def _pop_matching_result(results: list[MatchResult], spec: MatchSpec) -> MatchResult | None:
    """Pop the first result whose deck names match ``spec`` (order-independent).

    The governor returns results out of order and without a spec back-reference,
    so we pair by ``(deck_a, deck_b)`` name — unique per opponent within a single
    ``simulate`` (each opponent appears once). Returns ``None`` when no result
    matches (the matchup failed and was recorded as a governor failure)."""
    for i, res in enumerate(results):
        if res.deck_a == spec.deck_a[0] and res.deck_b == spec.deck_b[0]:
            return results.pop(i)
    return None


# --------------------------------------------------------------------------- #
# simulate.
# --------------------------------------------------------------------------- #


def _as_dck(deck: DeckInput) -> tuple[str, str]:
    """Normalize a candidate/variant to a ``(name, dck_text)`` pair.

    Accepts either a domain :class:`~pipeline.contracts.Deck` (rendered via the
    Forge exporter) or an already-rendered ``(name, dck_text)`` pair (passed
    through). Anything else is a programming error.
    """
    if isinstance(deck, tuple):
        return deck
    return (deck.name, deck_to_dck(deck))


def _aggregate_profile(features: list[GameFeatures]) -> TelemetryProfile:
    """Pool per-game features into a :class:`TelemetryProfile` (pure)."""
    kill_turns = [f.kill_turn for f in features if f.kill_turn is not None]
    margins = [f.win_margin_life for f in features if f.win_margin_life is not None]
    wincon_mix = Counter(f.wincon for f in features if f.wincon is not None)

    # Mean ramp curve: average the candidate's (deck_a) land-by-turn curves,
    # padding shorter games with their last observed value so a long game doesn't
    # skew early turns. Empty when no game supplied a curve.
    curves = [f.lands_by_turn_a for f in features if f.lands_by_turn_a]
    mean_ramp: list[float] = []
    if curves:
        width = max(len(c) for c in curves)
        for t in range(width):
            col = [c[t] if t < len(c) else c[-1] for c in curves]
            mean_ramp.append(sum(col) / len(col))

    return TelemetryProfile(
        games=len(features),
        avg_kill_turn=statistics.fmean(kill_turns) if kill_turns else None,
        median_kill_turn=float(statistics.median(kill_turns)) if kill_turns else None,
        avg_win_margin_life=statistics.fmean(margins) if margins else None,
        median_win_margin_life=float(statistics.median(margins)) if margins else None,
        wincon_mix=dict(wincon_mix),
        mean_ramp_curve=mean_ramp,
    )


def simulate(
    deck: DeckInput,
    gauntlet_source: str,
    *,
    games: int,
    fmt: str,
    seed: int,
    install: ForgeInstall | None = None,
    force: bool = False,
    store: object | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    pool_size: int | None = None,
) -> SimResult:
    """Simulate ``deck`` against a resolved gauntlet and aggregate the results.

    Resolves the opponent set (:func:`~pipeline.sim.gauntlet.resolve_gauntlet`),
    builds ONE :class:`~pipeline.sim.governor.MatchSpec` per opponent (the
    candidate as ``deck_a`` vs the opponent, ``n=games``, a per-opponent seed
    offset so parallel workers don't replay identical games), runs them through
    :func:`run_cached_matchups`, and folds the outcomes into a :class:`SimResult`:
    overall win-rate + Wilson CI, the per-opponent breakdown, and a pooled
    :class:`TelemetryProfile`.

    ``deck`` may be a :class:`~pipeline.contracts.Deck` or a ``(name, dck_text)``
    pair. ``store`` is the collection store for the ``mine`` / ``both`` gauntlet
    source (unused for ``curated``). ``force`` bypasses the cache; ``install`` is
    the resolved Forge install (only touched when a matchup actually runs).
    """
    cand_name, cand_dck = _as_dck(deck)
    opponents = resolve_gauntlet(gauntlet_source, fmt, store=store, data_dir=data_dir)  # type: ignore[arg-type]

    specs = [
        MatchSpec(
            deck_a=(cand_name, cand_dck),
            deck_b=(opp.name, opp.dck_text),
            n=games,
            # Vary the seed per opponent so distinct matchups don't share a key
            # AND parallel JVMs don't replay identical games.
            seed=seed + offset,
            fmt=fmt,
        )
        for offset, opp in enumerate(opponents)
    ]

    outcomes = run_cached_matchups(
        install, specs, force=force, data_dir=data_dir, pool_size=pool_size
    )

    per_opponent: list[OpponentResult] = []
    all_features: list[GameFeatures] = []
    total_wins = total_losses = total_draws = 0
    cached_n = fresh_n = 0
    for outcome in outcomes:
        decided = outcome.wins + outcome.losses
        rate = outcome.wins / decided if decided else 0.0
        per_opponent.append(
            OpponentResult(
                opponent=outcome.opponent,
                wins=outcome.wins,
                losses=outcome.losses,
                draws=outcome.draws,
                games=outcome.games,
                win_rate=rate,
                win_rate_ci=wilson_ci(outcome.wins, decided),
                cached=outcome.cached,
            )
        )
        all_features.extend(outcome.features)
        total_wins += outcome.wins
        total_losses += outcome.losses
        total_draws += outcome.draws
        cached_n += 1 if outcome.cached else 0
        fresh_n += 0 if outcome.cached else 1

    decided_total = total_wins + total_losses
    win_rate = total_wins / decided_total if decided_total else 0.0

    return SimResult(
        candidate=cand_name,
        gauntlet_source=gauntlet_source,
        fmt=fmt,
        games_per_opponent=games,
        total_games=total_wins + total_losses + total_draws,
        wins=total_wins,
        losses=total_losses,
        draws=total_draws,
        win_rate=win_rate,
        win_rate_ci=wilson_ci(total_wins, decided_total),
        per_opponent=per_opponent,
        profile=_aggregate_profile(all_features),
        cached_matchups=cached_n,
        fresh_matchups=fresh_n,
    )


# --------------------------------------------------------------------------- #
# compare.
# --------------------------------------------------------------------------- #

#: The scalar profile metrics diffed by :func:`compare` (A minus B).
_PROFILE_METRICS = (
    'avg_kill_turn',
    'median_kill_turn',
    'avg_win_margin_life',
    'median_win_margin_life',
)


def compare(
    variant_a: DeckInput,
    variant_b: DeckInput,
    gauntlet_source: str,
    *,
    games: int,
    fmt: str,
    seed: int,
    install: ForgeInstall | None = None,
    force: bool = False,
    store: object | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    pool_size: int | None = None,
) -> Comparison:
    """Simulate two variants over the SAME gauntlet and diff their profiles.

    Calls :func:`simulate` for ``variant_a`` and ``variant_b`` with identical
    gauntlet / games / seed / format, then diffs: the win-rate delta (A minus B,
    each side carrying its own Wilson CI) and the per-metric profile deltas.
    ``stronger`` names the higher-win-rate variant (``None`` on a tie).

    Variance comes from sample size (each side's Wilson CI), NOT from seed
    pairing — Forge's ``-s`` is not reliably reproducible, so no common-random-
    numbers pairing is claimed.
    """
    a = simulate(
        variant_a, gauntlet_source, games=games, fmt=fmt, seed=seed,
        install=install, force=force, store=store, data_dir=data_dir, pool_size=pool_size,
    )
    b = simulate(
        variant_b, gauntlet_source, games=games, fmt=fmt, seed=seed,
        install=install, force=force, store=store, data_dir=data_dir, pool_size=pool_size,
    )

    metric_deltas: dict[str, float | None] = {}
    for metric in _PROFILE_METRICS:
        av = getattr(a.profile, metric)
        bv = getattr(b.profile, metric)
        metric_deltas[metric] = (av - bv) if (av is not None and bv is not None) else None

    if a.win_rate > b.win_rate:
        stronger = a.candidate
    elif b.win_rate > a.win_rate:
        stronger = b.candidate
    else:
        stronger = None

    return Comparison(
        a=a,
        b=b,
        win_rate_delta=a.win_rate - b.win_rate,
        metric_deltas=metric_deltas,
        stronger=stronger,
    )
