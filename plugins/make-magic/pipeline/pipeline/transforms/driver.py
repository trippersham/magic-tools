"""Thin driver mapping normalized-table names to their build functions.

Keeps the "what tables exist and how do I (re)build them" knowledge in one
place, so ``build.py`` (and future callers) iterate ``TABLES`` instead of
hard-coding each transform. Each entry is ``name -> zero-arg callable`` that
reads from ``raw/`` and lands ``normalized/<name>``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pipeline.transforms import combo_detect, otag_rollup

#: normalized-table name -> its build function (raw -> normalized/<name>).
TABLES: dict[str, Callable[[], Path]] = {
    otag_rollup.NORMALIZED_TABLE: otag_rollup.build,
    combo_detect.NORMALIZED_TABLE: combo_detect.build,
}


def build_table(name: str) -> Path:
    """Build a single normalized table by name; raise ``KeyError`` if unknown."""
    if name not in TABLES:
        raise KeyError(f'Unknown normalized table {name!r}; known: {sorted(TABLES)}.')
    return TABLES[name]()


def build_all() -> dict[str, Path]:
    """Build every normalized table; return ``name -> landed Parquet path``."""
    return {name: fn() for name, fn in TABLES.items()}
