"""Phase 4 — Speed router (Tier-1 -> Tier-2-driven -> fundamental turn).

All offline: ``estimate_speed`` is stubbed and the goldfish engine is a fake (NO
JVM, no reactor). The load-bearing assertion is AC3: a ``needs_tier2`` deck with NO
valid driver must NOT call ``engine.goldfish`` (no naive XMage ever runs for Speed).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pipeline.sim import drivers, speed
from pipeline.sim.speed import FundamentalTurn, fundamental_turn
from pipeline.sim.speed_estimate import SpeedEstimate


@dataclass
class _FakeGoldfish:
    median_kills_own: float
    games: int = 20


class _FakeEngine:
    """Records every ``goldfish`` call so a test can assert it did (or did NOT) run."""

    def __init__(self, median: float = 4.0) -> None:
        self.median = median
        self.calls: list[dict] = []

    def goldfish(self, deck_ref, *, games, install, driver=None, **kw):  # noqa: ANN001
        self.calls.append({'deck_ref': deck_ref, 'games': games, 'install': install, 'driver': driver})
        return _FakeGoldfish(self.median, games)


class _Deck:
    name = 'Fake Deck'
    uuid = 'deck-uuid-1'


def _est(**kw) -> object:
    """A stubbed ``estimate_speed`` returning a fixed :class:`SpeedEstimate`."""
    defaults = dict(own_turn=5.0, confidence='high', needs_tier2=False, archetype='aggro', rationale='stub')
    defaults.update(kw)
    est = SpeedEstimate(**defaults)  # type: ignore[arg-type]

    def _fn(cards, card_otag=None, *, archetype=None, combo_pieces=None, lethal=20.0):  # noqa: ANN001
        return est

    return _fn


def _valid(monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    monkeypatch.setattr(drivers, 'driver_valid', lambda deck, *, data_dir=None: ok)
    monkeypatch.setattr(drivers, 'classes_dir', lambda deck, *, data_dir=None: '/tmp/classes')
    monkeypatch.setattr(
        drivers, 'read_meta',
        lambda deck, *, data_dir=None: drivers.DriverMeta(
            deck_version='v', harness_version='h', fqcn='makemagic.driver.Fake', gates_passed=True
        ),
    )


# --------------------------------------------------------------------------- #
# Tier-1 only — no engine ever touched.                                        #
# --------------------------------------------------------------------------- #


def test_tier1_only_no_engine_call() -> None:
    eng = _FakeEngine()
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng, estimate=_est(needs_tier2=False))
    assert ft.tier == 'tier1'
    assert ft.turn == 5.0
    assert ft.tier2_recommended is False
    assert eng.calls == []  # Tier-1 never touches the engine.


# --------------------------------------------------------------------------- #
# Tier-2 driven — goldfish runs WITH the driver tuple.                         #
# --------------------------------------------------------------------------- #


def test_tier2_driven_runs_goldfish_with_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    _valid(monkeypatch, ok=True)
    eng = _FakeEngine(median=3.0)
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng,
        deck_ref=('Fake Deck', 'dcktext'), estimate=_est(needs_tier2=True),
    )
    assert ft.tier == 'tier2'
    assert ft.turn == 3.0  # the goldfish median, not the tier-1 5.0.
    assert len(eng.calls) == 1
    assert eng.calls[0]['driver'] == ('/tmp/classes', 'makemagic.driver.Fake')


# --------------------------------------------------------------------------- #
# AC3 — needs_tier2 + NO valid driver => goldfish NEVER runs.                  #
# --------------------------------------------------------------------------- #


def test_ac3_needs_tier2_no_driver_never_runs_goldfish(monkeypatch: pytest.MonkeyPatch) -> None:
    _valid(monkeypatch, ok=False)
    eng = _FakeEngine()
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, estimate=_est(needs_tier2=True, confidence='low')
    )
    assert ft.tier == 'tier1'  # falls back to the tier-1 number.
    assert ft.turn == 5.0
    assert ft.tier2_recommended is True
    assert eng.calls == []  # <-- the invariant: naive XMage NEVER runs for Speed.


def test_ac3_no_install_never_runs_goldfish(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid driver but no XMage install still must NOT run a naive goldfish."""
    _valid(monkeypatch, ok=True)
    eng = _FakeEngine()
    ft = fundamental_turn(_Deck(), [], None, install=None, engine=eng, estimate=_est(needs_tier2=True))
    assert ft.tier == 'tier1'
    assert ft.tier2_recommended is True
    assert eng.calls == []


# --------------------------------------------------------------------------- #
# Control -> Speed N/A (never fabricate a turn).                               #
# --------------------------------------------------------------------------- #


def test_control_returns_na(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _FakeEngine()
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng,
        estimate=_est(own_turn=None, archetype='control', confidence='n/a'),
    )
    assert ft.tier == 'na'
    assert ft.turn is None
    assert eng.calls == []


# --------------------------------------------------------------------------- #
# Sentinel -1.0 from a driven goldfish -> fall back, not returned as a turn.   #
# --------------------------------------------------------------------------- #


def test_sentinel_no_kill_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _valid(monkeypatch, ok=True)
    eng = _FakeEngine(median=-1.0)
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng,
        deck_ref=('Fake Deck', 'dcktext'), estimate=_est(needs_tier2=True, own_turn=6.0),
    )
    assert ft.tier == 'tier1'
    assert ft.turn == 6.0  # the tier-1 number, NOT -1.
    assert ft.tier2_recommended is True
    assert len(eng.calls) == 1  # the goldfish DID run; its -1 was mapped honestly.


def test_returns_fundamental_turn_type() -> None:
    ft = fundamental_turn(_Deck(), [], None, estimate=_est())
    assert isinstance(ft, FundamentalTurn)
