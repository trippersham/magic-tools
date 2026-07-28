"""Shared, parametrized port-conformance suite for `CollectionStore`.

Runs the SAME round-trip assertions against BOTH concrete adapters — the local
YAML store (real files under a tmp data dir + a stub `CardResolver`) and the
Airtable store (the in-memory `FakeAirtable` httpx double). This is the guard
that would have caught the write-path drops: it asserts *behavior parity* on the
subset both backends can genuinely persist, so an adapter that silently drops a
link field fails here regardless of its own transport-double fixture.

Divergence that is INHERENT (and therefore scoped out of the shared assertions,
with explicit comments):
    - Chase `priority` / `status` / `target_price` — no column on the live
      Airtable base (local YAML retains them). Conformance asserts only the
      persistable subset (`for_decks`).
    - Airtable maindeck link rows carry no per-card multiplicity, so a non-basic
      card's `quantity` is not recoverable (it lives in the `Repeat Cards Count`
      rollup). Conformance uses quantity-1 non-basic cards; basic-land quantities
      (which DO round-trip via the count fields) are asserted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx
import pytest

from pipeline import config, store
from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard, Trade
from tests.test_airtable_collection import _FIELDS, FakeAirtable, _StubCardResolver

if TYPE_CHECKING:
    from pathlib import Path


class _StoreFactory(Protocol):
    def __call__(self, *, seed_cards: list[str] = ..., seed_decks: list[str] = ...) -> object: ...


# --------------------------------------------------------------------------- #
# Stub CardResolver (local adapter) — resolves any name to a bare Card.
# --------------------------------------------------------------------------- #


class _AnyResolver:
    """Resolve every name to a minimal `Card` (name-only enrichment is fine)."""

    def get_card(self, name: str) -> Card:
        return Card(name=name)


# --------------------------------------------------------------------------- #
# Adapter factories — each returns a writes-enabled store, seeded so that link
# resolution (Airtable) can succeed. The local adapter ignores the seeds (it is
# schema-free), which is exactly the point: same test body, both backends.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def local_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _StoreFactory:
    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))

    def make(*, seed_cards: list[str] | None = None, seed_decks: list[str] | None = None) -> LocalYamlStore:
        return LocalYamlStore(resolver=_AnyResolver())

    return make  # type: ignore[return-value]


@pytest.fixture()
def airtable_factory(monkeypatch: pytest.MonkeyPatch) -> _StoreFactory:
    config.get_settings.cache_clear()

    def make(
        *, seed_cards: list[str] | None = None, seed_decks: list[str] | None = None
    ) -> AirtableCollectionStore:
        inv = _FIELDS['Inventory Cards']
        d = _FIELDS['Decks']
        cards = [{'id': f'recCard{i}', 'fields': {inv['Card Name']: n}} for i, n in enumerate(seed_cards or [])]
        decks = [{'id': f'recDeck{i}', 'fields': {d['Name']: n}} for i, n in enumerate(seed_decks or [])]
        fake = FakeAirtable({'tblCards': cards, 'tblDecks': decks, 'tblChase': [], 'tblTrades': []})
        transport = httpx.MockTransport(fake.handler)
        client = httpx.Client(transport=transport)
        return AirtableCollectionStore.from_settings(
            'fake-token', writes_enabled=True, client=client, card_resolver=_StubCardResolver()
        )

    return make  # type: ignore[return-value]


@pytest.fixture(params=['local', 'airtable'])
def make_store(request: pytest.FixtureRequest) -> _StoreFactory:
    return request.getfixturevalue(f'{request.param}_factory')


# --------------------------------------------------------------------------- #
# save_deck -> get_deck preserves cards (names + roles + basic quantities)
# --------------------------------------------------------------------------- #


def test_conformance_save_deck_round_trip(make_store: _StoreFactory) -> None:
    # Non-basic cards must pre-exist for the Airtable link resolution; the local
    # adapter ignores the seed. Basic lands need no inventory row on either side.
    st = make_store(seed_cards=['Grumgully, the Generous', 'Sol Ring', 'Llanowar Elves'])
    deck = Deck(
        name='Gruul Aggro',
        strategy='Go-wide.',
        cards=[
            DeckCard(name='Grumgully, the Generous', role='commander'),
            DeckCard(name='Sol Ring'),
            DeckCard(name='Llanowar Elves'),
            DeckCard(name='Forest', quantity=8),
        ],
    )
    st.save_deck(deck)  # type: ignore[attr-defined]
    loaded = st.get_deck('Gruul Aggro')  # type: ignore[attr-defined]

    assert loaded.name == 'Gruul Aggro'
    assert loaded.strategy == 'Go-wide.'
    # commander role preserved (derived property).
    assert [c.name for c in loaded.commanders] == ['Grumgully, the Generous']
    by_name = {c.name: c for c in loaded.cards}
    # non-basic maindeck names preserved (quantity 1 — the subset both persist).
    assert 'Sol Ring' in by_name
    assert by_name['Sol Ring'].role is None
    assert 'Llanowar Elves' in by_name
    # basic-land quantity round-trips on BOTH backends.
    assert by_name['Forest'].quantity == 8


# --------------------------------------------------------------------------- #
# add_chase -> list_chase preserves for_decks (Target Decks link)
# --------------------------------------------------------------------------- #


def test_conformance_add_chase_for_deck_round_trip(make_store: _StoreFactory) -> None:
    st = make_store(seed_decks=['Gruul Aggro'])
    st.add_chase('The One Ring', for_deck='Gruul Aggro')  # type: ignore[attr-defined]
    [chase] = st.list_chase()  # type: ignore[attr-defined]
    assert chase.name == 'The One Ring'
    # persistable subset: the deck association round-trips on both backends.
    assert chase.for_decks == ['Gruul Aggro']
    # NOTE: priority/status/target_price are intentionally NOT asserted here —
    # the Airtable base has no columns for them (local YAML retains them). That
    # inherent divergence is covered by the per-adapter tests, not conformance.


# --------------------------------------------------------------------------- #
# log_trade -> list_trades preserves cards_in / cards_out
# --------------------------------------------------------------------------- #


def test_conformance_log_trade_cards_round_trip(make_store: _StoreFactory) -> None:
    st = make_store(seed_cards=['Sol Ring', 'Llanowar Elves'])
    st.log_trade(  # type: ignore[attr-defined]
        Trade(
            from_source='Store',
            to_destination='Library',
            cards_in=['Sol Ring'],
            cards_out=['Llanowar Elves'],
            status='Completed',
        )
    )
    [trade] = st.list_trades()  # type: ignore[attr-defined]
    assert trade.from_source == 'Store'
    assert trade.to_destination == 'Library'
    assert trade.cards_in == ['Sol Ring']
    assert trade.cards_out == ['Llanowar Elves']
    assert trade.status == 'Completed'
