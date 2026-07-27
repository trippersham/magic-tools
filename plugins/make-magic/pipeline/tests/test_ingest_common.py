"""TDD tests for the pure ingest primitives (``ingest/_common.py``).

Everything here is OFFLINE and pure: the watermark store is a JSON file under
the env-overridden data root; ``dedupe`` is a pure function over rows. No
network, no HTTP, no store/DuckDB — just the primitives the pullers build on.

Coverage:
    - Watermark: first-run has no watermark (None); write then read round-trips
      per source; distinct sources are independent; a corrupt/missing file reads
      as empty (fail-open, never raises).
    - is_newer: skip-if-not-newer semantics on both ISO timestamps and opaque
      etag/token strings (equal -> not newer; changed -> newer; no prior -> newer).
    - dedupe: keeps last-wins by key, preserves first-seen order, is stable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.ingest import _common


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store (and thus the watermark file) at an isolated tmp root."""
    root = tmp_path / 'data'
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(root))
    return root


# --------------------------------------------------------------------------- #
# Watermark read/write
# --------------------------------------------------------------------------- #


def test_first_run_has_no_watermark(data_dir: Path) -> None:
    wm = _common.Watermark.load()
    assert wm.get('oracle_tags') is None


def test_write_then_read_roundtrips(data_dir: Path) -> None:
    wm = _common.Watermark.load()
    wm.set('oracle_tags', '2026-07-26T21:00:38.932+00:00')
    wm.save()

    reloaded = _common.Watermark.load()
    assert reloaded.get('oracle_tags') == '2026-07-26T21:00:38.932+00:00'


def test_distinct_sources_are_independent(data_dir: Path) -> None:
    wm = _common.Watermark.load()
    wm.set('oracle_tags', 'tok-A')
    wm.set('combos', 'tok-B')
    wm.save()

    reloaded = _common.Watermark.load()
    assert reloaded.get('oracle_tags') == 'tok-A'
    assert reloaded.get('combos') == 'tok-B'
    assert reloaded.get('scryfall_bulk') is None


def test_corrupt_watermark_file_reads_as_empty(data_dir: Path) -> None:
    # A garbage file must not crash a puller — fail-open to "no watermark".
    path = _common.Watermark.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{ not valid json ', encoding='utf-8')

    wm = _common.Watermark.load()
    assert wm.get('oracle_tags') is None


def test_missing_watermark_file_reads_as_empty(data_dir: Path) -> None:
    assert not _common.Watermark.path().exists()
    wm = _common.Watermark.load()
    assert wm.get('anything') is None


# --------------------------------------------------------------------------- #
# is_newer — the skip-if-not-newer gate
# --------------------------------------------------------------------------- #


def test_is_newer_no_prior_is_always_newer() -> None:
    assert _common.is_newer(None, '2026-07-26T21:00:38Z') is True
    assert _common.is_newer(None, 'any-etag') is True


def test_is_newer_equal_token_is_not_newer() -> None:
    assert _common.is_newer('tok-A', 'tok-A') is False
    assert _common.is_newer('2026-07-26T21:00:38.932+00:00', '2026-07-26T21:00:38.932+00:00') is False


def test_is_newer_changed_token_is_newer() -> None:
    assert _common.is_newer('tok-A', 'tok-B') is True


def test_is_newer_iso_timestamp_ordering() -> None:
    older = '2026-07-25T00:00:00+00:00'
    newer = '2026-07-26T00:00:00+00:00'
    assert _common.is_newer(older, newer) is True
    # An older incoming timestamp is NOT newer -> skip.
    assert _common.is_newer(newer, older) is False


def test_is_newer_none_incoming_is_never_newer() -> None:
    # If the source gives us nothing to compare, don't claim it's newer.
    assert _common.is_newer('tok-A', None) is False
    assert _common.is_newer(None, None) is False


# --------------------------------------------------------------------------- #
# dedupe — append-dedupe by key
# --------------------------------------------------------------------------- #


def test_dedupe_by_key_last_wins() -> None:
    rows = [
        {'id': 'a', 'v': 1},
        {'id': 'b', 'v': 2},
        {'id': 'a', 'v': 3},  # later duplicate of 'a' wins
    ]
    out = _common.dedupe(rows, key='id')
    assert out == [
        {'id': 'a', 'v': 3},
        {'id': 'b', 'v': 2},
    ]


def test_dedupe_preserves_first_seen_order() -> None:
    rows = [
        {'id': 'z', 'v': 1},
        {'id': 'y', 'v': 2},
        {'id': 'z', 'v': 9},
        {'id': 'x', 'v': 3},
    ]
    out = _common.dedupe(rows, key='id')
    assert [r['id'] for r in out] == ['z', 'y', 'x']


def test_dedupe_empty_and_no_dupes() -> None:
    assert _common.dedupe([], key='id') == []
    rows = [{'id': 'a'}, {'id': 'b'}]
    assert _common.dedupe(rows, key='id') == rows


def test_dedupe_callable_key() -> None:
    rows = [
        {'a': 1, 'b': 2},
        {'a': 1, 'b': 3},
    ]
    out = _common.dedupe(rows, key=lambda r: r['a'])
    assert out == [{'a': 1, 'b': 3}]
