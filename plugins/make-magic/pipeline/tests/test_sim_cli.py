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

from pipeline.sim import run as sim_run
from pipeline.sim.core import Comparison, OpponentResult, SimResult, TelemetryProfile
from pipeline.sim.forge_runtime import ForgeInstall, ForgeUnavailableError
from pipeline.sim.runner import GameOutcome, MatchResult

# --------------------------------------------------------------------------- #
# Fixtures / builders.
# --------------------------------------------------------------------------- #


def _sim_result(candidate: str = 'Cand', fmt: str = 'constructed') -> SimResult:
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
        total_games=4,
        wins=3,
        losses=1,
        draws=0,
        win_rate=0.75,
        win_rate_ci=(0.3, 0.95),
        per_opponent=per_opp,
        profile=profile,
        cached_matchups=0,
        fresh_matchups=1,
    )


@pytest.fixture()
def install() -> ForgeInstall:
    """A dummy resolved install (paths never touched — resolve is mocked)."""
    return ForgeInstall(forge_dir=Path('/tmp/forge'), jar=Path('/tmp/forge/f.jar'), java=Path('/tmp/java'))


@pytest.fixture()
def mock_resolve(monkeypatch: pytest.MonkeyPatch, install: ForgeInstall) -> ForgeInstall:
    """Patch ``run.resolve`` to return a dummy install (Forge "available")."""
    monkeypatch.setattr(sim_run, 'resolve', lambda **_: install)
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
    dck_a.write_text('[metadata]\nName=A\n')
    dck_b.write_text('[metadata]\nName=B\n')

    seen: dict[str, object] = {}

    def _fake_run_matchup(
        install: ForgeInstall,
        deck_a: tuple[str, str],
        deck_b: tuple[str, str],
        *,
        n: int,
        seed: int,
        fmt: str = 'constructed',
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

    monkeypatch.setattr(sim_run, 'run_matchup', _fake_run_matchup)

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
    dck.write_text('[metadata]\nName=MyDeck\n')

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
    dck.write_text('x')
    seen: dict[str, object] = {}

    def _fake_simulate(deck: object, gauntlet_source: str, **kwargs: object) -> SimResult:
        seen.update(gauntlet_source=gauntlet_source, **kwargs)
        return _sim_result()

    monkeypatch.setattr(sim_run, 'simulate', _fake_simulate)
    sim_run.main(['deck', str(dck)])
    assert seen['gauntlet_source'] == 'curated'
    assert seen['force'] is False


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
    a.write_text('a')
    b.write_text('b')

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


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_available(
    monkeypatch: pytest.MonkeyPatch,
    install: ForgeInstall,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """doctor with a resolvable Forge prints version + pool size + paths, exit 0."""
    monkeypatch.setattr(sim_run, 'resolve', lambda **_: install)
    monkeypatch.setattr(sim_run, 'forge_version', lambda: '2.0.13')
    monkeypatch.setattr(sim_run, 'derive_pool_size', lambda **_: 4)
    monkeypatch.setattr(sim_run, 'free_ram_gib', lambda: 12.5)
    monkeypatch.setattr(sim_run, 'free_disk_gib', lambda: 88.0)

    sim_run.main(['doctor'])  # no SystemExit -> exit 0.

    out = capsys.readouterr().out
    assert '2.0.13' in out
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

    monkeypatch.setattr(sim_run, 'resolve', _raise)
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


# --------------------------------------------------------------------------- #
# deck-arg resolution: .dck path vs Airtable name
# --------------------------------------------------------------------------- #


def test_resolve_deck_arg_dck_file(tmp_path: Path) -> None:
    """A ``.dck`` path resolves to a (name, text) pair straight off disk (no store)."""
    dck = tmp_path / 'FromDisk.dck'
    dck.write_text('[metadata]\nName=FromDisk\n')
    name, text = sim_run._resolve_deck_arg(str(dck))
    assert name == 'FromDisk'
    assert 'Name=FromDisk' in text


def test_resolve_deck_arg_airtable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-path arg resolves via the store: get_deck -> ForgeDckExporter."""
    from pipeline.contracts import Deck, DeckCard

    deck = Deck(name='Goblins', format='constructed', cards=[DeckCard(name='Mountain', quantity=20)])

    class _FakeStore:
        def get_deck(self, name: str) -> Deck:
            assert name == 'Goblins'
            return deck

    monkeypatch.setattr(sim_run, 'get_store', lambda **_: _FakeStore())

    name, text = sim_run._resolve_deck_arg('Goblins')
    assert name == 'Goblins'
    assert 'Mountain' in text  # rendered via the Forge exporter.


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
        dck_a_text, dck_b_text, seed=seed, n_games=n_games, fmt='constructed', forge_version=forge_version
    )
    meta = sim_store.MatchupMeta(
        deck_a_hash=sim_store.deck_hash(dck_a_text),
        deck_b_hash=sim_store.deck_hash(dck_b_text),
        seed=seed,
        n_games=n_games,
        format='constructed',
        forge_version=forge_version,
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
