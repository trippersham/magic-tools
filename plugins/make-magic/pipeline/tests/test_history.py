"""TDD tests for the append-only DuckDB history mirror (Phase 2).

Everything here is OFFLINE: a tmp data dir (via the ``MAKE_MAGIC_DATA_DIR`` env
override, mirroring ``tests/test_store.py``) plus a tiny in-memory fake store.
No network, no Airtable, no skills.

Coverage:
    - first call persists (no prior snapshot) -> one row per deck + per inventory card.
    - a second call within TTL and no churn persists nothing (returns False).
    - crossing the TTL (advance injected ``now``) with no churn persists.
    - churn (inventory add/remove, or a deck's card-set change) within TTL persists.
    - a swap that stays at target rolls ``last_known_good_deck`` forward; a later
      sub-target drop does NOT change it (still returns the last at-target row).
    - ``inventory_history`` retains a row that is later removed from the store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection import history
from pipeline.contracts import Deck, DeckCard, OwnedCard


class FakeStore:
    """A minimal in-memory `CollectionStore` slice for history tests.

    Exposes only what :func:`history.record_snapshot` reads: ``list_decks``,
    ``list_inventory``, and ``backend_name``. Mutating the public lists between
    calls simulates churn / deletions.
    """

    def __init__(self, decks: list[Deck], inventory: list[OwnedCard], *, backend: str = 'local') -> None:
        self.decks = decks
        self.inventory = inventory
        self._backend = backend

    def list_decks(self) -> list[Deck]:
        return list(self.decks)

    def list_inventory(self) -> list[OwnedCard]:
        return list(self.inventory)

    @property
    def backend_name(self) -> str:
        return self._backend


def _deck(name: str, card_names: list[str], *, fmt: str | None = 'Commander') -> Deck:
    """A Commander deck padded to ``card_names`` cards (each quantity 1)."""
    cards = [DeckCard(name=n, oracle_id=f'oid-{n}') for n in card_names]
    return Deck(name=name, format=fmt, cards=cards)


def _commander_deck_at_target(name: str, *, size: int = 100) -> Deck:
    """A 100-card Commander deck (size distinct cards)."""
    return _deck(name, [f'{name}-card-{i}' for i in range(size)])


def _owned(name: str, *, rec: str | None = None, owned: int = 1) -> OwnedCard:
    return OwnedCard(name=name, oracle_id=f'oid-{name}', owned=owned, airtable_record_id=rec)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the DuckDB lake at an isolated tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def _count(table: str) -> int:
    with store.connect() as conn:
        history._ensure_history_tables(conn)
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


# --------------------------------------------------------------------------- #
# TTL / churn gate
# --------------------------------------------------------------------------- #


def test_first_call_persists_one_row_per_entity(data_dir: Path) -> None:
    store_ = FakeStore(
        decks=[_deck('Spiders', ['a', 'b', 'c'])],
        inventory=[_owned('a'), _owned('b')],
    )
    assert history.record_snapshot(store_, now=T0) is True
    assert _count('deck_history') == 1
    assert _count('inventory_history') == 2


def test_second_call_within_ttl_no_churn_persists_nothing(data_dir: Path) -> None:
    store_ = FakeStore(decks=[_deck('Spiders', ['a', 'b'])], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0) is True
    later = T0 + timedelta(hours=1)
    assert history.record_snapshot(store_, now=later) is False
    assert _count('deck_history') == 1
    assert _count('inventory_history') == 1


def test_crossing_ttl_no_churn_persists(data_dir: Path) -> None:
    store_ = FakeStore(decks=[_deck('Spiders', ['a', 'b'])], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0) is True
    later = T0 + timedelta(hours=25)
    assert history.record_snapshot(store_, now=later, ttl_hours=24) is True
    assert _count('deck_history') == 2
    assert _count('inventory_history') == 2


def test_inventory_churn_within_ttl_persists(data_dir: Path) -> None:
    store_ = FakeStore(decks=[_deck('Spiders', ['a', 'b'])], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0) is True
    store_.inventory.append(_owned('b'))  # add a row -> churn
    later = T0 + timedelta(hours=1)
    assert history.record_snapshot(store_, now=later) is True
    assert _count('deck_history') == 2
    assert _count('inventory_history') == 3


def test_deck_cardset_churn_within_ttl_persists(data_dir: Path) -> None:
    store_ = FakeStore(decks=[_deck('Spiders', ['a', 'b'])], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0) is True
    store_.decks[0].cards.append(DeckCard(name='c', oracle_id='oid-c'))  # card-set change
    later = T0 + timedelta(hours=1)
    assert history.record_snapshot(store_, now=later) is True
    assert _count('deck_history') == 2


def test_churn_below_min_cards_is_ignored(data_dir: Path) -> None:
    store_ = FakeStore(decks=[_deck('Spiders', ['a', 'b', 'c', 'd'])], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0, churn_min_cards=2) is True
    store_.decks[0].cards.append(DeckCard(name='e', oracle_id='oid-e'))  # 1-card delta < threshold
    later = T0 + timedelta(hours=1)
    assert history.record_snapshot(store_, now=later, churn_min_cards=2) is False


# --------------------------------------------------------------------------- #
# known-good semantics
# --------------------------------------------------------------------------- #


def test_swap_at_target_rolls_known_good_forward(data_dir: Path) -> None:
    deck = _commander_deck_at_target('Spiders')
    store_ = FakeStore(decks=[deck], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0) is True

    # A legitimate swap (still 100 cards): replace one card, advance past TTL.
    deck.cards[0] = DeckCard(name='swapped-in', oracle_id='oid-swapped-in')
    t1 = T0 + timedelta(hours=25)
    assert history.record_snapshot(store_, now=t1, ttl_hours=24) is True

    with store.connect() as conn:
        history._ensure_history_tables(conn)
        good = history.last_known_good_deck(conn, 'Spiders', 'local')
    assert good is not None
    assert good['size'] == 100
    # DuckDB TIMESTAMP is tz-naive; the mirror stores naive-UTC (see _as_naive_utc).
    assert good['snapshot_ts'] == t1.astimezone(UTC).replace(tzinfo=None)  # rolled forward to newer at-target


def test_subtarget_drop_leaves_last_known_good_intact(data_dir: Path) -> None:
    deck = _commander_deck_at_target('Spiders')
    store_ = FakeStore(decks=[deck], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0) is True

    # Drift below target: drop 5 cards, advance past TTL -> a new (bad) row.
    del deck.cards[:5]
    t1 = T0 + timedelta(hours=25)
    assert history.record_snapshot(store_, now=t1, ttl_hours=24) is True

    with store.connect() as conn:
        history._ensure_history_tables(conn)
        good = history.last_known_good_deck(conn, 'Spiders', 'local')
    assert good is not None
    assert good['size'] == 100
    assert good['passed_invariant'] is True
    # The drift row did NOT overwrite the good baseline (still the T0 at-target row).
    assert good['snapshot_ts'] == T0.astimezone(UTC).replace(tzinfo=None)


def test_no_known_good_when_never_at_target(data_dir: Path) -> None:
    # A Commander deck that starts and stays below target -> no known-good row.
    store_ = FakeStore(decks=[_deck('Spiders', ['a', 'b'])], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0) is True
    with store.connect() as conn:
        history._ensure_history_tables(conn)
        assert history.last_known_good_deck(conn, 'Spiders', 'local') is None


def test_untargeted_deck_has_no_known_good(data_dir: Path) -> None:
    # No format -> target None -> passed_invariant True, but target IS NULL so it
    # is not a known-good recovery reference.
    store_ = FakeStore(decks=[_deck('Kitchen Table', ['a', 'b'], fmt=None)], inventory=[_owned('a')])
    assert history.record_snapshot(store_, now=T0) is True
    with store.connect() as conn:
        history._ensure_history_tables(conn)
        assert history.last_known_good_deck(conn, 'Kitchen Table', 'local') is None


# --------------------------------------------------------------------------- #
# durable inventory backstop
# --------------------------------------------------------------------------- #


def test_inventory_history_retains_removed_row(data_dir: Path) -> None:
    store_ = FakeStore(
        decks=[_deck('Spiders', ['a'])],
        inventory=[_owned('Sol Ring', rec='rec123'), _owned('b')],
    )
    assert history.record_snapshot(store_, now=T0) is True

    # The naive-delete: Sol Ring's row vanishes from the live store.
    store_.inventory = [_owned('b')]
    t1 = T0 + timedelta(hours=25)
    assert history.record_snapshot(store_, now=t1, ttl_hours=24) is True

    with store.connect() as conn:
        history._ensure_history_tables(conn)
        rows = conn.execute('SELECT card_name, fields FROM inventory_history WHERE card_id = ?', ['rec123']).fetchall()
    # The deleted row is still recoverable from the first snapshot (append-only).
    assert any(name == 'Sol Ring' for name, _ in rows)


def test_record_snapshot_swallows_lake_errors(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Best-effort resilience: a mirror write must never raise into a read path.
    store_ = FakeStore(decks=[_deck('Spiders', ['a'])], inventory=[_owned('a')])

    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError('lake exploded')

    monkeypatch.setattr(history, '_record_snapshot', _boom)
    assert history.record_snapshot(store_, now=T0) is False
