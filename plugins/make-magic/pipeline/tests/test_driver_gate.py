"""Phase 5 gate tests — the DUAL-MODE solo own-turn-clock ship gate (mocked JVM), plus the
guarded Jeleva quad positive control against the real dist jar.

The unit tests use a FAKE goldfish engine (canned driven/baseline medians + a driven output
string carrying the standard slot-exercise markers) — NO real JVM. The gate routes by the
quad's shape (a macro ⇒ proactive, Φ-only ⇒ reactive):

  * proactive PASS = registers + ``MACRO_FIRE_REAL`` (true execution) + never-slower;
  * proactive FAIL = macro never fires, OR slower than CP7;
  * reactive PASS = registers + never-worse-solo floor (NO macro-fire requirement).

The final test is an opt-in integration: the Jeleva quad ECJ-compiled against the real jar
MUST register + fire its macro + pass; a non-firing driver MUST be rejected. It skips cleanly
when the local dist jar / JRE / ECJ are absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_authoring as da
from pipeline.sim import driver_compile as dcomp
from pipeline.sim import driver_gate as dg
from pipeline.sim import drivers
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.engines.xmage import GoldfishResult

# --------------------------------------------------------------------------- #
# fixtures / fakes                                                             #
# --------------------------------------------------------------------------- #


def _deck(name: str = 'Gate Deck') -> Deck:
    return Deck(name=name, cards=[DeckCard(name='Forest', quantity=1)])


def _proactive_spec() -> da.QuadSpec:
    """A minimal PROACTIVE quad (carries a macro ⇒ macro-fire required)."""
    return da.QuadSpec(
        name='gate-proactive',
        archetype='proactive',
        phi_body='return 0;',
        macro=da.MacroSpec(applicable_body='return true;', apply_body='return;'),
    )


def _reactive_spec() -> da.QuadSpec:
    """A minimal REACTIVE Φ-only quad (no macro ⇒ never-worse floor only)."""
    return da.QuadSpec(name='gate-reactive', archetype='reactive', phi_body='return 0;')


class _FakeEngine:
    """A goldfish engine that returns canned driven/baseline results + a driven output string
    — the gate's whole JVM dependency, replaced so the LOGIC is unit-testable offline."""

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


def _res(median: float, games: int = 5) -> GoldfishResult:
    return GoldfishResult(median_kills_own=median, games=games)


_REG = dg.DRIVER_REGISTERED_MARKER
_MACRO = da.DRIVER_MACRO_FIRED_MARKER  # reachability (apply() entered — search copy OR real)
_REAL = dg.MACRO_FIRE_REAL_MARKER  # true execution (act() committed the win on the REAL game)

_GAMES = 12  # >= dg._MIN_GATE_GAMES floor


# --------------------------------------------------------------------------- #
# 1. proactive mode — registers + macro fires + never-slower                   #
# --------------------------------------------------------------------------- #


def test_proactive_passes_when_registers_really_fires_and_not_slower(_store: Path) -> None:
    """Proactive: DRIVER_REGISTERED + MACRO_FIRE_REAL (true execution) present AND driven <=
    baseline → pass, meta stamped gates_passed + gate_mode='proactive', driver_valid True."""
    deck = _deck()
    output = (
        f'noise\n{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
        'GOLDFISH SUMMARY (OWN TURNS) ...\n'
    )
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store)

    assert result.passed is True
    assert result.mode == 'proactive'
    assert result.registered is True and result.macro_fired is True
    assert result.macro_reachable is True
    assert eng.calls == ['driven', 'baseline']
    meta = drivers.read_meta(deck, data_dir=_store)
    assert meta is not None and meta.gates_passed is True and meta.gate_mode == 'proactive'
    assert meta.extra['gate_driven_median_kills_own'] == 6.0
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_proactive_fails_when_macro_never_fires(_store: Path) -> None:
    """Proactive: registers but MACRO_FIRE_REAL absent → fail even if the median is fine;
    nothing stamped, driver_valid False."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\nGOLDFISH SUMMARY (OWN TURNS) ...\n'  # no fire markers
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store)

    assert result.passed is False
    assert result.mode == 'proactive'
    assert result.registered is True and result.macro_fired is False
    assert result.macro_reachable is False
    assert 'MACRO_FIRE_REAL' in result.reason
    assert drivers.read_meta(deck, data_dir=_store) is None
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_proactive_fails_when_reachable_but_never_really_fires(_store: Path) -> None:
    """THE NEW TEETH: registers + DRIVER_MACRO_FIRED present (the macro is REACHABLE — apply()
    entered on a throwaway search copy) but MACRO_FIRE_REAL absent (it never executed to win on
    the real game) → FAIL. Reachability alone must not pass the fire check."""
    deck = _deck()
    output = (
        f'{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_MACRO} pid=1\n'  # reachable in search, never real
        'GOLDFISH SUMMARY (OWN TURNS) ...\n'
    )
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store)

    assert result.passed is False
    assert result.macro_reachable is True  # reachability recorded…
    assert result.macro_fired is False  # …but NOT true execution
    assert 'MACRO_FIRE_REAL' in result.reason
    assert drivers.read_meta(deck, data_dir=_store) is None
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_games_floor_raises_below_minimum(_store: Path) -> None:
    """A silently-underpowered gate is worse than a hard stop: games below the floor raises
    ValueError LOUDLY (both modes); at the floor it does not."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    with pytest.raises(ValueError, match='games'):
        dg.gate_driver(
            deck, ('D', 'dck'),
            spec=_proactive_spec(), install=object(), games=8, engine=eng, data_dir=_store)
    # reactive too — jitter affects the never-worse floor as well.
    with pytest.raises(ValueError, match='games'):
        dg.gate_driver(
            deck, ('D', 'dck'),
            spec=_reactive_spec(), install=object(), games=8, engine=eng, data_dir=_store)
    # at the floor: no raise.
    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_proactive_spec(), install=object(), games=dg._MIN_GATE_GAMES, engine=eng, data_dir=_store)
    assert result.passed is True


