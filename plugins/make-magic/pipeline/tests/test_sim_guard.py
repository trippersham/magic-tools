"""OFFLINE tests for the sim's pre-JVM Forge-availability guard + --allow-missing.

The guard is what makes a deck with a Forge-absent card fail FAST (before a JVM
spawns) with an actionable error — or a warning under ``--allow-missing``. The
Forge card index is stubbed via ``from_install`` monkeypatch, so no real Forge is
needed.
"""

from __future__ import annotations

import pytest

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


def test_dck_card_names_parses_main_and_commander() -> None:
    text = (
        '[metadata]\nName=X\nDeck Type=Commander\n'
        '[Commander]\n1 Atraxa, Praetors Voice\n'
        '[Main]\n4 Lightning Bolt\n20 Island\n[Sideboard]\n'
    )
    assert sim_run._dck_card_names(text) == ['Atraxa, Praetors Voice', 'Lightning Bolt', 'Island']


def test_guard_raises_on_absent_card(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_index(monkeypatch, {'lightning bolt'})
    decks = [('MyDeck', '[Main]\n4 Lightning Bolt\n1 Pinnacle Kill-Ship\n')]
    with pytest.raises(sim_run.DeckExportError, match='Pinnacle Kill-Ship'):
        sim_run._guard_forge_availability(object(), decks, allow_missing=False)  # type: ignore[arg-type]


def test_guard_allow_missing_warns_not_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_index(monkeypatch, {'lightning bolt'})
    decks = [('MyDeck', '[Main]\n1 Pinnacle Kill-Ship\n')]
    sim_run._guard_forge_availability(object(), decks, allow_missing=True)  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert 'Pinnacle Kill-Ship' in err
    assert 'allow-missing' in err


def test_guard_passes_clean_deck(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_index(monkeypatch, {'lightning bolt'})
    # basics are always present; the DFC resolves to its front face.
    decks = [('D', '[Main]\n4 Lightning Bolt\n20 Mountain\n')]
    sim_run._guard_forge_availability(object(), decks, allow_missing=False)  # type: ignore[arg-type]


def test_guard_skips_when_index_unbuildable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A minimal install without cardsfolder.zip → can't build the index → skip
    # (Forge's own loader remains the backstop), so even an absent card doesn't raise.
    monkeypatch.setattr(ForgeCardIndex, 'from_install', classmethod(_raise_unbuildable))
    sim_run._guard_forge_availability(object(), [('D', '[Main]\n1 Pinnacle Kill-Ship\n')], allow_missing=False)  # type: ignore[arg-type]
