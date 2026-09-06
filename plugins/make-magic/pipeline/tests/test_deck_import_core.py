"""Core deck-ingestor contract — ports, ``RawDeck``, cache, registry (Phase 0).

Everything is OFFLINE and deterministic: an isolated tmp data root (via
``MAKE_MAGIC_DATA_DIR``) backs the ``raw/deck_import/`` cache; no network, no
adapters (the ``_IMPORTERS`` registry is empty in P0). Covers:

    - registry dispatch: a fake ``DeckImporter`` routes by ``matches``; ``source=``
      override; unknown ref / unknown source -> a ``ValueError`` naming supported
      sources;
    - ``RawDeck`` JSON round-trip;
    - cache write -> read hit; ``is_fresh`` inside/past the shared ``_PULL_TTL``;
      ``invalidate`` removes; ``content_key`` stable / distinct;
    - ``load_or_fetch`` policy: miss fetches + caches once; fresh hit skips fetch;
      ``refresh`` re-fetches; ``permanent`` ignores TTL but honors refresh/invalidate;
    - ``_normalize_rawdeck`` builds a ``Deck`` (quantities + roles, commander +
      sideboard), an unknown role is a loud error, and it imports NO resolver.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline import store
from pipeline.contracts import Deck
from pipeline.decks.access import _PULL_TTL
from pipeline.sources import deck_import
from pipeline.sources.deck_import import (
    DeckImporter,
    get_importer,
    import_deck,
)
from pipeline.sources.deck_import.raw import (
    RawDeck,
    RawEntry,
    cache_key,
    cache_path,
    content_key,
    invalidate,
    is_fresh,
    load_or_fetch,
    read_cache,
    write_cache,
)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the cache at an isolated tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


@pytest.fixture()
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, DeckImporter]:
    """A fresh, empty ``_IMPORTERS`` registry per test (never mutate the module one)."""
    registry: dict[str, DeckImporter] = {}
    monkeypatch.setattr(deck_import, '_IMPORTERS', registry)
    return registry


class _FakeImporter:
    """A minimal ``DeckImporter`` for dispatch tests — matches a fixed host prefix."""

    source = 'fake'

    def __init__(self, prefix: str = 'fake://') -> None:
        self._prefix = prefix
        self.last_refresh: bool | None = None

    def matches(self, ref: str) -> bool:
        return ref.startswith(self._prefix)

    def fetch(self, ref: str, *, refresh: bool = False) -> RawDeck:
        self.last_refresh = refresh
        return RawDeck(
            name='Fake Deck',
            cards=[RawEntry(name='Sol Ring', quantity=1, role=None)],
            source=self.source,
            source_ref=ref,
            fetched_at=datetime.now(tz=UTC),
        )

    def normalize(self, raw: RawDeck) -> Deck:
        from pipeline.sources.deck_import import _normalize_rawdeck

        return _normalize_rawdeck(raw)


# --------------------------------------------------------------------------- #
# Protocol / registry dispatch
# --------------------------------------------------------------------------- #


def test_fake_importer_satisfies_protocol() -> None:
    assert isinstance(_FakeImporter(), DeckImporter)


def test_get_importer_routes_by_matches(clean_registry: dict[str, DeckImporter]) -> None:
    imp = _FakeImporter()
    clean_registry[imp.source] = imp
    assert get_importer('fake://123') is imp


def test_get_importer_source_override(clean_registry: dict[str, DeckImporter]) -> None:
    imp = _FakeImporter()
    clean_registry[imp.source] = imp
    # A ref that does NOT match, resolved purely by the source= override.
    assert get_importer('https://example.com/x', source='fake') is imp


def test_get_importer_unknown_ref_raises_naming_sources(clean_registry: dict[str, DeckImporter]) -> None:
    clean_registry['fake'] = _FakeImporter()
    with pytest.raises(ValueError, match=r"no importer matches.*supported sources.*'fake'"):
        get_importer('https://unknown.example/deck/1')


def test_get_importer_unknown_source_raises_naming_sources(clean_registry: dict[str, DeckImporter]) -> None:
    clean_registry['fake'] = _FakeImporter()
    with pytest.raises(ValueError, match=r"unknown source 'nope'.*supported sources.*'fake'"):
        get_importer('fake://1', source='nope')


def test_import_deck_dispatches_and_normalizes(clean_registry: dict[str, DeckImporter], data_dir: Path) -> None:
    imp = _FakeImporter()
    clean_registry[imp.source] = imp
    deck = import_deck('fake://abc')
    assert deck.name == 'Fake Deck'
    assert [(c.name, c.quantity) for c in deck.cards] == [('Sol Ring', 1)]


def test_import_deck_threads_refresh_into_fetch(clean_registry: dict[str, DeckImporter], data_dir: Path) -> None:
    # Q2: import_deck(..., refresh=True) reaches the adapter's fetch with refresh=True.
    imp = _FakeImporter()
    clean_registry[imp.source] = imp
    import_deck('fake://abc', refresh=True)
    assert imp.last_refresh is True
    import_deck('fake://abc')
    assert imp.last_refresh is False


# --------------------------------------------------------------------------- #
# RawDeck round-trip
# --------------------------------------------------------------------------- #


def _sample_raw(source_ref: str = 'deck-1', source: str = 'archidekt') -> RawDeck:
    return RawDeck(
        name='Test Deck',
        cards=[
            RawEntry(name='Krenko, Mob Boss', quantity=1, role='commander'),
            RawEntry(name='Mountain', quantity=30, role=None),
            RawEntry(name='Lightning Bolt', quantity=1, role='sideboard'),
        ],
        source=source,
        source_ref=source_ref,
        meta={'edhBracket': 3},
        # Fresh by default so the load_or_fetch happy-path (cache-hit) tests hold;
        # staleness tests override fetched_at explicitly via model_copy.
        fetched_at=datetime.now(tz=UTC),
    )


def test_rawdeck_json_round_trip() -> None:
    raw = _sample_raw()
    restored = RawDeck.model_validate_json(raw.model_dump_json())
    assert restored == raw
    assert restored.meta == {'edhBracket': 3}
    assert restored.cards[0].role == 'commander'


# --------------------------------------------------------------------------- #
# Cache mechanics
# --------------------------------------------------------------------------- #


def test_cache_path_layout(data_dir: Path) -> None:
    path = cache_path('archidekt', 'deck-1')
    assert path == data_dir / 'raw' / 'deck_import' / 'archidekt' / 'deck-1.rawdeck.json'


def test_write_then_read_cache_hit(data_dir: Path) -> None:
    raw = _sample_raw()
    write_cache(raw)
    got = read_cache(raw.source, raw.source_ref)
    assert got == raw


def test_read_cache_miss_returns_none(data_dir: Path) -> None:
    assert read_cache('archidekt', 'absent') is None


def test_invalidate_removes_entry(data_dir: Path) -> None:
    raw = _sample_raw()
    write_cache(raw)
    assert read_cache(raw.source, raw.source_ref) is not None
    invalidate(raw.source, raw.source_ref)
    assert read_cache(raw.source, raw.source_ref) is None
    # Idempotent: invalidating an absent entry is a no-op, not an error.
    invalidate(raw.source, raw.source_ref)


def test_is_fresh_inside_and_past_ttl() -> None:
    now = datetime.now(tz=UTC)
    assert is_fresh(now) is True
    assert is_fresh(now - (_PULL_TTL / 2)) is True
    assert is_fresh(now - _PULL_TTL - timedelta(seconds=1)) is False


def test_is_fresh_naive_datetime_treated_as_utc() -> None:
    # A naive stamp (e.g. a hand-edited cache) is treated as UTC, never crashes.
    assert is_fresh(datetime.now(tz=UTC).replace(tzinfo=None)) is True


def test_content_key_stable_and_distinct() -> None:
    assert content_key('1 Sol Ring\n') == content_key('1 Sol Ring\n')
    assert content_key('1 Sol Ring\n') != content_key('1 Mox Opal\n')
    # Filesystem-safe: no path separators / dodgy chars.
    key = content_key('a/b\\c deck')
    assert '/' not in key and '\\' not in key


def test_content_key_salt_busts_stale_parse_cache() -> None:
    """A `salt` (a parser version) changes the key so identical text re-parses when the parser
    changes. Same text + same salt still dedupes; the key stays filesystem-safe.
    Same text + same salt still dedupes; the key stays filesystem-safe."""
    import hashlib

    text = '1 Sol Ring\n'
    assert content_key(text, salt='v1') == content_key(text, salt='v1')  # stable within a version
    assert content_key(text, salt='v1') != content_key(text, salt='v2')  # bumping the version busts it
    assert content_key(text) != content_key(text, salt='v2')  # unsalted differs from salted
    # An empty salt reproduces the bare content hash (unsalted callers keep their keys).
    assert content_key(text) == f'paste-{hashlib.sha256(text.encode()).hexdigest()[:16]}'
    salted = content_key('a/b\\c', salt='v9')
    assert '/' not in salted and '\\' not in salted


def test_cache_key_sanitizes_urls(data_dir: Path) -> None:
    # A URL source_ref must not escape the source dir via path separators.
    path = cache_path('archidekt', cache_key('https://archidekt.com/decks/10126962'))
    assert path.parent == data_dir / 'raw' / 'deck_import' / 'archidekt'
    assert path.name.endswith('.rawdeck.json')
    assert '/' not in path.name.removesuffix('.rawdeck.json')


def test_cache_key_distinguishes_short_colliding_refs() -> None:
    # F1: refs that sanitize to the same prefix must not collide (EDHREC slug/tag).
    assert cache_key('a/b') != cache_key('a-b')
    # Stable across calls for the same source_ref.
    assert cache_key('a/b') == cache_key('a/b')


def test_long_ref_round_trips_and_invalidates(data_dir: Path) -> None:
    # F2: read/write/invalidate all derive the key from source_ref, so a >80-char
    # ref that hash-suffixes on write reads back as a hit (not a silent miss).
    long_ref = 'https://archidekt.com/decks/' + 'x' * 120
    raw = _sample_raw(source_ref=long_ref)
    assert len(raw.source_ref) > 80
    write_cache(raw)
    assert read_cache(raw.source, long_ref) == raw
    invalidate(raw.source, long_ref)
    assert read_cache(raw.source, long_ref) is None


# --------------------------------------------------------------------------- #
# load_or_fetch policy (the single cache choke-point)
# --------------------------------------------------------------------------- #


class _Counter:
    def __init__(self, raw: RawDeck) -> None:
        self._raw = raw
        self.calls = 0

    def __call__(self) -> RawDeck:
        self.calls += 1
        return self._raw


def test_load_or_fetch_miss_fetches_and_caches(data_dir: Path) -> None:
    raw = _sample_raw()
    fetch = _Counter(raw)
    got = load_or_fetch(raw.source, raw.source_ref, fetch)
    assert got == raw
    assert fetch.calls == 1
    # It persisted: a second call is a fresh cache hit, no fetch.
    again = load_or_fetch(raw.source, raw.source_ref, fetch)
    assert again == raw
    assert fetch.calls == 1


def test_load_or_fetch_stale_refetches(data_dir: Path) -> None:
    stale = _sample_raw().model_copy(update={'fetched_at': datetime.now(tz=UTC) - _PULL_TTL - timedelta(minutes=1)})
    write_cache(stale)
    fresh = _sample_raw().model_copy(update={'name': 'Refetched'})
    fetch = _Counter(fresh)
    got = load_or_fetch(stale.source, stale.source_ref, fetch)
    assert got.name == 'Refetched'
    assert fetch.calls == 1


def test_load_or_fetch_refresh_forces_refetch(data_dir: Path) -> None:
    raw = _sample_raw()
    write_cache(raw)
    fetch = _Counter(raw.model_copy(update={'name': 'Forced'}))
    got = load_or_fetch(raw.source, raw.source_ref, fetch, refresh=True)
    assert got.name == 'Forced'
    assert fetch.calls == 1


def test_load_or_fetch_permanent_ignores_ttl(data_dir: Path) -> None:
    # A paste-sourced record: stale by TTL, but permanent -> served without fetch.
    stale = _sample_raw(source='plaintext').model_copy(
        update={'fetched_at': datetime.now(tz=UTC) - _PULL_TTL - timedelta(hours=1)}
    )
    write_cache(stale)
    fetch = _Counter(stale.model_copy(update={'name': 'nope'}))
    got = load_or_fetch(stale.source, stale.source_ref, fetch, permanent=True)
    assert got.name == stale.name
    assert fetch.calls == 0


def test_load_or_fetch_permanent_still_honors_refresh(data_dir: Path) -> None:
    stale = _sample_raw(source='plaintext')
    write_cache(stale)
    fetch = _Counter(stale.model_copy(update={'name': 'Repasted'}))
    got = load_or_fetch(stale.source, stale.source_ref, fetch, permanent=True, refresh=True)
    assert got.name == 'Repasted'
    assert fetch.calls == 1


def test_load_or_fetch_permanent_refetches_after_invalidate(data_dir: Path) -> None:
    stale = _sample_raw(source='plaintext')
    write_cache(stale)
    invalidate(stale.source, stale.source_ref)
    fetch = _Counter(stale.model_copy(update={'name': 'Repasted'}))
    got = load_or_fetch(stale.source, stale.source_ref, fetch, permanent=True)
    assert got.name == 'Repasted'
    assert fetch.calls == 1


# --------------------------------------------------------------------------- #
# _normalize_rawdeck — pure RawDeck -> Deck (no resolver)
# --------------------------------------------------------------------------- #


def test_normalize_builds_deck_with_roles_and_quantities() -> None:
    from pipeline.sources.deck_import import _normalize_rawdeck

    deck = _normalize_rawdeck(_sample_raw())
    assert deck.name == 'Test Deck'
    by_name = {c.name: c for c in deck.cards}
    assert by_name['Mountain'].quantity == 30
    assert by_name['Krenko, Mob Boss'].role == 'commander'
    assert [c.name for c in deck.commanders] == ['Krenko, Mob Boss']
    assert [c.name for c in deck.sideboard] == ['Lightning Bolt']
    # name-only DeckCards: no enrichment resolved.
    assert by_name['Mountain'].oracle_id is None


def test_normalize_passes_duplicate_names_through_unmerged() -> None:
    # F3: two same-named entries yield two DeckCards — qty aggregation is a
    # resolver/store concern, not this shape stage's.
    from pipeline.sources.deck_import import _normalize_rawdeck

    raw = RawDeck(
        name='Dup',
        cards=[
            RawEntry(name='Forest', quantity=1, role=None),
            RawEntry(name='Forest', quantity=1, role=None),
        ],
        source='plaintext',
        source_ref='dup',
        fetched_at=datetime.now(tz=UTC),
    )
    deck = _normalize_rawdeck(raw)
    assert [c.name for c in deck.cards] == ['Forest', 'Forest']


def test_normalize_unknown_role_raises() -> None:
    from pydantic import ValidationError

    from pipeline.sources.deck_import import _normalize_rawdeck

    bad = RawDeck(
        name='Bad',
        cards=[RawEntry(name='X', quantity=1, role='wishboard')],
        source='plaintext',
        source_ref='k',
        fetched_at=datetime.now(tz=UTC),
    )
    with pytest.raises(ValidationError):
        _normalize_rawdeck(bad)


def test_normalize_module_imports_no_resolver() -> None:
    """The importer canonicalizes shape, not card data — it must not IMPORT the resolver.

    Assert by construction: no imported name in the module references the resolver
    seam. (The docstrings legitimately *mention* the read-path resolver, so a raw
    text scan would be a false positive — this checks the module namespace + its
    AST imports instead.)
    """
    import ast
    import inspect

    # No resolver symbol bound in the module namespace.
    assert not any('resolve' in name.lower() for name in vars(deck_import)), vars(deck_import).keys()

    # No import statement pulls a resolver module/name.
    tree = ast.parse(inspect.getsource(deck_import))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or '')
            imported += [alias.name for alias in node.names]
    assert not any('resolv' in name.lower() for name in imported), imported
