"""Offline tests for the XMage :class:`~pipeline.sim.engines.xmage.XMageEngine`.

No JVM / no XMage reactor: exercises the deck translation, the declared
capabilities, the registry wiring, and the never-crash resolve contract. The
real headless CP7 run is validated end-to-end via the pipeline (see the task-2.3
behavioral evidence) + the telemetry drop-in in ``test_telemetry_xmage.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.sim.engine import EngineUnavailableError, SimEngine, get_engine
from pipeline.sim.engines import xmage as xmage_engine
from pipeline.sim.engines.xmage import (
    XMageEngine,
    XMageError,
    _clone_tree_cow,
    _forge_dck_to_xmage_txt,
    _stage_private_db,
)
from pipeline.sim.xmage_runtime import XMageInstall

# The card-DB warm memo is per-INSTANCE (XMageEngine is a registered singleton), so
# each test that exercises warming makes its OWN XMageEngine() → fresh empty memo,
# no reset fixture needed.


def _install(tmp_path: Path) -> XMageInstall:
    """A dummy resolved install (paths never launched — the JVM is mocked)."""
    return XMageInstall(mage_tests_dir=tmp_path, classpath='cp.jar', java=Path('/tmp/java'))


def test_registered_and_is_sim_engine() -> None:
    engine = get_engine('xmage')
    assert isinstance(engine, XMageEngine)
    assert isinstance(engine, SimEngine)
    assert engine.name == 'xmage'


def test_capabilities_reflect_xmage_cp7() -> None:
    caps = XMageEngine().capabilities()
    assert caps.has_hand_visibility is True
    assert caps.has_counter_metrics is True  # CP7 casts counters — the differentiator.
    # XMage names a combat kill generically — downstream must not read a named source.
    assert caps.kill_attribution == 'combat_generic'
    assert caps.expected_nondecisive_rate < 0.1  # XMage plays to a decisive result.


def test_forge_dck_translates_to_xmage_txt() -> None:
    # A Forge .dck's [Main] lines are already 'N Cardname' — keep ONLY those, drop
    # the [metadata] and [Sideboard] sections (XMage DeckImporter reads plain .txt).
    dck = (
        '[metadata]\n'
        'Name=Test\n'
        'Deck Type=Constructed\n'
        '[Main]\n'
        '4 Lightning Bolt\n'
        '20 Mountain\n'
        '[Sideboard]\n'
        '2 Smash to Smithereens\n'
    )
    out = _forge_dck_to_xmage_txt(dck)
    assert out == '4 Lightning Bolt\n20 Mountain\n'
    assert 'Name=Test' not in out and 'Smash to Smithereens' not in out


def test_resolve_unavailable_raises_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # No MAKE_MAGIC_XMAGE_HOME -> a clean EngineUnavailableError (never a traceback),
    # re-raised from XMageUnavailableError so the CLI/doctor stay actionable.
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    with pytest.raises(EngineUnavailableError) as exc:
        XMageEngine().resolve(provision=False)
    assert 'MAKE_MAGIC_XMAGE_HOME' in str(exc.value)


def test_commander_is_rejected_constructed_only() -> None:
    # XMage engine is constructed-only for now — commander must fail clearly, not
    # silently mis-run. (No install needed: the format check precedes resolve use.)
    from pipeline.sim.engine import EngineInstall

    class _Stub:
        pass

    with pytest.raises(EngineUnavailableError, match='constructed only'):
        XMageEngine().run_matchup(
            ('A', ''),
            ('B', ''),
            n=1,
            seed=1,
            fmt='commander',
            install=EngineInstall(version='x', handle=_Stub()),
        )


# --------------------------------------------------------------------------- #
# Card-DB warm-up: serialize the cold H2 build so parallel game JVMs don't race it.
# --------------------------------------------------------------------------- #


def test_warm_runs_once_then_memoized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The first call warms (one scan JVM); subsequent calls for the same reactor are
    a free memo hit — the game JVMs never re-scan a cold db."""
    calls = 0

    def _fake_scan(handle: XMageInstall) -> None:
        nonlocal calls
        calls += 1

    engine = XMageEngine()
    monkeypatch.setattr(engine, '_run_warm_scan', _fake_scan)
    handle = _install(tmp_path)
    engine._ensure_card_db_warm(handle)
    engine._ensure_card_db_warm(handle)
    engine._ensure_card_db_warm(handle)
    assert calls == 1


