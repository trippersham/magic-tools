"""Round-6 hardening regressions (Phase 6 P9) — ONE source-read chokepoint.

The round-6 spine is a single class of defect: a source-of-record read went by
NAME instead of by the row's bound external ref (in-file uuid for ``local``,
recordId for ``airtable``). P8 taught ``push`` to read bound; P9 collapses ALL
source reads through ONE chokepoint (``read_bound_source``) so reading a source
by NAME becomes impossible to write outside its own controlled first-pull
fallback. These regressions pin each Fable round-6 finding to that structural fix:

- **r6-B1:** ``--id`` / explicit ``pull`` / TTL-expiry reads serve the BOUND deck,
  not the base-slug deck.
- **r6-B2:** promote onto a RENAMED parent file still fires the drift guard (bound
  read), preserving a concurrent foreign edit.
- **r6-B3:** a dropped-in backup YAML must not rebind a LIVE row.
- **r6-B4** (contract): an Airtable dup-name clean-slate promote binds + updates
  the NEW record, never the unrelated original.
- **r6-M1:** a case-alias write under dup names is refused with the candidate list.
- **r6-M2** (contract): a correctly-bound Airtable row pushes with NO spurious drift.
- **ghost-uuid:** a ``uuid: null`` file gets a REAL minted uuid on backfill.

Everything OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of record
and a REAL canonicalizing resolver hydrates cards (the hazard is exercised, never
stubbed to identity). The Airtable findings are proven against a CONTRACT DOUBLE
that implements the adapter's OWN ``update_record`` / ``create_record`` targeting
rule verbatim under the REAL P9 store/sync/access code. ZERO prod writes; no
network; no ``delete_record``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore
from pipeline.decks.access import DeckAccess
from pipeline.decks.sync import SyncDriftError, promote, push, read_bound_source
from pipeline.decks.version import version


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


def _decks_dir(data_dir: Path) -> Path:
    return data_dir / 'collection' / 'decks'


def _yaml_files(data_dir: Path) -> list[str]:
    d = _decks_dir(data_dir)
    return sorted(p.name for p in d.glob('*.yaml')) if d.exists() else []


def _file_uuid(path: Path) -> str | None:
    for line in path.read_text().splitlines():
        if line.startswith('uuid: '):
            return line.split('uuid: ', 1)[1].strip() or None
    return None


def _require_file_uuid(path: Path) -> str:
    """The file's in-file uuid — asserted present (post-backfill/save)."""
    uuid = _file_uuid(path)
    assert uuid is not None, f'{path.name} carries no in-file uuid'
    return uuid


def _source_store(data_dir: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=data_dir / 'collection')


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


def _synced_rows(data_dir: Path) -> list[tuple[str, str, str | None]]:
    d = DecksStore()
    return [(r.deck_uuid, r.name, r.external_ids) for r in d.list_rows() if r.sync_status == 'synced']


def _write_legacy_yaml(
    data_dir: Path, slug: str, name: str, cards: Sequence[str], *, uuid: str | None = None, prefix: str = 'L'
) -> Path:
    d = _decks_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if uuid is not None:
        lines.append(f'uuid: {uuid}')
    lines += [f'name: {name}', 'format: Commander', 'cards:', '- card: "Krenko, Mob Boss"', '  role: commander']
    lines += [f'- card: "{c}"' for c in cards]
    p = d / f'{slug}.yaml'
    p.write_text('\n'.join(lines) + '\n')
    return p


# --------------------------------------------------------------------------- #
# Airtable contract double — the adapter's OWN targeting rule, verbatim.
# --------------------------------------------------------------------------- #


