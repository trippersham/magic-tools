"""Phase 5.4 / A3 tests — the corpus opponent field + rule-8 bucket aggregation.

After the A3 cutover the per-deck ``run_one_deck`` / ``run_corpus`` gate→compare harness is
retired (the live path is the simd engine via :func:`~pipeline.sim.driver_run.run_corpus_queue`,
covered by the simd + parity suites). What remains here is the still-live domain surface:

  * the opponent field resolves deterministically to 8 NAMED commander opponents, EXCLUDING any
    deck in the drive set (no subject faces itself);
  * the aggregation pools a correct Wilson-CI lift + the rule-8 ship/thin call on synthetic
    deltas — a bucket that SHIPS, one thin for n<30, one whose CI includes 0;
  * the v2 run-set + tight/loose-P tags come straight off the batch ledger DRIVE rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pipeline.sim import driver_run as dr
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.core import wilson_ci
from pipeline.sim.driver_batch import Ledger
from pipeline.sim.gauntlet import GauntletDeck


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. deterministic opponent field                                              #
# --------------------------------------------------------------------------- #


def test_field_is_eight_named_opponents_excluding_drive_set() -> None:
    """2 cedh + 3 mid + 2 casual + 1 precon = 8 named commander opponents, drive-set excluded."""
    field = dr.build_opponent_field(drive_deck_ids=[])
    assert len(field) == 8
    assert all(isinstance(g, GauntletDeck) and g.name for g in field)
    assert [g.name for g in field] == [g.name for g in dr.build_opponent_field(drive_deck_ids=[])]


def test_field_excludes_drive_decks() -> None:
    """A deck whose bundle-relative id is in the drive set never appears in the field."""
    base = dr.build_opponent_field(drive_deck_ids=[])
    picked = base[0].name
    excluded_id = f'commander/cedh/{picked}.dck'
    refield = dr.build_opponent_field(drive_deck_ids=[excluded_id])
    assert picked not in [g.name for g in refield]
    assert len(refield) == 8


def _precon_names() -> set[str]:
    from pipeline.sim.gauntlet import _bundle

    return {g.name for g in _bundle('commander', 'precons')}


def test_default_field_never_seats_a_zero_card_opponent() -> None:
    """Every opponent in the default (structural) field translates to >= 1 loadable card."""
    from pipeline.sim.engines import xmage as xe

    field = dr.build_opponent_field(drive_deck_ids=[])
    for g in field:
        n = sum(1 for ln in xe._forge_dck_to_xmage_txt(g.dck_text).splitlines() if ln.strip())
        assert n >= dr._MIN_LOADABLE_CARDS, f'{g.name} translated to only {n} cards'


def test_field_excludes_unloadable_and_stays_deterministic() -> None:
    """An injected loadability predicate drops rejected decks; the field is stable + all-loadable."""
    reject = _precon_names()  # pretend every precon fails to load (the real A4 blocker)
    loads = lambda g: g.name not in reject  # noqa: E731
    a = dr.build_opponent_field(drive_deck_ids=[], loads=loads)
    b = dr.build_opponent_field(drive_deck_ids=[], loads=loads)
    assert [g.name for g in a] == [g.name for g in b]  # deterministic
    assert len(a) == 8
    assert all(loads(g) for g in a)  # NO rejected deck survives
    assert not ({g.name for g in a} & reject)


def test_field_redistributes_stratum_slot_when_all_precons_unloadable() -> None:
    """When the whole precon stratum is unloadable the field still fills to 8 from the fallback."""
    reject = _precon_names()
    field = dr.build_opponent_field(drive_deck_ids=[], loads=lambda g: g.name not in reject)
    assert len(field) == 8
    assert not ({g.name for g in field} & reject)


def test_field_fails_loud_when_it_cannot_be_filled() -> None:
    """If nothing loads, the builder RAISES rather than seat a degenerate field."""
    with pytest.raises(ValueError, match='opponent field'):
        dr.build_opponent_field(drive_deck_ids=[], loads=lambda _g: False)


def _precon_deck(stem: str) -> GauntletDeck:
    from pipeline.sim.gauntlet import _bundle

    for g in _bundle('commander', 'precons'):
        if g.name == stem:
            return g
    raise AssertionError(f'precon {stem} not packaged')


@pytest.mark.integration
def test_xmage_deck_loads_rejects_unreleased_precon_accepts_clean_one() -> None:
    """Real base-DB oracle: a clean precon seats; an unreleased-set (Marvel) precon does NOT.

    ``WakandaForever_2026`` references a card absent from the base card DB (``Kimoyo Beads`` —
    an unreleased Marvel-set printing) so it seats 0 cards / aborts; ``AbzanArmor_2025`` is a
    fully-resolvable precon. Proves the residual A4 blocker the JVM-free structural check cannot
    see is caught by :func:`~pipeline.sim.driver_run.xmage_deck_loads`, and that the whole field
    excludes the unreleased deck. Self-skips without a resolvable XMage install + JRE + card DB.
    """
    import subprocess

    try:
        install = xr.resolve()
    except Exception as exc:
        pytest.skip(f'XMage install unresolved: {exc}')
    try:
        subprocess.run([str(install.java), '-version'], capture_output=True, check=True)
    except Exception as exc:
        pytest.skip(f'no runnable JRE (set MAKE_MAGIC_JAVA): {exc}')
    if not (install.mage_tests_dir / 'db').is_dir():
        pytest.skip('base card DB not built (run XMageBatch --warm)')

    assert dr.xmage_deck_loads(_precon_deck('AbzanArmor_2025'), install=install) is True
    assert dr.xmage_deck_loads(_precon_deck('WakandaForever_2026'), install=install) is False

    # Bound the JVM checks to the precon bundle (as run() does); the curated bundles use the
    # cheap structural default so the field build stays a handful of load-checks, not dozens.
    from pipeline.sim.gauntlet import _bundle

    precon_names = {g.name for g in _bundle('commander', 'precons')}

    def loads(g: GauntletDeck) -> bool:
        return dr.xmage_deck_loads(g, install=install) if g.name in precon_names else dr._default_loads(g)

    field = dr.build_opponent_field(drive_deck_ids=[], loads=loads)
    assert len(field) == 8
    assert 'WakandaForever_2026' not in [g.name for g in field]


# --------------------------------------------------------------------------- #
# 2. rule-8 bucket aggregation                                                 #
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
        _delta('a', 'tight-keep', 'drive-dedicated', 90, 100, 20, 100, 40),
        _delta('b', 'loose-keep', 'drive-capable', 9, 10, 1, 10, 8),
        _delta('c', 'tight-keep', 'drive-capable', 50, 100, 50, 100, 40),
    ]
    stats = dr.aggregate_buckets(deltas)

    tight = stats['tight-keep']
    assert tight.driver_decided == 200 and tight.cp7_decided == 200
    assert tight.ships is True and tight.verdict == 'ships-driven'
    assert tight.lift_ci[0] > 0

    loose = stats['loose-keep']
    assert loose.n_matchups == 8 and loose.ships is False
    assert 'matchups' in loose.reason

    capable = stats['drive-capable']
    assert capable.n_matchups == 48
    assert capable.ships is False
    assert capable.lift_ci[0] <= 0 <= capable.lift_ci[1]

    assert stats['all-driven'].n_decks == 3


def _delta_p(deck_id, tightness, dw, dd, cw, cd, n) -> dr.DeckDelta:
    return dr.DeckDelta(
        deck_id=deck_id, keep='', archetype='drive-dedicated',
        driver_wins=dw, driver_decided=dd, cp7_wins=cw, cp7_decided=cd,
        n_matchups=n, p_tightness=tightness,
    )


def test_tight_and_loose_p_buckets_from_tightness() -> None:
    """The v2 p_tightness tag routes decks into tight-P (headline) + loose-P buckets."""
    deltas = [
        _delta_p('a', 'tight', 90, 100, 20, 100, 40),
        _delta_p('b', 'tight', 50, 100, 40, 100, 40),
        _delta_p('c', 'loose', 30, 100, 60, 100, 40),
    ]
    stats = dr.aggregate_buckets(deltas)
    assert 'tight-P' in stats and 'loose-P' in stats
    assert stats['tight-P'].n_decks == 2 and stats['tight-P'].n_matchups == 80
    assert stats['loose-P'].n_decks == 1
    assert stats['all-driven'].n_decks == 3


def test_run_set_from_batch_ledger_reads_drive_rows_and_tightness(_store: Path) -> None:
    """The v2 run-set + tight/loose-P tags come straight off the batch ledger DRIVE rows."""
    led = Ledger(_store / 'v2.jsonl')
    led.record({'deck_id': 'commander/mid/x.dck', 'drive': True, 'stage': 'compiled', 'p_tightness': 'tight'})
    led.record({'deck_id': 'commander/mid/y.dck', 'drive': True, 'stage': 'compiled', 'p_tightness': 'loose'})
    led.record({'deck_id': 'commander/mid/z.dck', 'drive': False, 'stage': 'authored', 'p_tightness': None})
    run_set, tightness = dr.run_set_from_batch_ledger(led)
    assert run_set == ['commander/mid/x.dck', 'commander/mid/y.dck']
    assert tightness == {'commander/mid/x.dck': 'tight', 'commander/mid/y.dck': 'loose'}


def test_bucket_lift_ci_is_newcombe_diff_interval() -> None:
    """The pooled lift CI is Newcombe's method-10 difference-of-proportions interval, not endpoint
    subtraction of the two per-arm Wilson intervals."""
    from pipeline.sim.core import newcombe_diff_ci

    d = _delta('a', 'tight-keep', 'drive-dedicated', 80, 100, 30, 100, 30)
    stats = dr.aggregate_buckets([d])
    s = stats['tight-keep']
    assert s.lift_ci == newcombe_diff_ci(80, 100, 30, 100)
    assert s.pooled_lift == pytest.approx(0.5)
    # And it is NOT the old naive endpoint subtraction.
    d_lo, d_hi = wilson_ci(80, 100)
    c_lo, c_hi = wilson_ci(30, 100)
    assert s.lift_ci != (d_lo - c_hi, d_hi - c_lo)


# --------------------------------------------------------------------------- #
# 3. F-1: the shipped --run CLI wires a ZERO-ARG preflight that resolves+gates Java #
# --------------------------------------------------------------------------- #


def _stub_run_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Stub the ledger/field derivation and capture ``run_corpus_queue``'s kwargs.

    ``run_corpus_queue`` is replaced with a sentinel-raising capture so ``dr.run`` is exercised
    exactly through the preflight wiring, then short-circuits before any real games.
    """
    from types import SimpleNamespace

    captured: dict[str, object] = {}

    class _Sentinel(Exception):
        pass

    def _fake_queue(**kwargs: object) -> object:
        captured.update(kwargs)
        raise _Sentinel

    captured['_Sentinel'] = _Sentinel
    monkeypatch.setattr(dr, 'default_batch_ledger_v2_path', lambda: tmp_path / 'ledger.jsonl')
    monkeypatch.setattr(dr, 'Ledger', lambda _p: SimpleNamespace(rows=lambda: []))
    monkeypatch.setattr(dr, 'run_set_from_batch_ledger', lambda _b: ([], {}))
    monkeypatch.setattr(dr, '_drive_rows_for_run_set', lambda _b, _rs: [])
    monkeypatch.setattr(dr, 'build_opponent_field', lambda _ids, **_kw: [])
    # run() resolves an XMage install to build the real base-DB load-check; keep the stub offline.
    monkeypatch.setattr(xr, 'resolve', lambda *a, **k: (_ for _ in ()).throw(xr.XMageUnavailableError('stub')))
    monkeypatch.setattr(dr, 'run_corpus_queue', _fake_queue)
    return captured


