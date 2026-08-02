"""OFFLINE tests for the per-source pullers (parse/load + fail-open + safety).

No network: HTTP is monkeypatched to return canned payloads. Each puller's
parse/load is asserted by reading the loaded Parquet back through the store. The
oracle_tags fail-open path is proven by making the mocked fetch RAISE and
asserting the bundled snapshot loads. The Airtable safety property (GET-only) is
proven by (a) a unit test that the request wrapper rejects POST/PATCH/DELETE and
(b) a mocked list-records pull that loads rows while only ever issuing GET.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import config, store
from pipeline.sources import airtable, oracle_tags, scryfall_bulk, spellbook


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Settings is an lru_cached singleton; read env fresh for each test."""
    config.get_settings.cache_clear()


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(root))
    return root


def _rows(layer: str, name: str) -> list[tuple[Any, ...]]:
    with store.connect() as conn:
        return store.read_parquet(conn, layer, name).order('1').fetchall()


# --------------------------------------------------------------------------- #
# oracle_tags: parse/load + cursor advance + fail-open to snapshot
# --------------------------------------------------------------------------- #

_TAGS_PAYLOAD = [
    {
        'id': 't1',
        'label': 'removal',
        'slug': 'removal',
        'type': 'oracle',
        'parent_ids': [],
        'child_ids': ['t2'],
        'taggings': [],
    },
    {
        'id': 't2',
        'label': 'sweeper',
        'slug': 'sweeper',
        'type': 'oracle',
        'parent_ids': ['t1'],
        'child_ids': [],
        'taggings': [{'oracle_id': 'oid-a', 'weight': 'median'}],
    },
]


