"""Tests for the mutating ``collection recover-decks`` verb (Phase 5).

OFFLINE + source-agnostic: an in-memory ``FakeAirtable`` (reused from
``test_airtable_collection``) is the source of record, and a tmp
``MAKE_MAGIC_DATA_DIR`` isolates the DuckDB history mirror. ``cli._store`` is
patched to return the fake-backed Airtable store so recovery reads live decks
and its requests can be inspected — the central proof being that a DRY RUN (the
default) issues ZERO writes to Airtable.

Recovery is conservative by construction: propose, never blind-apply.

Covered:
    - dry-run (default): proposes exactly the missing set (tagged unlinked),
      predicts the target size, and writes NOTHING to Airtable.
    - ``--confirm``: re-links the missing cards; the deck is restored to target.
    - deleted-row path: a missing card whose inventory row was ALSO removed is
      tagged ``deleted-row``; ``--confirm`` RECREATES the row (a POST) BEFORE the
      re-link, then the deck is restored.
    - overfill/divergence BLOCK: a deck that gained a card NOT in its baseline is
      BLOCKED ("diverged"); nothing is written even with ``--confirm``.
    - no-baseline: an under-target deck with an empty mirror is reported and
      skipped; no writes.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pipeline.collection import record_snapshot
from pipeline.collection import run as cli
from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore
from tests.test_airtable_collection import (
    _FIELDS,
    FakeAirtable,
    _StubCardResolver,
)


def _recover_store(fake: FakeAirtable) -> AirtableCollectionStore:
    """A fake-backed Airtable store (writes enabled) with a stub resolver.

    A SINGLE shared client so the store's reads and writes hit the same
    stateful ``FakeAirtable`` (and so ``fake.requests`` captures every call).
    """
    transport = httpx.MockTransport(fake.handler)
    client = httpx.Client(transport=transport)
    return AirtableCollectionStore.from_settings(
        'fake-token', writes_enabled=True, client=client, card_resolver=_StubCardResolver()
    )


def _card_row(rid: str, name: str) -> dict[str, object]:
    return {'id': rid, 'fields': {_FIELDS['Inventory Cards']['Card Name']: name}}


def _deck_row(rid: str, name: str, *, fmt: str | None, commander: list[str], cards: list[str]) -> dict[str, object]:
    d = _FIELDS['Decks']
    fields: dict[str, object] = {d['Name']: name, d['Commander']: commander, d['Cards']: cards}
    if fmt is not None:
        fields[d['Format']] = fmt
    return {'id': rid, 'fields': fields}


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the DuckDB mirror under a tmp data dir."""
    root = tmp_path / 'data'
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(root))
    return root


def _run_recover(
    monkeypatch: pytest.MonkeyPatch, store: AirtableCollectionStore, *argv: str
) -> None:
    """Patch ``cli._store`` to the given store and dispatch ``recover-decks``.

    The SAME store instance is returned for every ``_store`` call so the plan
    phase and the apply phase share one client/state (and one ``fake.requests``).
    """
    monkeypatch.setattr(cli, '_store', lambda **_: store)
    monkeypatch.setattr('sys.argv', ['collection', 'recover-decks', *argv])
    cli.main()


def _mutations(fake: FakeAirtable) -> list[httpx.Request]:
    return [r for r in fake.requests if r.method in ('POST', 'PATCH', 'DELETE')]


def _full_deck_fixture() -> FakeAirtable:
    """A full 100-card Commander deck: commander (rec0) + 99 maindeck (rec1..99)."""
    cards = [_card_row(f'rec{i}', f'Card {i}') for i in range(100)]
    full = _deck_row(
        'recDeck', 'Drift EDH', fmt='Commander', commander=['rec0'], cards=[f'rec{i}' for i in range(1, 100)]
    )
    return FakeAirtable({'tblCards': cards, 'tblDecks': [full]})


# --------------------------------------------------------------------------- #
# Dry-run (default): proposes, writes nothing
# --------------------------------------------------------------------------- #


def test_dry_run_proposes_missing_and_writes_nothing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _full_deck_fixture()
    # Seed the known-good baseline from the full deck.
    assert record_snapshot(_recover_store(fake)) is True

    # Drop 3 linked cards -> 97 (commander + 96 maindeck). Their inventory rows
    # stay, so all three are `unlinked`.
    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(4, 100)]

    store = _recover_store(fake)
    fake.requests.clear()  # only measure recovery-phase requests.
    _run_recover(monkeypatch, store)  # dry-run (no --confirm).
    out = capsys.readouterr().out

    # Proposes exactly the three dropped cards, each tagged unlinked.
    for n in ('Card 1', 'Card 2', 'Card 3'):
        line = next(line for line in out.splitlines() if n in line)
        assert 'unlinked' in line
    # Predicted size lands on target.
    assert '100' in out
    assert 'dry-run' in out.lower()
    # THE central guard: not a single write reached Airtable.
    assert _mutations(fake) == [], f'dry-run must not write; saw {[r.method for r in _mutations(fake)]}'


