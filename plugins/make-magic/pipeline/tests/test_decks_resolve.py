"""Deck name resolution + source binding.

Two user-facing surfaces are pinned VERBATIM here:

1. dup-name disambiguation — a copy-paste ``--id`` candidate list;
2. the YAML protective comment + in-file ``uuid``.

Plus dup-name disambiguation, alias reads binding to ONE row, YAML identity
(rename-tolerant + legacy no-uuid), ``--id`` precedence, and the Airtable
recordId bind. Everything is OFFLINE (a real ``LocalYamlStore`` on a tmp dir is
the source of record; Airtable is mocked). Sync/resolution tests use the REAL
canonicalizing resolver — the hazard is never stubbed away.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from _decks_helpers import (
    commander_deck,
    decks_dir,
    save_source,
    source_store,
    write_source_yaml,
)

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksError, DecksStore
from pipeline.decks.access import DeckAccess, deck_access


class CanonicalizingResolver:
    """A REAL canonicalizing resolver (never stubbed to identity)."""

    _CANON: ClassVar[dict[str, str]] = {
        'krenko, mob boss': 'Krenko, Mob Boss',
        'mountain': 'Mountain',
        'sol ring': 'Sol Ring',
    }

    def _canonical(self, name: str) -> str:
        key = ' '.join(name.split()).casefold()
        return self._CANON.get(key, ' '.join(name.split()))

    def get_card(self, name: str) -> Card | None:
        canonical = self._canonical(name)
        return Card(name=canonical, oracle_id=f'oid-{canonical}', mana_cost='{1}{R}')


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    return root


def _source_store(tmp_path: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=tmp_path / 'collection')


def _commander_deck(name: str, *, uuid: str | None = None) -> Deck:
    cards = [DeckCard(name='krenko, mob boss', quantity=1, role='commander'), DeckCard(name='mountain', quantity=10)]
    for i in range(89):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    deck = Deck(name=name, format='commander', strategy='go wide', cards=cards)
    return deck.model_copy(update={'uuid': uuid}) if uuid else deck


# --------------------------------------------------------------------------- #
# dup-name disambiguation (the candidate list, verbatim)
# --------------------------------------------------------------------------- #


def _seed_three_gruul(decks: DecksStore) -> dict[str, str]:
    """Seed three live rows named 'Gruul' (mirrors the spec's example order)."""
    synced = _commander_deck('Gruul', uuid='6a3f0b' + '0' * 26)
    eph1 = _commander_deck('Gruul', uuid='b81c92' + '0' * 26)
    eph2 = _commander_deck('Gruul', uuid='0f2d14' + '0' * 26)
    decks.put(synced, deck_uuid=synced.uuid, sync_status='synced', source_ref='Gruul')
    decks.set_external_id(synced.uuid, 'airtable', 'recGRUUL')  # the synced · airtable candidate
    decks.put(eph1, deck_uuid=eph1.uuid, sync_status='ephemeral')
    decks.put(eph2, deck_uuid=eph2.uuid, sync_status='ephemeral')
    decks.archive(eph2.uuid)  # the archived candidate in the spec's example
    return {'synced': synced.uuid, 'eph1': eph1.uuid, 'eph2': eph2.uuid}


def test_dup_name_refuses_with_verbatim_candidate_list(data_dir: Path, tmp_path: Path) -> None:
    decks = DecksStore()
    _seed_three_gruul(decks)
    access = DeckAccess(_source_store(tmp_path), decks=decks)

    with pytest.raises(DecksError) as excinfo:
        access.resolve('Gruul')

    msg = str(excinfo.value)
    expected = (
        "'Gruul' is ambiguous (3 decks). Re-run with one of:\n"
        '  --id 6a3f0b   # synced · airtable\n'
        '  --id b81c92   # ephemeral · local\n'
        '  --id 0f2d14   # ephemeral,archived · local'
    )
    assert msg == expected


def test_id_prefix_resolves_the_right_dup(data_dir: Path, tmp_path: Path) -> None:
    decks = DecksStore()
    ids = _seed_three_gruul(decks)
    access = DeckAccess(_source_store(tmp_path), decks=decks)

    assert access.resolve(id_prefix='b81c92') == ids['eph1']
    assert access.resolve(id_prefix='6a3f0b') == ids['synced']


def test_id_prefix_zero_and_ambiguous(data_dir: Path, tmp_path: Path) -> None:
    decks = DecksStore()
    decks.put(_commander_deck('A', uuid='aaaa' + '0' * 28), deck_uuid='aaaa' + '0' * 28)
    decks.put(_commander_deck('B', uuid='aaab' + '0' * 28), deck_uuid='aaab' + '0' * 28)
    access = DeckAccess(_source_store(tmp_path), decks=decks)

    with pytest.raises(DecksError):
        access.resolve(id_prefix='zzzz')  # no match
    with pytest.raises(DecksError, match='ambiguous'):
        access.resolve(id_prefix='aaa')  # >1 match


def test_single_name_still_resolves(data_dir: Path, tmp_path: Path) -> None:
    decks = DecksStore()
    d = _commander_deck('Solo', uuid='c0ffee' + '0' * 26)
    decks.put(d, deck_uuid=d.uuid, sync_status='ephemeral')
    access = DeckAccess(_source_store(tmp_path), decks=decks)
    assert access.resolve('Solo') == d.uuid


# --------------------------------------------------------------------------- #
# YAML identity — comment header + in-file uuid, rename-tolerant, legacy-safe
# --------------------------------------------------------------------------- #


def test_save_deck_writes_comment_and_uuid_first(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    deck = _commander_deck('Krenko Goblins', uuid='deadbeef' + '0' * 24)
    driver.save_deck(deck, allow_shrink=False)

    path = tmp_path / 'collection' / 'decks' / 'krenko-goblins.yaml'
    text = path.read_text()
    expected_header = (
        "# This is the deck's permanent ID (used to keep copies in sync).\n"
        '# Please leave it as-is — renaming the FILE is fine, editing this is not.\n'
        f'uuid: {deck.uuid}\n'
    )
    assert text.startswith(expected_header)


def test_get_deck_reads_uuid_back(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    deck = _commander_deck('Krenko Goblins', uuid='feedface' + '0' * 24)
    driver.save_deck(deck, allow_shrink=False)
    read_back = driver.get_deck('Krenko Goblins')
    assert read_back.uuid == deck.uuid


def test_find_deck_path_by_uuid_is_rename_tolerant(data_dir: Path, tmp_path: Path) -> None:
    driver = _source_store(tmp_path)
    deck = _commander_deck('Krenko Goblins', uuid='abc123' + '0' * 26)
    driver.save_deck(deck, allow_shrink=False)

    decks_dir = tmp_path / 'collection' / 'decks'
    (decks_dir / 'krenko-goblins.yaml').rename(decks_dir / 'renamed-by-user.yaml')

    found = driver.find_deck_path_by_uuid(deck.uuid)
    assert found is not None
    assert found.name == 'renamed-by-user.yaml'
    assert driver.find_deck_path_by_uuid('no-such-uuid') is None


def test_legacy_file_without_uuid_gets_one_on_next_save(data_dir: Path, tmp_path: Path) -> None:
    import yaml

    decks_dir = tmp_path / 'collection' / 'decks'
    decks_dir.mkdir(parents=True)
    legacy = decks_dir / 'legacy.yaml'
    legacy.write_text(yaml.safe_dump({'name': 'Legacy', 'cards': [{'card': 'Mountain', 'qty': 100}]}))

    driver = _source_store(tmp_path)
    read = driver.get_deck('Legacy')  # must NOT crash — a uuid is assigned
    assert read.uuid  # non-empty
    # Next save persists the (now-stable) uuid into the file.
    driver.save_deck(read, allow_shrink=True)
    assert driver.get_deck('Legacy').uuid == read.uuid
    assert 'uuid:' in legacy.read_text()


# --------------------------------------------------------------------------- #
# a synced deck read via two name aliases binds to ONE local row
# --------------------------------------------------------------------------- #


def test_alias_reads_bind_to_one_local_row(data_dir: Path, tmp_path: Path) -> None:
    """Two case-variant names for the SAME synced source deck -> ONE local row.

    The source's in-file uuid is authoritative; a read via either alias binds by
    that ref, so no dual-row staleness can arise.
    """
    driver = _source_store(tmp_path)
    deck = _commander_deck('Alias Deck', uuid='a11a5' + '0' * 27)
    driver.save_deck(deck, allow_shrink=False)
    # The file is slugged 'alias-deck.yaml'; the local YAML adapter is
    # case-insensitive on slug, so both aliases resolve to the SAME file.
    decks = DecksStore()
    access = DeckAccess(driver, decks=decks)

    first = access.read_deck('Alias Deck')
    second = access.read_deck('alias deck')
    assert first.uuid == second.uuid == deck.uuid

    # Exactly ONE non-archived local row exists after both alias reads.
    rows = decks.list_rows()
    assert len([r for r in rows if r.deck_uuid == deck.uuid]) == 1
    assert len(rows) == 1
    # The row is bound to the source by the in-file uuid (external ref).
    row = decks.get_row(deck.uuid)
    assert row is not None
    import json as _json

    ext = _json.loads(row.external_ids or '{}')
    assert ext.get('local') == deck.uuid


def test_alias_rename_of_deck_name_still_binds_one_row(data_dir: Path, tmp_path: Path) -> None:
    """Renaming the source deck's NAME (same in-file uuid) reuses the same row."""
    driver = _source_store(tmp_path)
    deck = _commander_deck('Orig Name', uuid='0d1d0d' + '0' * 26)
    driver.save_deck(deck, allow_shrink=False)
    decks = DecksStore()
    access = DeckAccess(driver, decks=decks)
    access.read_deck('Orig Name')

    # User renames the deck (name change, same uuid) — rewrite the file by hand.
    import yaml

    path = tmp_path / 'collection' / 'decks' / 'orig-name.yaml'
    data = yaml.safe_load('\n'.join(ln for ln in path.read_text().splitlines() if not ln.startswith('#')))
    data['name'] = 'New Name'
    new_path = tmp_path / 'collection' / 'decks' / 'new-name.yaml'
    header = (
        "# This is the deck's permanent ID (used to keep copies in sync).\n"
        '# Please leave it as-is — renaming the FILE is fine, editing this is not.\n'
    )
    new_path.write_text(header + yaml.safe_dump(data, sort_keys=False))
    path.unlink()

    access.read_deck('New Name')
    rows = decks.list_rows(include_archived=True)
    assert len([r for r in rows if r.deck_uuid == deck.uuid]) == 1
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Airtable recordId bind — pull populates external_ids['airtable'] (MOCKED)
# --------------------------------------------------------------------------- #


class _MockAirtableDriver:
    """A minimal CollectionStore stand-in whose deck read carries a recordId.

    ZERO prod writes; never delete. ``get_deck`` returns a Deck stamped with an
    ``airtable_record_id`` (the external ref resolution binds on). A rename-safe
    re-read is served by recordId when the CRUD supports GET-by-id (mocked here).
    """

    backend_name = 'airtable'

    def __init__(self) -> None:
        self._by_name: dict[str, Deck] = {}
        self._by_id: dict[str, Deck] = {}

    def add(self, deck: Deck) -> None:
        self._by_name[deck.name] = deck
        if deck.airtable_record_id:
            self._by_id[deck.airtable_record_id] = deck

    def get_deck(self, name: str) -> Deck:
        if name in self._by_name:
            return self._by_name[name]
        raise FileNotFoundError(name)

    def get_deck_by_record_id(self, record_id: str) -> Deck:
        return self._by_id[record_id]

    def save_deck(self, deck: Deck, *, allow_shrink: bool = False) -> None:  # pragma: no cover - unused here
        raise AssertionError('no prod writes in this test')


def _airtable_deck(name: str, record_id: str) -> Deck:
    cards = [DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'), DeckCard(name='Mountain', quantity=99)]
    return Deck(name=name, format='commander', cards=cards, airtable_record_id=record_id)


def test_pull_populates_airtable_external_id(data_dir: Path) -> None:
    driver = _MockAirtableDriver()
    driver.add(_airtable_deck('Airtable Deck', 'rec123ABC'))
    decks = DecksStore()
    access = DeckAccess(driver, decks=decks)  # type: ignore[arg-type]

    deck = access.read_deck('Airtable Deck')
    row = decks.get_row(deck.uuid)
    assert row is not None
    import json as _json

    assert _json.loads(row.external_ids or '{}').get('airtable') == 'rec123ABC'


def test_airtable_rename_safe_reread_binds_one_row(data_dir: Path) -> None:
    """A synced airtable deck renamed on the source still binds to ONE row by recordId."""
    driver = _MockAirtableDriver()
    driver.add(_airtable_deck('Old Airtable Name', 'recSTABLE1'))
    decks = DecksStore()
    access = DeckAccess(driver, decks=decks)  # type: ignore[arg-type]
    first = access.read_deck('Old Airtable Name')

    # Airtable renames the deck (same recordId). The old name no longer exists;
    # the new name is served by the same recordId.
    renamed = _airtable_deck('New Airtable Name', 'recSTABLE1')
    driver2 = _MockAirtableDriver()
    driver2.add(renamed)
    access2 = DeckAccess(driver2, decks=decks)  # type: ignore[arg-type]
    second = access2.read_deck('New Airtable Name')

    assert first.uuid == second.uuid
    rows = decks.list_rows(include_archived=True)
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# dup-name promote refuses with candidates + --id disambiguates.
# --------------------------------------------------------------------------- #


def test_dup_name_promote_refuses_with_candidates(cli, data_dir: Path) -> None:
    """Two same-named drafts: promote by name refuses with a candidate list (no silent oldest)."""
    cli('new-draft', 'Scratch', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('new-draft', 'Scratch', '--commander', 'Zada, Hedron Grinder', '--format', 'Commander')

    code, _out, err = cli('promote-deck', 'Scratch', '--to', 'Landed')
    assert code != 0
    assert 'ambiguous' in err.lower()
    assert '--id' in err


def test_promote_id_flag_disambiguates(cli, data_dir: Path) -> None:
    """--id lets the user promote a specific dup-named draft."""
    cli('new-draft', 'Scratch', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('new-draft', 'Scratch', '--commander', 'Zada, Hedron Grinder', '--format', 'Commander')

    # Find the Zada draft's uuid (identify by its commander content, not row order).
    decks = DecksStore()
    zada = None
    for r in decks.list_rows():
        if r.name != 'Scratch':
            continue
        deck = decks.get(r.deck_uuid)
        if deck is not None and any(c.name == 'Zada, Hedron Grinder' for c in deck.cards):
            zada = r
    assert zada is not None
    code, _out, err = cli('promote-deck', 'Scratch', '--id', zada.deck_uuid[:6], '--to', 'Landed')
    assert code == 0, err
    # The promoted deck carries Zada (the selected draft).
    landed = decks_dir(data_dir) / 'landed.yaml'
    assert 'Zada' in landed.read_text()


# --------------------------------------------------------------------------- #
# alias-addressed WRITE lands on the bound row, exit 0.
# --------------------------------------------------------------------------- #


def test_alias_write_lands_on_bound_row(cli, data_dir: Path) -> None:
    """A case-alias WRITE (deck-add / set-strategy) lands on the ONE bound row, exit 0."""
    write_source_yaml(
        data_dir,
        'alias-deck',
        'Alias Deck',
        [('Krenko, Mob Boss', 'commander')] + [(f'ACard {i}', None) for i in range(99)],
        uuid='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    )
    cli('get-deck', 'Alias Deck')  # bind the canonical row

    code, _out, err = cli('deck-add', 'alias deck', 'Sol Ring')
    assert code == 0, err
    assert 'no deck with id' not in err
    src = (decks_dir(data_dir) / 'alias-deck.yaml').read_text()
    assert 'Sol Ring' in src, 'aliased deck-add did not land on the bound row'

    code, _out, err = cli('set-strategy', 'alias deck', 'aggro')
    assert code == 0, err
    assert 'strategy: aggro' in (decks_dir(data_dir) / 'alias-deck.yaml').read_text()


def test_alias_write_binds_to_read_uuid(data_dir: Path) -> None:
    """Direct DeckAccess: an alias write binds to the same uuid read_deck returns."""
    driver = source_store(data_dir)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)
    driver.save_deck(commander_deck('Alias Deck'), allow_shrink=False)

    bound = access.read_deck('Alias Deck')
    canonical_uuid = access.resolve('Alias Deck')
    # A cased alias write must target `canonical_uuid`, not mint a fresh hex.
    access.set_strategy('alias deck', 'aggro plan')
    assert bound is not None
    assert decks.get(canonical_uuid).strategy == 'aggro plan'  # type: ignore[union-attr]


def test_case_alias_write_under_dup_names_refuses_with_candidates(cli, data_dir: Path) -> None:
    """With two live rows named Precious, a cased-alias write must refuse (candidate
    list), NOT silently pick the base-slug deck."""
    save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    cli('get-deck', 'Precious')
    cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('promote-deck', 'JunkB', '--to', 'Precious')

    code, _out, err = cli('deck-add', 'Precious', 'Probe A')  # exact name refuses (control)
    assert code != 0 and '--id' in err

    code, _out, err = cli('set-strategy', 'pReCiOuS', 'alias strategy')  # cased alias
    assert code != 0, 'a cased-alias write under dup names must refuse'
    assert '--id' in err
    # No file received the sneaky strategy.
    for p in decks_dir(data_dir).glob('*.yaml'):
        assert 'alias strategy' not in p.read_text()


def test_id_addressed_edit_commits_by_uuid_under_dup_names(cli, data_dir: Path) -> None:
    """deck-add --id <prefix> under dup names commits by the resolved uuid, exit 0.

    The ``deck-*`` family commits through run.py's ``_commit_deck_edit``, which
    commits by the resolved uuid for ``--id`` calls (keeping the name wall for
    name-addressed ones), so an ``--id`` edit under dup names does not re-hit the
    name-ambiguity refusal at commit.
    """
    save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    d = DecksStore()
    rows = sorted(
        (r for r in d.list_rows() if r.name == 'Precious' and r.sync_status != 'consumed'),
        key=lambda r: r.deck_uuid,
    )
    assert len(rows) == 2
    # deck-add addressed by --id must NOT re-hit the name-ambiguity refusal at commit.
    target = rows[0].deck_uuid
    from _decks_helpers import expire

    expire('Precious')
    code, _out, err = cli('deck-add', '--id', target[:8], 'Bolt Card')
    assert code == 0, f'--id deck-add under dup names must commit by uuid: {err}'

    # A NAME-addressed deck-add under dup names STILL hits the dup-name wall (kept).
    expire('Precious')
    code, _out, err = cli('deck-add', 'Precious', 'Wall Card')
    assert code == 1
    assert 'is ambiguous' in err


# --------------------------------------------------------------------------- #
# error hygiene + new-draft source-name collision (--force).
# --------------------------------------------------------------------------- #


def test_unknown_name_clean_error(cli, data_dir: Path) -> None:
    """A name-miss at a verb requiring an existing deck is a clean error, no minted uuid/path."""
    code, _out, err = cli('archive-deck', 'No Such Deck')
    assert code != 0
    assert 'no deck' in err.lower()
    # No leaked minted uuid (32 hex) and no filesystem path.
    import re

    assert not re.search(r'\b[0-9a-f]{32}\b', err), f'leaked a minted uuid: {err}'
    assert '/collection/decks/' not in err


def test_new_draft_refuses_source_name_collision(cli, data_dir: Path) -> None:
    """new-draft named after an existing SOURCE deck is refused without --force."""
    write_source_yaml(
        data_dir,
        'gruul',
        'Gruul',
        [('Zada, Hedron Grinder', 'commander')] + [(f'GCard {i}', None) for i in range(99)],
        uuid='cccccccccccccccccccccccccccccccc',
    )
    code, _out, err = cli('new-draft', 'Gruul', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    assert code != 0
    assert '--force' in err or '--from' in err


def test_new_draft_force_allows_same_named_draft(cli, data_dir: Path) -> None:
    """--force lets a same-named draft be created deliberately."""
    write_source_yaml(
        data_dir,
        'gruul',
        'Gruul',
        [('Zada, Hedron Grinder', 'commander')] + [(f'GCard {i}', None) for i in range(99)],
        uuid='dddddddddddddddddddddddddddddddd',
    )
    code, _out, err = cli(
        'new-draft', 'Gruul', '--force', '--commander', 'Grumgully, the Generous', '--format', 'Commander'
    )
    assert code == 0, err


def test_case_alias_savedeck_under_dup_names_refused(cli, data_dir: Path) -> None:
    """save-deck 'PRECIOUS' with two 'Precious' rows → refused; precious.yaml intact."""
    import json

    save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    precious = decks_dir(data_dir) / 'precious.yaml'
    before = precious.read_text()

    payload = {
        'name': 'PRECIOUS',  # a CASE alias of the two 'Precious' rows
        'format': 'Commander',
        'cards': [{'name': 'Alias Cmdr', 'role': 'commander'}] + [{'name': f'A{i}'} for i in range(99)],
    }
    p = data_dir / 'alias.json'
    p.write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(p))
    assert code == 1, err
    assert 'is ambiguous' in err
    # The base-slug file was NOT overwritten with the alias content.
    assert precious.read_text() == before
    assert 'Alias Cmdr' not in precious.read_text()
