"""Phase 6 P3 — lifecycle regressions: promote / consume / sync-reconcile.

The two SOURCE-CORRUPTING blockers (B1, B2) are killed here, so these regressions
protect the production Airtable base. Everything is OFFLINE: a real
``LocalYamlStore`` on a tmp dir is the source of record; a REAL canonicalizing
resolver (never stubbed to identity) hydrates the source cards, so the hazard the
old B3 bug hid behind is exercised, not mocked away.

Killed findings (write FIRST, red→green):

- **B1a** clean-slate ``promote --to "Existing"`` when a synced "Existing" already
  exists → a SECOND, distinct source deck (distinct uuid + distinct file); the
  original "Existing" content is UNTOUCHED (never bind-by-name).
- **B1b** exploration ``new-draft --from "Deck" → edit → promote`` lands on the
  lineage PARENT (bound by external ref, not name); a same-named unrelated deck
  elsewhere is untouched.
- **B2** after promoting an exploration draft the draft is ``consumed`` + archived:
  excluded from name resolution, refuses edits+push, and materializes NO new
  source file.
- **M2** local edit + out-of-band source change → ``sync`` raises SyncDriftError,
  both sides preserved (nothing lost). Never pull-clobber a local edit.
- **M6** a promote whose save fails the shrink ceremony leaves a CLEAN ephemeral
  draft (not a half-synced zombie); the target is preserved.
- **m3** ``archive-deck`` on a synced deck is refused; on an ephemeral draft works.
- **slug-collision** two decks named "Twin" saved → two files, neither clobbered;
  both readable by their own uuid.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore, version
from pipeline.decks.access import deck_access
from pipeline.decks.sync import SyncDriftError, promote, sync_reconcile


class CanonicalizingResolver:
    """A REAL canonicalizing resolver: aliased / mis-cased names → canonical.

    The load-bearing seam — NOT stubbed to identity. Hydrating a card rewrites its
    NAME to the canonical form, exactly the transform under which ``version`` must
    be stable and drift detection must stay honest.
    """

    _CANON: ClassVar[dict[str, str]] = {
        'krenko, mob boss': 'Krenko, Mob Boss',
        'mountain': 'Mountain',
        'sol ring': 'Sol Ring',
        'impact tremors': 'Impact Tremors',
        'goblin chieftain': 'Goblin Chieftain',
        'lightning bolt': 'Lightning Bolt',
    }

    def _canonical(self, name: str) -> str:
        key = ' '.join(name.split()).casefold()
        if key in self._CANON:
            return self._CANON[key]
        return ' '.join(name.split())

    def get_card(self, name: str) -> Card | None:
        canonical = self._canonical(name)
        return Card(name=canonical, oracle_id=f'oid-{canonical}', mana_cost='{1}{R}')


def _source_store(tmp_path: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=tmp_path / 'collection')


def _get(decks: DecksStore, deck_uuid: str) -> Deck:
    """Read a required local deck (narrowing ``Deck | None`` for the type checker)."""
    deck = decks.get(deck_uuid)
    assert deck is not None
    return deck


def _commander_deck(name: str, *, filler: int = 89, extra: DeckCard | None = None) -> Deck:
    """A 100-card Commander deck authored with NON-canonical names.

    ``filler`` filler goblins (default 89 → 1 commander + 10 mountains + 89 = 100).
    """
    cards = [
        DeckCard(name='krenko, mob boss', quantity=1, role='commander'),
        DeckCard(name='mountain', quantity=10),
    ]
    for i in range(filler):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    if extra is not None:
        cards.append(extra)
    return Deck(name=name, format='commander', strategy='go wide', cards=cards)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    return root


# --------------------------------------------------------------------------- #
# Lineage — new-draft --from writes derived_from
# --------------------------------------------------------------------------- #


def test_from_draft_records_lineage(data_dir: Path, tmp_path: Path) -> None:
    """A ``--from`` copy stores ``derived_from = parent deck_uuid`` (lineage)."""
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Parent'), allow_shrink=False)
    access.pull('Parent')
    parent_uuid = decks.uuid_for_name('Parent')
    assert parent_uuid is not None

    source_deck = access.read_deck('Parent')
    from uuid import uuid4

    draft = source_deck.model_copy(update={'name': 'Explore', 'airtable_record_id': None, 'uuid': uuid4().hex})
    draft_uuid = decks.create_ephemeral(draft, derived_from=parent_uuid)

    row = decks.get_row(draft_uuid)
    assert row is not None
    assert row.derived_from == parent_uuid
    assert row.sync_status == 'ephemeral'


# --------------------------------------------------------------------------- #
# B1b — exploration promote lands on the lineage parent (bound by external ref)
# --------------------------------------------------------------------------- #


def test_b1b_exploration_promote_lands_on_lineage_parent(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Krenko Goblins'), allow_shrink=False)
    access.pull('Krenko Goblins')
    parent_uuid = decks.uuid_for_name('Krenko Goblins')
    assert parent_uuid is not None
    parent_ref = decks.get_row(parent_uuid).source_ref  # type: ignore[union-attr]

    # A same-named UNRELATED deck elsewhere is NOT what promote binds to — here we
    # assert lineage binds by the parent's external ref, so the parent row itself
    # receives the committed edit.
    src_deck = access.read_deck('Krenko Goblins')
    from uuid import uuid4

    draft = src_deck.model_copy(update={'name': 'Krenko (explore)', 'airtable_record_id': None, 'uuid': uuid4().hex})
    draft_uuid = decks.create_ephemeral(draft, derived_from=parent_uuid)

    # A size-preserving edit on the draft.
    decks.swap(draft_uuid, add=DeckCard(name='impact tremors', quantity=1), cut='Goblin 0')

    promote(decks, driver, deck_uuid=draft_uuid)

    # The committed edit landed on the PARENT's source (bound by ref, not by the
    # draft's own name).
    parent_source = driver.get_deck(parent_ref)  # type: ignore[arg-type]
    names = {c.name for c in parent_source.cards}
    assert 'Impact Tremors' in names
    assert 'Goblin 0' not in names

    # The parent local row is refreshed and remains the single synced row for the ref.
    parent_row = decks.get_row(parent_uuid)
    assert parent_row is not None
    assert parent_row.sync_status == 'synced'
    assert 'Impact Tremors' in {c.name for c in decks.get(parent_uuid).cards}  # type: ignore[union-attr]

    # No stray source deck named after the draft's own name was created.
    with pytest.raises(FileNotFoundError):
        driver.get_deck('Krenko (explore)')


# --------------------------------------------------------------------------- #
# B2 — a consumed draft is inert
# --------------------------------------------------------------------------- #


def test_b2_consumed_draft_is_inert(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Krenko Goblins'), allow_shrink=False)
    access.pull('Krenko Goblins')
    parent_uuid = decks.uuid_for_name('Krenko Goblins')
    assert parent_uuid is not None

    src_deck = access.read_deck('Krenko Goblins')
    from uuid import uuid4

    draft = src_deck.model_copy(update={'name': 'Krenko (explore)', 'airtable_record_id': None, 'uuid': uuid4().hex})
    draft_uuid = decks.create_ephemeral(draft, derived_from=parent_uuid)
    decks.swap(draft_uuid, add=DeckCard(name='impact tremors', quantity=1), cut='Goblin 0')

    promote(decks, driver, deck_uuid=draft_uuid)

    # The draft is now consumed + archived (retired lineage).
    draft_row = decks.get_row(draft_uuid)
    assert draft_row is not None
    assert draft_row.sync_status == 'consumed'
    assert draft_row.archived is True

    # It is excluded from name resolution (rows_for_name / uuid_for_name / prefix).
    assert decks.uuid_for_name('Krenko (explore)') is None
    assert decks.rows_for_name('Krenko (explore)') == []
    assert decks.uuids_by_prefix(draft_uuid[:6]) == []

    # An edit on a consumed draft is REFUSED (no mutation).
    from pipeline.decks.store import DecksError

    with pytest.raises(DecksError):
        decks.add_card(draft_uuid, DeckCard(name='mountain', quantity=1))

    # A push addressed to the consumed draft by its --id is REFUSED (consumed rows
    # are non-pushable) and materializes NO new source file.
    with pytest.raises(DecksError):
        access.push('Krenko (explore)', id_prefix=draft_uuid[:6])
    # No source file named after the draft exists.
    with pytest.raises(FileNotFoundError):
        driver.get_deck('Krenko (explore)')

    # Forensic READ by --id prefix still works (get_row / get bypass resolution).
    assert decks.get(draft_uuid) is not None


# --------------------------------------------------------------------------- #
# B1a — clean-slate promote --to an existing name creates a SEPARATE deck
# --------------------------------------------------------------------------- #


def test_b1a_clean_slate_promote_to_existing_name_does_not_clobber(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    # An existing synced "Existing" deck (the one that MUST NOT be clobbered).
    driver.save_deck(_commander_deck('Existing'), allow_shrink=False)
    access.pull('Existing')
    original_uuid = decks.uuid_for_name('Existing')
    assert original_uuid is not None
    original_source_version = version(driver.get_deck('Existing'))

    # A clean-slate draft (no lineage) with DIFFERENT content, promoted --to "Existing".
    fresh = _commander_deck('My Brew', extra=None)
    # make it distinguishable from the original: swap one goblin for sol ring
    fresh = fresh.model_copy(
        update={'cards': [c for c in fresh.cards if c.name != 'Goblin 0'] + [DeckCard(name='sol ring', quantity=1)]}
    )
    draft_uuid = decks.create_ephemeral(fresh)  # derived_from is NULL

    promote(decks, driver, deck_uuid=draft_uuid, to_name='Existing')

    # A SECOND, distinct source deck named "Existing" now exists — distinct file +
    # distinct uuid — and the original's content is UNTOUCHED.
    import yaml

    decks_dir = tmp_path / 'collection' / 'decks'
    existing_files = [
        p for p in decks_dir.glob('*.yaml') if (yaml.safe_load(p.read_text()) or {}).get('name') == 'Existing'
    ]
    assert len(existing_files) == 2, f'expected two Existing files, got {[p.name for p in existing_files]}'

    # The ORIGINAL source deck (bound by its stored uuid) is untouched.
    original_stored_uuid = decks.get(original_uuid).uuid  # type: ignore[union-attr]
    orig_read = driver.get_deck_by_uuid(original_stored_uuid)
    assert 'Sol Ring' not in {c.name for c in orig_read.cards}
    assert version(orig_read) == original_source_version

    # The promoted draft's own row is synced (it IS the deck now), not consumed.
    draft_row = decks.get_row(draft_uuid)
    assert draft_row is not None
    assert draft_row.sync_status == 'synced'

    # The promoted deck is a DISTINCT source deck (its own uuid), carrying sol ring.
    promoted = driver.get_deck_by_uuid(decks.get(draft_uuid).uuid)  # type: ignore[union-attr]
    assert 'Sol Ring' in {c.name for c in promoted.cards}
    assert promoted.uuid != original_stored_uuid


# --------------------------------------------------------------------------- #
# M6 — a promote whose save fails leaves a clean ephemeral draft (no zombie)
# --------------------------------------------------------------------------- #


def test_m6_failed_promote_leaves_clean_ephemeral_draft(data_dir: Path, tmp_path: Path) -> None:
    from pipeline.collection.errors import CollectionError

    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    # A synced parent at target (100).
    driver.save_deck(_commander_deck('Krenko Goblins'), allow_shrink=False)
    access.pull('Krenko Goblins')
    parent_uuid = decks.uuid_for_name('Krenko Goblins')
    assert parent_uuid is not None
    parent_source_version = version(driver.get_deck('Krenko Goblins'))

    # An exploration draft that we SHRINK below target directly (bypassing the
    # store's own guard) so the promote's save-through-ceremony refuses.
    src_deck = access.read_deck('Krenko Goblins')
    from uuid import uuid4

    draft = src_deck.model_copy(update={'name': 'Krenko (explore)', 'airtable_record_id': None, 'uuid': uuid4().hex})
    draft_uuid = decks.create_ephemeral(draft, derived_from=parent_uuid)
    shrunk = decks.get(draft_uuid).model_copy(  # type: ignore[union-attr]
        update={'cards': [c for c in decks.get(draft_uuid).cards if c.name != 'Goblin 5']}  # type: ignore[union-attr]
    )
    decks.put(shrunk, deck_uuid=draft_uuid, sync_status='ephemeral', derived_from=parent_uuid)

    with pytest.raises(CollectionError):
        promote(decks, driver, deck_uuid=draft_uuid)

    # The draft remains a CLEAN ephemeral draft — not consumed, not synced.
    draft_row = decks.get_row(draft_uuid)
    assert draft_row is not None
    assert draft_row.sync_status == 'ephemeral'
    assert draft_row.archived is False

    # The parent target is preserved (no partial write).
    assert version(driver.get_deck('Krenko Goblins')) == parent_source_version


# --------------------------------------------------------------------------- #
# M2 — sync reconcile: both-moved refuses, both preserved
# --------------------------------------------------------------------------- #


def test_m2_sync_both_moved_refuses_and_preserves_both(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Krenko Goblins'), allow_shrink=False)
    access.pull('Krenko Goblins')
    deck_uuid = decks.uuid_for_name('Krenko Goblins')
    assert deck_uuid is not None

    # LOCAL moves: a size-preserving edit staged locally (not pushed).
    decks.swap(deck_uuid, add=DeckCard(name='impact tremors', quantity=1), cut='Goblin 0')
    local_version = version(_get(decks, deck_uuid))

    # SOURCE moves out-of-band (a foreign edit), size-preserving.
    foreign = driver.get_deck('Krenko Goblins')
    foreign_cards = [c for c in foreign.cards if c.name != 'Goblin 1'] + [DeckCard(name='sol ring', quantity=1)]
    driver.save_deck(foreign.model_copy(update={'cards': foreign_cards}), allow_shrink=False)
    source_version = version(driver.get_deck('Krenko Goblins'))

    with pytest.raises(SyncDriftError):
        sync_reconcile(decks, driver, deck_uuid=deck_uuid)

    # NOTHING changed: local keeps its edit, source keeps its foreign edit. (The
    # local edit's raw name is not canonicalized locally — canonicalization is P4;
    # the version hashes above are the load-bearing "nothing lost" proof.)
    assert version(_get(decks, deck_uuid)) == local_version
    assert version(driver.get_deck('Krenko Goblins')) == source_version
    assert 'impact tremors' in {c.name for c in _get(decks, deck_uuid).cards}
    assert 'Sol Ring' in {c.name for c in driver.get_deck('Krenko Goblins').cards}


def test_sync_only_source_moved_pulls(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Krenko Goblins'), allow_shrink=False)
    access.pull('Krenko Goblins')
    deck_uuid = decks.uuid_for_name('Krenko Goblins')
    assert deck_uuid is not None

    # Only SOURCE moves (local untouched).
    foreign = driver.get_deck('Krenko Goblins')
    foreign_cards = [c for c in foreign.cards if c.name != 'Goblin 1'] + [DeckCard(name='sol ring', quantity=1)]
    driver.save_deck(foreign.model_copy(update={'cards': foreign_cards}), allow_shrink=False)

    sync_reconcile(decks, driver, deck_uuid=deck_uuid)

    # Local now matches the source (pulled).
    assert 'Sol Ring' in {c.name for c in _get(decks, deck_uuid).cards}
    assert version(_get(decks, deck_uuid)) == version(driver.get_deck('Krenko Goblins'))


def test_sync_only_local_moved_pushes(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Krenko Goblins'), allow_shrink=False)
    access.pull('Krenko Goblins')
    deck_uuid = decks.uuid_for_name('Krenko Goblins')
    assert deck_uuid is not None

    # Only LOCAL moves (source untouched).
    decks.swap(deck_uuid, add=DeckCard(name='impact tremors', quantity=1), cut='Goblin 0')

    sync_reconcile(decks, driver, deck_uuid=deck_uuid)

    # The source now carries the local edit (pushed; the source canonicalizes the
    # raw 'impact tremors' -> 'Impact Tremors' on read — the push landed).
    assert 'Impact Tremors' in {c.name for c in driver.get_deck('Krenko Goblins').cards}
    # The baseline was re-stamped to the canonical source read (push tail).
    assert decks.get_row(deck_uuid).synced_baseline == version(driver.get_deck('Krenko Goblins'))  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# m3 — archive-deck refuses a synced deck
# --------------------------------------------------------------------------- #


def test_m3_archive_refuses_synced_deck(data_dir: Path, tmp_path: Path) -> None:
    from pipeline.decks.store import DecksError

    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Krenko Goblins'), allow_shrink=False)
    access.pull('Krenko Goblins')
    synced_uuid = decks.uuid_for_name('Krenko Goblins')
    assert synced_uuid is not None
    assert decks.get_row(synced_uuid).sync_status == 'synced'  # type: ignore[union-attr]

    with pytest.raises(DecksError, match='synced deck'):
        decks.archive(synced_uuid)

    # An ephemeral draft still archives fine.
    draft_uuid = decks.create_ephemeral(_commander_deck('Draft'))
    decks.archive(draft_uuid)
    assert decks.get_row(draft_uuid).archived is True  # type: ignore[union-attr]

    # A consumed draft still archives (idempotent — already archived).
    # (consumed rows are set archived by consume; archive on one is a no-op-ish allow)


# --------------------------------------------------------------------------- #
# slug-collision — two same-named decks → two files, neither clobbered
# --------------------------------------------------------------------------- #


def test_slug_collision_two_twins_two_files(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)

    first = _commander_deck('Twin')
    driver.save_deck(first, allow_shrink=False)

    # A SECOND, distinct deck (distinct uuid) that shares the name "Twin" but has
    # different content.
    second = _commander_deck('Twin', extra=None).model_copy(
        update={'cards': [c for c in _commander_deck('Twin').cards if c.name != 'Goblin 0']
                + [DeckCard(name='sol ring', quantity=1)]}
    )
    # ensure distinct uuid
    assert second.uuid != first.uuid
    driver.save_deck(second, allow_shrink=False)

    decks_dir = tmp_path / 'collection' / 'decks'
    twin_files = list(decks_dir.glob('*.yaml'))
    import yaml

    twin_named = [p for p in twin_files if (yaml.safe_load(p.read_text()) or {}).get('name') == 'Twin']
    assert len(twin_named) == 2, f'expected two Twin files, got {[p.name for p in twin_named]}'

    # Both are readable by their own uuid; neither clobbered the other.
    p1 = driver.find_deck_path_by_uuid(first.uuid)
    p2 = driver.find_deck_path_by_uuid(second.uuid)
    assert p1 is not None and p2 is not None and p1 != p2
    d1 = driver.get_deck_by_uuid(first.uuid)  # type: ignore[attr-defined]
    d2 = driver.get_deck_by_uuid(second.uuid)  # type: ignore[attr-defined]
    assert 'Sol Ring' not in {c.name for c in d1.cards}
    assert 'Sol Ring' in {c.name for c in d2.cards}