def test_run_wires_zero_arg_preflight_that_boots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """F-1: the shipped ``--run`` wiring passes a ZERO-ARG ``preflight`` the engine can call.

    Regression: ``preflight=preflight_java`` (needs a ``java`` positional) → the engine's
    ``preflight()`` zero-arg call raised ``TypeError`` at boot. The fix wires a zero-arg closure
    that resolves the runtime Java path and version-gates it. With a good launcher + injected
    probe the closure returns the path — proving the CLI boots PAST preflight.
    """
    import pipeline.sim.simd.preflight as pf_mod

    captured = _stub_run_setup(monkeypatch, tmp_path)
    # A real, executable file to satisfy _resolve_java's is_file() gate; probe fakes a 21 banner.
    monkeypatch.setenv('MAKE_MAGIC_JAVA', str(sys.executable))
    monkeypatch.setattr(pf_mod, 'default_java_probe', lambda _java: 'openjdk version "21.0.3" 2024-04-16')

    with pytest.raises(captured['_Sentinel']):  # type: ignore[arg-type]
        dr.run(['--run', '--no-monitor'])

    preflight = captured['preflight']
    assert callable(preflight)
    # The engine calls preflight() with ZERO args — must not TypeError, must return a Java path.
    result = preflight()  # type: ignore[operator]
    assert Path(result) == Path(sys.executable)


