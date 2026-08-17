"""TDD tests for the ``simulate`` CLI dispatcher (Phase 7).

Every verb that would spawn Forge is exercised with the sim CORE mocked
(``core.simulate`` / ``core.compare`` / ``runner.run_matchup`` /
``forge_runtime.resolve``), so NO real Forge JVM ever runs in this suite. The
assertions pin the argparse wiring: that ``match`` / ``deck`` / ``ab`` /
``gauntlet show`` / ``doctor`` dispatch to the right core function with the
parsed args (n, seed, --gauntlet, --format, --force) mapped correctly, that
deck references resolve as an Airtable name (mock store) vs a ``.dck`` file, and
that ``doctor`` reports gracefully whether or not Forge is present.

``gauntlet show`` reads the REAL bundled curated data (no Forge, no network).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.sim import forge_runtime
from pipeline.sim import run as sim_run
from pipeline.sim.core import Comparison, OpponentResult, SimResult, TelemetryProfile
from pipeline.sim.engine import EngineInstall, EngineUnavailableError
from pipeline.sim.forge_runtime import FORGE_VERSION, ForgeInstall, ForgeUnavailableError
from pipeline.sim.runner import GameOutcome, MatchResult
from pipeline.sim.telemetry import PilotingProfile, unavailable_piloting

# A minimal but FLOOR-VALID deck body (>= 40 cards, all basics so they're always
# Forge-loadable) — used by the dispatch tests that mock the engine but still pass
# through the pre-JVM guard, whose R3-1 size floor rejects a below-minimum deck.
_VALID_CONSTRUCTED = '[Main]\n40 Mountain\n'
#: A floor-valid commander body (>= 100 cards).
_VALID_COMMANDER = '[Main]\n100 Mountain\n'


# --------------------------------------------------------------------------- #
# Fixtures / builders.
# --------------------------------------------------------------------------- #


def _piloting(counter_fire: float, removal_fire: float = 0.7, *, available: bool = True) -> PilotingProfile:
    """A populated ``PilotingProfile`` (or an unavailable marker) for the compare tests.

    The ``both`` renderer reads ``counter_fire`` / ``removal_fire`` — the false-read
    signal — so those are the params; the opportunity counts are plausible fillers.
    """
    if not available:
        return unavailable_piloting('otag lake could not classify the deck')
    return PilotingProfile(
        counter_opps=10,
        counter_casts=round(counter_fire * 10),
        counter_fire=counter_fire,
        counter_ci=(max(0.0, counter_fire - 0.1), min(1.0, counter_fire + 0.1)),
        removal_opps=10,
        removal_casts=round(removal_fire * 10),
        removal_fire=removal_fire,
        removal_ci=(max(0.0, removal_fire - 0.1), min(1.0, removal_fire + 0.1)),
        interaction_stranded_per_game=0.5,
        games=4,
    )


def _sim_result(
    candidate: str = 'Cand',
    fmt: str = 'constructed',
    *,
    failures: tuple[tuple[str, str], ...] = (),
    aborted: bool = False,
    total_games: int = 4,
    win_rate: float = 0.75,
    piloting: PilotingProfile | None = None,
) -> SimResult:
    """A populated ``SimResult`` a mocked ``core.simulate`` can return."""
    profile = TelemetryProfile(
        games=4,
        avg_kill_turn=7.5,
        median_kill_turn=7.0,
        avg_win_margin_life=6.0,
        median_win_margin_life=5.0,
        wincon_mix={'combat': 3, 'burn': 1},
        mean_ramp_curve=[1.0, 2.0, 3.0],
    )
    per_opp = [
        OpponentResult(
            opponent='MonoRedAggro',
            wins=3,
            losses=1,
            draws=0,
            games=4,
            win_rate=0.75,
            win_rate_ci=(0.3, 0.95),
            cached=False,
        ),
    ]
    return SimResult(
        candidate=candidate,
        gauntlet_source='curated',
        fmt=fmt,
        games_per_opponent=4,
        total_games=total_games,
        wins=3,
        losses=1,
        draws=0,
        win_rate=win_rate,
        win_rate_ci=(max(0.0, win_rate - 0.2), min(1.0, win_rate + 0.2)),
        per_opponent=per_opp,
        profile=profile,
        cached_matchups=0,
        fresh_matchups=1,
        failures=failures,
        aborted=aborted,
        piloting=piloting,
    )


@pytest.fixture()
def install() -> ForgeInstall:
    """A dummy resolved install (paths never touched — resolve is mocked)."""
    return ForgeInstall(forge_dir=Path('/tmp/forge'), jar=Path('/tmp/forge/f.jar'), java=Path('/tmp/java'))


@pytest.fixture()
def mock_resolve(monkeypatch: pytest.MonkeyPatch, install: ForgeInstall) -> ForgeInstall:
    """Patch the Forge runtime's ``resolve``/``ensure`` to return a dummy install.

    The engine seam (``_ensure_engine`` -> ``ForgeEngine.resolve``) delegates to
    :func:`pipeline.sim.forge_runtime.resolve` (read-only) / ``ensure``
    (fetch-on-miss); patching those keeps the CLI suite off any real fetch/locate
    while exercising the real engine wrapper.
    """
    monkeypatch.setattr(forge_runtime, 'resolve', lambda **_: install)
    monkeypatch.setattr(forge_runtime, 'ensure', lambda **_: install)
    return install


# --------------------------------------------------------------------------- #
# main dispatch
# --------------------------------------------------------------------------- #


def test_unknown_verb_usage_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """An unknown verb prints usage to stderr and exits non-zero (no traceback)."""
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['bogus'])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'usage' in err.lower()
    assert 'bogus' not in err or 'verbs' in err.lower()


def test_no_verb_usage_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """No verb at all -> usage + non-zero exit."""
    with pytest.raises(SystemExit) as exc:
        sim_run.main([])
    assert exc.value.code != 0
    assert 'usage' in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- #
# match
# --------------------------------------------------------------------------- #


def test_match_dispatches_run_matchup(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``match`` parses -n/-s/--format and calls run_matchup with a win tally."""
    dck_a = tmp_path / 'A.dck'
    dck_b = tmp_path / 'B.dck'
    # commander invocation below → >= 100-card floor-valid bodies.
    dck_a.write_text('[metadata]\nName=A\n' + _VALID_COMMANDER)
    dck_b.write_text('[metadata]\nName=B\n' + _VALID_COMMANDER)

    seen: dict[str, object] = {}

    def _fake_run_matchup(
        install: ForgeInstall,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str = 'constructed',
        timeout_s: int = 30,
    ) -> MatchResult:
        seen.update(deck_a=deck_a, deck_b=deck_b, n=n, seed=seed, fmt=fmt)
        return MatchResult(
            deck_a=deck_a[0],
            deck_b=deck_b[0],
            wins_a=6,
            wins_b=3,
            draws=1,
            per_game=(GameOutcome(winner='a', elapsed_ms=100),),
            raw_log='',
        )

    # `match` now calls the engine, which delegates to runner.run_matchup — patch
    # the runner so the real ForgeEngine wrapper is exercised end-to-end.
    monkeypatch.setattr('pipeline.sim.runner.run_matchup', _fake_run_matchup)

    sim_run.main(['match', str(dck_a), str(dck_b), '-n', '10', '-s', '99', '--format', 'commander'])

    assert seen['n'] == 10
    assert seen['seed'] == 99
    assert seen['fmt'] == 'commander'
    out = capsys.readouterr().out
    assert '6' in out and '3' in out  # win tally surfaced.


