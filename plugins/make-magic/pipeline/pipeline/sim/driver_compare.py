"""Phase 4 — the benchmark-relative driver-vs-CP7 comparison instrument (GAP-1).

The measurement backbone of the driver study: quantify a per-deck driver's VALUE as
its RELATIVE performance versus a plain CP7 piloting AGAINST A SHARED BENCHMARK. This
is **not** a mirror match — the driver-piloted deck never plays a copy of itself.

For one deck ``D`` with a gated driver ``(classes_dir, fqcn)``, two pilotings are run —
``{driver-piloted, CP7-piloted (driver=None)}`` — each INDEPENDENTLY against a shared
control, and the study reports the **delta**:

  * **Goldfish lens** (always): the Phase-1 own-turn goldfish clock, once driven and
    once driverless, over the SAME ``deck_ref`` + ``fmt`` → two own-turn medians →
    ``own_turn_delta = driver - cp7`` (lower own-turn kill = faster = better).
  * **Gauntlet lens** (when a gauntlet is supplied): for EACH opponent, the candidate
    (always PlayerA) faces that opponent's NORMAL CP7 twice — once driver-piloted, once
    driverless — at the SAME seed. Each piloting's win-rate ± Wilson CI vs the field is
    aggregated → ``winrate_delta`` + per-opponent rows. The opponents are the shared
    control; the candidate NEVER faces a copy of itself.

The anti-confound is structural: both pilotings run the SAME engine, SAME per-opponent
seed, and SAME opponent set — the ONLY difference is ``driver=(classes_dir, fqcn)`` vs
``driver=None`` (:func:`_build_specs`). Concurrency is bounded by the governor
(:func:`_default_run_matchups`); the runner is injectable so the Phase-2 match gate can
reuse the SAME comparison core with a deterministic sequential runner
(:func:`_sequential_run_matchups`) — one comparison impl, not two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pipeline.sim import drivers
from pipeline.sim.core import wilson_ci
from pipeline.sim.governor import MatchFailure, MatchSpec, PoolResult, run_matchups

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Sequence

    from pipeline.contracts import Deck
    from pipeline.sim.engine import EngineInstall
    from pipeline.sim.engines.xmage import GoldfishResult
    from pipeline.sim.gauntlet import GauntletDeck
    from pipeline.sim.runner import MatchResult

__all__ = (
    'DriverCompareError',
    'OpponentComparison',
    'PilotingComparison',
    'compare_pilotings',
)


class DriverCompareError(RuntimeError):
    """The comparison could not run — most often: ``deck`` has no VALID driver to
    compare (the caller is pointed at ``driver author``)."""


class _CompareEngine(Protocol):
    """The slice of the XMage engine the comparison needs — a Protocol so tests inject
    a JVM-free fake. ``goldfish`` drives the own-turn lens; ``run_matchup`` (dispatched
    by the governed / sequential runner) drives the gauntlet lens."""

    def goldfish(
        self,
        deck_a: tuple[str, str],
        *,
        games: int,
        install: EngineInstall,
        driver: tuple[str, str] | None = ...,
        fmt: str = ...,
    ) -> GoldfishResult: ...

    def run_matchup(
        self,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str,
        install: EngineInstall,
        driver: tuple[str, str] | None = ...,
    ) -> MatchResult: ...


# The matchup-runner seam is ``(engine, install, specs) -> PoolResult``: the default is
# the bounded governor (:func:`_default_run_matchups`); the gate injects a deterministic
# sequential runner. Both return a :class:`~pipeline.sim.governor.PoolResult` (only its
# ``pairs`` / ``failures`` are consumed), so the aggregation logic is shared verbatim.


# --------------------------------------------------------------------------- #
# Value types.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OpponentComparison:
    """One shared-control opponent's driver-vs-CP7 read.

    Win-rates are on DECIDED games (draws excluded — the Wilson-CI denominator), from
    the candidate's (PlayerA) perspective. ``winrate_delta = driver - cp7`` (higher =
    the driver won MORE of this matchup)."""

    opponent: str
    winrate_driver: float
    winrate_cp7: float
    winrate_delta: float
    driver_wins: int
    driver_decided: int
    cp7_wins: int
    cp7_decided: int
    winrate_driver_ci: tuple[float, float]
    winrate_cp7_ci: tuple[float, float]


@dataclass(frozen=True)
class PilotingComparison:
    """The full benchmark-relative read for one deck's driver vs a plain CP7 piloting.

    Goldfish lens (always populated): ``own_turn_{driver,cp7}`` are the two own-turn kill
    medians (the harness ``-1.0`` "never killed" sentinel is a real value, surfaced as a
    warning, not silently zeroed); ``own_turn_delta = driver - cp7`` (lower = faster).

    Gauntlet lens (``None`` when no gauntlet was supplied): ``winrate_{driver,cp7}`` are
    the field-aggregate win-rates ± Wilson CI on DECIDED games, ``winrate_delta =
    driver - cp7`` (higher = the driver wins MORE vs the field), and ``per_opponent``
    the shared-control breakdown.

    ``gate_mode_used`` records the driver's declared benchmark mode; ``warnings`` carry
    non-fatal notes (a no-kill sentinel, a failed matchup)."""

    candidate: str
    fmt: str
    games: int
    gate_mode_used: str
    fqcn: str
    own_turn_driver: float
    own_turn_cp7: float
    own_turn_delta: float
    winrate_driver: float | None = None
    winrate_cp7: float | None = None
    winrate_delta: float | None = None
    winrate_driver_ci: tuple[float, float] | None = None
    winrate_cp7_ci: tuple[float, float] | None = None
    per_opponent: tuple[OpponentComparison, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """A machine-readable projection for ``driver compare --json`` (the Phase-6 study
        consumes this)."""
        return {
            'candidate': self.candidate,
            'fmt': self.fmt,
            'games': self.games,
            'gate_mode_used': self.gate_mode_used,
            'fqcn': self.fqcn,
            'goldfish': {
                'own_turn_driver': self.own_turn_driver,
                'own_turn_cp7': self.own_turn_cp7,
                'own_turn_delta': self.own_turn_delta,
            },
            'gauntlet': None
            if self.winrate_driver is None
            else {
                'winrate_driver': self.winrate_driver,
                'winrate_cp7': self.winrate_cp7,
                'winrate_delta': self.winrate_delta,
                'winrate_driver_ci': list(self.winrate_driver_ci) if self.winrate_driver_ci else None,
                'winrate_cp7_ci': list(self.winrate_cp7_ci) if self.winrate_cp7_ci else None,
                'per_opponent': [
                    {
                        'opponent': o.opponent,
                        'winrate_driver': o.winrate_driver,
                        'winrate_cp7': o.winrate_cp7,
                        'winrate_delta': o.winrate_delta,
                        'driver_record': f'{o.driver_wins}/{o.driver_decided}',
                        'cp7_record': f'{o.cp7_wins}/{o.cp7_decided}',
                        'winrate_driver_ci': list(o.winrate_driver_ci),
                        'winrate_cp7_ci': list(o.winrate_cp7_ci),
                    }
                    for o in self.per_opponent
                ],
            },
            'warnings': list(self.warnings),
        }


@dataclass(frozen=True)
class _GauntletComparison:
    """The gauntlet lens's internal result — the aggregate + per-opponent read PLUS the
    combined DRIVEN-piloting log (the match gate greps it for the intent marker)."""

    winrate_driver: float
    winrate_cp7: float
    winrate_delta: float
    winrate_driver_ci: tuple[float, float]
    winrate_cp7_ci: tuple[float, float]
    per_opponent: tuple[OpponentComparison, ...]
    driven_log: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Matchup runners (the injectable execution substrate).
# --------------------------------------------------------------------------- #


def _default_engine() -> _CompareEngine:
    """The registered XMage engine singleton (the real goldfish/match runner)."""
    from pipeline.sim.engine import get_engine

    return get_engine('xmage')  # type: ignore[return-value]


def _default_run_matchups(engine: _CompareEngine, install: EngineInstall, specs: list[MatchSpec]) -> PoolResult:
    """Run ``specs`` across the bounded, resource-safe governor (the study path).

    Mirrors :func:`~pipeline.sim.core.run_cached_matchups`'s governor call: the engine's
    duck-typed ``max_concurrency`` / ``per_jvm_gib`` clamp the pool (XMage serializes on
    a non-COW staging volume and needs a fatter per-JVM RAM budget), and the disk floor
    watches the STAGING volume where the big per-run card DBs land. No caching — a
    comparison always runs both pilotings fresh (the whole point is the live delta)."""
    from pipeline.sim.governor import DEFAULT_PER_JVM_GIB
    from pipeline.sim.runner import reap_stale_staging, staging_root

    reap_stale_staging()
    cap = getattr(engine, 'max_concurrency', None)
    resolved_cap = cap(install) if callable(cap) else None
    max_concurrency = resolved_cap if isinstance(resolved_cap, int) else None
    budget = getattr(engine, 'per_jvm_gib', None)
    resolved_budget = budget() if callable(budget) else None
    per_jvm = float(resolved_budget) if isinstance(resolved_budget, (int, float)) else DEFAULT_PER_JVM_GIB
    return run_matchups(
        engine,  # type: ignore[arg-type]
        install,
        specs,
        max_concurrency=max_concurrency,
        per_jvm_gib=per_jvm,
        ram_floor_gib=max(2.0, per_jvm),
        disk_path=staging_root(),
    )


def _sequential_run_matchups(engine: _CompareEngine, install: EngineInstall, specs: list[MatchSpec]) -> PoolResult:
    """Run ``specs`` one at a time, in order — the deterministic runner the match gate
    injects. Preserves spec order (so the driven run precedes its driverless twin) and
    needs no threads/stagger, keeping the gate's fake-engine unit tests fast + ordered.
    Returns the SAME :class:`~pipeline.sim.governor.PoolResult` shape the governor does
    (only ``pairs`` / ``failures`` are read downstream)."""
    pairs: list[tuple[MatchSpec, MatchResult]] = []
    failures: list[MatchFailure] = []
    for spec in specs:
        driver_kw = {'driver': spec.driver} if spec.driver is not None else {}
        try:
            result = engine.run_matchup(
                spec.deck_a, spec.deck_b, n=spec.n, seed=spec.seed, fmt=spec.fmt, install=install, **driver_kw
            )
        except Exception as exc:  # a failed matchup is recorded, never fatal (governor parity).
            failures.append(MatchFailure(spec=spec, error=str(exc)))
            continue
        pairs.append((spec, result))
    return PoolResult(
        pool_size=1,
        results=[r for _, r in pairs],
        failures=failures,
        max_concurrent=1,
        aborted=False,
        min_free_ram_gib=0.0,
        min_free_disk_gib=0.0,
        pairs=pairs,
    )


# --------------------------------------------------------------------------- #
# The shared comparison core.
# --------------------------------------------------------------------------- #


def _build_specs(
    deck_ref: tuple[str, str],
    driver: tuple[str, str],
    opponents: Sequence[tuple[str, str]],
    *,
    games: int,
    seed: int,
    fmt: str,
) -> list[MatchSpec]:
    """Build the two-piloting spec list — the anti-confound made explicit.

    Per opponent, emit TWO specs that are byte-identical except ``driver``: the
    driver-piloted one (driver=tuple) IMMEDIATELY followed by its CP7 twin
    (driver=None), both with ``deck_a == deck_ref`` (the candidate is ALWAYS PlayerA, so
    it faces the opponent's normal CP7 — never a copy of itself) and the SAME
    per-opponent ``seed + i``. Holding engine (the caller's), seed, and opponent
    constant across the pair is the whole integrity guarantee."""
    specs: list[MatchSpec] = []
    for i, opp in enumerate(opponents):
        opp_seed = seed + i
        specs.append(MatchSpec(deck_a=deck_ref, deck_b=opp, n=games, seed=opp_seed, fmt=fmt, driver=driver))
        specs.append(MatchSpec(deck_a=deck_ref, deck_b=opp, n=games, seed=opp_seed, fmt=fmt, driver=None))
    return specs


def _pop_result(pairs: list[tuple[MatchSpec, MatchResult]], spec: MatchSpec) -> MatchResult | None:
    """Pop the first paired result whose spec EQUALS ``spec`` (order-independent, like
    :func:`~pipeline.sim.core._pop_matching_result`). Full-spec equality distinguishes
    the driver twin from its driverless one (the ``driver`` field differs) and one
    opponent from another (``deck_b`` / ``seed`` differ). ``None`` ⇒ a failed matchup."""
    for i, (paired, _) in enumerate(pairs):
        if paired == spec:
            return pairs.pop(i)[1]
    return None


def _rates(wins: int, decided: int) -> tuple[float, tuple[float, float]]:
    """A win-rate + its Wilson CI on ``decided`` games (0.0 / (0,1) when none decided)."""
    rate = wins / decided if decided else 0.0
    return rate, wilson_ci(wins, decided)


def compare_gauntlet(
    deck_ref: tuple[str, str],
    driver: tuple[str, str],
    opponents: Sequence[tuple[str, str]],
    *,
    install: EngineInstall,
    games: int,
    seed: int,
    fmt: str,
    engine: _CompareEngine,
    run_matchups: Callable[[_CompareEngine, EngineInstall, list[MatchSpec]], PoolResult],
) -> _GauntletComparison:
    """Run BOTH pilotings against the shared opponent field; aggregate the delta.

    Builds the anti-confound spec pairs (:func:`_build_specs`), runs them through the
    injected ``run_matchups`` (governed for the study, sequential for the gate), pairs
    results back by spec equality, and aggregates each piloting's win-rate ± Wilson CI on
    DECIDED games — overall and per opponent. The combined DRIVEN-piloting log is
    returned for the match gate's intent-fired check. Failed matchups are recorded as
    warnings and contribute no decided games (never a silent 0-0)."""
    specs = _build_specs(deck_ref, driver, opponents, games=games, seed=seed, fmt=fmt)
    pool = run_matchups(engine, install, specs)
    remaining = list(pool.pairs)

    warnings: list[str] = [f'matchup vs {f.spec.deck_b[0]} FAILED (no games): {f.error}' for f in pool.failures]
    if pool.aborted:
        warnings.append('the governed run was ABORTED before every matchup ran (resource starvation) — PARTIAL results')

    per_opponent: list[OpponentComparison] = []
    driven_logs: list[str] = []
    tot_dw = tot_dd = tot_cw = tot_cd = 0
    for i, opp in enumerate(opponents):
        opp_seed = seed + i
        driver_spec = MatchSpec(deck_a=deck_ref, deck_b=opp, n=games, seed=opp_seed, fmt=fmt, driver=driver)
        cp7_spec = MatchSpec(deck_a=deck_ref, deck_b=opp, n=games, seed=opp_seed, fmt=fmt, driver=None)
        driven = _pop_result(remaining, driver_spec)
        cp7 = _pop_result(remaining, cp7_spec)
        if driven is not None:
            driven_logs.append(driven.raw_log)
        dw, dd = (driven.wins_a, driven.wins_a + driven.wins_b) if driven is not None else (0, 0)
        cw, cd = (cp7.wins_a, cp7.wins_a + cp7.wins_b) if cp7 is not None else (0, 0)
        tot_dw += dw
        tot_dd += dd
        tot_cw += cw
        tot_cd += cd
        d_rate, d_ci = _rates(dw, dd)
        c_rate, c_ci = _rates(cw, cd)
        per_opponent.append(
            OpponentComparison(
                opponent=opp[0],
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

    driver_rate, driver_ci = _rates(tot_dw, tot_dd)
    cp7_rate, cp7_ci = _rates(tot_cw, tot_cd)
    return _GauntletComparison(
        winrate_driver=driver_rate,
        winrate_cp7=cp7_rate,
        winrate_delta=driver_rate - cp7_rate,
        winrate_driver_ci=driver_ci,
        winrate_cp7_ci=cp7_ci,
        per_opponent=tuple(per_opponent),
        driven_log='\n'.join(driven_logs),
        warnings=tuple(warnings),
    )


def compare_pilotings(
    deck: Deck,
    deck_ref: tuple[str, str],
    *,
    install: EngineInstall,
    games: int,
    gauntlet: Sequence[GauntletDeck] | None = None,
    seed: int = 42,
    fmt: str = 'constructed',
    engine: _CompareEngine | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    run_matchups: Callable[[_CompareEngine, EngineInstall, list[MatchSpec]], PoolResult] | None = None,
    power_run: bool = False,
) -> PilotingComparison:
    """Measure ``deck``'s gated driver vs a plain CP7 piloting against a shared benchmark.

    Resolves ``deck``'s CURRENT, gate-passed driver (a :class:`DriverCompareError` points
    the caller at ``driver author`` when absent), then runs two INDEPENDENT lenses over
    the shared ``deck_ref``:

      * the goldfish **solo own-turn clock** (driven vs driverless) → ``own_turn_delta``
        (the same metric the Phase-5 ship gate uses — a lower own-turn kill is faster);
      * when ``gauntlet`` is supplied, both pilotings vs each opponent's normal CP7 at a
        fixed per-opponent seed → win-rate ± Wilson-CI delta + per-opponent rows.

    ``power_run`` is the opt-in headline measurement — it DOUBLES ``games`` to tighten the
    medians / CIs toward statistical *superiority*. It is **default-off**: the gate never
    requires it, and a routine comparison runs at the caller's ``games``.

    ``engine`` defaults to the registered XMage engine; ``run_matchups`` to the bounded
    governor (:func:`_default_run_matchups`). Holding engine/seed/opponents constant
    across the two pilotings — differing ONLY in the driver — is the anti-confound; the
    candidate never faces a copy of itself."""
    if power_run:
        games *= 2  # opt-in ~2x sample for a headline superiority read; never a ship requirement.
    if not drivers.driver_valid(deck, data_dir=data_dir):
        raise DriverCompareError(
            f'deck {deck.name!r} has no VALID driver to compare — author + gate one first with '
            '`driver author "<deck>" --spec <linespec.json>`.'
        )
    meta = drivers.read_meta(deck, data_dir=data_dir)
    assert meta is not None  # driver_valid implies a parseable meta.
    driver = (str(drivers.classes_dir(deck, data_dir=data_dir)), meta.fqcn)
    eng = engine if engine is not None else _default_engine()
    runner = run_matchups if run_matchups is not None else _default_run_matchups

    # Goldfish lens — driven then driverless over the SAME deck_ref + fmt.
    driven_gf = eng.goldfish(deck_ref, games=games, install=install, driver=driver, fmt=fmt)
    cp7_gf = eng.goldfish(deck_ref, games=games, install=install, driver=None, fmt=fmt)
    warnings: list[str] = []
    for label, gf in (('driver', driven_gf), ('CP7', cp7_gf)):
        if gf.median_kills_own < 0:
            warnings.append(
                f'{label} goldfish NEVER killed on its own turn (medianKillsOwn={gf.median_kills_own}); '
                'own_turn_delta is not meaningful for this lens'
            )

    gaunt: _GauntletComparison | None = None
    if gauntlet:
        opponents = [(g.name, g.dck_text) for g in gauntlet]
        gaunt = compare_gauntlet(
            deck_ref,
            driver,
            opponents,
            install=install,
            games=games,
            seed=seed,
            fmt=fmt,
            engine=eng,
            run_matchups=runner,
        )
        warnings.extend(gaunt.warnings)

    return PilotingComparison(
        candidate=deck_ref[0],
        fmt=fmt,
        games=games,
        gate_mode_used=meta.gate_mode,
        fqcn=meta.fqcn,
        own_turn_driver=driven_gf.median_kills_own,
        own_turn_cp7=cp7_gf.median_kills_own,
        own_turn_delta=driven_gf.median_kills_own - cp7_gf.median_kills_own,
        winrate_driver=gaunt.winrate_driver if gaunt else None,
        winrate_cp7=gaunt.winrate_cp7 if gaunt else None,
        winrate_delta=gaunt.winrate_delta if gaunt else None,
        winrate_driver_ci=gaunt.winrate_driver_ci if gaunt else None,
        winrate_cp7_ci=gaunt.winrate_cp7_ci if gaunt else None,
        per_opponent=gaunt.per_opponent if gaunt else (),
        warnings=tuple(warnings),
    )
