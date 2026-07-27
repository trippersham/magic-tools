"""The io module that OWNS the DuckDB engine.

Callers never ``import duckdb`` or track the engine themselves — they hand a
connection and a (layer, name) coordinate, and this module resolves paths,
materializes Parquet, registers views, and attaches external SQLite.

Design (data-architecture §store): one io module owns read/write so the
read/write split lives in exactly one place. Unlike the compsych Delta pattern
(read via ``delta_scan``, write via delta-rs — because DuckDB's delta extension
is read-only), plain Parquet is symmetric: DuckDB both writes (``COPY ... TO``)
and reads (``read_parquet``) natively, so both sides live here with no second
engine.

``attach_sqlite`` is OPPORTUNISTIC: the Scryfall SQLite cache is ephemeral
(``$TMPDIR``, dies with the session), so a missing file is normal — attach
skips gracefully (logs + returns ``False``) rather than raising.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from pipeline.store.paths import StorePaths

if TYPE_CHECKING:  # a query source: a SQL string or a DuckDB relation.
    from duckdb import DuckDBPyRelation

    RelationOrSelect = str | DuckDBPyRelation

log = logging.getLogger('make_magic.store')


@contextmanager
def connect(
    db_path: str | os.PathLike[str] | None = None,
    *,
    read_only: bool = False,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open a DuckDB connection to the lake's working database.

    Context-manager friendly: the connection is closed on exit. ``db_path``
    defaults to the resolved ``data/make_magic.duckdb`` (honoring the
    ``MAKE_MAGIC_DATA_DIR`` override); the parent dir is created on demand.

    Args:
        db_path: Override the database file location (mostly for tests).
        read_only: Open read-only (no writes; the file must already exist).
    """
    paths = StorePaths.resolve()
    target = Path(db_path) if db_path is not None else paths.db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(target), read_only=read_only)
    try:
        yield conn
    finally:
        conn.close()


def write_parquet(
    conn: duckdb.DuckDBPyConnection,
    relation_or_select: RelationOrSelect,
    layer: str,
    name: str,
) -> Path:
    """Materialize a query/relation to ``data/<layer>/<name>.parquet``.

    ``relation_or_select`` may be a ``DuckDBPyRelation`` or a raw ``SELECT``
    string. Returns the written Parquet path (dir created on demand).
    """
    path = StorePaths.resolve().parquet_path(layer, name)
    rel = conn.sql(relation_or_select) if isinstance(relation_or_select, str) else relation_or_select
    # COPY ... TO writes a single Parquet file; overwrites any existing one.
    conn.sql(f"COPY ({rel.sql_query()}) TO '{path}' (FORMAT PARQUET)")
    return path


def read_parquet(
    conn: duckdb.DuckDBPyConnection,
    layer: str,
    name: str,
) -> DuckDBPyRelation:
    """Return a relation over ``data/<layer>/<name>.parquet`` (lazy; not fetched)."""
    path = StorePaths.resolve().parquet_path(layer, name, create=False)
    if not path.exists():
        raise FileNotFoundError(f'No parquet at {path} ({layer}/{name}).')
    return conn.read_parquet(str(path))


def register_view(
    conn: duckdb.DuckDBPyConnection,
    layer: str,
    name: str,
    *,
    view_name: str | None = None,
) -> str:
    """Register ``data/<layer>/<name>.parquet`` as a SQL view for joins.

    The view is named ``name`` by default (override with ``view_name``) so a
    later query can ``SELECT ... FROM <name>``. Returns the view name.
    """
    path = StorePaths.resolve().parquet_path(layer, name, create=False)
    if not path.exists():
        raise FileNotFoundError(f'No parquet at {path} ({layer}/{name}).')
    view = view_name or name
    conn.sql(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{path}')")
    return view


def attach_sqlite(
    conn: duckdb.DuckDBPyConnection,
    sqlite_path: str | os.PathLike[str],
    alias: str,
    *,
    read_only: bool = True,
) -> bool:
    """Opportunistically ATTACH a SQLite database under ``alias``.

    The Scryfall cache is ephemeral, so a missing file is expected: this logs
    and returns ``False`` instead of raising. On success returns ``True`` and
    the caller can join across ``<alias>.<table>``.
    """
    path = Path(sqlite_path)
    if not path.exists():
        log.info('attach_sqlite: %s absent; skipping (opportunistic).', path)
        return False
    # The bundled `sqlite` extension is needed to ATTACH a SQLite file. It ships
    # with DuckDB, so install/load is offline (no network) and idempotent.
    conn.install_extension('sqlite')
    conn.load_extension('sqlite')
    mode = ', READ_ONLY' if read_only else ''
    conn.sql(f"ATTACH '{path}' AS {alias} (TYPE SQLITE{mode})")
    return True


def table_exists(layer: str, name: str) -> bool:
    """True if ``data/<layer>/<name>.parquet`` exists on disk."""
    return StorePaths.resolve().parquet_path(layer, name, create=False).exists()


def list_layer(layer: str) -> list[str]:
    """Sorted names of the Parquet tables materialized in ``layer``."""
    layer_dir = StorePaths.resolve().layer_dir(layer, create=False)
    if not layer_dir.exists():
        return []
    return sorted(p.stem for p in layer_dir.glob('*.parquet'))
