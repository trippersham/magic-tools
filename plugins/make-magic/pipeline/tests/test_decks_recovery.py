"""``save-deck`` fresh-identity recovery — safe by construction.

Recovery of a SYNCED deck whose bound source is gone / re-identified is a normal
fresh-identity ``save-deck``: it creates a BRAND-NEW source at a fresh identity
(strips the stale ``airtable_record_id`` so Airtable takes ``create_record``, treats
the in-file uuid as absent so YAML mints a new file), binds the row to it, and
recovers — never adopting a same-named stranger and never 422-ing. The hatch
itself is safe: it writes FORCED-FRESH (never adopts a legacy base-slug in place),
and the ref-wipe is ATOMIC with the create (a failed create leaves the row
bound-and-dead so the honest refusal is restored and a retry cannot adopt a
stranger).

Covers the executable-advice surface too: ``save-deck "X"`` (no ``--from-json``)
recovers from the local copy; ``save-deck "X" --id <prefix>`` recovers a dup-named
dead row and composes end-to-end; ``list-decks`` flags a dead-bound deck; ``get-deck
"X" --local`` serves the local copy under a dead binding; a recovery preserves the
archived flag.

Everything OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of record
and a REAL canonicalizing resolver hydrates cards (the hazard is exercised, never
stubbed). Airtable findings run against a CONTRACT DOUBLE mirroring the current
adapter (create stamps recordId; update of a deleted record raises 422). ZERO prod
writes; no network; no ``delete_record``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from _decks_helpers import (
    CanonicalizingResolver,
    MockAirtableDecks,
    commander_deck,
    decks_dir,
    expire,
    legacy_restore,
    save_json,
    save_source,
    source_store,
    yaml_files,
)

from pipeline.contracts import Deck, DeckCard
from pipeline.decks import DecksStore
from pipeline.decks.access import DeckAccess
from pipeline.decks.sync import binding_is_dead


def _rows(name: str) -> list:
    d = DecksStore()
    return [r for r in d.list_rows(include_archived=True) if r.name == name and r.sync_status != 'consumed']


def _ext(name: str) -> str | None:
    rows = _rows(name)
    return rows[0].external_ids if rows else None


def _binding_is_dead_now(name: str, uuid: str) -> bool:
    from pipeline.collection.store import get_store

    driver = get_store()
    return binding_is_dead(DecksStore(), driver, deck_uuid=uuid, source_ref=name)


def _set_archived(deck_uuid: str, archived: bool) -> None:
    """Set a row's ``archived`` flag directly (the archived-and-synced state is
    reachable but not through the drafts-only ``archive`` API)."""
    d = DecksStore()
    with d._connect() as conn:  # test fixture reaches the same db.
        conn.execute('UPDATE decks SET archived = ? WHERE deck_uuid = ?', [archived, deck_uuid])


# --------------------------------------------------------------------------- #
# Deleted source recovers via a fresh-identity save (edit verbs still refuse).
# --------------------------------------------------------------------------- #


def test_deleted_source_recovers_via_fresh_save(cli, data_dir):
    """A deleted source recovers via a fresh-identity save.

    Recovery is a normal ``save-deck`` that creates a brand-new source at a fresh
    identity — there is no explicit ``--recreate`` flag. The edit verbs still REFUSE
    the dead binding; ``save-deck`` is the one recovery path.
    """
    save_source(cli, data_dir, 'Recr', filler=99, prefix='RCard')
    assert cli('get-deck', 'Recr')[0] == 0
    (decks_dir(data_dir) / 'recr.yaml').unlink()
    os.utime(decks_dir(data_dir))

    # An edit verb still refuses (no --recreate escape hatch).
    expire('Recr')
    assert cli('sync', 'Recr')[0] == 1
    assert yaml_files(data_dir) == []

    # save-deck recovers: it writes a FRESH file and rebinds the row.
    expire('Recr')
    payload = commander_deck('Recr', filler=99, prefix='RCard').model_dump(mode='json')
    p = data_dir / 'recover.json'
    p.write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(p))
    assert code == 0, err
    assert yaml_files(data_dir) == ['recr.yaml']
    expire('Recr')
    code, out, _err = cli('get-deck', 'Recr')
    assert code == 0 and len(json.loads(out)['cards']) == 100


def test_savedeck_local_deleted_file_creates_fresh(cli, data_dir):
    """save-deck on a deleted local bound file recreates a fresh file and recovers."""
    save_source(cli, data_dir, 'Treasure', filler=99, prefix='TCard')
    assert cli('get-deck', 'Treasure')[0] == 0
    (decks_dir(data_dir) / 'treasure.yaml').unlink()
    os.utime(decks_dir(data_dir))
    assert yaml_files(data_dir) == []

    expire('Treasure')
    # save-deck the same deck: recovery writes a FRESH file, exit 0.
    code, _out, err = save_json(
        cli,
        data_dir,
        commander_deck('Treasure', filler=99, prefix='TCard').model_dump(mode='json'),
    )
    assert code == 0, err
    files = yaml_files(data_dir)
    assert files == ['treasure.yaml'], files  # exactly one, freshly minted

    expire('Treasure')
    code, out, _err = cli('get-deck', 'Treasure')
    assert code == 0 and len(json.loads(out)['cards']) == 100

    # The recovered deck is now healthy: a subsequent edit lands (no dead binding).
    expire('Treasure')
    assert cli('set-strategy', 'Treasure', 'recovered')[0] == 0


def test_savedeck_reuuid_file_creates_fresh_no_stranger_adopt(cli, data_dir):
    """save-deck when the bound file's uuid was hand-changed recovers at a fresh id.

    The re-identified file is a STRANGER (a foreign edit). Recovery must NOT adopt it
    — it mints a fresh file for this row, leaving the stranger's foreign content intact.
    """
    save_source(cli, data_dir, 'Gruul', filler=99, prefix='GCard')
    assert cli('get-deck', 'Gruul')[0] == 0
    d = DecksStore()
    ext = next(r.external_ids for r in d.list_rows() if r.name == 'Gruul')
    bound = json.loads(ext)['local']

    gruul = decks_dir(data_dir) / 'gruul.yaml'
    text = gruul.read_text().replace(f'uuid: {bound}', 'uuid: deadbeefdeadbeefdeadbeefdeadbeef')
    text = text.replace('- card: GCard 0', '- card: Foreign Addition\n- card: GCard 0')
    gruul.write_text(text)
    os.utime(decks_dir(data_dir))
    expire('Gruul')

    code, _out, err = save_json(
        cli,
        data_dir,
        commander_deck('Gruul', filler=99, prefix='GCard').model_dump(mode='json'),
    )
    assert code == 0, err
    # The stranger file's foreign content survives (never adopted / overwritten).
    files = yaml_files(data_dir)
    assert 'gruul.yaml' in files
    assert 'Foreign Addition' in gruul.read_text()
    # A fresh disambiguated file was minted for this row (not the base slug stranger).
    fresh = [f for f in files if f.startswith('gruul') and f != 'gruul.yaml']
    assert len(fresh) == 1, files


def test_savedeck_airtable_deleted_record_creates_no_422(data_dir, monkeypatch):
    """save-deck on a dead Airtable binding takes create_record (not update→422)."""
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    at = MockAirtableDecks()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'C {i}') for i in range(99)]
    at.records['rec00001'] = Deck(name='Iroh', format='Commander', cards=cards, airtable_record_id='rec00001')
    at._n = 1
    access = DeckAccess(at, decks=decks)  # type: ignore[arg-type]
    access.read_deck('Iroh')
    u = decks.uuid_for_name('Iroh')
    del at.records['rec00001']  # the bound record is DELETED
    at.log.clear()

    fresh = Deck(name='Iroh', format='Commander', cards=cards)
    access.save_deck(fresh, allow_shrink=False)

    # A NEW record was CREATED (no 422 update against the dead record).
    assert not any('422' in c for c in at.log), at.log
    assert any('create_record' in c for c in at.log), at.log
    # Exactly one live record, and the row is REBOUND to its fresh recordId.
    assert len(at.records) == 1
    new_rid = next(iter(at.records))
    assert new_rid != 'rec00001'
    row_ext = json.loads(decks.external_ids(u) or '{}')
    assert row_ext.get('airtable') == new_rid, row_ext


def test_savedeck_airtable_does_not_adopt_stranger(data_dir, monkeypatch):
    """A same-named STRANGER Airtable record must never be adopted by recovery."""
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    at = MockAirtableDecks()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'C {i}') for i in range(99)]
    at.records['rec00001'] = Deck(name='Azula', format='Commander', cards=cards, airtable_record_id='rec00001')
    at._n = 1
    access = DeckAccess(at, decks=decks)  # type: ignore[arg-type]
    access.read_deck('Azula')
    u = decks.uuid_for_name('Azula')
    del at.records['rec00001']
    # A STRANGER same-named record appears (another user's deck).
    stranger = Deck(
        name='Azula',
        format='Commander',
        cards=[DeckCard(name='Stranger Cmdr', role='commander'), DeckCard(name='Stranger 1')],
        airtable_record_id='rec00777',
    )
    at.records['rec00777'] = stranger
    at.log.clear()

    access.save_deck(Deck(name='Azula', format='Commander', cards=cards), allow_shrink=False)

    # The row is bound to a FRESHLY-created record, NOT the stranger rec00777.
    row_ext = json.loads(decks.external_ids(u) or '{}')
    assert row_ext.get('airtable') not in (None, 'rec00777'), row_ext
    # The stranger record is untouched.
    assert any(c.name == 'Stranger Cmdr' for c in at.records['rec00777'].cards)


# --------------------------------------------------------------------------- #
# The recovery HATCH is safe: FORCED-FRESH write; ATOMIC ref-wipe.
# --------------------------------------------------------------------------- #


def test_savedeck_recovery_writes_fresh_backup_survives(cli, data_dir):
    """The advised ``save-deck "Vault"`` on a legacy-restore state must write a FRESH
    file and leave the restored backup byte-intact (never adopt it in place).
    """
    save_source(cli, data_dir, 'Vault', filler=99, prefix='VC')
    assert cli('get-deck', 'Vault')[0] == 0
    # A second synced deck so the backfill marker is minted (a clean pass persists it).
    save_source(cli, data_dir, 'Newer', filler=1, prefix='N')
    assert cli('get-deck', 'Newer')[0] == 0

    vy = decks_dir(data_dir) / 'vault.yaml'
    # cp -p style legacy restore: no uuid line, back-dated mtime so the marker key
    # stays unchanged (backfill gated → binding stays dead → the recovery hazard).
    restored = legacy_restore(vy, 'Vault', ['Old Vault Card', 'Irreplaceable Note'], old_mtime=True)
    expire('Vault')

    # The dead-binding read advises save-deck "Vault"; run it VERBATIM.
    code, out, err = cli('get-deck', 'Vault')
    assert code == 1, (out, err)
    assert 'save-deck "Vault"' in err

    code, _out, err = cli('save-deck', 'Vault', '--confirm')
    assert code == 0, err

    # The restored backup MUST survive byte-intact — never overwritten in place.
    assert vy.read_text() == restored, 'restored legacy backup was destroyed in place'
    assert 'Irreplaceable Note' in vy.read_text()

    # Recovery wrote a FRESH disambiguated file, not the base slug.
    fresh = [f for f in yaml_files(data_dir, 'vault*.yaml') if f != 'vault.yaml']
    assert fresh, f'no fresh vault-<uuid>.yaml minted: {yaml_files(data_dir, "vault*.yaml")}'

    # The recovered deck is served from the fresh file (the payload, 100 cards).
    expire('Vault')
    code, out, _err = cli('get-deck', 'Vault')
    assert code == 0
    served = json.loads(out)
    assert sum(c.get('quantity', 1) for c in served['cards']) == 100
    assert not any(c['name'] == 'Irreplaceable Note' for c in served['cards'])


def test_forced_fresh_never_adopts_legacy_slug(cli, data_dir):
    """A direct ``save_deck(force_fresh=True)`` never returns the legacy base-slug path."""
    src = source_store(data_dir)
    # A legacy no-uuid file at the base slug (a restored backup).
    decks_dir(data_dir).mkdir(parents=True, exist_ok=True)
    legacy = decks_dir(data_dir) / 'relic.yaml'
    body = legacy_restore(legacy, 'Relic', ['Keeper Card'])

    fresh = commander_deck('Relic', filler=5, prefix='RC')
    src.save_deck(fresh, allow_shrink=True, force_fresh=True)

    assert legacy.read_text() == body, 'legacy same-name file was adopted/overwritten'
    minted = [f for f in yaml_files(data_dir, 'relic*.yaml') if f != 'relic.yaml']
    assert minted, 'force_fresh did not disambiguate off the occupied base slug'


def test_failed_recovery_leaves_dead_binding_local(cli, data_dir):
    """A recovery whose create FAILS (read-only dir) must leave the row bound-and-dead;
    a retry with a same-named stranger at the slug must NOT adopt/overwrite it.
    """
    save_source(cli, data_dir, 'Cinder', filler=99, prefix='CC')
    assert cli('get-deck', 'Cinder')[0] == 0
    uuid = _rows('Cinder')[0].deck_uuid
    assert _binding_is_dead_now('Cinder', uuid) is False

    # Re-identified STRANGER at the slug (the source is gone, a different deck sits here).
    cy = decks_dir(data_dir) / 'cinder.yaml'
    stranger = (
        'name: Cinder\nuuid: ffffffffffffffffffffffffffffffff\ncards:\n- card: Stranger Cmdr\n- card: Stranger Keep 1\n'
    )
    cy.write_text(stranger)
    expire('Cinder')

    ext_before = _ext('Cinder')
    assert ext_before  # the row IS bound.
    assert _binding_is_dead_now('Cinder', uuid) is True  # bound + source re-identified.

    # Make the create fail: lock the decks dir read-only so the tmp-file write fails.
    d = decks_dir(data_dir)
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
    try:
        code, _out, _err = cli('save-deck', 'Cinder', '--confirm')
    finally:
        os.chmod(d, stat.S_IRWXU)
    assert code != 0, 'a failing create must surface a non-zero exit'

    # ATOMIC: the ref was NOT wiped; the row stays bound-and-dead → the refusal returns.
    assert _ext('Cinder') == ext_before, 'the dead ref was wiped by a failed recovery'
    expire('Cinder')
    assert _binding_is_dead_now('Cinder', uuid) is True

    # A RETRY must NOT name-adopt the stranger: its file is byte-intact and the row is
    # not rebound to it (it recovers to a FRESH file or refuses — never the stranger).
    expire('Cinder')
    cli('save-deck', 'Cinder', '--confirm')
    assert cy.read_text() == stranger, 'the stranger file was overwritten by a retry'
    assert 'ffffffffffffffffffffffffffffffff' not in (_ext('Cinder') or ''), 'the row was rebound to the stranger'


def test_failed_recovery_leaves_dead_binding_airtable(data_dir, monkeypatch):
    """A recovery whose create hits a 503 must leave the row bound-and-dead; a retry
    (with a same-named stranger RECORD present) must not bind to the stranger.
    """
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    at = MockAirtableDecks()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'C {i}') for i in range(99)]
    at.records['rec00001'] = Deck(name='Ember', format='Commander', cards=cards, airtable_record_id='rec00001')
    at._n = 1
    access = DeckAccess(at, decks=decks)  # type: ignore[arg-type]
    access.read_deck('Ember')
    uuid = decks.uuid_for_name('Ember')
    assert uuid is not None
    ext_before = decks.external_ids(uuid)

    # The bound record is deleted and a same-named STRANGER record appears.
    del at.records['rec00001']
    at.records['rec00888'] = Deck(name='Ember', format='Commander', cards=cards, airtable_record_id='rec00888')
    at.log.clear()

    assert binding_is_dead(decks, at, deck_uuid=uuid, source_ref='Ember') is True  # type: ignore[arg-type]

    # The recovery create hits a 503.
    at.fail_create = True
    payload = Deck(name='Ember', format='Commander', cards=cards)
    with pytest.raises(RuntimeError, match='503'):
        access.save_deck(payload, allow_shrink=True)

    # ATOMIC: ref not wiped; row stays bound-and-dead.
    assert decks.external_ids(uuid) == ext_before, 'the dead ref was wiped on a failed create'
    assert binding_is_dead(decks, at, deck_uuid=uuid, source_ref='Ember') is True  # type: ignore[arg-type]

    # A RETRY (503 cleared) recovers to a FRESH record — never binds to the stranger.
    at.fail_create = False
    access.save_deck(Deck(name='Ember', format='Commander', cards=cards), allow_shrink=True)
    bound_ext = json.loads(decks.external_ids(uuid) or '{}')
    assert bound_ext.get('airtable') != 'rec00888', 'the row was bound to the stranger record'
    assert 'rec00888' in at.records, 'the stranger record was mutated/overwritten'
    # The stranger record content is untouched.
    assert at.records['rec00888'].cards[0].name == 'Krenko, Mob Boss'


# --------------------------------------------------------------------------- #
# The advised recovery is executable + the local copy has an exit.
# --------------------------------------------------------------------------- #


def test_advised_save_deck_by_name_recovers(cli, data_dir):
    """The advised ``save-deck "X"`` (no --from-json) recovers from the local copy."""
    save_source(cli, data_dir, 'Sokka', filler=99, prefix='SC')
    assert cli('get-deck', 'Sokka')[0] == 0
    (decks_dir(data_dir) / 'sokka.yaml').unlink()
    os.utime(decks_dir(data_dir))
    expire('Sokka')

    # The dead read advises save-deck "Sokka"; run it verbatim, no --from-json.
    code, _out, err = cli('get-deck', 'Sokka')
    assert code == 1
    assert 'save-deck "Sokka"' in err

    code, _out, err = cli('save-deck', 'Sokka')
    assert code == 0, err

    # Recovered: a fresh file exists and get-deck now serves the local copy (100 cards).
    expire('Sokka')
    code, out, _err = cli('get-deck', 'Sokka')
    assert code == 0
    assert sum(c.get('quantity', 1) for c in json.loads(out)['cards']) == 100


def test_get_deck_local_serves_local_copy(cli, data_dir):
    """``get-deck "X" --local`` serves the local copy under a dead binding (no pull)."""
    save_source(cli, data_dir, 'Toph', filler=99, prefix='TC')
    assert cli('get-deck', 'Toph')[0] == 0
    (decks_dir(data_dir) / 'toph.yaml').unlink()
    os.utime(decks_dir(data_dir))
    expire('Toph')

    # A plain read refuses (dead binding).
    assert cli('get-deck', 'Toph')[0] == 1

    # --local serves the local copy with a source-missing note; NO pull, exit 0.
    code, out, err = cli('get-deck', 'Toph', '--local')
    assert code == 0, err
    note = (out + err).lower()
    assert 'source missing' in note or 'local copy' in note
    served = json.loads(out[out.index('{') :])
    assert sum(c.get('quantity', 1) for c in served['cards']) == 100
    # No fresh file was written (a --local read must not create/adopt anything).
    assert yaml_files(data_dir, 'toph*.yaml') == []


def test_list_decks_flags_dead_bound_deck(cli, data_dir):
    """``list-decks`` includes a dead-bound synced deck, flagged source-missing."""
    save_source(cli, data_dir, 'Zuko', filler=99, prefix='ZC')
    save_source(cli, data_dir, 'Iroh', filler=99, prefix='IC')
    assert cli('get-deck', 'Zuko')[0] == 0
    assert cli('get-deck', 'Iroh')[0] == 0
    (decks_dir(data_dir) / 'zuko.yaml').unlink()
    os.utime(decks_dir(data_dir))
    expire()

    code, out, _err = cli('list-decks')
    assert code == 0, out
    zuko_line = next((ln for ln in out.splitlines() if ln.startswith('Zuko')), None)
    assert zuko_line is not None, f'dead-bound Zuko omitted from list-decks:\n{out}'
    assert 'source-missing' in zuko_line, zuko_line
    assert any(ln.startswith('Iroh') for ln in out.splitlines())  # the live deck still listed.

    # --json carries a machine-readable flag too.
    code, out, _err = cli('list-decks', '--json')
    assert code == 0
    zuko = next(r for r in json.loads(out) if r['name'] == 'Zuko')
    assert zuko.get('source_missing') is True


def test_dup_named_dead_recovers_via_id(cli, data_dir):
    """A dup-named dead row recovers via ``save-deck "X" --id <prefix>``."""
    # Two SEPARATE source files both named 'Precious' (dup names are legal under uuid
    # identity — a promote clean-slate / a hand-made second deck). Materialize a row
    # for each by reading it into the store via a bound --id read.
    src = source_store(data_dir)
    decks_dir(data_dir).mkdir(parents=True, exist_ok=True)
    d1 = commander_deck('Precious', filler=99, prefix='PA')
    d2 = commander_deck('Precious', filler=50, prefix='PB')
    src.save_deck(d1, allow_shrink=True)
    src.save_deck(d2, allow_shrink=True)

    decks = DecksStore()
    for deck in (d1, d2):
        decks.put(
            deck,
            deck_uuid=deck.uuid,
            sync_status='synced',
            source_ref='Precious',
            synced_baseline=None,
            rationale='seed',
        )
        decks.set_external_id(deck.uuid, 'local', deck.uuid)

    rows = _rows('Precious')
    assert len(rows) == 2, f'expected two Precious rows, got {len(rows)}'

    # Kill ONE row's binding: delete its bound file.
    target = next(r for r in rows if r.deck_uuid == d1.uuid)
    path = src.find_deck_path_by_uuid(d1.uuid)
    assert path is not None
    path.unlink()
    os.utime(decks_dir(data_dir))
    expire('Precious')

    prefix = target.deck_uuid[:6]
    # A bare name save under dup names refuses with the candidate list naming --id.
    code, _out, err = cli('save-deck', 'Precious')
    assert code != 0
    assert '--id' in err

    # save-deck "Precious" --id <prefix> recovers THAT row.
    code, _out, err = cli('save-deck', 'Precious', '--id', prefix, '--confirm')
    assert code == 0, err
    expire('Precious')
    assert _binding_is_dead_now('Precious', target.deck_uuid) is False


# --------------------------------------------------------------------------- #
# stranger-present recovery — fresh mint, stranger untouched (both file + record).
# --------------------------------------------------------------------------- #


def test_stranger_present_recovery_local_mints_fresh(cli, data_dir):
    """Deleted source + a re-identified same-named stranger at the slug → fresh mint,
    stranger byte-intact, row bound to its OWN fresh uuid (not the stranger's).
    """
    save_source(cli, data_dir, 'Gruul', filler=99, prefix='GC')
    assert cli('get-deck', 'Gruul')[0] == 0
    uuid = _rows('Gruul')[0].deck_uuid

    gy = decks_dir(data_dir) / 'gruul.yaml'
    stranger = (
        'name: Gruul\nuuid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\ncards:\n- card: Stranger Cmdr\n- card: Stranger G 1\n'
    )
    gy.write_text(stranger)
    expire('Gruul')

    code, _out, err = cli('save-deck', 'Gruul', '--confirm')
    assert code == 0, err

    assert gy.read_text() == stranger, 'stranger at slug overwritten'
    ext = json.loads(_ext('Gruul') or '{}')
    assert ext.get('local') != 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'row bound to the stranger'
    expire('Gruul')
    assert _binding_is_dead_now('Gruul', uuid) is False


def test_stranger_present_recovery_airtable_mints_fresh(data_dir, monkeypatch):
    """Deleted record + a same-named stranger record → create_record only, stranger
    record untouched, row bound to the NEW record.
    """
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    at = MockAirtableDecks()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'C {i}') for i in range(99)]
    at.records['rec00777'] = Deck(name='Aang', format='Commander', cards=cards, airtable_record_id='rec00777')
    at._n = 777
    access = DeckAccess(at, decks=decks)  # type: ignore[arg-type]
    access.read_deck('Aang')
    uuid = decks.uuid_for_name('Aang')
    assert uuid is not None

    # Delete the bound record; a same-named stranger record appears at a different id.
    del at.records['rec00777']
    at.records['rec00999'] = Deck(name='Aang', format='Commander', cards=cards, airtable_record_id='rec00999')
    at.log.clear()

    access.save_deck(Deck(name='Aang', format='Commander', cards=cards), allow_shrink=True)

    # A create_record only (never update→422), and never binds to rec00999.
    assert not any('update_record' in ln for ln in at.log), at.log
    bound = json.loads(decks.external_ids(uuid) or '{}').get('airtable')
    assert bound not in (None, 'rec00999', 'rec00777'), (bound, at.log)
    assert at.records['rec00999'].cards[0].name == 'Krenko, Mob Boss'  # stranger untouched.


def test_case_alias_recovery_does_not_fork(cli, data_dir):
    """``save-deck --from-json`` with a CASE-ALIAS name on a dead row recovers THAT row,
    it does not create a new row+file and orphan the dead zombie.
    """
    save_source(cli, data_dir, 'Ember', filler=99, prefix='EC')
    assert cli('get-deck', 'Ember')[0] == 0
    uuid = _rows('Ember')[0].deck_uuid
    (decks_dir(data_dir) / 'ember.yaml').unlink()
    os.utime(decks_dir(data_dir))
    expire('Ember')

    # A case-alias authoring save (payload name 'EMBER') must recover the dead 'Ember'
    # row rather than fork a new one.
    payload = commander_deck('EMBER', filler=99, prefix='EC').model_dump(mode='json')
    (data_dir / 'ember.json').write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(data_dir / 'ember.json'), '--confirm')
    assert code == 0, err

    # Still ONE live row for Ember (no fork), recovered under its canonical name.
    rows = [r for r in DecksStore().list_rows() if r.name in ('Ember', 'EMBER') and r.sync_status == 'synced']
    assert len(rows) == 1, f'case-alias save forked into {len(rows)} rows'
    assert rows[0].deck_uuid == uuid, 'recovery did not land on the dead row'
    expire('Ember')
    assert _binding_is_dead_now('Ember', uuid) is False


# --------------------------------------------------------------------------- #
# --id recovery composes end-to-end (dead pinned row + live dup row).
# --------------------------------------------------------------------------- #


def _dup_dead_state(cli, data_dir: Path, name: str) -> tuple[str, str]:
    """Two same-named LOCAL rows; make the FIRST one's binding dead (source deleted).

    Returns (dead_uuid, live_uuid). Both bound on the ACTIVE (local) backend.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    save_source(cli, data_dir, name, filler=99, prefix='A')
    assert cli('get-deck', name)[0] == 0
    assert cli('new-draft', f'Junk{name}', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', f'Junk{name}', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', f'Junk{name}', '--to', name)[0] == 0
    rows = _rows(name)
    assert len(rows) == 2, rows
    dead, live = rows[0], rows[1]
    ext = json.loads(dead.external_ids or '{}')
    local_uuid = ext.get('local')
    assert local_uuid
    src = source_store(data_dir)
    path = src.find_deck_path_by_uuid(local_uuid)
    assert path is not None, 'dead row must have a bound source file to delete'
    path.unlink()
    expire(name)
    return dead.deck_uuid, live.deck_uuid


def test_savedeck_id_recovers_pinned_row(cli, data_dir):
    """``save-deck "Delta" --id <dead prefix>`` recovers the PINNED dead row end-to-end
    (no re-refusal on name ambiguity); the live sibling is untouched.
    """
    dead_uuid, live_uuid = _dup_dead_state(cli, data_dir, 'Delta')
    live_ext_before = DecksStore().external_ids(live_uuid)
    live_files_before = yaml_files(data_dir)

    code, out, err = cli('save-deck', 'Delta', '--id', dead_uuid[:6])
    assert code == 0, (out, err)

    # The pinned row recovered: its binding is no longer dead (fresh source minted).
    d = DecksStore()
    access = DeckAccess(source_store(data_dir), decks=d)
    row = d.get_row(dead_uuid)
    assert row is not None and row.source_ref is not None
    assert not access.is_binding_dead(dead_uuid, row.source_ref)
    # The live sibling is untouched (same binding).
    assert DecksStore().external_ids(live_uuid) == live_ext_before
    # A fresh disambiguated file was minted; the live sibling's file is still present.
    assert set(live_files_before).issubset(set(yaml_files(data_dir)))


def test_savedeck_id_live_dup_row_composes(cli, data_dir):
    """``save-deck "X" --id <LIVE dup row>`` composes end-to-end: the pinned uuid threads
    into the commit push so it does NOT re-refuse on NAME ambiguity (live path).
    """
    # Two live dup rows for the same name (no dead binding).
    data_dir.mkdir(parents=True, exist_ok=True)
    save_source(cli, data_dir, 'Gamma', filler=99, prefix='A')
    assert cli('get-deck', 'Gamma')[0] == 0
    assert cli('new-draft', 'JunkGamma', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkGamma', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkGamma', '--to', 'Gamma')[0] == 0
    rows = _rows('Gamma')
    assert len(rows) == 2, rows
    pinned = rows[0].deck_uuid

    # A bare ``save-deck "Gamma"`` refuses (ambiguous); the --id form must SUCCEED.
    bare_code, _o, _e = cli('save-deck', 'Gamma')
    assert bare_code != 0
    code, out, err = cli('save-deck', 'Gamma', '--id', pinned[:6])
    assert code == 0, (out, err)
    # Still exactly two rows (no fork), both live.
    assert len(_rows('Gamma')) == 2


# --------------------------------------------------------------------------- #
# get-deck --local note correct on healthy + dead; recovery preserves archived.
# --------------------------------------------------------------------------- #


def test_getlocal_note_healthy(cli, data_dir):
    """``get-deck --local`` on a HEALTHY deck must NOT claim "source missing" (false)."""
    save_source(cli, data_dir, 'Healthy', filler=99, prefix='H')
    assert cli('get-deck', 'Healthy')[0] == 0
    code, out, err = cli('get-deck', 'Healthy', '--local')
    assert code == 0, err
    assert 'source missing' not in err.lower()
    assert 'local copy' in err.lower()
    # STDOUT stays parseable deck JSON.
    assert json.loads(out)['name'] == 'Healthy'


def test_getlocal_note_dead(cli, data_dir):
    """``get-deck --local`` on a DEAD-bound deck still serves the local copy (note true)."""
    save_source(cli, data_dir, 'Vault', filler=99, prefix='V')
    assert cli('get-deck', 'Vault')[0] == 0
    vy = decks_dir(data_dir) / 'vault.yaml'
    vy.unlink()  # delete the bound source out of band → dead binding.
    expire('Vault')
    code, out, err = cli('get-deck', 'Vault', '--local')
    assert code == 0, err
    assert json.loads(out)['name'] == 'Vault'


def test_recovery_preserves_archived(cli, data_dir):
    """A save-deck recovery of an ARCHIVED dead deck must keep the archived flag set."""
    save_source(cli, data_dir, 'Attic', filler=99, prefix='A')
    assert cli('get-deck', 'Attic')[0] == 0
    d = DecksStore()
    uuid = d.uuid_for_name('Attic')
    assert uuid is not None
    # A synced row cannot be archived through the API (archive is for drafts), but the
    # ARCHIVED-and-dead state IS reachable (a formerly-archived draft that gained a
    # source, a hand-set flag). Set the flag directly to construct the state.
    _set_archived(uuid, True)
    before = DecksStore().get_row(uuid)
    assert before is not None and before.archived is True
    # Kill the bound source → dead binding.
    (decks_dir(data_dir) / 'attic.yaml').unlink()
    expire('Attic')

    code, _out, err = cli('save-deck', 'Attic')
    assert code == 0, err
    after = DecksStore().get_row(uuid)
    assert after is not None and after.archived is True, 'recovery must preserve archived'


# --------------------------------------------------------------------------- #
# recover-decks (admin verb) — dup-name scoping.
# --------------------------------------------------------------------------- #


def test_recover_decks_refuses_dup_names(cli, data_dir):
    """A REQUESTED duplicate name is refused (un-addressable by name)."""
    save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    # Explicitly requesting the dup name is refused (un-addressable by name).
    code, _out, err = cli('recover-decks', 'Precious')
    assert code == 1
    assert 'duplicate deck name' in err
    assert 'Precious' in err


def test_recover_decks_scopes_dup_refusal(cli, data_dir):
    """recover-decks of a uniquely-named deck is not blocked by an UNRELATED dup pair."""
    # A uniquely-named, under-target deck that IS recoverable.
    save_source(cli, data_dir, 'Solo', filler=98, prefix='SCard')  # 99 cards, target ...
    assert cli('get-deck', 'Solo')[0] == 0
    # An UNRELATED dup pair elsewhere.
    save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    # Recovering the uniquely-named Solo must NOT be blocked by the Precious dup.
    code, out, err = cli('recover-decks', 'Solo')
    assert code == 0, err
    assert 'duplicate deck name' not in out and 'duplicate deck name' not in err

    # Requesting a DUP name is still refused (naming it).
    code, _out, err = cli('recover-decks', 'Precious')
    assert code == 1
    assert 'duplicate deck name' in err and 'Precious' in err


def test_savedeck_fromjson_and_id_is_parser_error(cli, data_dir):
    """``save-deck --from-json f --id p`` → exit 2 (parser.error); nothing created."""
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = commander_deck('Combo', filler=99, prefix='C').model_dump(mode='json')
    p = data_dir / 'deck.json'
    p.write_text(json.dumps(payload))
    before = yaml_files(data_dir)
    code, _out, err = cli('save-deck', '--from-json', str(p), '--id', 'abcdef')
    assert code == 2, err
    assert '--from-json' in err and '--id' in err
    assert yaml_files(data_dir) == before  # nothing authored.
