"""Offline tests for the XMage :class:`~pipeline.sim.engines.xmage.XMageEngine`.

No JVM / no XMage reactor: exercises the deck translation, the declared
capabilities, the registry wiring, and the never-crash resolve contract. The
real headless CP7 run is validated end-to-end via the pipeline (see the task-2.3
behavioral evidence) + the telemetry drop-in in ``test_telemetry_xmage.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.sim.engine import EngineInstall, EngineUnavailableError, SimEngine, get_engine
from pipeline.sim.engines import xmage as xmage_engine
from pipeline.sim.engines.xmage import (
    _COMMANDER_TIMEOUT_S,
    _DEFAULT_TIMEOUT_S,
    XMageEngine,
    XMageError,
    _clone_tree_cow,
    _forge_dck_to_xmage_txt,
    _stage_private_db,
)
from pipeline.sim.runner import GameOutcome, MatchResult
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


def test_forge_dck_commander_zone_becomes_sideboard_line() -> None:
    # A commander .dck's [Commander] zone card must reach XMage as an `SB:` line
    # (TxtDeckImporter routes SB: → sideboard → command zone), while [Main] lines are
    # kept as-is and the ordinary [Sideboard] is dropped.
    dck = (
        '[metadata]\n'
        'Name=Kaervek\n'
        'Deck Type=Commander\n'
        '[Commander]\n'
        '1 Kaervek the Merciless\n'
        '[Main]\n'
        '47 Swamp\n'
        '[Sideboard]\n'
        '1 Dispel\n'
    )
    out = _forge_dck_to_xmage_txt(dck)
    assert 'SB: 1 Kaervek the Merciless' in out  # commander → command zone via SB:.
    assert '47 Swamp' in out  # maindeck kept as-is.
    assert 'Dispel' not in out  # the ordinary sideboard is still dropped.
    assert 'Kaervek the Merciless' in out and out.count('Kaervek') == 1  # commander appears once.


def test_forge_dck_strips_set_and_collector_suffix() -> None:
    # Forge .dck card lines can carry a `Name|SET|num` set/collector suffix (and a `+`/`*`
    # foil marker). XMage's TxtDeckImporter does NOT parse that suffix, so an un-stripped
    # `Betor, Ancestor's Voice|TDC|1` resolves to NOTHING -> the whole deck loads 0 cards
    # (the precon-opponent blocker). The translation must reduce each card line to a bare
    # `N Name` so it resolves against the base card DB.
    dck = (
        '[metadata]\n'
        'Name=Abzan Armor\n'
        '[Commander]\n'
        '1 Felothar the Steadfast|TDC|1\n'
        '[Main]\n'
        "1 Betor, Ancestor's Voice|TDC|1\n"
        '1 Vrondiss, Rage of Ancients+|AFC\n'
        '1 Kaalia of the Vast|COM\n'
    )
    out = _forge_dck_to_xmage_txt(dck)
    assert 'SB: 1 Felothar the Steadfast\n' in out
    assert "1 Betor, Ancestor's Voice\n" in out
    assert '1 Vrondiss, Rage of Ancients\n' in out  # foil `+` and single-pipe suffix stripped.
    assert '1 Kaalia of the Vast\n' in out
    # No Forge annotation leaks through.
    assert '|' not in out and '+' not in out


def test_forge_dck_constructed_unchanged_no_sideboard_prefix() -> None:
    # A constructed .dck (no [Commander] zone) is byte-identical to before — no SB:.
    dck = '[metadata]\nName=T\n[Main]\n4 Lightning Bolt\n20 Mountain\n[Sideboard]\n2 Duress\n'
    out = _forge_dck_to_xmage_txt(dck)
    assert out == '4 Lightning Bolt\n20 Mountain\n'
    assert 'SB:' not in out


def test_resolve_unavailable_raises_engine_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No MAKE_MAGIC_XMAGE_HOME -> a clean EngineUnavailableError (never a traceback),
    # re-raised from XMageUnavailableError so the CLI/doctor stay actionable.
    # Pin an EMPTY, isolated data_dir: otherwise a jar cached in the real default
    # data dir (e.g. from a prior run on this box) makes resolve() succeed and the
    # test spuriously fails — the env leak the regression sweep caught.
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    with pytest.raises(EngineUnavailableError) as exc:
        XMageEngine().resolve(provision=False, data_dir=tmp_path)
    assert 'MAKE_MAGIC_XMAGE_HOME' in str(exc.value)


def test_resolve_provision_true_calls_ensure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """resolve(provision=True) AUTO-FETCHES via xmage_runtime.ensure (2.3b); provision=False
    stays read-only (resolve)."""
    calls: list[str] = []
    monkeypatch.setattr(xmage_engine.xmage_runtime, 'ensure', lambda **kw: calls.append('ensure') or _install(tmp_path))
    monkeypatch.setattr(
        xmage_engine.xmage_runtime, 'resolve', lambda **kw: calls.append('resolve') or _install(tmp_path)
    )

    XMageEngine().resolve(provision=True)
    XMageEngine().resolve(provision=False)
    assert calls == ['ensure', 'resolve']


def _run_matchup_capturing_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, fmt: str, n: int = 1, timeout_s: int | None = None
) -> dict[str, object]:
    """Drive ``run_matchup`` with every JVM/IO seam mocked, capturing the launch args
    + timeout threaded into :func:`_launch_xmage`. Returns the captured dict."""
    seen: dict[str, object] = {}

    def _fake_launch(
        handle: object,
        args: list[str],
        *,
        cwd: object,
        timeout_s: int,
        what: str,
        driver: tuple[str, str] | None = None,
        stall_timeout_s: int | None = None,
        heartbeat: str = 'GOLDFISH GAME',
    ) -> tuple[str, int]:
        seen['args'] = args
        seen['timeout_s'] = timeout_s
        seen['driver'] = driver
        seen['stall_timeout_s'] = stall_timeout_s
        seen['heartbeat'] = heartbeat
        return ('OK', 0)

    def _fake_parse(output: str, *, deck_a: str, deck_b: str) -> MatchResult:
        return MatchResult(
            deck_a=deck_a,
            deck_b=deck_b,
            wins_a=n,
            wins_b=0,
            draws=0,
            per_game=tuple(GameOutcome(winner='a', elapsed_ms=1) for _ in range(n)),
            raw_log=output,
        )

    jar = tmp_path / 'harness.jar'
    jar.write_bytes(b'jar')
    monkeypatch.setattr(xmage_engine.xmage_runtime, '_HARNESS_JAR', jar)
    monkeypatch.setattr(xmage_engine.runner, 'staging_root', lambda: tmp_path / 'staging')
    monkeypatch.setattr(xmage_engine, '_stage_private_db', lambda handle, run_dir: None)
    monkeypatch.setattr(xmage_engine, '_launch_xmage', _fake_launch)
    monkeypatch.setattr(xmage_engine.runner, 'parse_match_log', _fake_parse)

    engine = XMageEngine()
    monkeypatch.setattr(engine, '_ensure_card_db_warm', lambda handle: None)
    install = EngineInstall(version='x', handle=_install(tmp_path))
    engine.run_matchup(
        ('A', '[Main]\n1 Sol Ring\n'),
        ('B', '[Main]\n1 Sol Ring\n'),
        n=n,
        seed=1,
        fmt=fmt,
        install=install,
        timeout_s=timeout_s,
    )
    return seen


def test_commander_is_accepted_and_passes_commander_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # XMage now runs 1v1 commander: run_matchup no longer rejects it, and the launch
    # gets a 5th 'commander' mode token (the Phase-2 harness parses it → CommanderDuel).
    seen = _run_matchup_capturing_launch(monkeypatch, tmp_path, fmt='commander')
    args = seen['args']
    assert isinstance(args, list)
    assert args[-1] == 'commander'  # the mode token appended for commander.
    assert len(args) == 5  # deckA, deckB, n, skill, mode.
    # Parity: run_matchup wires the per-game stall watchdog (2x the per-game budget) on the
    # match heartbeat — so a hung defended game is reaped, not run to the batch backstop.
    assert seen['stall_timeout_s'] == 2 * _COMMANDER_TIMEOUT_S
    assert seen['heartbeat'] == xmage_engine._MATCH_HEARTBEAT


def test_constructed_passes_no_commander_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Constructed launch is byte-identical to before: 4 args, no mode token.
    seen = _run_matchup_capturing_launch(monkeypatch, tmp_path, fmt='constructed')
    args = seen['args']
    assert isinstance(args, list)
    assert len(args) == 4  # deckA, deckB, n, skill — no 5th token.
    assert 'commander' not in args


def test_commander_none_timeout_uses_higher_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # fmt='commander' with no explicit timeout uses the longer EDH default, so the
    # external kill budget (headroom + n*timeout) is the larger commander budget.
    seen = _run_matchup_capturing_launch(monkeypatch, tmp_path, fmt='commander', n=2, timeout_s=None)
    constructed = _run_matchup_capturing_launch(monkeypatch, tmp_path, fmt='constructed', n=2, timeout_s=None)
    headroom = xmage_engine.runner._JVM_LOAD_HEADROOM_S
    cmdr_budget = seen['timeout_s']
    ctor_budget = constructed['timeout_s']
    assert isinstance(cmdr_budget, int) and isinstance(ctor_budget, int)
    assert _COMMANDER_TIMEOUT_S > _DEFAULT_TIMEOUT_S
    assert cmdr_budget == headroom + 2 * _COMMANDER_TIMEOUT_S
    assert ctor_budget == headroom + 2 * _DEFAULT_TIMEOUT_S
    assert cmdr_budget > ctor_budget  # commander gets the larger budget.


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
    assert '-Xmx3g' in captured['cmd']  # XMage's larger heap (#63), not Forge's 2g.


def test_max_concurrency_probes_the_db_subtree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The COW probe must target the ACTUAL clone source (the ``db/`` subtree
    _stage_private_db copies), not its Mage.Tests parent — else a ``db/`` on a
    different volume would be mis-probed as COW-capable."""
    from pipeline.sim.engine import EngineInstall

    captured: dict[str, Path] = {}
    monkeypatch.setattr(xmage_engine, '_probe_cow', lambda src, dst: captured.__setitem__('src', src) or True)
    monkeypatch.setattr(xmage_engine.runner, 'staging_root', lambda: tmp_path / 'staging')
    (tmp_path / 'db').mkdir()  # the db subtree exists -> probe it directly
    engine = XMageEngine()
    engine.max_concurrency(EngineInstall(version='x', handle=_install(tmp_path)))
    assert captured['src'] == tmp_path / 'db'