def test_run_preflight_still_fails_loud_on_bad_java(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """F-1: the zero-arg wiring still FAILS LOUD on a missing/non-executable Java (not TypeError)."""
    captured = _stub_run_setup(monkeypatch, tmp_path)
    monkeypatch.setenv('MAKE_MAGIC_JAVA', '/nonexistent/definitely/not/java')

    with pytest.raises(captured['_Sentinel']):  # type: ignore[arg-type]
        dr.run(['--run', '--no-monitor'])

    preflight = captured['preflight']
    with pytest.raises(xr.XMageUnavailableError):
        preflight()  # type: ignore[operator]


def _incomplete_result() -> object:
    """A terminal-but-incomplete simd result: one never-run cell + one INVALID cell, complete=False."""
    from pipeline.sim.simd.scheduler import SimdRunResult

    return SimdRunResult(
        results={},
        quarantined=set(),
        cells={('S', 'O', 'driven'): (0, 20), ('S', 'O', 'baseline'): (0, 20)},
        incomplete_cells=[('S', 'O', 'driven'), ('S', 'O', 'baseline')],
        quarantined_cells=[],
        exhausted_cells=[('S', 'O', 'baseline')],
        invalid_cells=[('S', 'O', 'driven')],
        fast_games=0,
        concede_games=0,
        complete=False,
        task_ids=['S|O|driven|0', 'S|O|baseline|0'],
    )


def test_run_refuses_final_publish_on_incomplete_and_writes_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """E2E: an all-timeout/all-invalid run TERMINATES but is incomplete → the CLI refuses to write the
    final ``driver-run-buckets.{json,md}``, writes only an explicitly PARTIAL coverage artifact, and
    exits nonzero. Publication is gated on genuine completeness + zero invalid cells (Sol BLOCKER 1)."""
    from types import SimpleNamespace

    out_dir = tmp_path / 'out'
    monkeypatch.setattr(dr, 'default_batch_ledger_v2_path', lambda: tmp_path / 'ledger.jsonl')
    monkeypatch.setattr(dr, 'Ledger', lambda _p: SimpleNamespace(rows=lambda: []))
    monkeypatch.setattr(dr, 'run_set_from_batch_ledger', lambda _b: (['deck'], {'deck': 'tight'}))
    monkeypatch.setattr(dr, '_drive_rows_for_run_set', lambda _b, _rs: [])
    monkeypatch.setattr(dr, 'build_opponent_field', lambda _ids, **_kw: [])
    monkeypatch.setattr(dr, 'run_corpus_queue', lambda **_kw: _incomplete_result())

    with pytest.raises(SystemExit) as exc:
        dr.run(['--run', '--no-monitor', '--out-dir', str(out_dir)])
    assert exc.value.code != 0

    assert not (out_dir / 'driver-run-buckets.json').exists()
    assert not (out_dir / 'driver-run-buckets.md').exists()
    partial = out_dir / 'driver-run-buckets.PARTIAL.json'
    assert partial.exists()
    import json as _json

    payload = _json.loads(partial.read_text())
    cov = payload['coverage']
    assert cov['complete'] is False
    assert ['S', 'O', 'driven'] in cov['invalid_cells']
    assert ['S', 'O', 'driven'] in cov['incomplete_cells']
    # A never-run task (in the registered universe, absent from results/quarantine) is surfaced.
    assert 'S|O|driven|0' in cov['never_run_tasks']


def test_bucket_markdown_renders_headline() -> None:
    """The markdown table names the tight-keep headline + a row per populated bucket."""
    stats = dr.aggregate_buckets([_delta('a', 'tight-keep', 'drive-dedicated', 90, 100, 20, 100, 40)])
    md = dr.render_bucket_markdown(stats, field_names=['Opp1', 'Opp2'])
    assert 'Headline (tight-keep)' in md
    assert '| tight-keep |' in md
    assert 'Opp1, Opp2' in md


# --------------------------------------------------------------------------- #
# Commander deck-size / integrity guard at the field + staging boundaries.      #
# --------------------------------------------------------------------------- #


def test_default_loads_excludes_undersized_commander_opponent() -> None:
    """The JVM-free default predicate rejects a short (95/98-card) commander deck, accepts a 99+1."""
    short = GauntletDeck(name='short', dck_text=_mk_dck(95))
    legal = GauntletDeck(name='legal', dck_text=_mk_dck(99))
    assert dr._default_loads(legal) is True
    assert dr._default_loads(short) is False


def test_default_field_all_legal_commander_size() -> None:
    """Every opponent in the DEFAULT (structural) field is a legal 100-card commander deck."""
    from pipeline.sim.engines import xmage as xe

    field = dr.build_opponent_field(drive_deck_ids=[])
    assert len(field) == 8
    for g in field:
        assert xe.commander_deck_ok(xe._forge_dck_to_xmage_txt(g.dck_text)), g.name


def test_field_excludes_a_known_undersized_deck_deterministically() -> None:
    """An undersized candidate injected into a stratum is never seated; the field stays 8 + stable."""
    a = dr.build_opponent_field(drive_deck_ids=[])
    b = dr.build_opponent_field(drive_deck_ids=[])
    names_a = [g.name for g in a]
    assert names_a == [g.name for g in b]
    # The two known-short cedh sources must not appear (they translate to 96/99 total).
    assert 'Kenrith, the Returned King' not in names_a
    assert "K'rrik, Son of Yawgmoth" not in names_a


def _mk_dck(main_qty: int, commander: str = '1 Yargle, Glutton of Urborg') -> str:
    """A synthetic Forge .dck with ``main_qty`` distinct maindeck cards + one commander."""
    lines = '\n'.join(f'1 Card {i:03d}' for i in range(main_qty))
    return f'[metadata]\nName=x\n[Commander]\n{commander}\n[Main]\n{lines}\n'


# --------------------------------------------------------------------------- #
# Sol BLOCKER 3 — protection BEFORE staging + guaranteed corpus-* cleanup.      #
# (CLI/run_corpus_queue level, not the engine level.)                          #
# --------------------------------------------------------------------------- #


def _corpus_staging_entries(data_dir: Path) -> list[str]:
    """Names of any ``corpus-*`` dirs currently under the run's staging root."""
    staging = data_dir / 'sim' / 'staging'
    if not staging.exists():
        return []
    return [p.name for p in staging.iterdir() if p.name.startswith('corpus-')]


def test_run_corpus_queue_preflight_fails_with_zero_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing Java (a failing preflight) aborts BEFORE any deck is staged — zero corpus-* dirs."""
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    built = {'n': 0}
    monkeypatch.setattr(dr, 'build_corpus_game_tasks', lambda *a, **k: built.__setitem__('n', built['n'] + 1))

    class _NoJava(Exception):
        pass

    def _preflight() -> None:
        raise _NoJava('java missing')

    with pytest.raises(_NoJava):
        dr.run_corpus_queue(
            rows=[], field=[], games=2, ops_db_path=tmp_path / 'ops.duckdb',
            worker_cmd=['x'], preflight=_preflight,
        )
    assert built['n'] == 0
    assert _corpus_staging_entries(tmp_path) == []


def test_run_corpus_queue_second_launch_refuses_with_zero_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second concurrent launch hits the singleton flock and refuses BEFORE staging — zero dirs."""
    from pipeline.sim.simd.reaper import AlreadyRunning, SingletonLock

    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    ops_db = tmp_path / 'q' / 'ops.duckdb'
    ops_db.parent.mkdir(parents=True)
    held = SingletonLock(ops_db.parent / 'simd.lock').acquire()  # simulate a first launch holding it.
    try:
        built = {'n': 0}
        monkeypatch.setattr(dr, 'build_corpus_game_tasks', lambda *a, **k: built.__setitem__('n', built['n'] + 1))
        with pytest.raises(AlreadyRunning):
            dr.run_corpus_queue(rows=[], field=[], games=2, ops_db_path=ops_db, worker_cmd=['x'])
        assert built['n'] == 0
        assert _corpus_staging_entries(tmp_path) == []
    finally:
        held.release()


def test_run_corpus_queue_cleans_staging_on_normal_and_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both a normal return and a mid-run error leave NO corpus-* staging dirs behind."""
    from types import SimpleNamespace

    import pipeline.sim.simd.engine as engine_mod

    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))

    def _fake_build(rows, field, *, games, stage_dir, data_dir=None):  # type: ignore[no-untyped-def]
        # Prove real files got staged (so cleanup has something to remove).
        (Path(stage_dir) / 's_x.txt').write_text('99 Forest\n', encoding='utf-8')
        return []

    monkeypatch.setattr(dr, 'build_corpus_game_tasks', _fake_build)
    monkeypatch.setattr(dr, 'ingest_queue_transcripts', lambda *a, **k: 0)

    # Normal exit.
    monkeypatch.setattr(engine_mod, 'run_games_simd', lambda *a, **k: SimpleNamespace(results={}))
    dr.run_corpus_queue(
        rows=[{'deck_id': 'd'}], field=[], games=2, ops_db_path=tmp_path / 'ops.duckdb', worker_cmd=['x'],
    )
    assert _corpus_staging_entries(tmp_path) == []

    # Error exit mid-run.
    def _boom(*a: object, **k: object) -> object:
        raise RuntimeError('mid-run failure')

    monkeypatch.setattr(engine_mod, 'run_games_simd', _boom)
    with pytest.raises(RuntimeError):
        dr.run_corpus_queue(
            rows=[{'deck_id': 'd'}], field=[], games=2, ops_db_path=tmp_path / 'ops.duckdb', worker_cmd=['x'],
        )
    assert _corpus_staging_entries(tmp_path) == []


def test_run_wires_disk_governor_and_boot_backoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fable M6: the shipped ``--run`` CLI wires a production DiskGovernor + nonzero boot backoff."""
    from pipeline.sim.simd.governor import DiskGovernor

    captured = _stub_run_setup(monkeypatch, tmp_path)
    monkeypatch.setenv('MAKE_MAGIC_JAVA', str(sys.executable))
    import pipeline.sim.simd.preflight as pf_mod

    monkeypatch.setattr(pf_mod, 'default_java_probe', lambda _java: 'openjdk version "21.0.3" 2024-04-16')

    with pytest.raises(captured['_Sentinel']):  # type: ignore[arg-type]
        dr.run(['--run', '--no-monitor'])

    assert isinstance(captured['disk_governor'], DiskGovernor)
    assert isinstance(captured['boot_backoff_base_s'], float)
    assert captured['boot_backoff_base_s'] > 0.0


# --------------------------------------------------------------------------- #
# Sol BLOCKER 1 — CONTENT-COMPLETE run identity at the production entry point.  #
# run_corpus_queue derives run_id from an immutable manifest of the whole       #
# science; ANY change (games, field, deck bytes) → a DIFFERENT run_id → a fresh #
# run whose ops rows never mix with the incompatible prior universe.            #
# --------------------------------------------------------------------------- #


def _capture_corpus_run_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tag: str,
    subject_names: list[str],
    opp_names: list[str],
    games: int,
    deck_bytes,
) -> str:
    """Drive ``run_corpus_queue`` with a fake staging build (real deck files under stage_dir) and a
    stub engine that captures the ``run_id`` kwarg. Returns the derived run_id."""
    from types import SimpleNamespace

    import pipeline.sim.simd.engine as engine_mod
    from pipeline.sim.game_tasks import SeatSpec, build_game_tasks

    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))

    def _fake_build(rows, field, *, games, stage_dir, data_dir=None):  # type: ignore[no-untyped-def]
        stage = Path(stage_dir)
        subjects = []
        for n in subject_names:
            base = f's_{n}.txt'
            (stage / base).write_text(deck_bytes(n), encoding='utf-8')
            subjects.append(SeatSpec(deck_path=base, driver=None))
        opponents = []
        for n in opp_names:
            base = f'o_{n}.txt'
            (stage / base).write_text(deck_bytes(n), encoding='utf-8')
            opponents.append(SeatSpec(deck_path=base, driver=None))
        return build_game_tasks(subjects, opponents, games, fmt='commander')

    monkeypatch.setattr(dr, 'build_corpus_game_tasks', _fake_build)
    monkeypatch.setattr(dr, 'ingest_queue_transcripts', lambda *a, **k: 0)

    captured: dict[str, str] = {}

    def _fake_simd(tasks, **kw):  # type: ignore[no-untyped-def]
        captured['run_id'] = kw['run_id']
        return SimpleNamespace(results={})

    monkeypatch.setattr(engine_mod, 'run_games_simd', _fake_simd)
    dr.run_corpus_queue(
        rows=[{'deck_id': 'd'}], field=[], games=games,
        ops_db_path=tmp_path / f'ops-{tag}.duckdb', worker_cmd=['x'],
    )
    return captured['run_id']