def test_confirm_relinks_unlinked_and_restores_target(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _full_deck_fixture()
    assert record_snapshot(_recover_store(fake)) is True
    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(4, 100)]

    store = _recover_store(fake)
    _run_recover(monkeypatch, store, '--confirm')
    out = capsys.readouterr().out

    # The deck is back to 100 (commander + 99 maindeck): the Cards link was
    # rebuilt to the baseline composition.
    verify = _recover_store(fake).get_deck('Drift EDH')
    assert sum(c.quantity for c in verify.cards) == 100
    assert 'OK' in out


# --------------------------------------------------------------------------- #
# Deleted-row path: recreate the inventory row before re-linking
# --------------------------------------------------------------------------- #


def test_confirm_recreates_deleted_row_before_relink(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _full_deck_fixture()
    assert record_snapshot(_recover_store(fake)) is True

    # Drop three cards from the deck; ALSO delete Card 2's inventory row entirely
    # so it must be recreated (deleted-row) while Card 1 / Card 3 stay unlinked.
    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(4, 100)]
    fake.tables['tblCards'] = [r for r in fake.tables['tblCards'] if r['id'] != 'rec2']

    store = _recover_store(fake)
    fake.requests.clear()
    _run_recover(monkeypatch, store, '--confirm')
    out = capsys.readouterr().out

    # Card 2 is tagged deleted-row in the plan.
    card2_line = next(line for line in out.splitlines() if 'Card 2' in line)
    assert 'deleted-row' in card2_line

    # A POST created a NEW inventory row for Card 2 BEFORE the deck PATCH re-links.
    inv_table = 'tblCards'
    inv_posts = [
        i for i, r in enumerate(fake.requests)
        if r.method == 'POST' and inv_table in str(r.url) and b'Card 2' in r.content
    ]
    deck_patches = [
        i for i, r in enumerate(fake.requests) if r.method == 'PATCH' and 'tblDecks' in str(r.url)
    ]
    assert inv_posts, 'expected a POST recreating Card 2 inventory row'
    assert deck_patches, 'expected a PATCH re-linking the deck'
    assert min(inv_posts) < min(deck_patches), 'inventory row must be recreated BEFORE the deck re-link'

    # Deck restored to target.
    verify = _recover_store(fake).get_deck('Drift EDH')
    assert sum(c.quantity for c in verify.cards) == 100
    assert 'OK' in out


# --------------------------------------------------------------------------- #
# Overfill / divergence BLOCK
# --------------------------------------------------------------------------- #


def test_divergence_blocks_and_writes_nothing_even_with_confirm(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _full_deck_fixture()
    assert record_snapshot(_recover_store(fake)) is True

    # Add a NEW card (rec100 / Card 100) to inventory and put it in the deck while
    # dropping Card 1 AND Card 2: the deck now holds a card NOT in the baseline
    # (diverged) AND is under target (commander + 97 maindeck + Card 100 = 99).
    # Recovery must BLOCK, not overfill / re-add the cut cards.
    fake.tables['tblCards'].append(_card_row('rec100', 'Card 100'))
    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(3, 100)] + ['rec100']

    store = _recover_store(fake)
    fake.requests.clear()
    _run_recover(monkeypatch, store, '--confirm')
    out = capsys.readouterr().out

    assert 'diverged' in out.lower()
    assert 'BLOCKED' in out
    # NOTHING written despite --confirm.
    assert _mutations(fake) == [], f'a diverged deck must never be written; saw {[r.method for r in _mutations(fake)]}'


# --------------------------------------------------------------------------- #
# No baseline (fresh mirror)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Placeholder surfacing: a deleted-row card with NO inventory history
# --------------------------------------------------------------------------- #


def _drop_inventory_history(card_name: str) -> None:
    """Delete every ``inventory_history`` row for ``card_name`` from the mirror.

    Models the production case a deleted-row card whose inventory row was gone
    long enough that the history mirror never retained (or has since lost) any
    capture of it — so ``last_known_inventory_row`` returns None and recovery must
    fabricate a PLACEHOLDER row rather than a faithful one.
    """
    from pipeline import store as _lake

    with _lake.connect() as conn:
        conn.execute('DELETE FROM inventory_history WHERE card_name = ?', [card_name])


