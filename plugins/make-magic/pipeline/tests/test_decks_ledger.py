"""The edit-triggered deck-version ledger.

The ledger is an APPEND-ONLY companion table (``deck_versions``) keyed on the
decks-store ``deck_id``: every deck mutation appends ``(deck_id, version,
deck_json, rationale, ts)``. Un-gated (unlike the TTL/churn-gated
``record_snapshot``). One structure serves three capabilities — undo
(restore a prior version), rationale (the edit note), and version provenance.

Everything is OFFLINE: an isolated tmp data root via ``MAKE_MAGIC_DATA_DIR``;
no network. Airtable is never touched here (pure local DuckDB).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _decks_helpers import decks_dir, expire, save_source, source_store

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


def _deck(name: str = 'Krenko Goblins', *, strategy: str | None = 'go wide') -> Deck:
    cards = [DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'), DeckCard(name='Mountain', quantity=10)]
    for i in range(89):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    return Deck(name=name, format='commander', strategy=strategy, cards=cards)


# --------------------------------------------------------------------------- #
# append_deck_version — un-gated per-edit append
# --------------------------------------------------------------------------- #


def test_append_deck_version_records_version_and_rationale(data_dir: Path) -> None:
    deck = _deck()
    with store.connect() as conn:
        history.append_deck_version(conn, deck_uuid='d1', deck=deck, rationale='initial')
        rows = history.deck_version_rows(conn, 'd1')
    assert len(rows) == 1
    assert rows[0]['version'] == version(deck)
    assert rows[0]['rationale'] == 'initial'


def test_append_is_ungated_every_call_appends(data_dir: Path) -> None:
    """Unlike record_snapshot, EVERY append lands a row — no TTL/churn gate."""
    deck = _deck()
    with store.connect() as conn:
        history.append_deck_version(conn, deck_uuid='d1', deck=deck, rationale='a')
        history.append_deck_version(conn, deck_uuid='d1', deck=deck, rationale='b')
        history.append_deck_version(conn, deck_uuid='d1', deck=deck, rationale='c')
        rows = history.deck_version_rows(conn, 'd1')
    assert [r['rationale'] for r in rows] == ['a', 'b', 'c']


def test_append_is_append_only_no_row_mutated(data_dir: Path) -> None:
    """A second append never UPDATEs the first row — the head history is preserved."""
    deck1 = _deck(strategy='v1')
    deck2 = _deck(strategy='v2')
    with store.connect() as conn:
        history.append_deck_version(conn, deck_uuid='d1', deck=deck1, rationale='first')
        history.append_deck_version(conn, deck_uuid='d1', deck=deck2, rationale='second')
        rows = history.deck_version_rows(conn, 'd1')
    assert len(rows) == 2
    # The first row still holds v1's version + rationale (nothing overwrote it).
    assert rows[0]['version'] == version(deck1)
    assert rows[0]['rationale'] == 'first'
    assert rows[1]['version'] == version(deck2)


# --------------------------------------------------------------------------- #
# DecksStore edits append a version (edit-triggered wiring)
# --------------------------------------------------------------------------- #


def test_edit_appends_a_version(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    s.set_strategy('d1', 'aristocrats', rationale='pivot to sacrifice')
    with store.connect() as conn:
        rows = history.deck_version_rows(conn, 'd1')
    assert any(r['rationale'] == 'pivot to sacrifice' for r in rows)
    assert rows[-1]['version'] == version(s.get('d1'))


def test_edit_without_rationale_still_appends_with_default_note(data_dir: Path) -> None:
    """An edit with rationale omitted still appends a version."""
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')  # establishes the baseline version (undo floor)
    s.add_card('d1', DeckCard(name='Sol Ring', quantity=1))
    with store.connect() as conn:
        rows = history.deck_version_rows(conn, 'd1')
    # put appended the baseline, the add appended the head — the head is the edit
    # and carries a non-empty DEFAULT note (rationale omitted).
    assert len(rows) == 2
    assert rows[-1]['version'] == version(s.get('d1'))
    assert rows[-1]['rationale']  # a non-empty default note


def test_undo_via_store_restores_prior_deck(data_dir: Path) -> None:
    """Apply a swap, then undo -> the deck is restored to the prior ledger version."""
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    before = version(s.get('d1'))
    s.swap('d1', add=DeckCard(name='Impact Tremors', quantity=1), cut='Goblin 0', rationale='upgrade')
    assert version(s.get('d1')) != before
    restored = s.undo('d1')
    assert restored is not None
    assert version(s.get('d1')) == before
    assert not any(c.name == 'Impact Tremors' for c in s.get('d1').cards)


def test_undo_preserves_sync_bookkeeping(data_dir: Path) -> None:
    """Undo restores deck_json but keeps the row's sync_status/source_ref/baseline."""
    s = DecksStore()
    deck = _deck()
    s.put(deck, deck_uuid='d1', sync_status='synced', source_ref='{"backend":"local","name":"Krenko Goblins"}',
          synced_baseline=version(deck))
    s.set_strategy('d1', 'changed')
    s.undo('d1')
    row = s.get_row('d1')
    assert row is not None
    assert row.sync_status == 'synced'
    assert row.source_ref == '{"backend":"local","name":"Krenko Goblins"}'
    assert row.synced_baseline == version(deck)


