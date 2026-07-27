"""TDD tests for the DuckDB store layer (Phase 2 — infrastructure only).

Everything here is OFFLINE: a tmp data dir (via the MAKE_MAGIC_DATA_DIR env
override) plus the committed tiny sample fixture. No network, no real bulk
download, no Airtable, no skills.

Coverage:
    - paths resolve under the env-overridden data root and create dirs on demand.
    - connect + write a small relation to raw/ as Parquet + read it back:
      row count and a column value assert.
    - a JOIN across two parquet tables (cards + a synthetic card_otag mapping) —
      the analytical join the whole architecture rests on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store

FIXTURE = Path(__file__).parent / 'fixtures' / 'sample_oracle_cards.json'


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at an isolated tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


def test_paths_resolve_under_env_override(data_dir: Path) -> None:
    paths = store.StorePaths.resolve()
    assert paths.data_dir == data_dir
    assert paths.raw == data_dir / 'raw'
    assert paths.normalized == data_dir / 'normalized'
    assert paths.marts == data_dir / 'marts'
    assert paths.db_path == data_dir / 'make_magic.duckdb'


def test_layer_dir_created_on_demand(data_dir: Path) -> None:
    paths = store.StorePaths.resolve()
    assert not paths.raw.exists()
    created = paths.layer_dir('raw')
    assert created.exists() and created.is_dir()
    assert created == data_dir / 'raw'


def test_unknown_layer_rejected(data_dir: Path) -> None:
    paths = store.StorePaths.resolve()
    with pytest.raises(ValueError):
        paths.layer_dir('bronze')


# --------------------------------------------------------------------------- #
# connect + write_parquet + read_parquet round-trip
# --------------------------------------------------------------------------- #


def test_write_then_read_parquet_roundtrip(data_dir: Path) -> None:
    with store.connect() as conn:
        rel = conn.read_json(str(FIXTURE))
        path = store.write_parquet(conn, rel, 'raw', 'oracle_cards')
        assert path.exists()
        assert path == data_dir / 'raw' / 'oracle_cards.parquet'

        rows = store.read_parquet(conn, 'raw', 'oracle_cards')
        result = rows.aggregate('count(*) AS n, max(cmc) AS max_cmc').fetchone()
        assert result is not None
        n, max_cmc = result
        assert n == 5
        assert max_cmc == 4.0

        bolt = rows.filter("name = 'Lightning Bolt'").project('color_identity').fetchone()
        assert bolt is not None
        assert list(bolt[0]) == ['R']


def test_table_exists_and_list_layer(data_dir: Path) -> None:
    with store.connect() as conn:
        assert store.list_layer('raw') == []
        assert not store.table_exists('raw', 'oracle_cards')

        rel = conn.read_json(str(FIXTURE))
        store.write_parquet(conn, rel, 'raw', 'oracle_cards')

        assert store.table_exists('raw', 'oracle_cards')
        assert store.list_layer('raw') == ['oracle_cards']


# --------------------------------------------------------------------------- #
# The analytical join across two parquet tables
# --------------------------------------------------------------------------- #


def test_join_across_two_parquet_tables(data_dir: Path) -> None:
    with store.connect() as conn:
        cards = conn.read_json(str(FIXTURE))
        store.write_parquet(conn, cards, 'raw', 'oracle_cards')

        # Tiny synthetic oracle-tag mapping (oracle_id -> otag slug).
        otag_rel = conn.sql(
            """
            SELECT * FROM (VALUES
                ('4457ed35-7c10-48c8-9b6c-cf9b3f31c0f7', 'burn'),
                ('56603e91-2f4c-4a44-9d3e-6d1d0b7d1e18', 'counterspell'),
                ('0d2b0c5e-3a3d-4c6f-9c4e-9c2f6f9d7a11', 'board-wipe')
            ) AS t(oracle_id, otag)
            """
        )
        store.write_parquet(conn, otag_rel, 'normalized', 'card_otag')

        store.register_view(conn, 'raw', 'oracle_cards')
        store.register_view(conn, 'normalized', 'card_otag')

        joined = conn.sql(
            """
            SELECT c.name, o.otag
            FROM oracle_cards c
            JOIN card_otag o USING (oracle_id)
            ORDER BY c.name
            """
        ).fetchall()

    assert joined == [
        ('Counterspell', 'counterspell'),
        ('Lightning Bolt', 'burn'),
        ('Wrath of God', 'board-wipe'),
    ]
