"""OFFLINE tests for the INLINE derived-column write on collection mutations (#5, 5b-2).

When a card is ADDED or UPDATED in the owned inventory in airtable mode, the
adapter — AFTER persisting the owned facts — writes the nine Scryfall-DERIVED
columns for THAT ONE card via the 5b-1 primitive, INLINE, following the
mutation's apply semantics (apply=True, not dry-run). The write reuses the
adapter's existing httpx connection and is best-effort (fail-open) so it can
never break the collection mutation.

Safety proofs (task guardrails):
    (a) `add_card` (new + existing) in airtable-mode writes the derived cols;
    (c) LOCAL-mode owned/chase add makes ZERO Airtable calls (LocalYamlStore has
        no such hook — structural + the primitive self-guards on the backend);
    (d) a pure resolve / hydrate-on-read (list_inventory) makes NO derived write;
    (e) a resolve-failure on the card does NOT break the owned mutation (facts
        still persist);
    (f) disjointness — the inline write never touches a human/owned field.

No real network: every request is served by an in-memory httpx.MockTransport;
the card-dim resolver + live price are stubs.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from pipeline import config
from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore
from pipeline.contracts import Card
from pipeline.destinations import airtable as wb

# --------------------------------------------------------------------------- #
# Schema: the derived cols + the human owned facts, all on Inventory Cards, so
# the wrong-table binding (denylist subset) is satisfied and the derived ids
# resolve. Field ids are synthetic but internally consistent.
# --------------------------------------------------------------------------- #

_INV_FIELDS: dict[str, str] = {
    # human / owned facts (the denylist)
    'Card Name': 'fldName',
    'Number Owned': 'fldOwned',
    'Foil Count': 'fldFoil',
    'Condition': 'fldCondition',
    'Sets': 'fldSets',
    'Sources': 'fldSources',
    'Decks': 'fldDecks',
    'Number in Decks': 'fldNumDecks',
    'Number in Library': 'fldNumLib',
    'Repeat Number in Decks': 'fldRepeatDecks',
    # the nine derived cols (the allowlist)
    'Card Type': 'fldType',
    'Mana Cost': 'fldMana',
    'CMC': 'fldCmc',
    'Power / Toughness': 'fldPT',
    'Oracle Text': 'fldText',
    'Card Art': 'fldArt',
    'Scryfall URL': 'fldUrl',
    'Price (TCGPlayer)': 'fldPrice',
    'Color Identity': 'fldColor',
    # the two engine ⚙ fields (present so ensure never needs to create)
    f'{wb.NS}Buckets': 'fldBuckets',
    f'{wb.NS}Otags': 'fldOtags',
}

_OTHER_TABLES: dict[str, dict[str, str]] = {
    'Decks': {'Name': 'fldDeckName'},
    'Trades': {'Date': 'fldDate'},
    'Chase Cards': {'Card Name': 'fldChaseName', 'Target Decks': 'fldTargetDecks'},
}


def _meta_payload() -> dict[str, Any]:
    tables = [
        {
            'id': 'tblCards',
            'name': 'Inventory Cards',
            'fields': [{'id': fid, 'name': name} for name, fid in _INV_FIELDS.items()],
        }
    ]
    for name, fields in _OTHER_TABLES.items():
        tables.append(
            {
                'id': f'tbl{name.replace(" ", "")}',
                'name': name,
                'fields': [{'id': fid, 'name': fname} for fname, fid in fields.items()],
            }
        )
    return {'tables': tables}


_SOL_RING = Card(
    name='Sol Ring',
    oracle_id='oid-sol',
    mana_value=1.0,
    mana_cost='{1}',
    type_line='Artifact',
    color_identity=[],
    oracle_text='{T}: Add {C}{C}.',
    art_crop='https://img.scryfall.io/sol.jpg',
    scryfall_uri='https://scryfall.com/card/sol',
    set_name='Commander',
)


class StubResolver:
    def __init__(self, cards: dict[str, Card]) -> None:
        self._cards = cards
        self.seen: list[str] = []

    def get_card(self, name: str) -> Card | None:
        self.seen.append(name)
        return self._cards.get(name)


class StubPrice:
    def __init__(self, prices: dict[str, str]) -> None:
        self._prices = prices
        self.seen: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.seen.append(name)
        return self._prices.get(name)


class FakeAirtable:
    """In-memory Airtable served by an httpx.MockTransport; records every request."""

    def __init__(self, cards: list[dict[str, Any]] | None = None) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {'tblCards': list(cards or [])}
        self.requests: list[httpx.Request] = []
        self._next = 0

    def _new_id(self) -> str:
        self._next += 1
        return f'recNew{self._next}'

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parsed = urlparse(str(request.url))
        parts = [p for p in parsed.path.split('/') if p]
        if 'meta' in parts:
            return httpx.Response(200, json=_meta_payload())
        table_id = parts[2]
        records = self.tables.setdefault(table_id, [])
        if request.method == 'GET':
            return httpx.Response(200, json={'records': records})
        if request.method == 'POST':
            body = json.loads(request.content)
            rec = {'id': self._new_id(), 'fields': body.get('fields', {})}
            records.append(rec)
            return httpx.Response(200, json=rec)
        if request.method == 'PATCH':
            body = json.loads(request.content)
            updated = []
            for upd in body.get('records', []):
                for r in records:
                    if r['id'] == upd['id']:
                        r['fields'].update(upd.get('fields', {}))
                        updated.append(r)
            return httpx.Response(200, json={'records': updated})
        return httpx.Response(405, json={'error': 'nope'})


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _airtable_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, 'INTER_CHUNK_DELAY_S', 0)


def _store(
    fake: FakeAirtable,
    *,
    writes_enabled: bool = True,
    resolver: StubResolver | None = None,
    price: StubPrice | None = None,
) -> AirtableCollectionStore:
    transport = httpx.MockTransport(fake.handler)
    client = httpx.Client(transport=transport)
    store = AirtableCollectionStore.from_settings('fake-token', writes_enabled=writes_enabled, client=client)
    # Inject the card-dim resolver + live price so the inline write is offline.
    store._derived_resolver = resolver or StubResolver({'Sol Ring': _SOL_RING})
    store._derived_price_fetcher = price or StubPrice({'Sol Ring': '1.25'})
    return store


def _patched_card_bodies(fake: FakeAirtable) -> list[dict[str, Any]]:
    """All PATCH bodies sent to the Cards table (the derived write path)."""
    out: list[dict[str, Any]] = []
    for r in fake.requests:
        if r.method == 'PATCH' and 'tblCards' in str(r.url):
            out.append(json.loads(r.content))
    return out


def _derived_write_bodies(fake: FakeAirtable) -> list[dict[str, Any]]:
    """PATCH bodies from the inline derived-write client.

    The derived-write client (``AllowlistWriteClient``) sends a body WITHOUT
    ``typecast`` (it sets only ``returnFieldsByFieldId``), whereas the record-CRUD
    owned-facts update path always sets ``typecast=True``. So the derived writes
    are exactly the field-id-keyed PATCHes that carry NO ``typecast``."""
    out: list[dict[str, Any]] = []
    for r in fake.requests:
        if r.method != 'PATCH' or 'tblCards' not in str(r.url):
            continue
        body = json.loads(r.content)
        if body.get('returnFieldsByFieldId') and 'typecast' not in body:
            out.append(body)
    return out


# --------------------------------------------------------------------------- #
# (a) add_card writes the derived cols (new + existing) in airtable mode.
# --------------------------------------------------------------------------- #


def test_add_card_new_writes_derived_columns_inline() -> None:
    fake = FakeAirtable(cards=[])
    resolver = StubResolver({'Sol Ring': _SOL_RING})
    price = StubPrice({'Sol Ring': '1.25'})
    _store(fake, resolver=resolver, price=price).add_card('Sol Ring', 2, condition=['NM'])

    # The owned facts were POSTed first.
    posts = [json.loads(r.content) for r in fake.requests if r.method == 'POST' and 'tblCards' in str(r.url)]
    assert len(posts) == 1
    assert posts[0]['fields'][_INV_FIELDS['Card Name']] == 'Sol Ring'
    assert posts[0]['fields'][_INV_FIELDS['Number Owned']] == 2

    # Then the derived columns were PATCHed onto the new record, keyed by field id.
    derived = _derived_write_bodies(fake)
    assert len(derived) == 1
    rec = derived[0]['records'][0]
    assert rec['id'] == 'recNew1'
    derived_ids = {_INV_FIELDS[n] for n in wb.DERIVED_CARD_FIELDS}
    assert set(rec['fields']).issubset(derived_ids)
    # The card dim + live price WERE consulted for that card.
    assert resolver.seen == ['Sol Ring']
    assert price.seen == ['Sol Ring']


def test_add_card_existing_refreshes_derived_columns_inline() -> None:
    existing = {'id': 'recSol', 'fields': {_INV_FIELDS['Card Name']: 'Sol Ring', _INV_FIELDS['Number Owned']: 3}}
    fake = FakeAirtable(cards=[existing])
    _store(fake).add_card('Sol Ring', 1)
    derived = _derived_write_bodies(fake)
    assert len(derived) == 1
    assert derived[0]['records'][0]['id'] == 'recSol'


def test_set_quantity_new_writes_derived_columns_inline() -> None:
    fake = FakeAirtable(cards=[])
    _store(fake).set_quantity('Sol Ring', 4)
    derived = _derived_write_bodies(fake)
    assert len(derived) == 1
    assert derived[0]['records'][0]['id'] == 'recNew1'


def test_set_quantity_existing_refreshes_derived_columns_inline() -> None:
    existing = {'id': 'recSol', 'fields': {_INV_FIELDS['Card Name']: 'Sol Ring', _INV_FIELDS['Number Owned']: 3}}
    fake = FakeAirtable(cards=[existing])
    _store(fake).set_quantity('Sol Ring', 9)
    derived = _derived_write_bodies(fake)
    assert len(derived) == 1
    assert derived[0]['records'][0]['id'] == 'recSol'


# --------------------------------------------------------------------------- #
# (f) disjointness — the inline write never touches a human / owned field.
# --------------------------------------------------------------------------- #


def test_inline_derived_write_never_touches_a_human_field() -> None:
    fake = FakeAirtable(cards=[])
    _store(fake).add_card('Sol Ring', 2)
    human_ids = {_INV_FIELDS[n] for n in wb._human_denylist() if n in _INV_FIELDS}
    for body in _derived_write_bodies(fake):
        for rec in body['records']:
            assert set(rec['fields']).isdisjoint(human_ids)


# --------------------------------------------------------------------------- #
# (e) a resolve-failure on the card does NOT break the owned mutation.
# --------------------------------------------------------------------------- #


def test_resolve_failure_fails_open_owned_facts_still_persist() -> None:
    fake = FakeAirtable(cards=[])
    # The resolver does NOT know the card -> the derived write finds no card.
    resolver = StubResolver({})
    _store(fake, resolver=resolver).add_card('Mystery Card', 5)
    # Owned facts STILL persisted (the POST happened, the mutation did not raise).
    posts = [json.loads(r.content) for r in fake.requests if r.method == 'POST' and 'tblCards' in str(r.url)]
    assert len(posts) == 1
    assert posts[0]['fields'][_INV_FIELDS['Number Owned']] == 5
    # No derived PATCH was issued (the card was skipped by the primitive), and no
    # exception propagated.
    assert _derived_write_bodies(fake) == []


def test_transport_error_on_derived_write_fails_open() -> None:
    """A transport error during the inline derived write is swallowed; the owned
    mutation stands."""
    fake = FakeAirtable(cards=[])
    store = _store(fake)

    # Force the derived writer to blow up on use.
    class _Boom:
        def list_fields(self) -> dict[str, str]:
            raise httpx.ConnectError('boom')

        def patch_records(self, records: list[dict[str, Any]]) -> Any:  # pragma: no cover
            raise httpx.ConnectError('boom')

        def create_field(self, *a: Any, **k: Any) -> None:  # pragma: no cover
            pass

    store._derived_writer = _Boom()
    # Must not raise despite the writer exploding.
    store.add_card('Sol Ring', 1)
    posts = [r for r in fake.requests if r.method == 'POST' and 'tblCards' in str(r.url)]
    assert len(posts) == 1


# --------------------------------------------------------------------------- #
# (d) a pure resolve / hydrate-on-read makes NO derived write.
# --------------------------------------------------------------------------- #


def test_pure_read_makes_no_derived_write() -> None:
    existing = {'id': 'recSol', 'fields': {_INV_FIELDS['Card Name']: 'Sol Ring', _INV_FIELDS['Number Owned']: 3}}
    fake = FakeAirtable(cards=[existing])
    store = _store(fake, writes_enabled=False)
    # A read hydrates from the row; it must NOT trigger any derived write.
    cards = store.list_inventory()
    assert [c.name for c in cards] == ['Sol Ring']
    assert not any(r.method in {'POST', 'PATCH', 'DELETE'} for r in fake.requests)
    # The derived writer was never even constructed on a read path.
    assert store._derived_writer is None


# --------------------------------------------------------------------------- #
# (c) LOCAL mode: the owned/chase path makes ZERO Airtable calls.
# --------------------------------------------------------------------------- #


def test_local_store_has_no_derived_hook_and_no_airtable(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """In local mode `get_store` builds the YAML adapter, which has no inline
    derived-write hook — a card add touches only local YAML, never Airtable."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)

    from pipeline.collection.adapters.local_yaml import LocalYamlStore
    from pipeline.collection.store import get_store

    store = get_store(writes_enabled=True)
    assert isinstance(store, LocalYamlStore)
    # Structural proof: the local adapter has NO derived-write hook at all.
    assert not hasattr(store, '_write_derived_inline')

    # Any transport the local adapter might reach would be a bug; prove the primitive
    # self-guards too — a direct call in local mode is a strict no-op (zero calls).
    report = wb.write_derived_fields(
        {'recX': 'Sol Ring'},
        resolver=StubResolver({'Sol Ring': _SOL_RING}),
        price_fetcher=StubPrice({'Sol Ring': '1.25'}),
        client=None,
        apply=True,
        dry_run=False,
    )
    assert report.skipped_no_airtable is True
    assert report.write_requests_issued == 0


# --------------------------------------------------------------------------- #
# Connection reuse: the inline write shares the adapter's httpx client.
# --------------------------------------------------------------------------- #


def test_inline_write_reuses_adapter_httpx_client() -> None:
    fake = FakeAirtable(cards=[])
    store = _store(fake)
    store.add_card('Sol Ring', 1)  # builds the derived writer lazily
    writer = store._derived_writer
    assert writer is not None
    # The guarded writer's private httpx client IS the adapter's shared client.
    shared = store._client.httpx_client
    assert writer._AllowlistWriteClient__client is shared
