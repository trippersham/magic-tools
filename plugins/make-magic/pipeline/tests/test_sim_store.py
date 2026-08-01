"""TDD tests for the sim matchup cache + telemetry feature store (Phase 5).

Everything here is OFFLINE: a tmp data dir (via the MAKE_MAGIC_DATA_DIR env
override) backing a DuckDB file. No Forge, no network, no Airtable. We hand the
store hand-built :class:`MatchResult` / :class:`GameFeatures` shapes (exactly
what Phases 2-3 produce) and assert the round-trip + aggregate queries.

Coverage:
    - miss -> store -> hit: get_cached round-trips the win tally + every
      GameFeatures field (incl. the INTEGER[] land curves).
    - invalidation: a changed .dck text OR a bumped forge_version yields a
      different matchup_key -> a cache miss.
    - feature_stats: several games aggregate to the correct avg kill_turn +
      wincon counts, filterable by format.
    - idempotent tables + re-store: storing the same key twice replaces the
      feature rows (no duplicates) rather than appending.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store
from pipeline.sim import store as sim_store
from pipeline.sim.runner import GameOutcome, MatchResult
from pipeline.sim.telemetry import GameFeatures

DCK_A = 'Name Aggro\n[Main]\n4 Lightning Bolt\n'
DCK_B = 'Name Control\n[Main]\n4 Counterspell\n'


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at an isolated tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _match_result(*, wins_a: int = 2, wins_b: int = 1, draws: int = 0) -> MatchResult:
    per_game = (
        *(GameOutcome(winner='a', elapsed_ms=1000) for _ in range(wins_a)),
        *(GameOutcome(winner='b', elapsed_ms=2000) for _ in range(wins_b)),
        *(GameOutcome(winner='draw', elapsed_ms=3000) for _ in range(draws)),
    )
    return MatchResult(
        deck_a='Aggro',
        deck_b='Control',
        wins_a=wins_a,
        wins_b=wins_b,
        draws=draws,
        per_game=per_game,
        raw_log='(elided)',
    )


def _features(
    *,
    winner: str = 'a',
    kill_turn: int | None = 6,
    wincon: str | None = 'combat',
    lands_a: list[int] | None = None,
    lands_b: list[int] | None = None,
) -> GameFeatures:
    return GameFeatures(
        winner=winner,
        kill_turn=kill_turn,
        win_margin_life=12,
        wincon=wincon,
        mulligans_a=1,
        mulligans_b=0,
        game_length_ms=1500,
        lands_by_turn_a=[0, 1, 2, 3] if lands_a is None else lands_a,
        lands_by_turn_b=[0, 1, 1, 2] if lands_b is None else lands_b,
    )


def _meta() -> sim_store.MatchupMeta:
    return sim_store.MatchupMeta(
        deck_a_hash=sim_store.deck_hash(DCK_A),
        deck_b_hash=sim_store.deck_hash(DCK_B),
        seed=42,
        n_games=3,
        format='constructed',
        forge_version='2.0.13',
    )


# --------------------------------------------------------------------------- #
# key derivation
# --------------------------------------------------------------------------- #


def test_matchup_key_is_stable_and_order_sensitive() -> None:
    kwargs = {'seed': 42, 'n_games': 3, 'fmt': 'constructed', 'forge_version': '2.0.13'}
    k1 = sim_store.matchup_key(DCK_A, DCK_B, **kwargs)
    k2 = sim_store.matchup_key(DCK_A, DCK_B, **kwargs)
    assert k1 == k2  # deterministic
    assert isinstance(k1, str) and len(k1) == 64  # sha256 hex

    swapped = sim_store.matchup_key(DCK_B, DCK_A, **kwargs)
    assert swapped != k1  # order-sensitive on (a, b)


# --------------------------------------------------------------------------- #
# miss -> store -> hit round-trip
# --------------------------------------------------------------------------- #


def test_miss_then_store_then_hit_roundtrips(data_dir: Path) -> None:
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=42, n_games=3, fmt='constructed', forge_version='2.0.13')
    assert sim_store.get_cached(key) is None  # miss on a fresh db

    result = _match_result(wins_a=2, wins_b=1)
    features = [
        _features(winner='a', kill_turn=6, wincon='combat'),
        _features(winner='a', kill_turn=8, wincon='burn', lands_a=[0, 1, 2]),
        _features(winner='b', kill_turn=10, wincon='mill', lands_b=[0, 1, 2, 3, 4]),
    ]
    sim_store.store_matchup(key, _meta(), result, features)

    cached = sim_store.get_cached(key)
    assert cached is not None
    assert (cached.wins_a, cached.wins_b, cached.draws) == (2, 1, 0)
    assert len(cached.features) == 3

    # Ordered by game_index; every field round-trips (incl. land curves).
    assert [f.winner for f in cached.features] == ['a', 'a', 'b']
    assert [f.kill_turn for f in cached.features] == [6, 8, 10]
    assert [f.wincon for f in cached.features] == ['combat', 'burn', 'mill']
    assert cached.features[0].lands_by_turn_a == [0, 1, 2, 3]
    assert cached.features[1].lands_by_turn_a == [0, 1, 2]
    assert cached.features[2].lands_by_turn_b == [0, 1, 2, 3, 4]
    assert cached.features[0].mulligans_a == 1
    assert cached.features[0].win_margin_life == 12
    assert cached.features[0].game_length_ms == 1500


def test_none_scalars_and_empty_curves_roundtrip(data_dir: Path) -> None:
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=1, n_games=1, fmt='constructed', forge_version='2.0.13')
    result = _match_result(wins_a=0, wins_b=0, draws=1)
    features = [
        GameFeatures(
            winner='draw',
            kill_turn=None,
            win_margin_life=None,
            wincon=None,
            mulligans_a=0,
            mulligans_b=0,
            game_length_ms=None,
            lands_by_turn_a=[],
            lands_by_turn_b=[],
        )
    ]
    sim_store.store_matchup(key, _meta(), result, features)

    cached = sim_store.get_cached(key)
    assert cached is not None
    got = cached.features[0]
    assert got.kill_turn is None
    assert got.win_margin_life is None
    assert got.wincon is None
    assert got.game_length_ms is None
    assert got.lands_by_turn_a == []
    assert got.lands_by_turn_b == []


# --------------------------------------------------------------------------- #
# invalidation: deck edit OR forge bump -> different key -> miss
# --------------------------------------------------------------------------- #


def test_changed_deck_text_changes_key(data_dir: Path) -> None:
    kwargs = {'seed': 42, 'n_games': 3, 'fmt': 'constructed', 'forge_version': '2.0.13'}
    base = sim_store.matchup_key(DCK_A, DCK_B, **kwargs)

    edited_a = DCK_A + '4 Shock\n'  # a single card change
    edited_key = sim_store.matchup_key(edited_a, DCK_B, **kwargs)
    assert edited_key != base

    # The base matchup is cached; the edited deck is a MISS.
    sim_store.store_matchup(base, _meta(), _match_result(), [_features()])
    assert sim_store.get_cached(base) is not None
    assert sim_store.get_cached(edited_key) is None


def test_bumped_forge_version_changes_key(data_dir: Path) -> None:
    base = sim_store.matchup_key(DCK_A, DCK_B, seed=42, n_games=3, fmt='constructed', forge_version='2.0.13')
    bumped = sim_store.matchup_key(DCK_A, DCK_B, seed=42, n_games=3, fmt='constructed', forge_version='2.0.14')
    assert bumped != base

    sim_store.store_matchup(base, _meta(), _match_result(), [_features()])
    assert sim_store.get_cached(base) is not None
    assert sim_store.get_cached(bumped) is None


# --------------------------------------------------------------------------- #
# feature_stats — the demonstration aggregate query
# --------------------------------------------------------------------------- #


def test_feature_stats_aggregates(data_dir: Path) -> None:
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=42, n_games=4, fmt='constructed', forge_version='2.0.13')
    result = _match_result(wins_a=3, wins_b=1)
    features = [
        _features(winner='a', kill_turn=4, wincon='combat'),
        _features(winner='a', kill_turn=6, wincon='combat'),
        _features(winner='a', kill_turn=8, wincon='burn'),
        _features(winner='b', kill_turn=10, wincon='mill'),
    ]
    sim_store.store_matchup(key, _meta(), result, features)

    stats = sim_store.feature_stats()
    assert stats['games'] == 4
    assert stats['avg_kill_turn'] == pytest.approx(7.0)  # (4+6+8+10)/4
    assert stats['median_kill_turn'] == pytest.approx(7.0)  # median of 4,6,8,10
    assert stats['wincon_counts'] == {'combat': 2, 'burn': 1, 'mill': 1}


def test_feature_stats_filters_by_format(data_dir: Path) -> None:
    con_key = sim_store.matchup_key(DCK_A, DCK_B, seed=1, n_games=1, fmt='constructed', forge_version='2.0.13')
    cmd_key = sim_store.matchup_key(DCK_A, DCK_B, seed=1, n_games=1, fmt='commander', forge_version='2.0.13')
    con_meta = sim_store.MatchupMeta(
        deck_a_hash='x',
        deck_b_hash='y',
        seed=1,
        n_games=1,
        format='constructed',
        forge_version='2.0.13',
    )
    cmd_meta = sim_store.MatchupMeta(
        deck_a_hash='x',
        deck_b_hash='y',
        seed=1,
        n_games=1,
        format='commander',
        forge_version='2.0.13',
    )
    sim_store.store_matchup(
        con_key,
        con_meta,
        _match_result(wins_a=1, wins_b=0),
        [_features(kill_turn=5, wincon='combat')],
    )
    sim_store.store_matchup(
        cmd_key,
        cmd_meta,
        _match_result(wins_a=1, wins_b=0),
        [_features(kill_turn=15, wincon='other')],
    )

    con_stats = sim_store.feature_stats(format='constructed')
    assert con_stats['games'] == 1
    assert con_stats['avg_kill_turn'] == pytest.approx(5.0)
    assert con_stats['wincon_counts'] == {'combat': 1}

    all_stats = sim_store.feature_stats()
    assert all_stats['games'] == 2


# --------------------------------------------------------------------------- #
# idempotent tables + re-store replaces (no duplicate feature rows)
# --------------------------------------------------------------------------- #


def test_restore_replaces_no_duplicate_feature_rows(data_dir: Path) -> None:
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=42, n_games=3, fmt='constructed', forge_version='2.0.13')
    sim_store.store_matchup(key, _meta(), _match_result(wins_a=2, wins_b=1), [_features()] * 3)

    # Re-store the SAME key with a different tally + a single feature row.
    sim_store.store_matchup(key, _meta(), _match_result(wins_a=1, wins_b=2), [_features(winner='b')])

    cached = sim_store.get_cached(key)
    assert cached is not None
    assert (cached.wins_a, cached.wins_b) == (1, 2)  # matchup row upserted
    assert len(cached.features) == 1  # feature rows REPLACED, not appended

    # And the raw feature table has exactly one row for this key (no dupes).
    with store.connect() as conn:
        count = conn.execute('SELECT count(*) FROM sim_game_features WHERE matchup_key = ?', [key]).fetchone()
        assert count is not None and count[0] == 1


def test_tables_created_idempotently(data_dir: Path) -> None:
    """Two independent store operations against a fresh db do not clash on DDL."""
    key1 = sim_store.matchup_key(DCK_A, DCK_B, seed=1, n_games=1, fmt='constructed', forge_version='2.0.13')
    key2 = sim_store.matchup_key(DCK_A, DCK_B, seed=2, n_games=1, fmt='constructed', forge_version='2.0.13')
    sim_store.store_matchup(key1, _meta(), _match_result(), [_features()])
    sim_store.store_matchup(key2, _meta(), _match_result(), [_features()])
    assert sim_store.get_cached(key1) is not None
    assert sim_store.get_cached(key2) is not None


# --------------------------------------------------------------------------- #
# per-game raw-log retention (forensic replay) — sim_game_logs
# --------------------------------------------------------------------------- #


def _multigame_log(n: int) -> str:
    """A realistic multi-game verbose log: n games, each ending in a Game Result.

    Preamble (card-DB banner) folds into game 1; each game body carries a unique
    marker line so a slice can be attributed to its game.
    """
    lines = ['Read cards: 33319 files', 'Simulation mode']
    for g in range(1, n + 1):
        winner = 'Ai(1)-Aggro' if g % 2 else 'Ai(2)-Control'
        lines += [
            f'Turn: Turn 1 (Ai(1)-Aggro)  [game {g} marker]',
            f'Game Result: Game {g} ended in {1000 * g} ms. {winner} has won!',
        ]
    return '\n'.join(lines)


def test_per_game_logs_persist_and_slice(data_dir: Path) -> None:
    """store_matchup slices raw_log per game; get_game_logs returns them in order."""
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=42, n_games=2, fmt='constructed', forge_version='2.0.13')
    result = MatchResult(
        deck_a='Aggro',
        deck_b='Control',
        wins_a=1,
        wins_b=1,
        draws=0,
        per_game=(GameOutcome(winner='a', elapsed_ms=1000), GameOutcome(winner='b', elapsed_ms=2000)),
        raw_log=_multigame_log(2),
    )
    features = [_features(winner='a'), _features(winner='b')]
    sim_store.store_matchup(key, _meta(), result, features)

    logs = sim_store.get_game_logs(key)
    assert len(logs) == 2
    # Segment 0 carries the preamble + game-1 marker + its Game Result terminator.
    assert 'Simulation mode' in logs[0]
    assert '[game 1 marker]' in logs[0]
    assert 'Game Result: Game 1 ended' in logs[0]
    # Segment 1 is game 2 only (preamble already consumed).
    assert '[game 2 marker]' in logs[1]
    assert 'Game Result: Game 2 ended' in logs[1]
    assert '[game 1 marker]' not in logs[1]


def test_get_game_logs_single_index(data_dir: Path) -> None:
    """A game_index filter returns just that game's log."""
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=7, n_games=3, fmt='constructed', forge_version='2.0.13')
    result = MatchResult(
        deck_a='Aggro',
        deck_b='Control',
        wins_a=2,
        wins_b=1,
        draws=0,
        per_game=tuple(GameOutcome(winner='a', elapsed_ms=1000) for _ in range(3)),
        raw_log=_multigame_log(3),
    )
    sim_store.store_matchup(key, _meta(), result, [_features()] * 3)

    only = sim_store.get_game_logs(key, game_index=1)
    assert len(only) == 1
    assert '[game 2 marker]' in only[0]


