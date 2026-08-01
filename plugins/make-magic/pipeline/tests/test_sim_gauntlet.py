"""TDD tests for gauntlet opponent-set resolution (Phase 6, Deliverable A).

Everything here is OFFLINE. The ``curated`` source loads the bundled ``.dck``
files that ship as plugin data under ``pipeline/data/gauntlet/``; the ``mine``
source pulls decks from a MOCKED :class:`~pipeline.collection.CollectionStore`
(no Airtable, no creds) and renders them via the Forge ``.dck`` exporter; the
``both`` source merges the two.

The one thing NOT covered here is a live Airtable pull — that path is exercised
only under ``-m live`` elsewhere; here the store is a hand-built stub.
"""

from __future__ import annotations

from typing import cast

import pytest

from pipeline.collection import CollectionStore
from pipeline.contracts import Deck, DeckCard
from pipeline.sim.gauntlet import GauntletDeck, resolve_gauntlet


def test_curated_constructed_loads_bundled_decks() -> None:
    """``curated`` / ``constructed`` returns every bundled constructed ``.dck``.

    Each :class:`GauntletDeck` carries a non-empty name + non-empty dck text that
    looks like a Forge deck (a ``[Main]`` section). The v1 set ships ~5 decks.
    """
    decks = resolve_gauntlet('curated', 'constructed')

    assert len(decks) >= 5
    names = {d.name for d in decks}
    assert len(names) == len(decks)  # unique names
    for deck in decks:
        assert isinstance(deck, GauntletDeck)
        assert deck.name
        assert deck.dck_text.strip()
        assert '[Main]' in deck.dck_text


def test_curated_commander_loads_bundled_decks() -> None:
    """``curated`` / ``commander`` returns the bundled commander ``.dck``s.

    Each carries a ``[Commander]`` section (the whole point of the format).
    """
    decks = resolve_gauntlet('curated', 'commander')

    assert len(decks) >= 2
    for deck in decks:
        assert deck.dck_text.strip()
        assert '[Commander]' in deck.dck_text


def test_curated_unknown_format_is_empty() -> None:
    """An unknown format resolves to no curated decks (no dir -> empty, no raise)."""
    assert resolve_gauntlet('curated', 'pauper') == []


class _StubStore:
    """A minimal CollectionStore stand-in: name -> Deck, for the ``mine`` path.

    Only the two methods :func:`~pipeline.sim.gauntlet.resolve_gauntlet` calls
    (``list_decks`` / ``get_deck``) are implemented; :func:`_store` casts it to
    the full ``CollectionStore`` protocol so the partial mock type-checks.
    """

    def __init__(self, decks: dict[str, Deck]) -> None:
        self._decks = decks

    def list_decks(self) -> list[Deck]:
        return list(self._decks.values())

    def get_deck(self, name: str) -> Deck:
        return self._decks[name]


def _store(decks: dict[str, Deck]) -> CollectionStore:
    """A :class:`_StubStore` typed as the ``CollectionStore`` port (partial mock)."""
    return cast('CollectionStore', _StubStore(decks))


def _mono_red_deck(name: str) -> Deck:
    return Deck(
        name=name,
        format='Modern',
        cards=[
            DeckCard(name='Mountain', quantity=17),
            DeckCard(name='Goblin Piker', quantity=23),
        ],
    )


def _commander_deck(name: str) -> Deck:
    return Deck(
        name=name,
        format='Commander',
        cards=[
            DeckCard(name='Torsten Von Ursus', quantity=1, role='commander'),
            DeckCard(name='Forest', quantity=40),
        ],
    )


def test_mine_renders_decks_from_a_mocked_store() -> None:
    """``mine`` pulls decks from the (mocked) store and renders each to ``.dck``.

    No Airtable: the store is a stub. Each returned GauntletDeck carries the
    deck's name + the Forge-rendered dck text (``[Main]`` present).
    """
    store = _store({'Aggro': _mono_red_deck('Aggro'), 'Beats': _mono_red_deck('Beats')})

    decks = resolve_gauntlet('mine', 'constructed', store=store)

    assert {d.name for d in decks} == {'Aggro', 'Beats'}
    for deck in decks:
        assert '17 Mountain' in deck.dck_text
        assert '[Main]' in deck.dck_text


def test_mine_commander_filters_by_format() -> None:
    """``mine`` / ``commander`` returns only commander-format decks from the store."""
    store = _store(
        {
            'EDH': _commander_deck('EDH'),
            'Sixty': _mono_red_deck('Sixty'),
        }
    )

    decks = resolve_gauntlet('mine', 'commander', store=store)

    assert {d.name for d in decks} == {'EDH'}
    assert '[Commander]' in decks[0].dck_text


def test_both_merges_curated_and_mine() -> None:
    """``both`` merges the curated bundle with the (mocked) store's decks."""
    store = _store({'MyDeck': _mono_red_deck('MyDeck')})

    curated = resolve_gauntlet('curated', 'constructed')
    both = resolve_gauntlet('both', 'constructed', store=store)

    names = {d.name for d in both}
    assert 'MyDeck' in names
    assert {d.name for d in curated} <= names
    assert len(both) == len(curated) + 1


def test_unknown_source_raises() -> None:
    """An unknown source is a programming error — a clear ValueError, not silence."""
    with pytest.raises(ValueError, match='source'):
        resolve_gauntlet('bogus', 'constructed')


def test_mine_without_store_raises() -> None:
    """``mine`` needs a store; omitting it is an actionable error, not a crash."""
    with pytest.raises(ValueError, match='store'):
        resolve_gauntlet('mine', 'constructed')
