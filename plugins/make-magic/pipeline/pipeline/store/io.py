"""The io module that owns the DuckDB engine.

Callers never ``import duckdb`` or track the engine themselves — they hand a
connection and a (layer, name) coordinate, and this module resolves paths,
materializes Parquet, and registers views.

One io module owns read/write so the split lives in exactly one place. Plain
Parquet is symmetric — DuckDB both writes (``COPY ... TO``) and reads
(``read_parquet``) natively — so both sides live here with no second engine.
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
    # Atomic write: COPY to a temp file in the same dir, then os.replace() it into
    # place (an atomic rename on POSIX). A mid-write crash leaves the temp file
    # (cleaned up here) and the prior Parquet at `path` fully intact, rather than
    # truncating the whole bulk on a partial write.
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        conn.sql(f"COPY ({rel.sql_query()}) TO '{tmp}' (FORMAT PARQUET)")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
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


def table_exists(layer: str, name: str) -> bool:
    """True if ``data/<layer>/<name>.parquet`` exists on disk."""
    return StorePaths.resolve().parquet_path(layer, name, create=False).exists()


def list_layer(layer: str) -> list[str]:
    """Sorted names of the Parquet tables materialized in ``layer``."""
    layer_dir = StorePaths.resolve().layer_dir(layer, create=False)
    if not layer_dir.exists():
        return []
    return sorted(p.stem for p in layer_dir.glob('*.parquet'))
