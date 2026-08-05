"""Lifecycle regressions: promote / consume / sync-reconcile.

These protect the source of record from clobbering. Everything is OFFLINE: a real
``LocalYamlStore`` on a tmp dir is the source of record; a REAL canonicalizing
resolver (never stubbed to identity) hydrates the source cards, so the
canonicalization hazard is exercised, not mocked away.

- clean-slate ``promote --to "Existing"`` when a synced "Existing" already exists →
  a SECOND, distinct source deck (distinct uuid + distinct file); the original
  "Existing" content is UNTOUCHED (never bind-by-name).
- exploration ``new-draft --from "Deck" → edit → promote`` lands on the lineage
  PARENT (bound by external ref, not name); a same-named unrelated deck elsewhere
  is untouched.
- after promoting an exploration draft the draft is ``consumed`` + archived:
  excluded from name resolution, refuses edits+push, and materializes NO new
  source file.
- local edit + out-of-band source change → ``sync`` raises SyncDriftError, both
  sides preserved (nothing lost). Never pull-clobber a local edit.
- a promote whose save fails the shrink ceremony leaves a CLEAN ephemeral draft
  (not a half-synced zombie); the target is preserved.
- ``archive-deck`` on a synced deck is refused; on an ephemeral draft works.
- two decks named "Twin" saved → two files, neither clobbered; both readable by
  their own uuid.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from _decks_helpers import commander_deck as _round_commander_deck
from _decks_helpers import decks_dir, source_store, write_legacy_yaml, write_source_yaml
from _decks_helpers import save_source as _save_source

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
# exploration promote lands on the lineage parent (bound by external ref)
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
# a consumed draft is inert
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
# clean-slate promote --to an existing name creates a SEPARATE deck
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
# a promote whose save fails leaves a clean ephemeral draft (no zombie)
# --------------------------------------------------------------------------- #


def test_failed_promote_leaves_clean_ephemeral_draft(data_dir: Path, tmp_path: Path) -> None:
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
# sync reconcile: both-moved refuses, both preserved
# --------------------------------------------------------------------------- #


def test_sync_both_moved_refuses_and_preserves_both(data_dir: Path, tmp_path: Path) -> None:
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
    # local edit's raw name is not canonicalized locally; the version hashes above
    # are the load-bearing "nothing lost" proof.)
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
# archive-deck refuses a synced deck
# --------------------------------------------------------------------------- #


def test_archive_refuses_synced_deck(data_dir: Path, tmp_path: Path) -> None:
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
        update={
            'cards': [c for c in _commander_deck('Twin').cards if c.name != 'Goblin 0']
            + [DeckCard(name='sol ring', quantity=1)]
        }
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


# --------------------------------------------------------------------------- #
# Exploration promote lands on the parent FILE when the PK diverges from the
# file-uuid (the migrated steady state) — no fork.
# --------------------------------------------------------------------------- #


def test_exploration_promote_lands_on_parent_when_pk_differs(data_dir: Path) -> None:
    """Exploration promote stamps the PARENT's in-file uuid, not the local row PK.

    Simulate the migrated steady state: the local row PK differs from the parent
    file's in-file uuid. Promote must land the edit on the parent FILE (no second
    ``<slug>-<hex>.yaml`` fork).
    """
    driver = source_store(data_dir)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_round_commander_deck('Synced', prefix='Goblin'), allow_shrink=False)
    parent = access.read_deck('Synced')
    parent_uuid = access.resolve('Synced')
    file_uuid = parent.uuid  # the source file's in-file uuid

    # Force the local PK to DIVERGE from the file uuid (the migrated steady state):
    # re-key the row under a fresh minted PK while leaving external_ids intact.
    forked_pk = uuid4().hex
    row = decks.get_row(parent_uuid)
    assert row is not None
    decks.put(
        parent.model_copy(update={'uuid': forked_pk}),
        deck_uuid=forked_pk,
        sync_status='synced',
        source_ref=row.source_ref,
        synced_baseline=row.synced_baseline,
    )
    decks.set_external_id(forked_pk, 'local', file_uuid)

    # Draft off the (now PK-divergent) parent, edit, promote.
    src = access.read_deck('Synced', id_prefix=forked_pk[:8])
    draft = src.model_copy(update={'name': 'Synced (explore)', 'airtable_record_id': None, 'uuid': uuid4().hex})
    draft_uuid = decks.create_ephemeral(draft, derived_from=forked_pk)
    decks.swap(draft_uuid, add=DeckCard(name='impact tremors', quantity=1), cut='Goblin 0')

    files_before = sorted(p.name for p in decks_dir(data_dir).glob('*.yaml'))
    promote(decks, driver, deck_uuid=draft_uuid)
    files_after = sorted(p.name for p in decks_dir(data_dir).glob('*.yaml'))

    assert files_after == files_before, f'promote FORKED the source deck: {files_after}'
    # The parent FILE received the edit.
    parent_source = driver.get_deck('Synced')
    names = {c.name for c in parent_source.cards}
    assert 'Impact Tremors' in names
    assert 'Goblin 0' not in names


# --------------------------------------------------------------------------- #
# clean-slate promote --to a never-pulled name stays addressable; edits land on IT.
# --------------------------------------------------------------------------- #


def test_promote_to_never_pulled_name_stays_addressable(cli, data_dir: Path) -> None:
    """clean-slate promote --to the name of a never-pulled existing deck: the promoted
    row is addressable and subsequent edits land on IT, not the unrelated pre-existing
    deck (no name-addressed force-pull rebind)."""
    import json

    original = write_source_yaml(
        data_dir,
        'precious',
        'Precious',
        [('Krenko, Mob Boss', 'commander')] + [(f'PCard {i}', None) for i in range(99)],
        uuid='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    )
    cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    for i in range(5):
        cli('deck-add', 'JunkB', f'JunkcardB {i}')

    code, _out, _err = cli('promote-deck', 'JunkB', '--to', 'Precious')
    assert code == 0

    # The promoted deck is addressable by name: get-deck 'Precious' returns the
    # PROMOTED draft (Grumgully), not the unrelated pre-existing Krenko deck.
    code, out, _err = cli('get-deck', 'Precious')
    assert code == 0
    got = json.loads(out)
    commanders = [c['name'] for c in got['cards'] if c.get('role') == 'commander']
    assert commanders == ['Grumgully, the Generous'], f'get-deck rebound to the wrong deck: {commanders}'

    # An edit addressed to "Precious" must land on the PROMOTED deck, NOT the
    # unrelated 100-card pre-existing deck (no name-addressed rebind, exit 0).
    code, _out, err = cli('deck-add', 'Precious', 'Sol Ring')
    assert code == 0, err
    assert 'Sol Ring' not in original.read_text(), 'edit redirected onto the unrelated pre-existing deck'
    promoted_files = list(decks_dir(data_dir).glob('precious-*.yaml'))
    assert promoted_files and 'Sol Ring' in promoted_files[0].read_text()


# --------------------------------------------------------------------------- #
# promote refuses a synced deck; the source deck is NOT renamed.
# --------------------------------------------------------------------------- #


def test_promote_refuses_synced_deck(cli, data_dir: Path) -> None:
    """Promoting a synced deck's name is refused; the source deck is NOT renamed."""
    src = write_source_yaml(
        data_dir,
        'gruul',
        'Gruul',
        [('Zada, Hedron Grinder', 'commander')] + [(f'GCard {i}', None) for i in range(99)],
        uuid='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    )
    cli('get-deck', 'Gruul')  # pull it into the local store as a synced row

    code, _out, _err = cli('promote-deck', 'Gruul', '--to', 'GruulCopy')
    assert code != 0
    assert 'name: Gruul' in src.read_text(), 'source deck was renamed in place'
    assert 'GruulCopy' not in src.read_text()


# --------------------------------------------------------------------------- #
# legacy clean-slate promote does not clobber + the defensive shrink guard.
# --------------------------------------------------------------------------- #


def test_legacy_clean_slate_promote_does_not_clobber(cli, data_dir: Path) -> None:
    """clean-slate promote --to a (formerly-legacy) deck must NOT silently replace it.

    After the backfill the target carries a uuid, so the slug-collision guard
    disambiguates; even absent that, the shrink ceremony now compares against the
    file's current contents regardless of uuid match — a gutting write is refused.
    """
    legacy = write_source_yaml(
        data_dir,
        'legacy-precious',
        'Legacy Precious',
        [('Krenko, Mob Boss', 'commander')] + [(f'LCard {i}', None) for i in range(99)],
    )
    cli('new-draft', 'JunkA', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    for i in range(5):
        cli('deck-add', 'JunkA', f'JunkcardA {i}')

    cli('promote-deck', 'JunkA', '--to', 'Legacy Precious')

    # The original 100-card deck is NOT clobbered by the 6-card junk draft.
    assert 'Krenko' in legacy.read_text(), 'legacy deck content was replaced (B1-class clobber)'


def test_legacy_save_defensive_shrink_guard(data_dir: Path) -> None:
    """A gutting write onto a legacy (no-uuid) file is refused by the shrink ceremony.

    The prior-size comparison runs against the file's CURRENT contents regardless of
    uuid match, so a no-uuid file is never a free upgrade target for a different deck.
    """
    from pipeline.collection.store import CollectionError

    driver = source_store(data_dir)
    # Hand-author a legacy (no-uuid) 100-card file.
    d = decks_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    lines = ['name: Legacy', 'format: Commander', 'cards:', '- card: "Krenko, Mob Boss"\n  role: commander']
    lines += [f'- card: "L {i}"' for i in range(99)]
    (d / 'legacy.yaml').write_text('\n'.join(lines) + '\n')

    # A DIFFERENT deck (fresh uuid) that would gut the legacy file if it upgraded it.
    junk = Deck(name='Legacy', format='Commander', cards=[DeckCard(name='Sol Ring')], uuid=uuid4().hex)
    with pytest.raises(CollectionError):
        driver.save_deck(junk, allow_shrink=False)
    assert 'Krenko' in (d / 'legacy.yaml').read_text()


# --------------------------------------------------------------------------- #
# a dropped-in backup YAML must not rebind a LIVE row; a genuinely-unbound
# legacy file DOES bind on first pull.
# --------------------------------------------------------------------------- #


def _synced_rows(data_dir: Path) -> list[tuple[str, str, str | None]]:
    return [(r.deck_uuid, r.name, r.external_ids) for r in DecksStore().list_rows() if r.sync_status == 'synced']


def _file_uuid(path: Path) -> str | None:
    for line in path.read_text().splitlines():
        if line.startswith('uuid: '):
            return line.split('uuid: ', 1)[1].strip() or None
    return None


def test_backup_yaml_does_not_rebind_a_live_row(cli, data_dir: Path) -> None:
    """Dropping a same-named legacy backup into decks/ must NOT hijack the live row:
    its external_ids['local'] stays on the real file; reads/writes stay on it."""
    import json

    _save_source(cli, data_dir, 'Treasure', filler=99, prefix='TCard')
    cli('get-deck', 'Treasure')  # row bound to treasure.yaml's uuid
    before = json.loads(_synced_rows(data_dir)[0][2] or '{}')['local']

    # Drop an OLD legacy backup of the deck into the dir.
    write_legacy_yaml(data_dir, 'z-old-backup-of-treasure', 'Treasure', [f'StaleCard {i}' for i in range(20)])
    cli('list-decks')  # any verb triggers the backfill

    after = json.loads(_synced_rows(data_dir)[0][2] or '{}')['local']
    assert after == before, 'the live binding must not move to the backup file'

    # The row still serves the live 100-card deck, and an edit lands in the real file.
    cli('pull', 'Treasure')
    code, out, _err = cli('get-deck', 'Treasure')
    assert code == 0
    assert sum(c.get('quantity', 1) for c in json.loads(out)['cards']) == 100
    cli('deck-add', 'Treasure', 'New Idea')
    real = (decks_dir(data_dir) / 'treasure.yaml').read_text()
    backup = (decks_dir(data_dir) / 'z-old-backup-of-treasure.yaml').read_text()
    assert 'New Idea' in real
    assert 'New Idea' not in backup


def test_backfill_binds_genuinely_unbound_legacy_files(cli, data_dir: Path) -> None:
    """A first-pull of a never-bound legacy file DOES bind (the fallback still works)."""
    import json

    write_legacy_yaml(data_dir, 'legacy-fresh', 'Legacy Fresh', [f'LCard {i}' for i in range(30)])
    code, _out, _err = cli('get-deck', 'Legacy Fresh')
    assert code == 0
    rows = _synced_rows(data_dir)
    bound = next(json.loads(e or '{}').get('local') for u, n, e in rows if n == 'Legacy Fresh')
    assert bound == _file_uuid(decks_dir(data_dir) / 'legacy-fresh.yaml')


# --------------------------------------------------------------------------- #
# dup-name save-deck refused BEFORE any put; pull under dup names refuses.
# --------------------------------------------------------------------------- #


def test_dupname_savedeck_refused_content_not_staged(cli, data_dir: Path) -> None:
    """A refused dup-name save-deck stages NOTHING; a later sync of the oldest row is clean."""
    import json

    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    # A second, dup-named 'Precious' via a clean-slate promote.
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    d = DecksStore()
    rows = sorted(
        (r for r in d.list_rows() if r.name == 'Precious' and r.sync_status != 'consumed'),
        key=lambda r: r.deck_uuid,
    )
    assert len(rows) == 2
    oldest = rows[0].deck_uuid

    payload = {
        'name': 'Precious',
        'format': 'Commander',
        'cards': (
            [{'name': 'Krenko, Mob Boss', 'role': 'commander'}]
            + [{'name': f'OrigCard {i}'} for i in range(99)]
            + [{'name': 'Sneaky Overwrite'}]
        ),
    }
    p = data_dir / 'dup.json'
    p.write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(p))
    assert code == 1
    assert 'is ambiguous' in err

    # The refused content was NOT staged into ANY row's deck_json.
    for row in rows:
        staged = DecksStore().get(row.deck_uuid)
        assert staged is not None
        assert not any(c.name == 'Sneaky Overwrite' for c in staged.cards)

    # A routine later sync of the oldest row cannot land the refused content.
    assert cli('sync', '--id', oldest[:8])[0] == 0
    assert 'Sneaky Overwrite' not in (decks_dir(data_dir) / 'precious.yaml').read_text()


def test_pull_under_dup_names_refuses(cli, data_dir: Path) -> None:
    """``pull`` under dup names refuses with the candidate list."""
    from _decks_helpers import expire

    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    expire('Precious')
    code, _out, err = cli('pull', 'Precious')
    assert code == 1
    assert 'is ambiguous' in err
    assert '--id' in err


# --------------------------------------------------------------------------- #
# in-place legacy restore: the re-keyed backfill marker heals the row and the
# subsequent write does NOT destroy the restored file.
# --------------------------------------------------------------------------- #


def test_inplace_legacy_restore_marker_rekeyed_and_write_does_not_destroy(cli, data_dir: Path) -> None:
    """An in-place overwrite (no dir-mtime bump) invalidates the re-keyed marker.

    A restore overwrites ``decks/vault.yaml`` content with a legacy (no-uuid) backup.
    An in-place write does not bump the DIR mtime, so a DIR-mtime-keyed marker would
    stay "clean" and the backfill would never heal the row. The re-keyed marker (max
    file mtime + count) bumps on the in-place overwrite, so the backfill mints a uuid
    + rebinds the row on the next read, and the subsequent write lands on the RESTORED
    file without gutting it.
    """
    import json

    _save_source(cli, data_dir, 'Vault', filler=99, prefix='VCard')
    assert cli('get-deck', 'Vault')[0] == 0
    assert cli('list-decks')[0] == 0  # a clean backfill pass drops the marker
    marker = decks_dir(data_dir) / '.uuid-backfill-clean'
    assert marker.exists()

    vault = decks_dir(data_dir) / 'vault.yaml'
    dir_mtime_before = decks_dir(data_dir).stat().st_mtime_ns
    legacy = 'name: Vault\ncards:\n- card: Old Vault Card\n  role: commander\n- card: VCard 1\n'
    with open(vault, 'w') as fh:  # cp semantics — truncate + write IN PLACE
        fh.write(legacy)
    dir_mtime_after = decks_dir(data_dir).stat().st_mtime_ns

    # The in-place overwrite does NOT bump the DIR mtime (that was the hole)...
    assert dir_mtime_before == dir_mtime_after
    # ...but the re-keyed marker (per-file max mtime + count) IS now invalid.
    assert marker.read_text().strip() != LocalYamlStore._decks_dir_files_key(decks_dir(data_dir))

    from _decks_helpers import expire

    expire('Vault')
    # The backfill heals the restored legacy file on read (mints a uuid, rebinds).
    code, out, err = cli('get-deck', 'Vault')
    assert code == 0, err
    served = json.loads(out)
    assert any(c['name'] == 'Old Vault Card' for c in served['cards'])
    assert 'uuid:' in vault.read_text()  # the backfill minted one into the file

    # The subsequent write lands on the RESTORED file and does NOT gut it to the
    # 100-card local cache. The restored content survives.
    expire('Vault')
    assert cli('set-strategy', 'Vault', 'overwrite probe')[0] == 0
    text = vault.read_text()
    assert 'Old Vault Card' in text
    assert text.count('- card:') == 2  # restored backup had 2 cards, not the 100-card cache
