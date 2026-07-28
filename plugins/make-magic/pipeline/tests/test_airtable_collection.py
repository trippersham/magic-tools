"""OFFLINE tests for the Airtable-records `CollectionStore` adapter (Phase 2.1).

No network, no creds: every request is served by an in-memory
``httpx.MockTransport`` that returns representative meta (schema) + record
payloads and RECORDS each request (method / URL / params / body) so a test can
assert the adapter built the right request and mapped rows <-> contracts
correctly.

Covered:
    - meta -> AirtableResolver name->id resolution (no hard-coded ids).
    - list_inventory maps an Inventory Cards row -> OwnedCard, hydrating the
      enrichment DIRECTLY from the row (no CardResolver).
    - a deck reconstruction round-trip (commander + maindeck + basics), with
      link record ids resolved to card names.
    - filterByFormula is built for name lookups.
    - a comma-containing card name survives the wire (no truncation).
    - the write guard blocks a mutating call unless writes are enabled.
    - get_store('airtable') constructs the adapter (no NotImplementedError).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from pipeline import config
from pipeline.collection.adapters.airtable_collection import (
    AirtableCollectionStore,
    ReadOnlyStoreError,
    _RecordClient,
)
from pipeline.collection.errors import CollectionError
from pipeline.config import AirtableResolver
from pipeline.contracts import Deck, DeckCard, Trade

# --------------------------------------------------------------------------- #
# Fixtures: synthetic per-base schema (meta) + record payloads.
# --------------------------------------------------------------------------- #

# tbl…/fld… ids are synthetic but internally consistent.
_TBL = {
    'Inventory Cards': 'tblCards',
    'Decks': 'tblDecks',
    'Trades': 'tblTrades',
    'Chase Cards': 'tblChase',
}

# field NAME -> field id, per table.
_FIELDS: dict[str, dict[str, str]] = {
    'Inventory Cards': {
        'Card Name': 'fldCardName',
        'Number Owned': 'fldOwned',
        'Foil Count': 'fldFoil',
        'Condition': 'fldCondition',
        'Sets': 'fldSets',
        'Sources': 'fldSources',
        'Card Type': 'fldType',
        'CMC': 'fldCmc',
        'Mana Cost': 'fldManaCost',
        'Oracle Text': 'fldOracle',
        'Color Identity': 'fldColorId',
    },
    # Matches the REAL live base: Decks has Strategy but NO Assessment / Focus
    # Otags columns. (A separate test ADDS those to exercise the write-error and
    # tolerance paths.)
    'Decks': {
        'Name': 'fldDeckName',
        'Strategy': 'fldStrategy',
        'Commander': 'fldCommander',
        'Cards': 'fldCards',
        'Repeat Cards Count': 'fldRepeat',
        'Plains': 'fldPlains',
        'Islands': 'fldIslands',
        'Swamps': 'fldSwamps',
        'Mountains': 'fldMountains',
        'Forests': 'fldForests',
        'Wastes': 'fldWastes',
    },
    'Trades': {
        'Date': 'fldDate',
        'From (Source)': 'fldFromSrc',
        'To (Destination)': 'fldToDst',
        'From (Deck)': 'fldFromDeck',
        'To (Deck)': 'fldToDeck',
        'Cards into Destination': 'fldCardsIn',
        'Cards out of Destination': 'fldCardsOut',
        'Status': 'fldStatus',
        'Completed Date': 'fldCompleted',
        'Reason': 'fldReason',
        'Notes': 'fldNotes',
    },
    'Chase Cards': {
        'Card Name': 'fldChaseName',
        'Card Type': 'fldChaseType',
        'CMC': 'fldChaseCmc',
        'Mana Cost': 'fldChaseManaCost',
        'Oracle Text': 'fldChaseOracle',
        'Color Identity': 'fldChaseColorId',
        'Target Decks': 'fldTargetDecks',
    },
}


def _meta_payload() -> dict[str, Any]:
    return {
        'tables': [
            {
                'id': _TBL[name],
                'name': name,
                'fields': [{'id': fid, 'name': fname} for fname, fid in fields.items()],
            }
            for name, fields in _FIELDS.items()
        ]
    }


class FakeAirtable:
    """A stateful in-memory Airtable that an httpx.MockTransport dispatches to.

    Holds one records list per table id (field-id-keyed ``fields``) and records
    every request it serves so tests can assert method/URL/params/body.
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = tables or {tid: [] for tid in _TBL.values()}
        self.requests: list[httpx.Request] = []
        self._next_id = 0

    def _new_id(self) -> str:
        self._next_id += 1
        return f'recNew{self._next_id}'

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parsed = urlparse(str(request.url))
        parts = [p for p in parsed.path.split('/') if p]
        # meta: /v0/meta/bases/{base}/tables
        if 'meta' in parts:
            return httpx.Response(200, json=_meta_payload())
        # records: /v0/{base}/{table}[/{record}]
        table_id = parts[2]
        records = self.tables.setdefault(table_id, [])
        if request.method == 'GET':
            qs = parse_qs(parsed.query)
            formula = qs.get('filterByFormula', [None])[0]
            out = records
            if formula:
                out = [r for r in records if _matches_formula(r, formula)]
            return httpx.Response(200, json={'records': out})
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
        if request.method == 'DELETE':
            qs = parse_qs(parsed.query)
            to_del = set(qs.get('records[]', []))
            self.tables[table_id] = [r for r in records if r['id'] not in to_del]
            return httpx.Response(200, json={'records': [{'id': rid, 'deleted': True} for rid in to_del]})
        return httpx.Response(405, json={'error': 'method not allowed'})


