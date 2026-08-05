"""Round-7 hardening regressions (Phase 6 P10) — a DEAD binding is a REFUSAL.

P9 collapsed all source READS through one chokepoint (``read_source_bound``): a
dead bound ref returns None ("source gone") and every read surfaces an error. P10
is the WRITE-side mirror: a write against a row that IS bound for the active
backend but whose bound source reads back None must REFUSE (never fall through the
``current_source is None`` guard-skip and silently create / adopt an existing slug
file / fork). A genuine FIRST push (no bound ref at all) still creates — unchanged.

These regressions pin each Fable round-7 finding to that one semantic plus the two
belts around it (the re-keyed backfill marker; save-deck's refusal-before-put):

- **r7-B1:** an in-place legacy restore (``cp backup.yaml decks/vault.yaml`` — no
  dir-mtime bump) invalidates the re-keyed marker so the backfill heals the row,
  and a subsequent write does NOT destroy the restored file.
- **r7-M2:** a deleted bound file / a re-uuid'd bound file → every write REFUSES
  (no silent recreate, no fork; a foreign edit in the re-uuid'd file survives).
  ``--recreate`` is the explicit opt-in override.
- **r7-M1:** a dup-name ``save-deck`` is refused BEFORE any put, so its content is
  never staged into a row for a later ``sync`` to land.
- **r7-m2:** ``pull`` under dup names refuses with the candidate list.
- **r7-m3:** a drift-refused ``undo-deck`` rolls back (no half-applied undo).
- **r7-m4:** an airtable-bound row read under the local backend does not silently
  clobber a same-named local file.
- **r7-m5:** ``recover-decks`` refuses duplicate deck names.

Everything OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of record
and a REAL canonicalizing resolver hydrates cards (the hazard is exercised, never
stubbed to identity). The Airtable findings run against a CONTRACT DOUBLE that
mirrors the CURRENT (P9) adapter's create/update targeting rule. ZERO prod writes;
no network; no ``delete_record``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore
from pipeline.decks.access import DeckAccess
from pipeline.decks.sync import DeadBindingError, binding_is_dead, push


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


def _expire(name: str | None = None) -> None:
    """Clear the W4 pull TTL so the next read re-consults the source (bound read)."""
    d = DecksStore()
    for row in d.list_rows():
        if name is None or row.name == name:
            d.set_freshness(row.deck_uuid, {})


def _synced_row(name: str) -> tuple[str, str | None]:
    """Return ``(deck_uuid, external_ids)`` for the single synced row named ``name``."""
    d = DecksStore()
    rows = [r for r in d.list_rows() if r.name == name and r.sync_status == 'synced']
    assert len(rows) == 1, f'expected 1 synced row named {name!r}, got {len(rows)}'
    row = rows[0]
    return row.deck_uuid, row.external_ids


def _source_store(data_dir: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=data_dir / 'collection')


# --------------------------------------------------------------------------- #
# r7-B1 — in-place legacy restore: re-keyed marker heals; the write does NOT destroy.
# --------------------------------------------------------------------------- #


def test_r7_b1_inplace_legacy_restore_marker_rekeyed_and_write_does_not_destroy(cli, data_dir):
    """An in-place overwrite (no dir-mtime bump) invalidates the re-keyed marker.

    The July restore persona overwrites ``decks/vault.yaml`` content with a legacy
    (no-uuid) backup. The OLD marker keyed on the DIR mtime stayed "clean" (an
    in-place write does not bump it), so the backfill never healed the row and the
    next write destroyed the restored file. The re-keyed marker (max file mtime +
    count) bumps on the in-place overwrite, so the backfill mints a uuid + rebinds
    the row on the next read, and the subsequent write lands on the RESTORED file
    without gutting it.
    """
    _save_source(cli, data_dir, 'Vault', filler=99, prefix='VCard')
    assert cli('get-deck', 'Vault')[0] == 0
    assert cli('list-decks')[0] == 0  # a clean backfill pass drops the marker
    marker = _decks_dir(data_dir) / '.uuid-backfill-clean'
    assert marker.exists()

    vault = _decks_dir(data_dir) / 'vault.yaml'
    dir_mtime_before = _decks_dir(data_dir).stat().st_mtime_ns
    legacy = 'name: Vault\ncards:\n- card: Old Vault Card\n  role: commander\n- card: VCard 1\n'
    with open(vault, 'w') as fh:  # cp semantics — truncate + write IN PLACE
        fh.write(legacy)
    dir_mtime_after = _decks_dir(data_dir).stat().st_mtime_ns

    # The in-place overwrite does NOT bump the DIR mtime (that was the r7-B1 hole)...
    assert dir_mtime_before == dir_mtime_after
    # ...but the re-keyed marker (per-file max mtime + count) IS now invalid.
    assert marker.read_text().strip() != LocalYamlStore._decks_dir_files_key(_decks_dir(data_dir))

    _expire('Vault')
    # The backfill heals the restored legacy file on read (mints a uuid, rebinds).
    code, out, err = cli('get-deck', 'Vault')
    assert code == 0, err
    served = json.loads(out)
    assert any(c['name'] == 'Old Vault Card' for c in served['cards'])
    assert 'uuid:' in vault.read_text()  # the backfill minted one into the file

    # The subsequent write lands on the RESTORED file and does NOT gut it to the
    # 100-card local cache (the r7-B1 destruction). The restored content survives.
    _expire('Vault')
    assert cli('set-strategy', 'Vault', 'overwrite probe')[0] == 0
    text = vault.read_text()
    assert 'Old Vault Card' in text
    assert text.count('- card:') == 2  # restored backup had 2 cards, not the 100-card cache


# --------------------------------------------------------------------------- #
# r7-M2 — dead binding: deleted file / re-uuid'd file → write REFUSES.
# --------------------------------------------------------------------------- #


def test_r7_m2a_deleted_bound_file_write_refuses_no_silent_recreate(cli, data_dir):
    """A synced row whose bound FILE was deleted refuses every write (no resurrect)."""
    _save_source(cli, data_dir, 'Treasure', filler=99, prefix='TCard')
    assert cli('get-deck', 'Treasure')[0] == 0
    (_decks_dir(data_dir) / 'treasure.yaml').unlink()
    os.utime(_decks_dir(data_dir))  # bump so the backfill re-runs (file HAS a uuid → dead ref)

    for verb in (
        ('sync', 'Treasure'),
        ('push', 'Treasure'),
        ('deck-add', 'Treasure', 'Phoenix Card'),
        ('set-strategy', 'Treasure', 'risen'),
    ):
        _expire('Treasure')
        code, _out, err = cli(*verb)
        assert code == 1, f'{verb} should refuse a dead binding, got exit {code}'
        assert 'gone or was re-identified' in err
        # The source was NOT silently recreated.
        assert _yaml_files(data_dir) == []


def test_r7_m2b_reuuid_bound_file_write_refuses_no_fork_foreign_edit_survives(cli, data_dir):
    """A row whose bound file's in-file uuid was hand-edited refuses writes; no fork."""
    _save_source(cli, data_dir, 'Gruul', filler=99, prefix='GCard')
    assert cli('get-deck', 'Gruul')[0] == 0
    _uuid, ext = _synced_row('Gruul')
    assert ext is not None
    bound = json.loads(ext)['local']

    gruul = _decks_dir(data_dir) / 'gruul.yaml'
    text = gruul.read_text().replace(f'uuid: {bound}', 'uuid: deadbeefdeadbeefdeadbeefdeadbeef')
    text = text.replace('- card: GCard 0', '- card: Foreign Addition\n- card: GCard 0')
    gruul.write_text(text)
    os.utime(_decks_dir(data_dir))
    _expire('Gruul')

    code, _out, err = cli('push', 'Gruul')
    assert code == 1 and 'gone or was re-identified' in err
    # No disambiguated FORK file (gruul-xxxx.yaml) was created; the foreign edit lives on.
    assert _yaml_files(data_dir) == ['gruul.yaml']
    assert 'Foreign Addition' in gruul.read_text()

    _expire('Gruul')
    code, _out, err = cli('deck-add', 'Gruul', 'X Card')
    assert code == 1 and 'gone or was re-identified' in err
    assert _yaml_files(data_dir) == ['gruul.yaml']
    assert 'Foreign Addition' in gruul.read_text()