# --------------------------------------------------------------------------- #
# deck -> simulate
# --------------------------------------------------------------------------- #


def test_deck_dispatches_simulate(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``deck`` parses --gauntlet/--games/--format/--force and calls simulate."""
    dck = tmp_path / 'MyDeck.dck'
    # commander invocation below → >= 100-card floor-valid body.
    dck.write_text('[metadata]\nName=MyDeck\n' + _VALID_COMMANDER)

    seen: dict[str, object] = {}

    def _fake_simulate(deck: object, gauntlet_source: str, **kwargs: object) -> SimResult:
        seen.update(deck=deck, gauntlet_source=gauntlet_source, **kwargs)
        return _sim_result()

    monkeypatch.setattr(sim_run, 'simulate', _fake_simulate)

    sim_run.main(['deck', str(dck), '--gauntlet', 'both', '--games', '8', '--format', 'commander', '--force'])

    assert seen['gauntlet_source'] == 'both'
    assert seen['games'] == 8
    assert seen['fmt'] == 'commander'
    assert seen['force'] is True
    out = capsys.readouterr().out
    assert 'win' in out.lower()  # win-rate reported.
    assert 'MonoRedAggro' in out  # per-opponent breakdown.


def test_deck_gauntlet_defaults(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default gauntlet is curated, default force is False."""
    dck = tmp_path / 'D.dck'
    dck.write_text(_VALID_CONSTRUCTED)
    seen: dict[str, object] = {}

    def _fake_simulate(deck: object, gauntlet_source: str, **kwargs: object) -> SimResult:
        seen.update(gauntlet_source=gauntlet_source, **kwargs)
        return _sim_result()

    monkeypatch.setattr(sim_run, 'simulate', _fake_simulate)
    sim_run.main(['deck', str(dck)])
    assert seen['gauntlet_source'] == 'curated'
    assert seen['force'] is False


def test_deck_surfaces_matchup_failures_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A FAILED matchup prints a distinct failure line to stderr (B2) — NOT a
    silent 0-0-0 row indistinguishable from 'lost every game'."""
    dck = tmp_path / 'D.dck'
    dck.write_text(_VALID_CONSTRUCTED)

    def _fake_simulate(deck: object, gauntlet_source: str, **kwargs: object) -> SimResult:
        return _sim_result(failures=(('BorosStrong', 'Forge could not load a deck'),))

    monkeypatch.setattr(sim_run, 'simulate', _fake_simulate)
    sim_run.main(['deck', str(dck)])

    captured = capsys.readouterr()
    assert 'FAILED' in captured.err
    assert 'BorosStrong' in captured.err
    assert 'could not load a deck' in captured.err.lower()
    # The failure is on STDERR, keeping stdout the clean result table.
    assert 'FAILED' not in captured.out


def test_deck_surfaces_aborted_run_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A partial (aborted) run prints a prominent notice to stderr (B2)."""
    dck = tmp_path / 'D.dck'
    dck.write_text(_VALID_CONSTRUCTED)
    monkeypatch.setattr(sim_run, 'simulate', lambda *a, **k: _sim_result(aborted=True))
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['deck', str(dck)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert 'ABORTED' in err
    assert 'PARTIAL' in err


# --------------------------------------------------------------------------- #
# R2-3 — non-zero exit when a run produced ZERO usable games (all-failed / aborted)
# --------------------------------------------------------------------------- #


def test_deck_all_matchups_failed_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run where EVERY matchup failed (zero usable games) exits non-zero so a
    cron/agent consumer can't read success on a dead run (R2-3)."""
    dck = tmp_path / 'D.dck'
    dck.write_text(_VALID_CONSTRUCTED)
    monkeypatch.setattr(
        sim_run,
        'simulate',
        lambda *a, **k: _sim_result(total_games=0, failures=(('BorosStrong', 'Could not load a deck'),)),
    )
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['deck', str(dck)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert 'FAILED' in err  # the failure is still surfaced before the exit.


def test_deck_legit_zero_winrate_with_real_games_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
) -> None:
    """A legitimate 0% win-rate WITH real games played (lost every game) is NOT a
    failure — it exits 0. Only a run with no usable games is a failure (R2-3)."""
    dck = tmp_path / 'D.dck'
    dck.write_text(_VALID_CONSTRUCTED)
    # total_games > 0, no failures, no abort -> a real (losing) run.
    monkeypatch.setattr(sim_run, 'simulate', lambda *a, **k: _sim_result(total_games=4))
    sim_run.main(['deck', str(dck)])  # no SystemExit -> exit 0.


def test_deck_aborted_run_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
) -> None:
    """An aborted run (even with some games) exits non-zero — results are partial
    and a consumer must not treat them as a clean success (R2-3)."""
    dck = tmp_path / 'D.dck'
    dck.write_text(_VALID_CONSTRUCTED)
    monkeypatch.setattr(sim_run, 'simulate', lambda *a, **k: _sim_result(total_games=4, aborted=True))
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['deck', str(dck)])
    assert exc.value.code == 1


