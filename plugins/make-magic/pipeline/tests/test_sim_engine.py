"""OFFLINE tests for the backend-agnostic engine seam (:mod:`pipeline.sim.engine`).

This is the load-bearing interface boundary between the sim core and a concrete
backend (Forge / XMage). Nothing here touches a JVM, the network, or a real
install: a ``FakeEngine`` implements the :class:`~pipeline.sim.engine.SimEngine`
Protocol with canned data so the seam's contract (registry round-trip, runtime
``isinstance`` conformance, capabilities surface, the never-crash
``EngineUnavailableError`` on an unavailable ``resolve``) is exercised purely.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pipeline.sim.engine import (
    EngineCapabilities,
    EngineInstall,
    EngineUnavailableError,
    SimEngine,
    available_engines,
    get_engine,
    register_engine,
)
from pipeline.sim.runner import GameOutcome, MatchResult

# --------------------------------------------------------------------------- #
# helpers — a pure in-memory engine that satisfies the Protocol
# --------------------------------------------------------------------------- #

_FAKE_CAPS = EngineCapabilities(
    has_hand_visibility=True,
    has_counter_metrics=False,
    expected_nondecisive_rate=0.05,
    reliability_note='fake engine — canned results only',
    kill_attribution='named',
)


class FakeEngine:
    """A no-JVM engine that returns canned data and satisfies :class:`SimEngine`.

    ``available`` toggles whether :meth:`resolve` yields an install or raises
    :class:`EngineUnavailableError` — so a test can drive the never-crash path
    without a real backend.
    """

    def __init__(self, *, name: str = 'fake', available: bool = True) -> None:
        self._name = name
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> EngineCapabilities:
        return _FAKE_CAPS

    def resolve(self, *, provision: bool, data_dir: Path | None = None) -> EngineInstall:
        del data_dir
        if not self._available:
            raise EngineUnavailableError(
                f'{self._name} is unavailable (provision={provision}); this is the actionable, never-crash contract.'
            )
        return EngineInstall(version='0.0.0-fake', handle=object())

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
            raw_log='fake log',
        )

    def replay(self, matchup_key: str, game_idx: int) -> str:
        return f'fake replay {matchup_key}#{game_idx}'


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    """Isolate every test from registry state written by another test."""
    from pipeline.sim import engine as engine_mod

    saved = dict(engine_mod._REGISTRY)
    engine_mod._REGISTRY.clear()
    try:
        yield
    finally:
        engine_mod._REGISTRY.clear()
        engine_mod._REGISTRY.update(saved)


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #


def test_fake_engine_is_a_sim_engine() -> None:
    """A duck-typed engine satisfies the runtime_checkable Protocol."""
    assert isinstance(FakeEngine(), SimEngine)


def test_capabilities_fields_surface() -> None:
    caps = FakeEngine().capabilities()
    assert caps.has_hand_visibility is True
    assert caps.has_counter_metrics is False
    assert caps.expected_nondecisive_rate == pytest.approx(0.05)
    assert caps.reliability_note
    assert caps.kill_attribution == 'named'


def test_capabilities_is_frozen() -> None:
    caps = FakeEngine().capabilities()
    with pytest.raises((AttributeError, TypeError)):
        caps.has_hand_visibility = False  # type: ignore[misc]


def test_run_matchup_returns_real_match_result() -> None:
    engine = FakeEngine()
    install = engine.resolve(provision=False)
    result = engine.run_matchup(
        ('Cand', 'DECK A'),
        ('Opp', 'DECK B'),
        n=3,
        seed=7,
        fmt='commander',
        install=install,
    )
    assert isinstance(result, MatchResult)
    assert result.deck_a == 'Cand'
    assert result.wins_a == 3
    assert result.games == 3


def test_engine_install_is_frozen() -> None:
    install = EngineInstall(version='1.2.3', handle=object())
    assert install.version == '1.2.3'
    with pytest.raises((AttributeError, TypeError)):
        install.version = '9.9.9'  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# resolve() never crashes — raises the actionable EngineUnavailableError
# --------------------------------------------------------------------------- #


def test_resolve_unavailable_raises_engine_unavailable_error() -> None:
    engine = FakeEngine(available=False)
    with pytest.raises(EngineUnavailableError):
        engine.resolve(provision=False)


def test_engine_unavailable_error_is_runtime_error() -> None:
    assert issubclass(EngineUnavailableError, RuntimeError)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_register_and_get_round_trip() -> None:
    engine = FakeEngine(name='forge')
    register_engine(engine)
    assert get_engine('forge') is engine


def test_available_engines_lists_registered() -> None:
    register_engine(FakeEngine(name='forge'))
    register_engine(FakeEngine(name='xmage'))
    assert sorted(available_engines()) == ['forge', 'xmage']


def test_get_unknown_engine_raises_clear_error_listing_known() -> None:
    register_engine(FakeEngine(name='forge'))
    with pytest.raises(KeyError) as excinfo:
        get_engine('nope')
    message = str(excinfo.value)
    assert 'nope' in message
    assert 'forge' in message


def test_get_unknown_with_empty_registry_names_none_known() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_engine('nope')
    assert 'nope' in str(excinfo.value)


def test_re_register_overwrites_same_name() -> None:
    first = FakeEngine(name='forge')
    second = FakeEngine(name='forge')
    register_engine(first)
    register_engine(second)
    assert get_engine('forge') is second
    assert available_engines() == ['forge']
