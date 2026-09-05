"""Issue #52 — regate_driver: recompile a stale driver against the current harness + re-gate.

All offline: the recompile + behavioral-gate seams are injected, so no real JVM/ECJ runs. The
tests assert the stamp state-machine: pass → current stamp (driver_valid True); compile/gate
failure → gates_passed=false (state 'broken') with the authored source KEPT; toolchain failure →
outcome 'environment', the driver NOT condemned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_gate as dg
from pipeline.sim import drivers
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.driver_compile import CompileResult, DriverCompileError, DriverCompileToolError


def _deck(name: str = 'Regate Deck') -> Deck:
    return Deck(name=name, cards=[DeckCard(name='Forest', quantity=1)])


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)
    monkeypatch.setattr(drivers, 'version', lambda deck: 'DECKV1')
    return tmp_path


def _stale_stamp(deck: Deck, store: Path) -> None:
    """A gate-passed stamp whose harness lags current → state 'stale'."""
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version='DECKV1', harness_version='OLD-HARNESS',
            fqcn='makemagic.driver.X', gates_passed=True, gate_mode='reactive',
        ),
        data_dir=store,
    )


def _passing_gate(deck, deck_ref, *, mode, install, games, data_dir, **kw):
    """A stand-in gate that STAMPS a current, passed meta (as the real gate does on pass)."""
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version=drivers.version(deck),
            harness_version=drivers.harness_version(data_dir=data_dir),
            fqcn='makemagic.driver.X', gates_passed=True, gate_mode=mode,
        ),
        data_dir=data_dir,
    )
    return dg.GateResult(passed=True, mode=mode, reason='')


def _failing_gate(deck, deck_ref, *, mode, install, games, data_dir, **kw):
    return dg.GateResult(passed=False, mode=mode, reason='macro never fired over 20 solo games')


def test_regate_success_restamps_current(_store: Path) -> None:
    deck = _deck()
    _stale_stamp(deck, _store)
    assert drivers.driver_state(deck, data_dir=_store) == 'stale'

    compiled: list = []
    res = dg.regate_driver(
        deck, ('D', 'dck'), install=object(), games=20, data_dir=_store,
        compile_fn=lambda d, fqcn, *, data_dir: compiled.append(fqcn),
        gate_fn=_passing_gate,
    )
    assert res.ok is True and res.outcome == 'regated'
    assert compiled == ['makemagic.driver.X']  # recompiled against the current harness.
    assert drivers.driver_state(deck, data_dir=_store) == 'valid'  # revived.


def test_regate_gate_failure_marks_broken(_store: Path) -> None:
    deck = _deck()
    _stale_stamp(deck, _store)
    res = dg.regate_driver(
        deck, ('D', 'dck'), install=object(), games=20, data_dir=_store,
        compile_fn=lambda d, fqcn, *, data_dir: None,
        gate_fn=_failing_gate,
    )
    assert res.ok is False and res.outcome == 'failed'
    assert 'macro never fired' in res.reason
    assert drivers.driver_state(deck, data_dir=_store) == 'broken'  # condemned, not left stale.
    meta = drivers.read_meta(deck, data_dir=_store)
    assert meta is not None and meta.gates_passed is False
    assert 'macro never fired' in str(meta.extra.get('regate_failure_reason'))


def test_regate_compile_failure_marks_broken(_store: Path) -> None:
    deck = _deck()
    _stale_stamp(deck, _store)

    def _boom(d, fqcn, *, data_dir):
        raise DriverCompileError(
            Path('Driver.java'),
            CompileResult(ok=False, class_dir=None, diagnostics=(), raw_stderr='boom', cache_hit=False),
        )

    res = dg.regate_driver(
        deck, ('D', 'dck'), install=object(), games=20, data_dir=_store,
        compile_fn=_boom, gate_fn=_failing_gate,
    )
    assert res.ok is False and res.outcome == 'failed'
    assert 're-compile' in res.reason
    assert drivers.driver_state(deck, data_dir=_store) == 'broken'


def test_regate_toolchain_failure_is_environment_not_condemned(_store: Path) -> None:
    deck = _deck()
    _stale_stamp(deck, _store)

    def _no_ecj(d, fqcn, *, data_dir):
        raise DriverCompileToolError('no ECJ jar and cannot fetch offline')

    res = dg.regate_driver(
        deck, ('D', 'dck'), install=object(), games=20, data_dir=_store,
        compile_fn=_no_ecj, gate_fn=_failing_gate,
    )
    assert res.ok is False and res.outcome == 'environment'
    assert 'toolchain could not run' in res.reason
    # NOT condemned — still stale, so a machine that CAN compile revives it later.
    assert drivers.driver_state(deck, data_dir=_store) == 'stale'


def test_regate_absent_meta(_store: Path) -> None:
    res = dg.regate_driver(
        _deck(), ('D', 'dck'), install=object(), games=20, data_dir=_store,
        compile_fn=lambda d, fqcn, *, data_dir: None, gate_fn=_passing_gate,
    )
    assert res.ok is False and res.outcome == 'absent'