def test_r7_m2_recreate_override_recreates_deleted_source(cli, data_dir):
    """``--recreate`` is the explicit opt-in: it recreates a deleted source, no refusal."""
    _save_source(cli, data_dir, 'Recr', filler=99, prefix='RCard')
    assert cli('get-deck', 'Recr')[0] == 0
    (_decks_dir(data_dir) / 'recr.yaml').unlink()
    os.utime(_decks_dir(data_dir))

    _expire('Recr')
    assert cli('sync', 'Recr')[0] == 1  # refuses without the override
    assert _yaml_files(data_dir) == []

    _expire('Recr')
    code, _out, err = cli('sync', 'Recr', '--recreate')
    assert code == 0, err
    assert _yaml_files(data_dir) == ['recr.yaml']
    _expire('Recr')
    code, out, _err = cli('get-deck', 'Recr')
    assert code == 0 and len(json.loads(out)['cards']) == 100


# --------------------------------------------------------------------------- #
# r7-M1 — dup-name save-deck refused BEFORE any put; later sync does not land it.
# --------------------------------------------------------------------------- #


def test_r7_m1_dupname_savedeck_refused_content_not_staged(cli, data_dir):
    """A refused dup-name save-deck stages NOTHING; a later sync of the oldest row is clean."""
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
    assert 'Sneaky Overwrite' not in (_decks_dir(data_dir) / 'precious.yaml').read_text()