def test_oracle_tags_parse_and_load(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_meta(client: httpx.Client) -> tuple[str, str]:
        return 'http://x/uri', '2026-07-26T21:00:00+00:00'

    def fake_payload(client: httpx.Client, uri: str) -> list[dict[str, Any]]:
        return _TAGS_PAYLOAD

    monkeypatch.setattr(oracle_tags, '_fetch_meta', fake_meta)
    monkeypatch.setattr(oracle_tags, '_fetch_payload', fake_payload)
    path = oracle_tags.sync(client=httpx.Client())

    assert path.exists()
    assert store.table_exists('raw', 'oracle_tags')
    with store.connect() as conn:
        n = store.read_parquet(conn, 'raw', 'oracle_tags').aggregate('count(*)').fetchone()[0]
    assert n == 2

    # cursor advanced
    from pipeline.sources._common import Cursor

    assert Cursor.load().get('oracle_tags') == '2026-07-26T21:00:00+00:00'


def test_oracle_tags_skips_when_not_newer(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.sources._common import Cursor

    cursor = Cursor.load()
    cursor.set('oracle_tags', '2026-07-26T21:00:00+00:00')
    cursor.save()

    calls = {'load': 0, 'payload': 0}
    real_load = oracle_tags._load

    def counting_load(tags: list[dict[str, Any]]) -> Path:
        calls['load'] += 1
        return real_load(tags)

    # Cheap meta says: same updated_at as the stored cursor -> not newer -> skip.
    def fake_meta(client: httpx.Client) -> tuple[str, str]:
        return 'http://x/uri', '2026-07-26T21:00:00+00:00'

    # The ~18 MB payload download MUST NOT run on a not-newer run.
    def counting_payload(client: httpx.Client, uri: str) -> list[dict[str, Any]]:
        calls['payload'] += 1
        return _TAGS_PAYLOAD

    monkeypatch.setattr(oracle_tags, '_fetch_meta', fake_meta)
    monkeypatch.setattr(oracle_tags, '_fetch_payload', counting_payload)
    monkeypatch.setattr(oracle_tags, '_load', counting_load)

    # First load a table so the skip branch can early-return it.
    real_load(_TAGS_PAYLOAD)
    oracle_tags.sync(client=httpx.Client())
    assert calls['load'] == 0  # skipped — not newer
    assert calls['payload'] == 0  # the big download was NOT issued


def test_oracle_tags_fail_open_to_snapshot(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(client: httpx.Client) -> tuple[str, str]:
        raise httpx.ConnectError('network down')

    monkeypatch.setattr(oracle_tags, '_fetch_meta', boom)
    # Must NOT raise — falls back to the bundled snapshot.
    path = oracle_tags.sync(client=httpx.Client())
    assert path.exists()
    assert store.table_exists('raw', 'oracle_tags')
    with store.connect() as conn:
        n = store.read_parquet(conn, 'raw', 'oracle_tags').aggregate('count(*)').fetchone()[0]
    # The bundled snapshot has the full 4,499-tag DAG.
    assert n == 4499


# --------------------------------------------------------------------------- #
# spellbook: parse/load + fail-open
# --------------------------------------------------------------------------- #

_COMBOS_PAYLOAD = [
    {'id': 'c1', 'identity': 'U', 'status': 'OK'},
    {'id': 'c2', 'identity': 'BG', 'status': 'OK'},
    {'id': 'c1', 'identity': 'U', 'status': 'OK'},  # duplicate id -> deduped
]


def test_spellbook_parse_and_load_dedupes(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spellbook, '_remote_cursor', lambda c: 'etag-1')
    monkeypatch.setattr(spellbook, '_fetch_remote', lambda c, m: _COMBOS_PAYLOAD)

    path = spellbook.sync(client=httpx.Client())
    assert path.exists()
    with store.connect() as conn:
        n = store.read_parquet(conn, 'raw', 'combos').aggregate('count(*)').fetchone()[0]
    assert n == 2  # c1 deduped


def test_spellbook_fail_open_to_snapshot(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(client: httpx.Client) -> str | None:
        raise httpx.ConnectError('down')

    monkeypatch.setattr(spellbook, '_remote_cursor', boom)
    path = spellbook.sync(client=httpx.Client())
    assert path.exists()
    with store.connect() as conn:
        n = store.read_parquet(conn, 'raw', 'combos').aggregate('count(*)').fetchone()[0]
    assert n == 2000  # bundled snapshot size


# --------------------------------------------------------------------------- #
# scryfall_bulk: streaming parse + bounded cap + projection
# --------------------------------------------------------------------------- #


def test_scryfall_bulk_streams_and_projects(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cards = [
        {
            'oracle_id': 'o1',
            'name': 'Llanowar Elves',
            'cmc': 1.0,
            'type_line': 'Creature — Elf Druid',
            'colors': ['G'],
            'color_identity': ['G'],
            'produced_mana': ['G'],
            'keywords': [],
            'power': '1',
            'toughness': '1',
            'image_uris': {'art_crop': 'https://cards.scryfall.io/art_crop/front/6/a/6a0b230b.jpg'},
            'scryfall_uri': 'https://scryfall.com/card/fdn/227/llanowar-elves',
            'set_name': 'Foundations',
            'extra': 'dropped',
        },
        {
            'oracle_id': 'o2',
            'name': 'Sol Ring',
            'cmc': 1.0,
            'type_line': 'Artifact',
            'colors': [],
            'color_identity': [],
            'produced_mana': ['C'],
            'keywords': [],
            'scryfall_uri': 'https://scryfall.com/card/c21/263/sol-ring',
            'set_name': 'Commander 2021',
        },
    ]

    monkeypatch.setattr(
        scryfall_bulk,
        '_fetch_meta',
        lambda c: ('http://x/uri', '2026-07-26T00:00:00+00:00'),
    )
    monkeypatch.setattr(
        scryfall_bulk,
        '_stream_cards',
        lambda c, uri, cap: iter(cards[:cap] if cap else cards),
    )

    path = scryfall_bulk.sync(client=httpx.Client(), max_cards=2)
    assert path.exists()
    with store.connect() as conn:
        rel = store.read_parquet(conn, 'raw', 'oracle_cards')
        n = rel.aggregate('count(*)').fetchone()[0]
        cols = rel.columns
        # The creature row exposes the widened presentation columns non-null.
        elves = rel.filter("name = 'Llanowar Elves'").fetchone()
        row = dict(zip(cols, elves, strict=True))
    assert n == 2
    assert 'extra' not in cols  # projected away
    assert 'oracle_id' in cols and 'produced_mana' in cols
    # W1: widened presentation columns present.
    for col in ('power', 'toughness', 'art_crop', 'scryfall_uri', 'set_name'):
        assert col in cols
    assert row['power'] == '1'
    assert row['toughness'] == '1'
    assert row['art_crop'] == 'https://cards.scryfall.io/art_crop/front/6/a/6a0b230b.jpg'
    assert row['scryfall_uri'] == 'https://scryfall.com/card/fdn/227/llanowar-elves'
    assert row['set_name'] == 'Foundations'


def test_scryfall_bulk_stream_decoder_parses_gzipped_jsonl() -> None:
    # Exercise the real streaming decoder over chunked GZIPPED JSONL (Scryfall's current format).
    import gzip
    import json

    cards = [{'oracle_id': 'a', 'name': 'A'}, {'oracle_id': 'b', 'name': 'B'}, {'oracle_id': 'c', 'name': 'C'}]
    body = gzip.compress(('\n'.join(json.dumps(c) for c in cards)).encode('utf-8'))

    class FakeResp:
        def raise_for_status(self) -> None: ...
        def iter_bytes(self):
            # feed the gzip bytes in awkward 7-byte chunks to test buffered inflate + line-splitting
            for i in range(0, len(body), 7):
                yield body[i : i + 7]

    class FakeStream:
        def __enter__(self):
            return FakeResp()

        def __exit__(self, *a):
            return False

    class FakeClient:
        def stream(self, *a, **k):
            return FakeStream()

    out = list(scryfall_bulk._stream_cards(FakeClient(), 'uri', max_cards=2))  # type: ignore[arg-type]
    assert [c['oracle_id'] for c in out] == ['a', 'b']  # capped at 2


# --------------------------------------------------------------------------- #
# Airtable safety: GET-only guard + mocked pull loads rows with no writes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('method', ['POST', 'PATCH', 'PUT', 'DELETE', 'post', 'patch'])
def test_airtable_client_rejects_non_get(method: str) -> None:
    client = airtable.GetOnlyClient('fake-token', _client=httpx.Client())
    with pytest.raises(airtable.NonGetMethodError):
        client.request(method, 'https://api.airtable.com/v0/x/y')


def test_airtable_get_is_allowed_and_routes_through_guard() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['method'] = request.method
        return httpx.Response(200, json={'records': []})

    transport = httpx.MockTransport(handler)
    client = airtable.GetOnlyClient('tok', _client=httpx.Client(transport=transport))
    resp = client.get('https://api.airtable.com/v0/base/tbl')
    assert resp.status_code == 200
    assert seen['method'] == 'GET'


#: Canned base schema for the mocked meta API — table NAMES -> ids/field ids.
#: Mirrors the env-driven default table names (Cards/Decks/Trades/Chase Cards).
_META_TABLES = {
    'tables': [
        {'id': 'tblDECKS', 'name': 'Decks', 'fields': [{'id': 'fldName', 'name': 'Name'}]},
        {
            'id': 'tblCHASE',
            'name': 'Chase Cards',
            'fields': [{'id': 'fldtLastMod', 'name': 'Last Modified'}],
        },
    ]
}


def test_airtable_pull_loads_rows_and_only_issues_get(
    data_dir: Path,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if '/meta/bases/' in str(request.url):  # schema resolution (name -> id)
            return httpx.Response(200, json=_META_TABLES)
        return httpx.Response(
            200,
            json={
                'records': [
                    {
                        'id': 'rec1',
                        'createdTime': '2026-01-01T00:00:00Z',
                        'fields': {'fldName': 'Sol Ring', 'fldLinks': ['recX', 'recY']},
                    },
                    {
                        'id': 'rec2',
                        'createdTime': '2026-01-02T00:00:00Z',
                        'fields': {'fldName': 'Lightning Bolt'},
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = airtable.GetOnlyClient('tok', _client=httpx.Client(transport=transport))

    path = airtable.run_table('decks', client=client, force=True)
    assert path.exists()
    with store.connect() as conn:
        n = store.read_parquet(conn, 'raw', 'airtable_decks').aggregate('count(*)').fetchone()[0]
    assert n == 2
    # THE PROOF: every request issued was a GET (including schema resolution).
    assert methods and all(m == 'GET' for m in methods)


def test_airtable_sync_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    with pytest.raises(RuntimeError, match='AIRTABLE_API_KEY'):
        airtable.sync()