class MockAirtableDecks:
    """A contract double implementing the adapter's create/update targeting rule.

    ``save_deck`` on a deck carrying ``airtable_record_id`` UPDATES that record;
    otherwise it CREATES a fresh one — and, mirroring the P9 adapter fix, STAMPS
    the new recordId back onto the passed ``deck`` instance so a caller can reread
    it by record id (never by name). ``get_deck`` = the name read = ``rows[0]``.
    ZERO network; nothing here writes upstream.
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
            self.log.append(f'update_record({deck.airtable_record_id}, name={deck.name!r})')
            self.records[deck.airtable_record_id] = deck
        else:
            rid = self._mint()
            self.log.append(f'create_record(-> {rid}, name={deck.name!r})')
            self.records[rid] = deck.model_copy(update={'airtable_record_id': rid})
            # P9: surface the created recordId to the caller (the adapter stamps it
            # in place so ``_reread_source`` rereads THIS record, not a name sibling).
            deck.airtable_record_id = rid

    def list_decks(self) -> list[Deck]:
        return list(self.records.values())


# --------------------------------------------------------------------------- #
# r6-B1 — the --id / pull / TTL read serves the BOUND deck, not the base slug.
# --------------------------------------------------------------------------- #


def test_r6_b1_id_read_serves_the_bound_deck_not_the_base_slug(cli, data_dir: Path) -> None:
    """After a dup-name clean-slate promote, a ``--id`` read of the promoted row
    must serve ITS OWN (2-card) content, never the unrelated 100-card base slug."""
    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    cli('get-deck', 'Precious')  # pull -> row A bound to precious.yaml
    cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('deck-add', 'JunkB', 'Junk Card 1')
    code, _out, err = cli('promote-deck', 'JunkB', '--to', 'Precious')
    assert code == 0, err

    base_uuid = _require_file_uuid(_decks_dir(data_dir) / 'precious.yaml')
    promoted = None
    for deck_uuid, name, ext in _synced_rows(data_dir):
        if name != 'Precious':
            continue
        bound = json.loads(ext or '{}').get('local')
        if bound != base_uuid:
            promoted = deck_uuid
    assert promoted is not None, 'expected a promoted Precious row bound to its own file'

    # --id read (freshness unset after promote -> would pull) must serve 2 cards.
    code, out, _err = cli('get-deck', '--id', promoted[:8])
    assert code == 0
    served = json.loads(out)
    assert sum(c.get('quantity', 1) for c in served['cards']) == 2

    # An explicit pull of the promoted row also stays on its own file.
    cli('pull', '--id', promoted[:8])
    code, out, _err = cli('get-deck', '--id', promoted[:8])
    assert sum(c.get('quantity', 1) for c in json.loads(out)['cards']) == 2

    # The base-slug row + file are untouched (still 100 cards).
    orig = _source_store(data_dir).get_deck_by_uuid(base_uuid)
    assert sum(c.quantity for c in orig.cards) == 100


def test_r6_b1_ttl_expiry_read_stays_on_the_bound_file(cli, data_dir: Path) -> None:
    """A TTL-expiry re-pull (freshness cleared) reads the row's BOUND file, not the slug."""
    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    cli('get-deck', 'Precious')
    cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('deck-add', 'JunkB', 'Junk Card 1')
    cli('promote-deck', 'JunkB', '--to', 'Precious')

    base_uuid = _file_uuid(_decks_dir(data_dir) / 'precious.yaml')
    decks = DecksStore()
    promoted = next(
        u for u, n, e in _synced_rows(data_dir) if n == 'Precious' and json.loads(e or '{}').get('local') != base_uuid
    )
    # Force TTL expiry: clear freshness so the very next read re-pulls.
    decks.set_freshness(promoted, {})
    code, out, _err = cli('get-deck', '--id', promoted[:8])
    assert code == 0
    assert sum(c.get('quantity', 1) for c in json.loads(out)['cards']) == 2


# --------------------------------------------------------------------------- #
# r6-B2 — promote onto a RENAMED parent file fires the drift guard.
# --------------------------------------------------------------------------- #


def test_r6_b2_promote_on_renamed_parent_fires_drift_guard(cli, data_dir: Path) -> None:
    """Renaming the parent FILE is supported; a foreign edit to it must NOT be
    silently destroyed by an exploration promote — the bound read sees the drift."""
    _save_source(cli, data_dir, 'Gruul', filler=99, prefix='GCard')
    cli('get-deck', 'Gruul')
    cli('new-draft', 'Explore', '--from', 'Gruul')
    cli('deck-swap', 'Explore', '--cut', 'GCard 0', '--add', 'Improvement Card')

    # The user renames the deck FILE (explicitly supported); a foreign writer edits it.
    decks_dir = _decks_dir(data_dir)
    (decks_dir / 'gruul.yaml').rename(decks_dir / 'krenko-tribal.yaml')
    p = decks_dir / 'krenko-tribal.yaml'
    p.write_text(p.read_text() + '- card: Foreign Addition\n')

    # Promote must refuse (drift) — the bound read finds the renamed file's foreign edit.
    code, _out, _err = cli('promote-deck', 'Explore')
    assert code != 0, 'promote onto a drifted (renamed) parent must refuse, not clobber'
    assert 'Foreign Addition' in p.read_text(), 'the concurrent foreign edit must survive'
    assert 'Improvement Card' not in p.read_text()


