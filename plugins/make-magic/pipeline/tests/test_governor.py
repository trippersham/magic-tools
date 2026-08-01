"""Tests for the resource-safety concurrency governor (:mod:`pipeline.sim.governor`).

The OFFLINE tests are the resource-safety core: they prove the runtime pool-size
math, the admission floor/back-off logic, the ~5 s staggered starts, and — most
importantly — that the governor NEVER runs more than ``pool_size`` JVMs at once.
No real Forge is spawned offline; ``run_matchup`` is monkeypatched with a mock
that records concurrency. ONE gated ``@pytest.mark.forge`` test runs a SMALL real
batch (``pool_size=2``, <=4 JVMs total) and asserts the same concurrency bound
holds against the live install.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from pipeline.sim import governor as gov
from pipeline.sim.forge_runtime import ENV_FORGE_HOME, ENV_JAVA, ForgeInstall, resolve
from pipeline.sim.governor import (
    MatchSpec,
    PoolResult,
    derive_pool_size,
    run_matchups,
)
from pipeline.sim.runner import ForgeError, GameOutcome, MatchResult

# --------------------------------------------------------------------------- #
# Helpers: build specs without spawning Forge.
# --------------------------------------------------------------------------- #


def _spec(seed: int, *, n: int = 1) -> MatchSpec:
    return MatchSpec(
        deck_a=('RedTest', 'DUMMY_A'),
        deck_b=('WhiteTest', 'DUMMY_B'),
        n=n,
        seed=seed,
    )


# --------------------------------------------------------------------------- #
# derive_pool_size — pure math: min(hard_cap, cores-2, mem_budget), floor >= 1
# --------------------------------------------------------------------------- #


def test_pool_size_bounded_by_hard_cap() -> None:
    # Plenty of cores + RAM -> clamped to hard_cap.
    assert derive_pool_size(hard_cap=6, per_jvm_gib=2.0, cores=64, free_mem_gib=256.0) == 6


def test_pool_size_bounded_by_cores_minus_two() -> None:
    # 6 cores -> cores-2 == 4 is the binding constraint (RAM ample, cap high).
    assert derive_pool_size(hard_cap=99, per_jvm_gib=2.0, cores=6, free_mem_gib=256.0) == 4


def test_pool_size_bounded_by_memory_budget() -> None:
    # 5 GiB free / 2 GiB per JVM == 2 JVMs; RAM is the binding constraint.
    assert derive_pool_size(hard_cap=99, per_jvm_gib=2.0, cores=64, free_mem_gib=5.0) == 2


def test_pool_size_never_below_one_even_when_starved() -> None:
    # 1 core and ~0 free RAM would compute <=0; the >=1 floor guarantees 1.
    assert derive_pool_size(hard_cap=6, per_jvm_gib=2.0, cores=1, free_mem_gib=0.5) == 1


def test_pool_size_takes_the_min_of_all_three() -> None:
    # cores-2 == 2, mem == 3, cap == 6 -> min is 2 (cores).
    assert derive_pool_size(hard_cap=6, per_jvm_gib=2.0, cores=4, free_mem_gib=8.0) == 2


def test_pool_size_reads_live_resources_when_not_injected() -> None:
    # Un-injected call must still return a sane bounded pool (>=1, <= hard_cap).
    pool = derive_pool_size(hard_cap=6)
    assert 1 <= pool <= 6


# --------------------------------------------------------------------------- #
# Pool orchestration — never exceeds pool_size concurrent JVMs.
# --------------------------------------------------------------------------- #


class _ConcurrencyTracker:
    """A mock ``run_matchup`` that holds each 'JVM' briefly while recording the
    max number of simultaneous in-flight calls."""

    def __init__(self, hold_s: float = 0.05) -> None:
        self.hold_s = hold_s
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self.seeds: list[int] = []

    def __call__(
        self,
        install: object,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str = 'constructed',
        timeout_s: int = 30,
    ) -> MatchResult:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.calls += 1
            self.seeds.append(seed)
        try:
            time.sleep(self.hold_s)
        finally:
            with self._lock:
                self.in_flight -= 1
        return MatchResult(
            deck_a=deck_a[0], deck_b=deck_b[0], wins_a=n, wins_b=0, draws=0,
            per_game=tuple(GameOutcome(winner='a', elapsed_ms=1) for _ in range(n)),
            raw_log='',
        )


def test_governor_never_exceeds_pool_size(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _ConcurrencyTracker(hold_s=0.05)
    monkeypatch.setattr(gov, 'run_matchup', tracker)

    specs = [_spec(1000 + i) for i in range(12)]
    result = run_matchups(
        install=None,  # type: ignore[arg-type]  # unused by the mock
        specs=specs,
        pool_size=3,
        stagger_s=0.0,  # no stagger delay in the unit test
    )

    assert isinstance(result, PoolResult)
    assert tracker.calls == 12
    assert tracker.max_in_flight <= 3, f'saw {tracker.max_in_flight} concurrent'
    assert result.pool_size == 3
    assert len(result.results) == 12
    assert not result.failures


def test_governor_returns_all_results_and_derived_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _ConcurrencyTracker(hold_s=0.0)
    monkeypatch.setattr(gov, 'run_matchup', tracker)

    specs = [_spec(2000 + i) for i in range(5)]
    # pool_size omitted -> derived from live resources; must be a sane int.
    result = run_matchups(install=None, specs=specs, stagger_s=0.0)  # type: ignore[arg-type]
    assert result.pool_size >= 1
    assert len(result.results) == 5
    assert sorted(r.deck_a for r in result.results) == ['RedTest'] * 5


def test_governor_pairs_every_result_to_its_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PoolResult.pairs`` binds each result to the EXACT spec that produced it.

    Deck NAMES are not unique across specs (e.g. a `both` gauntlet where a user
    deck shares a curated deck's name), so name-based pairing can attribute a
    result to the wrong spec — and the core would then store it under the wrong
    cache key. The seed is echoed into ``elapsed_ms`` so the binding is provable.
    """

    def echo_seed(
        install: object,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str = 'constructed',
        timeout_s: int = 30,
    ) -> MatchResult:
        return MatchResult(
            deck_a=deck_a[0],
            deck_b=deck_b[0],
            wins_a=n,
            wins_b=0,
            draws=0,
            per_game=tuple(GameOutcome(winner='a', elapsed_ms=seed) for _ in range(n)),
            raw_log='',
        )

    monkeypatch.setattr(gov, 'run_matchup', echo_seed)

    # Identical names/text across all specs — ONLY the seed distinguishes them.
    specs = [_spec(3000 + i) for i in range(4)]
    result = run_matchups(install=None, specs=specs, pool_size=2, stagger_s=0.0)  # type: ignore[arg-type]

    assert len(result.pairs) == 4
    for spec, match in result.pairs:
        assert match.per_game[0].elapsed_ms == spec.seed  # exact spec<->result binding


