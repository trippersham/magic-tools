"""OFFLINE tests for the provisional Forge :class:`~pipeline.sim.engine.SimEngine`.

The :class:`~pipeline.sim.engines.forge.ForgeEngine` is a behaviour-preserving
adapter: it satisfies the engine seam by delegating to today's stock-``sim`` path
(:mod:`pipeline.sim.forge_runtime` + :func:`pipeline.sim.runner.run_matchup`).
Nothing here touches a JVM, the network, or a real install — the registry
round-trip, the provisional capabilities surface, the delegation to the runner,
and the never-crash ``EngineUnavailableError`` (via a monkeypatched
``forge_runtime.resolve``) are all exercised purely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.sim import forge_runtime
from pipeline.sim.engine import EngineInstall, EngineUnavailableError, SimEngine, get_engine
from pipeline.sim.engines.forge import _DEFAULT_TIMEOUT_S, ForgeEngine
from pipeline.sim.forge_runtime import FORGE_VERSION, ForgeInstall, ForgeUnavailableError
from pipeline.sim.runner import GameOutcome, MatchResult


def _install() -> ForgeInstall:
    """A dummy resolved Forge install (paths never touched — resolve is mocked)."""
    return ForgeInstall(forge_dir=Path('/tmp/forge'), jar=Path('/tmp/forge/f.jar'), java=Path('/tmp/java'))


# --------------------------------------------------------------------------- #
# registry — importing the sim package registers the forge engine.
# --------------------------------------------------------------------------- #


def test_get_engine_forge_returns_forge_engine() -> None:
    """``get_engine('forge')`` resolves the registered :class:`ForgeEngine`."""
    engine = get_engine('forge')
    assert isinstance(engine, ForgeEngine)
    assert engine.name == 'forge'


def test_forge_engine_is_a_sim_engine() -> None:
    """The ForgeEngine satisfies the runtime_checkable SimEngine Protocol."""
    assert isinstance(ForgeEngine(), SimEngine)


# --------------------------------------------------------------------------- #
# capabilities — the LIVE sim-AI harness reality (SimAIMatch -sim 1).
# --------------------------------------------------------------------------- #


def test_capabilities_describe_sim_ai() -> None:
    caps = ForgeEngine().capabilities()
    assert caps.has_hand_visibility is True  # HANDLOG exposes player-1's hand
    assert caps.has_counter_metrics is True  # HANDLOG exposes stack casts
    assert caps.kill_attribution == 'named'
    # At the 90s draw clock, clockouts are rare — ~5% nondecisive (NPE / genuinely
    # stalled board), down from the pre-fix 0.12 that a 30s clock was masking.
    assert caps.expected_nondecisive_rate == pytest.approx(0.05)
    assert 'simulation ai' in caps.reliability_note.lower()


# --------------------------------------------------------------------------- #
# resolve — delegates to forge_runtime; never crashes the caller.
# --------------------------------------------------------------------------- #


def test_resolve_wraps_forge_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only ``resolve`` wraps the runtime's ForgeInstall in an EngineInstall."""
    forge = _install()
    monkeypatch.setattr(forge_runtime, 'resolve', lambda **_: forge)
    install = ForgeEngine().resolve(provision=False)
    assert isinstance(install, EngineInstall)
    assert install.version == FORGE_VERSION
    assert install.handle is forge


def test_resolve_provision_delegates_to_ensure(monkeypatch: pytest.MonkeyPatch) -> None:
    """``provision=True`` fetches via ``forge_runtime.ensure`` (not the read-only resolve)."""
    forge = _install()
    monkeypatch.setattr(forge_runtime, 'resolve', lambda **_: pytest.fail('provision must not read-only resolve'))
    seen: dict[str, object] = {}

    def _ensure(**kw: object) -> ForgeInstall:
        seen.update(kw)
        return forge

    monkeypatch.setattr(forge_runtime, 'ensure', _ensure)
    install = ForgeEngine().resolve(provision=True, data_dir=Path('/tmp/cache'))
    assert install.handle is forge
    assert seen.get('data_dir') == Path('/tmp/cache')  # data_dir threaded through
    assert 'on_fetch' in seen  # the one-time download notice UX is preserved


