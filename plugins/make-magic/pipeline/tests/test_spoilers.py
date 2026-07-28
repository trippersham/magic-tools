"""OFFLINE tests for the migrated spoiler lineage (source + transform).

Proves the SQLite-free replacement for ``spoiler_cache.db``:
    - ``sources.spoilers.sync`` scrapes MythicSpoiler HTML (mocked httpx) and
      lands ``raw/spoilers``; a re-scrape append-dedupes on ``(set_code, slug)``.
    - MythicSpoiler-unreachable degrades to the last snapshot (fail-open, I5).
    - ``transforms.spoilers`` reconciles slug -> oracle_id via a MOCKED resolver;
      "new since last sync" derives from the lake (current vs. prior normalized),
      NOT a SQLite meta table.
    - ``load_spoilers`` reads back the Spoiler contract for the CLI façade.

No network: httpx is monkeypatched; the resolver is a stub.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pipeline import store
from pipeline.contracts import Card
from pipeline.sources import spoilers as src_spoilers
from pipeline.transforms import spoilers as tr_spoilers


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(root))
    return root


# --------------------------------------------------------------------------- #
# Source: scrape -> raw/spoilers, append-dedupe, fail-open.
# --------------------------------------------------------------------------- #


def _raw_rows() -> list[tuple[str, str]]:
    with store.connect() as conn:
        return store.read_parquet(conn, 'raw', 'spoilers').select('slug, set_code').order('1').fetchall()


def test_sync_scrapes_and_lands_raw(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_scrape_set(client, base, sc):
        return [
            {'slug': 'alpha', 'image_url': 'i/a', 'set_code': sc, 'detail_url': 'd/a'},
            {'slug': 'beta', 'image_url': 'i/b', 'set_code': sc, 'detail_url': 'd/b'},
        ]

    monkeypatch.setattr(src_spoilers, 'scrape_set', fake_scrape_set)
    monkeypatch.setattr(src_spoilers, 'scrape_new', lambda c, b, t: [])
    monkeypatch.setattr(src_spoilers.time, 'sleep', lambda _s: None)

    path = src_spoilers.sync(['eoe'], client=httpx.Client())
    assert path.exists()
    assert store.table_exists('raw', 'spoilers')
    assert _raw_rows() == [('alpha', 'eoe'), ('beta', 'eoe')]

    # The spoiler cursor advanced (first_seen_cursor source).
    from pipeline.sources._common import Cursor

    assert Cursor.load().get('spoilers') is not None


def test_sync_append_dedupes_across_runs(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(src_spoilers, 'scrape_new', lambda c, b, t: [])
    monkeypatch.setattr(src_spoilers.time, 'sleep', lambda _s: None)

    monkeypatch.setattr(
        src_spoilers,
        'scrape_set',
        lambda c, b, sc: [{'slug': 'alpha', 'image_url': 'i/a', 'set_code': sc, 'detail_url': 'd/a'}],
    )
    src_spoilers.sync(['eoe'], client=httpx.Client())

    # Second run scrapes a NEW slug; alpha must persist (append-dedupe).
    monkeypatch.setattr(
        src_spoilers,
        'scrape_set',
        lambda c, b, sc: [
            {'slug': 'alpha', 'image_url': 'i/a2', 'set_code': sc, 'detail_url': 'd/a'},
            {'slug': 'gamma', 'image_url': 'i/g', 'set_code': sc, 'detail_url': 'd/g'},
        ],
    )
    src_spoilers.sync(['eoe'], client=httpx.Client())
    assert _raw_rows() == [('alpha', 'eoe'), ('gamma', 'eoe')]


def test_sync_fail_open_keeps_last_snapshot(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(src_spoilers, 'scrape_new', lambda c, b, t: [])
    monkeypatch.setattr(src_spoilers.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(
        src_spoilers,
        'scrape_set',
        lambda c, b, sc: [{'slug': 'alpha', 'image_url': 'i/a', 'set_code': sc, 'detail_url': 'd/a'}],
    )
    src_spoilers.sync(['eoe'], client=httpx.Client())

    # Now MythicSpoiler is unreachable — must NOT crash, must keep the snapshot.
    def boom(client, base, sc):
        raise httpx.ConnectError('network down')

    monkeypatch.setattr(src_spoilers, 'scrape_set', boom)
    path = src_spoilers.sync(['eoe'], client=httpx.Client())
    assert path.exists()
    assert _raw_rows() == [('alpha', 'eoe')]  # last snapshot preserved


# --------------------------------------------------------------------------- #
# Transform: reconcile via a MOCKED resolver + new-since-last-sync from the lake.
# --------------------------------------------------------------------------- #


class _StubResolver:
    """Resolves a fixed name->Card map; misses everything else."""

    def __init__(self, cards: dict[str, Card]) -> None:
        self._cards = cards
        self.calls: list[str] = []

    def get_card(self, name: str) -> Card | None:
        self.calls.append(name)
        return self._cards.get(name)


def _land_raw(rows: list[dict[str, str]]) -> None:
    src_spoilers._load(rows)


def test_reconcile_links_slug_to_oracle_id(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(src_spoilers, 'scrape_new', lambda c, b, t: [])
    monkeypatch.setattr(src_spoilers.time, 'sleep', lambda _s: None)
    _land_raw(
        [
            {'slug': 'solRing', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'},
            {'slug': 'mysteryCard', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'},
        ]
    )
    # slug_to_name_guess("solRing") -> "sol Ring"
    resolver = _StubResolver({'sol Ring': Card(name='Sol Ring', oracle_id='oid-sol')})

    path, new_slugs = tr_spoilers.build(resolver=resolver)
    assert path.exists()
    assert sorted(new_slugs) == ['mysteryCard', 'solRing']  # both new (no prior)

    spoilers = {s.slug: s for s in tr_spoilers.load_spoilers()}
    assert spoilers['solRing'].oracle_id == 'oid-sol'
    assert spoilers['solRing'].confirmed is True
    assert spoilers['solRing'].name == 'Sol Ring'
    assert spoilers['solRing'].source == 'scryfall'
    # Unreconciled preview stays unconfirmed, oracle_id None.
    assert spoilers['mysteryCard'].confirmed is False
    assert spoilers['mysteryCard'].oracle_id is None
    assert spoilers['mysteryCard'].source == 'mythicspoiler'


def test_new_since_last_sync_from_lake(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _StubResolver({})

    # First run: two previews -> both new.
    _land_raw(
        [
            {'slug': 'alpha', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'},
            {'slug': 'beta', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'},
        ]
    )
    _, new1 = tr_spoilers.build(resolver=resolver)
    assert sorted(new1) == ['alpha', 'beta']

    # Second run: alpha + a NEW gamma. Only gamma is new-since-last-sync (from the
    # lake's prior normalized/spoilers — no SQLite meta table involved).
    _land_raw(
        [
            {'slug': 'alpha', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'},
            {'slug': 'gamma', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'},
        ]
    )
    _, new2 = tr_spoilers.build(resolver=resolver)
    assert new2 == ['gamma']


def test_first_seen_cursor_preserved_across_runs(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _StubResolver({})
    from pipeline.sources._common import Cursor

    cursor = Cursor.load()
    cursor.set('spoilers', 'cursor-run-1')
    cursor.save()
    _land_raw([{'slug': 'alpha', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'}])
    tr_spoilers.build(resolver=resolver)
    first_cursor = tr_spoilers.load_spoilers()[0].first_seen_cursor
    assert first_cursor == 'cursor-run-1'

    # Cursor advances; alpha's first_seen_cursor must NOT change (seen before).
    cursor.set('spoilers', 'cursor-run-2')
    cursor.save()
    _land_raw([{'slug': 'alpha', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'}])
    tr_spoilers.build(resolver=resolver)
    assert tr_spoilers.load_spoilers()[0].first_seen_cursor == 'cursor-run-1'


def test_load_spoilers_filters(data_dir: Path) -> None:
    _land_raw(
        [
            {'slug': 'a', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'},
            {'slug': 'b', 'image_url': 'i', 'set_code': 'tdm', 'detail_url': 'd'},
        ]
    )
    tr_spoilers.build(resolver=_StubResolver({}))
    assert {s.slug for s in tr_spoilers.load_spoilers(set_code='eoe')} == {'a'}
    # both unconfirmed -> --new returns both
    assert {s.slug for s in tr_spoilers.load_spoilers(only_new=True)} == {'a', 'b'}


def test_reconcile_is_idempotent_on_rerun(data_dir: Path) -> None:
    _land_raw([{'slug': 'a', 'image_url': 'i', 'set_code': 'eoe', 'detail_url': 'd'}])
    resolver = _StubResolver({})
    tr_spoilers.build(resolver=resolver)
    _, new = tr_spoilers.build(resolver=resolver)  # same raw -> nothing new
    assert new == []
