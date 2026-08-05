"""OFFLINE tests for the inline chase derived-column write on chase mutations.

When a card is added or updated on the Chase Cards table in airtable mode, the
adapter — after persisting the chase facts (Card Name + Target Decks) — writes the
nine Chase Cards Scryfall-derived columns for that one card via the guarded
primitive, inline, following the mutation's apply semantics (apply=True, not dry-run).

Against the live base (tblXsNtGgT7UQLPXZ): the Chase Cards table carries the same
eleven engine-derived columns as Inventory Cards — the nine Scryfall-pure columns
(Card Type, Mana Cost, CMC, Power / Toughness, Oracle Text, Card Art, Scryfall URL,
Price (TCGPlayer), Color Identity — including a live-sourced price) plus the two
engine ⚙ otag fields (⚙ Buckets / ⚙ Otags). Unlike Inventory (whose ⚙ come from the
separate otag sync), Chase has no otag sync, so this inline write is chase's only path
to ⚙. The write reuses the adapter's existing httpx connection and is best-effort
(fail-open) so it can never break the chase mutation.

Safety proofs (task guardrails):
    (a) `add_chase` (new + existing) in airtable-mode writes all eleven chase derived
        cols — the nine Scryfall + a live price, ⚙ Buckets as a list of bucket names,
        ⚙ Otags as newline-joined text — keyed by chase field id;
    (a2) a card with empty otag_buckets/otags writes empty ⚙ (empty list / empty
        string) — no crash;
    (b) the chase write never touches a chase human field (Card Name / Target Decks /
        Sets / Priority / Status / Target Price) — the chase guard fails closed;
    (c) local-mode chase add makes zero Airtable calls (the primitive self-guards
        on the backend);
    (d) a pure read (list_chase) makes no chase derived write;
    (e) a resolve-failure fails open (chase facts still persist).

No real network: every request is served by an in-memory httpx.MockTransport;
the card-dim resolver and the live-price fetcher are stubs.
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
# Schema: the Chase Cards table has the NINE derived cols + its human/system
# fields (Card Name, Sets, Target Decks, Price Last Updated, …). Field ids are
# synthetic but internally consistent. The Decks table carries a deck so the
# Target Decks link resolves.
# --------------------------------------------------------------------------- #

_CHASE_FIELDS: dict[str, str] = {
    # chase human / system / link fields (never written)
    'Card Name': 'fldChaseName',
    'Sets': 'fldChaseSets',
    'Target Decks': 'fldTargetDecks',
    'Price Last Updated': 'fldChasePriceUpd',
    # the eleven chase derived cols (the chase allowlist) — identical to the
    # Inventory engine-derived set: nine Scryfall + the two engine ⚙ otag fields.
    'Card Type': 'fldChaseType',
    'Mana Cost': 'fldChaseMana',
    'CMC': 'fldChaseCmc',
    'Power / Toughness': 'fldChasePT',
    'Oracle Text': 'fldChaseText',
    'Card Art': 'fldChaseArt',
    'Scryfall URL': 'fldChaseUrl',
    'Price (TCGPlayer)': 'fldChasePrice',
    'Color Identity': 'fldChaseColor',
    f'{wb.NS}Buckets': 'fldChaseBuckets',
    f'{wb.NS}Otags': 'fldChaseOtags',
}

# The Inventory Cards table is still present in the base (the owned inline write
# path resolves against it); include its human fields so the OWNED wrong-table
# guard is satisfied for any incidental resolution, and Decks so links resolve.
_INV_FIELDS: dict[str, str] = {
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
    'Card Type': 'fldType',
    'Mana Cost': 'fldMana',
    'CMC': 'fldCmc',
    'Power / Toughness': 'fldPT',
    'Oracle Text': 'fldText',
    'Card Art': 'fldArt',
    'Scryfall URL': 'fldUrl',
    'Price (TCGPlayer)': 'fldPrice',
    'Color Identity': 'fldColor',
    f'{wb.NS}Buckets': 'fldBuckets',
    f'{wb.NS}Otags': 'fldOtags',
}

_TABLES: dict[str, tuple[str, dict[str, str]]] = {
    'Inventory Cards': ('tblCards', _INV_FIELDS),
    'Chase Cards': ('tblChase', _CHASE_FIELDS),
    'Decks': ('tblDecks', {'Name': 'fldDeckName'}),
    'Trades': ('tblTrades', {'Date': 'fldDate'}),
}

_LIVE_PRICE = '3.14'


def _meta_payload() -> dict[str, Any]:
    tables = []
    for name, (tid, fields) in _TABLES.items():
        tables.append(
            {
                'id': tid,
                'name': name,
                'fields': [{'id': fid, 'name': fname} for fname, fid in fields.items()],
            }
        )
    return {'tables': tables}


#: Otag facts for Sol Ring, populated in P2 (the resolver Card carries them). The
#: chase inline write sources ⚙ Buckets from ``otag_buckets`` (multipleSelects list)
#: and ⚙ Otags from ``otags`` (newline-joined multilineText) — SAME formatting the
#: Inventory otag sync (build_payload) uses.
_SOL_BUCKETS = ['ramp']
_SOL_OTAGS = ['mana-rock', 'ramp']

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
    otag_buckets=list(_SOL_BUCKETS),
    otags=list(_SOL_OTAGS),
)


class StubResolver:
    def __init__(self, cards: dict[str, Card]) -> None:
        self._cards = cards
        self.seen: list[str] = []

    def get_card(self, name: str) -> Card | None:
        self.seen.append(name)
        return self._cards.get(name)


class StubPriceFetcher:
    """A LIVE-price fetcher double: records the names it priced, returns a fixed price."""

    def __init__(self, price: str | None = _LIVE_PRICE) -> None:
        self._price = price
        self.seen: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.seen.append(name)
        return self._price


class FakeAirtable:
    """In-memory Airtable served by an httpx.MockTransport; records every request."""

    def __init__(self, chase: list[dict[str, Any]] | None = None) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {'tblChase': list(chase or [])}
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
    price_fetcher: StubPriceFetcher | None = None,
) -> AirtableCollectionStore:
    transport = httpx.MockTransport(fake.handler)
    client = httpx.Client(transport=transport)
    store = AirtableCollectionStore.from_settings('fake-token', writes_enabled=writes_enabled, client=client)
    store._derived_resolver = resolver or StubResolver({'Sol Ring': _SOL_RING})
    # Inject a stub LIVE-price fetcher so the chase derived write never hits network.
    store._derived_price_fetcher = price_fetcher or StubPriceFetcher()
    return store


def _chase_derived_write_bodies(fake: FakeAirtable) -> list[dict[str, Any]]:
    """PATCH bodies from the inline CHASE derived-write client.

    The derived-write client (``AllowlistWriteClient``) sends a body WITHOUT
    ``typecast`` (only ``returnFieldsByFieldId``), whereas the record-CRUD chase
    update path always sets ``typecast=True``. So the chase derived writes are
    exactly the field-id-keyed PATCHes to the Chase table that carry NO
    ``typecast``."""
    out: list[dict[str, Any]] = []
    for r in fake.requests:
        if r.method != 'PATCH' or 'tblChase' not in str(r.url):
            continue
        body = json.loads(r.content)
        if body.get('returnFieldsByFieldId') and 'typecast' not in body:
            out.append(body)
    return out


# --------------------------------------------------------------------------- #
# (a) add_chase writes ALL ELEVEN chase derived cols — nine Scryfall + a live price
#     + ⚙ Buckets (list) + ⚙ Otags (text) (new + existing).
# --------------------------------------------------------------------------- #


def test_add_chase_new_writes_chase_derived_columns_inline() -> None:
    fake = FakeAirtable(chase=[])
    resolver = StubResolver({'Sol Ring': _SOL_RING})
    price = StubPriceFetcher()
    note = _store(fake, resolver=resolver, price_fetcher=price).add_chase('Sol Ring')
    assert note is None

    # The chase facts were POSTed first.
    posts = [json.loads(r.content) for r in fake.requests if r.method == 'POST' and 'tblChase' in str(r.url)]
    assert len(posts) == 1
    assert posts[0]['fields'][_CHASE_FIELDS['Card Name']] == 'Sol Ring'

    # Then ALL ELEVEN chase derived columns were PATCHed onto the new record.
    derived = _chase_derived_write_bodies(fake)
    assert len(derived) == 1
    rec = derived[0]['records'][0]
    assert rec['id'] == 'recNew1'
    chase_derived_ids = {_CHASE_FIELDS[n] for n in wb.CHASE_DERIVED_CARD_FIELDS}
    assert set(rec['fields']) == chase_derived_ids
    # The eleven == the nine Inventory Scryfall cols + the two ⚙ otag fields. This is
    # the deliberate asymmetry: Inventory's inline write emits nine (its ⚙ come from
    # the separate otag sync); chase has no sync so its inline write emits eleven.
    gear_fields = {f'{wb.NS}Buckets', f'{wb.NS}Otags'}
    expected_chase_derived = wb.DERIVED_CARD_FIELDS | gear_fields
    assert expected_chase_derived == wb.CHASE_DERIVED_CARD_FIELDS
    assert wb.CHASE_DERIVED_CARD_FIELDS != wb.DERIVED_CARD_FIELDS  # asymmetry
    assert len(chase_derived_ids) == 11
    # The LIVE price landed in Price (TCGPlayer).
    assert rec['fields'][_CHASE_FIELDS['Price (TCGPlayer)']] == float(_LIVE_PRICE)  # currency: float, not str
    # ⚙ Buckets is a LIST of bucket names (multipleSelects); ⚙ Otags is the raw slugs
    # newline-joined into text — the SAME formatting the Inventory otag sync uses.
    assert rec['fields'][_CHASE_FIELDS[f'{wb.NS}Buckets']] == _SOL_BUCKETS
    assert rec['fields'][_CHASE_FIELDS[f'{wb.NS}Otags']] == '\n'.join(_SOL_OTAGS)
    # The card dim AND the live-price fetcher were consulted for that card.
    assert resolver.seen == ['Sol Ring']
    assert price.seen == ['Sol Ring']


def test_add_chase_existing_refreshes_chase_derived_columns_inline() -> None:
    existing = {'id': 'recSol', 'fields': {_CHASE_FIELDS['Card Name']: 'Sol Ring'}}
    fake = FakeAirtable(chase=[existing])
    # Update path: attach a deck so an update PATCH fires too, then derived write.
    fake.tables['tblDecks'] = [{'id': 'recDeckA', 'fields': {'fldDeckName': 'Ramp Deck'}}]
    _store(fake).add_chase('Sol Ring', for_deck='Ramp Deck')
    derived = _chase_derived_write_bodies(fake)
    assert len(derived) == 1
    rec = derived[0]['records'][0]
    assert rec['id'] == 'recSol'
    chase_derived_ids = {_CHASE_FIELDS[n] for n in wb.CHASE_DERIVED_CARD_FIELDS}
    assert set(rec['fields']) == chase_derived_ids
    assert rec['fields'][_CHASE_FIELDS['Price (TCGPlayer)']] == float(_LIVE_PRICE)  # currency: float, not str
    assert rec['fields'][_CHASE_FIELDS[f'{wb.NS}Buckets']] == _SOL_BUCKETS
    assert rec['fields'][_CHASE_FIELDS[f'{wb.NS}Otags']] == '\n'.join(_SOL_OTAGS)


# --------------------------------------------------------------------------- #
# (a2) a card with EMPTY otag_buckets/otags writes EMPTY ⚙ (no crash).
# --------------------------------------------------------------------------- #


def test_add_chase_empty_otags_writes_empty_gear_fields() -> None:
    """A resolved card carrying NO otag data still writes all eleven cols; the two ⚙
    fields land EMPTY (empty list / empty string) — fail-open and valid, no crash."""
    bare = Card(
        name='Bare Card',
        oracle_id='oid-bare',
        mana_value=2.0,
        mana_cost='{2}',
        type_line='Artifact',
        color_identity=[],
        oracle_text='Does nothing.',
        art_crop='https://img.scryfall.io/bare.jpg',
        scryfall_uri='https://scryfall.com/card/bare',
        set_name='Commander',
        # otag_buckets / otags default to empty lists.
    )
    fake = FakeAirtable(chase=[])
    _store(fake, resolver=StubResolver({'Bare Card': bare})).add_chase('Bare Card')
    derived = _chase_derived_write_bodies(fake)
    assert len(derived) == 1
    rec = derived[0]['records'][0]
    chase_derived_ids = {_CHASE_FIELDS[n] for n in wb.CHASE_DERIVED_CARD_FIELDS}
    assert set(rec['fields']) == chase_derived_ids  # all eleven still emitted
    assert rec['fields'][_CHASE_FIELDS[f'{wb.NS}Buckets']] == []
    assert rec['fields'][_CHASE_FIELDS[f'{wb.NS}Otags']] == ''


# --------------------------------------------------------------------------- #
# (b) the chase write never touches a chase human field.
# --------------------------------------------------------------------------- #


def test_inline_chase_write_never_touches_a_chase_human_field() -> None:
    fake = FakeAirtable(chase=[])
    _store(fake).add_chase('Sol Ring')
    human_ids = {_CHASE_FIELDS[n] for n in wb._chase_human_denylist() if n in _CHASE_FIELDS}
    # Card Name, Sets AND Target Decks must be in the denylist and never written.
    assert _CHASE_FIELDS['Card Name'] in human_ids
    assert _CHASE_FIELDS['Sets'] in human_ids
    assert _CHASE_FIELDS['Target Decks'] in human_ids
    for body in _chase_derived_write_bodies(fake):
        for rec in body['records']:
            assert set(rec['fields']).isdisjoint(human_ids)


def test_chase_guard_fails_closed_on_a_chase_human_field() -> None:
    """The CHASE guard refuses any chase human field name outright."""
    for human in ('Card Name', 'Sets', 'Target Decks'):
        with pytest.raises(wb.HumanFieldWriteError):
            wb.assert_no_chase_human_fields([human])


# --------------------------------------------------------------------------- #
# (e) a resolve-failure fails open (chase facts still persist).
# --------------------------------------------------------------------------- #


def test_chase_resolve_failure_fails_open_chase_facts_still_persist() -> None:
    fake = FakeAirtable(chase=[])
    resolver = StubResolver({})  # resolver knows nothing -> derived write finds no card
    _store(fake, resolver=resolver).add_chase('Mystery Card')
    posts = [json.loads(r.content) for r in fake.requests if r.method == 'POST' and 'tblChase' in str(r.url)]
    assert len(posts) == 1
    assert posts[0]['fields'][_CHASE_FIELDS['Card Name']] == 'Mystery Card'
    # No chase derived PATCH was issued (skipped by the primitive), nothing raised.
    assert _chase_derived_write_bodies(fake) == []


def test_chase_transport_error_on_derived_write_fails_open() -> None:
    """A transport error during the inline chase derived write is swallowed."""
    fake = FakeAirtable(chase=[])
    store = _store(fake)

    class _Boom:
        def list_fields(self) -> dict[str, str]:
            raise httpx.ConnectError('boom')

        def patch_records(self, records: list[dict[str, Any]]) -> Any:  # pragma: no cover
            raise httpx.ConnectError('boom')

        def create_field(self, *a: Any, **k: Any) -> None:  # pragma: no cover
            pass

    store._chase_derived_writer = _Boom()
    store.add_chase('Sol Ring')  # must not raise
    posts = [r for r in fake.requests if r.method == 'POST' and 'tblChase' in str(r.url)]
    assert len(posts) == 1


# --------------------------------------------------------------------------- #
# (d) a pure read makes NO chase derived write.
# --------------------------------------------------------------------------- #


def test_pure_chase_read_makes_no_derived_write() -> None:
    existing = {'id': 'recSol', 'fields': {_CHASE_FIELDS['Card Name']: 'Sol Ring'}}
    fake = FakeAirtable(chase=[existing])
    store = _store(fake, writes_enabled=False)
    cards = store.list_chase()
    assert [c.name for c in cards] == ['Sol Ring']
    assert not any(r.method in {'POST', 'PATCH', 'DELETE'} for r in fake.requests)
    assert store._chase_derived_writer is None


# --------------------------------------------------------------------------- #
# (c) LOCAL mode: the chase path makes ZERO Airtable calls.
# --------------------------------------------------------------------------- #


def test_local_chase_add_makes_no_airtable_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The local YAML adapter has no chase derived-write hook; a chase add touches
    only local YAML. The primitive self-guards too — a direct call in local mode is
    a strict no-op (zero calls)."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)

    from pipeline.collection.adapters.local_yaml import LocalYamlStore
    from pipeline.collection.store import get_store

    store = get_store(writes_enabled=True)
    assert isinstance(store, LocalYamlStore)
    assert not hasattr(store, '_write_chase_derived_inline')

    report = wb.write_chase_derived_fields(
        {'recX': 'Sol Ring'},
        resolver=StubResolver({'Sol Ring': _SOL_RING}),
        price_fetcher=StubPriceFetcher(),
        client=None,
        apply=True,
        dry_run=False,
    )
    assert report.skipped_no_airtable is True
    assert report.write_requests_issued == 0


# --------------------------------------------------------------------------- #
# Connection reuse + CHASE binding: the writer targets the Chase table.
# --------------------------------------------------------------------------- #


def test_inline_chase_write_reuses_client_and_binds_chase_table() -> None:
    fake = FakeAirtable(chase=[])
    store = _store(fake)
    store.add_chase('Sol Ring')  # builds the chase writer lazily
    writer = store._chase_derived_writer
    assert writer is not None
    # Shares the adapter's httpx client.
    assert writer._AllowlistWriteClient__client is store._client.httpx_client
    # Bound to the CHASE table + CHASE allowlist (not Inventory).
    assert writer._cards_table_name == 'Chase Cards'
    assert writer._allowlist == wb.CHASE_ALLOWLIST_NAMES