def test_game_logs_align_with_feature_index(data_dir: Path) -> None:
    """Log game_index lines up 1:1 with feature game_index (shared split_games)."""
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=9, n_games=3, fmt='constructed', forge_version='2.0.13')
    result = MatchResult(
        deck_a='Aggro',
        deck_b='Control',
        wins_a=2,
        wins_b=1,
        draws=0,
        per_game=tuple(GameOutcome(winner='a', elapsed_ms=1000) for _ in range(3)),
        raw_log=_multigame_log(3),
    )
    sim_store.store_matchup(key, _meta(), result, [_features()] * 3)

    cached = sim_store.get_cached(key)
    logs = sim_store.get_game_logs(key)
    assert cached is not None
    assert len(logs) == len(cached.features) == 3
    for i, log in enumerate(logs, start=1):
        assert f'[game {i} marker]' in log


def test_restore_replaces_game_logs(data_dir: Path) -> None:
    """Re-storing a key REPLACES its log rows (no append), like the feature rows."""
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=42, n_games=3, fmt='constructed', forge_version='2.0.13')
    first = MatchResult(
        deck_a='Aggro',
        deck_b='Control',
        wins_a=2,
        wins_b=1,
        draws=0,
        per_game=tuple(GameOutcome(winner='a', elapsed_ms=1000) for _ in range(3)),
        raw_log=_multigame_log(3),
    )
    sim_store.store_matchup(key, _meta(), first, [_features()] * 3)

    second = MatchResult(
        deck_a='Aggro',
        deck_b='Control',
        wins_a=1,
        wins_b=0,
        draws=0,
        per_game=(GameOutcome(winner='a', elapsed_ms=1000),),
        raw_log=_multigame_log(1),
    )
    sim_store.store_matchup(key, _meta(), second, [_features()])

    logs = sim_store.get_game_logs(key)
    assert len(logs) == 1  # replaced, not appended (3 -> 1)
    with store.connect() as conn:
        count = conn.execute('SELECT count(*) FROM sim_game_logs WHERE matchup_key = ?', [key]).fetchone()
        assert count is not None and count[0] == 1


