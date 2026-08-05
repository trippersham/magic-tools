"""The write-side dead / cross-backend REFUSAL.

A write against a row that IS bound for the active backend but whose bound source
reads back None must REFUSE — never fall through the ``current_source is None``
guard-skip and silently create / adopt an existing slug file / fork. A genuine
FIRST push (no bound ref at all) still creates, unchanged. A row bound on a
DIFFERENT backend than the active one refuses with the "switch back to its source
backend" advice (the read-side refusal, mirrored on the write side).

- ``binding_is_dead`` splits never-created from ref-dead;
- a deleted / re-uuid'd bound file → every write REFUSES (no recreate, no fork; a
  foreign edit survives);
- the dead-binding message is backend-aware, names ONLY ``save-deck`` (no "pull",
  no "--recreate"), and ``--recreate`` is gone from push/sync entirely;
- an airtable-bound row under the local backend does not clobber a same-named
  local file (read + backfill halves);
- the cross-backend write refusal (all four write verbs) + genuine-first-push
  still creates + same-backend live write unaffected + the dup-dead message names
  ``--id``.

Everything OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of record
and a REAL canonicalizing resolver hydrates cards (the hazard is exercised, never
stubbed). Airtable findings run against a CONTRACT DOUBLE mirroring the current
adapter. ZERO prod writes; no network; no ``delete_record``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from _decks_helpers import (
    CanonicalizingResolver,
    MockAirtableDecks,
    commander_deck,
    decks_dir,
    expire,
    legacy_restore,
    save_source,
    source_store,
    yaml_files,
)

from pipeline.contracts import Deck, DeckCard
from pipeline.decks import DecksStore
from pipeline.decks.access import DeckAccess
from pipeline.decks.sync import DeadBindingError, binding_is_dead, push


def _synced_row(name: str) -> tuple[str, str | None]:
    """Return ``(deck_uuid, external_ids)`` for the single synced row named ``name``."""
    d = DecksStore()
    rows = [r for r in d.list_rows() if r.name == name and r.sync_status == 'synced']
    assert len(rows) == 1, f'expected 1 synced row named {name!r}, got {len(rows)}'
    row = rows[0]
    return row.deck_uuid, row.external_ids


def _rows(name: str) -> list:
    d = DecksStore()
    return [r for r in d.list_rows(include_archived=True) if r.name == name and r.sync_status != 'consumed']


def _set_ext(name: str, ext: dict[str, str]) -> None:
    d = DecksStore()
    rows = _rows(name)
    assert rows, name
    d.replace_external_ids(rows[0].deck_uuid, ext)


def _ext_of(name: str) -> str | None:
    rows = _rows(name)
    return rows[0].external_ids if rows else None


# --------------------------------------------------------------------------- #
# Unit: binding_is_dead / require_writable_binding split never-created from ref-dead.
# --------------------------------------------------------------------------- #


def test_binding_is_dead_splits_never_created_from_ref_dead(data_dir, monkeypatch):
    """The write-side split: no bound ref → NOT dead (first push); bound + gone → dead."""
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    driver = source_store(data_dir)

    # A never-created deck (a fresh ephemeral-then-promote clean slate): no bound ref.
    d_uuid = uuid4().hex
    deck = commander_deck('Fresh', filler=2, prefix='F').model_copy(update={'uuid': d_uuid})
    decks.put(deck, deck_uuid=d_uuid, sync_status='synced', source_ref='Fresh', synced_baseline=None)
    assert binding_is_dead(decks, driver, deck_uuid=d_uuid, source_ref='Fresh') is False  # never-created
    # A first push (no bound ref) CREATES — the guard must not refuse it.
    push(decks, driver, deck_uuid=d_uuid)
    assert 'fresh.yaml' in yaml_files(data_dir)

    # Now bind + delete the source: the ref is DEAD → the guard refuses.
    _uuid, ext = _synced_row('Fresh')
    assert ext and 'local' in json.loads(ext)
    (decks_dir(data_dir) / 'fresh.yaml').unlink()
    assert binding_is_dead(decks, driver, deck_uuid=d_uuid, source_ref='Fresh') is True
    with pytest.raises(DeadBindingError):
        push(decks, driver, deck_uuid=d_uuid)


# --------------------------------------------------------------------------- #
# Deleted / re-uuid'd bound file → every write REFUSES (no recreate, no fork).
# --------------------------------------------------------------------------- #


def test_deleted_bound_file_write_refuses_no_silent_recreate(cli, data_dir):
    """A synced row whose bound FILE was deleted refuses every write (no resurrect)."""
    save_source(cli, data_dir, 'Treasure', filler=99, prefix='TCard')
    assert cli('get-deck', 'Treasure')[0] == 0
    (decks_dir(data_dir) / 'treasure.yaml').unlink()
    os.utime(decks_dir(data_dir))  # bump so the backfill re-runs (file HAS a uuid → dead ref)

    for verb in (
        ('sync', 'Treasure'),
        ('push', 'Treasure'),
        ('deck-add', 'Treasure', 'Phoenix Card'),
        ('set-strategy', 'Treasure', 'risen'),
    ):
        expire('Treasure')
        code, _out, err = cli(*verb)
        assert code == 1, f'{verb} should refuse a dead binding, got exit {code}'
        # Honest message: names ONLY save-deck; no circular "pull"; no --recreate.
        assert 'deck file' in err and 'save-deck' in err
        assert 'pull' not in err.lower() and 'recreate' not in err.lower()
        # The source was NOT silently recreated.
        assert yaml_files(data_dir) == []


def test_reuuid_bound_file_write_refuses_no_fork_foreign_edit_survives(cli, data_dir):
    """A row whose bound file's in-file uuid was hand-edited refuses writes; no fork."""
    save_source(cli, data_dir, 'Gruul', filler=99, prefix='GCard')
    assert cli('get-deck', 'Gruul')[0] == 0
    _uuid, ext = _synced_row('Gruul')
    assert ext is not None
    bound = json.loads(ext)['local']

    gruul = decks_dir(data_dir) / 'gruul.yaml'
    text = gruul.read_text().replace(f'uuid: {bound}', 'uuid: deadbeefdeadbeefdeadbeefdeadbeef')
    text = text.replace('- card: GCard 0', '- card: Foreign Addition\n- card: GCard 0')
    gruul.write_text(text)
    os.utime(decks_dir(data_dir))
    expire('Gruul')

    code, _out, err = cli('push', 'Gruul')
    assert code == 1 and 'deck file' in err and 'save-deck' in err
    assert 'pull' not in err.lower() and 'recreate' not in err.lower()
    # No disambiguated FORK file (gruul-xxxx.yaml) was created; the foreign edit lives on.
    assert yaml_files(data_dir) == ['gruul.yaml']
    assert 'Foreign Addition' in gruul.read_text()

    expire('Gruul')
    code, _out, err = cli('deck-add', 'Gruul', 'X Card')
    assert code == 1 and 'deck file' in err and 'save-deck' in err
    assert yaml_files(data_dir) == ['gruul.yaml']
    assert 'Foreign Addition' in gruul.read_text()