# --------------------------------------------------------------------------- #
# r7-m2 — pull under dup names refuses with the candidate list.
# --------------------------------------------------------------------------- #


def test_r7_m2_pull_under_dup_names_refuses(cli, data_dir):
    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    _expire('Precious')
    code, _out, err = cli('pull', 'Precious')
    assert code == 1
    assert 'is ambiguous' in err
    assert '--id' in err


# --------------------------------------------------------------------------- #
# r7-m3 — a drift-refused undo-deck rolls back (no half-applied undo).
# --------------------------------------------------------------------------- #


def test_r7_m3_drift_refused_undo_rolls_back(cli, data_dir):
    _save_source(cli, data_dir, 'R', filler=99, prefix='RCard')
    assert cli('get-deck', 'R')[0] == 0
    assert cli('set-strategy', 'R', 'good strategy')[0] == 0

    # A concurrent FOREIGN edit on the source file makes the next commit drift-refuse.
    rfile = _decks_dir(data_dir) / 'r.yaml'
    rfile.write_text(rfile.read_text().replace('- card: RCard 0', '- card: Foreign R\n- card: RCard 0'))
    os.utime(_decks_dir(data_dir))

    # A drift-refused set-strategy rolls back to 'good strategy'.
    assert cli('set-strategy', 'R', 'poison')[0] == 1
    code, out, err = cli('get-deck', 'R')
    assert code == 0, err
    assert json.loads(out)['strategy'] == 'good strategy'

    # A drift-refused undo-deck ALSO rolls back — the undo does not half-apply.
    assert cli('undo-deck', 'R')[0] == 1
    code, out, err = cli('get-deck', 'R')
    assert code == 0, err
    assert json.loads(out)['strategy'] == 'good strategy'
    assert 'Foreign R' in rfile.read_text()  # the foreign source edit is preserved


# --------------------------------------------------------------------------- #
# r7-m4 — an airtable-bound row under the local backend does not clobber a local file.
# --------------------------------------------------------------------------- #


