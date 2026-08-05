"""Round-9 hardening regressions (Phase 6 P12) — safe-by-construction recovery.

Round 8 (P11) removed ``--recreate`` and made a fresh-identity ``save-deck`` the
one recovery, and that held for the primary path. Round 9 found that the recovery
HATCH itself re-opened the exit-0 destruction class this redesign has fought since
round 5, and one hole is reached by following the tool's OWN advice:

- **r9-B1 (BLOCKER):** on a marker-evading legacy restore (a pre-P6 no-uuid backup
  dropped in place with an old mtime, so the backfill stays gated and the binding
  stays dead), the advised ``save-deck "X"`` recovery landed IN PLACE on the
  restored backup — the local adapter's legacy-upgrade branch (no in-file uuid +
  same name → base slug) adopted it as the "fresh" create target. Exit 0, no
  ledger. FIX: recovery writes FORCED-FRESH — ``save_deck(force_fresh=True)`` never
  returns the base-slug / legacy-upgrade path when the slug is occupied by another
  file; it always disambiguates to ``<slug>-<uuid>.yaml``.

- **r9-B2 (BLOCKER):** the recovery wiped the row's external ref BEFORE the create
  and never restored it on failure, so one transient create failure converted the
  proven-safe refusal state into the round-5/6 name-adoption regime. FIX: the
  ref-wipe is atomic with the create — create fresh FIRST, replace the ref only on
  success; on failure leave the old (dead) ref so ``binding_is_dead`` stays True and
  the honest refusal is restored (a retry cannot adopt a same-named stranger).

- **r9-M1 (MAJOR):** the advised recovery step was not executable. FIX: ``save-deck
  "<name>"`` (no ``--from-json``) recovers from the LOCAL copy to a fresh source;
  ``save-deck --id <prefix>`` recovers a dup-named dead row; ``list-decks`` shows a
  dead-bound deck flagged ``[synced,source-missing]``; ``get-deck "X" --local``
  serves the local copy under a dead binding (no pull).

Everything OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of record
and a REAL canonicalizing resolver hydrates cards (the hazard is exercised, never
stubbed). Airtable findings run against a CONTRACT DOUBLE mirroring the CURRENT
adapter (create stamps recordId; update of a deleted record raises 422). ZERO prod
writes; no network; no ``delete_record``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore
from pipeline.decks.access import DeckAccess
from pipeline.decks.sync import binding_is_dead


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
    return [r for r in d.list_rows() if r.name == name and r.sync_status != 'consumed']


def _ext(name: str) -> str | None:
    rows = _rows(name)
    return rows[0].external_ids if rows else None


def _source_store(data_dir: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=data_dir / 'collection')


def _legacy_restore(path: Path, name: str, cards: list[str], *, old_mtime: bool = False) -> str:
    """Drop a pre-P6 (no in-file uuid) backup at ``path`` — the restore persona.

    ``old_mtime`` back-dates the file so the per-file backfill marker key stays
    unchanged (the cp -p case): the backfill stays gated, so the row's binding stays
    dead and the restored legacy file is a marker-evading same-named no-uuid file at
    the slug — the exact r9-B1 hazard.
    """
    body = f'name: {name}\ncards:\n' + ''.join(f'- card: {c}\n' for c in cards)
    path.write_text(body)
    if old_mtime:
        old = path.stat().st_mtime_ns - 10**12
        os.utime(path, ns=(old, old))
    return body


def _marker_key(data_dir: Path) -> str:
    d = _decks_dir(data_dir)
    mtimes = [p.stat().st_mtime_ns for p in sorted(d.glob('*.yaml'))]
    return f'{max(mtimes) if mtimes else 0}:{len(mtimes)}'


class MockAirtableDecks:
    """Contract double mirroring the CURRENT adapter: create stamps recordId;
    update of a deleted record raises 422 (the r8f double, verbatim shape).
    """

    backend_name = 'airtable'

    def __init__(self) -> None:
        self.records: dict[str, Deck] = {}
        self._n = 0
        self.log: list[str] = []
        self.fail_create = False

    def _mint(self) -> str:
        self._n += 1
        return f'rec{self._n:05d}'

    def get_deck(self, name: str) -> Deck:
        self.log.append(f'get_deck(name={name!r})')
        for rid, d in self.records.items():
            if d.name == name:
                return d.model_copy(update={'airtable_record_id': rid})
        raise FileNotFoundError(f'No Airtable Decks record named {name!r}.')

    def get_deck_by_record_id(self, record_id: str) -> Deck:
        self.log.append(f'get_deck_by_record_id({record_id})')
        if record_id in self.records:
            return self.records[record_id].model_copy(update={'airtable_record_id': record_id})
        raise FileNotFoundError(f'No Airtable Decks record with id {record_id!r}.')

    def save_deck(self, deck: Deck, *, allow_shrink: bool = False, force_fresh: bool = False) -> None:
        # ``force_fresh`` (r9-B1) is a no-op on Airtable: create/update is driven by
        # ``airtable_record_id`` (absent → create_record), already fresh for recovery.
        if deck.airtable_record_id:
            if deck.airtable_record_id not in self.records:
                self.log.append(f'update_record({deck.airtable_record_id}) -> 422 DELETED')
                raise RuntimeError(f'422: record {deck.airtable_record_id!r} not found')
            self.log.append(f'update_record({deck.airtable_record_id}, name={deck.name!r})')
            self.records[deck.airtable_record_id] = deck.model_copy()
        else:
            if self.fail_create:
                self.log.append('create_record -> 503 (simulated outage)')
                raise RuntimeError('503: Airtable unavailable')
            rid = self._mint()
            self.log.append(f'create_record(-> {rid}, name={deck.name!r})')
            self.records[rid] = deck.model_copy(update={'airtable_record_id': rid})
            deck.airtable_record_id = rid

    def list_decks(self) -> list[Deck]:
        return list(self.records.values())


# --------------------------------------------------------------------------- #
# r9-B1 — recovery writes FORCED-FRESH; a marker-evading legacy restore survives.
# --------------------------------------------------------------------------- #


def test_r9_b1_savedeck_recovery_writes_fresh_backup_survives(cli, data_dir):
    """The advised ``save-deck "Vault"`` on the r9c legacy-restore state must write a
    FRESH file and leave the restored backup byte-intact (never adopt it in place).
    """
    _save_source(cli, data_dir, 'Vault', filler=99, prefix='VC')
    assert cli('get-deck', 'Vault')[0] == 0
    # A second synced deck so the backfill marker is minted (a clean pass persists it).
    _save_source(cli, data_dir, 'Newer', filler=1, prefix='N')
    assert cli('get-deck', 'Newer')[0] == 0

    vy = _decks_dir(data_dir) / 'vault.yaml'
    # cp -p style legacy restore: no uuid line, back-dated mtime so the marker key
    # stays unchanged (backfill gated → binding stays dead → the r9-B1 hazard).
    restored = _legacy_restore(vy, 'Vault', ['Old Vault Card', 'Irreplaceable Note'], old_mtime=True)
    _expire('Vault')

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
    fresh = [f for f in _yaml_files(data_dir, 'vault*.yaml') if f != 'vault.yaml']
    assert fresh, f'no fresh vault-<uuid>.yaml minted: {_yaml_files(data_dir, "vault*.yaml")}'

    # The recovered deck is served from the fresh file (the payload, 100 cards).
    _expire('Vault')
    code, out, _err = cli('get-deck', 'Vault')
    assert code == 0
    served = json.loads(out)
    assert sum(c.get('quantity', 1) for c in served['cards']) == 100
    assert not any(c['name'] == 'Irreplaceable Note' for c in served['cards'])


def test_r9_b1_forced_fresh_never_adopts_legacy_slug(cli, data_dir):
    """A direct ``save_deck(force_fresh=True)`` never returns the legacy base-slug path."""
    src = _source_store(data_dir)
    # A legacy no-uuid file at the base slug (a restored backup).
    _decks_dir(data_dir).mkdir(parents=True, exist_ok=True)
    legacy = _decks_dir(data_dir) / 'relic.yaml'
    body = _legacy_restore(legacy, 'Relic', ['Keeper Card'])

    fresh = _commander_deck('Relic', filler=5, prefix='RC')
    src.save_deck(fresh, allow_shrink=True, force_fresh=True)

    assert legacy.read_text() == body, 'legacy same-name file was adopted/overwritten'
    minted = [f for f in _yaml_files(data_dir, 'relic*.yaml') if f != 'relic.yaml']
    assert minted, 'force_fresh did not disambiguate off the occupied base slug'


# --------------------------------------------------------------------------- #
# r9-B2 — a failed recovery leaves the dead binding; a retry cannot adopt a stranger.
# --------------------------------------------------------------------------- #


def test_r9_b2_failed_recovery_leaves_dead_binding_local(cli, data_dir):
    """A recovery whose create FAILS (read-only dir) must leave the row bound-and-dead;
    a retry with a same-named stranger at the slug must NOT adopt/overwrite it.
    """
    _save_source(cli, data_dir, 'Cinder', filler=99, prefix='CC')
    assert cli('get-deck', 'Cinder')[0] == 0
    uuid = _rows('Cinder')[0].deck_uuid
    assert binding_is_dead_now('Cinder', uuid) is False

    # Re-identified STRANGER at the slug (the source is gone, a different deck sits here).
    cy = _decks_dir(data_dir) / 'cinder.yaml'
    stranger = (
        'name: Cinder\nuuid: ffffffffffffffffffffffffffffffff\ncards:\n'
        '- card: Stranger Cmdr\n- card: Stranger Keep 1\n'
    )
    cy.write_text(stranger)
    _expire('Cinder')

    ext_before = _ext('Cinder')
    assert ext_before  # the row IS bound.
    assert binding_is_dead_now('Cinder', uuid) is True  # bound + source re-identified.

    # Make the create fail: lock the decks dir read-only so the tmp-file write fails.
    d = _decks_dir(data_dir)
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
    try:
        code, _out, _err = cli('save-deck', 'Cinder', '--confirm')
    finally:
        os.chmod(d, stat.S_IRWXU)
    assert code != 0, 'a failing create must surface a non-zero exit'

    # ATOMIC: the ref was NOT wiped; the row stays bound-and-dead → the refusal returns.
    assert _ext('Cinder') == ext_before, 'the dead ref was wiped by a failed recovery'
    _expire('Cinder')
    assert binding_is_dead_now('Cinder', uuid) is True

    # A RETRY must NOT name-adopt the stranger: its file is byte-intact and the row is
    # not rebound to it (it recovers to a FRESH file or refuses — never the stranger).
    _expire('Cinder')
    cli('save-deck', 'Cinder', '--confirm')
    assert cy.read_text() == stranger, 'the stranger file was overwritten by a retry'
    assert 'ffffffffffffffffffffffffffffffff' not in (_ext('Cinder') or ''), (
        'the row was rebound to the stranger'
    )


def test_r9_b2_failed_recovery_leaves_dead_binding_airtable(data_dir, monkeypatch):
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


def binding_is_dead_now(name: str, uuid: str) -> bool:
    from pipeline.collection.store import get_store

    driver = get_store()
    return binding_is_dead(DecksStore(), driver, deck_uuid=uuid, source_ref=name)


# --------------------------------------------------------------------------- #
# r9-M1 — the advised recovery is executable + the local copy has an exit.
# --------------------------------------------------------------------------- #


def test_r9_m1_advised_save_deck_by_name_recovers(cli, data_dir):
    """The advised ``save-deck "X"`` (no --from-json) recovers from the local copy."""
    _save_source(cli, data_dir, 'Sokka', filler=99, prefix='SC')
    assert cli('get-deck', 'Sokka')[0] == 0
    (_decks_dir(data_dir) / 'sokka.yaml').unlink()
    os.utime(_decks_dir(data_dir))
    _expire('Sokka')

    # The dead read advises save-deck "Sokka"; run it verbatim, no --from-json.
    code, _out, err = cli('get-deck', 'Sokka')
    assert code == 1
    assert 'save-deck "Sokka"' in err

    code, _out, err = cli('save-deck', 'Sokka')
    assert code == 0, err

    # Recovered: a fresh file exists and get-deck now serves the local copy (100 cards).
    _expire('Sokka')
    code, out, _err = cli('get-deck', 'Sokka')
    assert code == 0
    assert sum(c.get('quantity', 1) for c in json.loads(out)['cards']) == 100


def test_r9_m1_get_deck_local_serves_local_copy(cli, data_dir):
    """``get-deck "X" --local`` serves the local copy under a dead binding (no pull)."""
    _save_source(cli, data_dir, 'Toph', filler=99, prefix='TC')
    assert cli('get-deck', 'Toph')[0] == 0
    (_decks_dir(data_dir) / 'toph.yaml').unlink()
    os.utime(_decks_dir(data_dir))
    _expire('Toph')

    # A plain read refuses (dead binding).
    assert cli('get-deck', 'Toph')[0] == 1

    # --local serves the local copy with a source-missing note; NO pull, exit 0.
    code, out, err = cli('get-deck', 'Toph', '--local')
    assert code == 0, err
    note = (out + err).lower()
    assert 'source missing' in note or 'local copy' in note
    served = json.loads(out[out.index('{'):])
    assert sum(c.get('quantity', 1) for c in served['cards']) == 100
    # No fresh file was written (a --local read must not create/adopt anything).
    assert _yaml_files(data_dir, 'toph*.yaml') == []


def test_r9_m1_list_decks_flags_dead_bound_deck(cli, data_dir):
    """``list-decks`` includes a dead-bound synced deck, flagged source-missing."""
    _save_source(cli, data_dir, 'Zuko', filler=99, prefix='ZC')
    _save_source(cli, data_dir, 'Iroh', filler=99, prefix='IC')
    assert cli('get-deck', 'Zuko')[0] == 0
    assert cli('get-deck', 'Iroh')[0] == 0
    (_decks_dir(data_dir) / 'zuko.yaml').unlink()
    os.utime(_decks_dir(data_dir))
    _expire()

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


def test_r9_m1_dup_named_dead_recovers_via_id(cli, data_dir):
    """A dup-named dead row recovers via ``save-deck "X" --id <prefix>``."""
    # Two SEPARATE source files both named 'Precious' (dup names are legal under uuid
    # identity — a promote clean-slate / a hand-made second deck). Materialize a row
    # for each by reading it into the store via a bound --id read.
    src = _source_store(data_dir)
    _decks_dir(data_dir).mkdir(parents=True, exist_ok=True)
    d1 = _commander_deck('Precious', filler=99, prefix='PA')
    d2 = _commander_deck('Precious', filler=50, prefix='PB')
    src.save_deck(d1, allow_shrink=True)
    src.save_deck(d2, allow_shrink=True)

    decks = DecksStore()
    for deck in (d1, d2):
        decks.put(deck, deck_uuid=deck.uuid, sync_status='synced', source_ref='Precious',
                  synced_baseline=None, rationale='seed')
        decks.set_external_id(deck.uuid, 'local', deck.uuid)

    rows = _rows('Precious')
    assert len(rows) == 2, f'expected two Precious rows, got {len(rows)}'

    # Kill ONE row's binding: delete its bound file.
    target = next(r for r in rows if r.deck_uuid == d1.uuid)
    path = src.find_deck_path_by_uuid(d1.uuid)
    assert path is not None
    path.unlink()
    os.utime(_decks_dir(data_dir))
    _expire('Precious')

    prefix = target.deck_uuid[:6]
    # A bare name save under dup names refuses with the candidate list naming --id.
    code, _out, err = cli('save-deck', 'Precious')
    assert code != 0
    assert '--id' in err

    # save-deck "Precious" --id <prefix> recovers THAT row.
    code, _out, err = cli('save-deck', 'Precious', '--id', prefix, '--confirm')
    assert code == 0, err
    _expire('Precious')
    assert binding_is_dead_now('Precious', target.deck_uuid) is False


# --------------------------------------------------------------------------- #
# stranger-present recovery — fresh mint, stranger untouched (both file + record).
# --------------------------------------------------------------------------- #


def test_r9_stranger_present_recovery_local_mints_fresh(cli, data_dir):
    """Deleted source + a re-identified same-named stranger at the slug → fresh mint,
    stranger byte-intact, row bound to its OWN fresh uuid (not the stranger's).
    """
    _save_source(cli, data_dir, 'Gruul', filler=99, prefix='GC')
    assert cli('get-deck', 'Gruul')[0] == 0
    uuid = _rows('Gruul')[0].deck_uuid

    gy = _decks_dir(data_dir) / 'gruul.yaml'
    stranger = (
        'name: Gruul\nuuid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\ncards:\n'
        '- card: Stranger Cmdr\n- card: Stranger G 1\n'
    )
    gy.write_text(stranger)
    _expire('Gruul')

    code, _out, err = cli('save-deck', 'Gruul', '--confirm')
    assert code == 0, err

    assert gy.read_text() == stranger, 'stranger at slug overwritten'
    ext = json.loads(_ext('Gruul') or '{}')
    assert ext.get('local') != 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'row bound to the stranger'
    _expire('Gruul')
    assert binding_is_dead_now('Gruul', uuid) is False


# --------------------------------------------------------------------------- #
# Minors: r9-m1 (case-alias recovery does not fork) + r9-m2 (cross-backend backfill).
# --------------------------------------------------------------------------- #


def test_r9_m1_case_alias_recovery_does_not_fork(cli, data_dir):
    """``save-deck --from-json`` with a CASE-ALIAS name on a dead row recovers THAT row,
    it does not create a new row+file and orphan the dead zombie.
    """
    _save_source(cli, data_dir, 'Ember', filler=99, prefix='EC')
    assert cli('get-deck', 'Ember')[0] == 0
    uuid = _rows('Ember')[0].deck_uuid
    (_decks_dir(data_dir) / 'ember.yaml').unlink()
    os.utime(_decks_dir(data_dir))
    _expire('Ember')

    # A case-alias authoring save (payload name 'EMBER') must recover the dead 'Ember'
    # row rather than fork a new one.
    payload = _commander_deck('EMBER', filler=99, prefix='EC').model_dump(mode='json')
    (data_dir / 'ember.json').write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(data_dir / 'ember.json'), '--confirm')
    assert code == 0, err

    # Still ONE live row for Ember (no fork), recovered under its canonical name.
    rows = [r for r in DecksStore().list_rows() if r.name in ('Ember', 'EMBER') and r.sync_status == 'synced']
    assert len(rows) == 1, f'case-alias save forked into {len(rows)} rows'
    assert rows[0].deck_uuid == uuid, 'recovery did not land on the dead row'
    _expire('Ember')
    assert binding_is_dead_now('Ember', uuid) is False


def test_r9_m2_backfill_does_not_adopt_cross_backend(cli, data_dir):
    """The legacy-backfill must NOT bind a same-named local backup to a row bound on
    ANOTHER backend (honor r7-m4): its source lives there, not as a local file.
    """
    # A synced row bound ONLY on airtable (a backend-switch / cross-machine row).
    decks = DecksStore()
    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'F {i}') for i in range(99)]
    seed = Deck(name='Foreign', format='Commander', cards=cards)
    decks.put(seed, deck_uuid=seed.uuid, sync_status='synced', source_ref='Foreign',
              synced_baseline=None, rationale='seed')
    decks.set_external_id(seed.uuid, 'airtable', 'recFOREIGN')

    # A dropped-in same-named LEGACY (no-uuid) local backup at the slug.
    _decks_dir(data_dir).mkdir(parents=True, exist_ok=True)
    _legacy_restore(_decks_dir(data_dir) / 'foreign.yaml', 'Foreign', ['Stray Backup Card'])

    # Any local verb triggers the backfill. The airtable-bound row must NOT gain a
    # local ref (which would adopt the stray backup cross-backend).
    cli('list-decks')

    ext = json.loads(decks.external_ids(seed.uuid) or '{}')
    assert 'local' not in ext, f'backfill adopted a same-named local backup cross-backend: {ext}'
    assert ext.get('airtable') == 'recFOREIGN'


def test_r9_stranger_present_recovery_airtable_mints_fresh(data_dir, monkeypatch):
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