# --------------------------------------------------------------------------- #
# --recreate is GONE (args + help); the dead-binding message names ONLY save-deck.
# --------------------------------------------------------------------------- #


def test_recreate_flag_removed_from_push_and_sync(cli, data_dir):
    """``--recreate`` is not a valid flag on push/sync and is not mentioned in help."""
    save_source(cli, data_dir, 'Flagless', filler=99, prefix='FCard')

    # The flag is rejected by argparse (exit 2 — unrecognized argument).
    for verb in ('push', 'sync'):
        code, _out, err = cli(verb, 'Flagless', '--recreate')
        assert code == 2, f'{verb} --recreate should be an argparse error, got {code}'
        assert 'recreate' in err.lower()  # argparse's "unrecognized arguments: --recreate"

    # The help text mentions no --recreate anywhere.
    for verb in ('push', 'sync'):
        code, out, _err = cli(verb, '--help')
        assert code == 0
        assert '--recreate' not in out
        assert 'recreate' not in out.lower()


def _assert_honest_message(err: str) -> None:
    """The dead-binding message must name ONLY save-deck and be hallucination-free."""
    low = err.lower()
    assert 'save-deck' in low, err
    # No hallucinated / circular recovery step.
    assert 'run "pull"' not in low, err
    assert 'run pull' not in low, err
    assert '--recreate' not in low, err
    assert 'recreate' not in low, err
    assert 'Your local copy is intact' in err, err


