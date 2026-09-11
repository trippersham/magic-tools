"""Phase 1 tests — per-deck driver classpath injection + solo goldfish parsing +
the driver storage/registry (staleness).

All offline: no reactor, no network, NO real java launch. The engine's launch
command is captured via the pure :func:`_compose_launch_cmd` builder (never
spawned); the goldfish parser is fed synthetic ``GOLDFISH SUMMARY`` stdout; the
registry is exercised against a ``MAKE_MAGIC_DATA_DIR`` tmp store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.decks.version import version
from pipeline.sim import driver_compile as dc
from pipeline.sim import drivers
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.engines import xmage as xe
from pipeline.sim.xmage_runtime import XMageInstall, XMageUnavailableError


@pytest.fixture(autouse=True)
def _pass_jre_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests never launch a real JVM, so default the driven-launch JRE version gate's probe
    to a valid Java 21 banner. A test wanting the gate to FIRE re-patches the probe locally."""
    monkeypatch.setattr(
        'pipeline.sim.simd.preflight.default_java_probe',
        lambda _p: 'openjdk version "21.0.12" 2026-08-18',
    )


def _install(tmp_path: Path) -> XMageInstall:
    """A synthetic resolved install with a two-entry classpath (harness jar FIRST,
    then a dist dep) — enough to assert driver-classes prepend ordering."""
    harness = tmp_path / 'make-magic-xmage.jar'
    dep = tmp_path / 'dep.jar'
    harness.write_bytes(b'PK harness')
    dep.write_bytes(b'PK dep')
    classpath = os.pathsep.join((str(harness), str(dep)))
    return XMageInstall(mage_tests_dir=tmp_path, classpath=classpath, java=Path('java'))


def _deck() -> Deck:
    return Deck(name='Test Deck', cards=[DeckCard(name='Forest', quantity=1)])


# --------------------------------------------------------------------------- #
# 1. Engine composes the exact classpath + -D                                 #
# --------------------------------------------------------------------------- #


def test_compose_driverless_argv_unchanged(tmp_path: Path) -> None:
    """With NO driver the composed argv is the prior driverless shape: the classpath is
    the install's own classpath verbatim and no -Dmakemagic.driver appears."""
    install = _install(tmp_path)
    cmd = xe._compose_launch_cmd(install, ['deckA.txt', '--solo', '1', '6'], heap='3g')
    assert '-cp' in cmd
    cp = cmd[cmd.index('-cp') + 1]
    assert cp == install.classpath
    assert not any(a.startswith('-Dmakemagic.driver') for a in cmd)


def test_compose_driver_prepends_classes_and_threads_prop(tmp_path: Path) -> None:
    """With a driver: drivers/<id>/classes sorts BEFORE the dist jar on the classpath
    (so the injected Driver wins class-load) and -Dmakemagic.driver=<FQCN> is passed
    (the register-by-playerId seam ``XMageBatch`` reads to invoke ``register(UUID)``)."""
    install = _install(tmp_path)
    classes = tmp_path / 'drivers' / 'abc123' / 'classes'
    fqcn = 'makemagic.driver.Deck_abc123'
    cmd = xe._compose_launch_cmd(install, ['deckA.txt', '--solo', '1', '6'], heap='3g', driver=(str(classes), fqcn))
    assert f'-Dmakemagic.driver={fqcn}' in cmd
    cp = cmd[cmd.index('-cp') + 1]
    entries = cp.split(os.pathsep)
    assert entries[0] == str(classes)  # driver classes FIRST
    # the injected dir sorts strictly before the dist jar / harness jar
    harness = install.classpath.split(os.pathsep)[0]
    assert entries.index(str(classes)) < entries.index(harness)


# --------------------------------------------------------------------------- #
# 1a. fail-loud: a REQUESTED driver that never registered must not score as CP7 #
# --------------------------------------------------------------------------- #


