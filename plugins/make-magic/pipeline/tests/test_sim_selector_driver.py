"""Phase 4 — sim engine selector: driverless -> Forge, driven -> XMage.

Offline: the driver registry (``driver_valid``/``read_meta``/``classes_dir``) is
stubbed, so no store or JVM is touched. Asserts ``_candidate_driver`` only yields a
driver tuple for XMage + a valid-driver store deck, the governor threads it into
``run_matchup`` only when present, and the cache key folds the driver in.
"""

from __future__ import annotations

import pytest

from pipeline.sim import drivers, run
from pipeline.sim.governor import Governor, MatchSpec
from pipeline.sim.store import matchup_key


class _Engine:
    def __init__(self, name: str) -> None:
        self.name = name


class _Deck:
    uuid = 'u1'


class _Resolved:
    def __init__(self, deck) -> None:
        self.deck = deck


def _stub_valid(monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    monkeypatch.setattr(drivers, 'driver_valid', lambda deck, *, data_dir=None: ok)
    monkeypatch.setattr(drivers, 'classes_dir', lambda deck, *, data_dir=None: '/c')
    monkeypatch.setattr(
        drivers,
        'read_meta',
        lambda deck, *, data_dir=None: drivers.DriverMeta(
            deck_version='v', harness_version='h', fqcn='mm.Fake', gates_passed=True
        ),
    )


def test_forge_never_gets_a_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_valid(monkeypatch, ok=True)  # even with a valid driver...
    assert run._candidate_driver(_Engine('forge'), _Resolved(_Deck())) is None


def test_xmage_valid_driver_yields_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_valid(monkeypatch, ok=True)
    assert run._candidate_driver(_Engine('xmage'), _Resolved(_Deck())) == ('/c', 'mm.Fake')


def test_xmage_no_valid_driver_is_driverless(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_valid(monkeypatch, ok=False)
    assert run._candidate_driver(_Engine('xmage'), _Resolved(_Deck())) is None


def test_raw_dck_no_deck_is_driverless(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_valid(monkeypatch, ok=True)
    assert run._candidate_driver(_Engine('xmage'), _Resolved(None)) is None


# --------------------------------------------------------------------------- #
# Governor threads the driver into run_matchup ONLY when the spec carries one. #
# --------------------------------------------------------------------------- #


class _RecordingEngine:
    name = 'xmage'

    def __init__(self) -> None:
        self.kwargs: list[dict] = []

    def run_matchup(self, deck_a, deck_b, *, n, seed, fmt, install, **kw):
        self.kwargs.append(kw)

        class _R:
            raw_log = ''

        return _R()


def _run_one(engine, spec) -> None:
    gov = Governor(pool_size=1, max_concurrency=1, stagger_s=0.0)
    gov.run(engine, install=object(), specs=[spec])


def test_governor_passes_driver_when_present() -> None:
    eng = _RecordingEngine()
    _run_one(eng, MatchSpec(('a', 'x'), ('b', 'y'), n=1, seed=0, driver=('/c', 'mm.Fake')))
    assert eng.kwargs and eng.kwargs[0].get('driver') == ('/c', 'mm.Fake')


def test_governor_omits_driver_kwarg_when_none() -> None:
    eng = _RecordingEngine()
    _run_one(eng, MatchSpec(('a', 'x'), ('b', 'y'), n=1, seed=0, driver=None))
    assert eng.kwargs and 'driver' not in eng.kwargs[0]  # Forge signature preserved.


def test_cache_key_folds_driver() -> None:
    base = {'seed': 0, 'n_games': 1, 'fmt': 'constructed', 'engine': 'xmage', 'engine_version': 'v1'}
    driverless = matchup_key('a', 'b', **base)
    driven = matchup_key('a', 'b', **base, driver=('/c', 'mm.Fake'))
    assert driverless != driven  # a driven run never collides with the driverless row.
    assert matchup_key('a', 'b', **base, driver=None) == driverless  # None is a no-op.
