"""TDD tests for the sim core: cached matchups, simulate, compare (Phase 6, B).

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
from typing import cast

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
from pipeline.sim.forge_runtime import ENV_FORGE_HOME, ENV_JAVA, ForgeInstall
from pipeline.sim.governor import MatchSpec, PoolResult
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


def _install_probe() -> ForgeInstall:
    """A sentinel 'install' — the mocked governor ignores it.

    Cast to ``ForgeInstall`` so the type-checker is satisfied; the mocked
    ``run_matchups`` never touches it, so its runtime shape is irrelevant.
    """
    return cast('ForgeInstall', object())


# --------------------------------------------------------------------------- #
# run_cached_matchups: cache miss -> run -> store; hit -> 0 games.
# --------------------------------------------------------------------------- #


def test_run_cached_matchups_miss_then_hit(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """First call runs the (mocked) games and stores; second hits the cache, 0 games."""
    calls: list[list[MatchSpec]] = []

    def fake_run_matchups(install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        calls.append(specs)
        return _pool_result_candidate_sweeps(specs)

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)
    monkeypatch.setattr(core, 'forge_version', lambda: 'test-forge')

    specs = [
        MatchSpec(deck_a=('Cand', 'A'), deck_b=('Opp', 'B'), n=4, seed=1, fmt='constructed'),
    ]

    first = run_cached_matchups(_install_probe(), specs, data_dir=str(data_dir))
    assert len(first) == 1
    assert first[0].wins == 4
    assert first[0].cached is False
    assert first[0].features  # telemetry parsed + returned
    assert len(calls) == 1  # one governor batch ran

    second = run_cached_matchups(_install_probe(), specs, data_dir=str(data_dir))
    assert second[0].wins == 4
    assert second[0].cached is True
    assert len(calls) == 1  # NO second governor batch — served from cache


def test_run_cached_matchups_force_bypasses_cache(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """``force=True`` re-runs even a cached matchup."""
    calls: list[list[MatchSpec]] = []

    def fake_run_matchups(install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        calls.append(specs)
        return _pool_result_candidate_sweeps(specs)

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)
    monkeypatch.setattr(core, 'forge_version', lambda: 'test-forge')

    specs = [MatchSpec(deck_a=('Cand', 'A'), deck_b=('Opp', 'B'), n=2, seed=1, fmt='constructed')]

    run_cached_matchups(_install_probe(), specs, data_dir=str(data_dir))
    run_cached_matchups(_install_probe(), specs, force=True, data_dir=str(data_dir))
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

    def fake_run_matchups(install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
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
    monkeypatch.setattr(core, 'forge_version', lambda: 'test-forge')

    outcomes = run_cached_matchups(_install_probe(), [spec_win, spec_lose], data_dir=str(data_dir))
    assert (outcomes[0].wins, outcomes[0].losses) == (1, 0)
    assert (outcomes[1].wins, outcomes[1].losses) == (0, 1)

    # And the cache is keyed right: a re-run serves each seed its OWN tally.
    second = run_cached_matchups(_install_probe(), [spec_win, spec_lose], data_dir=str(data_dir))
    assert second[0].cached and (second[0].wins, second[0].losses) == (1, 0)
    assert second[1].cached and (second[1].wins, second[1].losses) == (0, 1)


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
    monkeypatch.setattr(core, 'run_matchups', lambda i, specs, **k: _pool_result_candidate_sweeps(specs))
    monkeypatch.setattr(core, 'forge_version', lambda: 'test-forge')

    result = simulate(
        _deck('Candidate'),
        'curated',
        games=4,
        fmt='constructed',
        seed=7,
        install=_install_probe(),
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

    def fake_run_matchups(install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
        batches['n'] += 1
        return _pool_result_candidate_sweeps(specs)

    monkeypatch.setattr(core, 'run_matchups', fake_run_matchups)
    monkeypatch.setattr(core, 'forge_version', lambda: 'test-forge')

    kwargs = {
        'games': 3,
        'fmt': 'constructed',
        'seed': 1,
        'install': _install_probe(),
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
    monkeypatch.setattr(core, 'run_matchups', lambda i, specs, **k: _pool_result_candidate_sweeps(specs))
    monkeypatch.setattr(core, 'forge_version', lambda: 'test-forge')

    result = simulate(
        ('MyCand', 'Name MyCand\n[Main]\n17 Mountain\n'),
        'curated',
        games=2,
        fmt='constructed',
        seed=1,
        install=_install_probe(),
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

    def fake_run_matchups(install: object, specs: list[MatchSpec], **kw: object) -> PoolResult:
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
    monkeypatch.setattr(core, 'forge_version', lambda: 'test-forge')

    cmp = compare(
        ('A', 'Name A\n[Main]\n17 Mountain\n'),
        ('B', 'Name B\n[Main]\n17 Plains\n'),
        'curated',
        games=4,
        fmt='constructed',
        seed=1,
        install=_install_probe(),
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
    from pipeline.sim.forge_runtime import resolve
    from pipeline.sim.gauntlet import resolve_gauntlet
    from pipeline.sim.governor import free_disk_gib, free_ram_gib

    if not (os.getenv(ENV_FORGE_HOME) and os.getenv(ENV_JAVA)):
        pytest.skip(f'set {ENV_FORGE_HOME} + {ENV_JAVA} to run the gated Forge simulate')
    if free_ram_gib() < 5.0 or free_disk_gib() < 5.0:
        pytest.skip('insufficient free RAM/disk for a capped 2-JVM Forge simulate')

    install = resolve()
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
            install=install,
            pool_size=2,
        )
    finally:
        from pipeline.sim.gauntlet import resolve_gauntlet as _orig

        core.resolve_gauntlet = _orig  # type: ignore[assignment]

    assert isinstance(result, SimResult)
    assert result.total_games == 2
    assert 0.0 <= result.win_rate <= 1.0
    assert len(result.per_opponent) == 2
    assert result.profile.games == 2
    print(
        f'\n[forge] simulate {result.candidate} vs {len(result.per_opponent)} curated opponents: '
        f'win_rate={result.win_rate:.2f} CI={result.win_rate_ci} '
        f'avg_kill_turn={result.profile.avg_kill_turn} wincon_mix={result.profile.wincon_mix}'
    )
