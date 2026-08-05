"""OFFLINE tests for the one-shot ``copy_collection`` interop helper.

Copies EVERY record (inventory, decks, chase, trades) from a source store to a
destination store via the domain-typed port — no live sync, no network. The
Airtable side is the in-memory ``FakeAirtable`` httpx double; the local side is a
real ``LocalYamlStore`` over a tmp data dir with a name-only ``CardResolver``.

Covered:
    - local -> (fake) airtable copies each record kind.
    - (fake) airtable -> local copies each record kind.
    - a local -> airtable -> local round-trip preserves the persistable subset
      (decks w/ commander + cards + basics, inventory owned-facts, chase
      for_decks, trade card/deck links) — consistent with the port-conformance
      scope.
    - the copy report counts what was written per kind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from pipeline import config, store
from pipeline.collection import copy_collection
from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore, ReadOnlyStoreError
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard, Trade
from tests.test_airtable_collection import _FIELDS, FakeAirtable, _StubCardResolver

if TYPE_CHECKING:
    from pathlib import Path


class _AnyResolver:
    def get_card(self, name: str) -> Card:
        return Card(name=name)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    config.get_settings.cache_clear()


@pytest.fixture()
def local_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalYamlStore:
    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'data'))
    return LocalYamlStore(resolver=_AnyResolver())


def _airtable_store(fake: FakeAirtable, *, writes_enabled: bool = True) -> AirtableCollectionStore:
    transport = httpx.MockTransport(fake.handler)
    client = httpx.Client(transport=transport)
    return AirtableCollectionStore.from_settings(
        'fake-token', writes_enabled=writes_enabled, client=client, card_resolver=_StubCardResolver()
    )


def _seeded_fake(*, cards: list[str], decks: list[str]) -> FakeAirtable:
    """A FakeAirtable pre-seeded with named Inventory Cards + Decks records.

    Airtable link fields are RECORD IDS, so any card/deck a copy references must
    already exist as a row. The copy writes membership/links; it does not create
    the referenced inventory rows (those are copied by the inventory pass first,
    but for a seeded destination we prime them explicitly).
    """
    inv = _FIELDS['Inventory Cards']
    d = _FIELDS['Decks']
    return FakeAirtable(
        {
            'tblCards': [{'id': f'recCard{i}', 'fields': {inv['Card Name']: n}} for i, n in enumerate(cards)],
            'tblDecks': [{'id': f'recDeck{i}', 'fields': {d['Name']: n}} for i, n in enumerate(decks)],
            'tblChase': [],
            'tblTrades': [],
        }
    )


def _populate_local(st: LocalYamlStore) -> None:
    st.add_card('Sol Ring', 2, condition=['NM'])
    st.add_card('Llanowar Elves', 1)
    st.add_card('Grumgully, the Generous', 1)
    st.save_deck(
        Deck(
            name='Gruul Aggro',
            strategy='Go-wide.',
            cards=[
                DeckCard(name='Grumgully, the Generous', role='commander'),
                DeckCard(name='Sol Ring'),
                DeckCard(name='Llanowar Elves'),
                DeckCard(name='Forest', quantity=8),
            ],
        )
    )
    st.add_chase('The One Ring', for_deck='Gruul Aggro')
    st.log_trade(
        Trade(
            from_source='Store',
            to_destination='Library',
            cards_in=['Sol Ring'],
            cards_out=['Llanowar Elves'],
            status='Completed',
        )
    )


# --------------------------------------------------------------------------- #
# local -> airtable
# --------------------------------------------------------------------------- #


def test_copy_local_to_airtable_writes_every_kind(local_store: LocalYamlStore) -> None:
    _populate_local(local_store)
    # Seed the destination decks/cards so link resolution succeeds after the
    # inventory pass has created the card rows. Decks referenced by chase/trade
    # links must exist; the deck itself is created by the deck pass.
    fake = _seeded_fake(cards=[], decks=['Gruul Aggro'])
    dest = _airtable_store(fake, writes_enabled=True)

    report = copy_collection(local_store, dest)

    assert report.inventory == 3
    assert report.decks == 1
    assert report.chase == 1
    assert report.trades == 1
    # inventory landed as records.
    names = {r['fields'][_FIELDS['Inventory Cards']['Card Name']] for r in fake.tables['tblCards']}
    assert {'Sol Ring', 'Llanowar Elves', 'Grumgully, the Generous'} <= names


def test_copy_to_airtable_requires_writes_enabled_dest(local_store: LocalYamlStore) -> None:
    _populate_local(local_store)
    fake = _seeded_fake(cards=[], decks=['Gruul Aggro'])
    dest = _airtable_store(fake, writes_enabled=False)
    with pytest.raises(ReadOnlyStoreError):
        copy_collection(local_store, dest)


# --------------------------------------------------------------------------- #
# airtable -> local
# --------------------------------------------------------------------------- #


def test_copy_airtable_to_local(local_store: LocalYamlStore) -> None:
    inv = _FIELDS['Inventory Cards']
    d = _FIELDS['Decks']
    fake = FakeAirtable(
        {
            'tblCards': [
                {'id': 'recSol', 'fields': {inv['Card Name']: 'Sol Ring', inv['Number Owned']: 4}},
                {'id': 'recLlan', 'fields': {inv['Card Name']: 'Llanowar Elves', inv['Number Owned']: 1}},
            ],
            'tblDecks': [
                {
                    'id': 'recDeck',
                    'fields': {
                        d['Name']: 'Gruul Aggro',
                        d['Strategy']: 'Go-wide.',
                        d['Cards']: ['recSol', 'recLlan'],
                        d['Forests']: 8,
                    },
                }
            ],
            'tblChase': [],
            'tblTrades': [],
        }
    )
    src = _airtable_store(fake, writes_enabled=False)

    report = copy_collection(src, local_store)

    assert report.inventory == 2
    assert report.decks == 1
    inv_names = {c.name for c in local_store.list_inventory()}
    assert inv_names == {'Sol Ring', 'Llanowar Elves'}
    deck = local_store.get_deck('Gruul Aggro')
    assert deck.strategy == 'Go-wide.'
    by_name = {c.name: c for c in deck.cards}
    assert by_name['Forest'].quantity == 8


# --------------------------------------------------------------------------- #
# round-trip: local -> (fake) airtable -> local preserves records
# --------------------------------------------------------------------------- #


def test_round_trip_local_airtable_local_preserves_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'a'))
    src = LocalYamlStore(resolver=_AnyResolver())
    _populate_local(src)

    # Do NOT pre-seed an empty 'Gruul Aggro' decks row: the deck pass CREATES the
    # real deck, and under the dup-name-different-file rule a phantom empty seed
    # would land as a SECOND same-named file on the round-trip back to local (a
    # dual-row the copy correctly no longer clobbers). Chase's link target is the
    # real deck, created before the chase pass runs.
    fake = _seeded_fake(cards=[], decks=[])
    mid = _airtable_store(fake, writes_enabled=True)
    copy_collection(src, mid)

    # Fresh read-only Airtable view over the SAME fake state, then copy back to a
    # second local store.
    mid_ro = _airtable_store(fake, writes_enabled=False)
    monkeypatch.setenv(store.ENV_DATA_DIR, str(tmp_path / 'b'))
    dst = LocalYamlStore(resolver=_AnyResolver())
    copy_collection(mid_ro, dst)

    # inventory owned-facts preserved.
    inv = {c.name: c for c in dst.list_inventory()}
    assert set(inv) == {'Sol Ring', 'Llanowar Elves', 'Grumgully, the Generous'}
    assert inv['Sol Ring'].owned == 2
    # deck cards/roles + basic quantity preserved.
    deck = dst.get_deck('Gruul Aggro')
    assert [c.name for c in deck.commanders] == ['Grumgully, the Generous']
    by_name = {c.name: c for c in deck.cards}
    assert 'Sol Ring' in by_name
    assert by_name['Forest'].quantity == 8
    # chase for_decks preserved.
    [chase] = dst.list_chase()
    assert chase.name == 'The One Ring'
    assert chase.for_decks == ['Gruul Aggro']
    # trade card links preserved.
    [trade] = dst.list_trades()
    assert trade.cards_in == ['Sol Ring']
    assert trade.cards_out == ['Llanowar Elves']
