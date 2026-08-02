"""OFFLINE tests for the sim's pre-JVM Forge-availability guard + --allow-missing.

The guard is what makes a deck with a Forge-absent card fail FAST (before a JVM
spawns) with an actionable error — or a warning under ``--allow-missing``. The
Forge card index is stubbed via ``from_install`` monkeypatch, so no real Forge is
needed.
"""

from __future__ import annotations

import pytest

from pipeline.contracts import Deck, DeckCard
from pipeline.sim import run as sim_run
from pipeline.sim.forge_card_index import ForgeCardIndex


def _patch_index(monkeypatch: pytest.MonkeyPatch, names: set[str]) -> None:
    monkeypatch.setattr(
        ForgeCardIndex,
        'from_install',
        classmethod(lambda cls, install: ForgeCardIndex(frozenset(names))),
    )


def _raise_unbuildable(cls: type, install: object) -> ForgeCardIndex:
    raise FileNotFoundError('no cardsfolder')


def _path_deck(name: str, text: str) -> sim_run._ResolvedDeck:
    """A resolved deck as if from a ``.dck`` PATH (no hydrated Deck)."""
    return sim_run._ResolvedDeck(name=name, text=text, deck=None)


def _store_deck(name: str, cards: list[DeckCard]) -> sim_run._ResolvedDeck:
    """A resolved deck as if from the store (hydrated Deck kept), text rendered from it."""
    deck = Deck(name=name, cards=cards)
    from pipeline.destinations.deck_export import get_exporter

    return sim_run._ResolvedDeck(name=name, text=get_exporter('forge_dck').export(deck), deck=deck)


def test_dck_card_names_parses_main_and_commander() -> None:
    text = (
        '[metadata]\nName=X\nDeck Type=Commander\n'
        '[Commander]\n1 Atraxa, Praetors Voice\n'
        '[Main]\n4 Lightning Bolt\n20 Island\n[Sideboard]\n'
    )
    assert sim_run._dck_card_names(text) == ['Atraxa, Praetors Voice', 'Lightning Bolt', 'Island']


def test_dck_card_names_includes_sideboard() -> None:
    """S4: the sideboard is validated too — a Forge-unloadable sideboard card must
    not slip past the guard on the ``.dck`` path (the Airtable path already checks it)."""
    text = '[Main]\n4 Lightning Bolt\n[Sideboard]\n2 Pyroblast\n'
    assert sim_run._dck_card_names(text) == ['Lightning Bolt', 'Pyroblast']


def test_dck_card_names_parses_real_forge_dck_format() -> None:
    """A .dck exported by Forge itself uses LOWERCASE section headers and
    ``name|SET`` / ``name|SET|art`` pinned printings (see forge/res/cube/*.dck).
    The guard must validate the NAME, not `name|SET` (a guaranteed index miss →
    a false BLOCKING error on a perfectly loadable deck), and must not silently
    skip a `[main]` section it fails to recognize. The sideboard is validated too
    (S4), so its cards are included with the name-only normalization.
    """
    text = (
        '[metadata]\nName=Cube\n'
        '[main]\n'
        '4 Lightning Bolt|M10|1\n'
        '1 Dauntless Bodyguard|DOM\n'
        '2 Island\n'
        '[sideboard]\n2 Pyroblast|ICE\n'
    )
    assert sim_run._dck_card_names(text) == ['Lightning Bolt', 'Dauntless Bodyguard', 'Island', 'Pyroblast']


def test_guard_raises_on_absent_card(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_index(monkeypatch, {'lightning bolt'})
    decks = [_path_deck('MyDeck', '[Main]\n4 Lightning Bolt\n1 Pinnacle Kill-Ship\n')]
    with pytest.raises(sim_run.DeckExportError, match='Pinnacle Kill-Ship'):
        sim_run._guard_forge_availability(object(), decks, allow_missing=False)  # type: ignore[arg-type]


def test_guard_allow_missing_warns_not_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_index(monkeypatch, {'lightning bolt'})
    decks = [_path_deck('MyDeck', '[Main]\n1 Pinnacle Kill-Ship\n')]
    sim_run._guard_forge_availability(object(), decks, allow_missing=True)  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert 'Pinnacle Kill-Ship' in err
    assert 'allow-missing' in err


def test_guard_passes_clean_deck(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_index(monkeypatch, {'lightning bolt'})
    # basics are always present; the DFC resolves to its front face.
    decks = [_path_deck('D', '[Main]\n4 Lightning Bolt\n20 Mountain\n')]
    sim_run._guard_forge_availability(object(), decks, allow_missing=False)  # type: ignore[arg-type]


def test_guard_absent_check_matches_via_exporter_for_store_deck(monkeypatch: pytest.MonkeyPatch) -> None:
    """A STORE-resolved deck routes through the exporter's validate (single source):
    an absent card in a hydrated Deck still hard-fails pre-JVM."""
    _patch_index(monkeypatch, {'lightning bolt'})
    decks = [_store_deck('S', [DeckCard(name='Lightning Bolt', quantity=4), DeckCard(name='Pinnacle Kill-Ship')])]
    with pytest.raises(sim_run.DeckExportError, match='Pinnacle Kill-Ship'):
        sim_run._guard_forge_availability(object(), decks, allow_missing=False)  # type: ignore[arg-type]


def test_guard_surfaces_unresolved_warning_for_store_deck(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A store deck with a PRESENT but name-only (no oracle_id) card → a WARNING,
    not a block — the UNRESOLVED signal now reaches the sim path."""
    _patch_index(monkeypatch, {'lightning bolt'})
    # Lightning Bolt is present (in the index) but oracle_id is None → UNRESOLVED warning.
    decks = [_store_deck('S', [DeckCard(name='Lightning Bolt', quantity=4)])]
    sim_run._guard_forge_availability(object(), decks, allow_missing=False)  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert 'Lightning Bolt' in err
    assert 'name-only' in err


def test_guard_no_unresolved_warning_for_dck_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``.dck`` PATH deck legitimately has no oracle_ids — do NOT spam an
    UNRESOLVED warning for every card; only ABSENT matters there."""
    _patch_index(monkeypatch, {'lightning bolt'})
    decks = [_path_deck('P', '[Main]\n4 Lightning Bolt\n20 Mountain\n')]
    sim_run._guard_forge_availability(object(), decks, allow_missing=False)  # type: ignore[arg-type]
    assert capsys.readouterr().err == ''  # present cards, no oracle noise


def test_guard_skips_when_index_unbuildable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A minimal install without cardsfolder.zip → can't build the index → skip
    # (Forge's own loader remains the backstop), so even an absent card doesn't raise.
    monkeypatch.setattr(ForgeCardIndex, 'from_install', classmethod(_raise_unbuildable))
    decks = [_path_deck('D', '[Main]\n1 Pinnacle Kill-Ship\n')]
    sim_run._guard_forge_availability(object(), decks, allow_missing=False)  # type: ignore[arg-type]
