"""The STALE/ABSENT/BROKEN driver-state classifier (drivers.driver_state).

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
    # Bump the harness version under the stamp.
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


# --------------------------------------------------------------------------- #
# driver_class — the DRIVE/THIN richness stamp the Speed router reads.          #
# The single decider of whether Speed runs a driven goldfish (DRIVE) or keeps   #
# the deterministic closed form (THIN). Descriptive-only, mirrors gate_mode.    #
# --------------------------------------------------------------------------- #


def test_driver_class_round_trips_drive() -> None:
    meta = drivers.DriverMeta(
        deck_version='v', harness_version='h', fqcn='x', gates_passed=True,
        driver_class='drive',
    )
    assert drivers.DriverMeta.from_json(meta.to_json()).driver_class == 'drive'


def test_driver_class_round_trips_thin() -> None:
    meta = drivers.DriverMeta(
        deck_version='v', harness_version='h', fqcn='x', gates_passed=True,
        driver_class='thin',
    )
    assert drivers.DriverMeta.from_json(meta.to_json()).driver_class == 'thin'


def test_driver_class_legacy_meta_reads_unknown_not_a_guess() -> None:
    # A meta.json without driver_class must read back the honest sentinel — never
    # guessed as 'drive'/'thin' (labeling it a richness it was never stamped under is
    # worse than admitting we don't know). It also must NOT leak into `extra`.
    legacy = {
        'deck_version': 'v', 'harness_version': 'h', 'fqcn': 'x', 'gates_passed': True,
        'gate_mode': 'proactive',
    }
    meta = drivers.DriverMeta.from_json(legacy)
    assert meta.driver_class == drivers._LEGACY_DRIVER_CLASS
    assert 'driver_class' not in meta.extra


def test_driver_is_drive_reads_the_stamp(_store: Path) -> None:
    deck = _deck()
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version='DECKV1', harness_version=drivers.harness_version(data_dir=_store),
            fqcn='makemagic.driver.X', gates_passed=True, driver_class='drive',
        ),
        data_dir=_store,
    )
    assert drivers.driver_is_drive(deck, data_dir=_store) is True


def test_driver_is_drive_false_for_thin(_store: Path) -> None:
    deck = _deck()
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version='DECKV1', harness_version=drivers.harness_version(data_dir=_store),
            fqcn='makemagic.driver.X', gates_passed=True, driver_class='thin',
        ),
        data_dir=_store,
    )
    assert drivers.driver_is_drive(deck, data_dir=_store) is False


def test_driver_is_drive_none_when_unstamped(_store: Path) -> None:
    # Legacy stamp with no driver_class → None: the router must refuse + force re-gate,
    # never silently goldfish or silently skip.
    deck = _deck()
    _stamp(deck, _store, deck_version='DECKV1', gates_passed=True)
    assert drivers.driver_is_drive(deck, data_dir=_store) is None


def test_driver_is_drive_none_when_absent(_store: Path) -> None:
    assert drivers.driver_is_drive(_deck(), data_dir=_store) is None
