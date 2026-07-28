"""Tests for the package-native `ScryfallResolver` (Fixes 1, 4, 5).

OFFLINE: an `httpx.MockTransport` drives every branch (real card / 404 / 5xx /
connect-error) so no network is touched. Covers:
    - Fix 1: a genuine 404 is negatively-cached (persisted null, no refetch), but
      a transient 5xx / connect-error is NOT cached and RETRIES on the next lookup.
    - Fix 4: `close()` / context-manager support closes the httpx client.
    - Fix 5: the on-disk cache write is atomic (temp file + os.replace).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from pipeline.collection.resolver import ScryfallResolver

_SOL_RING = {
    'name': 'Sol Ring',
    'oracle_id': 'abc',
    'cmc': 1.0,
    'type_line': 'Artifact',
    'oracle_text': '{T}: Add {C}{C}.',
    'colors': [],
    'color_identity': [],
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Fix 1 — negative-cache only DEFINITIVE 404s, never transient failures
# --------------------------------------------------------------------------- #


def test_404_is_negatively_cached_no_refetch(tmp_path: Path) -> None:
    """A genuine 404 resolves to None, persists `null`, and a SECOND resolver
    reading the cache returns None WITHOUT a second network call."""
    cache = tmp_path / 'scryfall_names.json'
    calls = {'n': 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return httpx.Response(404, json={'object': 'error'})

    r1 = ScryfallResolver(cache_path=cache, client=_client(handler))
    assert r1.get_card('Nonesuch') is None
    r1.close()

    # Persisted as an explicit null (definitive not-found).
    assert json.loads(cache.read_text())['Nonesuch'] is None
    first_calls = calls['n']
    assert first_calls >= 1

    # A fresh resolver reads the cached null and does NOT hit the network again.
    r2 = ScryfallResolver(cache_path=cache, client=_client(handler))
    assert r2.get_card('Nonesuch') is None
    assert calls['n'] == first_calls  # no additional network calls
    r2.close()


def test_transient_error_not_cached_and_retries(tmp_path: Path) -> None:
    """A transient failure (5xx) returns None but is NOT persisted; a subsequent
    lookup RETRIES the network and can succeed."""
    cache = tmp_path / 'scryfall_names.json'
    state = {'fail': True, 'calls': 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state['calls'] += 1
        if state['fail']:
            return httpx.Response(503, json={'object': 'error'})
        return httpx.Response(200, json=_SOL_RING)

    resolver = ScryfallResolver(cache_path=cache, client=_client(handler))

    # First lookup: transient 5xx -> None, and NOTHING negatively cached.
    assert resolver.get_card('Sol Ring') is None
    if cache.exists():
        assert 'Sol Ring' not in json.loads(cache.read_text())
    calls_after_fail = state['calls']

    # Recover the backend; a subsequent lookup must RETRY (hit network again).
    state['fail'] = False
    card = resolver.get_card('Sol Ring')
    assert card is not None
    assert card.name == 'Sol Ring'
    assert state['calls'] > calls_after_fail  # it retried
    resolver.close()


def test_connect_error_not_cached_and_retries(tmp_path: Path) -> None:
    """A connect/timeout error behaves like a transient failure (no cache, retry)."""
    cache = tmp_path / 'scryfall_names.json'
    state = {'fail': True, 'calls': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state['calls'] += 1
        if state['fail']:
            raise httpx.ConnectError('boom', request=request)
        return httpx.Response(200, json=_SOL_RING)

    resolver = ScryfallResolver(cache_path=cache, client=_client(handler))
    assert resolver.get_card('Sol Ring') is None
    if cache.exists():
        assert 'Sol Ring' not in json.loads(cache.read_text())

    state['fail'] = False
    assert resolver.get_card('Sol Ring') is not None
    resolver.close()


# --------------------------------------------------------------------------- #
# Fix 4 — client lifecycle
# --------------------------------------------------------------------------- #


def test_context_manager_closes_client(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SOL_RING)

    client = _client(handler)
    with ScryfallResolver(cache_path=tmp_path / 'c.json', client=client) as resolver:
        assert resolver.get_card('Sol Ring') is not None
    assert client.is_closed


# --------------------------------------------------------------------------- #
# Fix 5 — atomic cache write (no stray temp files, no corruption)
# --------------------------------------------------------------------------- #


def test_cache_write_is_atomic(tmp_path: Path) -> None:
    cache = tmp_path / 'scryfall_names.json'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={'object': 'error'})

    resolver = ScryfallResolver(cache_path=cache, client=_client(handler))
    resolver.get_card('Whatever')
    resolver.close()

    # Final file is valid JSON and no leftover temp files linger in the dir.
    assert json.loads(cache.read_text()) == {'Whatever': None}
    leftovers = [p for p in tmp_path.iterdir() if p != cache]
    assert leftovers == []
