"""Phase 4 tests — the benchmark-relative driver-vs-CP7 comparison core + CLI (mocked JVM).

The unit tests use JVM-free fakes: a fake engine (canned ``goldfish`` + ``run_matchup``),
the deterministic sequential runner, and — for the AC9-integrity test — a capturing
``run_matchups`` seam that records the exact specs scheduled. The delta + Wilson-CI math
is asserted against hand values; the anti-confound test asserts BOTH pilotings run
same-engine / same-seed / same-opponents differing ONLY in ``driver``, and that no call
ever pits the candidate against a copy of itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_compare as dc
from pipeline.sim import drivers
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.core import wilson_ci
from pipeline.sim.engines.xmage import GoldfishResult
from pipeline.sim.gauntlet import GauntletDeck
from pipeline.sim.governor import MatchSpec, PoolResult
from pipeline.sim.runner import MatchResult

# --------------------------------------------------------------------------- #
# fixtures / fakes                                                             #
# --------------------------------------------------------------------------- #


def _deck(name: str = 'Compare Deck') -> Deck:
    return Deck(name=name, cards=[DeckCard(name='Forest', quantity=1)])


_DECK_REF = ('Compare Deck', '[metadata]\nName=Compare Deck\n[Main]\n60 Forest\n')
_OPP_A = GauntletDeck(name='OppA', dck_text='[metadata]\nName=OppA\n[Main]\n60 Swamp\n')
_OPP_B = GauntletDeck(name='OppB', dck_text='[metadata]\nName=OppB\n[Main]\n60 Island\n')


def _mr(wins_a: int, wins_b: int, draws: int = 0, raw_log: str = '') -> MatchResult:
    return MatchResult(
        deck_a='D', deck_b='Opp', wins_a=wins_a, wins_b=wins_b, draws=draws, per_game=(), raw_log=raw_log
    )


class _FakeEngine:
    """Canned ``goldfish`` (own-turn medians keyed by driver-presence) + ``run_matchup``
    (win tallies keyed by opponent + driver-presence). No JVM."""

    def __init__(
        self,
        *,
        driven_median: float,
        cp7_median: float,
        matchups: dict[tuple[str, bool], MatchResult] | None = None,
    ) -> None:
        self._driven_median = driven_median
        self._cp7_median = cp7_median
        self._matchups = matchups or {}
        self.goldfish_calls: list[tuple[bool, str]] = []  # (has_driver, fmt)

    def goldfish(self, deck_a, *, games, install, driver=None, fmt='constructed'):
        self.goldfish_calls.append((driver is not None, fmt))
        median = self._driven_median if driver is not None else self._cp7_median
        return GoldfishResult(median_kills_own=median, games=games)

    def run_matchup(self, deck_a, deck_b, *, n, seed, fmt, install, driver=None):
        return self._matchups[deck_b[0], driver is not None]


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)
    return tmp_path


def _stamp_valid_driver(deck: Deck, data_dir: Path, *, gate_mode: str = 'proactive') -> None:
    """Stamp a CURRENT, gate-passed meta so ``driver_valid`` is True (no compile needed —
    the comparison only reads the fqcn + classes_dir path + gate_mode)."""
    from pipeline.decks.version import version

    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version=version(deck),
            harness_version=drivers.harness_version(data_dir=data_dir),
            fqcn='DriverCompareDeck',
            gates_passed=True,
            gate_mode=gate_mode,
        ),
        data_dir=data_dir,
    )


# --------------------------------------------------------------------------- #
# 1. delta + Wilson-CI math on canned per-piloting results                     #
# --------------------------------------------------------------------------- #


def test_own_turn_delta_is_driver_minus_cp7(_store: Path) -> None:
    """The goldfish lens: own_turn_delta = driver - cp7 (lower = faster), both fmts threaded."""
    deck = _deck()
    _stamp_valid_driver(deck, _store)
    eng = _FakeEngine(driven_median=6.0, cp7_median=8.0)

    comparison = dc.compare_pilotings(deck, _DECK_REF, install=object(), games=5, engine=eng, data_dir=_store)

    assert comparison.own_turn_driver == 6.0
    assert comparison.own_turn_cp7 == 8.0
    assert comparison.own_turn_delta == pytest.approx(-2.0)  # driver 2 turns faster
    assert comparison.winrate_driver is None  # no gauntlet supplied → gauntlet lens omitted
    assert eng.goldfish_calls == [(True, 'constructed'), (False, 'constructed')]


def test_gauntlet_winrate_delta_and_wilson_ci(_store: Path) -> None:
    """The gauntlet lens: aggregate + per-opponent win-rate on DECIDED games, Wilson CI
    matching the reused ``wilson_ci`` helper, delta = driver - cp7."""
    deck = _deck()
    _stamp_valid_driver(deck, _store)
    matchups = {
        ('OppA', True): _mr(4, 1, raw_log='DRIVER_INTENT tag=x'),  # driver 4/5
        ('OppA', False): _mr(2, 3),  # cp7 2/5
        ('OppB', True): _mr(3, 2),  # driver 3/5
        ('OppB', False): _mr(1, 4),  # cp7 1/5
    }
    eng = _FakeEngine(driven_median=6.0, cp7_median=7.0, matchups=matchups)

    comparison = dc.compare_pilotings(
        deck,
        _DECK_REF,
        install=object(),
        games=5,
        gauntlet=[_OPP_A, _OPP_B],
        engine=eng,
        data_dir=_store,
        run_matchups=dc._sequential_run_matchups,
    )

    # driver 7/10, cp7 3/10, delta +0.4 — with the exact reused Wilson interval.
    assert comparison.winrate_driver == pytest.approx(0.7)
    assert comparison.winrate_cp7 == pytest.approx(0.3)
    assert comparison.winrate_delta == pytest.approx(0.4)
    assert comparison.winrate_driver_ci == wilson_ci(7, 10)
    assert comparison.winrate_cp7_ci == wilson_ci(3, 10)
    # per-opponent breakdown.
    opps = {o.opponent: o for o in comparison.per_opponent}
    assert opps['OppA'].winrate_driver == pytest.approx(0.8) and opps['OppA'].winrate_cp7 == pytest.approx(0.4)
    assert opps['OppA'].winrate_delta == pytest.approx(0.4)
    assert opps['OppB'].winrate_driver_ci == wilson_ci(3, 5)


def test_draws_excluded_from_decided_winrate(_store: Path) -> None:
    """Win-rate is on DECIDED games — a draw is neither a win nor a loss in the denominator."""
    deck = _deck()
    _stamp_valid_driver(deck, _store)
    matchups = {
        ('OppA', True): _mr(3, 1, draws=1),  # 3 wins / 4 decided = 0.75 (draw dropped)
        ('OppA', False): _mr(1, 1, draws=3),  # 1 / 2 decided = 0.5
    }
    eng = _FakeEngine(driven_median=6.0, cp7_median=6.0, matchups=matchups)

    comparison = dc.compare_pilotings(
        deck,
        _DECK_REF,
        install=object(),
        games=5,
        gauntlet=[_OPP_A],
        engine=eng,
        data_dir=_store,
        run_matchups=dc._sequential_run_matchups,
    )
    assert comparison.winrate_driver == pytest.approx(0.75)
    assert comparison.winrate_cp7 == pytest.approx(0.5)


def test_no_kill_sentinel_surfaces_a_warning(_store: Path) -> None:
    """A -1.0 'never killed' median is surfaced as a warning (own_turn_delta not meaningful)."""
    deck = _deck()
    _stamp_valid_driver(deck, _store)
    eng = _FakeEngine(driven_median=-1.0, cp7_median=8.0)

    comparison = dc.compare_pilotings(deck, _DECK_REF, install=object(), games=5, engine=eng, data_dir=_store)
    assert any('NEVER killed' in w for w in comparison.warnings)


def test_failed_matchup_is_a_warning_not_a_silent_zero(_store: Path) -> None:
    """A matchup with no paired result (a governor failure) contributes no decided games
    and is surfaced as a warning rather than a silent 0-0 loss."""
    deck = _deck()
    _stamp_valid_driver(deck, _store)

    def _runner(engine, install, specs):
        # Only the DRIVER spec vs OppA produces a result; everything else 'failed'.
        from pipeline.sim.governor import MatchFailure

        pairs = [(s, _mr(5, 0)) for s in specs if s.driver is not None]
        failures = [MatchFailure(spec=s, error='deck-load timeout') for s in specs if s.driver is None]
        return PoolResult(
            pool_size=1,
            results=[r for _, r in pairs],
            failures=failures,
            max_concurrent=1,
            aborted=False,
            min_free_ram_gib=0.0,
            min_free_disk_gib=0.0,
            pairs=pairs,
        )

    eng = _FakeEngine(driven_median=6.0, cp7_median=7.0)
    comparison = dc.compare_pilotings(
        deck, _DECK_REF, install=object(), games=5, gauntlet=[_OPP_A], engine=eng, data_dir=_store, run_matchups=_runner
    )
    assert comparison.winrate_driver == pytest.approx(1.0)  # 5/5
    assert comparison.winrate_cp7 == 0.0  # failed → no decided games
    assert any('FAILED' in w for w in comparison.warnings)


# --------------------------------------------------------------------------- #
# 2. anti-confound wiring (AC9 integrity)                                       #
# --------------------------------------------------------------------------- #


def test_anti_confound_both_pilotings_identical_except_driver(_store: Path) -> None:
    """AC9: both pilotings are scheduled with the SAME engine, SAME per-opponent seed, and
    SAME opponent set — differing ONLY in driver=(classes_dir, fqcn) vs driver=None — and
    NO scheduled matchup pits the candidate against a copy of itself (no mirror)."""
    deck = _deck()
    _stamp_valid_driver(deck, _store)
    captured: list[MatchSpec] = []

    def _capturing_runner(engine, install, specs):
        captured.extend(specs)
        pairs = [(s, _mr(3, 2)) for s in specs]
        return PoolResult(
            pool_size=1,
            results=[r for _, r in pairs],
            failures=[],
            max_concurrent=1,
            aborted=False,
            min_free_ram_gib=0.0,
            min_free_disk_gib=0.0,
            pairs=pairs,
        )

    eng = _FakeEngine(driven_median=6.0, cp7_median=7.0)
    dc.compare_pilotings(
        deck,
        _DECK_REF,
        install=object(),
        games=4,
        gauntlet=[_OPP_A, _OPP_B],
        seed=100,
        engine=eng,
        data_dir=_store,
        run_matchups=_capturing_runner,
    )

    # Two pilotings x two opponents = four specs.
    assert len(captured) == 4
    driver_specs = [s for s in captured if s.driver is not None]
    cp7_specs = [s for s in captured if s.driver is None]
    assert len(driver_specs) == 2 and len(cp7_specs) == 2

    expected_driver = (str(drivers.classes_dir(deck, data_dir=_store)), 'DriverCompareDeck')
    for opp, base_seed in ((_OPP_A, 100), (_OPP_B, 101)):
        dv = next(s for s in driver_specs if s.deck_b[0] == opp.name)
        cp = next(s for s in cp7_specs if s.deck_b[0] == opp.name)
        # Identical in every dimension EXCEPT the driver.
        assert dv.deck_a == cp.deck_a == _DECK_REF  # candidate is ALWAYS PlayerA
        assert dv.deck_b == cp.deck_b == (opp.name, opp.dck_text)
        assert dv.seed == cp.seed == base_seed  # same per-opponent seed
        assert dv.n == cp.n == 4 and dv.fmt == cp.fmt
        assert dv.driver == expected_driver and cp.driver is None
        # NO MIRROR: the opponent is never a copy of the candidate.
        assert dv.deck_b != _DECK_REF and cp.deck_b != _DECK_REF


# --------------------------------------------------------------------------- #
# 3. driver_valid requirement + CLI --json shape                                #
# --------------------------------------------------------------------------- #


def test_power_run_is_default_off_and_doubles_games_when_set(_store: Path) -> None:
    """The opt-in power run is default-off (routine games honoured) and ~2x games when set —
    never a ship requirement, just a tighter headline read."""
    deck = _deck()
    _stamp_valid_driver(deck, _store)
    eng = _FakeEngine(driven_median=6.0, cp7_median=8.0)

    routine = dc.compare_pilotings(deck, _DECK_REF, install=object(), games=5, engine=eng, data_dir=_store)
    assert routine.games == 5  # default-off: no doubling

    powered = dc.compare_pilotings(
        deck, _DECK_REF, install=object(), games=5, engine=eng, data_dir=_store, power_run=True
    )
    assert powered.games == 10  # opt-in: ~2x sample


def test_compare_pilotings_requires_a_valid_driver(_store: Path) -> None:
    """A deck with no gated driver → DriverCompareError pointing at `driver author`."""
    deck = _deck('No Driver Deck')
    eng = _FakeEngine(driven_median=6.0, cp7_median=7.0)
    with pytest.raises(dc.DriverCompareError, match='driver author'):
        dc.compare_pilotings(deck, _DECK_REF, install=object(), games=5, engine=eng, data_dir=_store)


def test_as_dict_json_shape(_store: Path) -> None:
    """The --json projection carries the goldfish + gauntlet + per-opponent structure the
    Phase-6 study consumes."""
    deck = _deck()
    _stamp_valid_driver(deck, _store)
    matchups = {
        ('OppA', True): _mr(4, 1),
        ('OppA', False): _mr(2, 3),
    }
    eng = _FakeEngine(driven_median=6.0, cp7_median=8.0, matchups=matchups)
    comparison = dc.compare_pilotings(
        deck,
        _DECK_REF,
        install=object(),
        games=5,
        gauntlet=[_OPP_A],
        engine=eng,
        data_dir=_store,
        run_matchups=dc._sequential_run_matchups,
    )
    d = comparison.as_dict()

    assert d['candidate'] == 'Compare Deck' and d['gate_mode_used'] == 'proactive'
    assert d['goldfish'] == {'own_turn_driver': 6.0, 'own_turn_cp7': 8.0, 'own_turn_delta': pytest.approx(-2.0)}
    gaunt = d['gauntlet']
    assert isinstance(gaunt, dict)
    assert gaunt['winrate_driver'] == pytest.approx(0.8) and gaunt['winrate_cp7'] == pytest.approx(0.4)
    assert gaunt['per_opponent'][0]['driver_record'] == '4/5'
    assert gaunt['per_opponent'][0]['cp7_record'] == '2/5'
    # round-trips through JSON (machine-readable).
    import json

    json.loads(json.dumps(d))


# --------------------------------------------------------------------------- #
# 4. INTEGRATION (opt-in): real jar, TINY + bounded — the two-piloting proof     #
# --------------------------------------------------------------------------- #

_DATA_DIR = Path.home() / '.local' / 'share' / 'make-magic'
_DIST_JAR = _DATA_DIR / 'xmage' / 'make-magic-xmage-dist.jar'
_LAB_JRE = Path.home() / 'mtg-sim-lab' / 'jre' / 'jdk-21.0.12+8-jre' / 'Contents' / 'Home' / 'bin' / 'java'
_JELEVA_TXT = Path.home() / 'mtg-sim-lab' / 'reward-poc' / 'decks' / 'jeleva.txt'
_INTEGRATION_READY = _DIST_JAR.is_file() and _LAB_JRE.is_file() and _JELEVA_TXT.is_file()


def _jeleva_dck() -> tuple[str, str]:
    lines = [ln.strip() for ln in _JELEVA_TXT.read_text().splitlines() if ln.strip()]
    return ('jeleva', '[metadata]\nName=jeleva\n[Main]\n' + '\n'.join(lines) + '\n')


@pytest.mark.integration
@pytest.mark.skipif(not _INTEGRATION_READY, reason='local dist jar / lab JRE / jeleva deck absent')
def test_jeleva_compare_pilotings_real_jar(monkeypatch: pytest.MonkeyPatch) -> None:
    """END-TO-END: ECJ-compile + gate the Jeleva quad on the real jar, then run the FULL
    benchmark-relative comparison (own-turn goldfish Δ + a 2-opponent curated gauntlet
    win-rate ± Wilson-CI Δ) through the real governor. TINY (games=2) — a mechanism
    proof, not a power measurement. The candidate faces the field, never a copy of itself."""
    from pipeline.sim import driver_authoring as da
    from pipeline.sim import driver_gate as dg
    from pipeline.sim.engine import get_engine
    from pipeline.sim.gauntlet import resolve_gauntlet

    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(_DATA_DIR))
    monkeypatch.setenv('MAKE_MAGIC_JAVA', str(_LAB_JRE))
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(_DIST_JAR))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)  # local-dev: hash the built jar.

    engine = get_engine('xmage')
    install = engine.resolve(provision=False, data_dir=_DATA_DIR)
    deck_ref = _jeleva_dck()
    deck = Deck(name='Jeleva Compare', cards=[DeckCard(name='Forest', quantity=1)])

    dg.compile_quad_driver(deck, da.JELEVA_QUAD_SPEC, data_dir=_DATA_DIR)
    gate = dg.gate_driver(
        deck, deck_ref, spec=da.JELEVA_QUAD_SPEC, install=install, games=12, engine=engine, data_dir=_DATA_DIR
    )
    assert gate.passed, f'gate failed: {gate.reason}'
    assert drivers.driver_valid(deck, data_dir=_DATA_DIR)

    gauntlet = resolve_gauntlet('curated', 'constructed')[:2]  # 2 curated opponents (bounded)
    comparison = dc.compare_pilotings(
        deck, deck_ref, install=install, games=2, gauntlet=gauntlet, fmt='constructed', data_dir=_DATA_DIR
    )

    import json as _json

    print('\nPilotingComparison (real jar):\n' + _json.dumps(comparison.as_dict(), indent=2))
    # Both lenses produced real numbers; the gauntlet lens ran the shared control.
    assert comparison.own_turn_driver == comparison.own_turn_driver  # not NaN
    assert comparison.winrate_driver is not None and comparison.winrate_cp7 is not None
    assert len(comparison.per_opponent) == 2
    # No mirror: every opponent is a curated field deck, never the candidate.
    assert all(o.opponent != 'jeleva' for o in comparison.per_opponent)
