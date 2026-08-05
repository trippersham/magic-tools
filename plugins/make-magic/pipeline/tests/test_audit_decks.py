"""Tests for the read-only ``collection audit-decks`` verb.

OFFLINE + source-agnostic: an in-memory ``FakeAirtable`` (reused from
``test_airtable_collection``) is the source of record, and a tmp
``MAKE_MAGIC_DATA_DIR`` isolates the DuckDB history mirror. ``cli._store`` is
patched to return the fake-backed Airtable store so the audit reads live decks
and its requests can be inspected (proving it never writes to the source).

Covered:
    - per-deck status: a below-target Commander deck is ``UNDER-TARGET`` (warning,
      exit 0), an at-target Commander deck is ``OK``, a 60-card deck at 60 is
      ``OK`` (not flagged), an untargeted deck is ``untargeted``.
    - exit code is 0 even with under-target decks.
    - read-only w.r.t. Airtable: no POST/PATCH/DELETE in ``fake.requests``.
    - drift diff vs a seeded known-good baseline: the correct missing cards are
      listed and tagged ``unlinked`` (row still exists) vs ``deleted-row`` (its
      inventory row is gone).
    - a fresh mirror (no baseline) reports the target check without crashing.
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


def _audit_store(fake: FakeAirtable) -> AirtableCollectionStore:
    """A fake-backed Airtable store (read-only) with a stub resolver."""
    transport = httpx.MockTransport(fake.handler)
    client = httpx.Client(transport=transport)
    return AirtableCollectionStore.from_settings(
        'fake-token', writes_enabled=False, client=client, card_resolver=_StubCardResolver()
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


def _run_audit(monkeypatch: pytest.MonkeyPatch, fake: FakeAirtable, *argv: str) -> None:
    """Patch ``cli._store`` to the fake-backed store and dispatch ``audit-decks``."""
    monkeypatch.setattr(cli, '_store', lambda **_: _audit_store(fake))
    monkeypatch.setattr('sys.argv', ['collection', 'audit-decks', *argv])
    cli.main()


# --------------------------------------------------------------------------- #
# Per-deck status + exit code
# --------------------------------------------------------------------------- #


def _status_fixture() -> FakeAirtable:
    """Four decks: under-target Commander, at-target Commander, 60-at-60, untargeted."""
    cards = [_card_row(f'rec{i}', f'Card {i}') for i in range(100)]
    under = _deck_row(
        'recUnder', 'Under EDH', fmt='Commander', commander=['rec0'], cards=[f'rec{i}' for i in range(1, 50)]
    )
    at = _deck_row('recAt', 'At EDH', fmt='Commander', commander=['rec0'], cards=[f'rec{i}' for i in range(1, 100)])
    sixty = _deck_row('recSixty', 'Sixty', fmt='Modern', commander=[], cards=[f'rec{i}' for i in range(60)])
    untargeted = _deck_row('recWip', 'WIP', fmt=None, commander=[], cards=[f'rec{i}' for i in range(5)])
    return FakeAirtable({'tblCards': cards, 'tblDecks': [under, at, sixty, untargeted]})


def test_under_target_commander_is_flagged_warning(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _status_fixture()
    _run_audit(monkeypatch, fake)
    out = capsys.readouterr().out
    assert 'Under EDH' in out
    assert 'UNDER-TARGET' in out
    # the at-target and 60-card decks are OK (not flagged under-target).
    assert 'At EDH' in out
    assert 'Sixty' in out
    # the untargeted deck is reported as untargeted, never flagged.
    assert 'untargeted' in out
    # summary counts one deck under target.
    assert '4 decks' in out
    assert '1 under target' in out


def test_at_target_commander_is_ok(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _status_fixture()
    _run_audit(monkeypatch, fake)
    lines = capsys.readouterr().out.splitlines()
    at_line = next(line for line in lines if 'At EDH' in line)
    assert 'OK' in at_line
    assert 'UNDER-TARGET' not in at_line


def test_sixty_card_deck_at_sixty_not_flagged(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _status_fixture()
    _run_audit(monkeypatch, fake)
    lines = capsys.readouterr().out.splitlines()
    sixty_line = next(line for line in lines if 'Sixty' in line)
    assert 'OK' in sixty_line
    assert 'UNDER-TARGET' not in sixty_line


def test_untargeted_deck_reported_untargeted(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _status_fixture()
    _run_audit(monkeypatch, fake)
    lines = capsys.readouterr().out.splitlines()
    wip_line = next(line for line in lines if 'WIP' in line)
    assert 'untargeted' in wip_line
    assert 'UNDER-TARGET' not in wip_line


def test_exit_code_zero_with_under_target(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _status_fixture()
    # audit-decks is a report, not a gate: SystemExit must NOT be raised.
    _run_audit(monkeypatch, fake)  # would raise SystemExit(1) on failure.


# --------------------------------------------------------------------------- #
# Read-only w.r.t. the source of record
# --------------------------------------------------------------------------- #


def test_audit_issues_no_writes_to_airtable(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _status_fixture()
    _run_audit(monkeypatch, fake)
    mutations = [r for r in fake.requests if r.method in ('POST', 'PATCH', 'DELETE')]
    assert mutations == [], f'audit must not write to Airtable; saw {[r.method for r in mutations]}'


# --------------------------------------------------------------------------- #
# Drift diff vs a seeded known-good baseline
# --------------------------------------------------------------------------- #


def test_drift_diff_tags_unlinked_vs_deleted_row(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # Full Commander deck at 100: commander + 99 maindeck cards, each with an
    # inventory row.
    cards = [_card_row(f'rec{i}', f'Card {i}') for i in range(100)]
    full = _deck_row(
        'recDeck', 'Drift EDH', fmt='Commander', commander=['rec0'], cards=[f'rec{i}' for i in range(1, 100)]
    )
    fake = FakeAirtable({'tblCards': cards, 'tblDecks': [full]})

    # Seed the known-good baseline in the mirror from the full deck.
    assert record_snapshot(_audit_store(fake)) is True

    # Now DROP two cards from the deck (Card 1 and Card 2) so it's under target,
    # and DELETE the inventory row for Card 2 (so it becomes a deleted-row) while
    # keeping Card 1's row (so it stays unlinked).
    deck_fields = fake.tables['tblDecks'][0]['fields']
    deck_fields[_FIELDS['Decks']['Cards']] = [f'rec{i}' for i in range(3, 100)]
    fake.tables['tblCards'] = [r for r in fake.tables['tblCards'] if r['id'] != 'rec2']

    _run_audit(monkeypatch, fake)
    out = capsys.readouterr().out

    assert 'UNDER-TARGET' in out
    # both missing cards listed with the right tag.
    card1 = next(line for line in out.splitlines() if 'Card 1' in line)
    card2 = next(line for line in out.splitlines() if 'Card 2' in line)
    assert 'unlinked' in card1  # its inventory row still exists.
    assert 'deleted-row' in card2  # its inventory row is gone.


# --------------------------------------------------------------------------- #
# No baseline (fresh mirror)
# --------------------------------------------------------------------------- #


def test_no_baseline_reports_target_check_without_crashing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    cards = [_card_row(f'rec{i}', f'Card {i}') for i in range(50)]
    under = _deck_row(
        'recUnder', 'Fresh EDH', fmt='Commander', commander=['rec0'], cards=[f'rec{i}' for i in range(1, 50)]
    )
    fake = FakeAirtable({'tblCards': cards, 'tblDecks': [under]})
    _run_audit(monkeypatch, fake)
    out = capsys.readouterr().out
    assert 'UNDER-TARGET' in out
    assert 'no historical baseline' in out