def test_get_game_logs_miss_returns_empty(data_dir: Path) -> None:
    """Unknown key (or fresh db) -> [] (never raises)."""
    assert sim_store.get_game_logs('deadbeef') == []


def test_find_matchups_by_deck_pair(data_dir: Path) -> None:
    """find_matchups locates stored runs of a deck pair (offline, by hash)."""
    a_hash, b_hash = sim_store.deck_hash(DCK_A), sim_store.deck_hash(DCK_B)
    k1 = sim_store.matchup_key(DCK_A, DCK_B, seed=1, n_games=2, fmt='constructed', forge_version='2.0.13')
    k2 = sim_store.matchup_key(DCK_A, DCK_B, seed=2, n_games=2, fmt='constructed', forge_version='2.0.13')
    m1 = sim_store.MatchupMeta(
        deck_a_hash=a_hash, deck_b_hash=b_hash, seed=1, n_games=2, format='constructed', forge_version='2.0.13'
    )
    m2 = sim_store.MatchupMeta(
        deck_a_hash=a_hash, deck_b_hash=b_hash, seed=2, n_games=2, format='constructed', forge_version='2.0.13'
    )
    sim_store.store_matchup(k1, m1, _match_result(), [_features()])
    sim_store.store_matchup(k2, m2, _match_result(), [_features()])

    found = sim_store.find_matchups(deck_a_hash=a_hash, deck_b_hash=b_hash)
    assert {m.matchup_key for m in found} == {k1, k2}
    assert {m.seed for m in found} == {1, 2}

    # A different deck pair returns nothing.
    assert sim_store.find_matchups(deck_a_hash='nope', deck_b_hash=b_hash) == []


def test_find_matchups_empty_store(data_dir: Path) -> None:
    """A fresh db yields no matchups (no raise on missing tables)."""
    assert sim_store.find_matchups() == []


def test_elided_log_stores_no_game_logs(data_dir: Path) -> None:
    """A result-less raw_log (no Game Result lines) persists zero log rows, no crash."""
    key = sim_store.matchup_key(DCK_A, DCK_B, seed=1, n_games=1, fmt='constructed', forge_version='2.0.13')
    sim_store.store_matchup(key, _meta(), _match_result(), [_features()])  # raw_log='(elided)'
    assert sim_store.get_game_logs(key) == []