def test_dead_binding_local_message_names_only_save_deck(cli, data_dir):
    """A deleted local bound file → edit verbs refuse with the honest local message."""
    save_source(cli, data_dir, 'Treasure', filler=99, prefix='TCard')
    assert cli('get-deck', 'Treasure')[0] == 0
    (decks_dir(data_dir) / 'treasure.yaml').unlink()
    os.utime(decks_dir(data_dir))

    for verb in (
        ('push', 'Treasure'),
        ('deck-add', 'Treasure', 'Phoenix Card'),
        ('set-strategy', 'Treasure', 'risen'),
    ):
        expire('Treasure')
        code, _out, err = cli(*verb)
        assert code == 1, f'{verb} should refuse, got {code}: {err}'
        _assert_honest_message(err)
        assert 'deck file' in err.lower()  # backend-specific: local YAML wording
        assert yaml_files(data_dir) == []  # nothing created / forked


def test_dead_binding_airtable_message_names_only_save_deck(data_dir, monkeypatch):
    """A deleted Airtable record → the write refuses with the honest Airtable message."""
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    at = MockAirtableDecks()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'C {i}') for i in range(99)]
    at.records['rec00001'] = Deck(name='Ozai', format='Commander', cards=cards, airtable_record_id='rec00001')
    at._n = 1
    access = DeckAccess(at, decks=decks)  # type: ignore[arg-type]
    access.read_deck('Ozai')
    del at.records['rec00001']
    at.log.clear()

    with pytest.raises(DeadBindingError) as exc:
        access.push('Ozai')
    msg = str(exc.value)
    _assert_honest_message(msg)
    assert 'record' in msg.lower()  # backend-specific: Airtable wording
    # Refused with ZERO writes (no create, no 422 update attempt).
    assert at.log == [] or all('get_deck' in c for c in at.log), at.log
    assert at.records == {}


# --------------------------------------------------------------------------- #
# An airtable-bound row under the local backend does not clobber a local file.
# --------------------------------------------------------------------------- #


def test_airtable_bound_row_under_local_does_not_clobber_local_file(data_dir, monkeypatch):
    """A row bound only ``{'airtable': rec}`` read under the local backend must not
    silently adopt/clobber an UNRELATED same-named local file — it refuses instead.
    """
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    at = MockAirtableDecks()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'ACard {i}') for i in range(99)]
    at.records['rec00001'] = Deck(name='Shared', format='Commander', cards=cards, airtable_record_id='rec00001')
    at._n = 1
    at_access = DeckAccess(at, decks=decks)  # type: ignore[arg-type]
    at_access.read_deck('Shared')  # binds the row to {'airtable': 'rec00001'}
    bound_uuid = at_access.resolve('Shared')
    ext_raw = decks.external_ids(bound_uuid)
    assert ext_raw is not None and 'airtable' in json.loads(ext_raw)

    # An UNRELATED same-named local deck exists in the local collection.
    ldir = decks_dir(data_dir)
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / 'shared.yaml').write_text(
        'uuid: 1111aaaa1111aaaa1111aaaa1111aaaa\nname: Shared\ncards:\n- card: Local Only Card\n  role: commander\n'
    )
    before = (ldir / 'shared.yaml').read_text()

    local = source_store(data_dir)
    local_access = DeckAccess(local, decks=decks)
    expire('Shared')

    # Under the LOCAL backend the airtable-bound row refuses to read (its source is on
    # another backend) rather than adopt the unrelated local file.
    from pipeline.decks.store import DecksError

    with pytest.raises(DecksError):
        local_access.read_deck('Shared')

    # The unrelated local file is untouched; the row still holds the airtable content.
    assert (ldir / 'shared.yaml').read_text() == before
    row_deck = decks.get(bound_uuid)
    assert row_deck is not None and len(row_deck.cards) == 100


