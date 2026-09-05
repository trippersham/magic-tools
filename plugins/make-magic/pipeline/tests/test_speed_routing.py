"""Phase 4 — Speed router (Tier-1 -> Tier-2-driven -> fundamental turn).

All offline: ``estimate_speed`` is stubbed and the goldfish engine is a fake (NO
JVM, no reactor). The load-bearing assertion is AC3: a ``needs_tier2`` deck with NO
valid driver must NOT call ``engine.goldfish`` (no naive XMage ever runs for Speed).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pipeline.sim import drivers
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

    def goldfish(self, deck_ref, *, games, install, driver=None, **kw):
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

    def _fn(cards, card_otag=None, *, archetype=None, combo_pieces=None, lethal=20.0):
        return est

    return _fn


def _valid(monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    # The router classifies via driver_state now; 'valid' drives Tier-2, 'absent' falls back
    # (the same quiet "no driver" path the old driver_valid=False produced).
    monkeypatch.setattr(drivers, 'driver_valid', lambda deck, *, data_dir=None: ok)
    monkeypatch.setattr(drivers, 'driver_state', lambda deck, *, data_dir=None: 'valid' if ok else 'absent')
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


# --- Rubric fidelity: the fundamental turn is the MEDIAN GAME, not the median kill ---
# deckcheck rubric (issue #30 spec of record): "the turn it actually COMPLETES an
# elimination ... in at least 50% of games"; "the score times the median game". A
# kills-only median flatters a deck that high-rolls (kills fast in <50% of games).

@dataclass
class _FakeGoldfishAll:
    median_kills_own: float
    games: int = 20
    max_turn: int | None = 25
    bricks: int | None = 0
    median_all_own: float | None = None


class _FakeEngineAll(_FakeEngine):
    def __init__(self, *, kills_median: float, all_median: float | None,
                 bricks: int | None = 0, max_turn: int | None = 25) -> None:
        super().__init__(median=kills_median)
        self.all_median = all_median
        self.bricks = bricks
        self.max_turn = max_turn

    def goldfish(self, deck_ref, *, games, install, driver=None, **kw):
        self.calls.append({'deck_ref': deck_ref, 'games': games, 'install': install, 'driver': driver})
        return _FakeGoldfishAll(self.median, games, self.max_turn, self.bricks, self.all_median)


def test_tier2_uses_all_games_median_not_kills_median(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills-median 4.0 but all-games median 6.0 (bricks drag it) -> the rubric's answer is 6.0."""
    _valid(monkeypatch, True)
    eng = _FakeEngineAll(kills_median=4.0, all_median=6.0, bricks=8)
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng,
                          deck_ref=('Fake Deck', 'dcktext'), estimate=_est(own_turn=5.0, needs_tier2=True))
    assert ft.tier == 'tier2'
    assert ft.turn == 6.0


def test_tier2_majority_bricks_falls_back_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """>=50% bricks -> all-games median lands past the brick cap -> no honest clock ->
    Tier-1 fallback with lowered confidence (never the flattering kills-only median)."""
    _valid(monkeypatch, True)
    eng = _FakeEngineAll(kills_median=4.0, all_median=26.0, bricks=11, max_turn=25)
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng,
                          deck_ref=('Fake Deck', 'dcktext'), estimate=_est(own_turn=5.0, needs_tier2=True, confidence='high'))
    assert ft.tier == 'tier1'
    assert ft.turn == 5.0
    assert ft.tier2_recommended is True
    assert ft.confidence != 'high'


