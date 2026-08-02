"""TDD tests for the edge contracts (Pydantic v2 boundary models).

Covers, per model: a good example validates; a bad example is rejected
(missing required / wrong type / extra field where forbidden). Plus:
    - FactSheet accepts a real build_factsheet()-shaped dict, verbatim.
    - The Card inheritance hierarchy (OwnedCard / ChaseCard / DeckCard) — base
      enrichment nullable, name-only unresolved card, hydration round-trips.
    - Deck.commanders is a DERIVED property (role == 'commander'), not a field.

No network. No imports of deck_factsheet.py (we replicate its output SHAPE in a
fixture so the contract can be verified without importing the script package).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.contracts import (
    Card,
    ChaseCard,
    Deck,
    DeckCard,
    FactSheet,
    OwnedCard,
    Spoiler,
    Trade,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _factsheet_dict() -> dict:
    """A dict shaped EXACTLY like scripts/deck_factsheet.py build_factsheet().

    Mirrors the top-level keys deck/shape/mana/keywords/interaction/
    card_advantage/structural/coverage/cards/missing and each nested shape.
    """
    return {
        'deck': 'Sokka Spellslinger',
        'shape': {
            'nonland_count': 63,
            'land_count': 37,
            'cmc_histogram': {
                '0': 2,
                '1': 8,
                '2': 15,
                '3': 14,
                '4': 10,
                '5': 7,
                '6': 4,
                '7+': 3,
            },
            'avg_cmc': 2.94,
            'top_end_count': 5,
        },
        'mana': {
            'ramp_sources': 9,
            'fixing_sources': 6,
            'pip_counts': {'W': 12, 'U': 20, 'B': 0, 'R': 15, 'G': 0, 'C': 3},
        },
        'keywords': {'Flash': 4, 'Flying': 6, 'Prowess': 3},
        'interaction': {
            'board_wipes': 3,
            'spot_removal': 8,
            'counterspells': 6,
            'protection': 4,
            'instant_speed': 22,
        },
        'card_advantage': {'repeatable_draw': 5, 'one_shot_draw': 9},
        'structural': {
            'etb_creatures': 7,
            'graveyard_recursion_present': True,
        },
        'coverage': {
            'categorized_pct': 71.43,
            'uncategorized_pct': 28.57,
            'uncategorized_cards': ['Some Synergy Card', 'Another One'],
        },
        'cards': [
            {
                'name': 'Sol Ring',
                'cmc': 1.0,
                'type_line': 'Artifact',
                'keywords': [],
                'produced_mana': ['C'],
                'is_land': False,
                'oracle_text': '{T}: Add {C}{C}.',
            },
            {
                'name': 'Island',
                'cmc': 0.0,
                'type_line': 'Basic Land — Island',
                'keywords': [],
                'produced_mana': ['U'],
                'is_land': True,
                'oracle_text': '',
            },
        ],
        'missing': ['Some Unresolved Card Name'],
    }


# --------------------------------------------------------------------------- #
# Card — base identity + enrichment (all enrichment nullable)
# --------------------------------------------------------------------------- #


def test_card_good() -> None:
    card = Card(
        name='Lightning Bolt',
        oracle_id='4457ed35-7c10-48c8-9b6c-cf9b3f31c0f7',
        mana_value=1.0,
        type_line='Instant',
        colors=['R'],
        color_identity=['R'],
        produced_mana=[],
        keywords=[],
        oracle_text='Lightning Bolt deals 3 damage to any target.',
    )
    assert card.name == 'Lightning Bolt'
    assert card.oracle_text is not None


def test_card_mana_cost_field() -> None:
    """`mana_cost` carries the raw Scryfall mana cost string; defaults to None."""
    card = Card(name='Cultivate', mana_cost='{2}{G}')
    assert card.mana_cost == '{2}{G}'
    assert Card(name='Mysterious Spoiler').mana_cost is None


def test_card_unresolved_name_only() -> None:
    """An unresolved card (pre-release / not yet in the catalog) is name-only."""
    card = Card(name='Mysterious Spoiler')
    assert card.name == 'Mysterious Spoiler'
    assert card.oracle_id is None
    assert card.mana_value is None
    assert card.mana_cost is None
    assert card.type_line is None
    assert card.colors == []
    assert card.oracle_text is None


def test_card_dim_presentation_fields() -> None:
    """#5 card-dim presentation fields carry through and default to None."""
    card = Card(
        name='Llanowar Elves',
        power='1',
        toughness='1',
        art_crop='https://cards.scryfall.io/art_crop/front/6/a/6a0b230b.jpg',
        scryfall_uri='https://scryfall.com/card/fdn/227/llanowar-elves',
        set_name='Foundations',
    )
    assert card.power == '1'
    assert card.toughness == '1'
    assert card.art_crop.startswith('https://')
    assert card.scryfall_uri.startswith('https://')
    assert card.set_name == 'Foundations'