def _matches_formula(rec: dict[str, Any], formula: str) -> bool:
    """Crude ``{Field} = 'value'`` matcher against a field-id-keyed record.

    The adapter builds formulas with field NAMES; the fake maps the name back to
    the field id via the schema to find the value.
    """
    # formula like: {Card Name} = 'Sol Ring'
    if '=' not in formula:
        return True
    lhs, rhs = formula.split('=', 1)
    field_name = lhs.strip().strip('{}').strip()
    want = rhs.strip().strip("'").replace("\\'", "'")
    # find the field id for this name across tables.
    for fields in _FIELDS.values():
        fid = fields.get(field_name)
        if fid and fid in rec['fields']:
            return str(rec['fields'][fid]) == want
    return False


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    config.get_settings.cache_clear()


def _store(fake: FakeAirtable, *, writes_enabled: bool = False) -> AirtableCollectionStore:
    transport = httpx.MockTransport(fake.handler)
    client = httpx.Client(transport=transport)
    return AirtableCollectionStore.from_settings('fake-token', writes_enabled=writes_enabled, client=client)


# --------------------------------------------------------------------------- #
# Meta / resolver
# --------------------------------------------------------------------------- #


def test_resolver_resolves_names_via_meta() -> None:
    fake = FakeAirtable()
    transport = httpx.MockTransport(fake.handler)
    client = _RecordClient('t', base_id='appX', client=httpx.Client(transport=transport))
    resolver = AirtableResolver(client, base_id='appX')
    assert resolver.table_id('Inventory Cards') == 'tblCards'
    assert resolver.field_id('Inventory Cards', 'Card Name') == 'fldCardName'


def test_backend_name() -> None:
    assert _store(FakeAirtable()).backend_name == 'airtable'


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


def _inv_row(rid: str, name: str, **overrides: Any) -> dict[str, Any]:
    f = _FIELDS['Inventory Cards']
    fields = {
        f['Card Name']: name,
        f['Number Owned']: 3,
        f['Foil Count']: 1,
        f['Condition']: ['NM'],
        f['Sets']: ['C21'],
        f['Sources']: ['Commander 2021'],
        f['Card Type']: 'Artifact',
        f['CMC']: 1,
        f['Mana Cost']: '{1}',
        f['Oracle Text']: '{T}: Add {C}{C}.',
        f['Color Identity']: [],
    }
    fields.update(overrides)
    return {'id': rid, 'fields': fields}


