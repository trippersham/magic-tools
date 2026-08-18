"""TDD tests for the sim core: cached matchups, simulate, compare.

Offline coverage MOCKS the governor (``run_matchups``) so NO real Forge JVM is
spawned: ``simulate`` is handed a deterministic set of fake
:class:`~pipeline.sim.governor.PoolResult` shapes and we assert the aggregation
(win-rate + Wilson CI + per-opponent breakdown + telemetry profile), the cache
integration (second identical run hits the store and runs 0 games; ``force``
bypasses), and ``compare`` (diff of two SimResults).

The ONE gated ``@pytest.mark.forge`` test at the bottom spawns real Forge (capped
pool_size<=2, games=1) against a SMALL curated gauntlet and asserts a populated
:class:`SimResult`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pipeline import store
from pipeline.contracts import Deck, DeckCard
from pipeline.sim import core
from pipeline.sim.core import (
    Comparison,
    SimResult,
    compare,
    run_cached_matchups,
    simulate,
    wilson_ci,
)
from pipeline.sim.engine import EngineCapabilities, EngineInstall
from pipeline.sim.forge_runtime import ENV_FORGE_HOME, ENV_JAVA, FORGE_VERSION
from pipeline.sim.governor import MatchFailure, MatchSpec, PoolResult
from pipeline.sim.runner import GameOutcome, MatchResult


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the cache store at a tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


# --------------------------------------------------------------------------- #
# Wilson CI helper
# --------------------------------------------------------------------------- #


def test_wilson_ci_basic() -> None:
    """Wilson CI brackets the point estimate and stays within [0, 1]."""
    lo, hi = wilson_ci(5, 10)
    assert 0.0 <= lo < 0.5 < hi <= 1.0


def test_wilson_ci_zero_games() -> None:
    """No games -> a degenerate (0, 1) interval, never a divide-by-zero."""
    assert wilson_ci(0, 0) == (0.0, 1.0)


def test_wilson_ci_sample_size_narrows() -> None:
    """More games at the same rate narrow the interval (variance from n)."""
    lo_small, hi_small = wilson_ci(5, 10)
    lo_big, hi_big = wilson_ci(500, 1000)
    assert (hi_big - lo_big) < (hi_small - hi_small + (hi_small - lo_small))
    assert (hi_big - lo_big) < (hi_small - lo_small)


# --------------------------------------------------------------------------- #
# Fakes: a deck, an opponent set, and a canned governor result.
# --------------------------------------------------------------------------- #


def _deck(name: str) -> Deck:
    return Deck(
        name=name,
        format='Modern',
        cards=[DeckCard(name='Mountain', quantity=17), DeckCard(name='Goblin Piker', quantity=23)],
    )


#: A tiny verbose two-game log the telemetry parser turns into real features
#: (kill_turn, wincon), so the aggregate profile is non-empty. deck_a = Ai(1).
_FAKE_LOG_A_WINS = (
    'Turn: Turn 1 (Ai(1)-Cand)\n'
    'Land: Ai(1)-Cand played Mountain\n'
    'Turn: Turn 5 (Ai(1)-Cand)\n'
    'Damage: Goblin deals 20 combat damage to Ai(2)-Opp.\n'
    'Life: Life: Ai(2)-Opp 3 > -1\n'
    'Game Result: Game 1 ended in 1000 ms. Ai(1)-Cand has won!\n'
)


def _pool_result_candidate_sweeps(specs: list[MatchSpec]) -> PoolResult:
    """Every spec: the candidate (deck_a / Ai(1)) wins all n games, with a log."""
    results: list[MatchResult] = []
    for spec in specs:
        per_game = tuple(GameOutcome(winner='a', elapsed_ms=1000) for _ in range(spec.n))
        results.append(
            MatchResult(
                deck_a=spec.deck_a[0],
                deck_b=spec.deck_b[0],
                wins_a=spec.n,
                wins_b=0,
                draws=0,
                per_game=per_game,
                raw_log=_FAKE_LOG_A_WINS * spec.n,
            )
        )
    return PoolResult(
        pool_size=1,
        results=results,
        failures=[],
        max_concurrent=1,
        aborted=False,
        min_free_ram_gib=8.0,
        min_free_disk_gib=50.0,
        pairs=list(zip(specs, results, strict=True)),
    )


def _engine_install() -> EngineInstall:
    """A sentinel resolved install for the version+handle the core threads.

    ``version='test-forge'`` is what keys the cache (formerly ``forge_version()``);
    the mocked ``run_matchups`` ignores the opaque ``handle``.
    """
    return EngineInstall(version='test-forge', handle=object())


# --------------------------------------------------------------------------- #
# FakeEngine: drives simulate() through the REAL governor with NO JVM, so the
# behaviour-preserving refactor's aggregation is locked against a canned engine.
# --------------------------------------------------------------------------- #

_FAKE_CAPS = EngineCapabilities(
    has_hand_visibility=False,
    has_counter_metrics=False,
    expected_nondecisive_rate=0.05,
    reliability_note='fake engine — canned results only',
    kill_attribution='named',
)


class _FakeEngine:
    """A no-JVM :class:`~pipeline.sim.engine.SimEngine`: the candidate sweeps.

    Its :meth:`run_matchup` returns the SAME canned per-game log
    (:data:`_FAKE_LOG_A_WINS`) the governor-mock path uses, so an aggregation
    driven through this engine (via the REAL governor) must match the mocked-
    governor tests byte-for-byte — proving the engine seam preserved behaviour.
    """

    name = 'fake'

    def capabilities(self) -> EngineCapabilities:
        return _FAKE_CAPS

    def resolve(self, *, provision: bool, data_dir: object = None) -> EngineInstall:
        del provision, data_dir
        return EngineInstall(version='test-forge', handle=object())

    def run_matchup(
        self,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str,
        install: EngineInstall,
        timeout_s: int | None = None,
    ) -> MatchResult:
        del seed, fmt, install, timeout_s
        per_game = tuple(GameOutcome(winner='a', elapsed_ms=1000) for _ in range(n))
        return MatchResult(
            deck_a=deck_a[0],
            deck_b=deck_b[0],
            wins_a=n,
            wins_b=0,
            draws=0,
            per_game=per_game,
            raw_log=_FAKE_LOG_A_WINS * n,
        )

    def replay(self, matchup_key: str, game_idx: int) -> str:
        return f'fake replay {matchup_key}#{game_idx}'


def test_simulate_with_fake_engine_aggregates_identically(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """simulate() driven by a FakeEngine through the REAL governor aggregates
    win-rate / Wilson CI / per-opponent / profile IDENTICALLY to the mocked path.

    No JVM, no ``run_matchups`` mock: the FakeEngine's ``run_matchup`` IS the path
    the governor now calls, so this proves the engine seam is byte-identical to
    today's aggregation (the headline behaviour-preserving invariant). The pool is
    pinned (size 1, no stagger) so the real governor stays fast + deterministic.
    """
    _patch_gauntlet(monkeypatch, [('Opp1', 'X'), ('Opp2', 'Y'), ('Opp3', 'Z')])
    # Drop the ~5s per-spawn stagger so the REAL governor stays fast in-suite (a
    # no-JVM FakeEngine has no disk thrash to space out).
    from pipeline.sim.governor import Governor

    monkeypatch.setattr(Governor, 'stagger_s', 0.0)

    result = simulate(
        _deck('Candidate'),
        'curated',
        games=4,
        fmt='constructed',
        seed=7,
        engine=_FakeEngine(),
        data_dir=str(data_dir),
        pool_size=1,
    )

    assert isinstance(result, SimResult)
    assert result.total_games == 12
    assert result.wins == 12
    assert result.win_rate == pytest.approx(1.0)
    lo, hi = result.win_rate_ci
    assert lo <= 1.0 and hi <= 1.0 and lo > 0.5
    assert len(result.per_opponent) == 3
    assert {o.opponent for o in result.per_opponent} == {'Opp1', 'Opp2', 'Opp3'}
    assert all(o.wins == 4 and o.games == 4 for o in result.per_opponent)
    assert result.profile.games == 12
    assert result.profile.avg_kill_turn == pytest.approx(5.0)
    assert result.profile.wincon_mix.get('combat', 0) == 12


# --------------------------------------------------------------------------- #
# run_cached_matchups: cache miss -> run -> store; hit -> 0 games.
# --------------------------------------------------------------------------- #


def test_run_cached_matchups_miss_then_hit(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """First call runs the (mocked) games and stores; second hits the cache, 0 games."""
    calls: list[list[MatchSpec]] = []

    def fake_run_matchups(engine: object, install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        calls.append(specs)
        return _pool_result_candidate_sweeps(specs)

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)

    specs = [
        MatchSpec(deck_a=('Cand', 'A'), deck_b=('Opp', 'B'), n=4, seed=1, fmt='constructed'),
    ]

    first = run_cached_matchups(_FakeEngine(), _engine_install(), specs, data_dir=str(data_dir)).outcomes
    assert len(first) == 1
    assert first[0].wins == 4
    assert first[0].cached is False
    assert first[0].features  # telemetry parsed + returned
    assert len(calls) == 1  # one governor batch ran

    second = run_cached_matchups(_FakeEngine(), _engine_install(), specs, data_dir=str(data_dir)).outcomes
    assert second[0].wins == 4
    assert second[0].cached is True
    assert len(calls) == 1  # NO second governor batch — served from cache


def test_run_cached_matchups_ram_floor_tracks_per_jvm(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """The admission RAM floor is threaded from the engine's per-JVM budget (max(2.0,
    per_jvm)) so a 3g XMage JVM is not admitted into <3 GiB free — the OOM #63 targets."""
    captured: dict[str, object] = {}

    def fake_run_matchups(engine: object, install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        captured.clear()
        captured.update(kw)
        return _pool_result_candidate_sweeps(specs)

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)
    specs = [MatchSpec(deck_a=('Cand', 'A'), deck_b=('Opp', 'B'), n=4, seed=1, fmt='constructed')]

    # XMage's 3.0 budget lifts the admission floor to 3.0.
    run_cached_matchups(_FakeEngine(), _engine_install(), specs, per_jvm_gib=3.0, force=True, data_dir=str(data_dir))
    assert captured['per_jvm_gib'] == 3.0
    assert captured['ram_floor_gib'] == 3.0

    # Forge's default (per_jvm_gib=None -> 2.0) keeps the historical 2.0 floor.
    run_cached_matchups(_FakeEngine(), _engine_install(), specs, force=True, data_dir=str(data_dir))
    assert captured['ram_floor_gib'] == 2.0