# --------------------------------------------------------------------------- #
# undo / rollback — a refused commit never half-applies (edit + undo, cursor).
# --------------------------------------------------------------------------- #


def test_set_strategy_rolls_back_on_push_refusal(cli, data_dir: Path) -> None:
    """A push-refused ``set-strategy`` (source drifted) must NOT leave the local edit
    half-applied — the deck's strategy stays what it was before the failed commit."""
    save_source(cli, data_dir, 'Rollback', filler=99, prefix='RCard')
    cli('get-deck', 'Rollback')  # pull -> local row

    # A foreign writer moves the source (drift) so the commit push will refuse.
    src = source_store(data_dir)
    moved = src.get_deck('Rollback')
    moved.cards.append(DeckCard(name='Foreign Card'))
    src.save_deck(moved, allow_shrink=True)

    code, _out, err = cli('set-strategy', 'Rollback', 'new strategy text')
    assert code != 0, 'a drifted commit must refuse'
    assert 'moved' in err or 'drift' in err.lower()
    # The local edit was rolled back — strategy is NOT the refused text.
    code, out, _err = cli('get-deck', 'Rollback', '--field', 'strategy')
    assert 'new strategy text' not in out


def test_drift_refused_undo_rolls_back(cli, data_dir: Path) -> None:
    """A drift-refused ``undo-deck`` rolls back (no half-applied undo)."""
    save_source(cli, data_dir, 'R', filler=99, prefix='RCard')
    assert cli('get-deck', 'R')[0] == 0
    assert cli('set-strategy', 'R', 'good strategy')[0] == 0

    # A concurrent FOREIGN edit on the source file makes the next commit drift-refuse.
    rfile = decks_dir(data_dir) / 'r.yaml'
    rfile.write_text(rfile.read_text().replace('- card: RCard 0', '- card: Foreign R\n- card: RCard 0'))
    os.utime(decks_dir(data_dir))

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


def test_drift_refused_undo_rolls_back_cursor(cli, data_dir: Path) -> None:
    """A drift-refused undo does not burn an undo step: the next undo restores head-1."""
    save_source(cli, data_dir, 'Cursor', filler=99, prefix='CCard')
    assert cli('get-deck', 'Cursor')[0] == 0
    # Build a ledger: v0 (baseline, no strategy) -> v1 (stratA) -> v2 (stratB).
    assert cli('set-strategy', 'Cursor', 'stratA')[0] == 0
    assert cli('set-strategy', 'Cursor', 'stratB')[0] == 0

    cfile = decks_dir(data_dir) / 'cursor.yaml'
    healthy = cfile.read_text()  # the source in agreement with the local stratB deck.

    # A concurrent FOREIGN source edit makes the NEXT commit drift-refuse.
    cfile.write_text(healthy.replace('- card: CCard 0', '- card: Foreign C\n- card: CCard 0'))
    os.utime(decks_dir(data_dir))

    # A drift-refused undo rolls content back to stratB AND must not burn the cursor.
    expire('Cursor')
    assert cli('undo-deck', 'Cursor')[0] == 1
    # The local content rolled back to stratB (verify from the store, WITHOUT a read
    # that would re-pull the still-drifted source and interpose a spurious version).
    assert DecksStore().get(DecksStore().uuid_for_name('Cursor')).strategy == 'stratB'

    # Heal the drift out-of-band BEFORE any read re-pulls the foreign content: restore
    # the source to EXACTLY the healthy state so the next commit is clean and no re-pull
    # alters the local content or resets the cursor. This isolates the cursor-burn: if the
    # refused undo had burned a step, the next undo would skip stratA down to the baseline.
    cfile.write_text(healthy)
    os.utime(decks_dir(data_dir))
    expire('Cursor')

    code, _out, err = cli('undo-deck', 'Cursor')
    assert code == 0, err
    assert DecksStore().get(DecksStore().uuid_for_name('Cursor')).strategy == 'stratA', (
        'undo skipped a step — cursor was burned'
    )


def test_first_save_permission_error_is_clean(cli, data_dir: Path) -> None:
    """A PermissionError from a failing driver write on a FIRST save-deck surfaces as a
    clean ``error:`` (exit 1), not a raw traceback (exit -1 in the harness); the
    just-created row is rolled back so no zombie lingers.
    """
    import stat

    from _decks_helpers import save_json

    # Seed the decks dir (an unrelated deck) so it exists, then make it read-only so
    # the NEXT save-deck's YAML write fails with a PermissionError.
    save_source(cli, data_dir, 'Seed', filler=99, prefix='SeedCard')
    ddir = decks_dir(data_dir)
    mode = ddir.stat().st_mode
    os.chmod(ddir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        code, _out, err = save_json(
            cli,
            data_dir,
            {'name': 'Brandnew', 'format': 'Commander',
             'cards': [{'name': 'Krenko, Mob Boss', 'role': 'commander'}]},
        )
    finally:
        os.chmod(ddir, mode)

    # A clean error, NOT a raw traceback escaping the CLI.
    assert code == 1, f'expected clean exit 1, got {code}: {err}'
    assert 'Traceback' not in err, err
    # The just-created row was rolled back (no zombie for a later sync to land).
    assert DecksStore().uuid_for_name('Brandnew') is None