def test_ab_all_failed_side_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ab`` exits non-zero when EITHER side produced zero usable games (R2-3)."""
    a = tmp_path / 'A.dck'
    b = tmp_path / 'B.dck'
    a.write_text(_VALID_CONSTRUCTED)
    b.write_text(_VALID_CONSTRUCTED)

    def _fake_compare(variant_a: object, variant_b: object, gauntlet_source: str, **kwargs: object) -> Comparison:
        return Comparison(
            a=_sim_result('A', total_games=4),
            b=_sim_result('B', total_games=0, failures=(('X', 'crash'),)),
            win_rate_delta=0.0,
            metric_deltas={},
            stronger=None,
        )

    monkeypatch.setattr(sim_run, 'compare', _fake_compare)
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['ab', str(a), str(b)])
    assert exc.value.code == 1


def test_ab_both_sides_have_games_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
) -> None:
    """``ab`` with real games on both sides exits 0 even at a lopsided win-rate."""
    a = tmp_path / 'A.dck'
    b = tmp_path / 'B.dck'
    a.write_text(_VALID_CONSTRUCTED)
    b.write_text(_VALID_CONSTRUCTED)

    def _fake_compare(variant_a: object, variant_b: object, gauntlet_source: str, **kwargs: object) -> Comparison:
        return Comparison(
            a=_sim_result('A', total_games=4),
            b=_sim_result('B', total_games=4),
            win_rate_delta=0.1,
            metric_deltas={},
            stronger='A',
        )

    monkeypatch.setattr(sim_run, 'compare', _fake_compare)
    sim_run.main(['ab', str(a), str(b)])  # no SystemExit -> exit 0.


# --------------------------------------------------------------------------- #
# ab -> compare
# --------------------------------------------------------------------------- #


def test_ab_dispatches_compare(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ab`` calls compare with both variants + parsed args."""
    a = tmp_path / 'A.dck'
    b = tmp_path / 'B.dck'
    a.write_text(_VALID_CONSTRUCTED)
    b.write_text(_VALID_CONSTRUCTED)

    seen: dict[str, object] = {}

    def _fake_compare(variant_a: object, variant_b: object, gauntlet_source: str, **kwargs: object) -> Comparison:
        seen.update(a=variant_a, b=variant_b, gauntlet_source=gauntlet_source, **kwargs)
        ra = _sim_result('A')
        rb = _sim_result('B')
        return Comparison(
            a=ra,
            b=rb,
            win_rate_delta=0.1,
            metric_deltas={'avg_kill_turn': -0.5},
            stronger='A',
        )

    monkeypatch.setattr(sim_run, 'compare', _fake_compare)

    sim_run.main(['ab', str(a), str(b), '--gauntlet', 'mine', '--games', '6', '--force'])

    assert seen['gauntlet_source'] == 'mine'
    assert seen['games'] == 6
    assert seen['force'] is True
    out = capsys.readouterr().out
    assert 'A' in out and 'B' in out
    assert 'avg_kill_turn' in out  # per-metric deltas surfaced.


# --------------------------------------------------------------------------- #
# gauntlet show (real bundled data)
# --------------------------------------------------------------------------- #


def test_gauntlet_show_constructed(capsys: pytest.CaptureFixture[str]) -> None:
    """``gauntlet show`` lists the 5 curated constructed decks (real data)."""
    sim_run.main(['gauntlet', 'show', '--format', 'constructed'])
    out = capsys.readouterr().out
    for name in ('MonoRedAggro', 'MonoBlueTempo', 'MonoGreenStompy', 'MonoWhiteWide', 'MonoBlackMidrange'):
        assert name in out


def test_gauntlet_show_commander(capsys: pytest.CaptureFixture[str]) -> None:
    """``gauntlet show --format commander`` lists the 2 curated commander decks."""
    sim_run.main(['gauntlet', 'show', '--format', 'commander'])
    out = capsys.readouterr().out
    assert 'GreenStompyEDH' in out
    assert 'BlackMidrangeEDH' in out


def test_gauntlet_show_named_bundle_guilds(capsys: pytest.CaptureFixture[str]) -> None:
    """``gauntlet show --source guilds`` lists the packaged 30-deck bundle."""
    sim_run.main(['gauntlet', 'show', '--source', 'guilds'])
    out = capsys.readouterr().out
    assert 'guilds gauntlet (constructed): 30 deck(s)' in out
    assert 'GruulStrong' in out
    assert 'AzoriusWeak' in out


@pytest.mark.parametrize('source', ['mine', 'both'])
def test_gauntlet_show_rejects_live_sources(source: str, capsys: pytest.CaptureFixture[str]) -> None:
    """``gauntlet show`` lists PACKAGED decks only; `mine`/`both` need a live store."""
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['gauntlet', 'show', '--source', source])
    assert exc.value.code == 1
    assert 'packaged decks only' in capsys.readouterr().err


def test_gauntlet_format_mismatch_rejected_before_forge(capsys: pytest.CaptureFixture[str]) -> None:
    """A constructed-only bundle under `--format commander` fails EARLY with a clean
    error — before Forge is provisioned (the ``--gauntlet guilds`` footgun fix)."""
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['deck', 'x.dck', '--gauntlet', 'guilds', '--format', 'commander'])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert 'guilds' in err and 'commander' in err


def test_top_level_help_lists_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    """`simulate -h` prints the verb catalog to stdout and exits 0 (no SystemExit)."""
    sim_run.main(['-h'])
    out = capsys.readouterr().out
    assert 'verbs:' in out
    for verb in ('deck', 'ab', 'match', 'gauntlet', 'log', 'doctor'):
        assert verb in out


# --------------------------------------------------------------------------- #
# --engine selection (task 1.7)
# --------------------------------------------------------------------------- #


def test_deck_defaults_to_forge_engine(
    monkeypatch: pytest.MonkeyPatch,
    mock_resolve: ForgeInstall,
    tmp_path: Path,
) -> None:
    """With no --engine, the deck verb routes to the Forge engine."""
    dck = tmp_path / 'D.dck'
    dck.write_text('[metadata]\nName=D\n' + _VALID_CONSTRUCTED)
    seen: dict[str, object] = {}
    monkeypatch.setattr(sim_run, 'simulate', lambda deck, src, **kw: seen.update(kw) or _sim_result())

    sim_run.main(['deck', str(dck)])

    assert seen['engine'].name == 'forge'  # type: ignore[union-attr]