def test_list_inventory_maps_row_to_owned_card_with_enrichment() -> None:
    fake = FakeAirtable({'tblCards': [_inv_row('recSol', 'Sol Ring')]})
    [card] = _store(fake).list_inventory()
    assert card.name == 'Sol Ring'
    assert card.owned == 3
    assert card.foil == 1
    assert card.condition == ['NM']
    assert card.sets == ['C21']
    assert card.sources == ['Commander 2021']
    # enrichment hydrated directly from the row (no CardResolver).
    assert card.type_line == 'Artifact'
    assert card.mana_value == 1
    assert card.mana_cost == '{1}'
    assert card.oracle_text == '{T}: Add {C}{C}.'
    assert card.airtable_record_id == 'recSol'


def test_list_inventory_requests_by_field_id() -> None:
    fake = FakeAirtable({'tblCards': [_inv_row('recSol', 'Sol Ring')]})
    _store(fake).list_inventory()
    getreq = [r for r in fake.requests if r.method == 'GET' and 'meta' not in str(r.url)]
    assert getreq, 'expected a record GET'
    assert 'returnFieldsByFieldId=true' in str(getreq[-1].url)


def test_add_card_new_record_builds_field_id_payload() -> None:
    fake = FakeAirtable({'tblCards': []})
    _store(fake, writes_enabled=True).add_card('Sol Ring', 2, condition=['NM'])
    posts = [r for r in fake.requests if r.method == 'POST']
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    f = _FIELDS['Inventory Cards']
    assert body['fields'][f['Card Name']] == 'Sol Ring'
    assert body['fields'][f['Number Owned']] == 2
    assert body['fields'][f['Condition']] == ['NM']


def test_add_card_existing_increments_owned() -> None:
    fake = FakeAirtable({'tblCards': [_inv_row('recSol', 'Sol Ring')]})
    _store(fake, writes_enabled=True).add_card('Sol Ring', 2)
    patches = [r for r in fake.requests if r.method == 'PATCH']
    assert len(patches) == 1
    body = json.loads(patches[0].content)
    f = _FIELDS['Inventory Cards']
    assert body['records'][0]['fields'][f['Number Owned']] == 5  # 3 + 2


def test_add_card_uses_filter_by_formula_lookup() -> None:
    fake = FakeAirtable({'tblCards': [_inv_row('recSol', 'Sol Ring')]})
    _store(fake, writes_enabled=True).add_card('Sol Ring', 1)
    lookups = [r for r in fake.requests if r.method == 'GET' and 'filterByFormula' in str(r.url)]
    assert lookups, 'expected a filterByFormula name lookup'
    assert 'Card+Name' in str(lookups[0].url) or 'Card%20Name' in str(lookups[0].url)


def test_set_quantity_updates_existing() -> None:
    fake = FakeAirtable({'tblCards': [_inv_row('recSol', 'Sol Ring')]})
    _store(fake, writes_enabled=True).set_quantity('Sol Ring', 9)
    body = json.loads(next(r for r in fake.requests if r.method == 'PATCH').content)
    assert body['records'][0]['fields'][_FIELDS['Inventory Cards']['Number Owned']] == 9


def test_remove_card_deletes() -> None:
    fake = FakeAirtable({'tblCards': [_inv_row('recSol', 'Sol Ring')]})
    _store(fake, writes_enabled=True).remove_card('Sol Ring')
    assert any(r.method == 'DELETE' for r in fake.requests)
    assert fake.tables['tblCards'] == []


def test_comma_containing_card_name_survives_the_wire() -> None:
    name = 'Grumgully, the Generous'
    fake = FakeAirtable({'tblCards': []})
    _store(fake, writes_enabled=True).add_card(name, 1)
    body = json.loads(next(r for r in fake.requests if r.method == 'POST').content)
    assert body['fields'][_FIELDS['Inventory Cards']['Card Name']] == name
    # round-trips back with the comma intact.
    [card] = _store(fake).list_inventory()
    assert card.name == name


# --------------------------------------------------------------------------- #
# Deck reconstruction
# --------------------------------------------------------------------------- #