def test_backfill_does_not_adopt_local_name_for_airtable_bound_row(data_dir, monkeypatch):
    """A row bound only {'airtable': rec} must not adopt a same-named local file
    under the local backend (the refusal is honored on the write/backfill half too).
    """
    from pipeline.collection import resolver as resolver_mod
    from pipeline.decks.store import DecksError

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    at = MockAirtableDecks()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'ACard {i}') for i in range(99)]
    at.records['rec00001'] = Deck(name='Shared', format='Commander', cards=cards, airtable_record_id='rec00001')
    at._n = 1
    at_access = DeckAccess(at, decks=decks)  # type: ignore[arg-type]
    at_access.read_deck('Shared')
    bound_uuid = at_access.resolve('Shared')

    ldir = decks_dir(data_dir)
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / 'shared.yaml').write_text(
        'uuid: 1111aaaa1111aaaa1111aaaa1111aaaa\nname: Shared\ncards:\n- card: Local Only Card\n  role: commander\n'
    )
    before = (ldir / 'shared.yaml').read_text()

    local = source_store(data_dir)
    local_access = DeckAccess(local, decks=decks)
    expire('Shared')
    with pytest.raises(DecksError):
        local_access.read_deck('Shared')
    # The unrelated local file is untouched and the row was NOT rebound to it.
    assert (ldir / 'shared.yaml').read_text() == before
    ext = json.loads(decks.external_ids(bound_uuid) or '{}')
    assert 'local' not in ext, ext


# --------------------------------------------------------------------------- #
# Write-side cross-backend refusal (the one guard).
# --------------------------------------------------------------------------- #


def _cross_backend_state(cli, data_dir: Path, name: str, cards: list[str]) -> str:
    """Build the cross-backend state for ``name``: a row bound on AIRTABLE (active
    backend local) + a marker-evading legacy restore at the slug (backfill gated).

    Returns the restored backup's byte content so a caller can assert survival.
    A second, newer synced deck holds the max-mtime so the restore stays gated.
    """
    save_source(cli, data_dir, name, filler=99, prefix='X')
    assert cli('get-deck', name)[0] == 0
    # A newer file (distinct slug) holds the max mtime — the restore's old mtime cannot
    # become the max, so the backfill marker stays gated.
    save_source(cli, data_dir, 'Newer', filler=1, prefix='N')
    assert cli('get-deck', 'Newer')[0] == 0
    # Bind the row to AIRTABLE only — the ACTIVE backend is local, so it is bound on a
    # DIFFERENT backend (a cross-machine / backend-switch row).
    _set_ext(name, {'airtable': f'rec-{name}'})
    slug = name.lower()
    path = decks_dir(data_dir) / f'{slug}.yaml'
    body = legacy_restore(path, name, cards, old_mtime=True)
    expire(name)
    return body


@pytest.mark.parametrize('verb', ['deck-add', 'push', 'sync', 'save-deck'])
def test_crossbackend_write_verbs_refuse(cli, data_dir, verb):
    """All four write verbs REFUSE the cross-backend+legacy-restore state with the
    "switch back to its source backend" message; the restored backup survives byte-intact
    and the row is unchanged (never gains a local ref, never overwritten in place).
    """
    name = 'Relic'
    body = _cross_backend_state(cli, data_dir, name, ['Family Heirloom A', 'R Note'])
    slug_path = decks_dir(data_dir) / f'{name.lower()}.yaml'
    ext_before = _ext_of(name)

    if verb == 'deck-add':
        code, out, err = cli('deck-add', name, 'Some New Card')
    elif verb == 'save-deck':
        code, out, err = cli('save-deck', name)
    else:
        code, out, err = cli(verb, name)

    assert code != 0, (verb, out, err)
    assert 'switch back to its source backend' in (out + err), (verb, out, err)
    # The restored backup MUST survive byte-intact — never overwritten/adopted in place.
    assert slug_path.read_text() == body, verb
    assert 'Family Heirloom A' in slug_path.read_text(), verb
    # The row is unchanged: no local ref adopted; still bound only on airtable.
    ext_after = _ext_of(name)
    assert ext_after == ext_before, verb
    assert json.loads(ext_after or '{}').get('local') is None, verb
    # Exactly ONE slug file — never disambiguated/forked next to the restore.
    assert yaml_files(data_dir, f'{name.lower()}*.yaml') == [f'{name.lower()}.yaml'], verb


