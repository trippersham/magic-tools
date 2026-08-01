"""OFFLINE tests for the CardExporter port + ForgeCardIndex availability oracle.

The index is built from tiny in-memory name sets or a hand-written ``.zip`` — no
real Forge, no network. These lock the DFC/case/basics normalization (the exact
logic that decides whether a card is "present at the target") and the card
exporter's render + validate contract.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pipeline.contracts import DeckCard
from pipeline.destinations.card_export import (
    ForgeDckCardExporter,
    get_card_exporter,
)
from pipeline.destinations.deck_export import IssueKind, Severity
from pipeline.sim.forge_card_index import ForgeCardIndex

# --------------------------------------------------------------------------- #
# ForgeCardIndex.has — normalization
# --------------------------------------------------------------------------- #


def test_has_is_case_insensitive() -> None:
    idx = ForgeCardIndex(frozenset({'Lightning Bolt'}))
    assert idx.has('lightning bolt')
    assert idx.has('LIGHTNING BOLT')


def test_has_normalizes_dfc_to_front_face() -> None:
    idx = ForgeCardIndex(frozenset({'Akoum Warrior', 'Kirol, Attentive First-Year'}))
    assert idx.has('Akoum Warrior // Akoum Teeth')  # combined → front
    assert idx.has('Akoum Warrior')  # front alone
    assert idx.has('Kirol, Attentive First-Year')


def test_has_is_unicode_normalization_insensitive() -> None:
    """An accented card matches across NFC/NFD forms — else a VALID deck hard-fails.

    Regression: names were only ``.strip().lower()``'d, so an index built from one
    Unicode normal form (e.g. Forge's cardsfolder ``Name:``) would miss a lookup in
    the other (e.g. Scryfall's), misclassifying real cards (Lim-Dûl's Vault,
    Jötun Grunt) as ABSENT_FROM_TARGET → a wrong pre-JVM hard-fail.
    """
    import unicodedata

    for name in ("Lim-Dûl's Vault", 'Jötun Grunt', 'Dandân'):
        nfc, nfd = unicodedata.normalize('NFC', name), unicodedata.normalize('NFD', name)
        assert nfc != nfd or name.isascii()  # sanity: these actually differ by form
        # Index built from the NFD form still matches an NFC lookup, and vice-versa.
        assert ForgeCardIndex(frozenset({nfd})).has(nfc)
        assert ForgeCardIndex(frozenset({nfc})).has(nfd)


def test_has_basics_always_present() -> None:
    idx = ForgeCardIndex(frozenset())  # empty real set
    for basic in ('Plains', 'Island', 'Swamp', 'Mountain', 'Forest', 'Wastes', 'Snow-Covered Island'):
        assert idx.has(basic)


def test_has_absent_returns_false() -> None:
    idx = ForgeCardIndex(frozenset({'Lightning Bolt'}))
    assert not idx.has('Pinnacle Kill-Ship')


def test_from_zip_reads_front_face_name_lines(tmp_path: Path) -> None:
    zp = tmp_path / 'cardsfolder.zip'
    with zipfile.ZipFile(zp, 'w') as zf:
        zf.writestr('a/lightning_bolt.txt', 'Name:Lightning Bolt\nManaCost:R\nA:...\n')
        # a DFC file: front Name: first, back Name: later — only the front is read.
        zf.writestr('a/akoum.txt', 'Name:Akoum Warrior\nTypes:Creature\nAltMode:\nName:Akoum Teeth\n')
        zf.writestr('not_a_card.md', 'ignore me')
    idx = ForgeCardIndex.from_zip(zp)
    assert idx.has('Lightning Bolt')
    assert idx.has('Akoum Warrior // Akoum Teeth')  # front-face resolved
    assert not idx.has('Totally Made Up')


def test_from_zip_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match='cardsfolder'):
        ForgeCardIndex.from_zip(tmp_path / 'nope.zip')


# --------------------------------------------------------------------------- #
# ForgeDckCardExporter — render + validate
# --------------------------------------------------------------------------- #


def test_card_exporter_render() -> None:
    exporter = ForgeDckCardExporter()
    assert exporter.render(DeckCard(name='Lightning Bolt', quantity=3)) == '3 Lightning Bolt'
    assert exporter.render(DeckCard(name='Plains', quantity=17)) == '17 Plains'


def test_card_exporter_validate_ok_when_present_and_resolved() -> None:
    exporter = get_card_exporter('forge_dck', availability=ForgeCardIndex(frozenset({'lightning bolt'})))
    assert exporter.validate(DeckCard(name='Lightning Bolt', oracle_id='o', quantity=1)) is None


def test_card_exporter_absent_outranks_unresolved() -> None:
    exporter = get_card_exporter('forge_dck', availability=ForgeCardIndex(frozenset({'lightning bolt'})))
    issue = exporter.validate(DeckCard(name='Nope', oracle_id=None, quantity=1))
    assert issue is not None
    assert issue.kind is IssueKind.ABSENT_FROM_TARGET
    assert issue.severity is Severity.BLOCKING


def test_card_exporter_unresolved_warning_without_availability() -> None:
    exporter = get_card_exporter('forge_dck')  # no availability oracle
    issue = exporter.validate(DeckCard(name='Whatever', oracle_id=None, quantity=1))
    assert issue is not None
    assert issue.kind is IssueKind.UNRESOLVED
    assert issue.severity is Severity.WARNING


def test_get_card_exporter_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match='unknown'):
        get_card_exporter('nope')