class MockAirtableDecks:
    """Contract double mirroring the CURRENT (P9) adapter's create/update rule."""

    backend_name = 'airtable'

    def __init__(self) -> None:
        self.records: dict[str, Deck] = {}
        self._n = 0

    def _mint(self) -> str:
        self._n += 1
        return f'rec{self._n:05d}'

    def get_deck(self, name: str) -> Deck:
        for rid, d in self.records.items():
            if d.name == name:
                return d.model_copy(update={'airtable_record_id': rid})
        raise FileNotFoundError(f'No Airtable Decks record named {name!r}.')

    def get_deck_by_record_id(self, record_id: str) -> Deck:
        if record_id in self.records:
            return self.records[record_id].model_copy(update={'airtable_record_id': record_id})
        raise FileNotFoundError(f'No Decks record {record_id!r}.')

    def save_deck(self, deck: Deck, *, allow_shrink: bool = False) -> None:
        if deck.airtable_record_id:
            self.records[deck.airtable_record_id] = deck.model_copy()
        else:
            rid = self._mint()
            self.records[rid] = deck.model_copy(update={'airtable_record_id': rid})
            deck.airtable_record_id = rid

    def list_decks(self) -> list[Deck]:
        return list(self.records.values())


def test_r7_m4_airtable_bound_row_under_local_does_not_clobber_local_file(data_dir, monkeypatch):
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

    # Under the LOCAL backend the airtable-bound row refuses to read (its source is on
    # another backend) rather than adopt the unrelated local file.
    from pipeline.decks.store import DecksError

    with pytest.raises(DecksError):
        local_access.read_deck('Shared')

    # The unrelated local file is untouched; the row still holds the airtable content.
    assert (ldir / 'shared.yaml').read_text() == before
    row_deck = decks.get(bound_uuid)
    assert row_deck is not None and len(row_deck.cards) == 100


# --------------------------------------------------------------------------- #
# r7-m5 — recover-decks refuses duplicate deck names.
# --------------------------------------------------------------------------- #


def test_r7_m5_recover_decks_refuses_dup_names(cli, data_dir):
    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    assert cli('get-deck', 'Precious')[0] == 0
    assert cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')[0] == 0
    assert cli('deck-add', 'JunkB', 'Junk Card 1')[0] == 0
    assert cli('promote-deck', 'JunkB', '--to', 'Precious')[0] == 0

    code, _out, err = cli('recover-decks')
    assert code == 1
    assert 'duplicate deck name' in err
    assert 'Precious' in err


# --------------------------------------------------------------------------- #
# Unit: binding_is_dead / guard_write_binding split never-created from ref-dead.
# --------------------------------------------------------------------------- #


def test_binding_is_dead_splits_never_created_from_ref_dead(data_dir, monkeypatch):
    """The write-side split: no bound ref → NOT dead (first push); bound + gone → dead."""
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())

    decks = DecksStore()
    driver = _source_store(data_dir)

    # A never-created deck (a fresh ephemeral-then-promote clean slate): no bound ref.
    d_uuid = uuid4().hex
    deck = _commander_deck('Fresh', filler=2, prefix='F').model_copy(update={'uuid': d_uuid})
    decks.put(deck, deck_uuid=d_uuid, sync_status='synced', source_ref='Fresh', synced_baseline=None)
    assert binding_is_dead(decks, driver, deck_uuid=d_uuid, source_ref='Fresh') is False  # never-created
    # A first push (no bound ref) CREATES — the guard must not refuse it.
    push(decks, driver, deck_uuid=d_uuid)
    assert 'fresh.yaml' in _yaml_files(data_dir)

    # Now bind + delete the source: the ref is DEAD → the guard refuses.
    _uuid, ext = _synced_row('Fresh')
    assert ext and 'local' in json.loads(ext)
    (_decks_dir(data_dir) / 'fresh.yaml').unlink()
    assert binding_is_dead(decks, driver, deck_uuid=d_uuid, source_ref='Fresh') is True
    with pytest.raises(DeadBindingError):
        push(decks, driver, deck_uuid=d_uuid)
