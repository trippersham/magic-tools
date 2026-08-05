"""Behavioral regressions for draft visibility, ephemeral factsheets, and identity.

- ``factsheet`` runs on an EPHEMERAL draft (which has no source of record): a fresh
  ``new-draft`` -> ``factsheet`` through the CLI exits 0 with valid factsheet JSON,
  routed through the local decks store, so ASSESS can run on a draft.
- A ``new-draft`` reusing a previously-ARCHIVED name is born VISIBLE. Identity is
  name-independent, so the new draft mints its own row (``archived=FALSE``) and
  shows in the default ``list-decks`` rather than re-keying onto the archived row.
- Cross-machine identity: two INDEPENDENT local stores (separate DuckDB files) over
  ONE shared YAML collection agree on ``deck_uuid`` — the source's in-file uuid
  binds the same identity on both "machines".

OFFLINE. The first two drive the CLI over a tmp ``MAKE_MAGIC_DATA_DIR`` + local
backend + stub resolver. The cross-machine test uses a REAL canonicalizing resolver
over a shared ``LocalYamlStore`` — the canonicalization hazard is NOT stubbed away.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import store
from pipeline.collection import run as cli
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore
from pipeline.decks.access import DeckAccess


class _StubResolver:
    def get_card(self, name: str) -> Card | None:
        return None


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _StubResolver())
    return root


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


# --------------------------------------------------------------------------- #
# factsheet runs on an ephemeral draft
# --------------------------------------------------------------------------- #


def test_b4_factsheet_on_ephemeral_draft(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``factsheet`` on a fresh draft exits 0 with valid JSON — no FileNotFoundError.

    An ephemeral draft has no source of record; factsheet routes through the local
    decks store so ASSESS can run on a draft the way the guided build needs.
    """
    # A clean-slate ephemeral draft — NO source of record exists for it.
    _run(monkeypatch, 'new-draft', 'Scratch Brew', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    capsys.readouterr()

    # factsheet on the DRAFT must succeed (exit 0, not FileNotFoundError).
    _run(monkeypatch, 'factsheet', 'Scratch Brew')
    out = capsys.readouterr().out
    report = json.loads(out)  # valid JSON, not a traceback

    # A real factsheet keyed to the draft (name flows through the local store read).
    assert isinstance(report, dict)
    assert report.get('deck') == 'Scratch Brew'


# --------------------------------------------------------------------------- #
# a new-draft reusing a previously-archived name is born VISIBLE
# --------------------------------------------------------------------------- #


def test_new_draft_under_archived_name_is_born_visible(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Create 'Scratch', archive it, new-draft 'Scratch' again -> the NEW draft shows.

    Identity is name-independent: the second ``new-draft`` mints its OWN row rather
    than re-keying onto the archived one, so it is born ``archived=FALSE`` and is
    visible in the default ``list-decks`` (a draft that reuses an archived name is
    not silently hidden).
    """
    _run(monkeypatch, 'new-draft', 'Scratch', '--format', 'Commander')
    capsys.readouterr()
    _run(monkeypatch, 'archive-deck', 'Scratch')
    capsys.readouterr()

    # After archiving, the default list no longer shows 'Scratch'.
    _run(monkeypatch, 'list-decks')
    assert 'Scratch' not in capsys.readouterr().out

    # A NEW draft reusing the archived name — a distinct identity.
    _run(monkeypatch, 'new-draft', 'Scratch', '--format', 'Commander')
    capsys.readouterr()

    # The new draft is BORN VISIBLE in the default (non-archived) listing.
    _run(monkeypatch, 'list-decks', '--json')
    rows = json.loads(capsys.readouterr().out)
    scratch = [r for r in rows if r['name'] == 'Scratch']
    assert scratch, 'the new Scratch draft must appear in the default list-decks'
    assert all(r['archived'] is False for r in scratch)  # its own row is not archived

    # Two distinct rows now exist for the name (the archived original + the new one);
    # the archived one only surfaces WITH --archived.
    decks = DecksStore()
    all_rows = [r for r in decks.list_rows(include_archived=True) if r.name == 'Scratch']
    assert len(all_rows) == 2
    assert {r.archived for r in all_rows} == {True, False}
    assert len({r.deck_uuid for r in all_rows}) == 2  # name-independent identities


# --------------------------------------------------------------------------- #
# cross-machine — two independent stores over one shared YAML bind one identity
# --------------------------------------------------------------------------- #


class _CanonicalizingResolver:
    """A REAL canonicalizing resolver (never stubbed to identity).

    Hydrating a source card rewrites its NAME to the canonical form — the transform
    under which the in-file uuid binding must stay stable across two machines.
    """

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


def _commander_deck(name: str) -> Deck:
    cards = [DeckCard(name='krenko, mob boss', quantity=1, role='commander'), DeckCard(name='mountain', quantity=10)]
    for i in range(89):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    return Deck(name=name, format='commander', strategy='go wide', cards=cards)


def test_cross_machine_shared_yaml_binds_one_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two independent local stores over ONE shared YAML collection agree on identity.

    Machine A saves a source deck (writing the in-file uuid). Machine B — a fresh
    ``DecksStore`` (its OWN empty DuckDB) + ``DeckAccess`` over the SAME collection
    root — reads the deck and binds to the SAME ``deck_uuid`` (the in-file uuid).
    Identity travels with the shared YAML, not the local store.
    """
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)

    shared_collection = tmp_path / 'shared-collection'
    driver = LocalYamlStore(resolver=_CanonicalizingResolver(), collection_root=shared_collection)

    # Machine A: its own DuckDB; save a source deck (writes the in-file uuid).
    decks_a = DecksStore(db_path=tmp_path / 'machine_a.duckdb')
    access_a = DeckAccess(driver, decks=decks_a)
    access_a.save_deck(_commander_deck('Shared Krenko'), allow_shrink=False)
    uuid_a = decks_a.uuid_for_name('Shared Krenko')
    assert uuid_a is not None

    # The in-file uuid the source YAML carries is the authoritative identity.
    source = driver.get_deck('Shared Krenko')
    assert uuid_a == source.uuid

    # Machine B: a fresh, EMPTY DuckDB over the SAME shared YAML collection.
    decks_b = DecksStore(db_path=tmp_path / 'machine_b.duckdb')
    assert decks_b.uuid_for_name('Shared Krenko') is None  # nothing local yet
    access_b = DeckAccess(driver, decks=decks_b)

    read_b = access_b.read_deck('Shared Krenko')
    uuid_b = decks_b.uuid_for_name('Shared Krenko')

    # Both machines bound the SAME deck_uuid — the shared in-file uuid.
    assert uuid_b == uuid_a == source.uuid == read_b.uuid
    # Machine B bound by the source's external ref (local in-file uuid), one row.
    row_b = decks_b.get_row(uuid_b)  # type: ignore[arg-type]
    assert row_b is not None
    ext = json.loads(row_b.external_ids or '{}')
    assert ext.get('local') == source.uuid
