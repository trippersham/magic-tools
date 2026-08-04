"""Phase 4 — PREVENTION: unit tests for the backend-agnostic guard helpers.

Covers :func:`decks_linking`, :func:`remove_impact` (size_after / goes_under_target
math for multi- and single-copy refs), and :func:`shrink_check` (the precise
at-target-only rule). Uses a tiny in-memory `CollectionStore` double plus a real
`LocalYamlStore` for parity — no network, no creds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store as _store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.collection.guards import DeckImpact, decks_linking, remove_impact, shrink_check
from pipeline.contracts import Card, Deck, DeckCard


class _StubResolver:
    def get_card(self, name: str) -> Card | None:
        return None


def _deck(name: str, cards: list[DeckCard], *, fmt: str | None = None) -> Deck:
    return Deck(name=name, format=fmt, cards=cards)


class _FakeStore:
    """A minimal `CollectionStore` double: only `list_decks` is exercised here."""

    def __init__(self, decks: list[Deck]) -> None:
        self._decks = decks

    def list_decks(self) -> list[Deck]:
        return list(self._decks)


# --------------------------------------------------------------------------- #
# decks_linking
# --------------------------------------------------------------------------- #


def test_decks_linking_matches_by_name_case_insensitive() -> None:
    a = _deck('A', [DeckCard(name='Sol Ring'), DeckCard(name='Forest', quantity=10)])
    b = _deck('B', [DeckCard(name='sol ring')])  # different case
    c = _deck('C', [DeckCard(name='Llanowar Elves')])
    hits = decks_linking(_FakeStore([a, b, c]), 'SOL RING')
    assert {d.name for d in hits} == {'A', 'B'}


def test_decks_linking_includes_commander() -> None:
    a = _deck('Cmd', [DeckCard(name='Grumgully, the Generous', role='commander')])
    assert [d.name for d in decks_linking(_FakeStore([a]), 'Grumgully, the Generous')] == ['Cmd']


def test_decks_linking_empty_when_unlinked() -> None:
    a = _deck('A', [DeckCard(name='Sol Ring')])
    assert decks_linking(_FakeStore([a]), 'Black Lotus') == []


# --------------------------------------------------------------------------- #
# remove_impact — math
# --------------------------------------------------------------------------- #


def test_remove_impact_single_copy_goes_under_target() -> None:
    # A 100-card Commander deck; removing a 1-of drops it to 99 -> under target.
    cards = [DeckCard(name='Sol Ring')] + [DeckCard(name=f'Card {i}') for i in range(99)]
    deck = _deck('Legal', cards, fmt='Commander')
    assert sum(c.quantity for c in cards) == 100
    [imp] = remove_impact(_FakeStore([deck]), 'Sol Ring')
    assert isinstance(imp, DeckImpact)
    assert imp.current_size == 100
    assert imp.target == 100
    assert imp.size_after == 99
    assert imp.goes_under_target is True


def test_remove_impact_multi_copy_subtracts_full_quantity() -> None:
    # A 60-card deck with 4 copies of a card; removing it drops by 4.
    cards = [DeckCard(name='Lightning Bolt', quantity=4)] + [DeckCard(name=f'C{i}') for i in range(56)]
    deck = _deck('Burn', cards, fmt='Modern')
    assert sum(c.quantity for c in cards) == 60
    [imp] = remove_impact(_FakeStore([deck]), 'Lightning Bolt')
    assert imp.current_size == 60
    assert imp.target == 60
    assert imp.size_after == 56  # 60 - 4
    assert imp.goes_under_target is True


def test_remove_impact_untargeted_never_under_target() -> None:
    deck = _deck('Kitchen Table', [DeckCard(name='Sol Ring'), DeckCard(name='Forest', quantity=5)])
    [imp] = remove_impact(_FakeStore([deck]), 'Sol Ring')
    assert imp.target is None
    assert imp.size_after == 5
    assert imp.goes_under_target is False


def test_remove_impact_over_target_stays_legal() -> None:
    # 101 cards; removing a 1-of -> 100, still meets target -> not under.
    cards = [DeckCard(name='Sol Ring')] + [DeckCard(name=f'C{i}') for i in range(100)]
    deck = _deck('Fat', cards, fmt='Commander')
    [imp] = remove_impact(_FakeStore([deck]), 'Sol Ring')
    assert imp.current_size == 101
    assert imp.size_after == 100
    assert imp.goes_under_target is False


def test_remove_impact_enumerates_every_linking_deck() -> None:
    d1 = _deck('D1', [DeckCard(name='Sol Ring')] + [DeckCard(name=f'a{i}') for i in range(99)], fmt='Commander')
    d2 = _deck('D2', [DeckCard(name='Sol Ring')] + [DeckCard(name=f'b{i}') for i in range(50)], fmt='Commander')
    d3 = _deck('D3', [DeckCard(name='Forest', quantity=5)])  # no Sol Ring
    impacts = remove_impact(_FakeStore([d1, d2, d3]), 'Sol Ring')
    assert {i.deck_name for i in impacts} == {'D1', 'D2'}
    assert all(i.goes_under_target for i in impacts)


# --------------------------------------------------------------------------- #
# shrink_check — the precise rule
# --------------------------------------------------------------------------- #


def _sized(name: str, n: int, fmt: str | None) -> Deck:
    return _deck(name, [DeckCard(name=f'{name}-c{i}') for i in range(n)], fmt=fmt)


def test_shrink_check_at_target_dropping_under_is_true() -> None:
    prior = _sized('D', 100, 'Commander')
    new = _sized('D', 98, 'Commander')
    assert shrink_check(prior, new) is True


def test_shrink_check_building_up_is_false() -> None:
    # 0 -> 99: prior never met target, so building never trips.
    prior = _sized('D', 0, 'Commander')
    new = _sized('D', 99, 'Commander')
    assert shrink_check(prior, new) is False


def test_shrink_check_already_under_target_is_false() -> None:
    # 98 -> 97: prior was ALREADY under target -> shrink_check does NOT re-guard
    # (documented: already-under decks aren't re-guarded here; remove_card still
    # enumerates them).
    prior = _sized('D', 98, 'Commander')
    new = _sized('D', 97, 'Commander')
    assert shrink_check(prior, new) is False


def test_shrink_check_untargeted_is_false() -> None:
    prior = _sized('D', 100, None)
    new = _sized('D', 10, None)
    assert shrink_check(prior, new) is False


def test_shrink_check_growing_at_target_is_false() -> None:
    prior = _sized('D', 100, 'Commander')
    new = _sized('D', 101, 'Commander')
    assert shrink_check(prior, new) is False


# --------------------------------------------------------------------------- #
# Local (LocalYamlStore) parity for the guard helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def local_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalYamlStore:
    monkeypatch.setenv(_store.ENV_DATA_DIR, str(tmp_path / 'data'))
    return LocalYamlStore(resolver=_StubResolver())


def test_guards_parity_on_local_store(local_store: LocalYamlStore) -> None:
    cards = [DeckCard(name='Sol Ring')] + [DeckCard(name=f'C{i}') for i in range(99)]
    local_store.save_deck(_deck('EDH', cards, fmt='Commander'))
    local_store.save_deck(_deck('Other', [DeckCard(name='Island', quantity=20)]))

    assert {d.name for d in decks_linking(local_store, 'Sol Ring')} == {'EDH'}
    [imp] = remove_impact(local_store, 'Sol Ring')
    assert imp.deck_name == 'EDH'
    assert imp.current_size == 100
    assert imp.size_after == 99
    assert imp.goes_under_target is True


# --------------------------------------------------------------------------- #
# Defensive save_deck(allow_shrink=...) on the adapter (bypasses the CLI)
# --------------------------------------------------------------------------- #


def test_local_save_deck_shrink_raises_without_allow_shrink(local_store: LocalYamlStore) -> None:
    from pipeline.collection.errors import CollectionError

    full = _deck('EDH', [DeckCard(name=f'C{i}') for i in range(100)], fmt='Commander')
    local_store.save_deck(full)
    # The SAME deck (same uuid) re-saved shrunk — a read-modify-write, not a new,
    # coincidentally same-named deck (which would land on its own file, P3).
    shrunk = full.model_copy(update={'cards': [DeckCard(name=f'C{i}') for i in range(98)]})
    with pytest.raises(CollectionError, match='below its target of 100'):
        local_store.save_deck(shrunk)  # default is SAFE
    # allow_shrink=True proceeds.
    local_store.save_deck(shrunk, allow_shrink=True)
    assert sum(c.quantity for c in local_store.get_deck('EDH').cards) == 98


def test_local_save_deck_build_does_not_raise(local_store: LocalYamlStore) -> None:
    # Creating a deck under target is a build, not a shrink -> no allow_shrink needed.
    local_store.save_deck(_deck('WIP', [DeckCard(name=f'C{i}') for i in range(40)], fmt='Commander'))
    assert sum(c.quantity for c in local_store.get_deck('WIP').cards) == 40


# --------------------------------------------------------------------------- #
# Port-level remove_card cascade guard — the CRITICAL defense-in-depth.
#
# Phase 4 only guarded the CLI's remove-card; a direct get_store().remove_card
# was still a silent hard-DELETE. These prove the guard now lives AT THE ADAPTER
# primitive (both backends), so a programmatic caller can't cascade-strip decks.
# --------------------------------------------------------------------------- #


def test_local_remove_card_linked_raises_without_force_no_delete(local_store: LocalYamlStore) -> None:
    from pipeline.collection.errors import CollectionError

    # Sol Ring linked into TWO at-target (100-card) Commander decks.
    for name in ('Alpha', 'Beta'):
        cards = [DeckCard(name='Sol Ring')] + [DeckCard(name=f'{name}-c{i}') for i in range(99)]
        local_store.save_deck(_deck(name, cards, fmt='Commander'))
    local_store.add_card('Sol Ring', qty=1)

    # No force -> enumerates BOTH decks and refuses; the row is NOT deleted.
    with pytest.raises(CollectionError) as exc:
        local_store.remove_card('Sol Ring')
    msg = str(exc.value)
    assert 'Alpha' in msg
    assert 'Beta' in msg
    assert 'UNDER TARGET' in msg
    assert '100 -> 99' in msg
    assert 'Sol Ring' in {c.name for c in local_store.list_inventory()}

    # force=True proceeds with the delete.
    local_store.remove_card('Sol Ring', force=True)
    assert 'Sol Ring' not in {c.name for c in local_store.list_inventory()}


def test_local_remove_card_unlinked_removes_with_default_force(local_store: LocalYamlStore) -> None:
    # No deck links Black Lotus -> default force=False removes it cleanly.
    local_store.add_card('Black Lotus', qty=1)
    local_store.remove_card('Black Lotus')
    assert 'Black Lotus' not in {c.name for c in local_store.list_inventory()}


# --------------------------------------------------------------------------- #
# S2: the shrink guard must count the MAINDECK, not sideboard cards. A save that
# moves maindeck cards into the sideboard keeps the raw total but shrinks the
# real deck — the guard must still trip (this is the July-incident protection).
# --------------------------------------------------------------------------- #


def test_shrink_check_ignores_sideboard_cards() -> None:
    """Moving maindeck cards to the sideboard (raw total unchanged) still trips the guard."""
    prior = _deck('Legal60', [DeckCard(name=f'Card{i}') for i in range(60)], fmt='Modern')
    # 45 maindeck + 15 sideboard = 60 raw total, but only 45 count toward the 60 target.
    new_cards = [DeckCard(name=f'Card{i}') for i in range(45)] + [
        DeckCard(name=f'SB{i}', role='sideboard') for i in range(15)
    ]
    new = _deck('Legal60', new_cards, fmt='Modern')
    assert shrink_check(prior, new) is True


def test_shrink_check_sideboard_addition_does_not_trip() -> None:
    """Adding a sideboard to an at-target deck (maindeck unchanged) is NOT a shrink."""
    prior = _deck('Legal60', [DeckCard(name=f'Card{i}') for i in range(60)], fmt='Modern')
    new_cards = [DeckCard(name=f'Card{i}') for i in range(60)] + [
        DeckCard(name=f'SB{i}', role='sideboard') for i in range(15)
    ]
    new = _deck('Legal60', new_cards, fmt='Modern')
    assert shrink_check(prior, new) is False
