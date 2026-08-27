"""Phase 5.3/5.4 tests — the corpus run-batch harness + bucket aggregation (mocked JVM).

The unit tests use JVM-free fakes: a fake engine (canned ``goldfish`` / ``goldfish_output`` /
``run_matchup``) drives both the gate and the compare, and the deterministic sequential runner
threads the compare. Coverage:

  * the opponent field resolves deterministically to 8 NAMED commander opponents, EXCLUDING any
    deck in the drive set (no subject faces itself);
  * a mocked gate-FAIL excludes that deck from compare (terminal ``gate-failed``);
  * the run ledger is RESTARTABLE (a 2nd run skips a ``compared`` deck);
  * the aggregation pools a correct Wilson-CI lift + the rule-8 ship/thin call on synthetic
    deltas — a bucket that SHIPS, one thin for n<30, one whose CI includes 0.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.sim import driver_run as dr
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.core import wilson_ci
from pipeline.sim.driver_batch import Ledger, _driver_uuid
from pipeline.sim.engines.xmage import GoldfishResult
from pipeline.sim.gauntlet import GauntletDeck
from pipeline.sim.runner import MatchResult

# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


def _mr(wins_a: int, wins_b: int, draws: int = 0) -> MatchResult:
    return MatchResult(deck_a='D', deck_b='Opp', wins_a=wins_a, wins_b=wins_b, draws=draws, per_game=(), raw_log='')


class _FakeEngine:
    """Canned gate + compare engine. ``gate_pass`` toggles the DRIVER_REGISTERED /
    MACRO_FIRE_REAL markers + a fast driven median so the gate passes or fails; ``run_matchup``
    returns a fixed driver/cp7 split so compare produces a real PilotingComparison."""

    def __init__(self, *, gate_pass: bool = True, driver_wins: int = 4, cp7_wins: int = 2, games_b: int = 5) -> None:
        self._gate_pass = gate_pass
        self._dw = driver_wins
        self._cw = cp7_wins
        self._gb = games_b

    def _markers(self) -> str:
        if self._gate_pass:
            return 'DRIVER_REGISTERED fqcn=x\nMACRO_FIRE_REAL name=y turn=3\n'
        return ''  # no registration → gate fails

    def goldfish_output(self, deck_a, *, games, install, driver=None, fmt='constructed'):
        median = 5.0 if driver is not None else 8.0
        return GoldfishResult(median_kills_own=median, games=games, max_turn=30), self._markers()

    def goldfish(self, deck_a, *, games, install, driver=None, fmt='constructed'):
        median = 5.0 if driver is not None else 8.0
        return GoldfishResult(median_kills_own=median, games=games, max_turn=30)

    def run_matchup(self, deck_a, deck_b, *, n, seed, fmt, install, driver=None):
        wins_a = self._dw if driver is not None else self._cw
        return _mr(wins_a, self._gb - wins_a)


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)
    return tmp_path


def _batch_ledger(tmp_path: Path, deck_ids: list[str]) -> Ledger:
    """A synthetic P3 batch ledger with compiled DRIVE rows for ``deck_ids``."""
    led = Ledger(tmp_path / 'batch.jsonl')
    for deck_id in deck_ids:
        led.record(
            {
                'deck_id': deck_id,
                'name': deck_id.rsplit('/', 1)[-1].removesuffix('.dck'),
                'source': 'gauntlet',
                'stage': 'compiled',
                'drive': True,
                'archetype': 'drive-dedicated',
                'driver_uuid': _driver_uuid(deck_id),
                'fqcn': f'makemagic.driver.d_{_driver_uuid(deck_id)}.Driver',
                'chosen_combo': {
                    'variant_id': '1-2',
                    'card_names': ['Heliod, Sun-Crowned', 'Walking Ballista'],
                    'result': 'Infinite damage',
                },
            }
        )
    return led


# --------------------------------------------------------------------------- #
# 1. deterministic opponent field                                              #
# --------------------------------------------------------------------------- #


def test_field_is_eight_named_opponents_excluding_drive_set() -> None:
    """2 cedh + 3 mid + 2 casual + 1 precon = 8 named commander opponents, drive-set excluded."""
    field = dr.build_opponent_field(drive_deck_ids=[])
    assert len(field) == 8
    assert all(isinstance(g, GauntletDeck) and g.name for g in field)
    # deterministic: same seed → same field.
    assert [g.name for g in field] == [g.name for g in dr.build_opponent_field(drive_deck_ids=[])]


def test_field_excludes_drive_decks() -> None:
    """A deck whose bundle-relative id is in the drive set never appears in the field."""
    base = dr.build_opponent_field(drive_deck_ids=[])
    # Exclude the first cedh pick; the field must no longer contain it.
    picked = base[0].name
    excluded_id = f'commander/cedh/{picked}.dck'
    refield = dr.build_opponent_field(drive_deck_ids=[excluded_id])
    assert picked not in [g.name for g in refield]
    assert len(refield) == 8


# --------------------------------------------------------------------------- #
# 2. gate-fail excludes a deck from compare                                    #
# --------------------------------------------------------------------------- #


def _real_drive_id() -> str:
    """A real packaged commander gauntlet .dck the harness can parse for commanders."""
    return 'commander/mid/mono-white__heliod-sun-crowned.dck'


def test_gate_fail_excludes_deck_from_compare(_store: Path) -> None:
    """A mocked gate-FAIL lands the deck terminal ``gate-failed`` and never runs compare."""
    deck_id = _real_drive_id()
    batch = _batch_ledger(_store, [deck_id])
    run_ledger = Ledger(_store / 'run.jsonl')
    field = dr.build_opponent_field(drive_deck_ids=[deck_id])

    status = dr.run_one_deck(
        batch.row(deck_id),
        field=field,
        install=object(),
        games=20,
        ledger=run_ledger,
        engine=_FakeEngine(gate_pass=False),
        data_dir=_store,
    )
    assert status == 'gate-failed'
    row = run_ledger.row(deck_id)
    assert row['stage'] == 'gate-failed'
    assert 'comparison' not in row  # compare never ran


def test_gate_pass_runs_compare_and_records(_store: Path) -> None:
    """A gate-PASS runs compare vs the field and persists the full PilotingComparison."""
    deck_id = _real_drive_id()
    batch = _batch_ledger(_store, [deck_id])
    run_ledger = Ledger(_store / 'run.jsonl')
    field = dr.build_opponent_field(drive_deck_ids=[deck_id])

    status = dr.run_one_deck(
        batch.row(deck_id),
        field=field,
        install=object(),
        games=20,
        ledger=run_ledger,
        engine=_FakeEngine(gate_pass=True, driver_wins=4, cp7_wins=2, games_b=5),
        data_dir=_store,
    )
    assert status == 'compared'
    row = run_ledger.row(deck_id)
    assert row['stage'] == 'compared'
    gaunt = row['comparison']['gauntlet']
    assert len(gaunt['per_opponent']) == 8
    # driver 4/5 vs cp7 2/5 per opponent → aggregate 32/40 vs 16/40.
    assert gaunt['winrate_driver'] == pytest.approx(0.8)
    assert gaunt['winrate_cp7'] == pytest.approx(0.4)


# --------------------------------------------------------------------------- #
# 3. restartable run ledger                                                    #
# --------------------------------------------------------------------------- #


def test_run_is_restartable_second_run_skips_compared(_store: Path) -> None:
    """A 2nd run_corpus skips a deck already ``compared`` (the engine is never re-invoked)."""
    deck_id = _real_drive_id()
    batch = _batch_ledger(_store, [deck_id])
    field = dr.build_opponent_field(drive_deck_ids=[deck_id])
    run_ledger_path = _store / 'run.jsonl'

    first = dr.run_corpus(
        run_set=[deck_id],
        install=object(),
        games=20,
        batch_ledger_path=batch.path,
        run_ledger_path=run_ledger_path,
        engine=_FakeEngine(gate_pass=True),
        field=field,
    )
    assert first['counts']['compared'] == 1

    class _Boom:  # any engine call would raise — proving the deck is skipped, not re-run.
        def __getattr__(self, _name):
            raise AssertionError('engine must not be called for an already-compared deck')

    second = dr.run_corpus(
        run_set=[deck_id],
        install=object(),
        games=20,
        batch_ledger_path=batch.path,
        run_ledger_path=run_ledger_path,
        engine=_Boom(),
        field=field,
    )
    assert second['counts']['skipped'] == 1
    assert second['counts']['compared'] == 0


# --------------------------------------------------------------------------- #
# 4. rule-8 bucket aggregation                                                 #
# --------------------------------------------------------------------------- #


def _delta(deck_id, keep, arche, dw, dd, cw, cd, n) -> dr.DeckDelta:
    return dr.DeckDelta(
        deck_id=deck_id,
        keep=keep,
        archetype=arche,
        driver_wins=dw,
        driver_decided=dd,
        cp7_wins=cw,
        cp7_decided=cd,
        n_matchups=n,
    )


def test_aggregation_ship_thin_and_ci_includes_zero() -> None:
    """Rule 8 on synthetic deltas: a SHIP bucket, a thin (n<30) bucket, a CI-includes-0 bucket."""
    deltas = [
        # tight-keep: a big, strongly-positive pooled lift → SHIPS (CI excludes 0, n>=30).
        _delta('a', 'tight-keep', 'drive-dedicated', 90, 100, 20, 100, 40),
        # loose-keep: strong lift but only 8 matchups → thin/inconclusive on n<30.
        _delta('b', 'loose-keep', 'drive-capable', 9, 10, 1, 10, 8),
        # drive-capable also gets deck 'c' with a wash (lift CI straddles 0) but plenty n.
        _delta('c', 'tight-keep', 'drive-capable', 50, 100, 50, 100, 40),
    ]
    stats = dr.aggregate_buckets(deltas)

    tight = stats['tight-keep']
    assert tight.driver_decided == 200 and tight.cp7_decided == 200  # decks a + c pooled
    assert tight.ships is True and tight.verdict == 'ships-driven'
    assert tight.lift_ci[0] > 0

    loose = stats['loose-keep']
    assert loose.n_matchups == 8 and loose.ships is False
    assert 'matchups' in loose.reason  # thin on n<30

    # drive-capable = decks b (9/10 vs 1/10, n=8) + c (50/100 vs 50/100, n=40). The pooled lift
    # CI straddles 0 (c is a wash, b is tiny) → inconclusive despite n>=30.
    capable = stats['drive-capable']
    assert capable.n_matchups == 48
    assert capable.ships is False
    assert capable.lift_ci[0] <= 0 <= capable.lift_ci[1]

    # all-driven pools every deck.
    assert stats['all-driven'].n_decks == 3


def test_bucket_lift_ci_reuses_wilson_helper() -> None:
    """The pooled lift CI is interval arithmetic on the two reused Wilson intervals."""
    d = _delta('a', 'tight-keep', 'drive-dedicated', 80, 100, 30, 100, 30)
    stats = dr.aggregate_buckets([d])
    s = stats['tight-keep']
    d_lo, d_hi = wilson_ci(80, 100)
    c_lo, c_hi = wilson_ci(30, 100)
    assert s.lift_ci == (d_lo - c_hi, d_hi - c_lo)
    assert s.pooled_lift == pytest.approx(0.5)


def test_deltas_from_ledger_pools_per_opponent(_store: Path) -> None:
    """DeckDeltas are extracted from ``compared`` rows, pooling per-opponent driver/cp7 records."""
    deck_id = _real_drive_id()
    batch = _batch_ledger(_store, [deck_id])
    run_ledger = Ledger(_store / 'run.jsonl')
    field = dr.build_opponent_field(drive_deck_ids=[deck_id])
    dr.run_one_deck(
        batch.row(deck_id),
        field=field,
        install=object(),
        games=20,
        ledger=run_ledger,
        engine=_FakeEngine(gate_pass=True, driver_wins=4, cp7_wins=2, games_b=5),
        data_dir=_store,
    )
    run_set_json = {'tight_keep': [deck_id], 'loose_keep': []}
    deltas = dr.deltas_from_ledger(run_ledger, run_set_json)
    assert len(deltas) == 1
    d = deltas[0]
    assert d.keep == 'tight-keep' and d.n_matchups == 8
    assert (d.driver_wins, d.driver_decided) == (32, 40)
    assert (d.cp7_wins, d.cp7_decided) == (16, 40)


def test_bucket_markdown_renders_headline() -> None:
    """The markdown table names the tight-keep headline + a row per populated bucket."""
    stats = dr.aggregate_buckets([_delta('a', 'tight-keep', 'drive-dedicated', 90, 100, 20, 100, 40)])
    md = dr.render_bucket_markdown(stats, field_names=['Opp1', 'Opp2'])
    assert 'Headline (tight-keep)' in md
    assert '| tight-keep |' in md
    assert 'Opp1, Opp2' in md


# --------------------------------------------------------------------------- #
# 5. SMOKE (opt-in): real jar — the full gate+compare pipeline on ONE deck      #
# --------------------------------------------------------------------------- #

_DATA_DIR = Path.home() / '.local' / 'share' / 'make-magic'
_DIST_JAR = Path(
    '/Users/trippwickersham/Code/magic-tools/.worktrees/insearch-quad/plugins/make-magic/'
    'pipeline/pipeline/sim/java/xmage-dist/target/make-magic-xmage-dist.jar'
)
_LAB_JRE = Path.home() / 'mtg-sim-lab' / 'jre' / 'jdk-21.0.12+8-jre' / 'Contents' / 'Home' / 'bin' / 'java'
_SMOKE_DECK = 'commander/mid/mono-white__heliod-sun-crowned.dck'
_BATCH_LEDGER = _DATA_DIR / 'sim' / 'driver_batch' / 'ledger.jsonl'
_SMOKE_READY = _DIST_JAR.is_file() and _LAB_JRE.is_file() and _BATCH_LEDGER.is_file()


@pytest.mark.integration
@pytest.mark.skipif(not _SMOKE_READY, reason='local dist jar / lab JRE / batch ledger absent')
def test_smoke_full_pipeline_one_deck_real_jar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """END-TO-END: real ECJ-compiled Heliod quad → gate (commander solo, games=20) → compare
    (games=2 vs 1 opponent) under a live ResourceMonitor + governor, writing a restartable
    run ledger. TINY — a wiring proof, not a power measurement."""
    from pipeline.sim.engine import get_engine
    from pipeline.sim.monitor import MonitorConfig, ResourceMonitor

    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(_DATA_DIR))
    monkeypatch.setenv('MAKE_MAGIC_JAVA', str(_LAB_JRE))
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(_DIST_JAR))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)  # local-dev: hash the built jar.

    engine = get_engine('xmage')
    install = engine.resolve(provision=False, data_dir=_DATA_DIR)

    batch = Ledger(_BATCH_LEDGER)
    row = batch.row(_SMOKE_DECK)
    assert row.get('stage') == 'compiled' and row.get('drive')

    # 1 opponent, excluding the whole compiled DRIVE set.
    drive_ids = [r['deck_id'] for r in batch.rows() if r.get('drive') and r.get('stage') in ('compiled', 'gated')]
    field = dr.build_opponent_field(drive_ids)[:1]

    run_ledger = Ledger(tmp_path / 'run.jsonl')
    monitor = ResourceMonitor(MonitorConfig(state_dir=tmp_path / 'monitor', sample_interval_s=5.0))
    monitor.start()
    try:
        status = dr.run_one_deck(
            row,
            field=field,
            install=install,
            games=20,  # gate floor
            compare_games=2,  # tiny compare
            ledger=run_ledger,
            engine=engine,
            data_dir=_DATA_DIR,
        )
    finally:
        monitor.stop()

    import json as _json

    persisted = run_ledger.row(_SMOKE_DECK)
    print('\nSMOKE run-ledger row:\n' + _json.dumps(persisted, indent=2))
    assert status in ('compared', 'gate-failed')
    assert persisted['stage'] == status
    if status == 'compared':
        gaunt = persisted['comparison']['gauntlet']
        assert len(gaunt['per_opponent']) == 1
        assert gaunt['per_opponent'][0]['opponent'] != 'mono-white__heliod-sun-crowned'


def test_run_reads_sys_argv_when_argv_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression: main()/run(None) must honor the real CLI (sys.argv), not parse an empty list.

    The original ``parse_args([] if argv is None else argv)`` discarded sys.argv, so ``--run``
    (and every flag) was silently dropped when invoked as ``python -m ...`` — the corpus run
    became a no-op that only emitted an empty bucket table. Here we prove a custom ``--out-dir``
    passed via sys.argv is honored (aggregate-only path, no sims).
    """
    out = tmp_path / 'custom_out'
    out.mkdir()
    empty_ledger = tmp_path / 'run_ledger.jsonl'
    empty_ledger.write_text('', encoding='utf-8')
    monkeypatch.setattr(
        'sys.argv',
        ['driver-run', '--aggregate-only', '--out-dir', str(out), '--run-ledger', str(empty_ledger)],
    )
    dr.main()  # argv=None -> must read sys.argv
    written = list(out.glob('driver-run-buckets.*'))
    assert {p.suffix for p in written} == {'.json', '.md'}, f'custom --out-dir not honored: {written}'