def test_deck_engine_flag_routes_to_selected_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`--engine <name>` selects that registered engine (registry-driven routing)."""
    import types

    from pipeline.sim import engine as engine_mod

    fake = types.SimpleNamespace(name='fake')
    # Register the fake so argparse's choices (available_engines) accept it and
    # get_engine resolves it; setitem auto-reverts after the test.
    monkeypatch.setitem(engine_mod._REGISTRY, 'fake', fake)  # type: ignore[arg-type]
    # Bypass the Forge-specific provision/guard — routing is what's under test.
    monkeypatch.setattr(sim_run, '_ensure_engine', lambda eng, **_: EngineInstall(version='x', handle=object()))
    monkeypatch.setattr(sim_run, '_guard_forge_availability', lambda *a, **k: None)
    seen: dict[str, object] = {}
    monkeypatch.setattr(sim_run, 'simulate', lambda deck, src, **kw: seen.update(kw) or _sim_result())

    dck = tmp_path / 'D.dck'
    dck.write_text('[metadata]\nName=D\n' + _VALID_CONSTRUCTED)
    sim_run.main(['deck', str(dck), '--engine', 'fake'])

    assert seen['engine'] is fake


def test_engine_bogus_choice_errors_cleanly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unregistered --engine is rejected by argparse (exit 2, no traceback)."""
    dck = tmp_path / 'D.dck'
    dck.write_text('[metadata]\nName=D\n' + _VALID_CONSTRUCTED)
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['deck', str(dck), '--engine', 'bogus'])
    assert exc.value.code == 2  # argparse usage error.
    err = capsys.readouterr().err
    assert 'invalid choice' in err and 'bogus' in err
    assert 'Traceback' not in err


# --------------------------------------------------------------------------- #
# --engine both — side-by-side compare mode (task 3.1)
# --------------------------------------------------------------------------- #


def _mock_both_paths(
    monkeypatch: pytest.MonkeyPatch,
    *,
    per_engine: dict[str, SimResult],
    unavailable: dict[str, str] | None = None,
) -> None:
    """Wire the ``deck --engine both`` seam: per-engine ``simulate`` results + which
    engines are unavailable. Bypasses the real resolve/provision/guard so no JVM runs.
    """
    unavailable = unavailable or {}

    def _ensure(engine: object, **_: object) -> EngineInstall:
        name = engine.name  # type: ignore[attr-defined]
        if name in unavailable:
            raise EngineUnavailableError(unavailable[name])
        return EngineInstall(version=f'{name}-x', handle=object())

    monkeypatch.setattr(sim_run, '_ensure_engine', _ensure)
    monkeypatch.setattr(sim_run, '_guard_forge_availability', lambda *a, **k: None)
    monkeypatch.setattr(sim_run, 'simulate', lambda deck, src, **kw: per_engine[kw['engine'].name])


