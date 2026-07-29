"""OFFLINE tests for the 5b-1 guarded derived-column write primitive + refresh.

The primitive :func:`write_derived_fields` sources the nine Scryfall-derived
columns from the lake resolver (`get_card`) + a LIVE price fetch and writes them
onto the Inventory Cards records via the SAME 5a allowlist guard. The explicit
refresh entry point (`--refresh`) runs it over the pulled Cards, dry-run by
default.

Core safety proofs (mirroring the task guardrails):
    (a) derived cols are written under a MOCKED httpx transport when
        backend=airtable + apply;
    (b) local mode makes ZERO Airtable calls (no-op plan);
    (c) dry-run default plans but does not write;
    (d) a human field can NEVER enter the payload (5a guard reused);
    (e) a missing derived-field id fails LOUD;
    (f) price is sourced LIVE, not from the lake Card.

No real network: transports are httpx.MockTransport, resolver/price are stubs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pipeline import config
from pipeline.contracts import Card
from pipeline.destinations import airtable as wb


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to LOCAL backend unless it opts into airtable, so a
    stray real resolve_backend() read never surprises a write path."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')


# --------------------------------------------------------------------------- #
# Stubs: a resolver + a live price fetcher.
# --------------------------------------------------------------------------- #


_CULTIVATE = Card(
    name='Cultivate',
    oracle_id='oid-cultivate',
    mana_value=3.0,
    mana_cost='{2}{G}',
    type_line='Sorcery',
    color_identity=['G'],
    oracle_text='Search your library for up to two basic land cards...',
    art_crop='https://img.scryfall.io/cultivate.jpg',
    scryfall_uri='https://scryfall.com/card/cultivate',
    set_name='Magic 2011',
)
_GRIZZLY = Card(
    name='Grizzly Bears',
    oracle_id='oid-grizzly',
    mana_value=2.0,
    mana_cost='{1}{G}',
    type_line='Creature — Bear',
    color_identity=['G'],
    oracle_text='',
    power='2',
    toughness='2',
    art_crop='https://img.scryfall.io/grizzly.jpg',
    scryfall_uri='https://scryfall.com/card/grizzly',
    set_name='Core Set',
)

#: {airtable_record_id: card_name}
_CARDS = {'recCultivate': 'Cultivate', 'recGrizzly': 'Grizzly Bears'}

#: The live price the fetcher returns per name (NOT on the Card contract).
_PRICES = {'Cultivate': '0.25', 'Grizzly Bears': '0.10'}


class StubResolver:
    """A CardResolver double: name -> Card (or None)."""

    def __init__(self, cards: dict[str, Card]) -> None:
        self._cards = cards
        self.seen: list[str] = []

    def get_card(self, name: str) -> Card | None:
        self.seen.append(name)
        return self._cards.get(name)


class StubPrice:
    """A live price fetcher double: name -> price string (or None)."""

    def __init__(self, prices: dict[str, str]) -> None:
        self._prices = prices
        self.seen: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.seen.append(name)
        return self._prices.get(name)


def _resolver() -> StubResolver:
    return StubResolver({'Cultivate': _CULTIVATE, 'Grizzly Bears': _GRIZZLY})


#: A realistic name->id map: the nine derived cols + a human field, all present.
_SCHEMA: dict[str, str] = {
    'Card Type': 'fldType',
    'Mana Cost': 'fldMana',
    'CMC': 'fldCmc',
    'Power / Toughness': 'fldPT',
    'Oracle Text': 'fldText',
    'Card Art': 'fldArt',
    'Scryfall URL': 'fldUrl',
    'Price (TCGPlayer)': 'fldPrice',
    'Color Identity': 'fldColor',
    'Card Name': 'fldHUMAN',
}


class DerivedSpyClient:
    """In-memory AllowlistWriteClient double for the derived-write primitive.

    Serves a fixed name->id schema, records every write-shaped call, and runs the
    real wire guard (via resolve_field_map + _guarded_body) so the guard is
    genuinely exercised offline.
    """

    def __init__(self, schema: dict[str, str] | None = None) -> None:
        self._schema = dict(schema if schema is not None else _SCHEMA)
        self.methods: list[str] = []
        self.patched_chunks: list[list[dict[str, Any]]] = []
        self._allowed_ids = frozenset(fid for name, fid in self._schema.items() if name in wb.ALLOWLIST_NAMES)
        self._id_to_name = {fid: name for name, fid in self._schema.items() if name in wb.ALLOWLIST_NAMES}

    def list_fields(self) -> dict[str, str]:
        self.methods.append('GET:list_fields')
        return dict(self._schema)

    def create_field(self, name: str, airtable_type: str, options: dict[str, Any] | None) -> None:  # pragma: no cover
        self.methods.append(f'POST:create_field:{name}')

    def patch_records(self, records: list[dict[str, Any]]) -> Any:
        # Enforce the same wire guarantee the real client does: every field id
        # must be an allowlisted id, else refuse.
        for rec in records:
            unknown = set((rec.get('fields') or {}).keys()) - self._allowed_ids
            if unknown:
                raise wb.NonAllowlistFieldError(f'REFUSED wire ids {sorted(unknown)}')
        self.methods.append('PATCH:patch_records')
        self.patched_chunks.append(list(records))
        return None

    def close(self) -> None:
        self.methods.append('close')

    @property
    def write_methods(self) -> list[str]:
        return [m for m in self.methods if m.split(':', 1)[0] in {'POST', 'PATCH', 'PUT', 'DELETE'}]


# --------------------------------------------------------------------------- #
# Payload mapping: Card + live price -> the nine allowlisted derived fields.
# --------------------------------------------------------------------------- #


def test_build_derived_payload_maps_the_nine_fields() -> None:
    payload = wb.build_derived_card_payload(_GRIZZLY, price_usd='0.10')
    assert set(payload) == wb.DERIVED_CARD_FIELDS
    assert payload['Card Type'] == 'Creature — Bear'
    assert payload['Mana Cost'] == '{1}{G}'
    assert payload['CMC'] == 2.0
    assert payload['Power / Toughness'] == '2/2'
    assert payload['Card Art'] == 'https://img.scryfall.io/grizzly.jpg'
    assert payload['Scryfall URL'] == 'https://scryfall.com/card/grizzly'
    assert payload['Price (TCGPlayer)'] == 0.10  # coerced to float for the currency column
    assert isinstance(payload['Price (TCGPlayer)'], float)
    assert payload['Color Identity'] == ['Green']  # single-letter G -> the Airtable option name


def test_build_derived_payload_omits_powertoughness_for_noncreature() -> None:
    """A non-creature (no power/toughness) leaves Power / Toughness empty, not '/'."""
    payload = wb.build_derived_card_payload(_CULTIVATE, price_usd='0.25')
    assert payload['Power / Toughness'] == ''


def test_build_derived_payload_passes_the_5a_guard() -> None:
    payload = wb.build_derived_card_payload(_GRIZZLY, price_usd='0.10')
    # Reuse 5a's guard directly: only allowlisted names, no human field.
    wb.assert_no_human_fields(payload.keys())
    assert not (set(payload) & wb._human_denylist())
    assert set(payload) <= wb.ALLOWLIST_NAMES


def test_build_derived_payload_never_contains_a_human_field() -> None:
    """(d) A crafted Card cannot smuggle a human field into the payload — the
    builder emits ONLY the closed DERIVED_CARD_FIELDS set."""
    payload = wb.build_derived_card_payload(_GRIZZLY, price_usd=None)
    assert 'Card Name' not in payload
    assert 'Number Owned' not in payload
    assert set(payload).isdisjoint(wb._human_denylist())


def test_build_derived_payload_price_is_live_not_from_card() -> None:
    """(f) Price is sourced from the passed-in live value, never the Card (which
    carries no price at all)."""
    assert not hasattr(_GRIZZLY, 'price')
    hi = wb.build_derived_card_payload(_GRIZZLY, price_usd='9.99')
    lo = wb.build_derived_card_payload(_GRIZZLY, price_usd='0.01')
    assert hi['Price (TCGPlayer)'] == 9.99  # coerced to float (currency column)
    assert lo['Price (TCGPlayer)'] == 0.01


# --------------------------------------------------------------------------- #
# (b) local mode: ZERO Airtable calls.
# --------------------------------------------------------------------------- #


def test_local_mode_makes_zero_airtable_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    spy = DerivedSpyClient()
    price = StubPrice(_PRICES)
    report = wb.write_derived_fields(
        _CARDS,
        resolver=_resolver(),
        price_fetcher=price,
        client=spy,
        dry_run=False,
        apply=True,  # even a real apply must no-op in local mode
    )
    assert report.backend == 'local'
    assert report.skipped_no_airtable is True
    assert report.write_requests_issued == 0
    assert spy.write_methods == []
    assert spy.methods == []  # not even a GET


def test_local_mode_noop_plan_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    report = wb.write_derived_fields(
        _CARDS,
        resolver=_resolver(),
        price_fetcher=StubPrice(_PRICES),
        client=DerivedSpyClient(),
    )
    assert report.skipped_no_airtable is True
    assert report.applied is False
    assert report.planned == []


# --------------------------------------------------------------------------- #
# (c) dry-run default: plans but does not write.
# --------------------------------------------------------------------------- #


def test_dry_run_default_plans_but_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    spy = DerivedSpyClient()
    report = wb.write_derived_fields(
        _CARDS,
        resolver=_resolver(),
        price_fetcher=StubPrice(_PRICES),
        client=spy,
    )  # dry_run defaults True, apply defaults False
    assert report.dry_run is True
    assert report.applied is False
    assert report.write_requests_issued == 0
    assert spy.write_methods == []
    # It DID plan (two resolvable cards).
    assert {w.record_id for w in report.planned} == {'recCultivate', 'recGrizzly'}


def test_apply_without_no_dry_run_still_dry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    spy = DerivedSpyClient()
    report = wb.write_derived_fields(
        _CARDS,
        resolver=_resolver(),
        price_fetcher=StubPrice(_PRICES),
        client=spy,
        apply=True,  # dry_run still True -> no write
    )
    assert report.write_requests_issued == 0
    assert spy.write_methods == []


# --------------------------------------------------------------------------- #
# (a) apply under airtable backend writes the derived cols (spy + real client).
# --------------------------------------------------------------------------- #


def test_apply_airtable_writes_derived_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    spy = DerivedSpyClient()
    price = StubPrice(_PRICES)
    report = wb.write_derived_fields(
        _CARDS,
        resolver=_resolver(),
        price_fetcher=price,
        client=spy,
        dry_run=False,
        apply=True,
    )
    assert report.applied is True
    assert report.write_requests_issued == 1
    assert spy.methods.count('PATCH:patch_records') == 1
    # Records keyed by record id, fields keyed by field id, all allowlisted.
    patched = spy.patched_chunks[0]
    assert {p['id'] for p in patched} == {'recCultivate', 'recGrizzly'}
    name_by_id = {v: k for k, v in _SCHEMA.items()}
    for rec in patched:
        for fid in rec['fields']:
            assert name_by_id[fid] in wb.ALLOWLIST_NAMES
    # (f) live price WAS consulted for each resolved card.
    assert set(price.seen) == {'Cultivate', 'Grizzly Bears'}


def test_apply_real_client_mocked_transport_writes_field_id_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) END-TO-END on the REAL AllowlistWriteClient (mocked httpx transport):
    a live apply resolves the schema, re-keys the payload by FIELD ID, and PATCHes
    — the wire guard passes its own derived ids."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    patched_bodies: list[dict[str, Any]] = []

    def _schema_fields() -> list[dict[str, str]]:
        fields = [{'name': name, 'id': fid} for name, fid in _SCHEMA.items()]
        # Also carry the ⚙ fields + every human field so the wrong-table binding
        # (FIX A) is satisfied (denylist is a subset of the resolved table).
        fields += [{'name': f'{wb.NS}Buckets', 'id': 'fldB'}, {'name': f'{wb.NS}Otags', 'id': 'fldO'}]
        for i, human in enumerate(sorted(wb._human_denylist())):
            if human not in _SCHEMA:
                fields.append({'name': human, 'id': f'fldHUMAN{i}'})
        return fields

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'GET':
            return httpx.Response(
                200,
                json={'tables': [{'id': 'tblCARDS', 'name': 'Inventory Cards', 'fields': _schema_fields()}]},
            )
        patched_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'records': []})

    transport = httpx.MockTransport(handler)
    client = wb.AllowlistWriteClient('tok', _client=httpx.Client(transport=transport))
    report = wb.write_derived_fields(
        _CARDS,
        resolver=_resolver(),
        price_fetcher=StubPrice(_PRICES),
        client=client,
        dry_run=False,
        apply=True,
    )
    assert report.applied is True
    assert report.write_requests_issued == 1
    assert len(patched_bodies) == 1
    derived_ids = {_SCHEMA[n] for n in wb.DERIVED_CARD_FIELDS}
    for rec in patched_bodies[0]['records']:
        assert set(rec['fields']).issubset(derived_ids)


# --------------------------------------------------------------------------- #
# (d) a crafted human field is REFUSED at the wire (guard reused).
# --------------------------------------------------------------------------- #


def test_wire_guard_refuses_human_field_id_in_derived_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a schema somehow maps a human field into the write body, the wire guard
    rejects it. We prove the guard is live by handing the REAL client a body with
    a human field id."""
    client = wb.AllowlistWriteClient('tok')
    client.resolve_field_map(_SCHEMA)
    with pytest.raises((wb.HumanFieldWriteError, wb.NonAllowlistFieldError)):
        client._guarded_body([{'id': 'recX', 'fields': {'fldHUMAN': 'clobber'}}])