def test_r6_b2_promote_unit_bound_read_preserves_foreign_edit(data_dir: Path) -> None:
    """Unit: a renamed parent file with a foreign edit -> promote raises SyncDriftError."""
    src = _source_store(data_dir)
    parent = _commander_deck('Gruul', filler=99, prefix='GCard')
    src.save_deck(parent)
    file_uuid = _require_file_uuid(_decks_dir(data_dir) / 'gruul.yaml')

    decks = DecksStore()
    p_uuid = uuid4().hex
    stored = src.get_deck_by_uuid(file_uuid).model_copy(update={'uuid': p_uuid})
    decks.put(stored, deck_uuid=p_uuid, sync_status='synced', source_ref='Gruul',
              synced_baseline=version(stored), rationale='pull')
    decks.set_external_id(p_uuid, 'local', file_uuid)

    draft = stored.model_copy(update={'name': 'Explore', 'uuid': uuid4().hex})
    d_uuid = decks.create_ephemeral(draft, derived_from=p_uuid)
    decks.swap(d_uuid, add=DeckCard(name='Improvement'), cut='GCard 0')

    # Rename the file + a foreign writer appends a card (drift).
    decks_dir = _decks_dir(data_dir)
    (decks_dir / 'gruul.yaml').rename(decks_dir / 'renamed.yaml')
    ren = decks_dir / 'renamed.yaml'
    ren.write_text(ren.read_text() + '- card: Foreign Addition\n')

    with pytest.raises(SyncDriftError):
        promote(decks, src, deck_uuid=d_uuid)
    assert 'Foreign Addition' in ren.read_text()


# --------------------------------------------------------------------------- #
# r6-B3 — a dropped-in backup YAML must not rebind a LIVE row.
# --------------------------------------------------------------------------- #


def test_r6_b3_backup_yaml_does_not_rebind_a_live_row(cli, data_dir: Path) -> None:
    """Dropping a same-named legacy backup into decks/ must NOT hijack the live row:
    its external_ids['local'] stays on the real file; reads/writes stay on it."""
    _save_source(cli, data_dir, 'Treasure', filler=99, prefix='TCard')
    cli('get-deck', 'Treasure')  # row bound to treasure.yaml's uuid
    before = json.loads(_synced_rows(data_dir)[0][2] or '{}')['local']

    # The naive-user move: drop an OLD pre-P6 backup of the deck into the dir.
    _write_legacy_yaml(data_dir, 'z-old-backup-of-treasure', 'Treasure', [f'StaleCard {i}' for i in range(20)])
    cli('list-decks')  # any verb triggers the backfill

    after = json.loads(_synced_rows(data_dir)[0][2] or '{}')['local']
    assert after == before, 'the live binding must not move to the backup file'

    # The row still serves the live 100-card deck, and an edit lands in the real file.
    cli('pull', 'Treasure')
    code, out, _err = cli('get-deck', 'Treasure')
    assert code == 0
    assert sum(c.get('quantity', 1) for c in json.loads(out)['cards']) == 100
    cli('deck-add', 'Treasure', 'New Idea')
    real = (_decks_dir(data_dir) / 'treasure.yaml').read_text()
    backup = (_decks_dir(data_dir) / 'z-old-backup-of-treasure.yaml').read_text()
    assert 'New Idea' in real
    assert 'New Idea' not in backup


def test_r6_b3_backfill_binds_genuinely_unbound_legacy_files(cli, data_dir: Path) -> None:
    """A first-pull of a never-bound legacy file DOES bind (the fallback still works)."""
    _write_legacy_yaml(data_dir, 'legacy-fresh', 'Legacy Fresh', [f'LCard {i}' for i in range(30)])
    code, _out, _err = cli('get-deck', 'Legacy Fresh')
    assert code == 0
    rows = _synced_rows(data_dir)
    bound = next(json.loads(e or '{}').get('local') for u, n, e in rows if n == 'Legacy Fresh')
    assert bound == _file_uuid(_decks_dir(data_dir) / 'legacy-fresh.yaml')


# --------------------------------------------------------------------------- #
# r6-B4 (contract) — Airtable dup-name promote binds + updates the NEW record.
# --------------------------------------------------------------------------- #


