"""``RawDeck`` — the cached, pre-canonical deck-import staging record + its cache.

``RawDeck`` is the read-side analog of the ``sources/`` "fetch -> cache into
``raw/``" convention: an adapter's :meth:`~pipeline.sources.deck_import.DeckImporter.fetch`
produces one (source-shaped: ``name`` / ``cards`` / ``meta``), and
:func:`~pipeline.sources.deck_import._normalize_rawdeck` turns it into the one
canonical :class:`~pipeline.contracts.Deck`. The cache stores the **normalized
``RawDeck`` only** (not the original source payload) under
``data/raw/deck_import/<source>/<key>.rawdeck.json``.

Freshness shares the decks-store pull-TTL (W1): :func:`is_fresh` reuses
``pipeline.decks.access._PULL_TTL`` so the import cache and the deck pull policy
stay in lockstep (import from ``access.py`` rather than lifting the constant —
the lower-churn choice, and ``access.py`` is already on the deck path). The
``_needs_pull`` logic there is deck-uuid/freshness-column bound, so this module
mirrors only its "absent OR elapsed" shape, never calls it.

:func:`load_or_fetch` is the single cache choke-point every adapter uses so the
policy (TTL vs. permanent, ``refresh`` force) is implemented once here rather than
re-derived per source.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from pipeline.decks.access import _PULL_TTL
from pipeline.store.paths import StorePaths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = (
    'RawDeck',
    'RawEntry',
    'cache_key',
    'cache_path',
    'content_key',
    'invalidate',
    'is_fresh',
    'load_or_fetch',
    'read_cache',
    'write_cache',
)

#: The lake sub-layer these caches live under (``data/raw/deck_import/``).
_CACHE_LAYER = 'deck_import'
#: The per-record filename suffix (so a key is human-legible in the cache dir).
_CACHE_SUFFIX = '.rawdeck.json'
#: Characters allowed verbatim in a cache key; anything else is sanitized out.
_KEY_SAFE = re.compile(r'[^A-Za-z0-9._-]+')
#: Cap on the sanitized-ref portion of a key before it is hash-suffixed.
_KEY_MAX = 80


class RawEntry(BaseModel):
    """One pre-canonical deck line: a card name, a count, and an optional role.

    Roles here are already the canonical vocabulary (``commander`` / ``sideboard``
    / ``None`` for maindeck) — the adapter's ``fetch`` maps the source's own role
    signal onto them; :func:`~pipeline.sources.deck_import._normalize_rawdeck` then
    passes them straight through the :class:`~pipeline.contracts.DeckCard`
    validator (which is the loud gate for an unknown role).
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Card name as the source names it (Scryfall-shaped; not lake-resolved).')
    quantity: int = Field(default=1, ge=1, description='Copies of this card (at least 1).')
    role: str | None = Field(default=None, description="Canonical role: 'commander' / 'sideboard' / None (maindeck).")