def test_proactive_fails_when_slower_than_baseline(_store: Path) -> None:
    """Proactive: registers + fires but driven median WORSE than baseline by MORE than the
    tolerance (default 2) → fail. 9.0 vs 6.0 is +3, beyond the 2-turn jitter slack."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(6.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store)

    assert result.passed is False
    assert result.macro_fired is True
    assert 'WORSE' in result.reason
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_gate_fails_when_not_registered(_store: Path) -> None:
    """Neither mode passes without DRIVER_REGISTERED (belt-and-suspenders over the P3 fail-loud)."""
    deck = _deck()
    output = f'{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ...\n'  # no registration
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store)

    assert result.passed is False
    assert result.registered is False
    assert 'DRIVER_REGISTERED' in result.reason
    assert drivers.driver_valid(deck, data_dir=_store) is False


# --------------------------------------------------------------------------- #
# 2. reactive mode — never-worse floor, NO macro-fire requirement              #
# --------------------------------------------------------------------------- #


def test_reactive_passes_on_floor_without_macro_fire(_store: Path) -> None:
    """Reactive Φ-only: registers + meets the never-worse floor → pass WITHOUT any macro-fire
    (a passive goldfish gives a reactive deck nothing to react to). gate_mode='reactive'."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\nGOLDFISH SUMMARY (OWN TURNS) ...\n'  # no macro marker — fine
    eng = _FakeEngine(driven=_res(8.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_reactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store)

    assert result.passed is True
    assert result.mode == 'reactive'
    assert result.macro_fired is False
    meta = drivers.read_meta(deck, data_dir=_store)
    assert meta is not None and meta.gate_mode == 'reactive'
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_reactive_fails_below_the_floor(_store: Path) -> None:
    """Reactive: even with no macro requirement, a driver meaningfully slower than CP7 fails
    the never-worse floor."""
    deck = _deck()
    output = f'{_REG} p=1\n'
    eng = _FakeEngine(driven=_res(-1.0), driven_output=output, baseline=_res(7.0))  # no-kill sentinel → +inf

    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_reactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store)

    assert result.passed is False
    assert 'floor' in result.reason
    assert drivers.driver_valid(deck, data_dir=_store) is False


# --------------------------------------------------------------------------- #
# 3. tolerance behaviour                                                        #
# --------------------------------------------------------------------------- #


def test_tolerance_absorbs_seedless_jitter(_store: Path) -> None:
    """Within the tolerance (default 2), a driven median slightly above baseline still passes
    — the check must not coin-flip on XMage's seedless ±1-turn jitter."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(8.0))  # +1, within tol

    result = dg.gate_driver(
        deck, ('D', 'dck'),
        spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store)

    assert result.passed is True
    assert result.extra['gate_tolerance'] == 2.0
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_tolerance_is_configurable(_store: Path) -> None:
    """A stricter tolerance=0 restores exact never-slower: +1 now fails."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, tolerance=0.0,
        engine=eng, data_dir=_store,
    )

    assert result.passed is False
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_kill_metric_treats_no_kill_sentinel_as_worst() -> None:
    """Unit: a negative median (the harness -1.0 'never killed') maps to +inf so it can never
    beat a real kill turn."""
    import math

    assert dg._kill_metric(-1.0) == math.inf
    assert dg._kill_metric(6.0) == 6.0