def test_warm_is_per_reactor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Distinct reactors (distinct Mage.Tests dirs) warm independently."""
    seen: list[Path] = []
    engine = XMageEngine()
    monkeypatch.setattr(engine, '_run_warm_scan', lambda h: seen.append(h.mage_tests_dir))
    a, b = tmp_path / 'a', tmp_path / 'b'
    engine._ensure_card_db_warm(_install(a))
    engine._ensure_card_db_warm(_install(b))
    engine._ensure_card_db_warm(_install(a))  # a already warm.
    assert seen == [a, b]


def test_warm_concurrent_calls_scan_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Under the governor's parallel pool, N concurrent first-calls warm exactly ONCE
    (the lock serializes; the slow scan runs once while siblings block)."""
    import threading
    import time

    calls = 0

    def _slow_scan(handle: XMageInstall) -> None:
        nonlocal calls
        calls += 1
        time.sleep(0.05)  # hold the lock long enough that the others pile up behind it.

    engine = XMageEngine()
    monkeypatch.setattr(engine, '_run_warm_scan', _slow_scan)
    handle = _install(tmp_path)
    threads = [threading.Thread(target=engine._ensure_card_db_warm, args=(handle,)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == 1


def test_warm_failure_propagates_and_does_not_memoize(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failed warm is NOT cached — it raises to the caller (→ that matchup fails)
    and the NEXT matchup retries (a transient race self-heals)."""
    attempts = 0

    def _flaky_scan(handle: XMageInstall) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise XMageError('cold build raced')

    engine = XMageEngine()
    monkeypatch.setattr(engine, '_run_warm_scan', _flaky_scan)
    handle = _install(tmp_path)
    with pytest.raises(XMageError):
        engine._ensure_card_db_warm(handle)  # first attempt fails, not memoized.
    engine._ensure_card_db_warm(handle)  # retry succeeds and memoizes.
    assert attempts == 2


def test_run_warm_scan_builds_warm_cmd_and_raises_on_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_run_warm_scan`` launches ``XMageBatch --warm`` from Mage.Tests (via
    :func:`_launch_xmage`) and raises :class:`XMageError` on a non-zero exit (never
    proceeds into the cold-scan race)."""
    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 3

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return ('', 'boom')

    def _fake_popen(cmd: list[str], **kwargs: object) -> _FakeProc:
        captured['cmd'] = cmd
        captured['cwd'] = kwargs.get('cwd')
        return _FakeProc()

    monkeypatch.setattr(xmage_engine.subprocess, 'Popen', _fake_popen)
    with pytest.raises(XMageError, match='warm-up failed'):
        XMageEngine()._run_warm_scan(_install(tmp_path))
    assert captured['cmd'][-1] == '--warm'  # the scan-only argument.
    assert 'org.makemagic.xmage.XMageBatch' in captured['cmd']
    assert captured['cwd'] == tmp_path  # launched from the reactor's Mage.Tests dir.


def test_launch_xmage_kills_and_raises_on_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The shared launcher enforces the kill contract: a timeout kills the process
    group and raises :class:`XMageError` naming the run (one place for run + warm)."""
    killed: list[object] = []

    class _HangProc:
        returncode = None

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            raise xmage_engine.subprocess.TimeoutExpired(cmd='x', timeout=timeout or 0)

    monkeypatch.setattr(xmage_engine.subprocess, 'Popen', lambda *a, **k: _HangProc())
    monkeypatch.setattr(xmage_engine.runner, '_kill_process_group', lambda proc: killed.append(proc))
    with pytest.raises(XMageError, match='exceeded the external'):
        xmage_engine._launch_xmage(_install(tmp_path), ['--warm'], cwd=tmp_path, timeout_s=1, what='warm-up')
    assert len(killed) == 1  # the process group was reaped before raising.


def test_stage_private_db_copies_db_into_run_dir(tmp_path: Path) -> None:
    """Each run gets its OWN copy of the warm ``db/`` (no shared H2 file to race)."""
    reactor = tmp_path / 'reactor'
    (reactor / 'db').mkdir(parents=True)
    (reactor / 'db' / 'cards.h2.mv.db').write_bytes(b'CARD DB BYTES')
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    _stage_private_db(_install(reactor), run_dir)
    copied = run_dir / 'db' / 'cards.h2.mv.db'
    assert copied.is_file()
    assert copied.read_bytes() == b'CARD DB BYTES'  # a real, independent copy.


def test_max_concurrency_serializes_on_non_cow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-COW staging volume → cap 1 (serialize) so pool x 266MB copies can't
    exhaust disk (#61), and it WARNs once."""
    from pipeline.sim.engine import EngineInstall

    monkeypatch.setattr(xmage_engine, '_probe_cow', lambda src, dst: False)
    monkeypatch.setattr(xmage_engine.runner, 'staging_root', lambda: tmp_path / 'staging')
    engine = XMageEngine()
    install = EngineInstall(version='x', handle=_install(tmp_path / 'reactor'))
    with pytest.warns(UserWarning, match='SERIALIZING'):
        assert engine.max_concurrency(install) == 1
    # memoized — a second call neither re-probes nor re-warns.
    monkeypatch.setattr(xmage_engine, '_probe_cow', lambda src, dst: pytest.fail('re-probed'))
    assert engine.max_concurrency(install) == 1


def test_max_concurrency_no_cap_on_cow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A COW volume → no cap (None) so parallelism is unrestricted (clones are ~free)."""
    from pipeline.sim.engine import EngineInstall

    monkeypatch.setattr(xmage_engine, '_probe_cow', lambda src, dst: True)
    monkeypatch.setattr(xmage_engine.runner, 'staging_root', lambda: tmp_path / 'staging')
    engine = XMageEngine()
    install = EngineInstall(version='x', handle=_install(tmp_path / 'reactor'))
    assert engine.max_concurrency(install) is None


def test_probe_cow_returns_bool_and_cleans_up(tmp_path: Path) -> None:
    """The real probe returns a bool (env-dependent), never raises, and leaves no
    probe files behind."""
    src, dst = tmp_path / 'src', tmp_path / 'dst'
    assert isinstance(xmage_engine._probe_cow(src, dst), bool)
    assert not list(src.glob('.mm-cow-probe*'))
    assert not list(dst.glob('.mm-cow-probe*'))


def test_stage_private_db_missing_source_raises(tmp_path: Path) -> None:
    """A missing canonical db (warm-up never built it) fails loudly, not silently."""
    reactor = tmp_path / 'reactor'
    reactor.mkdir()  # no db/ under it.
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    with pytest.raises(XMageError, match='card DB not found'):
        _stage_private_db(_install(reactor), run_dir)


def test_stage_private_db_falls_back_to_full_copy_when_cow_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a filesystem that can't reflink, staging still produces a real private db
    (a full copy) — the fast path is an optimization, never a correctness dependency."""
    reactor = tmp_path / 'reactor'
    (reactor / 'db').mkdir(parents=True)
    (reactor / 'db' / 'cards.h2.mv.db').write_bytes(b'DB')
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    monkeypatch.setattr(xmage_engine, '_clone_tree_cow', lambda src, dst: False)  # force fallback.
    _stage_private_db(_install(reactor), run_dir)
    assert (run_dir / 'db' / 'cards.h2.mv.db').read_bytes() == b'DB'


def test_clone_tree_cow_uses_platform_reflink_and_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_clone_tree_cow`` shells the platform reflink ``cp`` and reports success/failure
    as a bool (never raises) — so the caller's fallback is always reachable."""
    seen: dict[str, object] = {}

    class _Res:
        returncode = 0

    monkeypatch.setattr(xmage_engine.sys, 'platform', 'darwin')
    monkeypatch.setattr(xmage_engine.subprocess, 'run', lambda cmd, **kw: seen.update(cmd=cmd) or _Res())
    assert _clone_tree_cow(tmp_path / 'a', tmp_path / 'b') is True
    assert seen['cmd'][:2] == ['cp', '-Rc']  # clonefile flag on macOS.

    # An OSError from cp (missing binary / odd platform) is swallowed → False (fallback).
    def _boom(cmd: list[str], **kw: object) -> object:
        raise OSError('no cp')

    monkeypatch.setattr(xmage_engine.subprocess, 'run', _boom)
    assert _clone_tree_cow(tmp_path / 'a', tmp_path / 'b') is False
