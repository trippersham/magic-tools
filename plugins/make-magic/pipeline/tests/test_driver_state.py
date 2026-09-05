"""Issue #52 — the STALE/ABSENT/BROKEN driver-state classifier (drivers.driver_state).

The truth table ``driver_valid`` collapses into a single bool. All offline: a tmp data dir + a
pinned harness_version (via XMAGE_DIST_SHA256) + a stubbed deck version — no JVM/ECJ.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import drivers
from pipeline.sim import xmage_runtime as xr


def _deck(name: str = 'State Deck') -> Deck:
    return Deck(name=name, cards=[DeckCard(name='Forest', quantity=1)])


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)  # deterministic harness_version.
    monkeypatch.setattr(drivers, 'version', lambda deck: 'DECKV1')  # deterministic deck_version.
    return tmp_path


def _stamp(deck: Deck, store: Path, *, deck_version: str, gates_passed: bool = True) -> None:
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version=deck_version,
            harness_version=drivers.harness_version(data_dir=store),
            fqcn='makemagic.driver.X',
            gates_passed=gates_passed,
        ),
        data_dir=store,
    )


def test_absent_when_no_meta(_store: Path) -> None:
    assert drivers.driver_state(_deck(), data_dir=_store) == 'absent'
    assert drivers.driver_valid(_deck(), data_dir=_store) is False


def test_valid_when_current_and_passed(_store: Path) -> None:
    deck = _deck()
    _stamp(deck, _store, deck_version='DECKV1', gates_passed=True)
    assert drivers.driver_state(deck, data_dir=_store) == 'valid'
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_stale_on_deck_version_mismatch(_store: Path) -> None:
    deck = _deck()
    _stamp(deck, _store, deck_version='OLD-DECK-VERSION', gates_passed=True)
    assert drivers.driver_state(deck, data_dir=_store) == 'stale'
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_stale_on_harness_version_mismatch(_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deck = _deck()
    _stamp(deck, _store, deck_version='DECKV1', gates_passed=True)
    # Bump the harness under the stamp (the jar-cut case).
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'feedface' * 8)
    assert drivers.driver_state(deck, data_dir=_store) == 'stale'


def test_broken_when_gates_not_passed(_store: Path) -> None:
    deck = _deck()
    _stamp(deck, _store, deck_version='DECKV1', gates_passed=False)
    assert drivers.driver_state(deck, data_dir=_store) == 'broken'


def test_broken_when_meta_corrupt(_store: Path) -> None:
    deck = _deck()
    path = drivers.meta_path(deck, data_dir=_store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{ this is not valid json', encoding='utf-8')
    assert drivers.driver_state(deck, data_dir=_store) == 'broken'