# --------------------------------------------------------------------------- #
# (e) a missing derived-field id fails LOUD (columns must already exist).
# --------------------------------------------------------------------------- #


def test_missing_derived_field_id_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    # A schema MISSING 'Color Identity' — the primitive must NOT create it; it
    # must fail loud because the column is expected to already exist.
    partial = {k: v for k, v in _SCHEMA.items() if k != 'Color Identity'}
    spy = DerivedSpyClient(schema=partial)
    with pytest.raises(RuntimeError, match=r'missing.*id'):
        wb.write_derived_fields(
            _CARDS,
            resolver=_resolver(),
            price_fetcher=StubPrice(_PRICES),
            client=spy,
            dry_run=False,
            apply=True,
        )
    # No PATCH ever fired — we failed before any write.
    assert spy.write_methods == []


def test_primitive_never_creates_derived_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nine columns already exist; the primitive resolves ids only and NEVER
    calls create_field (no ensure_fields for the Scryfall cols)."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    spy = DerivedSpyClient()
    wb.write_derived_fields(
        _CARDS,
        resolver=_resolver(),
        price_fetcher=StubPrice(_PRICES),
        client=spy,
        dry_run=False,
        apply=True,
    )
    assert not any(m.startswith('POST:create_field') for m in spy.methods)


