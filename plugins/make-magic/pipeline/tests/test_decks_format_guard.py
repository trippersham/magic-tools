"""Guard test: the Decks ``Format`` field is HUMAN-OWNED.

``Format`` drives each deck's target size but is declared by a person, never by
the engine. It must therefore ride the write-back DENYLIST (present in a Decks
``required_fields`` set, absent from any ``derived_fields``) so
:func:`load_human_denylist` denies engine writes. This test pins that
classification and keeps the denylist/derived DISJOINTNESS assert green.
"""

from __future__ import annotations

from pipeline.destinations import airtable as wb


def test_format_is_in_decks_human_denylist() -> None:
    denied = wb.load_human_denylist(table_name='Decks')
    assert 'Format' in denied


def test_format_is_not_a_decks_derived_field() -> None:
    derived = wb.load_contract_derived_fields(table_name='Decks')
    assert 'Format' not in derived


def test_decks_denylist_and_derived_are_disjoint() -> None:
    denied = wb.load_human_denylist(table_name='Decks')
    derived = wb.load_contract_derived_fields(table_name='Decks')
    assert denied.isdisjoint(derived)
