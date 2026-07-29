"""Pacing / retry / transient coverage for the resolver's LIVE-FALLBACK path.

#6 hardened the interim per-card `ScryfallResolver` with pacing, bounded 429/503
retry (honoring ``Retry-After``, capped), and a transient-vs-definitive
distinction. #5 replaced that interim class with the lake-backed
`DuckDBCardResolver`, folding the SAME robustness onto its live-fallback fetch
(the on-miss lookup when a name is absent from the bulk). This module KEEPS that
coverage meaningful by exercising it through `DuckDBCardResolver` with an EMPTY
lake, so every `get_card` falls through to the paced/retried live fetch.

OFFLINE: an `httpx.MockTransport` drives every branch (real card / 404 / 5xx /
connect-error / 429 throttle) so no network is touched. Covers:
    - a genuine 404 (exact + fuzzy) resolves to None and is NOT landed.
    - a transient 5xx / connect-error resolves to None, is NOT landed, and a
      subsequent lookup RETRIES (fresh network call).
    - a 429 throttle is retried WITHIN a single lookup (honoring Retry-After).
    - a hostile ``Retry-After: 3600`` is CAPPED (no hour-long mid-read hang).
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from pipeline import store
from pipeline.collection.resolver import _MAX_BACKOFF, _MAX_RETRIES, DuckDBCardResolver

_SOL_RING = {
    'name': 'Sol Ring',
    'oracle_id': 'abc',
    'cmc': 1.0,
    'type_line': 'Artifact',
    'oracle_text': '{T}: Add {C}{C}.',
    'colors': [],
    'color_identity': [],
}


@pytest.fixture()
def empty_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated data dir with NO lake tables — every lookup falls to live."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _resolver(handler) -> DuckDBCardResolver:
    # min_interval=0.0 skips real pacing sleeps so the offline suite stays fast.
    return DuckDBCardResolver(client=_client(handler), min_interval=0.0)


def _landed() -> bool:
    """True once the live-fetch landing has written the oracle_cards table."""
    return store.table_exists('raw', 'oracle_cards')


# --------------------------------------------------------------------------- #
# Transient vs definitive — only a real card is landed; a transient miss retries
# --------------------------------------------------------------------------- #


def test_404_is_definitive_not_landed(empty_lake: Path) -> None:
    """A genuine 404 (exact + fuzzy) resolves to None and lands NOTHING."""
    calls = {'n': 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return httpx.Response(404, json={'object': 'error'})

    resolver = _resolver(handler)
    assert resolver.get_card('Nonesuch') is None
    assert calls['n'] == 2  # exact then fuzzy, both 404
    assert not _landed()  # a definitive miss lands nothing


def test_transient_5xx_not_landed_and_retries(empty_lake: Path) -> None:
    """A transient 5xx returns None and is NOT landed; a SUBSEQUENT lookup (fresh
    resolver, so the in-memory cache doesn't short-circuit) RETRIES and succeeds."""
    state = {'fail': True, 'calls': 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state['calls'] += 1
        if state['fail']:
            return httpx.Response(500, json={'object': 'error'})
        return httpx.Response(200, json=_SOL_RING)

    # First lookup: transient 5xx -> None, nothing landed.
    assert _resolver(handler).get_card('Sol Ring') is None
    assert not _landed()
    calls_after_fail = state['calls']

    # Recover the backend; a fresh lookup RETRIES the network and now succeeds.
    state['fail'] = False
    card = _resolver(handler).get_card('Sol Ring')
    assert card is not None
    assert card.name == 'Sol Ring'
    assert state['calls'] > calls_after_fail  # it retried the network


def test_connect_error_not_landed_and_retries(empty_lake: Path) -> None:
    """A connect/timeout error behaves like a transient failure (nothing landed,
    a later lookup retries)."""
    state = {'fail': True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state['fail']:
            raise httpx.ConnectError('boom', request=request)
        return httpx.Response(200, json=_SOL_RING)

    assert _resolver(handler).get_card('Sol Ring') is None
    assert not _landed()

    state['fail'] = False
    assert _resolver(handler).get_card('Sol Ring') is not None


# --------------------------------------------------------------------------- #
# 429/503 throttle — retried WITHIN a single lookup (honoring Retry-After)
# --------------------------------------------------------------------------- #


def test_retries_on_429_then_succeeds(empty_lake: Path) -> None:
    """A 429 throttle is RETRIED within a single lookup (honoring Retry-After), so
    a bulk-miss burst resolves instead of collapsing to name-only. Two 429s then
    a 200 -> get_card succeeds in one call. (Dogfooding: an un-paced burst got
    429'd and left a real deck un-hydrated.)"""
    state = {'n': 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state['n'] += 1
        if state['n'] <= 2:
            return httpx.Response(429, headers={'Retry-After': '0'}, json={'object': 'error'})
        return httpx.Response(200, json=_SOL_RING)

    card = _resolver(handler).get_card('Sol Ring')
    assert card is not None
    assert card.name == 'Sol Ring'
    assert state['n'] == 3  # two 429s retried, then a 200


def test_huge_retry_after_is_capped_no_hang(empty_lake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostile ``Retry-After: 3600`` must NOT be honored verbatim (a 1-hour
    mid-read hang). Every retry sleep is capped at ``_MAX_BACKOFF``, so after the
    bounded retries the lookup RETURNS name-only (None) — quickly, not in an hour.

    ``time.sleep`` is spied (never really slept) so the OFFLINE suite stays fast
    while proving the cap: no requested sleep exceeds ``_MAX_BACKOFF`` (a raw
    ``3600`` would fail this) and wall-clock stays trivially small.

    The exact endpoint exhausts ``_MAX_RETRIES`` retries and its FINAL response is
    still a 429 (not a 404), so the fetch never advances to the fuzzy endpoint —
    ``raise_for_status`` on the 429 makes it transient. Hence exactly
    ``_MAX_RETRIES`` capped sleeps."""
    slept: list[float] = []
    monkeypatch.setattr('pipeline.collection.resolver.time.sleep', slept.append)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={'Retry-After': '3600'}, json={'object': 'error'})

    resolver = _resolver(handler)
    start = time.monotonic()
    result = resolver.get_card('Sol Ring')
    elapsed = time.monotonic() - start

    # Falls through to a transient failure after retries exhaust -> None (name-only).
    assert result is None
    # It DID retry (bounded) on the exact endpoint before giving up.
    assert len(slept) == _MAX_RETRIES
    assert max(slept) <= _MAX_BACKOFF, f'a sleep of {max(slept)}s exceeded the cap'
    # Real wall-clock is trivial (sleeps are spied) — proving no hour-long hang.
    assert elapsed < 3.0
    # A transient throttle lands NOTHING.
    assert not _landed()