def test_deleted_row_with_history_tagged_faithful_no_placeholder_warning(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A deleted-row card WITH inventory history is a FAITHFUL recreate.

    Its plan line stays plain ``deleted-row`` (no ``NO history`` marker), and no
    placeholder summary warning is printed.
    """
    fake = _full_deck_fixture()
    assert record_snapshot(_recover_store(fake)) is True

    # Drop three cards and delete Card 2's inventory ROW (deleted-row) — but its
    # inventory HISTORY stays, so recovery can recreate it faithfully.
    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(4, 100)]
    fake.tables['tblCards'] = [r for r in fake.tables['tblCards'] if r['id'] != 'rec2']

    store = _recover_store(fake)
    fake.requests.clear()
    _run_recover(monkeypatch, store)  # dry-run.
    out = capsys.readouterr().out

    card2_line = next(line for line in out.splitlines() if 'Card 2' in line)
    assert 'deleted-row' in card2_line
    assert 'NO history' not in card2_line
    assert 'placeholder' not in card2_line.lower()
    # No placeholder summary warning when every deleted-row has history.
    assert 'placeholder' not in out.lower()
    assert _mutations(fake) == [], 'dry-run must not write'


def test_deleted_row_no_history_surfaces_placeholder_in_dry_run_plan(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A deleted-row card WITHOUT inventory history surfaces a placeholder warning.

    The DRY-RUN plan must reveal the fabrication BEFORE --confirm: the card line
    carries a ``NO history``/placeholder marker AND a summary count is printed,
    while the dry run still writes nothing.
    """
    fake = _full_deck_fixture()
    assert record_snapshot(_recover_store(fake)) is True

    # Drop three cards, delete Card 2's inventory row AND wipe its inventory
    # history so `last_known_inventory_row` returns None (no faithful payload).
    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(4, 100)]
    fake.tables['tblCards'] = [r for r in fake.tables['tblCards'] if r['id'] != 'rec2']
    _drop_inventory_history('Card 2')

    store = _recover_store(fake)
    fake.requests.clear()
    _run_recover(monkeypatch, store)  # dry-run.
    out = capsys.readouterr().out

    # The Card 2 line reveals the placeholder fabrication.
    card2_line = next(line for line in out.splitlines() if 'Card 2' in line)
    assert 'deleted-row: NO history' in card2_line
    assert 'placeholder' in card2_line.lower()
    assert 'owned=1' in card2_line
    # And a summary count warns about placeholder recreates.
    assert '1 card(s) will be recreated as placeholders' in out
    # Card 1 / Card 3 are still ordinary unlinked (not placeholders).
    card1_line = next(line for line in out.splitlines() if 'Card 1' in line)
    assert 'NO history' not in card1_line
    # THE guard: still writes nothing in dry-run.
    assert _mutations(fake) == [], 'dry-run must not write'


def test_confirm_no_history_recreates_placeholder_and_lands_on_target(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """With --confirm, the no-history case still recreates (owned=1) and lands target.

    Apply behavior is unchanged: the placeholder inventory row is recreated so the
    re-link resolves and the deck is restored to target.
    """
    fake = _full_deck_fixture()
    assert record_snapshot(_recover_store(fake)) is True

    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(4, 100)]
    fake.tables['tblCards'] = [r for r in fake.tables['tblCards'] if r['id'] != 'rec2']
    _drop_inventory_history('Card 2')

    store = _recover_store(fake)
    fake.requests.clear()
    _run_recover(monkeypatch, store, '--confirm')
    out = capsys.readouterr().out

    # A POST recreated Card 2 as a placeholder (owned=1) BEFORE the deck re-link.
    inv_posts = [
        i for i, r in enumerate(fake.requests)
        if r.method == 'POST' and 'tblCards' in str(r.url) and b'Card 2' in r.content
    ]
    deck_patches = [
        i for i, r in enumerate(fake.requests) if r.method == 'PATCH' and 'tblDecks' in str(r.url)
    ]
    assert inv_posts, 'expected a POST recreating the placeholder Card 2 row'
    assert deck_patches, 'expected a PATCH re-linking the deck'
    assert min(inv_posts) < min(deck_patches), 'placeholder row must be recreated BEFORE the re-link'

    # Deck restored to target.
    verify = _recover_store(fake).get_deck('Drift EDH')
    assert sum(c.quantity for c in verify.cards) == 100
    assert 'OK' in out


def test_no_baseline_reported_and_skipped_no_writes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    cards = [_card_row(f'rec{i}', f'Card {i}') for i in range(50)]
    under = _deck_row(
        'recUnder', 'Fresh EDH', fmt='Commander', commander=['rec0'], cards=[f'rec{i}' for i in range(1, 50)]
    )
    fake = FakeAirtable({'tblCards': cards, 'tblDecks': [under]})

    store = _recover_store(fake)
    fake.requests.clear()
    _run_recover(monkeypatch, store, '--confirm')
    out = capsys.readouterr().out

    assert 'no baseline' in out.lower()
    assert _mutations(fake) == [], 'no baseline -> no writes'


# --------------------------------------------------------------------------- #
# Dry-run default writes nothing even when recovery IS possible
# --------------------------------------------------------------------------- #


def test_dry_run_is_the_default_no_writes_without_confirm(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _full_deck_fixture()
    assert record_snapshot(_recover_store(fake)) is True
    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(4, 100)]

    store = _recover_store(fake)
    fake.requests.clear()
    _run_recover(monkeypatch, store)  # no --confirm.
    assert _mutations(fake) == [], 'recover-decks must be dry-run by default'
    # And the deck is untouched at 97.
    verify = _recover_store(fake).get_deck('Drift EDH')
    assert sum(c.quantity for c in verify.cards) == 97
