"""Round-10 hardening regressions (Phase 6 P13) — write-side cross-backend refusal
+ recovery-advice composability.

Round 9 (P12) proved the recovery HATCH itself is safe by construction; the blind
agent sweep passed in full. Round 10 found ONE remaining exit-0 destruction path
(the last of the class this redesign has fought since round 5) plus a cluster of
advice/composability minors:

- **r10-M1 (MAJOR):** the READ side refuses a row bound on a DIFFERENT backend than
  the active one ("switch back to its source backend"); the WRITE side had no twin.
  ``binding_is_dead`` answers "no ref for the ACTIVE backend → never-created", so
  ``guard_write_binding`` passes and ``push`` falls through to a first-save create
  that adopts a same-named legacy file in place (marker-evading restore), destroying
  the restored backup at exit 0, unledgered. FIX: write-side r7-m4 parity — a row
  with an ``external_ids`` ref but NONE for the active backend REFUSES with the same
  "switch back to its source backend" message. A row with NO ref at all is still a
  genuine first push (create) — unchanged.

- **r10-m1/m2:** the write-side dead-binding message is now dup-aware (names
  ``save-deck "X" --id <prefix>`` under dup names), and ``save-deck "X" --id <p>``
  threads the pinned uuid into the commit so ``--id`` composes end-to-end.

- **r10-m3:** ``save-deck --from-json f --id p`` → ``parser.error`` (mutually
  exclusive).

- **r10-m4:** ``get-deck --local`` note is correct on healthy + dead.
- **r10-m6:** recovering an ARCHIVED dead deck keeps it archived.

Everything OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of record
and a REAL canonicalizing resolver hydrates cards (the hazard is exercised, never
stubbed). Airtable findings run against a CONTRACT DOUBLE mirroring the CURRENT
adapter. ZERO prod writes; no network; no ``delete_record``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore


class CanonicalizingResolver:
    """A REAL canonicalizing resolver (never stubbed to identity — the hazard)."""

    _CANON: ClassVar[dict[str, str]] = {
        'krenko, mob boss': 'Krenko, Mob Boss',
        'grumgully, the generous': 'Grumgully, the Generous',
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
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    return root


def _cli_run(*argv: str) -> tuple[int, str, str]:
    from pipeline.collection import run as cli

    old_argv = sys.argv
    sys.argv = ['collection', *argv]
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
            except Exception as exc:  # a raw traceback escaping the CLI = a crash.
                code = -1
                import traceback

                err.write(''.join(traceback.format_exception(exc)))
    finally:
        sys.argv = old_argv
    return code, out.getvalue(), err.getvalue()


@pytest.fixture()
def cli(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())
    return _cli_run


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _decks_dir(data_dir: Path) -> Path:
    return data_dir / 'collection' / 'decks'


def _yaml_files(data_dir: Path, glob: str = '*.yaml') -> list[str]:
    d = _decks_dir(data_dir)
    return sorted(p.name for p in d.glob(glob)) if d.exists() else []


def _commander_deck(name: str, *, filler: int = 99, prefix: str = 'Filler') -> Deck:
    cards = [DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander')]
    cards += [DeckCard(name=f'{prefix} {i}', quantity=1) for i in range(filler)]
    return Deck(name=name, format='commander', cards=cards)


def _save_source(cli, data_dir: Path, name: str, *, filler: int = 99, prefix: str = 'Filler') -> None:
    payload = _commander_deck(name, filler=filler, prefix=prefix).model_dump(mode='json')
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / 'deck.json'
    p.write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(p))
    assert code == 0, err


def _expire(name: str | None = None) -> None:
    d = DecksStore()
    for row in d.list_rows():
        if name is None or row.name == name:
            d.set_freshness(row.deck_uuid, {})


def _rows(name: str) -> list:
    d = DecksStore()
    return [r for r in d.list_rows(include_archived=True) if r.name == name and r.sync_status != 'consumed']


def _set_ext(name: str, ext: dict[str, str]) -> None:
    """Replace a row's external_ids (bind it, e.g. to another backend)."""
    d = DecksStore()
    rows = _rows(name)
    assert rows, name
    d.replace_external_ids(rows[0].deck_uuid, ext)