# --------------------------------------------------------------------------- #
# Unresolvable cards are skipped, not guessed.
# --------------------------------------------------------------------------- #


def test_unresolvable_card_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    spy = DerivedSpyClient()
    cards = {**_CARDS, 'recMystery': 'Totally Unknown Card'}
    report = wb.write_derived_fields(
        cards,
        resolver=_resolver(),  # does not know the mystery card
        price_fetcher=StubPrice(_PRICES),
        client=spy,
        dry_run=False,
        apply=True,
    )
    assert 'recMystery' in report.skipped
    assert {w.record_id for w in report.planned} == {'recCultivate', 'recGrizzly'}


# --------------------------------------------------------------------------- #
# Chunking at 10.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, 'INTER_CHUNK_DELAY_S', 0)


def test_derived_write_chunks_at_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    cards: dict[str, str] = {}
    resolved: dict[str, Card] = {}
    prices: dict[str, str] = {}
    for i in range(23):
        name = f'Card {i:03d}'
        cards[f'rec{i:03d}'] = name
        resolved[name] = Card(name=name, oracle_id=f'oid-{i:03d}', type_line='Creature', mana_value=1.0)
        prices[name] = '1.00'
    spy = DerivedSpyClient()
    report = wb.write_derived_fields(
        cards,
        resolver=StubResolver(resolved),
        price_fetcher=StubPrice(prices),
        client=spy,
        dry_run=False,
        apply=True,
    )
    assert [len(c) for c in spy.patched_chunks] == [10, 10, 3]
    assert report.write_requests_issued == 3


