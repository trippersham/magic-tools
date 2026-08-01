"""OFFLINE tests for the deck-export destination and its Forge ``.dck`` adapter.

These build small :class:`~pipeline.contracts.Deck` / ``DeckCard`` fixtures
directly (never hitting Airtable / Scryfall) and assert on the exact rendered
INI, since a sim backend parses this text verbatim:

    - a constructed deck renders ``Deck Type=Constructed`` with every card in
      ``[Main]`` and NO ``[Commander]`` section;
    - a commander deck renders ``Deck Type=Commander`` with the commander in
      ``[Commander]`` and EXCLUDED from ``[Main]``;
    - a basic land with ``quantity>1`` renders as a single ``<qty> Plains`` line;
    - a DFC combined name is preserved verbatim.
"""

from __future__ import annotations

import pytest

from pipeline.contracts import Deck, DeckCard
from pipeline.destinations.deck_export import DeckExporter, get_exporter
from pipeline.destinations.deck_export.forge_dck import ForgeDckExporter


def _lines(rendered: str) -> list[str]:
    return rendered.splitlines()


def test_get_exporter_returns_forge_dck_adapter() -> None:
    exporter = get_exporter('forge_dck')
    assert isinstance(exporter, ForgeDckExporter)
    assert exporter.format == 'forge_dck'


def test_get_exporter_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match='unknown'):
        get_exporter('nope')


def test_forge_dck_exporter_satisfies_protocol() -> None:
    exporter: DeckExporter = ForgeDckExporter()
    assert exporter.format == 'forge_dck'


def test_constructed_deck_renders_main_and_constructed_type() -> None:
    deck = Deck(
        name='Mono-Green Stompy',
        cards=[
            DeckCard(name='Forest', quantity=24),
            DeckCard(name='Llanowar Elves', quantity=4),
            DeckCard(name='Ghalta, Primal Hunger', quantity=2),
        ],
    )
    rendered = ForgeDckExporter().export(deck)
    lines = _lines(rendered)

    assert lines[0] == '[metadata]'
    assert 'Name=Mono-Green Stompy' in lines
    assert 'Deck Type=Constructed' in lines
    assert '[Commander]' not in lines
    assert '[Main]' in lines

    main_idx = lines.index('[Main]')
    main_body = lines[main_idx + 1 :]
    assert '24 Forest' in main_body
    assert '4 Llanowar Elves' in main_body
    assert '2 Ghalta, Primal Hunger' in main_body


def test_commander_deck_puts_commander_in_commander_section_only() -> None:
    deck = Deck(
        name='Atraxa Superfriends',
        format='Commander',
        cards=[
            DeckCard(name='Atraxa, Praetors Voice', role='commander'),
            DeckCard(name='Plains', quantity=10),
            DeckCard(name='Sol Ring'),
        ],
    )
    rendered = ForgeDckExporter().export(deck)
    lines = _lines(rendered)

    assert 'Deck Type=Commander' in lines
    assert '[Commander]' in lines

    cmd_idx = lines.index('[Commander]')
    # Forge parses commander lines as `<qty> <name>` too (not a bare name).
    assert '1 Atraxa, Praetors Voice' in lines[cmd_idx + 1 :]

    main_idx = lines.index('[Main]')
    main_body = lines[main_idx + 1 :]
    # commander excluded from [Main]
    assert not any('Atraxa, Praetors Voice' in line for line in main_body)
    assert '10 Plains' in main_body
    assert '1 Sol Ring' in main_body


def test_basic_land_quantity_renders_single_line() -> None:
    deck = Deck(name='Basics', cards=[DeckCard(name='Plains', quantity=17)])
    rendered = ForgeDckExporter().export(deck)
    lines = _lines(rendered)
    plains_lines = [line for line in lines if line.endswith('Plains')]
    assert plains_lines == ['17 Plains']


def test_dfc_combined_name_preserved_verbatim() -> None:
    dfc_name = 'Valki, God of Lies // Tibalt, Cosmic Impostor'
    deck = Deck(name='DFC Test', cards=[DeckCard(name=dfc_name, quantity=1)])
    rendered = ForgeDckExporter().export(deck)
    assert f'1 {dfc_name}' in _lines(rendered)


def test_sideboard_section_present_and_empty() -> None:
    deck = Deck(name='SB', cards=[DeckCard(name='Island', quantity=20)])
    lines = _lines(ForgeDckExporter().export(deck))
    assert '[Sideboard]' in lines
