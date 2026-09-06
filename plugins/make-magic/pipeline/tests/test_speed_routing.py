"""Speed router — closed-form default -> DRIVEN goldfish, gated by DRIVER RICHNESS.

All offline: ``estimate_speed`` is stubbed and the goldfish engine is a fake (no JVM). Driver
richness (``drivers.driver_is_drive``) decides escalation: a drive driver runs the goldfish, a
thin driver keeps the closed form. The load-bearing invariant: the goldfish runs only with a
valid drive driver and an available install — never driverless, never with a thin driver.
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
    defaults = {'own_turn': 5.0, 'confidence': 'high', 'archetype': 'aggro', 'rationale': 'stub'}
    defaults.update(kw)
    est = SpeedEstimate(**defaults)  # type: ignore[arg-type]

    def _fn(cards, card_otag=None, *, archetype=None, combo_pieces=None, lethal=20.0):
        return est

    return _fn


def _driver(monkeypatch: pytest.MonkeyPatch, *, state: str, is_drive: bool | None,
            fqcn: str = 'makemagic.driver.Fake') -> None:
    """Stub the driver registry to a given state + richness.

    ``is_drive``: True (DRIVE), False (THIN), or None (unstamped/unknown OR absent).
    Richness reads flow through ``driver_is_drive``; the goldfish block reads ``read_meta``
    for the fqcn. ``state='absent'`` → no meta on disk.
    """
    monkeypatch.setattr(drivers, 'driver_state', lambda deck, *, data_dir=None: state)
    monkeypatch.setattr(drivers, 'driver_is_drive', lambda deck, *, data_dir=None: is_drive)
    monkeypatch.setattr(drivers, 'classes_dir', lambda deck, *, data_dir=None: '/tmp/classes')
    if state == 'absent':
        monkeypatch.setattr(drivers, 'read_meta', lambda deck, *, data_dir=None: None)
        return
    dclass = 'drive' if is_drive else ('thin' if is_drive is False else 'unknown')
    monkeypatch.setattr(
        drivers, 'read_meta',
        lambda deck, *, data_dir=None: drivers.DriverMeta(
            deck_version='v', harness_version='h', fqcn=fqcn,
            gates_passed=(state != 'broken'), driver_class=dclass,
        ),
    )


# --------------------------------------------------------------------------- #
# Closed-form default — no driver, no engine ever touched.                     #
# --------------------------------------------------------------------------- #


def test_absent_no_winline_closed_form_no_engine_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _driver(monkeypatch, state='absent', is_drive=None)
    eng = _FakeEngine()
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng, estimate=_est())
    assert ft.tier == 'tier1'
    assert ft.turn == 5.0
    assert ft.driver_recommended is False  # CP7-fine deck: closed form is the honest answer.
    assert eng.calls == []


def test_absent_with_winline_recommends_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deck with a detected win-line but no driver → closed form, but flags that a DRIVE
    driver would sharpen Speed."""
    _driver(monkeypatch, state='absent', is_drive=None)
    eng = _FakeEngine()
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng,
                          estimate=_est(archetype='combo'), combo_pieces=[[1, 1]])
    assert ft.tier == 'tier1'
    assert ft.driver_recommended is True
    assert eng.calls == []


# --------------------------------------------------------------------------- #
# DRIVE driver — goldfish runs WITH the driver tuple.                          #
# --------------------------------------------------------------------------- #


def test_drive_driver_runs_goldfish(monkeypatch: pytest.MonkeyPatch) -> None:
    _driver(monkeypatch, state='valid', is_drive=True)
    eng = _FakeEngine(median=3.0)
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng,
        deck_ref=('Fake Deck', 'dcktext'), estimate=_est(),
    )
    assert ft.tier == 'tier2'
    assert ft.turn == 3.0  # the goldfish median, not the tier-1 5.0.
    assert ft.driver_recommended is False
    assert len(eng.calls) == 1
    assert eng.calls[0]['driver'] == ('/tmp/classes', 'makemagic.driver.Fake')


# --------------------------------------------------------------------------- #
# Thin driver — deterministic closed form; the goldfish never runs.            #
# --------------------------------------------------------------------------- #