class _FakePopen:
    """Stand-in for subprocess.Popen: returns canned (stdout, stderr) + returncode."""

    def __init__(self, stdout: str, stderr: str = '', returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self._stdout, self._stderr


def _patch_launch(monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 0) -> None:
    """Route _launch_xmage's Popen + runner hooks to a no-JVM fake emitting ``stdout``."""
    monkeypatch.setattr(xe.subprocess, 'Popen', lambda *a, **k: _FakePopen(stdout, '', returncode))
    monkeypatch.setattr(xe.runner, '_register_active', lambda proc: None)
    monkeypatch.setattr(xe.runner, '_unregister_active', lambda proc: None)


def test_launch_driver_requested_without_registered_line_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run that REQUESTED a driver (driver=... → -Dmakemagic.driver present) but whose
    captured stdout carries NO ``DRIVER_REGISTERED`` line must RAISE — a shadowed/stale
    Driver silently degrading to bare CP7 (exit 0) must never be scored as a pass."""
    install = _install(tmp_path)
    _patch_launch(monkeypatch, 'XMAGEBATCH SOLO card db ready\nGOLDFISH SUMMARY ...\n')
    with pytest.raises(xe.XMageError, match='DRIVER_REGISTERED'):
        xe._launch_xmage(
            install,
            ['deckA.txt', '--solo', '1', '6'],
            cwd=tmp_path,
            timeout_s=5,
            what='goldfish',
            driver=(str(tmp_path / 'classes'), 'makemagic.driver.X'),
        )


def test_launch_driver_requested_with_registered_line_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A driver-requested run WITH a ``DRIVER_REGISTERED`` line does NOT raise."""
    install = _install(tmp_path)
    _patch_launch(
        monkeypatch,
        'DRIVER_REGISTERED fqcn=makemagic.driver.X playerId=abc\nGOLDFISH SUMMARY ...\n',
    )
    output, rc = xe._launch_xmage(
        install,
        ['deckA.txt', '--solo', '1', '6'],
        cwd=tmp_path,
        timeout_s=5,
        what='goldfish',
        driver=(str(tmp_path / 'classes'), 'makemagic.driver.X'),
    )
    assert rc == 0
    assert 'DRIVER_REGISTERED' in output


def test_launch_driven_run_gates_old_jre(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A DRIVEN launch under a Java <21 runtime FAILS FAST with an actionable version error
    (before spawning) — per-deck drivers are Java-21 bytecode, so an older JRE would otherwise die
    with an opaque UnsupportedClassVersionError surfacing only as 'no DRIVER_REGISTERED'."""
    monkeypatch.setattr(
        'pipeline.sim.simd.preflight.default_java_probe',
        lambda _p: 'openjdk version "17.0.9" 2026-01-01',
    )
    install = _install(tmp_path)
    with pytest.raises(XMageUnavailableError, match='Java 21'):
        xe._launch_xmage(
            install,
            ['deckA.txt', '--solo', '1', '6'],
            cwd=tmp_path,
            timeout_s=5,
            what='goldfish',
            driver=(str(tmp_path / 'classes'), 'makemagic.driver.X'),
        )


def test_launch_no_driver_does_not_gate_jre(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A DRIVERLESS run under an old JRE is NOT gated — it needs only the harness/dist target."""
    monkeypatch.setattr(
        'pipeline.sim.simd.preflight.default_java_probe',
        lambda _p: 'openjdk version "17.0.9" 2026-01-01',
    )
    install = _install(tmp_path)
    _patch_launch(monkeypatch, 'GOLDFISH SUMMARY ... (no driver)\n')
    _, rc = xe._launch_xmage(install, ['deckA.txt', '--solo', '1', '6'], cwd=tmp_path, timeout_s=5, what='goldfish')
    assert rc == 0


def test_launch_no_driver_never_raises_on_missing_registered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A run with NO driver requested never checks for DRIVER_REGISTERED (bare CP7 is a
    legitimate driverless run)."""
    install = _install(tmp_path)
    _patch_launch(monkeypatch, 'GOLDFISH SUMMARY ... (no driver)\n')
    output, rc = xe._launch_xmage(
        install, ['deckA.txt', '--solo', '1', '6'], cwd=tmp_path, timeout_s=5, what='goldfish'
    )
    assert rc == 0
    assert 'DRIVER_REGISTERED' not in output


# --------------------------------------------------------------------------- #
# 1b. author -> ECJ compile -> inject wiring (Phase 3.3)                       #
# --------------------------------------------------------------------------- #


def test_compile_for_injection_returns_classdir_and_fqcn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """compile_for_injection compiles the Driver .java (ECJ, mocked here) and returns the
    exact ``(classes_dir, fqcn)`` tuple the engine's ``driver=`` parameter takes."""
    class_dir = tmp_path / 'out'
    class_dir.mkdir()
    monkeypatch.setattr(
        dc,
        'compile_driver',
        lambda source, *, fqcn=None, data_dir=None: dc.CompileResult(
            ok=True, class_dir=class_dir, diagnostics=(), raw_stderr='', cache_hit=False
        ),
    )
    src = tmp_path / 'JelevaThoracleReferenceDriver.java'
    src.write_text('// stub', encoding='utf-8')
    fqcn = 'makemagic.driver.reference.JelevaThoracleReferenceDriver'
    injection = dc.compile_for_injection(src, fqcn)
    assert injection == (str(class_dir), fqcn)


def test_compile_for_injection_raises_on_compile_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A compile FAILURE is fail-loud (DriverCompileError carrying the diagnostics) — never a
    tuple pointing at an empty/partial class dir that would silently run as pure CP7."""
    diag = dc.Diagnostic(severity='ERROR', file='Driver.java', line=3, message='cannot find symbol')
    monkeypatch.setattr(
        dc,
        'compile_driver',
        lambda source, *, fqcn=None, data_dir=None: dc.CompileResult(
            ok=False, class_dir=None, diagnostics=(diag,), raw_stderr='boom', cache_hit=False
        ),
    )
    src = tmp_path / 'Driver.java'
    src.write_text('// stub', encoding='utf-8')
    with pytest.raises(dc.DriverCompileError) as excinfo:
        dc.compile_for_injection(src, 'makemagic.driver.Driver')
    assert 'cannot find symbol' in str(excinfo.value)


def test_injection_tuple_threads_into_launch_cmd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End-to-end wiring: the tuple from compile_for_injection composes into the launch argv
    with the compiled dir FIRST on the classpath and -Dmakemagic.driver=<fqcn> set."""
    class_dir = tmp_path / 'drivers' / 'classes'
    class_dir.mkdir(parents=True)
    fqcn = 'makemagic.driver.reference.JelevaThoracleReferenceDriver'
    monkeypatch.setattr(
        dc,
        'compile_driver',
        lambda source, *, fqcn=None, data_dir=None: dc.CompileResult(
            ok=True, class_dir=class_dir, diagnostics=(), raw_stderr='', cache_hit=False
        ),
    )
    src = tmp_path / 'JelevaThoracleReferenceDriver.java'
    src.write_text('// stub', encoding='utf-8')
    injection = dc.compile_for_injection(src, fqcn)

    install = _install(tmp_path)
    cmd = xe._compose_launch_cmd(install, ['deckA.txt', '--solo', '1', '6'], heap='3g', driver=injection)
    assert f'-Dmakemagic.driver={fqcn}' in cmd
    cp = cmd[cmd.index('-cp') + 1]
    assert cp.split(os.pathsep)[0] == str(class_dir)


# --------------------------------------------------------------------------- #
# 2. goldfish() parses medianKillsOwn                                          #
# --------------------------------------------------------------------------- #

_SUMMARY = (
    'XMAGEBATCH SOLO card db ready; deckA=deckA.txt games=5 skill=6 driver=cp7\n'
    'GOLDFISH GAME 1/5 deck=deckA.txt variant=cp7 killed=true ownKillTurn=6 globalTurn=11 lifeB=-2 ms=900\n'
    'GOLDFISH SUMMARY (OWN TURNS) deck=deckA.txt variant=cp7 games=5 maxTurn=20 skill=6 '
    'kills=4 bricks=1 medianAllOwn=6.0 medianKillsOwn=6.0 meanKillsOwn=6.25 bestOwn=5 distOwn=[6, 6, 6, 6, 21]\n'
)


def test_parse_goldfish_summary_extracts_median_and_games() -> None:
    result = xe._parse_goldfish_summary(_SUMMARY)
    assert result.median_kills_own == 6.0
    assert result.games == 5
    # maxTurn / bricks feed the gate's brick-cap validity guard (deckout/freeze-at-cap).
    assert result.max_turn == 20
    assert result.bricks == 1


def test_parse_goldfish_summary_max_turn_absent_is_none() -> None:
    """An older summary line without maxTurn parses cleanly with max_turn=None (the brick-cap
    guard then no-ops rather than guessing a cap)."""
    out = 'GOLDFISH SUMMARY (OWN TURNS) deck=d.txt games=5 skill=6 medianKillsOwn=6.0 distOwn=[6]\n'
    result = xe._parse_goldfish_summary(out)
    assert result.max_turn is None and result.bricks is None


def test_parse_goldfish_summary_missing_line_raises() -> None:
    """No GOLDFISH SUMMARY line (crash / deck-load failure) must RAISE — never a silent 0."""
    with pytest.raises(xe.XMageError):
        xe._parse_goldfish_summary('XMAGEBATCH SOLO card db ready\nsome noise\n')


def test_parse_goldfish_summary_no_kills_sentinel() -> None:
    """A run where nothing killed emits medianKillsOwn=-1.0 (the Java sentinel); the
    parser returns it faithfully (a negative median, never coerced to 0)."""
    out = (
        'GOLDFISH SUMMARY (OWN TURNS) deck=d.txt variant=cp7 games=3 maxTurn=20 skill=6 '
        'kills=0 bricks=3 medianAllOwn=21.0 medianKillsOwn=-1 meanKillsOwn=-1.00 bestOwn=-1 distOwn=[21, 21, 21]\n'
    )
    result = xe._parse_goldfish_summary(out)
    assert result.median_kills_own == -1.0
    assert result.games == 3


# --------------------------------------------------------------------------- #
# 3. driver_valid staleness                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    # Deterministic harness_version without a real jar: pin a fake dist SHA.
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)
    return tmp_path


def _place_driver(deck: Deck, data_dir: Path, **overrides: object) -> None:
    meta = {
        'deck_version': version(deck),
        'harness_version': drivers.harness_version(data_dir=data_dir),
        'fqcn': f'makemagic.driver.Deck_{deck.uuid}',
        'gates_passed': True,
    }
    meta.update(overrides)
    classes = data_dir / 'drivers' / deck.uuid / 'classes'
    classes.mkdir(parents=True, exist_ok=True)
    (data_dir / 'drivers' / deck.uuid / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')


def test_driver_valid_happy_path(_store: Path) -> None:
    deck = _deck()
    _place_driver(deck, _store)
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_driver_valid_missing_dir(_store: Path) -> None:
    """No driver dir/meta at all → invalid (never a crash)."""
    assert drivers.driver_valid(_deck(), data_dir=_store) is False


def test_driver_valid_stale_deck_version(_store: Path) -> None:
    deck = _deck()
    _place_driver(deck, _store, deck_version='stale' * 12)
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_driver_valid_stale_harness_version(_store: Path) -> None:
    deck = _deck()
    _place_driver(deck, _store, harness_version='different-harness')
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_driver_valid_gates_not_passed(_store: Path) -> None:
    deck = _deck()
    _place_driver(deck, _store, gates_passed=False)
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_driver_valid_corrupt_meta(_store: Path) -> None:
    """A corrupt/half-written meta.json → invalid, not a JSON crash."""
    deck = _deck()
    classes = _store / 'drivers' / deck.uuid / 'classes'
    classes.mkdir(parents=True)
    (_store / 'drivers' / deck.uuid / 'meta.json').write_text('{ not json', encoding='utf-8')
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_registry_roundtrip_and_extras_preserved(_store: Path) -> None:
    """write_meta/read_meta round-trips the core fields AND preserves Phase-2 extra
    stamps (so a future gate can add fields without this module dropping them)."""
    deck = _deck()
    meta = drivers.DriverMeta(
        deck_version=version(deck),
        harness_version=drivers.harness_version(data_dir=_store),
        fqcn='makemagic.driver.X',
        gates_passed=True,
        extra={'goldfish_median': 6.0},
    )
    drivers.write_meta(deck, meta, data_dir=_store)
    got = drivers.read_meta(deck, data_dir=_store)
    assert got is not None
    assert got.fqcn == 'makemagic.driver.X'
    assert got.gates_passed is True
    assert got.extra['goldfish_median'] == 6.0
    # the classes dir is resolvable off the same identity
    assert drivers.classes_dir(deck, data_dir=_store) == _store / 'drivers' / deck.uuid / 'classes'


def test_legacy_meta_without_gate_mode_reads_as_unknown() -> None:
    """A meta JSON predating the Phase-5 gate_mode field reads back as the 'unknown' sentinel
    (NOT an arbitrary 'proactive' guess): the field is descriptive-only and driver_valid does
    not branch on it, so a legacy meta must not be mislabeled."""
    legacy = {
        'deck_version': 'v1',
        'harness_version': 'h1',
        'fqcn': 'makemagic.driver.X',
        'gates_passed': True,
    }
    meta = drivers.DriverMeta.from_json(legacy)
    assert meta.gate_mode == 'unknown'


def test_harness_version_uses_pinned_sha(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When XMAGE_DIST_SHA256 is pinned, harness_version returns it directly (the ABI
    identity a compiled driver is bound to) — no jar hashing needed."""
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'a' * 64)
    assert drivers.harness_version(data_dir=tmp_path) == 'a' * 64


def test_harness_version_hashes_jar_when_unpinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Local-dev (pin=None): fall back to hashing the resolved jar so staleness still
    tracks the actual built jar bytes."""
    import hashlib

    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    body = b'PK\x03\x04 built jar bytes'
    xmage = tmp_path / 'xmage'
    xmage.mkdir()
    (xmage / xr._DIST_JAR_NAME).write_bytes(body)
    got = drivers.harness_version(data_dir=tmp_path)
    assert got == hashlib.sha256(body).hexdigest()