def test_card_dim_presentation_defaults_none() -> None:
    """A name-only Card still validates; presentation fields default to None."""
    card = Card(name='Mysterious Spoiler')
    assert card.power is None
    assert card.toughness is None
    assert card.art_crop is None
    assert card.scryfall_uri is None
    assert card.set_name is None


def test_card_dim_otag_fields() -> None:
    """#5 otag fields carry through and default to empty lists."""
    card = Card(name='Academy Manufactor', otag_buckets=['ramp', 'draw'], otags=['ramp', 'card-draw'])
    assert card.otag_buckets == ['ramp', 'draw']
    assert card.otags == ['ramp', 'card-draw']
    # Defaulted empty on a name-only card.
    bare = Card(name='Bare')
    assert bare.otag_buckets == []
    assert bare.otags == []


def test_card_missing_name_rejected() -> None:
    with pytest.raises(ValidationError):
        Card()  # type: ignore[call-arg]


def test_card_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Card(
            name='Bad CMC',
            mana_value='not-a-number',  # type: ignore[arg-type]
        )


def test_card_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        Card(name='Extra', surprise='not allowed')  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# OwnedCard — Card + ownership facts
# --------------------------------------------------------------------------- #


def test_owned_card_good() -> None:
    owned = OwnedCard(
        name='Sol Ring',
        oracle_id='abc',
        mana_value=1.0,
        type_line='Artifact',
        owned=3,
        foil=1,
        condition=['NM'],
        sets=['C21'],
        sources=['Commander 2021'],
        airtable_record_id='recABC',
    )
    assert owned.owned == 3
    assert owned.foil == 1
    assert owned.name == 'Sol Ring'
    # Inherits base Card enrichment.
    assert owned.mana_value == 1.0


def test_owned_card_defaults() -> None:
    owned = OwnedCard(name='Island')
    assert owned.owned == 0
    assert owned.foil == 0
    assert owned.condition == []
    assert owned.sets == []
    assert owned.sources == []
    assert owned.airtable_record_id is None
    # Base enrichment nullable / empty when unresolved.
    assert owned.oracle_id is None


def test_owned_card_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        OwnedCard(name='X', bananas=1)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# ChaseCard — Card + acquisition intent
# --------------------------------------------------------------------------- #


def test_chase_card_good() -> None:
    chase = ChaseCard(
        name='The One Ring',
        priority=1,
        for_decks=['gruul'],
        status='wanted',
        target_price=25.0,
    )
    assert chase.priority == 1
    assert chase.for_decks == ['gruul']
    assert chase.status == 'wanted'


def test_chase_card_defaults() -> None:
    chase = ChaseCard(name='Unreleased Card')
    assert chase.priority is None
    assert chase.for_decks == []
    assert chase.status is None
    assert chase.target_price is None


def test_chase_card_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        ChaseCard(name='X', mystery='y')  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# DeckCard — Card + how it participates in a deck
# --------------------------------------------------------------------------- #


def test_deck_card_good() -> None:
    dc = DeckCard(name='Sol Ring', quantity=1)
    assert dc.quantity == 1
    assert dc.role is None


def test_deck_card_commander_role() -> None:
    dc = DeckCard(name='Grumgully, the Generous', role='commander')
    assert dc.role == 'commander'
    assert dc.quantity == 1  # default


