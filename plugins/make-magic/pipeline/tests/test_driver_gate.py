"""Phase 2 gate tests — compile gate + behavioral gate LOGIC (mocked JVM), plus the
guarded Mikaeus positive control against the real dist jar.

The unit tests use a FAKE goldfish engine + a monkeypatched ``javac`` — NO real JVM,
mirroring ``tests/test_xmage_driver.py``. The final test is an opt-in integration:
the ported Mikaeus ``Driver.java`` compiled against the real jar MUST pass both gates,
and a deliberately-broken driver MUST be rejected. It skips cleanly when the local
jar / JRE / javac is absent.
"""

from __future__ import annotations

import shutil
import types
from pathlib import Path

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_authoring as da
from pipeline.sim import driver_gate as dg
from pipeline.sim import drivers
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.engines.xmage import GoldfishResult
from pipeline.sim.xmage_runtime import XMageInstall

# --------------------------------------------------------------------------- #
# fixtures / fakes                                                             #
# --------------------------------------------------------------------------- #


def _deck(name: str = 'Gate Deck') -> Deck:
    return Deck(name=name, cards=[DeckCard(name='Forest', quantity=1)])


def _install(tmp_path: Path) -> types.SimpleNamespace:
    """A synthetic EngineInstall-shaped object: ``.handle`` carries a one-jar classpath
    + a ``java`` path (so ``_resolve_javac``'s sibling probe has something to look at)."""
    jar = tmp_path / 'make-magic-xmage-dist.jar'
    jar.write_bytes(b'PK jar')
    handle = XMageInstall(mage_tests_dir=tmp_path, classpath=str(jar), java=tmp_path / 'bin' / 'java')
    return types.SimpleNamespace(version='v', handle=handle)


class _FakeEngine:
    """A goldfish engine that returns canned driven/baseline results + output — the
    gate's whole JVM dependency, replaced so the LOGIC is unit-testable offline."""

    def __init__(self, *, driven: GoldfishResult, driven_output: str, baseline: GoldfishResult) -> None:
        self._driven = driven
        self._driven_output = driven_output
        self._baseline = baseline
        self.calls: list[str] = []

    def goldfish_output(self, deck_a, *, games, install, driver=None):
        assert driver is not None  # the driven run always injects the driver
        self.calls.append('driven')
        return self._driven, self._driven_output

    def goldfish(self, deck_a, *, games, install, driver=None):
        assert driver is None  # the baseline is driverless (throwaway pure CP7)
        self.calls.append('baseline')
        return self._baseline


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)  # deterministic harness_version
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. compile gate                                                             #
# --------------------------------------------------------------------------- #


def test_compile_gate_success_writes_source_and_returns_classes(
    monkeypatch: pytest.MonkeyPatch, _store: Path
) -> None:
    """A well-formed source 'compiles' (javac exit 0) → Driver.java written + classes_dir
    returned; NO meta stamped (compile does not sign off validity)."""
    deck = _deck()
    install = _install(_store)

    def _fake_run(cmd, **kwargs):
        assert cmd[0] == 'javac-stub'
        assert '-cp' in cmd and '-d' in cmd
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(dg.subprocess, 'run', _fake_run)
    src = da.render_driver(deck, da.MIKAEUS_LINE_SPEC)
    classes = dg.compile_driver(deck, src, install=install, javac='javac-stub', data_dir=_store)

    assert classes == drivers.classes_dir(deck, data_dir=_store)
    assert (drivers.driver_dir(deck, data_dir=_store) / 'Driver.java').read_text().startswith('package ')
    assert drivers.read_meta(deck, data_dir=_store) is None  # nothing stamped
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_compile_gate_failure_raises_and_stamps_nothing(
    monkeypatch: pytest.MonkeyPatch, _store: Path
) -> None:
    """A javac failure raises DriverCompileError with the compiler output and stamps no meta."""
    deck = _deck()
    install = _install(_store)

    def _fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout='', stderr='Driver.java:9: error: cannot find symbol')

    monkeypatch.setattr(dg.subprocess, 'run', _fake_run)
    with pytest.raises(dg.DriverCompileError, match='cannot find symbol'):
        dg.compile_driver(deck, 'broken source', install=install, javac='javac-stub', data_dir=_store)
    assert drivers.read_meta(deck, data_dir=_store) is None


# --------------------------------------------------------------------------- #
# 2. behavioral gate — check (a) line fired                                    #
# --------------------------------------------------------------------------- #


def _res(median: float, games: int = 5) -> GoldfishResult:
    return GoldfishResult(median_kills_own=median, games=games)