def test_tier2_old_harness_without_all_median_keeps_kills_median(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older summary output (no medianAllOwn) -> preserve prior behavior rather than guessing."""
    _valid(monkeypatch, True)
    eng = _FakeEngineAll(kills_median=4.0, all_median=None, max_turn=None, bricks=None)
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng,
                          deck_ref=('Fake Deck', 'dcktext'), estimate=_est(own_turn=5.0, needs_tier2=True))
    assert ft.tier == 'tier2'
    assert ft.turn == 4.0


def test_parse_goldfish_summary_reads_median_all_own() -> None:
    from pipeline.sim.engines.xmage import _parse_goldfish_summary
    line = ('GOLDFISH SUMMARY (OWN TURNS) deck=x.dck variant=base games=20 maxTurn=25 '
            'skill=6 kills=12 bricks=8 medianAllOwn=7.5 medianKillsOwn=5.0 '
            'meanKillsOwn=5.2 bestOwn=4 distOwn=[...]')
    r = _parse_goldfish_summary(line)
    assert r.median_all_own == 7.5
    assert r.median_kills_own == 5.0
    assert r.bricks == 8 and r.max_turn == 25


# --------------------------------------------------------------------------- #
# Issue #52 — STALE driver auto-re-gate flows (all mocked; no JVM/ECJ).        #
# --------------------------------------------------------------------------- #


@dataclass
class _FakeRegate:
    """A stubbed regate seam recording its calls and returning a canned RegateResult-like."""

    ok: bool
    outcome: str
    reason: str = ''
    calls: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def __call__(self, deck, deck_ref, **kwargs):
        self.calls.append({'deck_ref': deck_ref, 'kwargs': kwargs})
        return self


def _stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drivers, 'driver_state', lambda deck, *, data_dir=None: 'stale')
    monkeypatch.setattr(drivers, 'classes_dir', lambda deck, *, data_dir=None: '/tmp/classes')
    monkeypatch.setattr(
        drivers, 'read_meta',
        lambda deck, *, data_dir=None: drivers.DriverMeta(
            deck_version='v', harness_version='h', fqcn='makemagic.driver.Fake', gates_passed=True
        ),
    )


def test_stale_regates_then_runs_tier2(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE + successful re-gate → the driver becomes valid and Tier-2 runs the goldfish."""
    _stale(monkeypatch)
    # after a successful re-gate the router treats state as valid and reads meta for the driver.
    eng = _FakeEngine(median=3.0)
    regate = _FakeRegate(ok=True, outcome='regated')
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'),
        estimate=_est(needs_tier2=True), regate=regate,
    )
    assert ft.tier == 'tier2'
    assert ft.turn == 3.0
    assert len(regate.calls) == 1  # re-gate attempted exactly ONCE.
    assert len(eng.calls) == 1


def test_stale_failed_regate_falls_back_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE + gate REJECTS on re-gate → loud Tier-1 fallback NAMING the gate failure."""
    _stale(monkeypatch)
    eng = _FakeEngine()
    regate = _FakeRegate(ok=False, outcome='failed', reason='driven medianKillsOwn WORSE than CP7')
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'),
        estimate=_est(needs_tier2=True), regate=regate,
    )
    assert ft.tier == 'tier1'
    assert ft.tier2_recommended is True
    assert 'FAILED re-gate' in ft.source_rationale
    assert 'WORSE than CP7' in ft.source_rationale  # names the gate failure, not the quiet text.
    assert eng.calls == []  # no Tier-2 goldfish after a failed re-gate.


def test_stale_environment_failure_falls_back_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE but the toolchain can't compile here → loud Tier-1 naming the environment cause."""
    _stale(monkeypatch)
    eng = _FakeEngine()
    regate = _FakeRegate(ok=False, outcome='environment', reason='no ECJ/JRE available')
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'),
        estimate=_est(needs_tier2=True), regate=regate,
    )
    assert ft.tier == 'tier1'
    assert ft.tier2_recommended is True
    assert 'could not run in THIS environment' in ft.source_rationale
    assert 'no ECJ/JRE' in ft.source_rationale
    assert eng.calls == []


def test_broken_driver_names_gate_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BROKEN driver (gates_passed=false) → loud Tier-1 naming the recorded gate failure."""
    monkeypatch.setattr(drivers, 'driver_state', lambda deck, *, data_dir=None: 'broken')
    monkeypatch.setattr(
        drivers, 'read_meta',
        lambda deck, *, data_dir=None: drivers.DriverMeta(
            deck_version='v', harness_version='h', fqcn='x', gates_passed=False,
            extra={'regate_failure_reason': 'macro never fired'},
        ),
    )
    eng = _FakeEngine()
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng, estimate=_est(needs_tier2=True))
    assert ft.tier == 'tier1'
    assert ft.tier2_recommended is True
    assert 'BROKEN' in ft.source_rationale
    assert 'macro never fired' in ft.source_rationale
    assert eng.calls == []


def test_stale_no_install_does_not_regate(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE with NO install must not attempt a re-gate (needs the engine) — quiet fallback."""
    _stale(monkeypatch)
    regate = _FakeRegate(ok=True, outcome='regated')
    ft = fundamental_turn(_Deck(), [], None, install=None, estimate=_est(needs_tier2=True), regate=regate)
    assert ft.tier == 'tier1'
    assert ft.tier2_recommended is True
    assert regate.calls == []  # no engine → no re-gate.
