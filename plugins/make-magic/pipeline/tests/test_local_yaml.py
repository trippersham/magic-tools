"""TDD tests for the local YAML CollectionStore adapter (Phase 1.3).

OFFLINE: a tmp MAKE_MAGIC_DATA_DIR + a STUB CardResolver (no network, no
scripts/ import). Covers per domain: write -> read round-trips, hydrate-on-read
via the resolver, the unresolved-card (name-only) path, and the deck YAML shape
matching the design.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.collection.errors import CollectionError
from pipeline.contracts import Card, Deck, DeckCard, Trade


class StubResolver:
    """A deterministic in-memory CardResolver — enrichment for a fixed catalog."""

    def __init__(self, catalog: dict[str, Card] | None = None) -> None:
        self._catalog = catalog or {
            'Sol Ring': Card(
                name='Sol Ring',
                oracle_id='sol-ring-oid',
                mana_value=1.0,
                type_line='Artifact',
                produced_mana=['C'],
                oracle_text='{T}: Add {C}{C}.',
            ),
            'Llanowar Elves': Card(
                name='Llanowar Elves',
                oracle_id='llanowar-oid',
                mana_value=1.0,
                type_line='Creature — Elf Druid',
                colors=['G'],
                color_identity=['G'],
                produced_mana=['G'],
            ),
            'Grumgully, the Generous': Card(
                name='Grumgully, the Generous',
                oracle_id='grumgully-oid',
                mana_value=3.0,
                type_line='Legendary Creature — Goblin Shaman',
                color_identity=['R', 'G'],
            ),
        }

    def get_card(self, name: str) -> Card | None:
        return self._catalog.get(name)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


@pytest.fixture()
def adapter(data_dir: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=StubResolver())


def _collection_dir() -> Path:
    return store.StorePaths.resolve().collection


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #


def test_backend_name(adapter: LocalYamlStore) -> None:
    assert adapter.backend_name == 'local'


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


def test_add_card_then_list_hydrates(adapter: LocalYamlStore) -> None:
    adapter.add_card('Sol Ring', qty=3, condition=['NM'], foil=1, sets=['C21'])
    inv = adapter.list_inventory()
    assert len(inv) == 1
    owned = inv[0]
    assert owned.name == 'Sol Ring'
    assert owned.owned == 3
    assert owned.foil == 1
    assert owned.condition == ['NM']
    assert owned.sets == ['C21']
    # Hydrated from the resolver.
    assert owned.oracle_id == 'sol-ring-oid'
    assert owned.mana_value == 1.0
    assert owned.type_line == 'Artifact'


def test_inventory_yaml_is_owned_facts_only(adapter: LocalYamlStore) -> None:
    """The persisted YAML stores owned facts only — no enrichment mirror."""
    adapter.add_card('Sol Ring', qty=2)
    raw = yaml.safe_load((_collection_dir() / 'inventory.yaml').read_text())
    assert raw == [{'card': 'Sol Ring', 'owned': 2}]
    # No enrichment keys leaked into persistence.
    assert 'oracle_id' not in raw[0]
    assert 'type_line' not in raw[0]


def test_add_card_accumulates_quantity(adapter: LocalYamlStore) -> None:
    adapter.add_card('Sol Ring', qty=1)
    adapter.add_card('Sol Ring', qty=2)
    inv = {c.name: c for c in adapter.list_inventory()}
    assert inv['Sol Ring'].owned == 3


def test_set_quantity(adapter: LocalYamlStore) -> None:
    adapter.add_card('Sol Ring', qty=1)
    adapter.set_quantity('Sol Ring', 5)
    inv = {c.name: c for c in adapter.list_inventory()}
    assert inv['Sol Ring'].owned == 5


def test_remove_card(adapter: LocalYamlStore) -> None:
    adapter.add_card('Sol Ring', qty=1)
    adapter.add_card('Llanowar Elves', qty=1)
    adapter.remove_card('Sol Ring')
    names = {c.name for c in adapter.list_inventory()}
    assert names == {'Llanowar Elves'}


def test_unresolved_card_reads_back_name_only(adapter: LocalYamlStore) -> None:
    adapter.add_card('Mysterious Spoiler', qty=1)
    inv = {c.name: c for c in adapter.list_inventory()}
    card = inv['Mysterious Spoiler']
    assert card.name == 'Mysterious Spoiler'
    assert card.owned == 1
    # Unresolved -> null enrichment.
    assert card.oracle_id is None
    assert card.type_line is None
    assert card.colors == []


def test_list_inventory_empty_when_no_file(adapter: LocalYamlStore) -> None:
    assert adapter.list_inventory() == []


# --------------------------------------------------------------------------- #
# Chase
# --------------------------------------------------------------------------- #


def test_add_chase_then_list_hydrates(adapter: LocalYamlStore) -> None:
    adapter.add_chase('Sol Ring', priority=1, for_deck='gruul', status='wanted', target_price=2.5)
    chase = adapter.list_chase()
    assert len(chase) == 1
    c = chase[0]
    assert c.name == 'Sol Ring'
    assert c.priority == 1
    assert c.for_decks == ['gruul']
    assert c.status == 'wanted'
    assert c.target_price == 2.5
    # Hydrated.
    assert c.oracle_id == 'sol-ring-oid'


def test_remove_chase(adapter: LocalYamlStore) -> None:
    adapter.add_chase('Sol Ring', priority=1)
    adapter.remove_chase('Sol Ring')
    assert adapter.list_chase() == []


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #


def test_log_trade_then_list(adapter: LocalYamlStore) -> None:
    adapter.log_trade(
        Trade(
            date='2026-07-20',
            from_source='Store',
            to_destination='Library',
            cards_in=['Sol Ring'],
            status='Completed',
        )
    )
    trades = adapter.list_trades()
    assert len(trades) == 1
    assert trades[0].from_source == 'Store'
    assert trades[0].cards_in == ['Sol Ring']


def test_log_trade_appends(adapter: LocalYamlStore) -> None:
    adapter.log_trade(Trade(from_source='Store', to_destination='Library'))
    adapter.log_trade(Trade(from_source='Library', to_destination='Deck', to_deck='gruul'))
    assert len(adapter.list_trades()) == 2


# --------------------------------------------------------------------------- #
# Decks
# --------------------------------------------------------------------------- #


def test_save_deck_then_get_hydrates(adapter: LocalYamlStore) -> None:
    deck = Deck(
        name='Gruul Aggro',
        strategy='Go-wide creature aggro.',
        cards=[
            DeckCard(name='Grumgully, the Generous', role='commander'),
            DeckCard(name='Sol Ring', quantity=1),
            DeckCard(name='Llanowar Elves', quantity=1),
        ],
    )
    adapter.save_deck(deck)
    loaded = adapter.get_deck('Gruul Aggro')
    assert loaded.name == 'Gruul Aggro'
    assert loaded.strategy == 'Go-wide creature aggro.'
    assert len(loaded.cards) == 3
    # commanders derived + hydrated.
    assert [c.name for c in loaded.commanders] == ['Grumgully, the Generous']
    by_name = {c.name: c for c in loaded.cards}
    assert by_name['Sol Ring'].oracle_id == 'sol-ring-oid'
    assert by_name['Grumgully, the Generous'].role == 'commander'


def test_deck_yaml_shape_matches_design(adapter: LocalYamlStore) -> None:
    """Deck YAML is self-contained: name, strategy, airtable_record_id, cards[]."""
    deck = Deck(
        name='Gruul Aggro',
        strategy='Go-wide.',
        cards=[
            DeckCard(name='Grumgully, the Generous', role='commander'),
            DeckCard(name='Sol Ring', quantity=1),
        ],
    )
    adapter.save_deck(deck)
    raw = yaml.safe_load((_collection_dir() / 'decks' / 'gruul-aggro.yaml').read_text())
    assert raw['name'] == 'Gruul Aggro'
    assert raw['strategy'] == 'Go-wide.'
    assert raw['airtable_record_id'] is None
    # qty is omitted for the default quantity (1); role is emitted when set.
    assert raw['cards'] == [
        {'card': 'Grumgully, the Generous', 'role': 'commander'},
        {'card': 'Sol Ring'},
    ]
    # No enrichment leaked into deck persistence.
    assert 'oracle_id' not in raw['cards'][0]


def test_list_decks(adapter: LocalYamlStore) -> None:
    adapter.save_deck(Deck(name='Gruul Aggro', cards=[DeckCard(name='Sol Ring')]))
    adapter.save_deck(Deck(name='Mono Blue', cards=[DeckCard(name='Llanowar Elves')]))
    names = sorted(d.name for d in adapter.list_decks())
    assert names == ['Gruul Aggro', 'Mono Blue']


def test_set_strategy(adapter: LocalYamlStore) -> None:
    adapter.save_deck(Deck(name='Gruul Aggro', cards=[DeckCard(name='Sol Ring')]))
    adapter.set_strategy('Gruul Aggro', 'New strategy text.')
    assert adapter.get_deck('Gruul Aggro').strategy == 'New strategy text.'
    # Cards preserved through the strategy edit.
    assert [c.name for c in adapter.get_deck('Gruul Aggro').cards] == ['Sol Ring']


def test_get_deck_missing_raises(adapter: LocalYamlStore) -> None:
    with pytest.raises(FileNotFoundError):
        adapter.get_deck('Nonexistent')


def test_save_deck_persists_assessment_and_focus_otags(adapter: LocalYamlStore) -> None:
    deck = Deck(
        name='Gruul Aggro',
        strategy='Go-wide.',
        assessment='Thin on removal.',
        focus_otags=['sacrifice', 'aristocrats'],
        cards=[DeckCard(name='Sol Ring')],
    )
    adapter.save_deck(deck)
    loaded = adapter.get_deck('Gruul Aggro')
    assert loaded.assessment == 'Thin on removal.'
    assert loaded.focus_otags == ['sacrifice', 'aristocrats']
    raw = yaml.safe_load((_collection_dir() / 'decks' / 'gruul-aggro.yaml').read_text())
    assert raw['assessment'] == 'Thin on removal.'
    assert raw['focus_otags'] == ['sacrifice', 'aristocrats']


def test_save_deck_omits_empty_assessment_and_focus_otags(adapter: LocalYamlStore) -> None:
    """A deck with no assessment/focus keeps the YAML clean (keys omitted)."""
    adapter.save_deck(Deck(name='Gruul Aggro', cards=[DeckCard(name='Sol Ring')]))
    raw = yaml.safe_load((_collection_dir() / 'decks' / 'gruul-aggro.yaml').read_text())
    assert 'assessment' not in raw
    assert 'focus_otags' not in raw


def test_set_assessment(adapter: LocalYamlStore) -> None:
    adapter.save_deck(Deck(name='Gruul Aggro', cards=[DeckCard(name='Sol Ring')]))
    adapter.set_assessment('Gruul Aggro', 'New assessment.')
    assert adapter.get_deck('Gruul Aggro').assessment == 'New assessment.'
    # Cards preserved through the edit.
    assert [c.name for c in adapter.get_deck('Gruul Aggro').cards] == ['Sol Ring']


def test_set_focus_otags(adapter: LocalYamlStore) -> None:
    adapter.save_deck(Deck(name='Gruul Aggro', cards=[DeckCard(name='Sol Ring')]))
    adapter.set_focus_otags('Gruul Aggro', ['sacrifice', 'tokens'])
    assert adapter.get_deck('Gruul Aggro').focus_otags == ['sacrifice', 'tokens']
    assert [c.name for c in adapter.get_deck('Gruul Aggro').cards] == ['Sol Ring']


def test_set_assessment_missing_deck_raises(adapter: LocalYamlStore) -> None:
    with pytest.raises(FileNotFoundError):
        adapter.set_assessment('Nonexistent', 'x')


def test_set_focus_otags_missing_deck_raises(adapter: LocalYamlStore) -> None:
    with pytest.raises(FileNotFoundError):
        adapter.set_focus_otags('Nonexistent', ['x'])


# --------------------------------------------------------------------------- #
# Fix 2 — role + non-default quantity both round-trip (they are orthogonal)
# --------------------------------------------------------------------------- #


def test_deck_card_role_and_quantity_both_round_trip(adapter: LocalYamlStore) -> None:
    """A commander with 2 copies must preserve BOTH role and quantity on save/read."""
    deck = Deck(
        name='Gruul Aggro',
        cards=[
            DeckCard(name='Grumgully, the Generous', role='commander', quantity=2),
            DeckCard(name='Sol Ring', quantity=3),
        ],
    )
    adapter.save_deck(deck)

    # Both facts persisted in the row (role AND qty).
    raw = yaml.safe_load((_collection_dir() / 'decks' / 'gruul-aggro.yaml').read_text())
    assert raw['cards'] == [
        {'card': 'Grumgully, the Generous', 'role': 'commander', 'qty': 2},
        {'card': 'Sol Ring', 'qty': 3},
    ]

    loaded = {c.name: c for c in adapter.get_deck('Gruul Aggro').cards}
    assert loaded['Grumgully, the Generous'].role == 'commander'
    assert loaded['Grumgully, the Generous'].quantity == 2
    assert loaded['Sol Ring'].quantity == 3
    assert loaded['Sol Ring'].role is None


# --------------------------------------------------------------------------- #
# Fix 3 — comma card names: flow-style corruption is LOUD; quoted names round-trip
# --------------------------------------------------------------------------- #


def test_unquoted_comma_flow_entry_raises_actionable_error(adapter: LocalYamlStore) -> None:
    """A hand-edited flow-style entry with an unquoted comma name must NOT silently
    read the wrong card — it raises a clear ValueError telling the user to quote."""
    decks = _collection_dir() / 'decks'
    decks.mkdir(parents=True, exist_ok=True)
    # `{ card: Grumgully, the Generous, role: commander }` parses to
    # {'card': 'Grumgully', 'the Generous': None, 'role': 'commander'}.
    (decks / 'gruul-aggro.yaml').write_text(
        'name: Gruul Aggro\ncards:\n  - { card: Grumgully, the Generous, role: commander }\n'
    )
    with pytest.raises(CollectionError, match='comma') as exc:
        adapter.get_deck('Gruul Aggro')
    assert 'gruul-aggro.yaml' in str(exc.value)


def test_quoted_comma_name_round_trips(adapter: LocalYamlStore) -> None:
    """A properly quoted comma name reads back with the correct identity."""
    decks = _collection_dir() / 'decks'
    decks.mkdir(parents=True, exist_ok=True)
    (decks / 'gruul-aggro.yaml').write_text(
        'name: Gruul Aggro\ncards:\n  - card: "Grumgully, the Generous"\n    role: commander\n'
    )
    deck = adapter.get_deck('Gruul Aggro')
    assert [c.name for c in deck.commanders] == ['Grumgully, the Generous']


def test_malformed_non_dict_deck_yaml_raises_value_error(adapter: LocalYamlStore) -> None:
    """A non-mapping deck YAML raises a clear ValueError naming the file, not KeyError."""
    decks = _collection_dir() / 'decks'
    decks.mkdir(parents=True, exist_ok=True)
    (decks / 'gruul-aggro.yaml').write_text('- just\n- a\n- list\n')
    with pytest.raises(CollectionError, match=r'gruul-aggro\.yaml'):
        adapter.get_deck('Gruul Aggro')


def test_inventory_unquoted_comma_flow_entry_raises(adapter: LocalYamlStore) -> None:
    """The inventory reader is equally loud about the flow-comma trap."""
    (_collection_dir()).mkdir(parents=True, exist_ok=True)
    (_collection_dir() / 'inventory.yaml').write_text('- { card: Grumgully, the Generous, owned: 1 }\n')
    with pytest.raises(CollectionError, match='comma'):
        adapter.list_inventory()


# --------------------------------------------------------------------------- #
# Fix 4 — trade reads tolerate a harmless extra hand-added key
# --------------------------------------------------------------------------- #


def test_list_trades_ignores_extra_key(adapter: LocalYamlStore) -> None:
    """An extra annotation key in a trades.yaml row is ignored (not a crash)."""
    (_collection_dir()).mkdir(parents=True, exist_ok=True)
    (_collection_dir() / 'trades.yaml').write_text(
        '- from_source: Store\n'
        '  to_destination: Library\n'
        '  cards_in:\n'
        '    - Sol Ring\n'
        '  my_annotation: some note the user jotted down\n'
    )
    trades = adapter.list_trades()
    assert len(trades) == 1
    assert trades[0].from_source == 'Store'
    assert trades[0].cards_in == ['Sol Ring']