def test_thin_driver_stays_closed_form_no_goldfish(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-decider heart: a valid THIN driver keeps the closed form and must NOT run
    a goldfish (a thin driver reproduces baseline play, so it adds nothing)."""
    _driver(monkeypatch, state='valid', is_drive=False)
    eng = _FakeEngine(median=3.0)
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng,
        deck_ref=('Fake Deck', 'dcktext'), estimate=_est(),
    )
    assert ft.tier == 'tier1'
    assert ft.turn == 5.0  # the deterministic closed form, not a goldfish number.
    assert ft.driver_recommended is False  # thin is the CORRECT authored outcome; nothing to recommend.
    assert eng.calls == []  # <-- the invariant: no thin/naive goldfish for Speed.
    assert 'thin' in ft.source_rationale.lower()


# --------------------------------------------------------------------------- #
# Drive driver but no install => goldfish never runs.                          #
# --------------------------------------------------------------------------- #


def test_ac3_drive_no_install_never_runs_goldfish(monkeypatch: pytest.MonkeyPatch) -> None:
    _driver(monkeypatch, state='valid', is_drive=True)
    eng = _FakeEngine()
    ft = fundamental_turn(_Deck(), [], None, install=None, engine=eng, estimate=_est())
    assert ft.tier == 'tier1'
    assert ft.driver_recommended is True  # a DRIVE driver exists; the install is what's missing.
    assert eng.calls == []


# --------------------------------------------------------------------------- #
# Valid driver with unknown richness — closed form + recommend, no re-gate.    #
# --------------------------------------------------------------------------- #


def test_valid_unknown_richness_keeps_closed_form_and_recommends(monkeypatch: pytest.MonkeyPatch) -> None:
    """A current, valid driver whose richness can't be determined (driver_is_drive → None) keeps
    the closed form and recommends re-authoring. It must NOT goldfish (no drive evidence) and must
    NOT re-gate — a scoring call never re-gates a current driver."""
    _driver(monkeypatch, state='valid', is_drive=None)
    eng = _FakeEngine(median=3.0)
    regate = _FakeRegate(ok=True, outcome='regated')
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'),
        estimate=_est(), regate=regate,
    )
    assert ft.tier == 'tier1'
    assert ft.driver_recommended is True
    assert eng.calls == []  # no drive evidence → no goldfish.
    assert regate.calls == []  # a current driver is never re-gated during scoring.


def test_valid_drive_but_meta_unreadable_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Racy delete: state=='valid' and driver_is_drive True, but read_meta returns None between
    the state check and the goldfish (the driver dir vanished). The DRIVE block must fall back to
    the closed form rather than crash or run a driverless goldfish."""
    monkeypatch.setattr(drivers, 'driver_state', lambda deck, *, data_dir=None: 'valid')
    monkeypatch.setattr(drivers, 'driver_is_drive', lambda deck, *, data_dir=None: True)
    monkeypatch.setattr(drivers, 'classes_dir', lambda deck, *, data_dir=None: '/tmp/classes')
    monkeypatch.setattr(drivers, 'read_meta', lambda deck, *, data_dir=None: None)  # racy delete.
    eng = _FakeEngine(median=3.0)
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'), estimate=_est(),
    )
    assert ft.tier == 'tier1'
    assert ft.driver_recommended is True
    assert eng.calls == []  # no driverless goldfish on a vanished driver.


# --------------------------------------------------------------------------- #
# Control -> Speed N/A (never fabricate a turn).                               #
# --------------------------------------------------------------------------- #


def test_control_returns_na(monkeypatch: pytest.MonkeyPatch) -> None:
    _driver(monkeypatch, state='absent', is_drive=None)
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
    _driver(monkeypatch, state='valid', is_drive=True)
    eng = _FakeEngine(median=-1.0)
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng,
        deck_ref=('Fake Deck', 'dcktext'), estimate=_est(own_turn=6.0),
    )
    assert ft.tier == 'tier1'
    assert ft.turn == 6.0  # the tier-1 number, NOT -1.
    assert ft.driver_recommended is True
    assert len(eng.calls) == 1  # the goldfish DID run; its -1 was mapped honestly.


def test_returns_fundamental_turn_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _driver(monkeypatch, state='absent', is_drive=None)
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
    _driver(monkeypatch, state='valid', is_drive=True)
    eng = _FakeEngineAll(kills_median=4.0, all_median=6.0, bricks=8)
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng,
                          deck_ref=('Fake Deck', 'dcktext'), estimate=_est(own_turn=5.0))
    assert ft.tier == 'tier2'
    assert ft.turn == 6.0


def test_tier2_majority_bricks_falls_back_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """>=50% bricks -> all-games median lands past the brick cap -> no honest clock ->
    Tier-1 fallback with lowered confidence (never the flattering kills-only median)."""
    _driver(monkeypatch, state='valid', is_drive=True)
    eng = _FakeEngineAll(kills_median=4.0, all_median=26.0, bricks=11, max_turn=25)
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng,
                          deck_ref=('Fake Deck', 'dcktext'), estimate=_est(own_turn=5.0, confidence='high'))
    assert ft.tier == 'tier1'
    assert ft.turn == 5.0
    assert ft.driver_recommended is True
    assert ft.confidence != 'high'


