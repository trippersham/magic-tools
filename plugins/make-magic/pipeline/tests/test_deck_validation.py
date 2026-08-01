"""OFFLINE tests for deck-name sanitization (0.3) + validate-on-export (0.4/0.5).

No Forge, no network: card availability is a real
:class:`~pipeline.sim.forge_card_index.ForgeCardIndex` built from a tiny hand-made
name set (its DFC/case/basics normalization is the real logic under test), and
decks are hand-built :class:`~pipeline.contracts.Deck` fixtures.
"""

from __future__ import annotations

import pytest

from pipeline.contracts import Deck, DeckCard
from pipeline.destinations.deck_export import (
    DeckExportError,
    IssueKind,
    Severity,
    export_checked,
    get_exporter,
    safe_deck_stem,
)
from pipeline.sim.forge_card_index import ForgeCardIndex


def _idx(*names: str) -> ForgeCardIndex:
    return ForgeCardIndex(frozenset(names))


# --------------------------------------------------------------------------- #
# 0.3 — safe_deck_stem
# --------------------------------------------------------------------------- #


def test_stem_replaces_path_illegal_chars() -> None:
    # The '/' bug: a real deck name must not become a sub-directory.
    assert safe_deck_stem('U/R Izzet (Chaos Sealed)') == 'U_R Izzet (Chaos Sealed)'
    assert '/' not in safe_deck_stem('a/b/c')
    assert safe_deck_stem('a:b*c?d"e') == 'a_b_c_d_e'


def test_stem_never_empty_and_bounded() -> None:
    assert safe_deck_stem('   ') == 'deck'
    assert safe_deck_stem('....') == 'deck'
    assert safe_deck_stem('///') == 'deck'
    assert len(safe_deck_stem('x' * 500)) <= 100


def test_stem_preserves_readable_names() -> None:
    assert safe_deck_stem('MonoRedAggro') == 'MonoRedAggro'
    assert safe_deck_stem('Boros Midrange 2.0') == 'Boros Midrange 2.0'


# --------------------------------------------------------------------------- #
# 0.4/0.5 — validate() severity + DFC/basics/absent
# --------------------------------------------------------------------------- #


def test_absent_card_is_blocking_dfc_and_basics_pass() -> None:
    idx = _idx('lightning bolt', 'akoum warrior')  # basics auto-present
    deck = Deck(
        name='T',
        cards=[
            DeckCard(name='Lightning Bolt', oracle_id='o1', quantity=4),
            DeckCard(name='Akoum Warrior // Akoum Teeth', oracle_id='o2', quantity=1),  # DFC → front
            DeckCard(name='Mountain', quantity=20),  # basic
            DeckCard(name='Pinnacle Kill-Ship', oracle_id='o3', quantity=1),  # absent
        ],
    )
    report = get_exporter('forge_dck', availability=idx).validate(deck)
    assert [i.card_name for i in report.blocking] == ['Pinnacle Kill-Ship']
    assert report.blocking[0].kind is IssueKind.ABSENT_FROM_TARGET


def test_unresolved_but_present_is_warning_not_blocking() -> None:
    idx = _idx('rapturous moment')  # Forge HAS it, even though our pipeline didn't resolve it
    deck = Deck(name='T', cards=[DeckCard(name='Rapturous Moment', oracle_id=None, quantity=1)])
    report = get_exporter('forge_dck', availability=idx).validate(deck)
    assert report.blocking == ()
    assert [i.kind for i in report.warnings] == [IssueKind.UNRESOLVED]
    assert report.warnings[0].severity is Severity.WARNING


def test_absent_outranks_unresolved() -> None:
    idx = _idx('lightning bolt')
    # name-only AND absent → the blocking (absent) issue wins, reported once.
    deck = Deck(name='T', cards=[DeckCard(name='Ghost Card', oracle_id=None, quantity=1)])
    report = get_exporter('forge_dck', availability=idx).validate(deck)
    assert [i.kind for i in report.issues] == [IssueKind.ABSENT_FROM_TARGET]


def test_validate_without_availability_reports_only_unresolved() -> None:
    deck = Deck(
        name='T',
        cards=[
            DeckCard(name='Name Only', oracle_id=None, quantity=1),
            DeckCard(name='Resolved', oracle_id='o', quantity=1),
        ],
    )
    report = get_exporter('forge_dck').validate(deck)  # no index injected
    assert [i.card_name for i in report.issues] == ['Name Only']
    assert report.issues[0].severity is Severity.WARNING


# --------------------------------------------------------------------------- #
# export_checked — the fail-before-emit gate
# --------------------------------------------------------------------------- #


def test_export_checked_strict_raises_and_names_the_card() -> None:
    idx = _idx('lightning bolt')
    deck = Deck(name='Bad Deck', cards=[DeckCard(name='Pinnacle Kill-Ship', oracle_id='o', quantity=1)])
    exporter = get_exporter('forge_dck', availability=idx)
    with pytest.raises(DeckExportError, match='Pinnacle Kill-Ship') as exc:
        export_checked(exporter, deck, strict=True)
    assert exc.value.report.blocking  # carries the report


def test_export_checked_lenient_returns_text_plus_report() -> None:
    idx = _idx('lightning bolt')
    deck = Deck(name='Bad Deck', cards=[DeckCard(name='Pinnacle Kill-Ship', oracle_id='o', quantity=1)])
    exporter = get_exporter('forge_dck', availability=idx)
    result = export_checked(exporter, deck, strict=False)  # --allow-missing semantics
    assert '1 Pinnacle Kill-Ship' in result.text
    assert [i.card_name for i in result.report.blocking] == ['Pinnacle Kill-Ship']


def test_export_stays_lenient_unchanged() -> None:
    # export() must NEVER validate — backward-compatible with all existing callers.
    deck = Deck(name='X', cards=[DeckCard(name='Pinnacle Kill-Ship', quantity=1)])
    text = get_exporter('forge_dck', availability=_idx()).export(deck)
    assert '1 Pinnacle Kill-Ship' in text
