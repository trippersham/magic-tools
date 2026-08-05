"""Round-5 hardening regressions (Phase 6 P8) — finish the identity binding.

The spine (one root defect, four hats): the promote save-path must be stamped
with the SOURCE's own external identity (in-file uuid / recordId), NEVER the local
row PK, and every source object must carry that identity (the local YAML backfill).
``push`` already does this (``sync.py``); ``promote`` did not, and the §7 backfill
was never implemented. These regressions pin each Fable round-5 finding.

Everything is OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of
record and a REAL canonicalizing resolver (never a stub-to-identity) hydrates
cards — the hazard the old B3 bug hid behind is exercised, not mocked. The one
Airtable finding (F3) is proven against a CONTRACT DOUBLE that implements the
adapter's own ``update_record``/``create_record`` targeting rule verbatim under the
REAL Phase-6 store/sync code. ZERO prod writes; no network; no ``delete_record``.
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
from pipeline.decks.access import DeckAccess, deck_access
from pipeline.decks.sync import promote


class CanonicalizingResolver:
    """A REAL canonicalizing resolver (never stubbed to identity — the hazard)."""

    _CANON: ClassVar[dict[str, str]] = {
        'krenko, mob boss': 'Krenko, Mob Boss',
        'grumgully, the generous': 'Grumgully, the Generous',
        'zada, hedron grinder': 'Zada, Hedron Grinder',
        'mountain': 'Mountain',
        'sol ring': 'Sol Ring',
        'impact tremors': 'Impact Tremors',
    }

    def _canonical(self, name: str) -> str:
        key = ' '.join(name.split()).casefold()
        return self._CANON.get(key, ' '.join(name.split()))

    def get_card(self, name: str) -> Card | None:
        canonical = self._canonical(name)
        return Card(name=canonical, oracle_id=f'oid-{canonical}', mana_cost='{1}{R}')


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated tmp data root; force the local backend; no live Airtable creds."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    return root


def _source_store(tmp_path: Path) -> LocalYamlStore:
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=tmp_path / 'collection')


def _commander_deck(name: str, *, commander: str = 'krenko, mob boss', filler: int = 89) -> Deck:
    """A 100-card Commander deck authored with NON-canonical names."""
    cards = [DeckCard(name=commander, quantity=1, role='commander'), DeckCard(name='mountain', quantity=10)]
    for i in range(filler):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    return Deck(name=name, format='commander', strategy='go wide', cards=cards)


# --------------------------------------------------------------------------- #
# In-process CLI harness (mirrors the fable repro pattern) — real verbs.
# --------------------------------------------------------------------------- #


def _cli(*argv: str) -> tuple[int, str, str]:
    """Run one collection CLI verb in-process; return (exit_code, stdout, stderr)."""
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
    """Wire the CLI's card resolver to the REAL canonicalizing resolver, then hand
    back the ``_cli`` runner. The local backend reads ``MAKE_MAGIC_DATA_DIR``."""
    from pipeline.collection import resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, 'default_card_resolver', lambda: CanonicalizingResolver())
    return _cli


def _decks_dir(data_dir: Path) -> Path:
    return data_dir / 'collection' / 'decks'


def _yaml_files(data_dir: Path) -> list[str]:
    d = _decks_dir(data_dir)
    return sorted(p.name for p in d.glob('*.yaml')) if d.exists() else []


def _write_source_yaml(
    data_dir: Path, slug: str, name: str, cards: Sequence[tuple[str, str | None]], *, uuid: str | None = None
) -> Path:
    """Hand-author a source YAML the way a pre-P6 store / a human would (optionally legacy)."""
    d = _decks_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if uuid:
        lines.append(f'uuid: {uuid}')
    lines += [f'name: {name}', 'format: Commander', 'cards:']
    for n, role in cards:
        lines.append(f'- card: "{n}"' + (f'\n  role: {role}' if role else ''))
    p = d / f'{slug}.yaml'
    p.write_text('\n'.join(lines) + '\n')
    return p


# --------------------------------------------------------------------------- #
# F1 — legacy promote refused/disambiguated + the backfill injects uuids
# --------------------------------------------------------------------------- #


def test_f1_backfill_injects_uuids_into_legacy_files(cli, data_dir: Path) -> None:
    """The §7 backfill walks collection/decks/*.yaml and injects uuid + header (additive)."""
    legacy = _write_source_yaml(
        data_dir,
        'legacy-precious',
        'Legacy Precious',
        [('Krenko, Mob Boss', 'commander')] + [(f'LCard {i}', None) for i in range(99)],
    )
    assert 'uuid:' not in legacy.read_text()  # a genuine legacy file (no in-file uuid)

    # Any deck verb that constructs the local store triggers the one-time backfill.
    code, _out, _err = cli('list-decks')
    assert code == 0

    import yaml

    text = legacy.read_text()
    assert 'uuid:' in text  # a uuid was injected
    data = yaml.safe_load(text)
    assert isinstance(data.get('uuid'), str) and data['uuid']
    assert 'permanent ID' in text  # the protective comment header
    # Additive only: the content is preserved (still 100 cards, commander intact).
    assert 'Krenko, Mob Boss' in text


def test_f1_backfill_is_idempotent(cli, data_dir: Path) -> None:
    """A file that already carries a uuid is left byte-identical by the backfill."""
    keeper = _write_source_yaml(
        data_dir,
        'keeper',
        'Keeper',
        [('Krenko, Mob Boss', 'commander')] + [(f'KCard {i}', None) for i in range(99)],
        uuid='ffffffffffffffffffffffffffffffff',
    )
    before = keeper.read_text()
    cli('list-decks')
    cli('list-decks')
    assert keeper.read_text() == before


def test_f1_legacy_clean_slate_promote_does_not_clobber(cli, data_dir: Path) -> None:
    """clean-slate promote --to a (formerly-legacy) deck must NOT silently replace it.

    After the backfill the target carries a uuid, so the slug-collision guard
    disambiguates; even absent that, the shrink ceremony now compares against the
    file's current contents regardless of uuid match — a gutting write is refused.
    """
    legacy = _write_source_yaml(
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


def test_f1_legacy_save_defensive_shrink_guard(data_dir: Path, tmp_path: Path) -> None:
    """A gutting write onto a legacy (no-uuid) file is refused by the shrink ceremony.

    The prior-size comparison runs against the file's CURRENT contents regardless of
    uuid match, so a no-uuid file is never a free upgrade target for a different deck.
    """
    from pipeline.collection.store import CollectionError

    driver = _source_store(tmp_path)
    # Hand-author a legacy (no-uuid) 100-card file.
    d = tmp_path / 'collection' / 'decks'
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
# F2 — migrated-store exploration promote LANDS on the parent file (no fork)
# --------------------------------------------------------------------------- #


def test_f2_exploration_promote_lands_on_parent_when_pk_differs(data_dir: Path, tmp_path: Path) -> None:
    """Exploration promote stamps the PARENT's in-file uuid, not the local row PK.

    Simulate the migrated steady state: the local row PK differs from the parent
    file's in-file uuid. Promote must land the edit on the parent FILE (no second
    ``<slug>-<hex>.yaml`` fork).
    """
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Synced'), allow_shrink=False)
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

    files_before = sorted(p.name for p in (tmp_path / 'collection' / 'decks').glob('*.yaml'))
    promote(decks, driver, deck_uuid=draft_uuid)
    files_after = sorted(p.name for p in (tmp_path / 'collection' / 'decks').glob('*.yaml'))

    assert files_after == files_before, f'promote FORKED the source deck: {files_after}'
    # The parent FILE received the edit.
    parent_source = driver.get_deck('Synced')
    names = {c.name for c in parent_source.cards}
    assert 'Impact Tremors' in names
    assert 'Goblin 0' not in names


# --------------------------------------------------------------------------- #
# F3 — Airtable exploration promote -> update_record on parent recordId (no dup)
# --------------------------------------------------------------------------- #


class MockAirtableDecks:
    """The Decks slice of the Airtable adapter's contract (a CONTRACT DOUBLE).

    Implements the adapter's exact save-targeting rule verbatim
    (airtable_collection.py:1184-1187): ``update_record`` when the deck carries a
    recordId, else ``create_record``. NO network — this is a double, not a stub of
    the hazard; the store/sync code under test is the REAL Phase-6 code.
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
            self.log.append(f'update_record({deck.airtable_record_id})')
            self.records[deck.airtable_record_id] = deck
        else:
            rid = self._mint()
            self.log.append(f'create_record(->{rid})')
            self.records[rid] = deck.model_copy(update={'airtable_record_id': rid})

    def list_decks(self) -> list[Deck]:
        return list(self.records.values())


def test_f3_airtable_exploration_promote_updates_parent_record(data_dir: Path) -> None:
    """Exploration promote takes the update_record branch on the parent's recordId.

    NO duplicate Decks row is created; the parent record receives the edit; the
    user reads the improvement back. Proven against the adapter's contract double.
    """
    decks = DecksStore()
    driver = MockAirtableDecks()
    access = DeckAccess(driver, decks=decks)  # type: ignore[arg-type]

    cards = [DeckCard(name='Krenko, Mob Boss', role='commander')] + [DeckCard(name=f'Card {i}') for i in range(99)]
    driver.save_deck(Deck(name='Ozai', format='Commander', cards=cards))
    driver.log.clear()

    parent = access.read_deck('Ozai')
    parent_uuid = access.resolve('Ozai')
    draft = parent.model_copy(update={'name': 'Ozai (explore)', 'airtable_record_id': None, 'uuid': uuid4().hex})
    draft_uuid = decks.create_ephemeral(draft, derived_from=parent_uuid)
    decks.swap(draft_uuid, add=DeckCard(name='Improvement'), cut='Card 0')

    promote(decks, driver, deck_uuid=draft_uuid)  # type: ignore[arg-type]

    # Exactly one Decks row named 'Ozai' — no junk duplicate created.
    ozai_rows = [r for r, d in driver.records.items() if d.name == 'Ozai']
    assert ozai_rows == ['rec00001'], f'duplicate Decks row created: {driver.records}'
    assert any('update_record(rec00001)' in line for line in driver.log)
    assert not any('create_record' in line for line in driver.log)
    # The parent record received the improvement; the user reads it back.
    assert any(c.name == 'Improvement' for c in driver.records['rec00001'].cards)
    seen = access.read_deck('Ozai')
    assert any(c.name == 'Improvement' for c in seen.cards)


# --------------------------------------------------------------------------- #
# F4 — post-promote refresh by uuid, not name (no rebind to an unrelated deck)
# --------------------------------------------------------------------------- #


def test_f4_promote_to_never_pulled_name_stays_addressable(cli, data_dir: Path) -> None:
    """clean-slate promote --to the name of a never-pulled existing deck: the promoted
    row is addressable and subsequent edits land on IT, not the unrelated pre-existing
    deck (no name-addressed force-pull rebind)."""
    original = _write_source_yaml(
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
    promoted_files = list((_decks_dir(data_dir)).glob('precious-*.yaml'))
    assert promoted_files and 'Sol Ring' in promoted_files[0].read_text()


# --------------------------------------------------------------------------- #
# F5 — promote refuses a non-ephemeral (synced) row
# --------------------------------------------------------------------------- #


def test_f5_promote_refuses_synced_deck(cli, data_dir: Path) -> None:
    """Promoting a synced deck's name is refused; the source deck is NOT renamed."""
    src = _write_source_yaml(
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
# F6 — dup-name promote refuses with candidates + --id escape hatch
# --------------------------------------------------------------------------- #


def test_f6_dup_name_promote_refuses_with_candidates(cli, data_dir: Path) -> None:
    """Two same-named drafts: promote by name refuses with a candidate list (no silent oldest)."""
    cli('new-draft', 'Scratch', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('new-draft', 'Scratch', '--commander', 'Zada, Hedron Grinder', '--format', 'Commander')

    code, _out, err = cli('promote-deck', 'Scratch', '--to', 'Landed')
    assert code != 0
    assert 'ambiguous' in err.lower()
    assert '--id' in err


def test_f6_promote_id_flag_disambiguates(cli, data_dir: Path) -> None:
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
    landed = (_decks_dir(data_dir) / 'landed.yaml')
    assert 'Zada' in landed.read_text()


# --------------------------------------------------------------------------- #
# F7 — new-draft refuses a source-name collision without --force (USER DECISION)
# --------------------------------------------------------------------------- #


def test_f7_new_draft_refuses_source_name_collision(cli, data_dir: Path) -> None:
    """new-draft named after an existing SOURCE deck is refused without --force."""
    _write_source_yaml(
        data_dir,
        'gruul',
        'Gruul',
        [('Zada, Hedron Grinder', 'commander')] + [(f'GCard {i}', None) for i in range(99)],
        uuid='cccccccccccccccccccccccccccccccc',
    )
    code, _out, err = cli('new-draft', 'Gruul', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    assert code != 0
    assert '--force' in err or '--from' in err


def test_f7_new_draft_force_allows_same_named_draft(cli, data_dir: Path) -> None:
    """--force lets a same-named draft be created deliberately."""
    _write_source_yaml(
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


# --------------------------------------------------------------------------- #
# F8 — re-pull preserves the assessment stamp (merge_freshness, not set)
# --------------------------------------------------------------------------- #


def test_f8_re_pull_preserves_assessment_stamp(data_dir: Path, tmp_path: Path) -> None:
    """A plain re-pull must NOT wipe the assessment freshness stamp (merge, not clobber)."""
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(_commander_deck('Prov'), allow_shrink=False)
    access.read_deck('Prov')
    deck_uuid = access.resolve('Prov')
    access.set_assessment('Prov', 'looks strong')
    assert decks.assessment_state(deck_uuid) == 'fresh'

    # Force a re-pull (bind by ref); the assessment stamp must survive.
    access.pull('Prov')
    assert decks.assessment_state(deck_uuid) == 'fresh', 're-pull wiped the assessment stamp'


# --------------------------------------------------------------------------- #
# F9 — alias-addressed WRITE lands on the bound row, exit 0 (the M4-write regression)
# --------------------------------------------------------------------------- #


def test_f9_alias_write_lands_on_bound_row(cli, data_dir: Path) -> None:
    """A case-alias WRITE (deck-add / set-strategy) lands on the ONE bound row, exit 0."""
    _write_source_yaml(
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
    src = (_decks_dir(data_dir) / 'alias-deck.yaml').read_text()
    assert 'Sol Ring' in src, 'aliased deck-add did not land on the bound row'

    code, _out, err = cli('set-strategy', 'alias deck', 'aggro')
    assert code == 0, err
    assert 'strategy: aggro' in (_decks_dir(data_dir) / 'alias-deck.yaml').read_text()


def test_f9_alias_write_direct_access(data_dir: Path, tmp_path: Path) -> None:
    """Direct DeckAccess: an alias write binds to the same uuid read_deck returns."""
    driver = _source_store(tmp_path)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)
    driver.save_deck(_commander_deck('Alias Deck'), allow_shrink=False)

    bound = access.read_deck('Alias Deck')
    canonical_uuid = access.resolve('Alias Deck')
    # A cased alias write must target `canonical_uuid`, not mint a fresh hex.
    access.set_strategy('alias deck', 'aggro plan')
    assert bound is not None
    assert decks.get(canonical_uuid).strategy == 'aggro plan'  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# F10 — new-draft --commander canonicalizes (no duplicate singleton entries)
# --------------------------------------------------------------------------- #


def test_f10_new_draft_commander_is_canonicalized(cli, data_dir: Path) -> None:
    """new-draft --commander runs boundary canonicalization (M3's hole moved one verb over)."""
    code, _out, err = cli('new-draft', 'Brew', '--commander', 'krenko, mob boss', '--format', 'Commander')
    assert code == 0, err
    decks = DecksStore()
    uuid = decks.uuid_for_name('Brew')
    assert uuid is not None
    deck = decks.get(uuid)
    assert deck is not None
    commanders = [c.name for c in deck.cards if c.role == 'commander']
    assert commanders == ['Krenko, Mob Boss'], f'commander not canonicalized: {commanders}'


# --------------------------------------------------------------------------- #
# F11 — duplicate deck FILE (same in-file uuid) is refused with a clear error
# --------------------------------------------------------------------------- #


def test_f11_duplicate_file_uuid_refused(tmp_path: Path) -> None:
    """>1 file carrying the same in-file uuid is refused with a clear duplicate error."""
    from pipeline.collection.store import CollectionError

    driver = _source_store(tmp_path)
    d = tmp_path / 'collection' / 'decks'
    d.mkdir(parents=True, exist_ok=True)
    dup_uuid = 'abcabcabcabcabcabcabcabcabcabcab'
    body = f'uuid: {dup_uuid}\nname: Solo\nformat: Commander\ncards:\n- card: "Sol Ring"\n'
    (d / 'a-backup-of-solo.yaml').write_text(body)
    (d / 'solo.yaml').write_text(body)

    with pytest.raises(CollectionError) as exc:
        driver.get_deck_by_uuid(dup_uuid)
    msg = str(exc.value)
    assert 'a-backup-of-solo.yaml' in msg and 'solo.yaml' in msg


# --------------------------------------------------------------------------- #
# F12 — migration writes external_ids key 'local' (matches the binder)
# --------------------------------------------------------------------------- #

_OLD_DECKS_DDL = (
    'CREATE TABLE decks ('
    'deck_id TEXT PRIMARY KEY, name TEXT, deck_json TEXT, sync_status TEXT, '
    'source_ref TEXT, synced_baseline TEXT, freshness TEXT, last_sim TEXT, '
    'archived BOOLEAN DEFAULT FALSE)'
)


def test_f12_migration_writes_local_key(data_dir: Path) -> None:
    """Migration binds a non-airtable row under external_ids['local'] (the binder's key)."""
    deck = _commander_deck('Draft Brew')
    with store.connect() as conn:
        conn.execute(_OLD_DECKS_DDL)
        conn.execute(
            'CREATE TABLE deck_versions '
            '(seq BIGINT, ts TIMESTAMP, deck_id TEXT, version TEXT, rationale TEXT, deck_json JSON)'
        )
        conn.execute(
            'INSERT INTO decks (deck_id, name, deck_json, sync_status, source_ref, archived) '
            'VALUES (?, ?, ?, ?, ?, FALSE)',
            ['local:Draft Brew', deck.name, deck.model_dump_json(), 'ephemeral', None],
        )

    s = DecksStore()
    row = next(r for r in s.list_rows(include_archived=True) if r.name == 'Draft Brew')
    raw = s.external_ids(row.deck_uuid)
    assert raw is not None
    ext = json.loads(raw)
    assert 'local' in ext, f"expected key 'local', got {ext}"
    assert 'local_yaml' not in ext


# --------------------------------------------------------------------------- #
# F13 — error hygiene: a name-miss is a clean "no deck named" error
# --------------------------------------------------------------------------- #


def test_f13_unknown_name_clean_error(cli, data_dir: Path) -> None:
    """A name-miss at a verb requiring an existing deck is a clean error, no minted uuid/path."""
    code, _out, err = cli('archive-deck', 'No Such Deck')
    assert code != 0
    assert 'no deck' in err.lower()
    # No leaked minted uuid (32 hex) and no filesystem path.
    import re

    assert not re.search(r'\b[0-9a-f]{32}\b', err), f'leaked a minted uuid: {err}'
    assert '/collection/decks/' not in err


# --------------------------------------------------------------------------- #
# F14 — migration rebuild is crash-atomic (wrapped in a transaction)
# --------------------------------------------------------------------------- #


def test_f14_migration_is_transactional(data_dir: Path) -> None:
    """The migrated store's rebuild leaves a well-formed decks table (never a lost table)."""
    deck = _commander_deck('Brew')
    with store.connect() as conn:
        conn.execute(_OLD_DECKS_DDL)
        conn.execute(
            'CREATE TABLE deck_versions '
            '(seq BIGINT, ts TIMESTAMP, deck_id TEXT, version TEXT, rationale TEXT, deck_json JSON)'
        )
        conn.execute(
            'INSERT INTO decks (deck_id, name, deck_json, sync_status, source_ref, archived) '
            'VALUES (?, ?, ?, ?, ?, FALSE)',
            ['local:Brew', deck.name, deck.model_dump_json(), 'ephemeral', None],
        )

    s = DecksStore()
    rows = s.list_rows(include_archived=True)
    assert len(rows) == 1  # the row survived the rebuild
    # A second open is a clean no-op (the table was renamed atomically, not dropped).
    s2 = DecksStore()
    assert len(s2.list_rows(include_archived=True)) == 1