def test_tier2_old_harness_without_all_median_keeps_kills_median(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older summary output (no medianAllOwn) -> preserve prior behavior rather than guessing."""
    _driver(monkeypatch, state='valid', is_drive=True)
    eng = _FakeEngineAll(kills_median=4.0, all_median=None, max_turn=None, bricks=None)
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng,
                          deck_ref=('Fake Deck', 'dcktext'), estimate=_est(own_turn=5.0))
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
# Stale-driver auto-re-gate flows (all mocked; no JVM/ECJ).                    #
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


def _stale(monkeypatch: pytest.MonkeyPatch, *, is_drive: bool | None = True) -> None:
    """A STALE DRIVE driver by default (post-regate it becomes a valid DRIVE driver → goldfish)."""
    monkeypatch.setattr(drivers, 'driver_state', lambda deck, *, data_dir=None: 'stale')
    monkeypatch.setattr(drivers, 'driver_is_drive', lambda deck, *, data_dir=None: is_drive)
    monkeypatch.setattr(drivers, 'classes_dir', lambda deck, *, data_dir=None: '/tmp/classes')
    dclass = 'drive' if is_drive else ('thin' if is_drive is False else 'unknown')
    monkeypatch.setattr(
        drivers, 'read_meta',
        lambda deck, *, data_dir=None: drivers.DriverMeta(
            deck_version='v', harness_version='h', fqcn='makemagic.driver.Fake',
            gates_passed=True, driver_class=dclass,
        ),
    )


def test_stale_regates_then_runs_tier2(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE + successful re-gate → the driver becomes valid and (being DRIVE) Tier-2 runs."""
    _stale(monkeypatch, is_drive=True)
    eng = _FakeEngine(median=3.0)
    regate = _FakeRegate(ok=True, outcome='regated')
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'),
        estimate=_est(), regate=regate,
    )
    assert ft.tier == 'tier2'
    assert ft.turn == 3.0
    assert len(regate.calls) == 1  # re-gate attempted exactly ONCE.
    assert len(eng.calls) == 1


def test_stale_thin_regates_then_stays_closed_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE THIN driver + successful re-gate → valid THIN → closed form, no goldfish."""
    _stale(monkeypatch, is_drive=False)
    eng = _FakeEngine(median=3.0)
    regate = _FakeRegate(ok=True, outcome='regated')
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'),
        estimate=_est(own_turn=5.0), regate=regate,
    )
    assert len(regate.calls) == 1
    assert ft.tier == 'tier1'
    assert ft.turn == 5.0
    assert eng.calls == []  # re-gated, but THIN → no goldfish.


def test_stale_failed_regate_falls_back_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE + gate REJECTS on re-gate → loud Tier-1 fallback NAMING the gate failure."""
    _stale(monkeypatch)
    eng = _FakeEngine()
    regate = _FakeRegate(ok=False, outcome='failed', reason='driven medianKillsOwn WORSE than CP7')
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'),
        estimate=_est(), regate=regate,
    )
    assert ft.tier == 'tier1'
    assert ft.driver_recommended is True
    assert 'failed re-gate' in ft.source_rationale
    assert 'WORSE than CP7' in ft.source_rationale  # the gate failure reason is surfaced.
    assert eng.calls == []  # no Tier-2 goldfish after a failed re-gate.


def test_stale_environment_failure_falls_back_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE but the toolchain can't compile here → loud Tier-1 naming the environment cause."""
    _stale(monkeypatch)
    eng = _FakeEngine()
    regate = _FakeRegate(ok=False, outcome='environment', reason='no ECJ/JRE available')
    ft = fundamental_turn(
        _Deck(), [], None, install=object(), engine=eng, deck_ref=('Fake Deck', 'dck'),
        estimate=_est(), regate=regate,
    )
    assert ft.tier == 'tier1'
    assert ft.driver_recommended is True
    assert 'could not be re-gated in this environment' in ft.source_rationale
    assert 'no ECJ/JRE' in ft.source_rationale
    assert eng.calls == []


def test_broken_driver_names_gate_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BROKEN driver (gates_passed=false) → loud Tier-1 naming the recorded gate failure."""
    monkeypatch.setattr(drivers, 'driver_state', lambda deck, *, data_dir=None: 'broken')
    monkeypatch.setattr(drivers, 'driver_is_drive', lambda deck, *, data_dir=None: None)
    monkeypatch.setattr(
        drivers, 'read_meta',
        lambda deck, *, data_dir=None: drivers.DriverMeta(
            deck_version='v', harness_version='h', fqcn='x', gates_passed=False,
            extra={'regate_failure_reason': 'macro never fired'},
        ),
    )
    eng = _FakeEngine()
    ft = fundamental_turn(_Deck(), [], None, install=object(), engine=eng, estimate=_est())
    assert ft.tier == 'tier1'
    assert ft.driver_recommended is True
    assert 'BROKEN' in ft.source_rationale
    assert 'macro never fired' in ft.source_rationale
    assert eng.calls == []


def test_stale_no_install_does_not_regate(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE with NO install must not attempt a re-gate (needs the engine) — quiet fallback."""
    _stale(monkeypatch)
    regate = _FakeRegate(ok=True, outcome='regated')
    ft = fundamental_turn(_Deck(), [], None, install=None, estimate=_est(), regate=regate)
    assert ft.tier == 'tier1'
    assert ft.driver_recommended is True
    assert regate.calls == []  # no engine → no re-gate.
