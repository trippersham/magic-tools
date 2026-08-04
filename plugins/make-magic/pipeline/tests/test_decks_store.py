"""TDD tests for the DuckDB-backed ``DecksStore`` (Phase 1).

Everything is OFFLINE: an isolated tmp data root (via ``MAKE_MAGIC_DATA_DIR``)
backs the ``decks`` table in ``make_magic.duckdb``; no network. Covers:

    - a typed ``Deck`` round-trips through DuckDB (get == put, VALIDATED on read);
    - list / exists / create_ephemeral;
    - typed edits are quantity-aware + commander-safe BY CONSTRUCTION — the exact
      round-3 bugs (quantity-blind removal, two commanders, singleton-dup,
      cutting the sole commander) are made impossible, asserted here once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store
from pipeline.contracts import Deck, DeckCard
from pipeline.decks import DecksError, DecksStore


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at an isolated tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    return root


def _commander_deck(name: str = 'Krenko Goblins') -> Deck:
    """A 100-card-ish commander deck (a commander + a multi-copy basic + singles)."""
    cards = [DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander')]
    cards.append(DeckCard(name='Mountain', quantity=9))
    # Pad with singles so the deck sits AT its target (100) — so shrink guards bite.
    for i in range(90):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    return Deck(name=name, format='commander', strategy='go wide', cards=cards)


# --------------------------------------------------------------------------- #
# Round-trip / basic surface
# --------------------------------------------------------------------------- #


def test_put_get_roundtrip_is_validated_deck(data_dir: Path) -> None:
    s = DecksStore()
    deck = _commander_deck()
    s.put(deck, deck_id='d1')
    got = s.get('d1')
    assert isinstance(got, Deck)
    assert got.name == deck.name
    assert got.format == 'commander'
    assert {(c.name, c.quantity, c.role) for c in got.cards} == {
        (c.name, c.quantity, c.role) for c in deck.cards
    }


def test_get_missing_returns_none(data_dir: Path) -> None:
    assert DecksStore().get('nope') is None


def test_exists(data_dir: Path) -> None:
    s = DecksStore()
    assert not s.exists('d1')
    s.put(_commander_deck(), deck_id='d1')
    assert s.exists('d1')


def test_list_returns_all_decks(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck('A'), deck_id='a')
    s.put(_commander_deck('B'), deck_id='b')
    names = sorted(d.name for d in s.list())
    assert names == ['A', 'B']


def test_create_ephemeral(data_dir: Path) -> None:
    s = DecksStore()
    s.create_ephemeral(_commander_deck('Draft'), 'draft-1')
    assert s.exists('draft-1')
    assert s.get('draft-1').name == 'Draft'


def test_put_upserts(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck('First'), deck_id='d1')
    s.put(_commander_deck('Second'), deck_id='d1')
    assert s.get('d1').name == 'Second'
    assert len(s.list()) == 1


# --------------------------------------------------------------------------- #
# add_card / remove_card — quantity-aware (the R3 quantity-blind fix)
# --------------------------------------------------------------------------- #


def _qty_of(deck: Deck, name: str) -> int:
    return sum(c.quantity for c in deck.cards if c.name == name)


def test_add_card_increments_existing(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    s.add_card('d1', DeckCard(name='Mountain', quantity=1))
    assert _qty_of(s.get('d1'), 'Mountain') == 10


def test_add_card_adds_new_entry(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    s.add_card('d1', DeckCard(name='Lightning Bolt', quantity=1))
    assert _qty_of(s.get('d1'), 'Lightning Bolt') == 1


def test_remove_card_drops_one_copy_not_the_whole_entry(data_dir: Path) -> None:
    """A multi-copy basic loses ONE copy, not all of them (the R3 fix)."""
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    # Keep the deck at-target so this removal exercises quantity-awareness, not the
    # shrink guard: add one Mountain (100 -> 101), then remove one (101 -> 100).
    s.add_card('d1', DeckCard(name='Mountain', quantity=1))  # Mountain 9 -> 10, size 101
    s.remove_card('d1', 'Mountain', qty=1)  # Mountain 10 -> 9, size 100 (at target)
    got = s.get('d1')
    assert _qty_of(got, 'Mountain') == 9  # entry survives, one copy removed
    assert any(c.name == 'Mountain' for c in got.cards)


def test_remove_card_drops_entry_only_at_zero(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    # Goblin 0 is a single; removing 1 drops it entirely. Add one back elsewhere to
    # keep the deck at-target so the shrink guard does not fire on this removal.
    s.add_card('d1', DeckCard(name='Mountain', quantity=1))  # 100 -> 101
    s.remove_card('d1', 'Goblin 0', qty=1)  # 101 -> 100, still at target
    got = s.get('d1')
    assert not any(c.name == 'Goblin 0' for c in got.cards)


def test_remove_card_that_shrinks_at_target_deck_under_target_is_guarded(data_dir: Path) -> None:
    """A bare remove that would drop an at-target deck under target must be refused."""
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')  # exactly at target (100)
    with pytest.raises(DecksError):
        s.remove_card('d1', 'Goblin 0', qty=1)  # 100 -> 99 under target


# --------------------------------------------------------------------------- #
# set_* edits
# --------------------------------------------------------------------------- #


def test_set_strategy_assessment_focus(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    s.set_strategy('d1', 'aristocrats')
    s.set_assessment('d1', 'needs a sac outlet')
    s.set_focus_otags('d1', ['sacrifice', 'tokens'])
    got = s.get('d1')
    assert got.strategy == 'aristocrats'
    assert got.assessment == 'needs a sac outlet'
    assert got.focus_otags == ['sacrifice', 'tokens']


# --------------------------------------------------------------------------- #
# swap — size-preserving + commander-safe BY CONSTRUCTION
# --------------------------------------------------------------------------- #


def test_swap_is_size_preserving(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    before = sum(c.quantity for c in s.get('d1').cards)
    s.swap('d1', add=DeckCard(name='Impact Tremors', quantity=1), cut='Goblin 0')
    got = s.get('d1')
    after = sum(c.quantity for c in got.cards)
    assert after == before
    assert any(c.name == 'Impact Tremors' for c in got.cards)
    assert not any(c.name == 'Goblin 0' for c in got.cards)


def test_swap_refuses_when_cut_absent(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    with pytest.raises(DecksError):
        s.swap('d1', add=DeckCard(name='Impact Tremors', quantity=1), cut='Not In Deck')


def test_swap_cannot_cut_the_sole_commander(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    with pytest.raises(DecksError):
        s.swap('d1', add=DeckCard(name='Goblin Chieftain', quantity=1), cut='Krenko, Mob Boss')


def test_swap_cannot_create_a_second_commander(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    with pytest.raises(DecksError):
        s.swap('d1', add=DeckCard(name='Goblin Chieftain', quantity=1, role='commander'), cut='Goblin 0')


def test_swap_cannot_create_a_singleton_duplicate(data_dir: Path) -> None:
    """Adding a card already present (as a distinct entry) must not silently create
    two entries for the same name — the store keeps one entry per name."""
    s = DecksStore()
    s.put(_commander_deck(), deck_id='d1')
    # 'Sol Ring' is not in the base deck; add it, then a swap that re-adds it must
    # increment the existing entry (not spawn a duplicate row for the same name).
    s.add_card('d1', DeckCard(name='Sol Ring', quantity=1))
    s.swap('d1', add=DeckCard(name='Sol Ring', quantity=1), cut='Goblin 0')
    got = s.get('d1')
    entries = [c for c in got.cards if c.name == 'Sol Ring']
    assert len(entries) == 1
    assert entries[0].quantity == 2


# --------------------------------------------------------------------------- #
# archive lifecycle — hide experimental drafts from the default list
# --------------------------------------------------------------------------- #


def test_archive_hides_from_default_list_and_include_archived_shows_it(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck('Keep'), deck_id='keep')
    s.put(_commander_deck('Junk'), deck_id='junk')
    s.archive('junk')
    # Default list excludes the archived deck.
    assert sorted(d.name for d in s.list()) == ['Keep']
    # include_archived surfaces it again (recovery view).
    assert sorted(d.name for d in s.list(include_archived=True)) == ['Junk', 'Keep']


def test_unarchive_restores_to_default_list(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck('Junk'), deck_id='junk')
    s.archive('junk')
    assert s.list() == []
    s.unarchive('junk')
    assert [d.name for d in s.list()] == ['Junk']


def test_archived_survives_an_edit_put(data_dir: Path) -> None:
    """An edit/re-put must NOT silently reset the archived flag."""
    s = DecksStore()
    s.put(_commander_deck('Junk'), deck_id='junk')
    s.archive('junk')
    # An edit (typed) keeps it archived.
    s.set_strategy('junk', 'reworked')
    assert s.list() == []
    assert [d.name for d in s.list(include_archived=True)] == ['Junk']
    # A re-put (e.g. a re-pull of an unchanged source) also preserves archived.
    s.put(_commander_deck('Junk'), deck_id='junk')
    assert s.list() == []
    assert [d.name for d in s.list(include_archived=True)] == ['Junk']


def test_list_rows_filters_by_sync_status_and_archived(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_commander_deck('Synced'), deck_id='synced', sync_status='synced', source_ref='{"backend":"x"}')
    s.create_ephemeral(_commander_deck('Draft'), 'draft')
    s.create_ephemeral(_commander_deck('Archived Draft'), 'adraft')
    s.archive('adraft')

    ephemeral = s.list_rows(sync_status='ephemeral')
    assert [r.deck_id for r in ephemeral] == ['draft']
    assert all(r.sync_status == 'ephemeral' for r in ephemeral)

    ephemeral_all = s.list_rows(sync_status='ephemeral', include_archived=True)
    assert sorted(r.deck_id for r in ephemeral_all) == ['adraft', 'draft']

    synced = s.list_rows(sync_status='synced')
    assert [r.deck_id for r in synced] == ['synced']

    # No filter -> every non-archived row regardless of sync_status.
    everything = s.list_rows()
    assert sorted(r.deck_id for r in everything) == ['draft', 'synced']


def test_archive_absent_id_raises(data_dir: Path) -> None:
    s = DecksStore()
    with pytest.raises(DecksError):
        s.archive('nope')
    with pytest.raises(DecksError):
        s.unarchive('nope')