def test_gate_passes_when_line_fires_and_not_worse(_store: Path) -> None:
    """Marker present AND driven median <= baseline → pass, meta stamped gates_passed,
    and driver_valid then True."""
    deck = _deck()
    install = _install(_store)
    output = f'noise\n{da.DRIVER_LINE_FIRED_MARKER} name=PlayerA\nGOLDFISH SUMMARY (OWN TURNS) ...\n'
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(deck, ('D', 'dck'), install=install, games=5, engine=eng, data_dir=_store)

    assert result.passed is True
    assert result.line_fired is True
    assert eng.calls == ['driven', 'baseline']
    meta = drivers.read_meta(deck, data_dir=_store)
    assert meta is not None and meta.gates_passed is True
    assert meta.extra['gate_driven_median_kills_own'] == 6.0
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_gate_fails_when_marker_absent(_store: Path) -> None:
    """No DRIVER_LINE_FIRED in the driven output → fail (a) even if the median is fine;
    nothing stamped, driver_valid False."""
    deck = _deck()
    install = _install(_store)
    eng = _FakeEngine(driven=_res(6.0), driven_output='GOLDFISH SUMMARY (OWN TURNS) ...\n', baseline=_res(8.0))

    result = dg.gate_driver(deck, ('D', 'dck'), install=install, games=5, engine=eng, data_dir=_store)

    assert result.passed is False
    assert result.line_fired is False
    assert 'never emitted' in result.reason
    assert drivers.read_meta(deck, data_dir=_store) is None
    assert drivers.driver_valid(deck, data_dir=_store) is False


# --------------------------------------------------------------------------- #
# 3. behavioral gate — check (b) never worse than CP7                          #
# --------------------------------------------------------------------------- #


def test_gate_fails_when_driven_slower_than_baseline(_store: Path) -> None:
    """Marker present but driven median WORSE than baseline by MORE than the tolerance
    (default 2) → fail (b). 9.0 vs 6.0 is +3, beyond the 2-turn jitter slack."""
    deck = _deck()
    install = _install(_store)
    output = f'{da.DRIVER_LINE_FIRED_MARKER} name=PlayerA\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(6.0))

    result = dg.gate_driver(deck, ('D', 'dck'), install=install, games=5, engine=eng, data_dir=_store)

    assert result.passed is False
    assert result.line_fired is True
    assert 'WORSE' in result.reason
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_gate_tolerance_absorbs_seedless_jitter(_store: Path) -> None:
    """Within the tolerance (default 2), a driven median slightly above baseline still
    passes — the check must not coin-flip on XMage's seedless ±1-turn jitter."""
    deck = _deck()
    install = _install(_store)
    output = f'{da.DRIVER_LINE_FIRED_MARKER} name=PlayerA\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(8.0))  # +1, within tol

    result = dg.gate_driver(deck, ('D', 'dck'), install=install, games=5, engine=eng, data_dir=_store)

    assert result.passed is True
    assert result.extra['gate_tolerance'] == 2.0
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_gate_tolerance_is_configurable(_store: Path) -> None:
    """A stricter tolerance=0 restores exact never-worse: +1 now fails."""
    deck = _deck()
    install = _install(_store)
    output = f'{da.DRIVER_LINE_FIRED_MARKER} name=PlayerA\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(deck, ('D', 'dck'), install=install, games=5, tolerance=0.0, engine=eng, data_dir=_store)

    assert result.passed is False
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_gate_fails_when_driven_never_kills_vs_real_baseline(_store: Path) -> None:
    """Driven -1.0 (no-kill sentinel) vs a real baseline → the sentinel is treated as
    +inf, so the driver is strictly worse → fail (b)."""
    deck = _deck()
    install = _install(_store)
    output = f'{da.DRIVER_LINE_FIRED_MARKER} name=PlayerA\n'
    eng = _FakeEngine(driven=_res(-1.0), driven_output=output, baseline=_res(7.0))

    result = dg.gate_driver(deck, ('D', 'dck'), install=install, games=5, engine=eng, data_dir=_store)

    assert result.passed is False
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_gate_passes_on_equal_median(_store: Path) -> None:
    """Driven == baseline is 'no worse' → pass (the boundary is inclusive)."""
    deck = _deck()
    install = _install(_store)
    output = f'{da.DRIVER_LINE_FIRED_MARKER} name=PlayerA\n'
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(6.0))

    result = dg.gate_driver(deck, ('D', 'dck'), install=install, games=5, engine=eng, data_dir=_store)

    assert result.passed is True
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_kill_metric_treats_no_kill_sentinel_as_worst() -> None:
    """Unit: a negative median (the harness -1.0 'never killed') maps to +inf so it can
    never beat a real kill turn."""
    import math

    assert dg._kill_metric(-1.0) == math.inf
    assert dg._kill_metric(6.0) == 6.0


