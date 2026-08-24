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
from pipeline.sim import drivers
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.engines import xmage as xe
from pipeline.sim.xmage_runtime import XMageInstall


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
    the install's own classpath verbatim and no -Dmakemagic.driverA appears."""
    install = _install(tmp_path)
    cmd = xe._compose_launch_cmd(install, ['deckA.txt', '--solo', '1', '6'], heap='3g')
    assert '-cp' in cmd
    cp = cmd[cmd.index('-cp') + 1]
    assert cp == install.classpath
    assert not any(a.startswith('-Dmakemagic.driverA') for a in cmd)


def test_compose_driver_prepends_classes_and_threads_prop(tmp_path: Path) -> None:
    """With a driver: drivers/<id>/classes sorts BEFORE the dist jar on the classpath
    (so the injected Driver wins class-load) and -Dmakemagic.driverA=<FQCN> is passed."""
    install = _install(tmp_path)
    classes = tmp_path / 'drivers' / 'abc123' / 'classes'
    fqcn = 'makemagic.driver.Deck_abc123'
    cmd = xe._compose_launch_cmd(
        install, ['deckA.txt', '--solo', '1', '6'], heap='3g', driver=(str(classes), fqcn)
    )
    assert f'-Dmakemagic.driverA={fqcn}' in cmd
    cp = cmd[cmd.index('-cp') + 1]
    entries = cp.split(os.pathsep)
    assert entries[0] == str(classes)  # driver classes FIRST
    # the injected dir sorts strictly before the dist jar / harness jar
    harness = install.classpath.split(os.pathsep)[0]
    assert entries.index(str(classes)) < entries.index(harness)


# --------------------------------------------------------------------------- #
# 2. goldfish() parses medianKillsOwn                                          #
# --------------------------------------------------------------------------- #

_SUMMARY = (
    'XMAGEBATCH SOLO card db ready; deckA=deckA.txt games=5 skill=6 driverA=cp7\n'
    'GOLDFISH GAME 1/5 deck=deckA.txt variant=cp7 killed=true ownKillTurn=6 globalTurn=11 lifeB=-2 ms=900\n'
    'GOLDFISH SUMMARY (OWN TURNS) deck=deckA.txt variant=cp7 games=5 maxTurn=20 skill=6 '
    'kills=4 bricks=1 medianAllOwn=6.0 medianKillsOwn=6.0 meanKillsOwn=6.25 bestOwn=5 distOwn=[6, 6, 6, 6, 21]\n'
)


def test_parse_goldfish_summary_extracts_median_and_games() -> None:
    result = xe._parse_goldfish_summary(_SUMMARY)
    assert result.median_kills_own == 6.0
    assert result.games == 5


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