def test_deck_card_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        DeckCard(name='X', foil=True)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Deck — has-many DeckCard; commanders is a DERIVED property
# --------------------------------------------------------------------------- #


def test_deck_good() -> None:
    deck = Deck(
        name='Gruul Aggro',
        strategy='Go-wide aggro.',
        cards=[
            DeckCard(name='Grumgully, the Generous', role='commander'),
            DeckCard(name='Sol Ring', quantity=1),
            DeckCard(name='Island', quantity=10),
        ],
        airtable_record_id='recABC123',
    )
    assert len(deck.cards) == 3


def test_deck_commanders_is_derived_property() -> None:
    deck = Deck(
        name='Gruul Aggro',
        cards=[
            DeckCard(name='Grumgully, the Generous', role='commander'),
            DeckCard(name='Sol Ring', quantity=1),
        ],
    )
    commanders = deck.commanders
    assert [c.name for c in commanders] == ['Grumgully, the Generous']
    # It is a property, not a settable field.
    with pytest.raises(ValidationError):
        Deck(name='X', cards=[], commanders=['Y'])  # type: ignore[call-arg]


def test_deck_defaults() -> None:
    deck = Deck(name='Empty Deck', cards=[])
    assert deck.cards == []
    assert deck.commanders == []
    assert deck.strategy is None
    assert deck.assessment is None
    assert deck.focus_otags == []
    assert deck.airtable_record_id is None


def test_deck_assessment_and_focus_otags() -> None:
    deck = Deck(
        name='Gruul Aggro',
        strategy='Go-wide aggro.',
        assessment='Solid aggro but thin on removal.',
        focus_otags=['sacrifice', 'aristocrats'],
        cards=[DeckCard(name='Grumgully, the Generous', role='commander')],
    )
    assert deck.assessment == 'Solid aggro but thin on removal.'
    assert deck.focus_otags == ['sacrifice', 'aristocrats']


def test_deck_assessment_focus_otags_roundtrip() -> None:
    deck = Deck(
        name='Gruul Aggro',
        assessment='Reality synthesis.',
        focus_otags=['sacrifice'],
        cards=[DeckCard(name='Sol Ring')],
    )
    restored = Deck.model_validate(deck.model_dump())
    assert restored.assessment == 'Reality synthesis.'
    assert restored.focus_otags == ['sacrifice']


def test_deck_format_defaults_none_and_untargeted() -> None:
    deck = Deck(name='WIP Deck', cards=[])
    assert deck.format is None
    assert deck.target_size is None


def test_deck_format_derives_target_size() -> None:
    commander = Deck(name='EDH Deck', format='Commander', cards=[])
    assert commander.format == 'Commander'
    assert commander.target_size == 100
    standard = Deck(name='Std Deck', format='Standard', cards=[])
    assert standard.target_size == 60
    weird = Deck(name='Odd Deck', format='Weird', cards=[])
    assert weird.target_size is None


def test_deck_format_roundtrip() -> None:
    deck = Deck(name='EDH Deck', format='Commander', cards=[DeckCard(name='Sol Ring')])
    restored = Deck.model_validate(deck.model_dump())
    assert restored.format == 'Commander'
    assert restored.target_size == 100


def test_deck_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        Deck(cards=[])  # type: ignore[call-arg]


def test_deck_cards_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Deck(name='Bad', cards=[{'quantity': 1}])  # missing name


def test_deck_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        Deck(name='X', cards=[], bogus='commander')  # type: ignore[call-arg]


def test_deck_roundtrip_model_dump() -> None:
    deck = Deck(
        name='Gruul Aggro',
        cards=[DeckCard(name='Grumgully, the Generous', role='commander')],
    )
    dumped = deck.model_dump()
    # commanders is derived — not serialized as a field.
    assert 'commanders' not in dumped
    restored = Deck.model_validate(dumped)
    assert restored.commanders[0].name == 'Grumgully, the Generous'


# --------------------------------------------------------------------------- #
# FactSheet
# --------------------------------------------------------------------------- #