def _ext_of(name: str) -> str | None:
    rows = _rows(name)
    return rows[0].external_ids if rows else None


def _set_archived(deck_uuid: str, archived: bool) -> None:
    """Set a row's ``archived`` flag directly (the archived-and-synced state is
    reachable but not through the drafts-only ``archive`` API)."""
    d = DecksStore()
    with d._connect() as conn:  # test fixture reaches the same db.
        conn.execute('UPDATE decks SET archived = ? WHERE deck_uuid = ?', [archived, deck_uuid])


def _legacy_restore(path: Path, name: str, cards: list[str], *, old_mtime: bool = False) -> str:
    """Drop a pre-P6 (no in-file uuid) backup at ``path`` — the restore persona.

    ``old_mtime`` back-dates the file so the per-file backfill marker key stays
    unchanged (the cp -p case): the backfill stays gated, so the row's binding stays
    dead and the restored legacy file is a marker-evading same-named no-uuid file at
    the slug — the exact r10-M1 hazard.
    """
    body = f'name: {name}\ncards:\n' + ''.join(f'- card: {c}\n' for c in cards)
    path.write_text(body)
    if old_mtime:
        old = path.stat().st_mtime_ns - 10**12
        os.utime(path, ns=(old, old))
    return body


def _source_store(data_dir: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=data_dir / 'collection')


def _cross_backend_state(cli, data_dir: Path, name: str, cards: list[str]) -> str:
    """Build the r10d2 state for ``name``: a row bound on AIRTABLE (active backend
    local) + a marker-evading legacy restore at the slug (backfill gated).

    Returns the restored backup's byte content so a caller can assert survival.
    A second, newer synced deck holds the max-mtime so the restore stays gated.
    """
    _save_source(cli, data_dir, name, filler=99, prefix='X')
    assert cli('get-deck', name)[0] == 0
    # A newer file (distinct slug) holds the max mtime — the restore's old mtime cannot
    # become the max, so the backfill marker stays gated.
    _save_source(cli, data_dir, 'Newer', filler=1, prefix='N')
    assert cli('get-deck', 'Newer')[0] == 0
    # Bind the row to AIRTABLE only — the ACTIVE backend is local, so it is bound on a
    # DIFFERENT backend (a cross-machine / backend-switch row).
    _set_ext(name, {'airtable': f'rec-{name}'})
    slug = name.lower()
    path = _decks_dir(data_dir) / f'{slug}.yaml'
    body = _legacy_restore(path, name, cards, old_mtime=True)
    _expire(name)
    return body


# --------------------------------------------------------------------------- #
# r10-M1 — write-side cross-backend refusal (the one guard).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('verb', ['deck-add', 'push', 'sync', 'save-deck'])
def test_r10_m1_crossbackend_write_verbs_refuse(cli, data_dir, verb):
    """All four write verbs REFUSE the r10d2 cross-backend+legacy-restore state with the
    "switch back to its source backend" message; the restored backup survives byte-intact
    and the row is unchanged (never gains a local ref, never overwritten in place).
    """
    name = 'Relic'
    body = _cross_backend_state(cli, data_dir, name, ['Family Heirloom A', 'R Note'])
    slug_path = _decks_dir(data_dir) / f'{name.lower()}.yaml'
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
    assert _yaml_files(data_dir, f'{name.lower()}*.yaml') == [f'{name.lower()}.yaml'], verb


def test_r10_m1_genuine_first_push_still_creates(cli, data_dir):
    """A row with NO external ref at all is a genuine first push — the guard must NOT
    refuse it; save-deck --from-json creates the source normally.
    """
    payload = _commander_deck('FirstPush', filler=99, prefix='F').model_dump(mode='json')
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / 'deck.json'
    p.write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(p))
    assert code == 0, err
    # The source file exists and the row is bound on the ACTIVE (local) backend.
    assert 'firstpush.yaml' in _yaml_files(data_dir)
    assert json.loads(_ext_of('FirstPush') or '{}').get('local'), _ext_of('FirstPush')


