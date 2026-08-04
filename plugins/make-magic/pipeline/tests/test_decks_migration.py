"""TDD tests for the P1 store-identity migration (deck_id -> deck_uuid).

The redesign re-keys the local decks store from the name-derived
``deck_id = '<backend>:<name>'`` to a globally-unique ``deck_uuid`` PK. A
POPULATED old-shape store (rows carrying the old ``deck_id``, one with an
``airtable_record_id`` and one without, plus ``deck_versions`` ledger history)
must migrate LOSSLESSLY and IDEMPOTENTLY on the next ``DecksStore`` open:

    - every row gains a ``deck_uuid`` (the old ``deck_id`` column is gone);
    - ``external_ids`` is populated ``{"airtable": <rec>}`` when the deck held an
      Airtable record id, else ``{"local_yaml": <deck_uuid>}``;
    - the ``deck_versions`` ledger is re-keyed from the old ``deck_id`` to the new
      ``deck_uuid`` so undo still reaches the pre-migration history;
    - a SECOND open is a no-op (no re-mint, no double-rebuild);
    - the uuid addition does NOT change ``version(deck)``.

Everything is LOCAL-ONLY: an isolated tmp data root via ``MAKE_MAGIC_DATA_DIR``;
zero Airtable network calls (the migration reads the record id from the EXISTING
LOCAL row, never from Airtable).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection import history
from pipeline.contracts import Deck, DeckCard
from pipeline.decks import DecksStore, version


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at an isolated tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    return root


def _deck(name: str, *, record_id: str | None = None, strategy: str = 'v1') -> Deck:
    cards = [DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'), DeckCard(name='Mountain', quantity=10)]
    for i in range(89):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    return Deck(name=name, format='commander', strategy=strategy, cards=cards, airtable_record_id=record_id)


# The OLD-shape decks table (the shape before this migration): keyed on
# ``deck_id TEXT PRIMARY KEY``, NO ``deck_uuid`` / ``external_ids`` / ``derived_from``.
_OLD_DECKS_DDL = (
    'CREATE TABLE decks ('
    'deck_id TEXT PRIMARY KEY, name TEXT, deck_json TEXT, sync_status TEXT, '
    'source_ref TEXT, synced_baseline TEXT, freshness TEXT, last_sim TEXT, '
    'archived BOOLEAN DEFAULT FALSE)'
)


def _seed_old_store(deck_a: Deck, deck_b: Deck) -> tuple[str, str]:
    """Build a POPULATED old-shape store (two rows + ledger history) via raw SQL.

    Returns the two old ``deck_id`` keys. ``deck_a`` carries an Airtable record id
    (so migration should bind ``external_ids['airtable']``); ``deck_b`` does not
    (so it binds ``external_ids['local_yaml']``). Each deck gets a two-version
    ledger history so we can prove undo still reaches the pre-migration version.
    """
    id_a = f'airtable:{deck_a.name}'
    id_b = f'local:{deck_b.name}'
    with store.connect() as conn:
        conn.execute(_OLD_DECKS_DDL)
        # The ledger tables use the shared history DDL (already deck_id-keyed here).
        conn.execute(
            'CREATE TABLE deck_versions ('
            'seq BIGINT, ts TIMESTAMP, deck_id TEXT, version TEXT, rationale TEXT, deck_json JSON)'
        )
        seq = 0
        for old_id, deck, status, source_ref in (
            (id_a, deck_a, 'synced', deck_a.name),
            (id_b, deck_b, 'ephemeral', None),
        ):
            conn.execute(
                'INSERT INTO decks (deck_id, name, deck_json, sync_status, source_ref, archived) '
                'VALUES (?, ?, ?, ?, ?, FALSE)',
                [old_id, deck.name, deck.model_dump_json(), status, source_ref],
            )
            # Two ledger rows per deck: an original (v0) then the current (v1), so
            # the newest is the head and the prior is the undo target.
            v0 = deck.model_copy(update={'strategy': 'v0'})
            for d in (v0, deck):
                conn.execute(
                    'INSERT INTO deck_versions (seq, ts, deck_id, version, rationale, deck_json) '
                    "VALUES (?, now(), ?, ?, 'seed', ?)",
                    [seq, old_id, version(d), d.model_dump_json()],
                )
                seq += 1
    return id_a, id_b


def test_populated_old_store_migrates_losslessly(data_dir: Path) -> None:
    """A populated old-shape store gains deck_uuid + external_ids without data loss."""
    deck_a = _deck('Krenko', record_id='recABC123')
    deck_b = _deck('Draft Brew')
    _seed_old_store(deck_a, deck_b)

    # Opening the store triggers the one-time migration.
    s = DecksStore()

    rows = s.list_rows(include_archived=True)
    assert len(rows) == 2
    by_name = {r.name: r for r in rows}

    # Every row now has a non-empty deck_uuid (the old deck_id column is gone).
    for r in rows:
        assert r.deck_uuid
        assert ':' not in r.deck_uuid  # not the old '<backend>:<name>' form

    # The decks themselves round-trip unchanged (lossless content).
    got_a = s.get(by_name['Krenko'].deck_uuid)
    got_b = s.get(by_name['Draft Brew'].deck_uuid)
    assert got_a is not None and got_b is not None
    assert version(got_a) == version(deck_a)
    assert version(got_b) == version(deck_b)


def test_external_ids_bound_from_local_row_not_airtable(data_dir: Path) -> None:
    """external_ids: airtable-record-id row -> {"airtable": rec}; else local_yaml."""
    deck_a = _deck('Krenko', record_id='recABC123')
    deck_b = _deck('Draft Brew')
    _seed_old_store(deck_a, deck_b)

    s = DecksStore()
    by_name = {r.name: r for r in s.list_rows(include_archived=True)}

    ext_a = json.loads(s.external_ids(by_name['Krenko'].deck_uuid))
    assert ext_a == {'airtable': 'recABC123'}

    ext_b = json.loads(s.external_ids(by_name['Draft Brew'].deck_uuid))
    assert ext_b == {'local_yaml': by_name['Draft Brew'].deck_uuid}


def test_ledger_history_reachable_under_new_uuid(data_dir: Path) -> None:
    """The deck_versions ledger is re-keyed so undo reaches the pre-migration history."""
    deck_a = _deck('Krenko', record_id='recABC123')
    deck_b = _deck('Draft Brew')
    id_a, _id_b = _seed_old_store(deck_a, deck_b)

    s = DecksStore()
    uuid_a = next(r.deck_uuid for r in s.list_rows(include_archived=True) if r.name == 'Krenko')

    # The OLD deck_id must no longer key any ledger rows (fully re-keyed).
    with store.connect() as conn:
        assert history.deck_version_rows(conn, id_a) == []
        rekeyed = history.deck_version_rows(conn, uuid_a)
    assert len(rekeyed) == 2  # the two seeded versions, now under the uuid

    # Undo restores the pre-migration prior version (v0), proving the history is live.
    restored = s.undo(uuid_a)
    assert restored is not None
    assert restored.strategy == 'v0'


def test_second_open_is_a_noop(data_dir: Path) -> None:
    """A second DecksStore open must not re-mint uuids or double-rebuild (idempotent)."""
    deck_a = _deck('Krenko', record_id='recABC123')
    deck_b = _deck('Draft Brew')
    _seed_old_store(deck_a, deck_b)

    first = DecksStore()
    uuids_first = sorted(r.deck_uuid for r in first.list_rows(include_archived=True))
    ext_first = {r.name: first.external_ids(r.deck_uuid) for r in first.list_rows(include_archived=True)}

    second = DecksStore()
    uuids_second = sorted(r.deck_uuid for r in second.list_rows(include_archived=True))
    ext_second = {r.name: second.external_ids(r.deck_uuid) for r in second.list_rows(include_archived=True)}

    assert uuids_first == uuids_second  # uuids stable across a re-open
    assert ext_first == ext_second
    assert len(uuids_second) == 2  # no duplicated rows


def test_migration_does_not_change_version(data_dir: Path) -> None:
    """Adding the uuid identity must not perturb version(deck) (it excludes uuid)."""
    deck_a = _deck('Krenko', record_id='recABC123')
    deck_b = _deck('Draft Brew')
    _seed_old_store(deck_a, deck_b)

    before = version(deck_a)
    s = DecksStore()
    uuid_a = next(r.deck_uuid for r in s.list_rows(include_archived=True) if r.name == 'Krenko')
    after = version(s.get(uuid_a))  # type: ignore[arg-type]
    assert after == before
