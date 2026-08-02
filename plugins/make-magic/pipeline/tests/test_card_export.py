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


# --------------------------------------------------------------------------- #
# ForgeCardIndex.forge_deck_name — the loadable-name resolver
# --------------------------------------------------------------------------- #


def test_forge_deck_name_passthrough_for_known() -> None:
    """A normal card Forge loads is returned unchanged (original casing kept)."""
    idx = ForgeCardIndex(frozenset({'lightning bolt'}))
    assert idx.forge_deck_name('Lightning Bolt') == 'Lightning Bolt'
    assert idx.forge_deck_name('Plains') == 'Plains'  # basic


def test_forge_deck_name_front_face_for_mdfc() -> None:
    """A combined ``A // B`` MDFC (front face known, combined NOT a key) → front face."""
    idx = ForgeCardIndex(frozenset({'Akoum Warrior'}))
    assert idx.forge_deck_name('Akoum Warrior // Akoum Teeth') == 'Akoum Warrior'


def test_forge_deck_name_verbatim_when_combined_is_key() -> None:
    """A card Forge stores under its combined name (e.g. a true split) is NOT
    truncated — the input-as-key branch wins over the front-face split."""
    idx = ForgeCardIndex(frozenset({'Fire // Ice'}))
    assert idx.forge_deck_name('Fire // Ice') == 'Fire // Ice'


def test_forge_deck_name_none_for_backface_and_typo() -> None:
    """A back-face-only name and a typo are unloadable → ``None`` (blocked)."""
    idx = ForgeCardIndex(frozenset({'Akoum Warrior'}))
    assert idx.forge_deck_name('Akoum Teeth') is None  # back face, no ' // '
    assert idx.forge_deck_name('Zzznonexistent Bogus Card') is None


# --------------------------------------------------------------------------- #
# ForgeDckCardExporter.render — emits the loadable name
# --------------------------------------------------------------------------- #


def test_render_emits_front_face_for_mdfc_with_index() -> None:
    exporter = ForgeDckCardExporter(availability=ForgeCardIndex(frozenset({'Akoum Warrior'})))
    line = exporter.render(DeckCard(name='Akoum Warrior // Akoum Teeth', quantity=1))
    assert line == '1 Akoum Warrior'


def test_render_passthrough_normal_with_index_is_byte_stable() -> None:
    exporter = ForgeDckCardExporter(availability=ForgeCardIndex(frozenset({'lightning bolt'})))
    assert exporter.render(DeckCard(name='Lightning Bolt', quantity=3)) == '3 Lightning Bolt'


def test_render_autorepairs_mdfc_without_index() -> None:
    """Even with no availability oracle, a pure-string ``A // B`` is repaired to
    the front face — the common MDFC bug never reaches Forge."""
    line = ForgeDckCardExporter().render(DeckCard(name='Valki, God of Lies // Tibalt, Cosmic Impostor', quantity=1))
    assert line == '1 Valki, God of Lies'


def test_render_keeps_split_verbatim_with_index() -> None:
    exporter = ForgeDckCardExporter(availability=ForgeCardIndex(frozenset({'Fire // Ice'})))
    assert exporter.render(DeckCard(name='Fire // Ice', quantity=2)) == '2 Fire // Ice'


def test_has_folds_typographic_apostrophes() -> None:
    """A smart-quote name (macOS auto-substitution in hand-entered data) must
    match the ASCII-apostrophe form Forge/Scryfall use — in BOTH directions."""
    from pipeline.sim.forge_card_index import ForgeCardIndex

    ascii_index = ForgeCardIndex(frozenset(["Urza's Saga"]))
    assert ascii_index.has('Urza\u2019s Saga')  # U+2019 lookup vs ASCII index

    curly_index = ForgeCardIndex(frozenset(['Urza\u2019s Saga']))
    assert curly_index.has("Urza's Saga")  # ASCII lookup vs U+2019 index
