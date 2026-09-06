"""TDD tests for the XMage game-transcript parser + persistence (Phase 4).

PURE PYTHON — NO JVM. The fixtures are hand-authored transcript strings that
match the EXACT marker grammar the Phase-2 live-flush harness emits, grounded in
``mage/collectors/MakeMagicHooks.java`` + ``org/makemagic/xmage/XMageBatch.java``
+ the driver seam patches (``MACRO_FIRE_REAL``, ``DRIVER_MACRO_FIRED``,
``DRIVER_REGISTERED``, ``DRIVER_MULLIGAN``). See that module for the P4->P2 marker
contract these fixtures encode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store
from pipeline.sim import store as sim_store
from pipeline.sim import transcript_parser as tp

# --- fixtures: real-marker transcripts (data files, not inline, to keep the ---
# long HANDLOG/RESULT marker lines intact without tripping the 120-col limit) --- #
#
# (a) driven_combo_win: driver registers, the macro becomes reachable in search
#     (DRIVER_MACRO_FIRED) at global turn 3 = ASSEMBLED, then really commits
#     (MACRO_FIRE_REAL turn=5) = FIRED, so assembled_turn (3) < fired_turn (5); the
#     opponent cast Duress at turn 2 before the fire (disruption survived).
# (b) driverless_loss: no DRIVER_* markers; PlayerB wins by combat; A crosses 0 at 8.
# (c) timeout_brick: reached the cap, ended=false, genuine Draw terminator.
# (d) partial_truncated: live-flush caught mid-game, final line cut off mid-token.
_FIX = Path(__file__).parent / 'fixtures' / 'transcripts'
DRIVEN_COMBO_WIN = (_FIX / 'driven_combo_win.log').read_text()
DRIVERLESS_LOSS = (_FIX / 'driverless_loss.log').read_text()
TIMEOUT_BRICK = (_FIX / 'timeout_brick.log').read_text()
PARTIAL = (_FIX / 'partial_truncated.log').read_text()


# --- parse tests ----------------------------------------------------------- #


def test_driven_combo_win_assembled_before_fired() -> None:
    f = tp.parse_transcript(DRIVEN_COMBO_WIN)
    assert f.winner == 'a'
    assert f.wincon == 'combo'
    assert f.driver_registered is True
    assert f.macro_reachable is True
    # The NEW richer metric: reachable (assembled) at turn 3, committed (fired) at 5.
    assert f.assembled_turn == 3
    assert f.fired_turn == 5
    assert f.assembled_turn < f.fired_turn
    # kill_turn falls back to the fire turn for a non-life-loss combo win.
    assert f.kill_turn == 5
    # three spells cast on the fire turn.
    assert f.storm_count == 3
    # opponent cast Duress before the fire -> the combo survived disruption.
    assert f.disruption_survived is True
    assert f.game_length_ms == 8421
    assert f.incomplete is False


def test_driverless_loss_combat() -> None:
    f = tp.parse_transcript(DRIVERLESS_LOSS)
    assert f.winner == 'b'
    assert f.wincon == 'combat'
    assert f.kill_turn == 8
    assert f.win_margin_life == 14
    assert f.driver_registered is False
    assert f.macro_reachable is False
    assert f.assembled_turn is None
    assert f.fired_turn is None
    assert f.storm_count is None
    assert f.game_length_ms == 15000
    assert f.incomplete is False


def test_timeout_brick_is_anomaly() -> None:
    f = tp.parse_transcript(TIMEOUT_BRICK)
    assert f.winner == 'draw'
    assert f.kill_turn is None
    assert f.wincon is None
    assert f.timeout is True
    assert f.is_anomaly is True
    assert f.game_length_ms == 60000


def test_partial_transcript_is_graceful() -> None:
    # Must not raise on a truncated mid-token final line.
    f = tp.parse_transcript(PARTIAL)
    assert f.incomplete is True
    assert f.is_anomaly is True
    assert f.winner == 'unknown'
    assert f.kill_turn is None
    assert f.game_length_ms is None
    # It still parsed what was present: the last full turn boundary seen was 3.
    assert f.last_turn == 3


def test_parse_accepts_lines_iterable() -> None:
    f = tp.parse_transcript(DRIVERLESS_LOSS.splitlines())
    assert f.winner == 'b'


# --- persistence tests ----------------------------------------------------- #


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def _feature_rows(cell_key: str) -> list[tuple]:
    db = sim_store._db_path(None)
    with store.connect(db) as conn:
        sim_store._ensure_tables(conn)
        tp._ensure_transcript_columns(conn)
        return conn.execute(
            'SELECT game_index, winner, wincon, assembled_turn, fired_turn, storm_count, '
            'driver_registered FROM sim_game_features WHERE matchup_key = ? ORDER BY game_index',
            [cell_key],
        ).fetchall()


def _log_rows(cell_key: str) -> list[tuple]:
    db = sim_store._db_path(None)
    with store.connect(db) as conn:
        return conn.execute(
            'SELECT game_index, raw_log FROM sim_game_logs WHERE matchup_key = ? ORDER BY game_index',
            [cell_key],
        ).fetchall()


def test_ingest_writes_features_and_log(data_dir: Path, tmp_path: Path) -> None:
    path = _write(tmp_path, 'g0.log', DRIVEN_COMBO_WIN)
    persisted = tp.ingest_transcript(None, 'cellX', 0, path)
    assert persisted is True
    rows = _feature_rows('cellX')
    assert len(rows) == 1
    game_index, winner, wincon, assembled, fired, storm, registered = rows[0]
    assert (game_index, winner, wincon) == (0, 'a', 'combo')
    assert (assembled, fired, storm) == (3, 5, 3)
    assert registered is True
    logs = _log_rows('cellX')
    assert len(logs) == 1
    assert 'MACRO_FIRE_REAL' in logs[0][1]
    # default cleanup removes the transcript file after ingest.
    assert not path.exists()


def test_interest_policy_filters(data_dir: Path, tmp_path: Path) -> None:
    # A driverless, non-anomaly LOSS is NOT of interest -> skipped.
    loss = _write(tmp_path, 'loss.log', DRIVERLESS_LOSS)
    assert tp.ingest_transcript(None, 'cellI', 0, loss, policy='interest') is False
    assert _feature_rows('cellI') == []
    # A driven combo win (macro fired) IS of interest -> kept.
    win = _write(tmp_path, 'win.log', DRIVEN_COMBO_WIN)
    assert tp.ingest_transcript(None, 'cellI', 1, win, policy='interest') is True
    # A timeout/brick anomaly IS of interest -> kept.
    brick = _write(tmp_path, 'brick.log', TIMEOUT_BRICK)
    assert tp.ingest_transcript(None, 'cellI', 2, brick, policy='interest') is True
    idxs = [r[0] for r in _feature_rows('cellI')]
    assert idxs == [1, 2]


def test_sample_policy_filters(data_dir: Path, tmp_path: Path) -> None:
    kept = []
    for i in range(5):
        p = _write(tmp_path, f'g{i}.log', DRIVERLESS_LOSS)
        if tp.ingest_transcript(None, 'cellS', i, p, policy='sample:2'):
            kept.append(i)
    assert kept == [0, 2, 4]


def test_missing_and_malformed_file_non_fatal(data_dir: Path, tmp_path: Path) -> None:
    # Missing file: no raise, returns False, nothing persisted.
    missing = tmp_path / 'nope.log'
    assert tp.ingest_transcript(None, 'cellM', 0, missing) is False
    assert _feature_rows('cellM') == []
    # Empty/garbage file: parses to an incomplete row, still non-fatal.
    junk = _write(tmp_path, 'junk.log', 'not a real transcript\n\x00\xff\n')
    assert tp.ingest_transcript(None, 'cellM', 1, junk, cleanup=False) is True
    rows = _feature_rows('cellM')
    assert [r[0] for r in rows] == [1]