def test_run_cached_matchups_force_bypasses_cache(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """``force=True`` re-runs even a cached matchup."""
    calls: list[list[MatchSpec]] = []

    def fake_run_matchups(engine: object, install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        calls.append(specs)
        return _pool_result_candidate_sweeps(specs)

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)

    specs = [MatchSpec(deck_a=('Cand', 'A'), deck_b=('Opp', 'B'), n=2, seed=1, fmt='constructed')]

    run_cached_matchups(_FakeEngine(), _engine_install(), specs, data_dir=str(data_dir))
    run_cached_matchups(_FakeEngine(), _engine_install(), specs, force=True, data_dir=str(data_dir))
    assert len(calls) == 2  # force re-ran the governor


def test_run_cached_matchups_duplicate_names_attributed_by_spec(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """Duplicate (deck_a, deck_b) NAME pairs must not cross-attribute results.

    A `both` gauntlet can hold a user deck named identically to a curated one:
    two specs then share names but differ by seed (and by cache key). Each
    result must land on ITS spec and be stored under ITS key — name-based
    pairing hands the first-completed result to whichever spec comes first,
    silently poisoning the content-addressed cache.
    """
    spec_win = MatchSpec(deck_a=('Cand', 'A'), deck_b=('Opp', 'B'), n=1, seed=1)
    spec_lose = MatchSpec(deck_a=('Cand', 'A'), deck_b=('Opp', 'B'), n=1, seed=2)

    def fake_run_matchups(engine: object, install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        pairs: list[tuple[MatchSpec, MatchResult]] = []
        for spec in specs:
            wins_a = 1 if spec.seed == 1 else 0
            match = MatchResult(
                deck_a=spec.deck_a[0],
                deck_b=spec.deck_b[0],
                wins_a=wins_a,
                wins_b=1 - wins_a,
                draws=0,
                per_game=(GameOutcome(winner='a' if wins_a else 'b', elapsed_ms=1),),
                raw_log='',
            )
            pairs.append((spec, match))
        pairs.reverse()  # completion order is NOT submission order
        return PoolResult(
            pool_size=1,
            results=[m for _, m in pairs],
            failures=[],
            max_concurrent=1,
            aborted=False,
            min_free_ram_gib=8.0,
            min_free_disk_gib=50.0,
            pairs=pairs,
        )

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)

    outcomes = run_cached_matchups(
        _FakeEngine(), _engine_install(), [spec_win, spec_lose], data_dir=str(data_dir)
    ).outcomes
    assert (outcomes[0].wins, outcomes[0].losses) == (1, 0)
    assert (outcomes[1].wins, outcomes[1].losses) == (0, 1)

    # And the cache is keyed right: a re-run serves each seed its OWN tally.
    second = run_cached_matchups(
        _FakeEngine(), _engine_install(), [spec_win, spec_lose], data_dir=str(data_dir)
    ).outcomes
    assert second[0].cached and (second[0].wins, second[0].losses) == (1, 0)
    assert second[1].cached and (second[1].wins, second[1].losses) == (0, 1)


def test_run_cached_matchups_surfaces_failures(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """A governor failure (no paired result) surfaces on ``MatchupBatch.failures``
    AND records a zeroed placeholder outcome — the two are DISTINCT (B2)."""
    spec = MatchSpec(deck_a=('Cand', 'A'), deck_b=('BadOpp', 'B'), n=2, seed=1, fmt='constructed')

    def fake_run_matchups(engine: object, install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        return PoolResult(
            pool_size=1,
            results=[],
            failures=[MatchFailure(spec=spec, error='Forge could not load a deck')],
            max_concurrent=1,
            aborted=False,
            min_free_ram_gib=8.0,
            min_free_disk_gib=50.0,
            pairs=[],  # no result -> the spec is a failure, not a 0-0-0 game.
        )

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)
    batch = run_cached_matchups(_FakeEngine(), _engine_install(), [spec], data_dir=str(data_dir))
    assert batch.failures == (('BadOpp', 'Forge could not load a deck'),)
    assert batch.aborted is False
    # A zeroed placeholder outcome still fills the row (so the aggregate has one).
    assert (batch.outcomes[0].wins, batch.outcomes[0].losses, batch.outcomes[0].draws) == (0, 0, 0)


def test_run_cached_matchups_propagates_aborted(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """The governor's ``aborted`` flag threads onto the batch (B2)."""
    specs = [MatchSpec(deck_a=('Cand', 'A'), deck_b=('Opp', 'B'), n=1, seed=1)]

    def fake_run_matchups(engine: object, install: object, ss: list[MatchSpec], **kw: object) -> PoolResult:
        pr = _pool_result_candidate_sweeps(ss)
        return PoolResult(
            pool_size=pr.pool_size,
            results=pr.results,
            failures=[],
            max_concurrent=pr.max_concurrent,
            aborted=True,
            min_free_ram_gib=pr.min_free_ram_gib,
            min_free_disk_gib=pr.min_free_disk_gib,
            pairs=pr.pairs,
        )

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)
    batch = run_cached_matchups(_FakeEngine(), _engine_install(), specs, data_dir=str(data_dir))
    assert batch.aborted is True


# --------------------------------------------------------------------------- #
# simulate: aggregation + cache integration.
# --------------------------------------------------------------------------- #


def _patch_gauntlet(monkeypatch: pytest.MonkeyPatch, opponents: list[tuple[str, str]]) -> None:
    from pipeline.sim.gauntlet import GauntletDeck

    def fake_resolve(source: str, fmt: str, **kw: object) -> list[GauntletDeck]:
        return [GauntletDeck(name=n, dck_text=t) for n, t in opponents]

    monkeypatch.setattr(core, 'resolve_gauntlet', fake_resolve)


def test_simulate_aggregates_winrate_ci_and_profile(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """simulate over 3 opponents (candidate sweeps) -> 100% win-rate + profile."""
    _patch_gauntlet(monkeypatch, [('Opp1', 'X'), ('Opp2', 'Y'), ('Opp3', 'Z')])
    monkeypatch.setattr(core, 'run_matchups', lambda e, i, specs, **k: _pool_result_candidate_sweeps(specs))

    result = simulate(
        _deck('Candidate'),
        'curated',
        games=4,
        fmt='constructed',
        seed=7,
        engine=_FakeEngine(),
        data_dir=str(data_dir),
    )

    assert isinstance(result, SimResult)
    assert result.total_games == 12  # 3 opponents * 4 games
    assert result.wins == 12
    assert result.win_rate == pytest.approx(1.0)
    lo, hi = result.win_rate_ci
    assert lo <= 1.0 and hi <= 1.0 and lo > 0.5  # Wilson CI, upper-bounded at 1
    # per-opponent breakdown
    assert len(result.per_opponent) == 3
    assert {o.opponent for o in result.per_opponent} == {'Opp1', 'Opp2', 'Opp3'}
    assert all(o.wins == 4 and o.games == 4 for o in result.per_opponent)
    # aggregate telemetry profile is populated from the parsed logs
    assert result.profile.games == 12
    assert result.profile.avg_kill_turn == pytest.approx(5.0)
    assert result.profile.wincon_mix.get('combat', 0) == 12


def test_simulate_second_run_hits_cache(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """A second identical simulate serves from cache and runs 0 governor batches."""
    _patch_gauntlet(monkeypatch, [('Opp1', 'X'), ('Opp2', 'Y')])
    batches = {'n': 0}

    def fake_run_matchups(engine: object, install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        batches['n'] += 1
        return _pool_result_candidate_sweeps(specs)

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)

    kwargs = {
        'games': 3,
        'fmt': 'constructed',
        'seed': 1,
        'engine': _FakeEngine(),
        'data_dir': str(data_dir),
    }
    first = simulate(_deck('Cand'), 'curated', **kwargs)  # type: ignore[arg-type]
    assert batches['n'] == 1
    assert first.cached_matchups == 0 and first.fresh_matchups == 2

    second = simulate(_deck('Cand'), 'curated', **kwargs)  # type: ignore[arg-type]
    assert batches['n'] == 1  # no fresh Forge work
    assert second.cached_matchups == 2 and second.fresh_matchups == 0
    assert second.win_rate == pytest.approx(first.win_rate)


def test_simulate_accepts_name_dck_tuple(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """The candidate may be a ``(name, dck_text)`` pair, not just a Deck."""
    _patch_gauntlet(monkeypatch, [('Opp1', 'X')])
    monkeypatch.setattr(core, 'run_matchups', lambda e, i, specs, **k: _pool_result_candidate_sweeps(specs))

    result = simulate(
        ('MyCand', 'Name MyCand\n[Main]\n17 Mountain\n'),
        'curated',
        games=2,
        fmt='constructed',
        seed=1,
        engine=_FakeEngine(),
        data_dir=str(data_dir),
    )
    assert result.candidate == 'MyCand'
    assert result.win_rate == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #


def test_compare_diffs_two_variants(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """compare runs A and B over the same gauntlet and diffs their win-rates.

    Variant A sweeps (wins all); variant B is set to lose all — so the win-rate
    delta is +1.0 for A and the comparison names the stronger variant.
    """
    _patch_gauntlet(monkeypatch, [('Opp1', 'X'), ('Opp2', 'Y')])

    def fake_run_matchups(engine: object, install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        # deck_a name tells us which variant is candidate: 'A' sweeps, 'B' loses.
        results: list[MatchResult] = []
        for spec in specs:
            a_wins = spec.n if spec.deck_a[0] == 'A' else 0
            per_game = tuple(GameOutcome(winner='a' if i < a_wins else 'b', elapsed_ms=1000) for i in range(spec.n))
            results.append(
                MatchResult(
                    deck_a=spec.deck_a[0],
                    deck_b=spec.deck_b[0],
                    wins_a=a_wins,
                    wins_b=spec.n - a_wins,
                    draws=0,
                    per_game=per_game,
                    raw_log='',
                )
            )
        return PoolResult(
            pool_size=1,
            results=results,
            failures=[],
            max_concurrent=1,
            aborted=False,
            min_free_ram_gib=8.0,
            min_free_disk_gib=50.0,
            pairs=list(zip(specs, results, strict=True)),
        )

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)

    cmp = compare(
        ('A', 'Name A\n[Main]\n17 Mountain\n'),
        ('B', 'Name B\n[Main]\n17 Plains\n'),
        'curated',
        games=4,
        fmt='constructed',
        seed=1,
        engine=_FakeEngine(),
        data_dir=str(data_dir),
    )

    assert isinstance(cmp, Comparison)
    assert cmp.a.win_rate == pytest.approx(1.0)
    assert cmp.b.win_rate == pytest.approx(0.0)
    assert cmp.win_rate_delta == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# GATED: one REAL curated-gauntlet simulate (capped: pool<=2, games=1).
# --------------------------------------------------------------------------- #


@pytest.mark.forge
def test_simulate_real_curated_gauntlet() -> None:
    """simulate a curated deck vs a SMALL curated gauntlet with real Forge.

    Caps: pool_size=2, games=1, 2 opponents. Reuses the existing install (no
    downloads). Skips if the resource floors aren't met. Requires
    MAKE_MAGIC_FORGE_HOME + MAKE_MAGIC_JAVA.
    """
    from pipeline.sim.engine import get_engine
    from pipeline.sim.gauntlet import resolve_gauntlet
    from pipeline.sim.governor import free_disk_gib, free_ram_gib
    from pipeline.sim.store import deck_hash, find_matchups, get_game_logs

    if not (os.getenv(ENV_FORGE_HOME) and os.getenv(ENV_JAVA)):
        pytest.skip(f'set {ENV_FORGE_HOME} + {ENV_JAVA} to run the gated Forge simulate')
    if free_ram_gib() < 5.0 or free_disk_gib() < 5.0:
        pytest.skip('insufficient free RAM/disk for a capped 2-JVM Forge simulate')

    # Drive the full cached-gauntlet path through the CURRENT engine seam (not the
    # pre-seam ``install=`` arg): simulate resolves the forge engine's install
    # read-only for the cache-key version and launches the sim-AI harness for any
    # fresh matchup.
    engine = get_engine('forge')
    # Use two curated decks: the first is our candidate, the next two are the gauntlet.
    curated = resolve_gauntlet('curated', 'constructed')
    assert len(curated) >= 3
    candidate = (curated[0].name, curated[0].dck_text)

    # Restrict the gauntlet to 2 opponents by monkeypatch-free slicing: patch
    # resolve_gauntlet via a tiny shim so simulate sees exactly 2 opponents.
    opponents = curated[1:3]

    def _two(source: str, fmt: str, **kw: object) -> list:
        return list(opponents)

    core.resolve_gauntlet = _two  # type: ignore[assignment]
    try:
        result = simulate(
            candidate,
            'curated',
            games=1,
            fmt='constructed',
            seed=42,
            engine=engine,
            force=True,  # force fresh Forge games so the sim-AI assertion has a log
            pool_size=2,
        )
    finally:
        from pipeline.sim.gauntlet import resolve_gauntlet as _orig

        core.resolve_gauntlet = _orig  # type: ignore[assignment]

    assert isinstance(result, SimResult)
    assert result.total_games == 2
    assert 0.0 <= result.win_rate <= 1.0
    assert len(result.per_opponent) == 2
    assert not result.aborted
    # The pooled profile aggregates only DECISIVE games. The sim AI is ~30%
    # non-decisive (NPE / clocked-out draw, see forge.py `_FORGE_CAPABILITIES`), so
    # with only 2 games BOTH can occasionally clock out -> 0 decisive games. That is
    # a VALID outcome, not a failure (the old `1 <= profile.games` assertion flaked
    # ~9% of runs on it). Assert it stays within range either way.
    assert 0 <= result.profile.games <= result.total_games

    # STRENGTHEN: prove the run used the sim AI, not the stock ``sim`` heuristic.
    # The engine version (``<FORGE_VERSION>+simai-``) proves the harness is wired
    # REGARDLESS of the game outcome — the robust, always-checkable proof.
    install = engine.resolve(provision=False)
    assert install.version.startswith(f'{FORGE_VERSION}+simai-')

    # The stronger RUNTIME proof (the harness ``useSimulationAI=true`` banner + its
    # ``HANDLOG`` decision stream) lives in the retained verbose log — but only
    # DECISIVE games are stored (clockout logs are discarded), so it's only
    # available when >=1 game decided. Scope the lookup to THIS run's matchups
    # (the candidate is deck_a) so the proof is about our games, not any unrelated
    # stored run. If both games clocked out (rare), the version check above already
    # proves the harness; assert the non-decisive path is coherent and flag it.
    rows = find_matchups(deck_a_hash=deck_hash(candidate[1]), fmt='constructed')
    combined = '\n'.join(log for row in rows for log in get_game_logs(row.matchup_key))
    if combined:
        assert result.profile.games >= 1  # a decisive game was retained
        assert 'useSimulationAI=true' in combined  # the harness banner — stock `sim` lacks it
        assert 'HANDLOG' in combined  # the sim-AI decision stream — stock `sim` lacks it
    else:
        assert result.profile.games == 0  # both games non-decisive -> nothing retained
        print('[forge] both games non-decisive (clocked out) — sim-AI proven via engine version only')

    print(
        f'\n[forge] simulate {result.candidate} vs {len(result.per_opponent)} curated opponents: '
        f'win_rate={result.win_rate:.2f} CI={result.win_rate_ci} '
        f'avg_kill_turn={result.profile.avg_kill_turn} wincon_mix={result.profile.wincon_mix}'
    )
