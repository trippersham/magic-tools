"""TDD tests for the solo-goldfish persistence store (EP4a).

OFFLINE: a tmp data dir (MAKE_MAGIC_DATA_DIR override) backing a DuckDB file. No JVM,
no network. We hand the store a hand-built GoldfishResult-shaped object + a raw solo
log (exactly what the XMage --solo harness prints) and assert the round-trip + the
per-game parse + the aggregate query.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline import store
from pipeline.sim import store as sim_store

DECK = 'Name Aggro\n[Main]\n4 Lightning Bolt\n'

_RAW_LOG = """\
DRIVER_REGISTERED fqcn=org.x.Driver playerId=abc
GOLDFISH GAME 1/3 deck=deckA.txt variant=none killed=true ownKillTurn=6 globalTurn=11 lifeB=-2 ms=1200
MACRO_FIRE_REAL name=Combo turn=6
GOLDFISH GAME 2/3 deck=deckA.txt variant=none killed=false ownKillTurn=NONE globalTurn=39 lifeB=17 ms=3400
GOLDFISH GAME 3/3 deck=deckA.txt variant=none killed=true ownKillTurn=7 globalTurn=13 lifeB=0 ms=1500
MACRO_FIRE_REAL name=Combo turn=7
GOLDFISH SUMMARY (OWN TURNS) deck=deckA.txt variant=none games=3 maxTurn=20 skill=7 kills=2 bricks=1 medianKillsOwn=6.5
"""


@dataclass(frozen=True)
class _FakeGoldfish:
    median_kills_own: float
    games: int
    max_turn: int | None = None
    bricks: int | None = None


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def test_parse_goldfish_games() -> None:
    rows = sim_store.parse_goldfish_games(_RAW_LOG)
    assert len(rows) == 3
    assert rows[0] == sim_store.GoldfishGameRow(own_turn=6, killed=True, global_turn=11, ms=1200)
    assert rows[1].own_turn is None
    assert rows[1].killed is False
    assert rows[2].own_turn == 7


def test_parse_empty_log_no_rows() -> None:
    assert sim_store.parse_goldfish_games('(elided)') == []


def test_store_and_get_roundtrip(data_dir: Path) -> None:
    result = _FakeGoldfish(median_kills_own=6.5, games=3, max_turn=20, bricks=1)
    key = sim_store.persist_goldfish_run(
        DECK,
        driver_fqcn='org.x.Driver',
        alpha=2000,
        fmt='commander',
        games=3,
        result=result,
        raw_log=_RAW_LOG,
        engine='xmage',
    )
    assert key is not None

    rec = sim_store.get_goldfish(key)
    assert rec is not None
    assert rec.median_kills_own == 6.5
    assert rec.games == 3
    assert rec.bricks == 1
    assert rec.max_turn == 20
    assert rec.driver_fqcn == 'org.x.Driver'
    assert rec.alpha == 2000
    assert rec.format == 'commander'
    assert rec.registered is True
    assert rec.fire_count == 2
    assert rec.engine == 'xmage'
    # mean_ms over 1200, 3400, 1500
    assert rec.mean_ms_per_game == pytest.approx((1200 + 3400 + 1500) / 3)
    assert len(rec.per_game) == 3
    assert rec.per_game[0].own_turn == 6
    assert rec.per_game[1].killed is False

    # raw log retained for replay
    assert sim_store.get_goldfish_log(key) == _RAW_LOG


def test_driven_vs_driverless_distinct_keys(data_dir: Path) -> None:
    k_driven = sim_store.goldfish_key(DECK, driver_fqcn='org.x.Driver', alpha=2000, fmt='commander', games=3)
    k_bare = sim_store.goldfish_key(DECK, driver_fqcn=None, alpha=None, fmt='commander', games=3)
    assert k_driven != k_bare


def test_restore_same_key_upserts(data_dir: Path) -> None:
    result = _FakeGoldfish(median_kills_own=6.5, games=3, max_turn=20, bricks=1)
    key = sim_store.persist_goldfish_run(
        DECK, driver_fqcn=None, alpha=None, fmt='commander', games=3,
        result=result, raw_log=_RAW_LOG, engine='xmage',
    )
    assert key is not None
    # Re-store the same content -> upsert, not duplicate.
    sim_store.persist_goldfish_run(
        DECK, driver_fqcn=None, alpha=None, fmt='commander', games=3,
        result=result, raw_log=_RAW_LOG, engine='xmage',
    )
    rec = sim_store.get_goldfish(key)
    assert rec is not None
    assert len(rec.per_game) == 3  # not 6


def test_goldfish_features_aggregate(data_dir: Path) -> None:
    sim_store.persist_goldfish_run(
        DECK, driver_fqcn='org.x.Driver', alpha=2000, fmt='commander', games=3,
        result=_FakeGoldfish(median_kills_own=6.5, games=3, max_turn=20, bricks=1),
        raw_log=_RAW_LOG, engine='xmage',
    )
    sim_store.persist_goldfish_run(
        DECK, driver_fqcn=None, alpha=None, fmt='commander', games=3,
        result=_FakeGoldfish(median_kills_own=8.0, games=3, max_turn=20, bricks=0),
        raw_log=_RAW_LOG, engine='xmage',
    )
    feats = sim_store.goldfish_features(fmt='commander')
    assert feats['runs'] == 2
    assert feats['games'] == 6
    assert feats['driven_runs'] == 1
    assert feats['total_fires'] == 4  # 2 fires per run raw log
    assert feats['avg_median_kills_own'] == pytest.approx((6.5 + 8.0) / 2)


def test_persist_never_raises_on_bad_result(data_dir: Path) -> None:
    # A result missing median_kills_own -> swallowed, returns None (non-fatal side-channel).
    key = sim_store.persist_goldfish_run(
        DECK, driver_fqcn=None, alpha=None, fmt='commander', games=3,
        result=object(), raw_log=_RAW_LOG, engine='xmage',
    )
    assert key is None