def _same_bytes(n: str) -> str:
    return f'99 Forest\n# deck {n}\n'


def test_corpus_run_id_stable_for_same_universe_and_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same universe + same config → the SAME run_id → a resume replays committed state."""
    kw = {'subject_names': ['sa'], 'opp_names': ['oa'], 'games': 2, 'deck_bytes': _same_bytes}
    a = _capture_corpus_run_id(monkeypatch, tmp_path, tag='a', **kw)
    b = _capture_corpus_run_id(monkeypatch, tmp_path, tag='b', **kw)
    assert a == b and a.startswith('run-')


def test_corpus_run_id_changes_when_games_shrinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sol's changed-``--games`` repro: a reduced game count is a DIFFERENT universe → new run_id."""
    two = _capture_corpus_run_id(
        monkeypatch, tmp_path, tag='g2', subject_names=['sa'], opp_names=['oa'], games=2, deck_bytes=_same_bytes
    )
    one = _capture_corpus_run_id(
        monkeypatch, tmp_path, tag='g1', subject_names=['sa'], opp_names=['oa'], games=1, deck_bytes=_same_bytes
    )
    assert two != one


def test_corpus_run_id_changes_when_field_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A changed opponent field is a different task universe → a different run_id."""
    base = _capture_corpus_run_id(
        monkeypatch, tmp_path, tag='f1', subject_names=['sa'], opp_names=['oa'], games=2, deck_bytes=_same_bytes
    )
    changed = _capture_corpus_run_id(
        monkeypatch, tmp_path, tag='f2', subject_names=['sa'], opp_names=['oa', 'ob'], games=2, deck_bytes=_same_bytes
    )
    assert base != changed


def test_corpus_run_id_changes_on_same_name_different_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same deck BASENAMES but different staged BYTES → a different run_id (content binding, not
    names): the exact same-name-different-content collision Sol reproduced at the fingerprint level
    can no longer mix incompatible science under one identity."""
    v1 = _capture_corpus_run_id(
        monkeypatch, tmp_path, tag='c1', subject_names=['sa'], opp_names=['oa'], games=2,
        deck_bytes=lambda n: f'99 Forest\n# v1 {n}\n',
    )
    v2 = _capture_corpus_run_id(
        monkeypatch, tmp_path, tag='c2', subject_names=['sa'], opp_names=['oa'], games=2,
        deck_bytes=lambda n: f'98 Forest 1 Island\n# v2 {n}\n',  # DIFFERENT bytes, same basenames
    )
    assert v1 != v2