def test_factsheet_accepts_build_factsheet_shape() -> None:
    fs = FactSheet.model_validate(_factsheet_dict())
    # Existing keys preserved verbatim.
    assert fs.deck == 'Sokka Spellslinger'
    assert fs.shape.nonland_count == 63
    assert fs.shape.cmc_histogram['7+'] == 3
    assert fs.mana.pip_counts['U'] == 20
    assert fs.interaction.instant_speed == 22
    assert fs.card_advantage.repeatable_draw == 5
    assert fs.structural.graveyard_recursion_present is True
    assert fs.coverage.uncategorized_pct == 28.57
    assert fs.cards[0].name == 'Sol Ring'
    assert fs.missing == ['Some Unresolved Card Name']


def test_factsheet_forward_looking_fields_default_empty() -> None:
    fs = FactSheet.model_validate(_factsheet_dict())
    assert fs.otag_buckets == {}
    assert fs.susceptibility == []


def test_factsheet_forward_looking_fields_populate() -> None:
    d = _factsheet_dict()
    d['otag_buckets'] = {'ramp': 9, 'removal': 11}
    d['susceptibility'] = ['no graveyard hate', 'combo present']
    fs = FactSheet.model_validate(d)
    assert fs.otag_buckets['removal'] == 11
    assert 'combo present' in fs.susceptibility


def test_factsheet_focus_fields_default_empty() -> None:
    fs = FactSheet.model_validate(_factsheet_dict())
    assert fs.focus == []
    assert fs.focus_relative.coverage_of_focus == {}
    assert fs.focus_relative.thin_focus == []
    assert fs.focus_relative.off_focus == []


def test_factsheet_focus_fields_populate() -> None:
    d = _factsheet_dict()
    d['focus'] = ['counters', 'tokens', 'typal']
    d['focus_relative'] = {
        'coverage_of_focus': {'counters': 5, 'tokens': 3, 'typal': 1},
        'thin_focus': ['typal'],
        'off_focus': ['ramp', 'draw'],
    }
    fs = FactSheet.model_validate(d)
    assert fs.focus == ['counters', 'tokens', 'typal']
    assert fs.focus_relative.coverage_of_focus['counters'] == 5
    assert fs.focus_relative.thin_focus == ['typal']
    assert 'ramp' in fs.focus_relative.off_focus


def test_factsheet_roundtrip_preserves_keys() -> None:
    d = _factsheet_dict()
    fs = FactSheet.model_validate(d)
    dumped = fs.model_dump()
    for key in d:
        assert key in dumped


def test_factsheet_missing_required_rejected() -> None:
    d = _factsheet_dict()
    del d['shape']
    with pytest.raises(ValidationError):
        FactSheet.model_validate(d)


def test_factsheet_wrong_type_rejected() -> None:
    d = _factsheet_dict()
    d['shape']['nonland_count'] = 'sixty-three'
    with pytest.raises(ValidationError):
        FactSheet.model_validate(d)


# --------------------------------------------------------------------------- #
# Trade — stands alone (a movement event, not a card)
# --------------------------------------------------------------------------- #


def test_trade_good() -> None:
    trade = Trade(
        date='2026-07-20',
        from_source='Library',
        to_destination='Deck',
        to_deck='Sokka Spellslinger',
        cards_in=['Lightning Bolt'],
        cards_out=[],
        status='Completed',
    )
    assert trade.from_source == 'Library'
    assert trade.to_destination == 'Deck'


def test_trade_defaults() -> None:
    trade = Trade(from_source='Store', to_destination='Library')
    assert trade.cards_in == []
    assert trade.cards_out == []
    assert trade.status is None


def test_trade_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        Trade(from_source='Library')  # type: ignore[call-arg]


def test_trade_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Trade(from_source='Library', to_destination='Deck', cards_in='Sol Ring')  # type: ignore[arg-type]


def test_trade_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        Trade(from_source='Library', to_destination='Deck', mystery='x')  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Spoiler — a reconciled preview row (MythicSpoiler <-> Scryfall)
# --------------------------------------------------------------------------- #


