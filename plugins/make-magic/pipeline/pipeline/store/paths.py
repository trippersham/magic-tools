"""Store path resolution — the ONE place that knows where the lake lives.

The medallion lake (``raw/`` -> ``normalized/`` -> ``marts/``) plus the DuckDB
file live under ``pipeline/data/``. Paths anchor to this module's location (NOT
the current working directory) so ``uv run`` from anywhere resolves the same
lake — mirroring the compsych ``config.py`` ``.git``-walk-up pattern, but here
the data dir sits inside the package so a package-relative anchor is enough.

Overridable via the ``MAKE_MAGIC_DATA_DIR`` env var (used by tests to point at
an isolated tmp dir). Directories are created on demand, never eagerly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Env var that overrides the resolved data root (absolute path). Tests set this.
ENV_DATA_DIR = 'MAKE_MAGIC_DATA_DIR'

#: The three medallion layers, in refinement order.
LAYERS = ('raw', 'normalized', 'marts')

#: DuckDB working-database filename inside the data root.
DB_FILENAME = 'make_magic.duckdb'


def _default_data_dir() -> Path:
    """The project-relative data root: ``pipeline/data/`` beside the package.

    Anchored to this file (``pipeline/pipeline/store/paths.py``) so it is
    cwd-independent: parents[2] is the project root (the dir holding
    ``pyproject.toml``, ``.gitignore``, and the committed ``data/snapshots/``);
    its ``data`` child is the lake. This is the SAME ``data/`` the ingest
    snapshot loaders resolve, so raw/normalized/marts sit alongside the
    bundled snapshots.
    """
    return Path(__file__).resolve().parents[2] / 'data'


@dataclass(frozen=True)
class StorePaths:
    """Resolved lake paths. Construct via :meth:`resolve` (honors the env override)."""

    data_dir: Path

    @classmethod
    def resolve(cls) -> StorePaths:
        """Resolve the data root: ``MAKE_MAGIC_DATA_DIR`` if set, else package-relative."""
        override = os.getenv(ENV_DATA_DIR)
        root = Path(override).resolve() if override else _default_data_dir()
        return cls(data_dir=root)

    @property
    def raw(self) -> Path:
        return self.data_dir / 'raw'

    @property
    def normalized(self) -> Path:
        return self.data_dir / 'normalized'

    @property
    def marts(self) -> Path:
        return self.data_dir / 'marts'

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def collection(self) -> Path:
        """The hand-editable ``collection/`` dir (decks/inventory/chase/trades YAML).

        Resolved off the SAME data root as the lake + DuckDB so a single
        ``MAKE_MAGIC_DATA_DIR`` override relocates the whole store (lake, db, and
        collection) together — the cleanest one-knob option for tests + isolation.
        """
        return self.data_dir / 'collection'

    def layer_dir(self, layer: str, *, create: bool = True) -> Path:
        """Return the directory for ``layer`` (one of :data:`LAYERS`).

        Creates it (and parents) on demand unless ``create=False``. Raises
        ``ValueError`` for an unknown layer so typos fail loudly.
        """
        if layer not in LAYERS:
            raise ValueError(f'Unknown layer {layer!r}; expected one of {LAYERS}.')
        path = self.data_dir / layer
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def parquet_path(self, layer: str, name: str, *, create: bool = True) -> Path:
        """Full path to ``data/<layer>/<name>.parquet`` (dir created on demand)."""
        return self.layer_dir(layer, create=create) / f'{name}.parquet'