def test_r6_b4_airtable_dup_name_promote_binds_and_updates_new_record(data_dir: Path) -> None:
    """A clean-slate promote --to an EXISTING Airtable name must bind the promoted
    row to the NEW record and later update THAT record, never the unrelated original."""
    decks = DecksStore()
    driver = MockAirtableDecks()
    access = DeckAccess(driver, decks=decks)  # type: ignore[arg-type]
    driver.save_deck(_commander_deck('Ozai', filler=99))  # rec00001

    access.read_deck('Ozai')  # row A -> rec00001
    draft = Deck(
        name='Junk', format='Commander',
        cards=[DeckCard(name='Grumgully, the Generous', role='commander'), DeckCard(name='Junk 1')],
    )
    d_uuid = decks.create_ephemeral(draft)
    promote(decks, driver, deck_uuid=d_uuid, to_name='Ozai')  # type: ignore[arg-type]

    # The promoted row is bound to the NEW record (rec00002), not rec00001.
    prow = decks.get_row(d_uuid)
    assert prow is not None
    ext = json.loads(prow.external_ids or '{}')
    assert ext.get('airtable') == 'rec00002'
    pdeck = decks.get(d_uuid)
    assert pdeck is not None
    assert sum(c.quantity for c in pdeck.cards) == 2

    # One ordinary edit + push must update rec00002, never touch rec00001.
    decks.add_card(d_uuid, DeckCard(name='My New Card'), rationale='user edit')
    driver.log.clear()
    push(decks, driver, deck_uuid=d_uuid)  # type: ignore[arg-type]
    assert any('rec00002' in c for c in driver.log)
    assert all('rec00001' not in c for c in driver.log)
    assert any(c.name == 'My New Card' for c in driver.records['rec00002'].cards)
    assert not any(c.name == 'My New Card' for c in driver.records['rec00001'].cards)


# --------------------------------------------------------------------------- #
# r6-M1 — a case-alias write under dup names is refused with the candidates.
# --------------------------------------------------------------------------- #


def test_r6_m1_case_alias_write_under_dup_names_refuses_with_candidates(cli, data_dir: Path) -> None:
    """With two live rows named Precious, a cased-alias write must refuse (candidate
    list), NOT silently pick the base-slug deck."""
    _save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    cli('get-deck', 'Precious')
    cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('promote-deck', 'JunkB', '--to', 'Precious')

    code, _out, err = cli('deck-add', 'Precious', 'Probe A')  # exact name refuses (control)
    assert code != 0 and '--id' in err

    code, _out, err = cli('set-strategy', 'pReCiOuS', 'alias strategy')  # cased alias
    assert code != 0, 'a cased-alias write under dup names must refuse'
    assert '--id' in err
    # No file received the sneaky strategy.
    for p in _decks_dir(data_dir).glob('*.yaml'):
        assert 'alias strategy' not in p.read_text()


# --------------------------------------------------------------------------- #
# r6-M2 (contract) — a correctly-bound Airtable row pushes without spurious drift.
# --------------------------------------------------------------------------- #


def test_r6_m2_correctly_bound_airtable_row_pushes_without_spurious_drift(data_dir: Path) -> None:
    """A row correctly bound to rec00002 must push to rec00002, never spuriously
    drift-refuse by comparing against a same-named rec00001."""
    decks = DecksStore()
    driver = MockAirtableDecks()
    driver.save_deck(_commander_deck('Azula', filler=49))  # rec00001 unrelated dup
    driver.save_deck(_commander_deck('Azula', filler=99))  # rec00002 the deck

    p_uuid = uuid4().hex
    src = driver.get_deck_by_record_id('rec00002')
    decks.put(src.model_copy(update={'uuid': p_uuid}), deck_uuid=p_uuid, sync_status='synced',
              source_ref='Azula', synced_baseline=version(src), rationale='pull')
    decks.set_external_id(p_uuid, 'airtable', 'rec00002')
    decks.add_card(p_uuid, DeckCard(name='My Edit'), rationale='edit')

    driver.log.clear()
    push(decks, driver, deck_uuid=p_uuid)  # type: ignore[arg-type]  # must NOT raise
    assert any('rec00002' in c for c in driver.log)
    assert any(c.name == 'My Edit' for c in driver.records['rec00002'].cards)
    assert not any(c.name == 'My Edit' for c in driver.records['rec00001'].cards)


# --------------------------------------------------------------------------- #
# ghost-uuid — a uuid: null file gets a REAL minted uuid on backfill.
# --------------------------------------------------------------------------- #