def test_genuine_first_push_still_creates(cli, data_dir):
    """A row with NO external ref at all is a genuine first push — the guard must NOT
    refuse it; save-deck --from-json creates the source normally.
    """
    payload = commander_deck('FirstPush', filler=99, prefix='F').model_dump(mode='json')
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / 'deck.json'
    p.write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(p))
    assert code == 0, err
    # The source file exists and the row is bound on the ACTIVE (local) backend.
    assert 'firstpush.yaml' in yaml_files(data_dir)
    assert json.loads(_ext_of('FirstPush') or '{}').get('local'), _ext_of('FirstPush')


def test_same_backend_live_write_unaffected(cli, data_dir):
    """A normal same-backend (local) live deck edits + pushes as before — the new guard
    only fires for a row bound ELSEWHERE, never a healthy local binding.
    """
    save_source(cli, data_dir, 'Healthy', filler=99, prefix='H')
    assert cli('get-deck', 'Healthy')[0] == 0
    code, _out, err = cli('deck-add', 'Healthy', 'Sol Ring')
    assert code == 0, err
    code, _out, err = cli('push', 'Healthy')
    assert code == 0, err


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
    # Kill the DEAD row's bound source file (delete it out of band → dead binding).
    ext = json.loads(dead.external_ids or '{}')
    local_uuid = ext.get('local')
    assert local_uuid
    src = source_store(data_dir)
    path = src.find_deck_path_by_uuid(local_uuid)
    assert path is not None, 'dead row must have a bound source file to delete'
    path.unlink()
    expire(name)
    return dead.deck_uuid, live.deck_uuid


def test_dup_dead_message_names_id(cli, data_dir):
    """Under dup names, the write-side dead-binding refusal advises ``--id <prefix>`` —
    not a bare ``save-deck "X"`` that would only re-refuse on ambiguity.
    """
    dead_uuid, _live = _dup_dead_state(cli, data_dir, 'Delta')
    # A write against the dead dup row (addressed by --id) refuses with the dup-aware msg.
    code, out, err = cli('push', 'Delta', '--id', dead_uuid[:6])
    assert code != 0, (out, err)
    msg = out + err
    assert 'save-deck "Delta" --id' in msg, msg


def test_backfill_does_not_adopt_cross_backend(cli, data_dir):
    """The legacy-backfill must NOT bind a same-named local backup to a row bound on
    ANOTHER backend: its source lives there, not as a local file.
    """
    # A synced row bound ONLY on airtable (a backend-switch / cross-machine row).
    decks = DecksStore()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'F {i}') for i in range(99)]
    seed = Deck(name='Foreign', format='Commander', cards=cards)
    decks.put(
        seed, deck_uuid=seed.uuid, sync_status='synced', source_ref='Foreign', synced_baseline=None, rationale='seed'
    )
    decks.set_external_id(seed.uuid, 'airtable', 'recFOREIGN')

    # A dropped-in same-named LEGACY (no-uuid) local backup at the slug.
    decks_dir(data_dir).mkdir(parents=True, exist_ok=True)
    legacy_restore(decks_dir(data_dir) / 'foreign.yaml', 'Foreign', ['Stray Backup Card'])

    # Any local verb triggers the backfill. The airtable-bound row must NOT gain a
    # local ref (which would adopt the stray backup cross-backend).
    cli('list-decks')

    ext = json.loads(decks.external_ids(seed.uuid) or '{}')
    assert 'local' not in ext, f'backfill adopted a same-named local backup cross-backend: {ext}'
    assert ext.get('airtable') == 'recFOREIGN'
