"""Pure ingest primitives: incremental cursor tracking + append-dedupe.

These are the TDD'd, side-effect-light building blocks every puller shares. The
design (data-architecture §ingest): a puller does
``fetch -> cursor/updated_at check (skip if not newer) -> append-dedupe into raw/``.
This module owns the first and third steps as pure-ish functions so the pullers
stay thin and the gate logic is tested in isolation.

Cursor storage: a single JSON map ``{source: token}`` at
``data/raw/_cursors.json``. ``token`` is whatever a source uses to detect
change — an ISO ``updated_at`` for Scryfall bulk, an HTTP ``ETag`` /
``Last-Modified`` for Spellbook, a ``Last Modified Time`` high-water for
Airtable. Reads are FAIL-OPEN: a missing or corrupt file degrades to "no
cursor" (forces a refresh) rather than crashing a caller.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Hashable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pipeline.store.paths import StorePaths

log = logging.getLogger('make_magic.sources')

#: Filename of the per-source cursor map inside the raw/ layer dir.
CURSOR_FILENAME = '_cursors.json'

Row = TypeVar('Row', bound=Mapping[str, object])


class Cursor:
    """A read/write map of ``{source: last_token}`` persisted as JSON.

    Load with :meth:`load` (fail-open), mutate via :meth:`set`, persist with
    :meth:`save`. The backing path lives under the resolved data root so the
    ``MAKE_MAGIC_DATA_DIR`` test override applies automatically.
    """

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self._tokens: dict[str, str] = dict(tokens or {})

    # -- location ---------------------------------------------------------- #

    @staticmethod
    def path() -> Path:
        """Resolve the cursor file path (``data/raw/_cursors.json``).

        Uses ``create=False`` so merely locating the file never materializes the
        ``raw/`` directory as a side effect.
        """
        raw_dir = StorePaths.resolve().layer_dir('raw', create=False)
        return raw_dir / CURSOR_FILENAME

    # -- load / save ------------------------------------------------------- #

    @classmethod
    def load(cls) -> Cursor:
        """Load the cursor map, FAIL-OPEN.

        A missing file, unreadable file, invalid JSON, or non-dict payload all
        degrade to an empty map (so the puller treats everything as stale and
        refreshes) instead of raising.
        """
        path = cls.path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            log.warning('cursor: unreadable %s (%s); treating as empty.', path, exc)
            return cls()
        if not isinstance(data, dict):
            log.warning('cursor: %s is not an object; treating as empty.', path)
            return cls()
        # Coerce to str->str, dropping any malformed entries defensively.
        tokens = {str(k): str(v) for k, v in data.items() if v is not None}
        return cls(tokens)

    def save(self) -> Path:
        """Persist the map to ``data/raw/_cursors.json`` (dir created)."""
        raw_dir = StorePaths.resolve().layer_dir('raw', create=True)
        path = raw_dir / CURSOR_FILENAME
        path.write_text(json.dumps(self._tokens, indent=2, sort_keys=True), encoding='utf-8')
        return path

    # -- accessors --------------------------------------------------------- #

    def get(self, source: str) -> str | None:
        """The last-recorded token for ``source``, or ``None`` if never set."""
        return self._tokens.get(source)

    def set(self, source: str, token: str) -> None:
        """Record ``token`` as the latest cursor for ``source`` (in memory)."""
        self._tokens[source] = token


def is_newer(prior: str | None, incoming: str | None) -> bool:
    """Return True if ``incoming`` should be treated as newer than ``prior``.

    Semantics (the skip-if-not-newer gate):
        - No prior cursor -> always newer (first run must fetch).
        - No incoming token   -> never newer (nothing to compare; don't churn).
        - Both parse as ISO-8601 datetimes -> strict datetime comparison.
        - Otherwise -> opaque token equality (etag/Last-Modified/version string):
          different string means changed => newer; identical means unchanged.
    """
    if incoming is None:
        return False
    if prior is None:
        return True

    prior_dt = _parse_iso(prior)
    incoming_dt = _parse_iso(incoming)
    if prior_dt is not None and incoming_dt is not None:
        return incoming_dt > prior_dt

    # Opaque tokens (etags, version ids): any difference means "changed".
    return incoming != prior


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``); else None."""
    try:
        # Normalize a trailing 'Z' to an explicit UTC offset for fromisoformat.
        normalized = value[:-1] + '+00:00' if value.endswith('Z') else value
        return datetime.fromisoformat(normalized)
    except (ValueError, AttributeError):
        return None


def dedupe[Row: Mapping[str, object]](
    rows: Sequence[Row],
    key: str | Callable[[Row], Hashable],
) -> list[Row]:
    """Append-dedupe: keep the LAST row per key, preserving first-seen order.

    ``key`` is either a field name (dict subscript) or a callable extracting the
    key. Last-wins matches append-dedupe semantics: a re-fetched record replaces
    the older copy, and the output order follows the first appearance of each key
    (stable, deterministic).
    """
    keyfn: Callable[[Row], Hashable]
    if isinstance(key, str):
        field = key

        def _by_field(row: Row) -> Hashable:
            return row[field]  # type: ignore[return-value]

        keyfn = _by_field
    else:
        keyfn = key

    order: list[Hashable] = []
    latest: dict[Hashable, Row] = {}
    for row in rows:
        k = keyfn(row)
        if k not in latest:
            order.append(k)
        latest[k] = row
    return [latest[k] for k in order]