def test_ghost_uuid_null_file_gets_real_minted_uuid(cli, data_dir: Path) -> None:
    """A legacy file with ``uuid:`` (null) must receive a REAL minted uuid — the
    falsy value must not override the mint, and no row may bind to a ghost uuid."""
    p = _decks_dir(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    nully = p / 'nully.yaml'
    nully.write_text('uuid:\nname: Nully\nformat: Commander\ncards:\n- card: "Krenko, Mob Boss"\n  role: commander\n')

    cli('list-decks')  # triggers backfill
    injected = _file_uuid(nully)
    assert injected, 'a real uuid must be injected (not left null)'

    cli('list-decks')  # idempotent: same uuid, not re-minted
    assert _file_uuid(nully) == injected

    # Any bound row points at a uuid that ACTUALLY exists in a file.
    cli('get-deck', 'Nully')
    rows = [r for r in _synced_rows(data_dir) if r[1] == 'Nully']
    assert rows
    bound = json.loads(rows[0][2] or '{}').get('local')
    assert bound == injected
    on_disk = _source_store(data_dir).find_deck_path_by_uuid(bound)
    assert on_disk is not None, 'the bound uuid must exist in a real file (no ghost binding)'


# --------------------------------------------------------------------------- #
# r6-m6 — set-* verbs roll back the local edit when the push is refused.
# --------------------------------------------------------------------------- #


def test_r6_m6_set_strategy_rolls_back_on_push_refusal(cli, data_dir: Path) -> None:
    """A push-refused ``set-strategy`` (source drifted) must NOT leave the local edit
    half-applied — the deck's strategy stays what it was before the failed commit."""
    _save_source(cli, data_dir, 'Rollback', filler=99, prefix='RCard')
    cli('get-deck', 'Rollback')  # pull -> local row

    # A foreign writer moves the source (drift) so the commit push will refuse.
    src = _source_store(data_dir)
    moved = src.get_deck('Rollback')
    moved.cards.append(DeckCard(name='Foreign Card'))
    src.save_deck(moved, allow_shrink=True)

    code, _out, err = cli('set-strategy', 'Rollback', 'new strategy text')
    assert code != 0, 'a drifted commit must refuse'
    assert 'moved' in err or 'drift' in err.lower()
    # The local edit was rolled back — strategy is NOT the refused text.
    code, out, _err = cli('get-deck', 'Rollback', '--field', 'strategy')
    assert 'new strategy text' not in out


# --------------------------------------------------------------------------- #
# The chokepoint itself — direct unit coverage of read_bound_source.
# --------------------------------------------------------------------------- #


def test_read_source_bound_returns_none_when_bound_ref_is_gone(data_dir: Path) -> None:
    """A bound ref that resolves to nothing (deleted file) returns None — never a
    name-read of a different object, never a crash."""
    src = _source_store(data_dir)
    src.save_deck(_commander_deck('Ghost', filler=99))
    file_uuid = _require_file_uuid(_decks_dir(data_dir) / 'ghost.yaml')
    decks = DecksStore()
    g_uuid = uuid4().hex
    stored = src.get_deck_by_uuid(file_uuid).model_copy(update={'uuid': g_uuid})
    decks.put(stored, deck_uuid=g_uuid, sync_status='synced', source_ref='Ghost',
              synced_baseline=version(stored), rationale='pull')
    decks.set_external_id(g_uuid, 'local', file_uuid)

    # Delete the file the row is bound to; another same-named file appears.
    (_decks_dir(data_dir) / 'ghost.yaml').unlink()
    _write_legacy_yaml(data_dir, 'ghost-decoy', 'Ghost', [f'Decoy {i}' for i in range(5)])

    got = read_bound_source(decks, src, deck_uuid=g_uuid, source_ref='Ghost')
    assert got is None, 'a dead bound ref returns None, never the same-named decoy'


def test_read_source_bound_expected_ref_reads_that_ref(data_dir: Path) -> None:
    """``expected_ref`` reads strictly by that ref (reread-after-create), never by name."""
    src = _source_store(data_dir)
    src.save_deck(_commander_deck('Base', filler=99))
    base_uuid = _require_file_uuid(_decks_dir(data_dir) / 'base.yaml')
    # A second same-named file with its own uuid.
    _write_legacy_yaml(data_dir, 'base-two', 'Base', [f'Two {i}' for i in range(5)])
    cli_backfill = LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=data_dir / 'collection')
    cli_backfill.backfill_deck_uuids()
    two_uuid = _require_file_uuid(_decks_dir(data_dir) / 'base-two.yaml')

    decks = DecksStore()
    b_uuid = uuid4().hex
    decks.put(_commander_deck('Base').model_copy(update={'uuid': b_uuid}), deck_uuid=b_uuid,
              sync_status='synced', source_ref='Base', synced_baseline='x', rationale='pull')
    decks.set_external_id(b_uuid, 'local', base_uuid)

    got = read_bound_source(decks, src, deck_uuid=b_uuid, source_ref='Base', expected_ref=two_uuid)
    assert got is not None
    assert any(c.name.startswith('Two') for c in got.cards), 'expected_ref must win over the bound ref + name'