def _deck_fixture() -> FakeAirtable:
    inv = _FIELDS['Inventory Cards']
    cards = [
        {'id': 'recGrum', 'fields': {inv['Card Name']: 'Grumgully, the Generous'}},
        {'id': 'recSol', 'fields': {inv['Card Name']: 'Sol Ring'}},
        {'id': 'recLlan', 'fields': {inv['Card Name']: 'Llanowar Elves'}},
    ]
    d = _FIELDS['Decks']
    deck = {
        'id': 'recDeck',
        'fields': {
            d['Name']: 'Gruul Aggro',
            d['Strategy']: 'Go-wide aggro.',
            d['Commander']: ['recGrum'],
            d['Cards']: ['recSol', 'recLlan'],
            d['Forests']: 8,
            d['Mountains']: 5,
            d['Repeat Cards Count']: 0,
        },
    }
    return FakeAirtable({'tblCards': cards, 'tblDecks': [deck]})


def test_deck_reconstruction_round_trip() -> None:
    deck = _store(_deck_fixture()).get_deck('Gruul Aggro')
    assert deck.name == 'Gruul Aggro'
    assert deck.strategy == 'Go-wide aggro.'
    # The live base has NO Assessment / Focus Otags columns -> read degrades.
    assert deck.assessment is None
    assert deck.focus_otags == []
    assert deck.airtable_record_id == 'recDeck'
    # commander resolved from link -> role=commander.
    assert [c.name for c in deck.commanders] == ['Grumgully, the Generous']
    by_name = {c.name: c for c in deck.cards}
    # maindeck links resolved to names.
    assert 'Sol Ring' in by_name
    assert by_name['Sol Ring'].role is None
    assert 'Llanowar Elves' in by_name
    # basic-land counts -> DeckCard per nonzero count with that quantity.
    assert by_name['Forest'].quantity == 8
    assert by_name['Mountain'].quantity == 5


def test_list_decks_reconstructs_all() -> None:
    decks = _store(_deck_fixture()).list_decks()
    assert [d.name for d in decks] == ['Gruul Aggro']
    assert [c.name for c in decks[0].commanders] == ['Grumgully, the Generous']