def test_spoiler_good() -> None:
    spoiler = Spoiler(
        slug='new-mythic-creature',
        set_code='EOE',
        name='New Mythic Creature',
        oracle_id='4457ed35-7c10-48c8-9b6c-cf9b3f31c0f7',
        source='scryfall',
        first_seen_cursor='2026-07-27T00:00:00+00:00',
        confirmed=True,
    )
    assert spoiler.slug == 'new-mythic-creature'
    assert spoiler.set_code == 'EOE'
    assert spoiler.source == 'scryfall'
    assert spoiler.confirmed is True


def test_spoiler_defaults() -> None:
    """Unconfirmed preview: oracle_id/first_seen_cursor null, confirmed False."""
    spoiler = Spoiler(slug='mystery', set_code='EOE', name='Mystery Card', source='mythicspoiler')
    assert spoiler.oracle_id is None
    assert spoiler.first_seen_cursor is None
    assert spoiler.confirmed is False


def test_spoiler_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        Spoiler(slug='x', set_code='EOE', name='X')  # type: ignore[call-arg]  # missing source


def test_spoiler_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        Spoiler(slug='x', set_code='EOE', name='X', source='scryfall', mystery='y')  # type: ignore[call-arg]


def test_deck_roles_partition_maindeck_commander_sideboard() -> None:
    """`maindeck`, `commanders`, and `sideboard` are disjoint and cover `cards`."""
    from pipeline.contracts import Deck, DeckCard

    deck = Deck(
        name='Roles',
        cards=[
            DeckCard(name='Atraxa, Praetors Voice', role='commander'),
            DeckCard(name='Sol Ring'),
            DeckCard(name='Forest', quantity=10),
            DeckCard(name='Pithing Needle', role='sideboard'),
            DeckCard(name='Naturalize', role='sideboard'),
        ],
    )
    assert [c.name for c in deck.commanders] == ['Atraxa, Praetors Voice']
    assert {c.name for c in deck.sideboard} == {'Pithing Needle', 'Naturalize'}
    assert {c.name for c in deck.maindeck} == {'Sol Ring', 'Forest'}
    # partition: disjoint + total, no double-count.
    partition = deck.maindeck + deck.commanders + deck.sideboard
    assert len(partition) == len(deck.cards)
    assert {id(c) for c in partition} == {id(c) for c in deck.cards}


def test_deck_no_sideboard_is_empty() -> None:
    from pipeline.contracts import Deck, DeckCard

    deck = Deck(name='NoSB', cards=[DeckCard(name='Sol Ring'), DeckCard(name='Island', quantity=17)])
    assert deck.sideboard == []
    assert {c.name for c in deck.maindeck} == {'Sol Ring', 'Island'}


# --------------------------------------------------------------------------- #
# DeckCard.role validation (S4): normalize known roles, reject unknown ones.
# --------------------------------------------------------------------------- #


def test_deckcard_role_rejects_unknown_value() -> None:
    """A typo'd/unknown non-empty role is a loud error, not a silent maindeck card."""
    with pytest.raises(ValidationError, match='role'):
        DeckCard(name='Sol Ring', role='sidebord')  # typo
    with pytest.raises(ValidationError, match='role'):
        DeckCard(name='Sol Ring', role='main')  # 'main' is spelled as role=None, not a value


def test_deckcard_role_normalizes_case_and_whitespace() -> None:
    """Known roles are canonicalized (case/whitespace-insensitive) so a hand-typed
    `role: Sideboard` correctly becomes a sideboard card rather than silently maindeck."""
    assert DeckCard(name='X', role='Sideboard').role == 'sideboard'
    assert DeckCard(name='X', role=' commander ').role == 'commander'
    assert DeckCard(name='X', role='COMMANDER').role == 'commander'


def test_deckcard_role_empty_is_maindeck() -> None:
    """An empty / whitespace-only role means no role (maindeck), not an error."""
    assert DeckCard(name='X', role='').role is None
    assert DeckCard(name='X', role='   ').role is None
    assert DeckCard(name='X', role=None).role is None