# --------------------------------------------------------------------------- #
# 4. POSITIVE CONTROL (opt-in integration): real jar, real gates               #
# --------------------------------------------------------------------------- #

_DATA_DIR = Path.home() / '.local' / 'share' / 'make-magic'
_DIST_JAR = _DATA_DIR / 'xmage' / 'make-magic-xmage-dist.jar'
_LAB_JRE = Path.home() / 'mtg-sim-lab' / 'jre' / 'jdk-21.0.12+8-jre' / 'Contents' / 'Home' / 'bin' / 'java'
_MIKAEUS_TXT = Path.home() / 'mtg-sim-lab' / 'xmage-lab' / 'mage' / 'Mage.Tests' / 'Combo_Mikaeus.txt'


def _find_javac() -> str | None:
    """A JDK javac whose default target the lab JRE 21 can load (openjdk@17), else PATH."""
    for cand in ('/opt/homebrew/opt/openjdk@17/bin/javac',):
        if Path(cand).is_file():
            return cand
    return shutil.which('javac')


def _mikaeus_dck() -> tuple[str, str]:
    """Build the goldfish (name, forge-.dck) tuple from the lab's Combo_Mikaeus deck —
    a bare ``[Main]`` section is all ``_forge_dck_to_xmage_txt`` needs."""
    lines = [ln.strip() for ln in _MIKAEUS_TXT.read_text().splitlines() if ln.strip()]
    dck = '[metadata]\nName=Combo_Mikaeus\n[Main]\n' + '\n'.join(lines) + '\n'
    return ('Combo_Mikaeus', dck)


_INTEGRATION_READY = (
    _DIST_JAR.is_file() and _LAB_JRE.is_file() and _MIKAEUS_TXT.is_file() and _find_javac() is not None
)


@pytest.mark.integration
@pytest.mark.skipif(not _INTEGRATION_READY, reason='local dist jar / lab JRE / javac / Mikaeus deck absent')
def test_mikaeus_positive_control_and_broken_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """END-TO-END: the ported Mikaeus driver compiles against the real jar and PASSES
    both gates (marker seen + never-worse); a deliberately-broken driver (line can never
    fire) is REJECTED. This is the phase's proof."""
    from pipeline.sim.engine import get_engine

    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(_DATA_DIR))
    monkeypatch.setenv('MAKE_MAGIC_JAVA', str(_LAB_JRE))
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)  # local-dev: hash the built jar
    javac = _find_javac()

    engine = get_engine('xmage')
    install = engine.resolve(provision=False, data_dir=_DATA_DIR)
    deck_ref = _mikaeus_dck()
    games = 6  # a small solo sample; the gate's tolerance absorbs XMage's seedless jitter

    # driver dirs keyed by deck.uuid → use two distinct decks so good/broken don't collide
    good_deck = Deck(name='Mikaeus Good', cards=[DeckCard(name='Forest', quantity=1)])
    bad_deck = Deck(name='Mikaeus Broken', cards=[DeckCard(name='Swamp', quantity=1)])

    # ---- positive control: MUST pass ----
    good_src = da.render_driver(good_deck, da.MIKAEUS_LINE_SPEC)
    dg.compile_driver(good_deck, good_src, install=install, javac=javac, data_dir=_DATA_DIR)
    good = dg.gate_driver(good_deck, deck_ref, install=install, games=games, engine=engine, data_dir=_DATA_DIR)

    assert good.line_fired is True, f'marker never seen: {good.reason}'
    assert good.passed is True, f'positive control failed: {good.reason}'
    assert drivers.driver_valid(good_deck, data_dir=_DATA_DIR) is True
    # spike band sanity: an own-turn kill actually happened (a real, non-sentinel median)
    assert good.driven_median is not None and good.driven_median > 0

    # ---- broken driver: line can NEVER fire (marker unreachable) → MUST be rejected ----
    broken_spec = da.LineSpec(
        name='broken-never-fires',
        imports=(),
        members=(
            '    @Override\n'
            '    public boolean priority(mage.game.Game game) {\n'
            '        if (false) { markLineFired(); }  // dead code: the line never runs\n'
            '        return super.priority(game);\n'
            '    }'
        ),
    )
    bad_src = da.render_driver(bad_deck, broken_spec)
    dg.compile_driver(bad_deck, bad_src, install=install, javac=javac, data_dir=_DATA_DIR)
    bad = dg.gate_driver(bad_deck, deck_ref, install=install, games=games, engine=engine, data_dir=_DATA_DIR)

    assert bad.line_fired is False
    assert bad.passed is False
    assert drivers.driver_valid(bad_deck, data_dir=_DATA_DIR) is False
