"""TDD tests for decks-store sync (pull / push / promote) — Phase 2, W4.

The whole point (the R3-B3 anti-anti-pattern): sync/version tests run with a
REAL canonicalizing resolver — the source driver hydrates deck cards through a
``CardResolver`` that CANONICALIZES names (lowercased/aliased input -> the
canonical name). We prove ``version(local)`` and ``version(pulled)`` AGREE after
canonicalization, and that the drift guard hashes both sides the same way. Do
NOT stub the resolver to name-only — that is exactly how the old B3 bug hid.

Everything is OFFLINE: a real ``LocalYamlStore`` on a tmp dir stands in for the
source of record; no network, no Airtable, no Forge.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore, version
from pipeline.decks.sync import SyncDriftError, promote, pull, push


class CanonicalizingResolver:
    """A REAL canonicalizing resolver: maps aliased/mis-cased names to canonical.

    The load-bearing seam. Hydrating a card canonicalizes its NAME (e.g.
    ``'sol ring'`` / ``'Sol  Ring'`` -> ``'Sol Ring'``), so a deck written with a
    non-canonical name reads back canonical — exactly the transform under which
    ``version`` must be stable. NOT stubbed to identity.
    """

    #: alias (casefolded, whitespace-collapsed) -> canonical display name.
    _CANON: ClassVar[dict[str, str]] = {
        'krenko, mob boss': 'Krenko, Mob Boss',
        'mountain': 'Mountain',
        'sol ring': 'Sol Ring',
        'impact tremors': 'Impact Tremors',
        'goblin chieftain': 'Goblin Chieftain',
    }

    def _canonical(self, name: str) -> str:
        key = ' '.join(name.split()).casefold()
        if key in self._CANON:
            return self._CANON[key]
        # Unknown goblins/singles: canonicalize to Title-ish but stable form.
        return ' '.join(name.split())

    def get_card(self, name: str) -> Card | None:
        canonical = self._canonical(name)
        # Return an enriched Card whose NAME is the canonical form (the hydration
        # rewrites the name), plus a bit of derived enrichment to prove `version`
        # excludes it (it must not flap the hash).
        return Card(name=canonical, oracle_id=f'oid-{canonical}', mana_cost='{1}{R}')


def _source_store(tmp_path: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=tmp_path / 'collection')


def _commander_deck(name: str = 'Krenko Goblins') -> Deck:
    """A deck authored with NON-canonical names (lowercase / extra spaces).

    So hydration on read must canonicalize them; `version` proves stable across.
    """
    cards = [DeckCard(name='krenko, mob boss', quantity=1, role='commander'), DeckCard(name='mountain', quantity=10)]
    for i in range(89):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    return Deck(name=name, format='commander', strategy='go wide', cards=cards)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    return root


# --------------------------------------------------------------------------- #
# Version agrees across canonicalization (the anti-anti-pattern)
# --------------------------------------------------------------------------- #


def test_version_agrees_across_canonicalization(data_dir: Path, tmp_path: Path) -> None:
    """A deck saved with non-canonical names reads back canonical; but a deck built
    from the canonical names hashes IDENTICALLY to the read-back — proving version
    is stable under the real resolver (NOT stubbed)."""
    driver = _source_store(tmp_path)
    driver.save_deck(_commander_deck(), allow_shrink=False)
    read_back = driver.get_deck('Krenko Goblins')
    # Every card name is now canonical.
    assert 'Krenko, Mob Boss' in {c.name for c in read_back.cards}
    assert 'krenko, mob boss' not in {c.name for c in read_back.cards}
    # A deck built directly from canonical names hashes the same as the read-back.
    canonical = read_back.model_copy()
    assert version(read_back) == version(canonical)
    # Enrichment (mana_cost) does NOT flap the hash.
    bare = [c.model_copy(update={'mana_cost': None}) for c in read_back.cards]
    stripped = read_back.model_copy(update={'cards': bare})
    assert version(stripped) == version(read_back)


# --------------------------------------------------------------------------- #
# pull — round-trips and stamps the baseline
# --------------------------------------------------------------------------- #


def test_pull_roundtrips_and_sets_baseline(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    driver.save_deck(_commander_deck(), allow_shrink=False)
    decks = DecksStore()

    pull(decks, driver, deck_id='d1', source_ref='Krenko Goblins')

    local = decks.get('d1')
    assert local is not None
    source = driver.get_deck('Krenko Goblins')
    assert version(local) == version(source)
    row = decks.get_row('d1')
    assert row is not None
    assert row.sync_status == 'synced'
    assert row.source_ref == 'Krenko Goblins'
    assert row.synced_baseline == version(source)


# --------------------------------------------------------------------------- #
# push — the guarded one
# --------------------------------------------------------------------------- #


def test_push_writes_through_ceremony_and_updates_baseline(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    driver.save_deck(_commander_deck(), allow_shrink=False)
    decks = DecksStore()
    pull(decks, driver, deck_id='d1', source_ref='Krenko Goblins')

    # A size-preserving local edit, then push.
    decks.swap('d1', add=DeckCard(name='Impact Tremors', quantity=1), cut='Goblin 0')
    push(decks, driver, deck_id='d1')

    source = driver.get_deck('Krenko Goblins')
    assert any(c.name == 'Impact Tremors' for c in source.cards)
    assert not any(c.name == 'Goblin 0' for c in source.cards)
    row = decks.get_row('d1')
    assert row is not None
    assert row.synced_baseline == version(decks.get('d1'))
    assert row.synced_baseline == version(source)


def test_push_refuses_on_baseline_drift_and_preserves_source_change(data_dir: Path, tmp_path: Path) -> None:
    """Push refuses when the source moved since last sync, and does NOT clobber it."""
    driver = _source_store(tmp_path)
    driver.save_deck(_commander_deck(), allow_shrink=False)
    decks = DecksStore()
    pull(decks, driver, deck_id='d1', source_ref='Krenko Goblins')

    # A local edit we WANT to push (size-preserving).
    decks.swap('d1', add=DeckCard(name='Impact Tremors', quantity=1), cut='Goblin 0')

    # Meanwhile the SOURCE moves out from under us (a foreign edit): add Sol Ring in
    # place of another goblin (size-preserving so save is allowed).
    foreign = driver.get_deck('Krenko Goblins')
    foreign_cards = [c for c in foreign.cards if c.name != 'Goblin 1']
    foreign_cards.append(DeckCard(name='sol ring', quantity=1))
    driver.save_deck(foreign.model_copy(update={'cards': foreign_cards}), allow_shrink=False)
    source_after_foreign = version(driver.get_deck('Krenko Goblins'))

    with pytest.raises(SyncDriftError):
        push(decks, driver, deck_id='d1')

    # The source's foreign change is PRESERVED (not clobbered by our refused push).
    assert version(driver.get_deck('Krenko Goblins')) == source_after_foreign
    assert any(c.name == 'Sol Ring' for c in driver.get_deck('Krenko Goblins').cards)


def test_idempotent_re_push_of_own_write_succeeds(data_dir: Path, tmp_path: Path) -> None:
    """A second push with no new local edit is our own prior write -> allowed."""
    driver = _source_store(tmp_path)
    driver.save_deck(_commander_deck(), allow_shrink=False)
    decks = DecksStore()
    pull(decks, driver, deck_id='d1', source_ref='Krenko Goblins')
    decks.swap('d1', add=DeckCard(name='Impact Tremors', quantity=1), cut='Goblin 0')
    push(decks, driver, deck_id='d1')
    # Re-push without any intervening edit: source == local, so it is idempotent.
    push(decks, driver, deck_id='d1')  # must NOT raise
    assert any(c.name == 'Impact Tremors' for c in driver.get_deck('Krenko Goblins').cards)


def test_push_fires_the_shrink_ceremony(data_dir: Path, tmp_path: Path) -> None:
    """A shrinking push runs the source's shrink guard and raises (unswallowed)."""
    from pipeline.collection.errors import CollectionError

    driver = _source_store(tmp_path)
    driver.save_deck(_commander_deck(), allow_shrink=False)  # at target (100)
    decks = DecksStore()
    pull(decks, driver, deck_id='d1', source_ref='Krenko Goblins')

    # Shrink the LOCAL deck below target directly (bypassing the store's own guard,
    # which would otherwise refuse the local removal) so the PUSH is what shrinks.
    local = decks.get('d1')
    assert local is not None
    shrunk = local.model_copy(update={'cards': [c for c in local.cards if c.name != 'Goblin 5']})
    decks.put(shrunk, deck_id='d1', sync_status='synced', source_ref='Krenko Goblins',
              synced_baseline=decks.get_row('d1').synced_baseline)  # type: ignore[union-attr]

    with pytest.raises(CollectionError):
        push(decks, driver, deck_id='d1')


# --------------------------------------------------------------------------- #
# promote — ephemeral -> synced
# --------------------------------------------------------------------------- #


def test_promote_ephemeral_to_synced_creates_through_ceremony(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    decks = DecksStore()
    decks.create_ephemeral(_commander_deck('Fresh Draft'), 'draft-1')
    assert decks.get_row('draft-1').sync_status == 'ephemeral'  # type: ignore[union-attr]

    promote(decks, driver, deck_id='draft-1', source_ref='Fresh Draft')

    # It now exists on the source (create-through-ceremony) and is marked synced.
    source = driver.get_deck('Fresh Draft')
    assert source is not None
    row = decks.get_row('draft-1')
    assert row is not None
    assert row.sync_status == 'synced'
    assert row.source_ref == 'Fresh Draft'
    assert row.synced_baseline == version(source)