def test_resolve_unavailable_raises_engine_unavailable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ForgeUnavailableError is re-raised AS EngineUnavailableError (message kept)."""

    def _raise(**_: object) -> ForgeInstall:
        raise ForgeUnavailableError('No Forge install found. Set MAKE_MAGIC_FORGE_HOME ...')

    monkeypatch.setattr(forge_runtime, 'resolve', _raise)
    with pytest.raises(EngineUnavailableError, match='MAKE_MAGIC_FORGE_HOME'):
        ForgeEngine().resolve(provision=False)


# --------------------------------------------------------------------------- #
# run_matchup — unwraps the handle and delegates to the unchanged runner.
# --------------------------------------------------------------------------- #


def test_run_matchup_delegates_to_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_matchup`` unwraps ``install.handle`` and calls ``runner.run_matchup``."""
    forge = _install()
    seen: dict[str, object] = {}

    def _fake_run_matchup(
        handle: ForgeInstall,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str = 'constructed',
        timeout_s: int = 30,
    ) -> MatchResult:
        seen.update(handle=handle, n=n, seed=seed, fmt=fmt, timeout_s=timeout_s)
        return MatchResult(
            deck_a=deck_a[0],
            deck_b=deck_b[0],
            wins_a=n,
            wins_b=0,
            draws=0,
            per_game=tuple(GameOutcome(winner='a', elapsed_ms=1) for _ in range(n)),
            raw_log='',
        )

    monkeypatch.setattr('pipeline.sim.runner.run_matchup', _fake_run_matchup)
    engine = ForgeEngine()
    install = EngineInstall(version=FORGE_VERSION, handle=forge)
    result = engine.run_matchup(('A', 'DA'), ('B', 'DB'), n=2, seed=7, fmt='commander', install=install)

    assert seen['handle'] is forge  # the ForgeInstall handle is unwrapped and passed through
    assert seen['n'] == 2 and seen['seed'] == 7 and seen['fmt'] == 'commander'
    # timeout_s=None -> the engine's sim-appropriate default draw clock (B1a: 90s,
    # raised from 30s to stop clocked-out games becoming fabricated wins).
    assert seen['timeout_s'] == _DEFAULT_TIMEOUT_S
    assert result.wins_a == 2


def test_run_matchup_forwards_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``timeout_s`` is forwarded to the runner verbatim."""
    seen: dict[str, object] = {}

    def _fake_run_matchup(handle: object, a: tuple[str, str], b: tuple[str, str], **kw: object) -> MatchResult:
        seen.update(kw)
        return MatchResult(
            deck_a=a[0],
            deck_b=b[0],
            wins_a=1,
            wins_b=0,
            draws=0,
            per_game=(GameOutcome(winner='a', elapsed_ms=1),),
            raw_log='',
        )

    monkeypatch.setattr('pipeline.sim.runner.run_matchup', _fake_run_matchup)
    install = EngineInstall(version=FORGE_VERSION, handle=_install())
    ForgeEngine().run_matchup(('A', 'DA'), ('B', 'DB'), n=1, seed=1, fmt='constructed', install=install, timeout_s=90)
    assert seen['timeout_s'] == 90


# --------------------------------------------------------------------------- #
# replay — delegates to the store's game-log retrieval.
# --------------------------------------------------------------------------- #


def test_replay_returns_stored_game_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """``replay`` returns the single stored game log for the (key, game_idx)."""
    monkeypatch.setattr('pipeline.sim.store.get_game_logs', lambda key, *, game_index: [f'log {key}#{game_index}'])
    assert ForgeEngine().replay('abc123', 2) == 'log abc123#2'


def test_replay_missing_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing log yields '' (never raises)."""
    monkeypatch.setattr('pipeline.sim.store.get_game_logs', lambda key, *, game_index: [])
    assert ForgeEngine().replay('nope', 0) == ''
