"""TDD tests for the edge contracts (Pydantic v2 boundary models).

Covers, per model: a good example validates; a bad example is rejected
(missing required / wrong type / extra field where forbidden). Plus:
    - FactSheet accepts a real build_factsheet()-shaped dict, verbatim.
    - A schema-drift guard: regenerate schemas into a temp dir and assert they
      byte-match the committed pipeline/contracts/schema/*.json.

No network. No imports of deck_factsheet.py (we replicate its output SHAPE in a
fixture so the contract can be verified without importing the script package).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.contracts import (
    Card,
    Deck,
    DeckLine,
    FactSheet,
    InventoryRow,
    TradeRow,
    export_schemas,
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
# Card
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


def test_card_optional_oracle_text() -> None:
    card = Card(
        name='Forest',
        oracle_id='b34bb2dc-c1af-4d77-b0b3-a0fb342a5fc6',
        mana_value=0.0,
        type_line='Basic Land — Forest',
    )
    assert card.oracle_text is None
    assert card.colors == []


def test_card_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        Card(name='No Oracle Id')  # type: ignore[call-arg]


def test_card_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Card(
            name='Bad CMC',
            oracle_id='x',
            mana_value='not-a-number',  # type: ignore[arg-type]
            type_line='Instant',
        )


def test_card_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        Card(
            name='Extra',
            oracle_id='x',
            mana_value=1.0,
            type_line='Instant',
            surprise='not allowed',  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# DeckLine
# --------------------------------------------------------------------------- #


def test_deckline_good() -> None:
    line = DeckLine(card_name='Sol Ring', quantity=1)
    assert line.oracle_id is None
    line2 = DeckLine(card_name='Lightning Bolt', quantity=4, oracle_id='abc')
    assert line2.oracle_id == 'abc'


def test_deckline_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        DeckLine(quantity=1)  # type: ignore[call-arg]


def test_deckline_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError):
        DeckLine(card_name='Sol Ring', quantity='one')  # type: ignore[arg-type]


def test_deckline_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        DeckLine(card_name='Sol Ring', quantity=1, foil=True)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Deck
# --------------------------------------------------------------------------- #


def test_deck_good() -> None:
    deck = Deck(
        name='Sokka Spellslinger',
        commanders=['Sokka, Master of Water'],
        strategy='Prowess and magecraft go wide.',
        lines=[
            DeckLine(card_name='Sol Ring', quantity=1),
            DeckLine(card_name='Island', quantity=10),
        ],
        airtable_record_id='recABC123',
    )
    assert len(deck.lines) == 2
    assert deck.commanders == ['Sokka, Master of Water']


def test_deck_defaults() -> None:
    deck = Deck(name='Empty Deck')
    assert deck.commanders == []
    assert deck.lines == []
    assert deck.strategy is None
    assert deck.airtable_record_id is None


def test_deck_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        Deck(commanders=['x'])  # type: ignore[call-arg]


def test_deck_lines_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Deck(name='Bad', lines=[{'card_name': 'Sol Ring'}])  # missing quantity


def test_deck_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        Deck(name='X', format='commander')  # type: ignore[call-arg]


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
    # Phase-4 forward fields default empty and don't break the current shape.
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
    # Focus-relative fields are OPTIONAL and default empty (deck declared no focus).
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
# InventoryRow (Airtable "Cards" table)
# --------------------------------------------------------------------------- #


def test_inventory_row_good() -> None:
    row = InventoryRow(
        card_name='Sol Ring',
        number_owned=3,
        foil_count=1,
        condition=['Near Mint'],
        sets=['Commander 2021'],
        card_type='Artifact',
        mana_cost='{1}',
        cmc=1.0,
        color_identity=['Colorless'],
        price_tcgplayer=2.49,
    )
    assert row.card_name == 'Sol Ring'
    assert row.number_owned == 3


def test_inventory_row_defaults() -> None:
    row = InventoryRow(card_name='Island')
    assert row.number_owned == 0
    assert row.condition == []
    assert row.price_tcgplayer is None


def test_inventory_row_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        InventoryRow(number_owned=1)  # type: ignore[call-arg]


def test_inventory_row_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError):
        InventoryRow(card_name='X', number_owned='three')  # type: ignore[arg-type]


def test_inventory_row_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        InventoryRow(card_name='X', bananas=1)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# TradeRow (Airtable "Trades" table)
# --------------------------------------------------------------------------- #


def test_trade_row_good() -> None:
    row = TradeRow(
        date='2026-07-20',
        from_source='Library',
        to_destination='Deck',
        to_deck='Sokka Spellslinger',
        cards_in=['Lightning Bolt'],
        cards_out=[],
        status='Completed',
    )
    assert row.from_source == 'Library'
    assert row.to_destination == 'Deck'


def test_trade_row_defaults() -> None:
    row = TradeRow(from_source='Store', to_destination='Library')
    assert row.cards_in == []
    assert row.cards_out == []
    assert row.status is None


def test_trade_row_missing_required_rejected() -> None:
    with pytest.raises(ValidationError):
        TradeRow(from_source='Library')  # type: ignore[call-arg]


def test_trade_row_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError):
        TradeRow(from_source='Library', to_destination='Deck', cards_in='Sol Ring')  # type: ignore[arg-type]


def test_trade_row_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        TradeRow(from_source='Library', to_destination='Deck', mystery='x')  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Schema export + drift guard
# --------------------------------------------------------------------------- #


def test_export_schemas_writes_all_models(tmp_path: Path) -> None:
    written = export_schemas.export_all(tmp_path)
    names = {p.name for p in written}
    assert names == {
        'Card.json',
        'DeckLine.json',
        'Deck.json',
        'FactSheet.json',
        'InventoryRow.json',
        'TradeRow.json',
    }
    for path in written:
        # Valid JSON, pretty-printed, trailing newline.
        text = path.read_text()
        json.loads(text)
        assert text.endswith('\n')


def test_committed_schemas_match_regeneration(tmp_path: Path) -> None:
    """Schema-drift guard: committed schema/*.json must byte-match a fresh export.

    Fails if a model changed without re-running the exporter.
    """
    committed_dir = export_schemas.SCHEMA_DIR
    assert committed_dir.exists(), (
        f'Committed schema dir missing: {committed_dir}. '
        'Run: uv run --project plugins/make-magic/pipeline '
        'python -m pipeline.contracts.export_schemas'
    )
    export_schemas.export_all(tmp_path)
    fresh = sorted(p.name for p in tmp_path.glob('*.json'))
    committed = sorted(p.name for p in committed_dir.glob('*.json'))
    assert fresh == committed, 'Set of schema files drifted'
    for name in fresh:
        fresh_bytes = (tmp_path / name).read_bytes()
        committed_bytes = (committed_dir / name).read_bytes()
        assert fresh_bytes == committed_bytes, f'{name} drifted from committed schema. Re-run the exporter.'