# --------------------------------------------------------------------------- #
# Failure handling — a failed/timed-out matchup is recorded, not raised.
# --------------------------------------------------------------------------- #


def test_governor_records_failures_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    def flaky(install, deck_a, deck_b, *, n, seed, fmt='constructed', timeout_s=30):
        if seed % 2 == 0:
            raise ForgeError(f'boom seed={seed}')
        return MatchResult(
            deck_a=deck_a[0], deck_b=deck_b[0], wins_a=n, wins_b=0, draws=0,
            per_game=tuple(GameOutcome(winner='a', elapsed_ms=1) for _ in range(n)),
            raw_log='',
        )

    monkeypatch.setattr(gov, 'run_matchup', flaky)
    specs = [_spec(seed) for seed in (10, 11, 12, 13)]  # 10,12 fail; 11,13 pass
    result = run_matchups(install=None, specs=specs, pool_size=2, stagger_s=0.0)  # type: ignore[arg-type]

    assert len(result.results) == 2
    assert len(result.failures) == 2
    failed_seeds = {f.spec.seed for f in result.failures}
    assert failed_seeds == {10, 12}
    assert all('boom' in f.error for f in result.failures)


# --------------------------------------------------------------------------- #
# Admission floors — below-floor RAM/disk -> back off (do not spawn).
# --------------------------------------------------------------------------- #


def test_admission_waits_when_below_ram_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """First admission check sees RAM below the floor and must back off; a later
    check sees RAM recovered and admits. Proves the governor re-checks per spawn
    and does not spawn while starved."""
    tracker = _ConcurrencyTracker(hold_s=0.0)
    monkeypatch.setattr(gov, 'run_matchup', tracker)

    # RAM readings: first two below floor (back off), then ample forever.
    readings = iter([1.0, 1.0])
    monkeypatch.setattr(gov, 'free_ram_gib', lambda: next(readings, 64.0))
    monkeypatch.setattr(gov, 'free_disk_gib', lambda _p=None: 500.0)

    sleeps: list[float] = []
    monkeypatch.setattr(gov.time, 'sleep', lambda s: sleeps.append(s))

    specs = [_spec(3000 + i) for i in range(2)]
    result = run_matchups(
        install=None,  # type: ignore[arg-type]
        specs=specs,
        pool_size=2,
        ram_floor_gib=2.0,
        disk_floor_gib=1.0,
        stagger_s=0.0,
    )
    # It backed off at least once (slept) before admitting all work.
    assert sleeps, 'expected a back-off sleep while below the RAM floor'
    assert len(result.results) == 2