class RawDeck(BaseModel):
    """The cached, source-shaped deck record (the pre-canonical staging stage).

    ``source`` / ``source_ref`` together key the cache entry; ``meta`` carries
    source-specific signal (e.g. Archidekt ``edhBracket``, EDHREC tags) forward
    without a re-fetch; ``fetched_at`` stamps the TTL. It is fully JSON
    round-trippable so the cache is a plain ``.rawdeck.json`` file.
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Deck name as the source names it.')
    cards: list[RawEntry] = Field(default_factory=list, description='Every deck line (commander/sideboard via role).')
    source: str = Field(description="Adapter registry key: 'archidekt' | 'edhrec' | 'moxfield' | 'plaintext'.")
    source_ref: str = Field(description='Native id / url / paste marker — the cache-key component.')
    meta: dict[str, object] = Field(default_factory=dict, description='Source-carried extras (edhBracket, tags, …).')
    fetched_at: datetime = Field(description='When this record was fetched (UTC-aware); drives the TTL.')


# --------------------------------------------------------------------------- #
# Cache keys + paths
# --------------------------------------------------------------------------- #


def content_key(text: str, *, salt: str = '') -> str:
    """A stable, filesystem-safe key for pasted text (a content hash).

    Paste-sourced imports have no re-fetchable ``source_ref``, so the key is a
    truncated SHA-256 of the content: identical text -> identical key (a re-paste
    reuses the cache); different text -> a different key.

    ``salt`` folds a caller-owned discriminator (e.g. a parser version) into the hash so a
    parser change yields a new key and re-parses rather than serving a stale cache entry.
    An empty salt reproduces the bare content hash, so unsalted callers keep their keys.
    """
    payload = text if not salt else f'{len(salt)}:{salt}{text}'
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f'paste-{digest[:16]}'


def cache_key(source_ref: str) -> str:
    """A filesystem-safe cache key for a re-fetchable ``source_ref`` (url / id).

    Builds a legible prefix (path separators and other unsafe characters sanitized
    to ``-``, then length-capped) and **always** appends a short
    ``sha256(source_ref)`` suffix, so distinctness never depends on length: two refs
    that sanitize to the same prefix (e.g. EDHREC ``a/b`` vs ``a-b``) still map to
    distinct files. The same ``source_ref`` is stable across calls. (Pasted text
    should key via :func:`content_key` instead.)
    """
    prefix = _KEY_SAFE.sub('-', source_ref).strip('-')
    if not prefix:
        prefix = 'ref'
    h8 = hashlib.sha256(source_ref.encode('utf-8')).hexdigest()[:8]
    return f'{prefix[:_KEY_MAX]}-{h8}'


def cache_path(source: str, key: str) -> Path:
    """Resolve ``data/raw/deck_import/<source>/<key>.rawdeck.json``.

    Uses the shared lake-dir resolver (:class:`~pipeline.store.paths.StorePaths`)
    so the ``MAKE_MAGIC_DATA_DIR`` test override relocates these caches too. The
    ``source``/``key`` components are sanitized so a caller-supplied value can
    never escape the cache dir via a path separator.
    """
    raw_dir = StorePaths.resolve().layer_dir('raw', create=False)
    safe_source = _KEY_SAFE.sub('-', source).strip('-') or 'unknown'
    safe_key = _KEY_SAFE.sub('-', key).strip('-') or 'ref'
    return raw_dir / _CACHE_LAYER / safe_source / f'{safe_key}{_CACHE_SUFFIX}'


# --------------------------------------------------------------------------- #
# Cache read / write / invalidate
# --------------------------------------------------------------------------- #


def read_cache(source: str, source_ref: str) -> RawDeck | None:
    """Return the cached :class:`RawDeck` for ``source``/``source_ref``, or ``None``.

    The key is derived internally via :func:`cache_key` (the ONE contract shared by
    :func:`write_cache`, :func:`invalidate`, and :func:`load_or_fetch`) so a long ref
    that hash-suffixes on write reads back as a hit rather than a silent miss.

    FAIL-OPEN: a missing file, unreadable bytes, or a payload that no longer
    validates against :class:`RawDeck` (a schema change) all read back as a miss
    (forcing a re-fetch) rather than raising.
    """
    path = cache_path(source, cache_key(source_ref))
    if not path.exists():
        return None
    try:
        return RawDeck.model_validate_json(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def write_cache(raw: RawDeck) -> None:
    """Persist ``raw`` to ``data/raw/deck_import/<source>/<key>.rawdeck.json``.

    The key is derived from ``raw.source_ref`` (a paste ref is already a
    :func:`content_key`; a url/id is sanitized by :func:`cache_key`). Creates the
    source dir on demand.
    """
    path = cache_path(raw.source, cache_key(raw.source_ref))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw.model_dump_json(indent=2), encoding='utf-8')


def invalidate(source: str, source_ref: str) -> None:
    """Remove the cache entry for ``source``/``source_ref`` (idempotent).

    The coherence primitive (design addition #1): a downstream write clears the
    import snapshot so a later read never serves a pre-write state. Keyed by the
    same :func:`cache_key` as :func:`write_cache`; an absent entry is a no-op.
    """
    path = cache_path(source, cache_key(source_ref))
    path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Freshness (shares _PULL_TTL — W1) + the load_or_fetch choke-point
# --------------------------------------------------------------------------- #


def is_fresh(fetched_at: datetime) -> bool:
    """True iff ``fetched_at`` is within the shared pull-TTL (``now - t < _PULL_TTL``).

    Timezone-aware, matching ``access.py``: a naive stamp (e.g. a hand-edited
    cache) is treated as UTC rather than crashing the comparison.
    """
    stamp = fetched_at if fetched_at.tzinfo is not None else fetched_at.replace(tzinfo=UTC)
    return datetime.now(tz=UTC) - stamp < _PULL_TTL


def load_or_fetch(
    source: str,
    source_ref: str,
    fetch_fn: Callable[[], RawDeck],
    *,
    refresh: bool = False,
    permanent: bool = False,
) -> RawDeck:
    """Return a cached-or-freshly-fetched :class:`RawDeck` — the single cache policy.

    Every adapter routes its ``fetch`` through here so the cache policy lives in
    one place:

    - ``refresh=True`` -> always re-fetch (and rewrite the cache), ignoring any
      cached entry.
    - a cache hit that is served -> a **permanent** record is served regardless of
      age; otherwise it is served only while :func:`is_fresh`.
    - a miss / stale (non-permanent) entry -> call ``fetch_fn``, write the cache,
      return the fresh record.

    ``permanent=True`` (paste-sourced) means "never TTL-expire": only ``refresh``
    or an explicit :func:`invalidate` (which drops the entry, making the next call
    a miss) re-fetches it.
    """
    if not refresh:
        cached = read_cache(source, source_ref)
        if cached is not None and (permanent or is_fresh(cached.fetched_at)):
            return cached
    fresh = fetch_fn()
    write_cache(fresh)
    return fresh
