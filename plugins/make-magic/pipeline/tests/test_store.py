"""TDD tests for the DuckDB store layer.

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
        assert n == 6
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


# --------------------------------------------------------------------------- #
# write_parquet is atomic (tmp file + os.replace).
#
# write_parquet copies to a temp path in the same dir, then `os.replace()`s it
# into place (atomic rename) — a failed write leaves the prior Parquet intact and
# never leaves a partial at the target.
# --------------------------------------------------------------------------- #


class _ConnProxy:
    """Delegates everything to a real DuckDB connection, but lets a test observe
    (or blow up) the COPY. The DuckDB connection's own `sql` attribute is
    read-only, so the spy has to WRAP it rather than monkeypatch it."""

    def __init__(self, conn, *, on_copy=None) -> None:
        self._conn = conn
        self._on_copy = on_copy
        self.copied: list[str] = []

    def sql(self, query, *a, **k):
        if isinstance(query, str) and query.lstrip().upper().startswith('COPY'):
            self.copied.append(query)
            if self._on_copy is not None:
                self._on_copy(query)
        return self._conn.sql(query, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_write_parquet_uses_tmp_then_rename(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The COPY target is a TEMP path, promoted to the final path via os.replace."""
    import pipeline.store.io as io

    replaced: list[tuple[str, str]] = []
    real_replace = io.os.replace

    def spy_replace(src, dst):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(io.os, 'replace', spy_replace)

    with store.connect() as conn:
        proxy = _ConnProxy(conn)
        rel = conn.read_json(str(FIXTURE))
        target = store.StorePaths.resolve().parquet_path('raw', 'oracle_cards')
        path = io.write_parquet(proxy, rel, 'raw', 'oracle_cards')

    # The COPY did NOT write straight to the final path.
    assert proxy.copied, 'a COPY statement should run'
    assert f"TO '{target}'" not in proxy.copied[0], 'COPY must not target the final path directly'
    # An atomic rename promoted the temp file to the final path.
    assert replaced, 'write_parquet must os.replace(tmp, path)'
    assert replaced[-1][1] == str(target)
    assert path == target
    assert target.exists()


def test_failed_write_leaves_prior_parquet_intact(data_dir: Path) -> None:
    """A crash mid-write leaves the PRIOR Parquet fully readable (no truncation)."""
    import pipeline.store.io as io

    # Seed a good prior Parquet (6 rows).
    with store.connect() as conn:
        rel = conn.read_json(str(FIXTURE))
        store.write_parquet(conn, rel, 'raw', 'oracle_cards')

    target = store.StorePaths.resolve().parquet_path('raw', 'oracle_cards')
    before = target.read_bytes()

    def boom(_query: str) -> None:
        raise RuntimeError('disk full mid-COPY')

    # A second write that BLOWS UP during the COPY (mid-write crash).
    with store.connect() as conn:
        rel2 = conn.read_json(str(FIXTURE))
        proxy = _ConnProxy(conn, on_copy=boom)
        with pytest.raises(RuntimeError, match='disk full'):
            io.write_parquet(proxy, rel2, 'raw', 'oracle_cards')

    # The prior Parquet is byte-identical and still readable (6 rows).
    assert target.read_bytes() == before
    with store.connect() as conn:
        n = store.read_parquet(conn, 'raw', 'oracle_cards').aggregate('count(*) AS n').fetchone()
        assert n is not None and n[0] == 6
    # No leftover temp file at the target dir.
    leftovers = list(target.parent.glob('*.tmp*'))
    assert leftovers == [], f'a failed write must not leave a temp file: {leftovers}'
