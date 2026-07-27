"""Store — the DuckDB medallion lake (raw/ -> normalized/ -> marts/).

Public surface: an io module that OWNS the engine (callers never import duckdb)
plus path resolution. This is local infrastructure only — READ-ONLY with
respect to Airtable and skills.

    from pipeline import store
    with store.connect() as conn:
        store.write_parquet(conn, some_relation, "raw", "oracle_cards")
        rows = store.read_parquet(conn, "raw", "oracle_cards")
"""

from __future__ import annotations

from pipeline.store.io import (
    connect,
    list_layer,
    read_parquet,
    register_view,
    table_exists,
    write_parquet,
)
from pipeline.store.paths import (
    DB_FILENAME,
    ENV_DATA_DIR,
    LAYERS,
    StorePaths,
)

__all__ = [
    'DB_FILENAME',
    'ENV_DATA_DIR',
    'LAYERS',
    'StorePaths',
    'connect',
    'list_layer',
    'read_parquet',
    'register_view',
    'table_exists',
    'write_parquet',
]