def test_deck_engine_both_renders_side_by_side_and_false_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--engine both` prints a per-engine row, the Δ, and a computed false-read note.

    Forge under-pilots counters (low fire-rate) vs XMage → its win-rate is flagged as
    the weaker read where the two diverge.
    """
    dck = tmp_path / 'D.dck'
    dck.write_text('[metadata]\nName=D\n' + _VALID_CONSTRUCTED)
    _mock_both_paths(
        monkeypatch,
        per_engine={
            'forge': _sim_result('D', win_rate=0.42, piloting=_piloting(counter_fire=0.03)),
            'xmage': _sim_result('D', win_rate=0.30, piloting=_piloting(counter_fire=0.31)),
        },
    )

    sim_run.main(['deck', str(dck), '--engine', 'both'])  # no SystemExit -> exit 0.

    out = capsys.readouterr().out
    assert 'forge' in out and 'xmage' in out  # both rows.
    assert 'Δ' in out  # the delta line.
    assert 'false-read' in out and 'UNDER-CASTS' in out
    # Forge (0.03 counter-fire) is the under-casting engine named in the note, and the
    # note is COUNTER-specific (not generalized to "interaction").
    note = next(line for line in out.splitlines() if 'UNDER-CASTS' in line)
    assert 'forge' in note and 'forge' in note.split('UNDER-CASTS')[0]
    assert 'COUNTERS' in note and 'interaction' not in note.lower()


def test_deck_engine_both_comparable_piloting_is_neutral(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the engines' counter fire-rates are comparable, the note is neutral (no false-read)."""
    dck = tmp_path / 'D.dck'
    dck.write_text('[metadata]\nName=D\n' + _VALID_CONSTRUCTED)
    _mock_both_paths(
        monkeypatch,
        per_engine={
            'forge': _sim_result('D', win_rate=0.50, piloting=_piloting(counter_fire=0.30)),
            'xmage': _sim_result('D', win_rate=0.52, piloting=_piloting(counter_fire=0.32)),
        },
    )

    sim_run.main(['deck', str(dck), '--engine', 'both'])

    out = capsys.readouterr().out
    assert 'comparable' in out
    assert 'UNDER-CASTS' not in out


def test_deck_engine_both_one_unavailable_runs_other_and_reports_skip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One engine unavailable → run the other, print its row, report the skip on stderr, exit 0."""
    dck = tmp_path / 'D.dck'
    dck.write_text('[metadata]\nName=D\n' + _VALID_CONSTRUCTED)
    _mock_both_paths(
        monkeypatch,
        per_engine={'forge': _sim_result('D', win_rate=0.42, piloting=_piloting(counter_fire=0.03))},
        unavailable={'xmage': 'set MAKE_MAGIC_XMAGE_HOME to a built reactor'},
    )

    sim_run.main(['deck', str(dck), '--engine', 'both'])  # forge produced games -> exit 0.

    captured = capsys.readouterr()
    assert 'forge' in captured.out  # the available side ran.
    assert 'xmage' in captured.err and 'SKIPPED' in captured.err
    assert 'MAKE_MAGIC_XMAGE_HOME' in captured.err  # actionable how-to-enable.


def test_deck_engine_both_all_unavailable_errors_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every engine unavailable → a clean error naming the miss, non-zero exit (nothing to compare)."""
    dck = tmp_path / 'D.dck'
    dck.write_text('[metadata]\nName=D\n' + _VALID_CONSTRUCTED)
    _mock_both_paths(
        monkeypatch,
        per_engine={},
        unavailable={'forge': 'no Forge install', 'xmage': 'no XMage reactor'},
    )

    with pytest.raises(SystemExit) as exc:
        sim_run.main(['deck', str(dck), '--engine', 'both'])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert 'no sim engine is available' in err
    assert 'Traceback' not in err


@pytest.mark.parametrize('verb', ['match', 'ab'])
def test_engine_both_rejected_on_non_deck_verbs(
    verb: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`both` is a `deck`-only pseudo-engine — `match`/`ab` reject it (argparse, exit 2)."""
    a = tmp_path / 'A.dck'
    b = tmp_path / 'B.dck'
    a.write_text('[metadata]\nName=A\n' + _VALID_CONSTRUCTED)
    b.write_text('[metadata]\nName=B\n' + _VALID_CONSTRUCTED)
    with pytest.raises(SystemExit) as exc:
        sim_run.main([verb, str(a), str(b), '--engine', 'both'])
    assert exc.value.code == 2
    assert 'invalid choice' in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_available(
    monkeypatch: pytest.MonkeyPatch,
    install: ForgeInstall,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """doctor with a resolvable Forge prints version + pool size + paths, exit 0."""
    monkeypatch.setattr(forge_runtime, 'resolve', lambda **_: install)
    monkeypatch.setattr(sim_run, 'derive_pool_size', lambda **_: 4)
    monkeypatch.setattr(sim_run, 'free_ram_gib', lambda: 12.5)
    monkeypatch.setattr(sim_run, 'free_disk_gib', lambda: 88.0)

    sim_run.main(['doctor'])  # no SystemExit -> exit 0.

    out = capsys.readouterr().out
    assert FORGE_VERSION in out  # engine install version (the pinned Forge version)
    assert '4' in out  # pool size.
    assert str(install.jar) in out
    assert str(install.java) in out
    assert '12.5' in out and '88.0' in out  # RAM/disk snapshot.


def test_doctor_unavailable_graceful(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """doctor with an unavailable Forge prints an actionable message, exits non-zero, no traceback."""

    def _raise(**_: object) -> ForgeInstall:
        raise ForgeUnavailableError('No Forge install found. Set MAKE_MAGIC_FORGE_HOME ...')

    monkeypatch.setattr(forge_runtime, 'resolve', _raise)
    # Still report the runtime snapshot even when Forge is absent.
    monkeypatch.setattr(sim_run, 'derive_pool_size', lambda **_: 4)
    monkeypatch.setattr(sim_run, 'free_ram_gib', lambda: 12.5)
    monkeypatch.setattr(sim_run, 'free_disk_gib', lambda: 88.0)

    with pytest.raises(SystemExit) as exc:
        sim_run.main(['doctor'])
    assert exc.value.code != 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert 'not available' in combined.lower() or 'no forge' in combined.lower()
    assert 'MAKE_MAGIC_FORGE_HOME' in combined  # actionable: names the override.
    assert 'Traceback' not in combined  # graceful — no raw traceback.


def test_doctor_provision_fetches_via_ensure(
    monkeypatch: pytest.MonkeyPatch,
    install: ForgeInstall,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`doctor --provision` fetches via ensure() (not the read-only resolve)."""

    def _resolve_raises(**_: object) -> ForgeInstall:
        raise ForgeUnavailableError('No Forge install found.')

    called: dict[str, bool] = {}

    def _ensure(**_: object) -> ForgeInstall:
        called['ensure'] = True
        return install

    monkeypatch.setattr(forge_runtime, 'resolve', _resolve_raises)  # read-only path would fail…
    monkeypatch.setattr(forge_runtime, 'ensure', _ensure)  # …but --provision fetches (via the engine).
    monkeypatch.setattr(sim_run, 'derive_pool_size', lambda **_: 4)
    monkeypatch.setattr(sim_run, 'free_ram_gib', lambda: 12.5)
    monkeypatch.setattr(sim_run, 'free_disk_gib', lambda: 88.0)

    sim_run.main(['doctor', '--provision'])  # no SystemExit -> exit 0.

    assert called.get('ensure') is True
    out = capsys.readouterr().out
    assert 'available' in out.lower()
    assert 'provisioned' in out.lower()


def test_doctor_optin_engine_absent_is_informational_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
    install: ForgeInstall,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """doctor loops EVERY registered engine: an available Forge (the DEFAULT) + an
    unavailable OPT-IN engine → both reported, but exit 0 (only a DEFAULT-engine
    failure fails the exit code). The opt-in engine gets its OWN message, NOT
    Forge's ~350MB/MAKE_MAGIC_FORGE_HOME how-to."""
    import types

    from pipeline.sim import engine as engine_mod
    from pipeline.sim.engine import EngineCapabilities, EngineUnavailableError

    caps = EngineCapabilities(
        has_hand_visibility=False,
        has_counter_metrics=True,
        expected_nondecisive_rate=0.1,
        reliability_note='fake',
        kill_attribution='combat_generic',
    )

    def _resolve_raises(*, provision: bool, data_dir: object = None) -> object:
        raise EngineUnavailableError('zzfake needs its own bootstrap (this is the fake how-to).')

    fake = types.SimpleNamespace(name='zzfake', capabilities=lambda: caps, resolve=_resolve_raises)
    monkeypatch.setitem(engine_mod._REGISTRY, 'zzfake', fake)  # type: ignore[arg-type]
    monkeypatch.setattr(forge_runtime, 'resolve', lambda **_: install)  # forge (default) available.
    monkeypatch.setattr(sim_run, 'derive_pool_size', lambda **_: 4)
    monkeypatch.setattr(sim_run, 'free_ram_gib', lambda: 12.5)
    monkeypatch.setattr(sim_run, 'free_disk_gib', lambda: 88.0)

    sim_run.main(['doctor'])  # default forge available -> no SystemExit (exit 0).

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert 'forge: available' in captured.out  # the default engine reported.
    assert 'zzfake: NOT AVAILABLE' in combined  # the opt-in engine reported (informational).
    assert 'this is the fake how-to' in combined  # its own message surfaced.
    # MINOR-2: the Forge-specific provision advice must NOT be printed for zzfake.
    assert '350MB' not in combined and 'MAKE_MAGIC_FORGE_HOME' not in combined
    # MINOR-2: the Forge-specific provision advice must NOT be printed for zzfake.
    assert '350MB' not in combined and 'MAKE_MAGIC_FORGE_HOME' not in combined


def test_match_auto_provisions_via_ensure(
    monkeypatch: pytest.MonkeyPatch,
    install: ForgeInstall,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A game verb (`match`) auto-provisions via ensure() when resolve() misses."""
    dck_a, dck_b = tmp_path / 'A.dck', tmp_path / 'B.dck'
    dck_a.write_text('Name=A\n[Main]\n40 Mountain\n')
    dck_b.write_text('Name=B\n[Main]\n40 Plains\n')

    def _resolve_miss(**_: object) -> ForgeInstall:
        raise ForgeUnavailableError('no cached install')

    # read-only resolve misses -> _ensure_forge falls through to the engine's
    # provisioning resolve (forge_runtime.ensure).
    monkeypatch.setattr(forge_runtime, 'resolve', _resolve_miss)
    monkeypatch.setattr(forge_runtime, 'ensure', lambda **_: install)

    def _fake_run_matchup(inst: ForgeInstall, a: tuple[str, str], b: tuple[str, str], **_: object) -> MatchResult:
        assert inst is install  # the provisioned install's handle is threaded through.
        return MatchResult(
            deck_a=a[0],
            deck_b=b[0],
            wins_a=1,
            wins_b=0,
            draws=0,
            per_game=(GameOutcome(winner='a', elapsed_ms=1000),),
            raw_log='(elided)',
        )

    monkeypatch.setattr('pipeline.sim.runner.run_matchup', _fake_run_matchup)

    sim_run.main(['match', str(dck_a), str(dck_b), '-n', '1'])

    out = capsys.readouterr().out
    assert 'A: 1 wins' in out


# --------------------------------------------------------------------------- #
# S2 — first-run ~350MB download consent gate
# --------------------------------------------------------------------------- #


def _miss(**_: object) -> ForgeInstall:
    """A read-only ``forge_runtime.resolve`` that misses (raises ForgeUnavailableError)."""
    raise ForgeUnavailableError('miss')


def test_ensure_forge_returns_cached_without_prompt(monkeypatch: pytest.MonkeyPatch, install: ForgeInstall) -> None:
    """When Forge already resolves, no prompt and no fetch — and the EngineInstall wraps it."""
    monkeypatch.setattr(forge_runtime, 'resolve', lambda **_: install)
    monkeypatch.setattr(forge_runtime, 'ensure', lambda **_: pytest.fail('must not fetch when cached'))
    monkeypatch.setattr('builtins.input', lambda _p: pytest.fail('must not prompt when cached'))
    result = sim_run._ensure_engine(sim_run.get_engine('forge'))
    assert isinstance(result, EngineInstall)
    assert result.handle is install
    # M3: the version now folds the sim-AI harness identity in.
    assert result.version.startswith(f'{FORGE_VERSION}+simai-')


def test_ensure_forge_non_interactive_auto_proceeds(monkeypatch: pytest.MonkeyPatch, install: ForgeInstall) -> None:
    """S2: non-interactive stdin (agent/CI) fetches WITHOUT prompting."""
    monkeypatch.setattr(forge_runtime, 'resolve', _miss)
    monkeypatch.setattr(forge_runtime, 'ensure', lambda **_: install)
    monkeypatch.setattr('pipeline.sim.run.sys.stdin.isatty', lambda: False)
    monkeypatch.setattr('builtins.input', lambda _p: pytest.fail('non-interactive must not prompt'))
    assert sim_run._ensure_engine(sim_run.get_engine('forge')).handle is install


def test_ensure_forge_tty_decline_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """S2: an interactive user answering 'n' aborts with a clean ForgeUnavailableError."""
    monkeypatch.setattr(forge_runtime, 'resolve', _miss)
    monkeypatch.setattr(forge_runtime, 'ensure', lambda **_: pytest.fail('declined download must not fetch'))
    monkeypatch.setattr('pipeline.sim.run.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _p: 'n')
    with pytest.raises(ForgeUnavailableError, match='declined'):
        sim_run._ensure_engine(sim_run.get_engine('forge'))


def test_ensure_forge_tty_accept_fetches(monkeypatch: pytest.MonkeyPatch, install: ForgeInstall) -> None:
    """S2: an interactive user answering 'y' proceeds with the fetch."""
    monkeypatch.setattr(forge_runtime, 'resolve', _miss)
    monkeypatch.setattr(forge_runtime, 'ensure', lambda **_: install)
    monkeypatch.setattr('pipeline.sim.run.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _p: 'y')
    assert sim_run._ensure_engine(sim_run.get_engine('forge')).handle is install


def test_ensure_forge_yes_flag_skips_prompt(monkeypatch: pytest.MonkeyPatch, install: ForgeInstall) -> None:
    """S2: --yes fetches without prompting even on an interactive TTY."""
    monkeypatch.setattr(forge_runtime, 'resolve', _miss)
    monkeypatch.setattr(forge_runtime, 'ensure', lambda **_: install)
    monkeypatch.setattr('pipeline.sim.run.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _p: pytest.fail('--yes must not prompt'))
    assert sim_run._ensure_engine(sim_run.get_engine('forge'), assume_yes=True).handle is install


# --------------------------------------------------------------------------- #
# deck-arg resolution: .dck path vs Airtable name
# --------------------------------------------------------------------------- #


def test_resolve_deck_arg_dck_file(tmp_path: Path) -> None:
    """A ``.dck`` path resolves off disk (no store); ``deck`` is None (no oracle info)."""
    dck = tmp_path / 'FromDisk.dck'
    dck.write_text('[metadata]\nName=FromDisk\n')
    resolved = sim_run._resolve_deck_arg(str(dck))
    assert resolved.name == 'FromDisk'
    assert 'Name=FromDisk' in resolved.text
    assert resolved.ref == ('FromDisk', resolved.text)
    assert resolved.deck is None  # a raw .dck carries no hydrated Deck


def test_resolve_deck_arg_airtable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-path arg resolves via the store: get_deck -> ForgeDckExporter, Deck kept."""
    from pipeline.contracts import Deck, DeckCard

    deck = Deck(name='Goblins', format='constructed', cards=[DeckCard(name='Mountain', quantity=20)])

    class _FakeStore:
        def get_deck(self, name: str) -> Deck:
            assert name == 'Goblins'
            return deck

    monkeypatch.setattr(sim_run, 'get_store', lambda **_: _FakeStore())

    resolved = sim_run._resolve_deck_arg('Goblins')
    assert resolved.name == 'Goblins'
    assert 'Mountain' in resolved.text  # rendered via the Forge exporter.
    assert resolved.deck is deck  # the hydrated Deck is kept for validation.


def test_resolve_deck_arg_prefers_store_for_bareword(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bareword that is NOT a file and lacks .dck goes to the store, not disk."""
    from pipeline.contracts import Deck

    called: dict[str, object] = {}

    class _FakeStore:
        def get_deck(self, name: str) -> Deck:
            called['name'] = name
            return Deck(name=name, format='constructed')

    monkeypatch.setattr(sim_run, 'get_store', lambda **_: _FakeStore())
    sim_run._resolve_deck_arg('Some Deck Name')
    assert called['name'] == 'Some Deck Name'


# --------------------------------------------------------------------------- #
# `log` verb — forensic per-game log retrieval (real tmp store, no Forge).
# --------------------------------------------------------------------------- #


def _seed_matchup(
    data_root: Path, dck_a_text: str, dck_b_text: str, *, seed: int, n_games: int, forge_version: str = '2.0.13'
) -> str:
    """Store a matchup + a real multi-game log into a tmp DuckDB; return its key."""
    from pipeline.sim import store as sim_store
    from pipeline.sim.runner import GameOutcome, MatchResult
    from pipeline.sim.telemetry import GameFeatures

    key = sim_store.matchup_key(
        dck_a_text,
        dck_b_text,
        seed=seed,
        n_games=n_games,
        fmt='constructed',
        engine='forge',
        engine_version=forge_version,
    )
    meta = sim_store.MatchupMeta(
        deck_a_hash=sim_store.deck_hash(dck_a_text),
        deck_b_hash=sim_store.deck_hash(dck_b_text),
        seed=seed,
        n_games=n_games,
        format='constructed',
        engine='forge',
        engine_version=forge_version,
    )
    log = '\n'.join(
        ['Simulation mode']
        + [
            line
            for g in range(1, n_games + 1)
            for line in (
                f'Turn: Turn 1 (Ai(1)-A)  [game {g} marker]',
                f'Game Result: Game {g} ended in {g * 1000} ms. Ai(1)-A has won!',
            )
        ]
    )
    result = MatchResult(
        deck_a='A',
        deck_b='B',
        wins_a=n_games,
        wins_b=0,
        draws=0,
        per_game=tuple(GameOutcome(winner='a', elapsed_ms=1000) for _ in range(n_games)),
        raw_log=log,
    )
    feats = [
        GameFeatures(
            winner='a',
            kill_turn=5,
            win_margin_life=10,
            wincon='combat',
            mulligans_a=0,
            mulligans_b=0,
            game_length_ms=1000,
            lands_by_turn_a=[],
            lands_by_turn_b=[],
        )
        for _ in range(n_games)
    ]
    sim_store.store_matchup(key, meta, result, feats)
    return key


def test_log_lists_games_for_single_matchup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pipeline import store

    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))
    a, b = tmp_path / 'A.dck', tmp_path / 'B.dck'
    a.write_text('Name=A\n[Main]\n4 Forest\n')
    b.write_text('Name=B\n[Main]\n4 Plains\n')
    _seed_matchup(tmp_path, a.read_text(), b.read_text(), seed=42, n_games=2)

    sim_run.main(['log', str(a), str(b)])
    out = capsys.readouterr().out
    assert 'seed=42' in out
    assert '[0]' in out and '[1]' in out  # per-game index listing
    assert 'winner=a' in out


def test_log_prints_single_game_full_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pipeline import store

    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))
    a, b = tmp_path / 'A.dck', tmp_path / 'B.dck'
    a.write_text('Name=A\n[Main]\n4 Forest\n')
    b.write_text('Name=B\n[Main]\n4 Plains\n')
    _seed_matchup(tmp_path, a.read_text(), b.read_text(), seed=42, n_games=2)

    sim_run.main(['log', str(a), str(b), '--game', '1'])
    out = capsys.readouterr().out
    assert '[game 2 marker]' in out
    assert 'Game Result: Game 2 ended' in out
    assert '[game 1 marker]' not in out


def test_log_unknown_matchup_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pipeline import store

    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))
    a, b = tmp_path / 'A.dck', tmp_path / 'B.dck'
    a.write_text('Name=A\n[Main]\n4 Forest\n')
    b.write_text('Name=B\n[Main]\n4 Plains\n')  # nothing stored

    with pytest.raises(SystemExit) as exc:
        sim_run.main(['log', str(a), str(b)])
    assert exc.value.code == 1
    assert 'no stored matchup' in capsys.readouterr().err


def test_log_multi_match_with_game_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two runs of the SAME pair (differing only by seed) + --game -> narrow, exit 1."""
    from pipeline import store

    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))
    a, b = tmp_path / 'A.dck', tmp_path / 'B.dck'
    a.write_text('Name=A\n[Main]\n4 Forest\n')
    b.write_text('Name=B\n[Main]\n4 Plains\n')
    _seed_matchup(tmp_path, a.read_text(), b.read_text(), seed=1, n_games=2)
    _seed_matchup(tmp_path, a.read_text(), b.read_text(), seed=2, n_games=2)

    with pytest.raises(SystemExit) as exc:
        sim_run.main(['log', str(a), str(b), '--game', '0'])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert 'matchups match' in err and 'narrow with' in err


def test_log_game_index_out_of_range_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--game N`` beyond the stored game count is an actionable CollectionError."""
    from pipeline import store

    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))
    a, b = tmp_path / 'A.dck', tmp_path / 'B.dck'
    a.write_text('Name=A\n[Main]\n4 Forest\n')
    b.write_text('Name=B\n[Main]\n4 Plains\n')
    _seed_matchup(tmp_path, a.read_text(), b.read_text(), seed=42, n_games=2)

    with pytest.raises(SystemExit) as exc:
        sim_run.main(['log', str(a), str(b), '--game', '5'])  # only games 0,1 exist
    assert exc.value.code == 1
    assert 'no log for game 5' in capsys.readouterr().err


def _seed_matchup_with_clockout(data_root: Path, dck_a_text: str, dck_b_text: str, *, seed: int) -> str:
    """Store a 2-game run where ONE game clocked out (its log is NOT retained).

    ``n_games`` (total that ran) is 2 but only the ONE decisive game has a stored
    feature row + retained log — mirrors store.py discarding clockout logs. Used to
    prove the ``log`` index UX reports the RETRIEVABLE count, not the total (R3-3).
    """
    from pipeline.sim import store as sim_store
    from pipeline.sim.runner import GameOutcome, MatchResult
    from pipeline.sim.telemetry import GameFeatures

    key = sim_store.matchup_key(
        dck_a_text, dck_b_text, seed=seed, n_games=2, fmt='constructed', engine='forge', engine_version='2.0.13'
    )
    meta = sim_store.MatchupMeta(
        deck_a_hash=sim_store.deck_hash(dck_a_text),
        deck_b_hash=sim_store.deck_hash(dck_b_text),
        seed=seed,
        n_games=2,
        format='constructed',
        engine='forge',
        engine_version='2.0.13',
    )
    # Game 1 clocks out (marker present) → non-decisive, no retained log; game 2 decides.
    log = '\n'.join(
        [
            'Simulation mode',
            'Stopping slow match as draw',
            'Game Result: Game 1 ended in 90000 ms. Ai(1)-A has won!',
            'Turn: Turn 1 (Ai(1)-A)  [game 2 marker]',
            'Game Result: Game 2 ended in 2000 ms. Ai(1)-A has won!',
        ]
    )
    result = MatchResult(
        deck_a='A',
        deck_b='B',
        wins_a=1,
        wins_b=0,
        draws=1,
        per_game=(GameOutcome(winner='draw', elapsed_ms=90000), GameOutcome(winner='a', elapsed_ms=2000)),
        raw_log=log,
    )
    # ONE feature row (the decisive game) — 1:1 with the ONE retained (non-clockout) log.
    feats = [
        GameFeatures(
            winner='a',
            kill_turn=5,
            win_margin_life=10,
            wincon='combat',
            mulligans_a=0,
            mulligans_b=0,
            game_length_ms=2000,
            lands_by_turn_a=[],
            lands_by_turn_b=[],
        )
    ]
    sim_store.store_matchup(key, meta, result, feats)
    return key


def test_log_index_reports_retrievable_not_total_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The index header + out-of-range error reflect the STORED (retrievable) game
    count, not the TOTAL that ran — clockout logs are discarded, so ``n_games``
    overstates what ``--game N`` can address (R3-3)."""
    from pipeline import store

    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))
    a, b = tmp_path / 'A.dck', tmp_path / 'B.dck'
    a.write_text('Name=A\n[Main]\n4 Forest\n')
    b.write_text('Name=B\n[Main]\n4 Plains\n')
    _seed_matchup_with_clockout(tmp_path, a.read_text(), b.read_text(), seed=42)

    # Index header: 1 of 2 retrievable (one game clocked out).
    sim_run.main(['log', str(a), str(b)])
    out = capsys.readouterr().out
    assert '1 of 2 game(s) retrievable' in out

    # Out-of-range --game 1 (only index 0 is retrievable) names the STORED count.
    with pytest.raises(SystemExit) as exc:
        sim_run.main(['log', str(a), str(b), '--game', '1'])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert 'no log for game 1' in err
    assert '1 retrievable game log(s)' in err
    assert '2 game(s) ran' in err  # the total is still surfaced for context


def test_log_forge_filter_disambiguates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two runs differing ONLY by forge_version -> --forge narrows to one and reads it."""
    from pipeline import store

    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))
    a, b = tmp_path / 'A.dck', tmp_path / 'B.dck'
    a.write_text('Name=A\n[Main]\n4 Forest\n')
    b.write_text('Name=B\n[Main]\n4 Plains\n')
    # Same seed/games/format; only the Forge version differs.
    _seed_matchup(tmp_path, a.read_text(), b.read_text(), seed=42, n_games=2, forge_version='2.0.13')
    _seed_matchup(tmp_path, a.read_text(), b.read_text(), seed=42, n_games=2, forge_version='2.0.14')

    # Without --forge, --game is ambiguous (both share seed/games/format).
    with pytest.raises(SystemExit):
        sim_run.main(['log', str(a), str(b), '--game', '0'])
    ambiguous = capsys.readouterr()
    assert 'matchups match' in ambiguous.err  # guidance header on stderr
    assert 'key=' in ambiguous.out  # key prefix surfaced (rows) to disambiguate

    # With --forge, it narrows to one and prints that game's log.
    sim_run.main(['log', str(a), str(b), '--forge', '2.0.14', '--game', '1'])
    out = capsys.readouterr().out
    assert '[game 2 marker]' in out


# --------------------------------------------------------------------------- #
# Piloting block in the deck printer (_print_sim_result / _print_piloting).
# --------------------------------------------------------------------------- #


def _sim_result_with_piloting(piloting: object) -> SimResult:
    """A ``SimResult`` carrying a piloting profile (or None) for printer tests."""
    base = _sim_result()
    from dataclasses import replace

    return replace(base, piloting=piloting)  # type: ignore[type-var]


def test_print_piloting_available_block(capsys: pytest.CaptureFixture[str]) -> None:
    """An AVAILABLE piloting profile prints the fire-rate block + the one-line frame."""
    from pipeline.sim.telemetry import PilotingProfile

    prof = PilotingProfile(
        counter_opps=7,
        counter_casts=0,
        counter_fire=0.0,
        counter_ci=(0.0, 0.35),
        removal_opps=2,
        removal_casts=2,
        removal_fire=1.0,
        removal_ci=(0.34, 1.0),
        interaction_stranded_per_game=0.33,
        games=3,
    )
    sim_run._print_sim_result(_sim_result_with_piloting(prof))
    out = capsys.readouterr().out
    assert 'piloting:' in out
    assert 'counter fire-rate' in out
    assert '0.0%' in out  # 0/7 sim-AI signature
    assert '(0/7 opps)' in out
    assert 'removal fire-rate' in out
    assert 'stranded/game' in out
    assert 'how often the AI cast it' in out  # the one-line frame
    # cards_total defaults to 0 here -> no coverage line (unpopulated).
    assert 'classification coverage' not in out


def test_print_piloting_coverage_line_names_blind_spots(capsys: pytest.CaptureFixture[str]) -> None:
    """A populated coverage surfaces how much of the deck was classified + names the blind spots."""
    from pipeline.sim.telemetry import PilotingProfile

    prof = PilotingProfile(
        counter_opps=0,
        counter_casts=0,
        counter_fire=None,
        counter_ci=None,
        removal_opps=2,
        removal_casts=1,
        removal_fire=0.5,
        removal_ci=(0.1, 0.9),
        interaction_stranded_per_game=0.0,
        games=4,
        cards_total=37,
        cards_classified=35,
        uncategorized=('Spoiler Card A', 'Spoiler Card B'),
    )
    sim_run._print_sim_result(_sim_result_with_piloting(prof))
    out = capsys.readouterr().out
    assert 'classification coverage: 35/37 non-land cards carry otags' in out
    assert 'Spoiler Card A' in out and 'Spoiler Card B' in out  # blind spots named


def test_print_piloting_unavailable_is_one_honest_line(capsys: pytest.CaptureFixture[str]) -> None:
    """An UNAVAILABLE profile prints ONE honest line naming the reason — no 0/0."""
    from pipeline.sim.telemetry import unavailable_piloting

    prof = unavailable_piloting('otag lake not populated — run the otag build to enable piloting metrics')
    sim_run._print_sim_result(_sim_result_with_piloting(prof))
    out = capsys.readouterr().out
    assert 'piloting:' in out
    assert 'unavailable' in out
    assert 'otag lake not populated' in out
    # The honest line must NOT masquerade as a real zero fire-rate.
    assert 'fire-rate' not in out
    assert 'opps)' not in out


def test_print_piloting_none_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """No piloting profile (engine lacks hand visibility) -> no piloting block."""
    sim_run._print_sim_result(_sim_result_with_piloting(None))
    out = capsys.readouterr().out
    assert 'piloting:' not in out
