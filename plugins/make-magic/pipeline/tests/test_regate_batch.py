"""Issue #52 — the batch re-gate CLI (regate_batch): walk the ledger, report, exit-on-fail.

All offline: a synthetic ledger + injected regate/deck_ref seams — no JVM/ECJ. Asserts the
dry-run listing mode (states only, nothing re-gated) and the nonzero exit when any driver FAILED.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.sim import driver_batch, drivers, regate_batch
from pipeline.sim import xmage_runtime as xr


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)
    monkeypatch.setattr(drivers, 'version', lambda deck: 'DECKV1')
    return tmp_path


def _ledger_with_stale_driver(store: Path) -> Path:
    """A ledger holding one compiled DRIVE row whose driver is stamped STALE on disk."""
    ledger_path = store / 'ledger.jsonl'
    ledger = driver_batch.Ledger(ledger_path)
    ledger.record(
        {
            'deck_id': 'gauntlet/x.dck',
            'name': 'X',
            'drive': True,
            'stage': 'compiled',
            'fqcn': 'makemagic.driver.X',
        }
    )
    # THIN rows carry no driver — must be skipped from the driver walk.
    ledger.record({'deck_id': 'gauntlet/thin.dck', 'name': 'Thin', 'drive': False, 'stage': 'gated'})

    deck = driver_batch._deck_for_row(ledger.row('gauntlet/x.dck'))
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version='DECKV1',
            harness_version='OLD-HARNESS',
            fqcn='makemagic.driver.X',
            gates_passed=True,
            gate_mode='reactive',
        ),
        data_dir=store,
    )
    return ledger_path


class _RG:
    """A stand-in for driver_gate.RegateResult (duck-typed: ok/outcome/reason)."""

    def __init__(self, ok: bool, outcome: str, reason: str = '') -> None:
        self.ok = ok
        self.outcome = outcome
        self.reason = reason


def _fake_regate(ok: bool, outcome: str, reason: str = ''):
    def _fn(deck, deck_ref, **kwargs):
        return _RG(ok, outcome, reason)

    return _fn


def test_dry_run_lists_states_and_regates_nothing(_store: Path) -> None:
    ledger_path = _ledger_with_stale_driver(_store)
    called: list = []

    reports = regate_batch.run_regate_batch(
        ledger_path=ledger_path,
        data_dir=_store,
        dry_run=True,
        regate_fn=lambda *a, **k: called.append(1),
        deck_ref_fn=lambda deck, row: ('X', 'dck'),
    )
    assert called == []  # dry-run re-gates nothing.
    assert len(reports) == 1  # only the DRIVE-compiled row; THIN skipped.
    assert reports[0].state == 'stale' and reports[0].outcome == 'stale'


def test_live_run_regates_stale(_store: Path) -> None:
    ledger_path = _ledger_with_stale_driver(_store)
    reports = regate_batch.run_regate_batch(
        ledger_path=ledger_path,
        data_dir=_store,
        dry_run=False,
        install=object(),
        regate_fn=_fake_regate(ok=True, outcome='regated'),
        deck_ref_fn=lambda deck, row: ('X', 'dck'),
    )
    assert reports[0].outcome == 'regated'


def test_failed_regate_reported(_store: Path) -> None:
    ledger_path = _ledger_with_stale_driver(_store)
    reports = regate_batch.run_regate_batch(
        ledger_path=ledger_path,
        data_dir=_store,
        dry_run=False,
        install=object(),
        regate_fn=_fake_regate(ok=False, outcome='failed', reason='macro never fired'),
        deck_ref_fn=lambda deck, row: ('X', 'dck'),
    )
    assert reports[0].outcome == 'failed'
    assert 'macro never fired' in reports[0].reason


def test_cli_exits_nonzero_on_failure(_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_path = _ledger_with_stale_driver(_store)
    # Dry-run path avoids resolving a real install; inject a failed report via the batch fn.
    monkeypatch.setattr(
        regate_batch,
        'run_regate_batch',
        lambda **kw: [
            regate_batch.RegateReport(
                deck_id='gauntlet/x.dck', name='X', state='stale', outcome='failed', reason='boom'
            )
        ],
    )
    code = regate_batch.run(['--ledger', str(ledger_path), '--dry-run'])
    assert code == 1  # any failed driver → nonzero exit.


def test_cli_exit_zero_when_all_clean(_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_path = _ledger_with_stale_driver(_store)
    monkeypatch.setattr(
        regate_batch,
        'run_regate_batch',
        lambda **kw: [regate_batch.RegateReport(deck_id='gauntlet/x.dck', name='X', state='valid', outcome='valid')],
    )
    code = regate_batch.run(['--ledger', str(ledger_path), '--dry-run'])
    assert code == 0


def test_cli_without_live_is_dry_run(_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The incident-class guard: WITHOUT --live the CLI is dry-run — dry_run=True is threaded to the
    batch fn and no XMage install is ever resolved (so a missing install cannot even be reached)."""
    ledger_path = _ledger_with_stale_driver(_store)
    seen: dict[str, object] = {}

    def _spy(**kw: object) -> list[regate_batch.RegateReport]:
        seen.update(kw)
        return [regate_batch.RegateReport(deck_id='gauntlet/x.dck', name='X', state='stale', outcome='stale')]

    monkeypatch.setattr(regate_batch, 'run_regate_batch', _spy)
    # Deliberately NO --live and NO --dry-run: default must still be dry-run.
    code = regate_batch.run(['--ledger', str(ledger_path)])
    assert code == 0
    assert seen['dry_run'] is True
    assert seen['install'] is None  # never resolved an install on the dry path.


def test_cli_live_resolves_install_and_writes(_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WITH --live the CLI threads dry_run=False (mutation permitted). We stub install resolution so
    no real JVM is needed and assert the live flag reaches the batch fn."""
    ledger_path = _ledger_with_stale_driver(_store)
    seen: dict[str, object] = {}

    monkeypatch.setattr(xr, 'resolve', lambda: object())

    def _spy(**kw: object) -> list[regate_batch.RegateReport]:
        seen.update(kw)
        return [regate_batch.RegateReport(deck_id='gauntlet/x.dck', name='X', state='stale', outcome='regated')]

    monkeypatch.setattr(regate_batch, 'run_regate_batch', _spy)
    code = regate_batch.run(['--ledger', str(ledger_path), '--live'])
    assert code == 0
    assert seen['dry_run'] is False
    assert seen['install'] is not None
