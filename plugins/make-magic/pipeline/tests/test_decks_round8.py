"""Round-8 hardening regressions (Phase 6 P11) — drop ``--recreate``; recover via a save.

The P10 dead-binding REFUSAL for edit/sync verbs held up under round 8. What did
NOT hold was the ``--recreate`` escape hatch (r8-M2/M3) and a case-alias
``save-deck`` (r8-M1). USER DECISION for P11:

- **Remove ``--recreate`` entirely.** It could never create on Airtable (retained
  ``airtable_record_id`` → update→422) and on a re-identified/dup source it silently
  rebound the row to a same-named STRANGER under a refused push. The flag is gone
  from ``push``/``sync`` (args + help).
- **Recovery becomes a normal fresh-identity save.** ``save-deck "X"`` on a SYNCED
  deck whose bound source is gone/re-identified creates a BRAND-NEW source at a fresh
  identity (strips the stale ``airtable_record_id`` so Airtable takes ``create_record``,
  treats the in-file uuid as absent so YAML mints a new file), binds the row to it,
  and recovers — never adopting a same-named stranger and never 422-ing.
- **The dead-binding message is backend-aware, honest, hallucination-free.** It names
  ONLY ``save-deck`` as recovery (NO "pull" — circular; NO "--recreate" — removed),
  and ends "Your local copy is intact."
- **r8-M1:** the dup-name refusal at the top of ``save_deck`` is alias-aware (a cased
  alias under dup names is refused, base file untouched).
- **Minors r8-m2..m6** as scoped.

Everything OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of record
and a REAL canonicalizing resolver hydrates cards (the hazard is exercised, never
stubbed). The Airtable findings run against a CONTRACT DOUBLE mirroring the CURRENT
adapter's create/update targeting rule (create stamps recordId; update of a deleted
record raises 422). ZERO prod writes; no network; no ``delete_record``.
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
from pipeline.decks.access import DeckAccess


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


def _cli(*argv: str) -> tuple[int, str, str]:
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
    return _cli


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _decks_dir(data_dir: Path) -> Path:
    return data_dir / 'collection' / 'decks'


def _yaml_files(data_dir: Path) -> list[str]:
    d = _decks_dir(data_dir)
    return sorted(p.name for p in d.glob('*.yaml')) if d.exists() else []


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


def _save_json(cli, data_dir: Path, payload: dict, *, confirm: bool = False) -> tuple[int, str, str]:
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / 'payload.json'
    p.write_text(json.dumps(payload))
    argv = ['save-deck', '--from-json', str(p)]
    if confirm:
        argv.append('--confirm')
    return cli(*argv)


def _expire(name: str | None = None) -> None:
    d = DecksStore()
    for row in d.list_rows():
        if name is None or row.name == name:
            d.set_freshness(row.deck_uuid, {})


def _source_store(data_dir: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=data_dir / 'collection')


class MockAirtableDecks:
    """Contract double mirroring the CURRENT adapter: create stamps recordId;
    update of a deleted record raises 422 (the r8f double, verbatim shape).
    """

    backend_name = 'airtable'

    def __init__(self) -> None:
        self.records: dict[str, Deck] = {}
        self._n = 0
        self.log: list[str] = []

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
        # ``force_fresh`` (r9-B1) is a no-op on Airtable (create/update is driven by
        # ``airtable_record_id``); accepted for current-adapter contract parity.
        if deck.airtable_record_id:
            if deck.airtable_record_id not in self.records:
                self.log.append(f'update_record({deck.airtable_record_id}) -> 422 DELETED')
                raise RuntimeError(f'422: record {deck.airtable_record_id!r} not found')
            self.log.append(f'update_record({deck.airtable_record_id}, name={deck.name!r})')
            self.records[deck.airtable_record_id] = deck.model_copy()
        else:
            rid = self._mint()
            self.log.append(f'create_record(-> {rid}, name={deck.name!r})')
            self.records[rid] = deck.model_copy(update={'airtable_record_id': rid})
            deck.airtable_record_id = rid

    def list_decks(self) -> list[Deck]:
        return list(self.records.values())


# --------------------------------------------------------------------------- #
# 1. --recreate is GONE (args + help).
# --------------------------------------------------------------------------- #


def test_recreate_flag_removed_from_push_and_sync(cli, data_dir):
    """``--recreate`` is not a valid flag on push/sync and is not mentioned in help."""
    _save_source(cli, data_dir, 'Flagless', filler=99, prefix='FCard')

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


# --------------------------------------------------------------------------- #
# Dead-binding message: backend-aware, names ONLY save-deck, no pull / no --recreate.
# --------------------------------------------------------------------------- #


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
    _save_source(cli, data_dir, 'Treasure', filler=99, prefix='TCard')
    assert cli('get-deck', 'Treasure')[0] == 0
    (_decks_dir(data_dir) / 'treasure.yaml').unlink()
    os.utime(_decks_dir(data_dir))

    for verb in (
        ('push', 'Treasure'),
        ('deck-add', 'Treasure', 'Phoenix Card'),
        ('set-strategy', 'Treasure', 'risen'),
    ):
        _expire('Treasure')
        code, _out, err = cli(*verb)
        assert code == 1, f'{verb} should refuse, got {code}: {err}'
        _assert_honest_message(err)
        assert 'deck file' in err.lower()  # backend-specific: local YAML wording
        assert _yaml_files(data_dir) == []  # nothing created / forked


def test_dead_binding_airtable_message_names_only_save_deck(data_dir, monkeypatch):
    """A deleted Airtable record → the write refuses with the honest Airtable message."""
    from pipeline.collection import resolver as resolver_mod
    from pipeline.decks.sync import DeadBindingError

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
# RECOVERY: save-deck on a dead binding creates a FRESH source, no stranger adopt.
# --------------------------------------------------------------------------- #


def test_recovery_savedeck_local_deleted_file_creates_fresh(cli, data_dir):
    """save-deck on a deleted local bound file recreates a fresh file and recovers."""
    _save_source(cli, data_dir, 'Treasure', filler=99, prefix='TCard')
    assert cli('get-deck', 'Treasure')[0] == 0
    (_decks_dir(data_dir) / 'treasure.yaml').unlink()
    os.utime(_decks_dir(data_dir))
    assert _yaml_files(data_dir) == []

    _expire('Treasure')
    # save-deck the same deck: recovery writes a FRESH file, exit 0.
    code, _out, err = _save_json(
        cli,
        data_dir,
        _commander_deck('Treasure', filler=99, prefix='TCard').model_dump(mode='json'),
    )
    assert code == 0, err
    files = _yaml_files(data_dir)
    assert files == ['treasure.yaml'], files  # exactly one, freshly minted

    _expire('Treasure')
    code, out, _err = cli('get-deck', 'Treasure')
    assert code == 0 and len(json.loads(out)['cards']) == 100

    # The recovered deck is now healthy: a subsequent edit lands (no dead binding).
    _expire('Treasure')
    assert cli('set-strategy', 'Treasure', 'recovered')[0] == 0


def test_recovery_savedeck_reuuid_file_creates_fresh_no_stranger_adopt(cli, data_dir):
    """save-deck when the bound file's uuid was hand-changed recovers at a fresh id.

    The re-identified file is a STRANGER (a foreign edit). Recovery must NOT adopt it
    — it mints a fresh file for this row, leaving the stranger's foreign content intact.
    """
    _save_source(cli, data_dir, 'Gruul', filler=99, prefix='GCard')
    assert cli('get-deck', 'Gruul')[0] == 0
    d = DecksStore()
    ext = next(r.external_ids for r in d.list_rows() if r.name == 'Gruul')
    bound = json.loads(ext)['local']

    gruul = _decks_dir(data_dir) / 'gruul.yaml'
    text = gruul.read_text().replace(f'uuid: {bound}', 'uuid: deadbeefdeadbeefdeadbeefdeadbeef')
    text = text.replace('- card: GCard 0', '- card: Foreign Addition\n- card: GCard 0')
    gruul.write_text(text)
    os.utime(_decks_dir(data_dir))
    _expire('Gruul')

    code, _out, err = _save_json(
        cli,
        data_dir,
        _commander_deck('Gruul', filler=99, prefix='GCard').model_dump(mode='json'),
    )
    assert code == 0, err
    # The stranger file's foreign content survives (never adopted / overwritten).
    files = _yaml_files(data_dir)
    assert 'gruul.yaml' in files
    assert 'Foreign Addition' in gruul.read_text()
    # A fresh disambiguated file was minted for this row (not the base slug stranger).
    fresh = [f for f in files if f.startswith('gruul') and f != 'gruul.yaml']
    assert len(fresh) == 1, files


def test_recovery_savedeck_airtable_deleted_record_creates_no_422(data_dir, monkeypatch):
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


def test_recovery_savedeck_airtable_does_not_adopt_stranger(data_dir, monkeypatch):
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
        name='Azula', format='Commander',
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
# r8-M1 — a CASE-ALIAS save-deck under dup names is REFUSED (base file untouched).
# --------------------------------------------------------------------------- #


def test_r8_m1_case_alias_savedeck_under_dup_names_refused(cli, data_dir):
    """save-deck 'PRECIOUS' with two 'Precious' rows → refused; precious.yaml intact."""
    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    precious = _decks_dir(data_dir) / 'precious.yaml'
    before = precious.read_text()

    payload = {
        'name': 'PRECIOUS',  # a CASE alias of the two 'Precious' rows
        'format': 'Commander',
        'cards': [{'name': 'Alias Cmdr', 'role': 'commander'}] + [{'name': f'A{i}'} for i in range(99)],
    }
    code, _out, err = _save_json(cli, data_dir, payload)
    assert code == 1, err
    assert 'is ambiguous' in err
    # The base-slug file was NOT overwritten with the alias content.
    assert precious.read_text() == before
    assert 'Alias Cmdr' not in precious.read_text()


# --------------------------------------------------------------------------- #
# r8-m4 — a drift-refused undo-deck rolls back the undo CURSOR (no skipped step).
# --------------------------------------------------------------------------- #


def test_r8_m4_drift_refused_undo_rolls_back_cursor(cli, data_dir):
    """A drift-refused undo does not burn an undo step: the next undo restores head-1."""
    _save_source(cli, data_dir, 'Cursor', filler=99, prefix='CCard')
    assert cli('get-deck', 'Cursor')[0] == 0
    # Build a ledger: v0 (baseline, no strategy) -> v1 (stratA) -> v2 (stratB).
    assert cli('set-strategy', 'Cursor', 'stratA')[0] == 0
    assert cli('set-strategy', 'Cursor', 'stratB')[0] == 0

    cfile = _decks_dir(data_dir) / 'cursor.yaml'
    healthy = cfile.read_text()  # the source in agreement with the local stratB deck.

    # A concurrent FOREIGN source edit makes the NEXT commit drift-refuse.
    cfile.write_text(healthy.replace('- card: CCard 0', '- card: Foreign C\n- card: CCard 0'))
    os.utime(_decks_dir(data_dir))

    # A drift-refused undo rolls content back to stratB AND must not burn the cursor.
    _expire('Cursor')
    assert cli('undo-deck', 'Cursor')[0] == 1
    # The local content rolled back to stratB (verify from the store, WITHOUT a read
    # that would re-pull the still-drifted source and interpose a spurious version).
    assert DecksStore().get(DecksStore().uuid_for_name('Cursor')).strategy == 'stratB'

    # Heal the drift out-of-band BEFORE any read re-pulls the foreign content: restore
    # the source to EXACTLY the healthy state so the next commit is clean and no re-pull
    # alters the local content or resets the cursor. This isolates the cursor-burn: if the
    # refused undo had burned a step, the next undo would skip stratA down to the baseline.
    cfile.write_text(healthy)
    os.utime(_decks_dir(data_dir))
    _expire('Cursor')

    code, _out, err = cli('undo-deck', 'Cursor')
    assert code == 0, err
    assert DecksStore().get(DecksStore().uuid_for_name('Cursor')).strategy == 'stratA', (
        'undo skipped a step — cursor was burned'
    )


# --------------------------------------------------------------------------- #
# r8-m5 — recover-decks refuses only for RELEVANT dup names, not any dup anywhere.
# --------------------------------------------------------------------------- #


def test_r8_m5_recover_decks_scopes_dup_refusal(cli, data_dir):
    """recover-decks of a uniquely-named deck is not blocked by an UNRELATED dup pair."""
    # A uniquely-named, under-target deck that IS recoverable.
    _save_source(cli, data_dir, 'Solo', filler=98, prefix='SCard')  # 99 cards, target ...
    assert cli('get-deck', 'Solo')[0] == 0
    # An UNRELATED dup pair elsewhere.
    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
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


# --------------------------------------------------------------------------- #
# r8-m6 — an --id-addressed edit verb under dup names commits by UUID (no ambiguity).
# --------------------------------------------------------------------------- #


def test_r8_m6_id_addressed_edit_commits_by_uuid_under_dup_names(cli, data_dir):
    """deck-add --id <prefix> under dup names commits by the resolved uuid, exit 0.

    The ``deck-*`` family commits through run.py's ``_commit_deck_edit`` — which
    resolved by canonical NAME, so under dup names the commit re-hit the ambiguity
    refusal even though the edit was addressed by ``--id``. The fix commits by the
    resolved uuid for ``--id`` calls (keeping the name wall for name-addressed ones).
    """
    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
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
    _expire('Precious')
    code, _out, err = cli('deck-add', '--id', target[:8], 'Bolt Card')
    assert code == 0, f'--id deck-add under dup names must commit by uuid: {err}'

    # A NAME-addressed deck-add under dup names STILL hits the dup-name wall (kept).
    _expire('Precious')
    code, _out, err = cli('deck-add', 'Precious', 'Wall Card')
    assert code == 1
    assert 'is ambiguous' in err


# --------------------------------------------------------------------------- #
# r8-m3 — first-save rollback under a failing driver write surfaces a clean error.
# --------------------------------------------------------------------------- #


def test_r8_m3_first_save_permission_error_is_clean(cli, data_dir):
    """A PermissionError from a failing driver write on a FIRST save-deck surfaces as a
    clean ``error:`` (exit 1), not a raw traceback (exit -1 in the harness); the
    just-created row is rolled back so no zombie lingers.
    """
    import stat

    # Seed the decks dir (an unrelated deck) so it exists, then make it read-only so
    # the NEXT save-deck's YAML write fails with a PermissionError.
    _save_source(cli, data_dir, 'Seed', filler=99, prefix='SeedCard')
    decks_dir = _decks_dir(data_dir)
    mode = decks_dir.stat().st_mode
    os.chmod(decks_dir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        code, _out, err = _save_json(
            cli,
            data_dir,
            {'name': 'Brandnew', 'format': 'Commander',
             'cards': [{'name': 'Krenko, Mob Boss', 'role': 'commander'}]},
        )
    finally:
        os.chmod(decks_dir, mode)

    # A clean error, NOT a raw traceback escaping the CLI.
    assert code == 1, f'expected clean exit 1, got {code}: {err}'
    assert 'Traceback' not in err, err
    # The just-created row was rolled back (no zombie for a later sync to land).
    assert DecksStore().uuid_for_name('Brandnew') is None


# --------------------------------------------------------------------------- #
# r8-m2 — the backfill must not name-adopt a local file for an airtable-bound row.
# --------------------------------------------------------------------------- #


def test_r8_m2_airtable_bound_row_not_adopted_by_local_name(data_dir, monkeypatch):
    """A row bound only {'airtable': rec} must not adopt a same-named local file
    under the local backend (r7-m4 honored on the write/backfill half too).
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

    ldir = _decks_dir(data_dir)
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / 'shared.yaml').write_text(
        'uuid: 1111aaaa1111aaaa1111aaaa1111aaaa\n'
        'name: Shared\ncards:\n- card: Local Only Card\n  role: commander\n'
    )
    before = (ldir / 'shared.yaml').read_text()

    local = _source_store(data_dir)
    local_access = DeckAccess(local, decks=decks)
    _expire('Shared')
    with pytest.raises(DecksError):
        local_access.read_deck('Shared')
    # The unrelated local file is untouched and the row was NOT rebound to it.
    assert (ldir / 'shared.yaml').read_text() == before
    ext = json.loads(decks.external_ids(bound_uuid) or '{}')
    assert 'local' not in ext, ext