# --------------------------------------------------------------------------- #
# 4. compile step — structured failure                                         #
# --------------------------------------------------------------------------- #


def test_compile_quad_driver_raises_structured_on_compile_failure(
    monkeypatch: pytest.MonkeyPatch, _store: Path
) -> None:
    """A compile failure surfaces as a structured DriverCompileError (parsed diagnostics), not
    a silent skip; the render + guardrail step still ran (source written)."""
    deck = _deck()
    spec = _proactive_spec()

    def _boom(source, fqcn, *, data_dir=None):
        result = dcomp.CompileResult(
            ok=False,
            class_dir=None,
            diagnostics=(dcomp.Diagnostic(severity='ERROR', file='Driver.java', line=9, message='cannot find symbol'),),
            raw_stderr='1. ERROR in Driver.java (at line 9)',
            cache_hit=False,
        )
        raise dcomp.DriverCompileError(Path(source), result)

    monkeypatch.setattr(dcomp, 'compile_for_injection', _boom)
    with pytest.raises(dcomp.DriverCompileError, match='cannot find symbol'):
        dg.compile_quad_driver(deck, spec, data_dir=_store)
    # the source was rendered + written before the (failing) compile.
    assert (drivers.driver_dir(deck, data_dir=_store) / 'Driver.java').read_text().startswith('package ')


def test_compile_quad_driver_rejects_guardrail_violation(_store: Path) -> None:
    """A quad that owns something the §7.1 do-not-own list forbids is rejected BEFORE compile."""
    deck = _deck()
    bad = da.QuadSpec(
        name='bad', archetype='proactive', phi_body='return 0;',
        macro=da.MacroSpec(applicable_body='return true;', apply_body='game.copy();'),
    )
    with pytest.raises(da.GuardrailViolation):
        dg.compile_quad_driver(deck, bad, data_dir=_store)


# --------------------------------------------------------------------------- #
# 5. POSITIVE CONTROL (opt-in integration): real jar, real gates               #
# --------------------------------------------------------------------------- #

_DATA_DIR = Path.home() / '.local' / 'share' / 'make-magic'
_DIST_JAR = _DATA_DIR / 'xmage' / 'make-magic-xmage-dist.jar'
_LAB_JRE = Path.home() / 'mtg-sim-lab' / 'jre' / 'jdk-21.0.12+8-jre' / 'Contents' / 'Home' / 'bin' / 'java'
_JELEVA_TXT = Path.home() / 'mtg-sim-lab' / 'reward-poc' / 'decks' / 'jeleva.txt'
_ECJ_JAR = _DATA_DIR / 'xmage' / 'tools' / f'ecj-{dcomp.ECJ_VERSION}.jar'
_INTEGRATION_READY = _DIST_JAR.is_file() and _LAB_JRE.is_file() and _JELEVA_TXT.is_file()


def _jeleva_dck() -> tuple[str, str]:
    lines = [ln.strip() for ln in _JELEVA_TXT.read_text().splitlines() if ln.strip()]
    return ('jeleva', '[metadata]\nName=jeleva\n[Main]\n' + '\n'.join(lines) + '\n')


@pytest.mark.integration
@pytest.mark.skipif(not _INTEGRATION_READY, reason='local dist jar / lab JRE / jeleva deck absent')
def test_jeleva_positive_control_and_nonfiring_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """END-TO-END: the Jeleva quad ECJ-compiles against the real jar and PASSES (registers +
    macro fires + never-slower); a non-firing driver is REJECTED."""
    from pipeline.sim.engine import get_engine

    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(_DATA_DIR))
    monkeypatch.setenv('MAKE_MAGIC_JAVA', str(_LAB_JRE))
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(_DIST_JAR))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)

    engine = get_engine('xmage')
    install = engine.resolve(provision=False, data_dir=_DATA_DIR)
    deck_ref = _jeleva_dck()
    games = 12  # >= dg._MIN_GATE_GAMES floor

    good_deck = Deck(name='Jeleva Good', cards=[DeckCard(name='Forest', quantity=1)])
    dg.compile_quad_driver(good_deck, da.JELEVA_QUAD_SPEC, data_dir=_DATA_DIR)
    good = dg.gate_driver(
        good_deck, deck_ref, spec=da.JELEVA_QUAD_SPEC, install=install, games=games, engine=engine, data_dir=_DATA_DIR
    )
    assert good.registered is True, f'never registered: {good.reason}'
    assert good.macro_fired is True, f'macro never fired: {good.reason}'
    assert good.passed is True, f'positive control failed: {good.reason}'
    assert drivers.driver_valid(good_deck, data_dir=_DATA_DIR) is True