def test_admission_aborts_when_disk_floor_breached_persistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent below-floor disk -> stop admitting and return a partial result
    with an aborted status rather than spinning forever."""
    tracker = _ConcurrencyTracker(hold_s=0.0)
    monkeypatch.setattr(gov, 'run_matchup', tracker)
    monkeypatch.setattr(gov, 'free_ram_gib', lambda: 64.0)
    monkeypatch.setattr(gov, 'free_disk_gib', lambda _p=None: 0.1)  # always below floor
    monkeypatch.setattr(gov.time, 'sleep', lambda _s: None)

    specs = [_spec(4000 + i) for i in range(3)]
    result = run_matchups(
        install=None,  # type: ignore[arg-type]
        specs=specs,
        pool_size=2,
        disk_floor_gib=1.0,
        stagger_s=0.0,
        max_admission_backoffs=3,  # bounded so the test can't hang
    )
    assert result.aborted is True
    assert tracker.calls == 0  # nothing was ever admitted
    assert len(result.results) == 0


# --------------------------------------------------------------------------- #
# Stagger — ~5 s staggered starts are honored between admissions.
# --------------------------------------------------------------------------- #


def test_stagger_is_honored_between_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _ConcurrencyTracker(hold_s=0.0)
    monkeypatch.setattr(gov, 'run_matchup', tracker)
    monkeypatch.setattr(gov, 'free_ram_gib', lambda: 64.0)
    monkeypatch.setattr(gov, 'free_disk_gib', lambda _p=None: 500.0)

    sleeps: list[float] = []
    monkeypatch.setattr(gov.time, 'sleep', lambda s: sleeps.append(s))

    specs = [_spec(5000 + i) for i in range(3)]
    run_matchups(install=None, specs=specs, pool_size=3, stagger_s=5.0)  # type: ignore[arg-type]

    # At least the two admissions after the first waited ~5 s each.
    stagger_sleeps = [s for s in sleeps if s == pytest.approx(5.0)]
    assert len(stagger_sleeps) >= 2


# --------------------------------------------------------------------------- #
# Resource readers — stdlib only, return sane non-negative numbers.
# --------------------------------------------------------------------------- #


def test_resource_readers_return_non_negative() -> None:
    assert gov.free_ram_gib() >= 0.0
    assert gov.free_disk_gib() >= 0.0
    # cores derived internally; a bounded pool proves the readers wired up.
    assert derive_pool_size(hard_cap=6) >= 1


# --------------------------------------------------------------------------- #
# GATED: SMALL real batch — pool_size=2, <=4 JVMs total. Deselected by default.
# --------------------------------------------------------------------------- #


@pytest.mark.forge
def test_run_small_real_batch_bounded_concurrency() -> None:
    """Run 4 real matchups (n=1) through a pool_size=2 governor and assert every
    matchup completes AND the governor never exceeded 2 concurrent JVMs.

    HARD CAP: pool_size=2, 4 matchups => <=4 JVMs total, each -Xmx2g + external
    kill. Skips with a clear message if the env overrides are unset or free
    RAM/disk is too low to run safely.
    """
    if not (os.getenv(ENV_FORGE_HOME) and os.getenv(ENV_JAVA)):
        pytest.skip(f'set {ENV_FORGE_HOME} + {ENV_JAVA} to run the gated Forge batch')

    # Resource-safety pre-check for OUR OWN run: need headroom for 2x -Xmx2g.
    if gov.free_ram_gib() < 6.0:
        pytest.skip(f'free RAM {gov.free_ram_gib():.1f} GiB < 6 GiB; skipping the real batch')
    if gov.free_disk_gib() < 2.0:
        pytest.skip(f'free disk {gov.free_disk_gib():.1f} GiB < 2 GiB; skipping the real batch')

    install: ForgeInstall = resolve()
    profile_decks = Path.home() / 'Library' / 'Application Support' / 'Forge' / 'decks' / 'constructed'
    text_a = (profile_decks / 'RedTest.dck').read_text()
    text_b = (profile_decks / 'WhiteTest.dck').read_text()

    specs = [
        MatchSpec(deck_a=('RedTest', text_a), deck_b=('WhiteTest', text_b), n=1, seed=42 + i)
        for i in range(4)
    ]

    result = run_matchups(
        install,
        specs,
        pool_size=2,  # HARD CAP — never raise this in the gated test.
        stagger_s=5.0,
        ram_floor_gib=2.0,
        disk_floor_gib=1.0,
    )

    assert not result.aborted
    assert len(result.results) == 4, f'failures: {[f.error for f in result.failures]}'
    assert not result.failures
    assert result.pool_size == 2
    assert result.max_concurrent <= 2, f'saw {result.max_concurrent} concurrent JVMs'
    for r in result.results:
        assert r.games == 1
        assert r.wins_a + r.wins_b + r.draws == 1
    print(
        f'\n[forge] batch of 4 @ pool_size=2: max_concurrent={result.max_concurrent}, '
        f'min_free_ram={result.min_free_ram_gib:.1f} GiB'
    )