def test_r10_m1_same_backend_live_write_unaffected(cli, data_dir):
    """A normal same-backend (local) live deck edits + pushes as before — the new guard
    only fires for a row bound ELSEWHERE, never a healthy local binding.
    """
    _save_source(cli, data_dir, 'Healthy', filler=99, prefix='H')
    assert cli('get-deck', 'Healthy')[0] == 0
    code, _out, err = cli('deck-add', 'Healthy', 'Sol Ring')
    assert code == 0, err
    code, _out, err = cli('push', 'Healthy')
    assert code == 0, err


# --------------------------------------------------------------------------- #
# r10-m1/m2 — dup-name dead row: message names --id; save-deck --id recovers.
# --------------------------------------------------------------------------- #


def _dup_dead_state(cli, data_dir: Path, name: str) -> tuple[str, str]:
    """Two same-named LOCAL rows; make the FIRST one's binding dead (source deleted).

    Returns (dead_uuid, live_uuid). Both bound on the ACTIVE (local) backend.
    """
    # Two distinct source decks that share a NAME (dup names, uuid identity): the first
    # via save-deck --from-json, the second by promoting a clean-slate draft --to <name>
    # (the code explicitly ALLOWS dup names on a clean-slate promote — never bind-by-name).
    data_dir.mkdir(parents=True, exist_ok=True)
    _save_source(cli, data_dir, name, filler=99, prefix='A')
    assert cli('get-deck', name)[0] == 0
    assert cli('new-draft', f'Junk{name}', '--commander', 'Grumgully, the Generous',
               '--format', 'Commander')[0] == 0
    assert cli('deck-add', f'Junk{name}', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', f'Junk{name}', '--to', name)[0] == 0
    rows = _rows(name)
    assert len(rows) == 2, rows
    dead, live = rows[0], rows[1]
    # Kill the DEAD row's bound source file (delete it out of band → dead binding).
    ext = json.loads(dead.external_ids or '{}')
    local_uuid = ext.get('local')
    assert local_uuid
    src = _source_store(data_dir)
    path = src.find_deck_path_by_uuid(local_uuid)
    assert path is not None, 'dead row must have a bound source file to delete'
    path.unlink()
    _expire(name)
    return dead.deck_uuid, live.deck_uuid


def test_r10_m1_dup_dead_message_names_id(cli, data_dir):
    """Under dup names, the write-side dead-binding refusal advises ``--id <prefix>`` —
    not a bare ``save-deck "X"`` that would only re-refuse on ambiguity.
    """
    dead_uuid, _live = _dup_dead_state(cli, data_dir, 'Delta')
    # A write against the dead dup row (addressed by --id) refuses with the dup-aware msg.
    code, out, err = cli('push', 'Delta', '--id', dead_uuid[:6])
    assert code != 0, (out, err)
    msg = out + err
    assert 'save-deck "Delta" --id' in msg, msg


def test_r10_m2_savedeck_id_recovers_pinned_row(cli, data_dir):
    """``save-deck "Delta" --id <dead prefix>`` recovers the PINNED dead row end-to-end
    (no re-refusal on name ambiguity); the live sibling is untouched.
    """
    dead_uuid, live_uuid = _dup_dead_state(cli, data_dir, 'Delta')
    live_ext_before = DecksStore().external_ids(live_uuid)
    live_files_before = _yaml_files(data_dir)

    code, out, err = cli('save-deck', 'Delta', '--id', dead_uuid[:6])
    assert code == 0, (out, err)

    # The pinned row recovered: its binding is no longer dead (fresh source minted).
    from pipeline.decks.access import DeckAccess

    d = DecksStore()
    access = DeckAccess(_source_store(data_dir), decks=d)
    row = d.get_row(dead_uuid)
    assert row is not None and row.source_ref is not None
    assert not access.is_binding_dead(dead_uuid, row.source_ref)
    # The live sibling is untouched (same binding).
    assert DecksStore().external_ids(live_uuid) == live_ext_before
    # A fresh disambiguated file was minted; the live sibling's file is still present.
    assert set(live_files_before).issubset(set(_yaml_files(data_dir)))


def test_r10_m2_savedeck_id_live_dup_row_composes(cli, data_dir):
    """``save-deck "X" --id <LIVE dup row>`` composes end-to-end: the pinned uuid threads
    into the commit push so it does NOT re-refuse on NAME ambiguity (r10-m2 live path).
    """
    # Two live dup rows for the same name (no dead binding).
    data_dir.mkdir(parents=True, exist_ok=True)
    _save_source(cli, data_dir, 'Gamma', filler=99, prefix='A')
    assert cli('get-deck', 'Gamma')[0] == 0
    assert cli('new-draft', 'JunkGamma', '--commander', 'Grumgully, the Generous',
               '--format', 'Commander')[0] == 0
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
# r10-m3 — save-deck --from-json + --id is a parser error (mutually exclusive).
# --------------------------------------------------------------------------- #


def test_r10_m3_fromjson_and_id_is_parser_error(cli, data_dir):
    """``save-deck --from-json f --id p`` → exit 2 (parser.error); nothing created."""
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = _commander_deck('Combo', filler=99, prefix='C').model_dump(mode='json')
    p = data_dir / 'deck.json'
    p.write_text(json.dumps(payload))
    before = _yaml_files(data_dir)
    code, _out, err = cli('save-deck', '--from-json', str(p), '--id', 'abcdef')
    assert code == 2, err
    assert '--from-json' in err and '--id' in err
    assert _yaml_files(data_dir) == before  # nothing authored.


# --------------------------------------------------------------------------- #
# r10-m4 — get-deck --local note correct on healthy + dead.
# --------------------------------------------------------------------------- #


def test_r10_m4_getlocal_note_healthy(cli, data_dir):
    """``get-deck --local`` on a HEALTHY deck must NOT claim "source missing" (false)."""
    _save_source(cli, data_dir, 'Healthy', filler=99, prefix='H')
    assert cli('get-deck', 'Healthy')[0] == 0
    code, out, err = cli('get-deck', 'Healthy', '--local')
    assert code == 0, err
    assert 'source missing' not in err.lower()
    assert 'local copy' in err.lower()
    # STDOUT stays parseable deck JSON.
    assert json.loads(out)['name'] == 'Healthy'


def test_r10_m4_getlocal_note_dead(cli, data_dir):
    """``get-deck --local`` on a DEAD-bound deck still serves the local copy (note true)."""
    _save_source(cli, data_dir, 'Vault', filler=99, prefix='V')
    assert cli('get-deck', 'Vault')[0] == 0
    vy = _decks_dir(data_dir) / 'vault.yaml'
    vy.unlink()  # delete the bound source out of band → dead binding.
    _expire('Vault')
    code, out, err = cli('get-deck', 'Vault', '--local')
    assert code == 0, err
    assert json.loads(out)['name'] == 'Vault'


# --------------------------------------------------------------------------- #
# r10-m6 — recovering an ARCHIVED dead deck keeps it archived.
# --------------------------------------------------------------------------- #


def test_r10_m6_recovery_preserves_archived(cli, data_dir):
    """A save-deck recovery of an ARCHIVED dead deck must keep the archived flag set."""
    _save_source(cli, data_dir, 'Attic', filler=99, prefix='A')
    assert cli('get-deck', 'Attic')[0] == 0
    d = DecksStore()
    uuid = d.uuid_for_name('Attic')
    assert uuid is not None
    # A synced row cannot be archived through the API (archive is for drafts), but the
    # ARCHIVED-and-dead state IS reachable (a formerly-archived draft that gained a
    # source, a hand-set flag). Set the flag directly to construct the r10-m6 state.
    _set_archived(uuid, True)
    before = DecksStore().get_row(uuid)
    assert before is not None and before.archived is True
    # Kill the bound source → dead binding.
    (_decks_dir(data_dir) / 'attic.yaml').unlink()
    _expire('Attic')

    code, _out, err = cli('save-deck', 'Attic')
    assert code == 0, err
    after = DecksStore().get_row(uuid)
    assert after is not None and after.archived is True, 'recovery must preserve archived'
