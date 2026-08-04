"""TDD tests for the edit-triggered deck-version ledger (Phase 2, W2).

The ledger is an APPEND-ONLY companion table (``deck_versions``) keyed on the
decks-store ``deck_id``: every deck mutation appends ``(deck_id, version,
deck_json, rationale, ts)``. Un-gated (unlike the TTL/churn-gated
``record_snapshot``). Yields three feature-babies from one structure — undo
(restore a prior version), rationale (the edit note), and version provenance.

Everything is OFFLINE: an isolated tmp data root via ``MAKE_MAGIC_DATA_DIR``;
no network. Airtable is never touched here (pure local DuckDB).
"""

from __future__ import annotations

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
# undo — previous_deck_version restores the version BEFORE the head
# --------------------------------------------------------------------------- #


def test_previous_deck_version_returns_the_version_before_head(data_dir: Path) -> None:
    deck1 = _deck(strategy='v1')
    deck2 = _deck(strategy='v2')
    with store.connect() as conn:
        history.append_deck_version(conn, deck_uuid='d1', deck=deck1, rationale='first')
        history.append_deck_version(conn, deck_uuid='d1', deck=deck2, rationale='second')
        prev = history.previous_deck_version(conn, 'd1')
    assert prev is not None
    assert prev.strategy == 'v1'
    assert version(prev) == version(deck1)


def test_previous_deck_version_none_with_single_version(data_dir: Path) -> None:
    """With only one recorded version there is nothing to undo TO."""
    with store.connect() as conn:
        history.append_deck_version(conn, deck_uuid='d1', deck=_deck(), rationale='only')
        assert history.previous_deck_version(conn, 'd1') is None


def test_previous_deck_version_none_when_absent(data_dir: Path) -> None:
    with store.connect() as conn:
        assert history.previous_deck_version(conn, 'nope') is None


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
    """Phase-1 behavior is preserved when rationale is omitted — it still appends."""
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