def test_supports_format_accepts_constructed_and_commander() -> None:
    """XMage now runs both constructed and 1v1 commander, so the CLI pre-flight guard
    admits both (no bogus commander SKIP)."""
    engine = XMageEngine()
    assert engine.supports_format('constructed') is True
    assert engine.supports_format('commander') is True


def test_launch_xmage_kills_group_on_nontimeout_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-timeout failure reading the JVM pipes (e.g. MemoryError under the RAM
    pressure this subsystem fights) must still kill the session-leader process group —
    else a 3g JVM is orphaned holding its full heap. The error then propagates."""
    killed: list[object] = []

    class _FakeProc:
        returncode = None

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            raise MemoryError('pipe read under pressure')

    monkeypatch.setattr(xmage_engine.subprocess, 'Popen', lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(xmage_engine.runner, '_kill_process_group', lambda p: killed.append(p))
    with pytest.raises(MemoryError):
        xmage_engine._launch_xmage(_install(tmp_path), ['--warm'], cwd=tmp_path, timeout_s=5, what='test')
    assert len(killed) == 1  # the group was reaped before the error propagated


def test_xmage_per_jvm_gib_exceeds_forge() -> None:
    """XMage declares a larger per-JVM RAM budget than Forge's 2 GiB so the pool isn't
    over-sized for CP7 minimax (#63)."""
    from pipeline.sim import runner

    assert XMageEngine().per_jvm_gib() == 3.5  # 3g heap + non-heap RSS headroom
    assert XMageEngine().per_jvm_gib() > 3.0  # strictly above the bare heap (avoid over-admit)
    assert XMageEngine().per_jvm_gib() > 2.0  # and above Forge's default per_jvm budget
    # the heap and the budget move in lockstep
    assert '-Xmx3g' in runner._jvm_args(heap='3g')


def test_jvm_args_heap_override() -> None:
    """runner._jvm_args(heap=...) overrides -Xmx and keeps the other headless args."""
    from pipeline.sim import runner

    assert '-Xmx2g' in runner._jvm_args()  # default
    args = runner._jvm_args(heap='3g')
    assert '-Xmx3g' in args and '-Xmx2g' not in args
    assert '-XX:+DisableExplicitGC' in args  # base arg preserved


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


def test_cow_clone_is_independent_and_falls_back(tmp_path: Path) -> None:
    """A COW clone of the ``db/`` yields an INDEPENDENT copy — writing to the clone does
    not mutate the source (proving copy-on-write, not a hardlink/shared file) — and the
    non-COW path still produces an independent full copy. The reflink is a near-free stage
    on APFS; the fallback keeps correctness on any FS.
    """
    import pipeline.sim.engines.xmage as xe

    reactor = tmp_path / 'reactor'
    (reactor / 'db').mkdir(parents=True)
    (reactor / 'db' / 'cards.h2.mv.db').write_bytes(b'ORIGINAL')

    # Real reflink path (macOS `cp -Rc` / Linux `cp -a --reflink=auto`). On a non-reflink
    # FS this still copies; either way the result must be an independent file.
    run_a = tmp_path / 'runA'
    run_a.mkdir()
    _stage_private_db(_install(reactor), run_a)
    clone = run_a / 'db' / 'cards.h2.mv.db'
    assert clone.read_bytes() == b'ORIGINAL'
    clone.write_bytes(b'MUTATED-IN-CLONE')  # a write breaks COW sharing (or hits the copy).
    assert (reactor / 'db' / 'cards.h2.mv.db').read_bytes() == b'ORIGINAL'  # source intact.

    # Forced fallback (simulate a non-COW volume): still an independent copy.
    run_b = tmp_path / 'runB'
    run_b.mkdir()
    orig = xe._clone_tree_cow
    try:
        xe._clone_tree_cow = lambda src, dst: False  # type: ignore[assignment]
        _stage_private_db(_install(reactor), run_b)
    finally:
        xe._clone_tree_cow = orig  # type: ignore[assignment]
    copied = run_b / 'db' / 'cards.h2.mv.db'
    assert copied.read_bytes() == b'ORIGINAL'


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


# --------------------------------------------------------------------------- #
# Per-game stall watchdog (the timeout-ceiling fix)                            #
# --------------------------------------------------------------------------- #


def _spawn(script: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, '-u', '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def test_watchdog_kills_a_stalled_game_fast_not_after_the_batch_budget() -> None:
    """A process that emits ONE heartbeat then hangs is killed at the per-game stall bound,
    NOT after the (much larger) batch backstop — the ceiling-bug fix."""
    import time

    from pipeline.sim.engines.xmage import _run_with_watchdog

    # Print one heartbeat, then sleep far past the stall bound with no further progress.
    proc = _spawn("import time; print('GOLDFISH GAME 1/20 killed=false'); time.sleep(60)")
    t0 = time.monotonic()
    out, rc, state = _run_with_watchdog(
        proc, heartbeat='GOLDFISH GAME', stall_timeout_s=2, backstop_s=600, poll_s=0.25
    )
    elapsed = time.monotonic() - t0
    assert state.stalled is True
    assert state.backstopped is False
    assert 'GOLDFISH GAME 1/20' in out  # output captured up to the kill
    assert elapsed < 15  # reaped in ~stall_bound, nowhere near the 600s batch backstop
    assert rc != 0  # killed


def test_watchdog_lets_a_steadily_progressing_run_finish() -> None:
    """Regular heartbeats keep resetting the clock, so a healthy multi-game run completes."""
    from pipeline.sim.engines.xmage import _run_with_watchdog

    # Five quick "games", each a heartbeat well within the stall bound, then a clean exit.
    proc = _spawn(
        "import time\n"
        "for g in range(1, 6):\n"
        "    print(f'GOLDFISH GAME {g}/5 killed=true'); time.sleep(0.3)\n"
        "print('GOLDFISH SUMMARY (OWN TURNS) medianKillsOwn=7.0')\n"
    )
    out, rc, state = _run_with_watchdog(
        proc, heartbeat='GOLDFISH GAME', stall_timeout_s=2, backstop_s=600, poll_s=0.25
    )
    assert state.stalled is False
    assert state.backstopped is False
    assert rc == 0
    assert out.count('GOLDFISH GAME') == 5
    assert 'medianKillsOwn=7.0' in out


def test_watchdog_covers_match_heartbeat_not_only_goldfish() -> None:
    """Parity: the stall watchdog reaps a hung MATCH game (keyed on the match heartbeat
    'XMAGEBATCH RESULT game=') just like a goldfish — one match heartbeat then a hang is killed
    fast, while steady match heartbeats run to completion."""
    from pipeline.sim.engines.xmage import _MATCH_HEARTBEAT, _run_with_watchdog

    hung = _spawn(f"import time; print('{_MATCH_HEARTBEAT}1/12 winner=A'); time.sleep(60)")
    _out, rc, state = _run_with_watchdog(
        hung, heartbeat=_MATCH_HEARTBEAT, stall_timeout_s=2, backstop_s=600, poll_s=0.25
    )
    assert state.stalled is True and rc != 0

    healthy = _spawn(
        "import time\n"
        f"for g in range(1, 5): print(f'{_MATCH_HEARTBEAT}{{g}}/4 winner=A'); time.sleep(0.3)\n"
    )
    out2, rc2, state2 = _run_with_watchdog(
        healthy, heartbeat=_MATCH_HEARTBEAT, stall_timeout_s=2, backstop_s=600, poll_s=0.25
    )
    assert state2.stalled is False and rc2 == 0
    assert out2.count(_MATCH_HEARTBEAT) == 4


# --------------------------------------------------------------------------- #
# Commander deck-size / integrity validation (fail LOUD at staging).            #
# --------------------------------------------------------------------------- #


def _dck(main_lines: list[str], commander: str = '1 Kenrith, the Returned King') -> str:
    """A minimal Forge .dck with the given [Main] lines and one [Commander]."""
    body = '\n'.join(main_lines)
    return f'[metadata]\nName=x\n[Commander]\n{commander}\n[Main]\n{body}\n'


def test_count_xmage_deck_sums_quantity_multipliers() -> None:
    """Card QUANTITIES are summed (the ``N`` multiplier), not lines: 30 Swamp == 30 cards."""
    from pipeline.sim.engines.xmage import count_xmage_deck

    txt = '30 Swamp\n1 Sol Ring\nSB: 1 K\'rrik, Son of Yawgmoth\n'
    assert count_xmage_deck(txt) == (31, 1)


def test_validate_commander_deck_passes_99_plus_1() -> None:
    """A legal 99 main + 1 commander deck validates and returns its counts."""
    from pipeline.sim.engines.xmage import _forge_dck_to_xmage_txt, validate_commander_deck

    dck = _dck([f'1 Card {i:02d}' for i in range(1, 99)] + ['1 Swamp'])
    txt = _forge_dck_to_xmage_txt(dck)
    assert validate_commander_deck(txt, deck_name='legal') == (99, 1)


def test_validate_commander_deck_passes_partner_98_plus_2() -> None:
    """A legal partner pair (98 main + 2 commanders = 100 total) validates."""
    from pipeline.sim.engines.xmage import _forge_dck_to_xmage_txt, validate_commander_deck

    dck = _dck(
        [f'1 Card {i:02d}' for i in range(1, 98)] + ['1 Swamp'],
        commander='1 Ardenn, Intrepid Archaeologist\n1 Rograkh, Son of Rohgahh',
    )
    txt = _forge_dck_to_xmage_txt(dck)
    assert validate_commander_deck(txt, deck_name='partners') == (98, 2)


def test_validate_commander_deck_fails_short_95_names_deck_and_counts() -> None:
    """A 95-card deck fails LOUD; the message names the deck AND the offending counts."""
    from pipeline.sim.engines.xmage import (
        DeckSizeError,
        _forge_dck_to_xmage_txt,
        validate_commander_deck,
    )

    dck = _dck([f'1 Card {i:02d}' for i in range(1, 95)] + ['1 Swamp'])  # 95 main + 1 commander
    txt = _forge_dck_to_xmage_txt(dck)
    with pytest.raises(DeckSizeError) as exc:
        validate_commander_deck(txt, deck_name='short-deck')
    msg = str(exc.value)
    assert 'short-deck' in msg and '95' in msg


def test_translation_keeps_split_and_slash_names() -> None:
    """A split-card / ``//`` name is NOT dropped by translation (counted, annotation stripped)."""
    from pipeline.sim.engines.xmage import _forge_dck_to_xmage_txt, count_xmage_deck

    dck = _dck(['1 Fire // Ice|MH2|290', '2 Wear // Tear'] + [f'1 Card {i:02d}' for i in range(1, 97)])
    txt = _forge_dck_to_xmage_txt(dck)
    assert 'Fire // Ice' in txt and 'Wear // Tear' in txt
    assert '|MH2|' not in txt  # set/collector annotation stripped
    main, commander = count_xmage_deck(txt)
    assert (main, commander) == (99, 1)


def test_audit_header_is_an_inert_comment_line() -> None:
    """The audit header is a single ``//`` comment (skipped by the importer) recording counts."""
    from pipeline.sim.engines.xmage import audit_header, count_xmage_deck

    header = audit_header(99, 1)
    assert header.startswith('// audit: main=99 commander=1')
    # Prepended to a deck it must not change the counted cards (comment is ignored).
    assert count_xmage_deck(header + '99 Swamp\nSB: 1 Yargle, Glutton of Urborg\n') == (99, 1)
