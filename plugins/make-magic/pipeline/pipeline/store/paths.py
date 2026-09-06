"""Store path resolution — the one place that knows where the lake lives.

The mutable store — the medallion lake (``raw/`` -> ``normalized/`` -> ``marts/``),
the DuckDB working database, and the hand-editable ``collection/`` YAML — lives
under a single **per-user home** data root by default, NOT inside the code
checkout. The bundled offline snapshots (``data/snapshots/``) still ship in the
package and are resolved package-relative by their own loaders (see
``sources/oracle_tags.py`` and ``sources/spellbook.py``, which anchor on
``__file__``), so the home anchor here only relocates the working store — offline
degradation is unaffected.

Resolution (see :meth:`StorePaths.resolve`):
  1. ``MAKE_MAGIC_DATA_DIR`` if set — the explicit override (tests + user choice);
  2. else ``$XDG_DATA_HOME/make-magic`` if ``XDG_DATA_HOME`` is set;
  3. else ``~/.local/share/make-magic`` (the XDG default).

Why a home anchor, not package-relative: the default used to resolve to
``<this checkout>/pipeline/data``, so every git worktree, source checkout, and
installed plugin VERSION minted its own ``make_magic.duckdb`` + ``collection/``
the first time it ran — the store silently forked per copy of the code. A stable
home root gives one shared store across all of them. Directories are created on
demand, never eagerly.
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

#: Env var honored (when ``MAKE_MAGIC_DATA_DIR`` is unset) for the XDG data home.
ENV_XDG_DATA_HOME = 'XDG_DATA_HOME'

#: The application's subdirectory name under the resolved data home.
APP_DIR_NAME = 'make-magic'


def _default_data_dir() -> Path:
    """The default data root: a stable per-user home location (XDG-aware).

    Used only when ``MAKE_MAGIC_DATA_DIR`` is unset. Resolves to
    ``$XDG_DATA_HOME/make-magic`` when ``XDG_DATA_HOME`` is set, else
    ``~/.local/share/make-magic`` (the XDG Base Directory default).

    Home-anchored ON PURPOSE. The default previously resolved package-relative
    (``parents[2]/'data'``), so every git worktree, source checkout, and installed
    plugin version created its own ``make_magic.duckdb`` + ``collection/`` — the
    store forked per copy of the code. A home anchor gives all of them one shared
    store. The bundled ``data/snapshots/`` still ship in the package and are
    resolved package-relative by their loaders, so this does not touch offline
    degradation. Override with ``MAKE_MAGIC_DATA_DIR`` for tests or a custom store.
    """
    xdg = os.getenv(ENV_XDG_DATA_HOME)
    base = Path(xdg).resolve() if xdg else Path.home() / '.local' / 'share'
    return base / APP_DIR_NAME


@dataclass(frozen=True)
class StorePaths:
    """Resolved lake paths. Construct via :meth:`resolve` (honors the env override)."""

    data_dir: Path

    @classmethod
    def resolve(cls) -> StorePaths:
        """Resolve the data root: ``MAKE_MAGIC_DATA_DIR`` if set, else the home default.

        The home default is XDG-aware (``$XDG_DATA_HOME/make-magic`` or
        ``~/.local/share/make-magic``); see :func:`_default_data_dir`.
        """
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

        Resolved off the same data root as the lake + DuckDB so a single
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