def test_set_assessment_and_focus_otags_write_field_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a base DOES carry Assessment / Focus Otags, the setters write them.

    The live base lacks these columns, so they are added to the schema here to
    exercise the (schema-permitting) write path.
    """
    augmented = {**_FIELDS['Decks'], 'Assessment': 'fldAssessment', 'Focus Otags': 'fldFocus'}
    monkeypatch.setitem(_FIELDS, 'Decks', augmented)
    fake = _deck_fixture()
    store = _store(fake, writes_enabled=True)
    store.set_assessment('Gruul Aggro', 'New reality.')
    store.set_focus_otags('Gruul Aggro', ['ramp'])
    patches = [json.loads(r.content) for r in fake.requests if r.method == 'PATCH']
    assert any(p['records'][0]['fields'].get('fldAssessment') == 'New reality.' for p in patches)
    assert any(p['records'][0]['fields'].get('fldFocus') == ['ramp'] for p in patches)


def test_set_assessment_on_base_without_column_raises() -> None:
    """The live base lacks an Assessment column -> writing it raises loudly."""
    fake = _deck_fixture()
    store = _store(fake, writes_enabled=True)
    with pytest.raises(config.AirtableConfigError):
        store.set_assessment('Gruul Aggro', 'x')


def test_set_strategy_missing_deck_raises() -> None:
    with pytest.raises(FileNotFoundError):
        _store(FakeAirtable({'tblDecks': []}), writes_enabled=True).set_strategy('Nope', 'x')


# --------------------------------------------------------------------------- #
# Chase / Trades
# --------------------------------------------------------------------------- #


def test_list_chase_maps_row() -> None:
    c = _FIELDS['Chase Cards']
    fake = FakeAirtable(
        {'tblChase': [{'id': 'recCh', 'fields': {c['Card Name']: 'The One Ring', c['Card Type']: 'Artifact'}}]}
    )
    [card] = _store(fake).list_chase()
    assert card.name == 'The One Ring'
    assert card.type_line == 'Artifact'
    assert card.airtable_record_id == 'recCh'


def test_add_chase_creates_by_name() -> None:
    fake = FakeAirtable({'tblChase': []})
    _store(fake, writes_enabled=True).add_chase('The One Ring')
    posts = [r for r in fake.requests if r.method == 'POST']
    assert len(posts) == 1
    assert json.loads(posts[0].content)['fields'][_FIELDS['Chase Cards']['Card Name']] == 'The One Ring'


def test_list_trades_maps_row() -> None:
    t = _FIELDS['Trades']
    fake = FakeAirtable(
        {
            'tblTrades': [
                {
                    'id': 'recTr',
                    'fields': {
                        t['From (Source)']: 'Library',
                        t['To (Destination)']: 'Deck',
                        t['Status']: 'Completed',
                    },
                }
            ]
        }
    )
    [trade] = _store(fake).list_trades()
    assert trade.from_source == 'Library'
    assert trade.to_destination == 'Deck'
    assert trade.status == 'Completed'


def test_log_trade_creates_record() -> None:
    fake = FakeAirtable({'tblTrades': []})
    trade = Trade(from_source='Library', to_destination='Deck', status='Draft')
    _store(fake, writes_enabled=True).log_trade(trade)
    body = json.loads(next(r for r in fake.requests if r.method == 'POST').content)
    t = _FIELDS['Trades']
    assert body['fields'][t['From (Source)']] == 'Library'
    assert body['fields'][t['To (Destination)']] == 'Deck'
    assert body['fields'][t['Status']] == 'Draft'


def test_log_trade_notes_maps_to_notes_column() -> None:
    fake = FakeAirtable({'tblTrades': []})
    _store(fake, writes_enabled=True).log_trade(
        Trade(from_source='Store', to_destination='Library', notes='bought at LGS')
    )
    body = json.loads(next(r for r in fake.requests if r.method == 'POST').content)
    assert body['fields'][_FIELDS['Trades']['Notes']] == 'bought at LGS'


# --------------------------------------------------------------------------- #
# Fix B — save_deck writes full membership (Commander/Cards links + basics)
# --------------------------------------------------------------------------- #


def _inv_id_fixture() -> FakeAirtable:
    """Inventory Cards populated so name -> record-id link resolution succeeds."""
    inv = _FIELDS['Inventory Cards']
    cards = [
        {'id': 'recGrum', 'fields': {inv['Card Name']: 'Grumgully, the Generous'}},
        {'id': 'recSol', 'fields': {inv['Card Name']: 'Sol Ring'}},
        {'id': 'recLlan', 'fields': {inv['Card Name']: 'Llanowar Elves'}},
    ]
    return FakeAirtable({'tblCards': cards, 'tblDecks': []})


def test_save_deck_create_writes_commander_cards_and_basics() -> None:
    fake = _inv_id_fixture()
    deck = Deck(
        name='Gruul Aggro',
        strategy='Go-wide.',
        cards=[
            DeckCard(name='Grumgully, the Generous', role='commander'),
            DeckCard(name='Sol Ring'),
            DeckCard(name='Llanowar Elves'),
            DeckCard(name='Forest', quantity=8),
            DeckCard(name='Mountain', quantity=5),
        ],
    )
    _store(fake, writes_enabled=True).save_deck(deck)
    body = json.loads(next(r for r in fake.requests if r.method == 'POST' and 'tblDecks' in str(r.url)).content)
    d = _FIELDS['Decks']
    f = body['fields']
    assert f[d['Name']] == 'Gruul Aggro'
    assert f[d['Strategy']] == 'Go-wide.'
    # commander + maindeck links are record ids (basics are NOT in the Cards link).
    assert f[d['Commander']] == ['recGrum']
    assert set(f[d['Cards']]) == {'recSol', 'recLlan'}
    # basic-land counts land in the number fields.
    assert f[d['Forests']] == 8
    assert f[d['Mountains']] == 5


def test_save_deck_update_patches_existing_record() -> None:
    fake = _inv_id_fixture()
    fake.tables['tblDecks'] = [{'id': 'recDeck', 'fields': {_FIELDS['Decks']['Name']: 'Gruul Aggro'}}]
    deck = Deck(
        name='Gruul Aggro',
        airtable_record_id='recDeck',
        cards=[DeckCard(name='Sol Ring'), DeckCard(name='Grumgully, the Generous', role='commander')],
    )
    _store(fake, writes_enabled=True).save_deck(deck)
    patch = json.loads(next(r for r in fake.requests if r.method == 'PATCH').content)
    f = patch['records'][0]['fields']
    d = _FIELDS['Decks']
    assert patch['records'][0]['id'] == 'recDeck'
    assert f[d['Commander']] == ['recGrum']
    assert f[d['Cards']] == ['recSol']


def test_save_deck_unresolved_card_raises() -> None:
    fake = _inv_id_fixture()
    deck = Deck(name='Gruul Aggro', cards=[DeckCard(name='Card Not In Inventory')])
    with pytest.raises(CollectionError, match='Card Not In Inventory'):
        _store(fake, writes_enabled=True).save_deck(deck)


def test_save_deck_writes_repeat_count_for_multiples() -> None:
    fake = _inv_id_fixture()
    deck = Deck(name='Gruul Aggro', cards=[DeckCard(name='Sol Ring', quantity=3)])
    _store(fake, writes_enabled=True).save_deck(deck)
    body = json.loads(next(r for r in fake.requests if r.method == 'POST' and 'tblDecks' in str(r.url)).content)
    # 3 copies -> 2 repeats beyond the single link row.
    assert body['fields'][_FIELDS['Decks']['Repeat Cards Count']] == 2


# --------------------------------------------------------------------------- #
# Fix C — add_chase writes Target Decks; skips non-persistable fields
# --------------------------------------------------------------------------- #


def _chase_deck_fixture() -> FakeAirtable:
    d = _FIELDS['Decks']
    fake = FakeAirtable({'tblChase': [], 'tblDecks': [{'id': 'recDeck', 'fields': {d['Name']: 'Gruul Aggro'}}]})
    return fake


def test_add_chase_writes_target_decks_link() -> None:
    fake = _chase_deck_fixture()
    _store(fake, writes_enabled=True).add_chase('The One Ring', for_deck='Gruul Aggro')
    body = json.loads(next(r for r in fake.requests if r.method == 'POST' and 'tblChase' in str(r.url)).content)
    c = _FIELDS['Chase Cards']
    assert body['fields'][c['Card Name']] == 'The One Ring'
    assert body['fields'][c['Target Decks']] == ['recDeck']


def test_add_chase_existing_record_extends_target_decks() -> None:
    c = _FIELDS['Chase Cards']
    d = _FIELDS['Decks']
    fake = FakeAirtable(
        {
            'tblChase': [{'id': 'recCh', 'fields': {c['Card Name']: 'The One Ring', c['Target Decks']: ['recOld']}}],
            'tblDecks': [{'id': 'recDeck', 'fields': {d['Name']: 'Gruul Aggro'}}],
        }
    )
    _store(fake, writes_enabled=True).add_chase('The One Ring', for_deck='Gruul Aggro')
    patch = json.loads(next(r for r in fake.requests if r.method == 'PATCH').content)
    assert patch['records'][0]['fields'][c['Target Decks']] == ['recOld', 'recDeck']


def test_add_chase_skips_non_persistable_fields_without_error() -> None:
    fake = FakeAirtable({'tblChase': []})
    note = _store(fake, writes_enabled=True).add_chase(
        'The One Ring', priority=1, status='wanted', target_price=25.0
    )
    body = json.loads(next(r for r in fake.requests if r.method == 'POST').content)
    # only Card Name was written — no priority/status/price columns touched.
    assert list(body['fields'].keys()) == [_FIELDS['Chase Cards']['Card Name']]
    assert note is not None
    assert 'priority' in note and 'status' in note and 'target_price' in note


# --------------------------------------------------------------------------- #
# Fix D — log_trade writes cards_in/out + from/to deck links
# --------------------------------------------------------------------------- #


def test_log_trade_writes_card_and_deck_links() -> None:
    inv = _FIELDS['Inventory Cards']
    d = _FIELDS['Decks']
    fake = FakeAirtable(
        {
            'tblTrades': [],
            'tblCards': [
                {'id': 'recSol', 'fields': {inv['Card Name']: 'Sol Ring'}},
                {'id': 'recLlan', 'fields': {inv['Card Name']: 'Llanowar Elves'}},
            ],
            'tblDecks': [
                {'id': 'recGruul', 'fields': {d['Name']: 'Gruul Aggro'}},
                {'id': 'recMono', 'fields': {d['Name']: 'Mono Blue'}},
            ],
        }
    )
    trade = Trade(
        from_source='Deck',
        to_destination='Deck',
        from_deck='Gruul Aggro',
        to_deck='Mono Blue',
        cards_in=['Sol Ring'],
        cards_out=['Llanowar Elves'],
    )
    _store(fake, writes_enabled=True).log_trade(trade)
    body = json.loads(next(r for r in fake.requests if r.method == 'POST' and 'tblTrades' in str(r.url)).content)
    t = _FIELDS['Trades']
    assert body['fields'][t['Cards into Destination']] == ['recSol']
    assert body['fields'][t['Cards out of Destination']] == ['recLlan']
    assert body['fields'][t['From (Deck)']] == ['recGruul']
    assert body['fields'][t['To (Deck)']] == ['recMono']


def test_log_trade_unresolved_card_raises() -> None:
    fake = FakeAirtable({'tblTrades': [], 'tblCards': []})
    trade = Trade(from_source='Store', to_destination='Library', cards_in=['Ghost Card'])
    with pytest.raises(CollectionError, match='Ghost Card'):
        _store(fake, writes_enabled=True).log_trade(trade)


# --------------------------------------------------------------------------- #
# Write guard (opt-in)
# --------------------------------------------------------------------------- #


def test_write_guard_blocks_when_read_only() -> None:
    fake = FakeAirtable({'tblCards': []})
    store = _store(fake, writes_enabled=False)
    with pytest.raises(ReadOnlyStoreError):
        store.add_card('Sol Ring', 1)
    # nothing mutated the base.
    assert not any(r.method in {'POST', 'PATCH', 'DELETE'} for r in fake.requests)


def test_write_guard_blocks_log_trade_when_read_only() -> None:
    fake = FakeAirtable({'tblTrades': []})
    store = _store(fake, writes_enabled=False)
    with pytest.raises(ReadOnlyStoreError):
        store.log_trade(Trade(from_source='Library', to_destination='Deck'))


# --------------------------------------------------------------------------- #
# Schema tolerance (skill-authored fields absent on the base)
# --------------------------------------------------------------------------- #


def test_list_decks_tolerates_missing_skill_authored_fields() -> None:
    """A base whose Decks table lacks Assessment / Focus Otags must not crash.

    Regression for the live-base reality (the real base has ``Strategy`` but
    neither ``Assessment`` nor ``Focus Otags``): resolving those field ids raises
    ``AirtableConfigError``, and ``_get_optional`` must degrade the read to
    ``None`` / ``[]`` rather than exploding ``list_decks``. The default fixture
    schema now MATCHES that reality (no Assessment / Focus Otags), which is what
    the pre-fix fixture masked.
    """
    fake = FakeAirtable()
    fake.tables['tblDecks'] = [{'id': 'recDeck1', 'fields': {'fldDeckName': 'Gruul', 'fldStrategy': 'aggro'}}]
    store = _store(fake)
    decks = store.list_decks()
    assert len(decks) == 1
    assert decks[0].name == 'Gruul'
    assert decks[0].strategy == 'aggro'
    assert decks[0].assessment is None
    assert decks[0].focus_otags == []