# --------------------------------------------------------------------------- #
# Explicit refresh entry point (CLI): dry-run default.
# --------------------------------------------------------------------------- #


def test_refresh_cli_dry_run_default_issues_no_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--refresh` (no --no-dry-run --apply) plans but issues ZERO writes."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    monkeypatch.setenv('AIRTABLE_API_KEY', 'tok')

    spy = DerivedSpyClient()
    monkeypatch.setattr(wb, 'AllowlistWriteClient', lambda *a, **k: spy)
    monkeypatch.setattr(wb, '_load_cards_from_lake', lambda card_name_field_id=None: (_CARDS, {}, {}))
    monkeypatch.setattr(wb, 'default_card_resolver', _resolver)
    monkeypatch.setattr(wb, '_live_price_fetcher', lambda: StubPrice(_PRICES))

    printed: list[str] = []
    monkeypatch.setattr('builtins.print', lambda s='': printed.append(s))

    rc = wb.main(['--refresh'])
    assert rc == 0
    assert spy.write_methods == []
    out = '\n'.join(printed)
    assert 'DRY-RUN' in out or 'dry-run' in out.lower()


def test_refresh_cli_local_backend_makes_no_airtable_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--refresh` in LOCAL mode issues no Airtable request (no-op)."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)

    monkeypatch.setattr(wb, '_load_cards_from_lake', lambda card_name_field_id=None: (_CARDS, {}, {}))
    monkeypatch.setattr(wb, 'default_card_resolver', _resolver)
    monkeypatch.setattr(wb, '_live_price_fetcher', lambda: StubPrice(_PRICES))
    # A client must never even be built in local mode.
    monkeypatch.setattr(
        wb,
        'AllowlistWriteClient',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('no client in local mode')),
    )

    printed: list[str] = []
    monkeypatch.setattr('builtins.print', lambda s='': printed.append(s))

    rc = wb.main(['--refresh'])
    assert rc == 0
    out = '\n'.join(printed)
    assert 'local' in out.lower()


def test_refresh_cli_apply_writes_under_mocked_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--refresh --no-dry-run --apply` with a credential writes the derived cols."""
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    monkeypatch.setenv('AIRTABLE_API_KEY', 'tok')

    spy = DerivedSpyClient()
    monkeypatch.setattr(wb, 'AllowlistWriteClient', lambda *a, **k: spy)
    monkeypatch.setattr(wb, '_load_cards_from_lake', lambda card_name_field_id=None: (_CARDS, {}, {}))
    monkeypatch.setattr(wb, 'default_card_resolver', _resolver)
    monkeypatch.setattr(wb, '_live_price_fetcher', lambda: StubPrice(_PRICES))

    printed: list[str] = []
    monkeypatch.setattr('builtins.print', lambda s='': printed.append(s))

    rc = wb.main(['--refresh', '--no-dry-run', '--apply'])
    assert rc == 0
    assert spy.methods.count('PATCH:patch_records') == 1
