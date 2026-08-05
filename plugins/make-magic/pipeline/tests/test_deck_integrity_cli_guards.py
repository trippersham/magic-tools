"""PREVENTION: CLI-level guard enforcement (the user-facing surface).

OFFLINE + source-agnostic. Two harnesses:
    - a fake-backed Airtable store (reused ``FakeAirtable``) drives the CRITICAL
      proof: an aborted ``remove-card`` issues NO Airtable DELETE, and a forced
      one does — inspected via ``fake.requests``.
    - a tmp local YAML store exercises the same CLI verbs end-to-end offline.

Covered:
    - remove-card of a card linked to 2+ decks aborts without --force; the error
      enumerates every affected deck and flags under-target ones; NO DELETE
      reaches Airtable. With --force it deletes.
    - remove-card of an UNLINKED card deletes with no flag.
    - save-deck that shrinks an at-target deck under target aborts without
      --confirm and proceeds with it; a build (create) never trips.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pipeline import store as _store_mod
from pipeline.collection import run as cli
from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore
from pipeline.contracts import Card
from tests.test_airtable_collection import _FIELDS, FakeAirtable, _StubCardResolver

# --------------------------------------------------------------------------- #
# Airtable-backed harness (CRITICAL: prove abort issues no DELETE)
# --------------------------------------------------------------------------- #


def _airtable_store(fake: FakeAirtable, *, writes_enabled: bool = True) -> AirtableCollectionStore:
    transport = httpx.MockTransport(fake.handler)
    client = httpx.Client(transport=transport)
    return AirtableCollectionStore.from_settings(
        'fake-token', writes_enabled=writes_enabled, client=client, card_resolver=_StubCardResolver()
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
    root = tmp_path / 'data'
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(root))
    return root


def _run_airtable(monkeypatch: pytest.MonkeyPatch, fake: FakeAirtable, *argv: str) -> None:
    """Patch ``cli._store`` to the fake-backed store and dispatch a verb."""
    monkeypatch.setattr(cli, '_store', lambda **_: _airtable_store(fake))
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


def _shared_card_fixture() -> FakeAirtable:
    """One shared inventory card (Sol Ring) linked into two at-target Commander
    decks, so a delete would drop BOTH from 100 -> 99."""
    cards = [_card_row('recSol', 'Sol Ring')] + [_card_row(f'rec{i}', f'Card {i}') for i in range(99)]
    # 99 unique 'Card i' + Sol Ring in Cards, commander = Card 0 -> 100 cards each.
    d1 = _deck_row(
        'recD1',
        'Alpha EDH',
        fmt='Commander',
        commander=['rec0'],
        cards=['recSol'] + [f'rec{i}' for i in range(1, 99)],
    )
    d2 = _deck_row(
        'recD2',
        'Beta EDH',
        fmt='Commander',
        commander=['rec0'],
        cards=['recSol'] + [f'rec{i}' for i in range(1, 99)],
    )
    return FakeAirtable({'tblCards': cards, 'tblDecks': [d1, d2]})


def test_remove_linked_card_aborts_without_force_and_no_delete(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _shared_card_fixture()
    with pytest.raises(SystemExit) as exc:
        _run_airtable(monkeypatch, fake, 'remove-card', 'Sol Ring')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    # Enumerates BOTH affected decks and flags them under-target.
    assert 'Alpha EDH' in err
    assert 'Beta EDH' in err
    assert 'UNDER TARGET' in err
    assert '100 -> 99' in err
    # THE PROOF: not a single DELETE reached Airtable.
    assert not any(r.method == 'DELETE' for r in fake.requests), 'abort must issue NO Airtable delete'
    # The shared row is still present.
    assert any(r['fields'].get(_FIELDS['Inventory Cards']['Card Name']) == 'Sol Ring' for r in fake.tables['tblCards'])


def test_remove_linked_card_with_force_deletes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = _shared_card_fixture()
    _run_airtable(monkeypatch, fake, 'remove-card', 'Sol Ring', '--force')
    out = capsys.readouterr().out
    assert 'remove-card: Sol Ring' in out
    assert any(r.method == 'DELETE' for r in fake.requests), '--force must delete'
    assert not any(
        r['fields'].get(_FIELDS['Inventory Cards']['Card Name']) == 'Sol Ring' for r in fake.tables['tblCards']
    )


def test_remove_unlinked_card_deletes_without_flag(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # A card that no deck links -> deletes normally, no flag needed.
    fake = FakeAirtable({'tblCards': [_card_row('recOrphan', 'Black Lotus')], 'tblDecks': []})
    _run_airtable(monkeypatch, fake, 'remove-card', 'Black Lotus')
    assert 'remove-card: Black Lotus' in capsys.readouterr().out
    assert any(r.method == 'DELETE' for r in fake.requests)
    assert fake.tables['tblCards'] == []


# --------------------------------------------------------------------------- #
# Port-level (bypass the CLI): a DIRECT store.remove_card must ALSO guard.
#
# Defense-in-depth: the CLI guard is not the only barrier. These hit the adapter
# primitive directly (no argparse in the loop) to prove a programmatic
# get_store().remove_card can no longer silently cascade-strip decks.
# --------------------------------------------------------------------------- #


def test_adapter_remove_linked_card_raises_and_issues_no_delete() -> None:
    from pipeline.collection.errors import CollectionError

    fake = _shared_card_fixture()  # Sol Ring in two at-target 100-card decks
    store = _airtable_store(fake)
    with pytest.raises(CollectionError) as exc:
        store.remove_card('Sol Ring')  # NO force
    msg = str(exc.value)
    assert 'Alpha EDH' in msg
    assert 'Beta EDH' in msg
    assert 'UNDER TARGET' in msg
    assert '100 -> 99' in msg
    # THE PROOF: the aborted direct call issued no Airtable DELETE.
    assert not any(r.method == 'DELETE' for r in fake.requests), 'adapter abort must issue NO delete'
    assert any(r['fields'].get(_FIELDS['Inventory Cards']['Card Name']) == 'Sol Ring' for r in fake.tables['tblCards'])


def test_adapter_remove_linked_card_with_force_deletes() -> None:
    fake = _shared_card_fixture()
    store = _airtable_store(fake)
    store.remove_card('Sol Ring', force=True)
    assert any(r.method == 'DELETE' for r in fake.requests), 'force=True must delete'
    assert not any(
        r['fields'].get(_FIELDS['Inventory Cards']['Card Name']) == 'Sol Ring' for r in fake.tables['tblCards']
    )


def test_adapter_remove_unlinked_card_removes_with_default_force() -> None:
    fake = FakeAirtable({'tblCards': [_card_row('recOrphan', 'Black Lotus')], 'tblDecks': []})
    store = _airtable_store(fake)
    store.remove_card('Black Lotus')  # default force=False, but unlinked
    assert any(r.method == 'DELETE' for r in fake.requests)
    assert fake.tables['tblCards'] == []


# --------------------------------------------------------------------------- #
# Local YAML harness for the CLI verbs (offline end-to-end)
# --------------------------------------------------------------------------- #


class _StubResolver:
    def get_card(self, name: str) -> Card | None:
        return None


@pytest.fixture()
def local_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(_store_mod.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _StubResolver())
    return root


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


def _write_deck_json(tmp_path: Path, name: str, n_cards: int, fmt: str | None) -> Path:
    cards = [{'name': 'Sol Ring', 'role': 'commander'}] + [{'name': f'Card {i}'} for i in range(n_cards - 1)]
    payload: dict[str, object] = {'name': name, 'cards': cards}
    if fmt is not None:
        payload['format'] = fmt
    p = tmp_path / f'{name}.json'
    p.write_text(json.dumps(payload))
    return p


def test_local_remove_linked_card_aborts(
    local_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _run(monkeypatch, 'add-card', 'Sol Ring')
    deck_json = _write_deck_json(tmp_path, 'EDH', 100, 'Commander')
    _run(monkeypatch, 'save-deck', '--from-json', str(deck_json))
    capsys.readouterr()
    # Sol Ring is the commander of a 100-card deck -> linked -> abort without --force.
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'remove-card', 'Sol Ring')
    assert exc.value.code != 0
    assert 'EDH' in capsys.readouterr().err
    # Still in inventory (not deleted).
    _run(monkeypatch, 'list-inventory')
    assert 'Sol Ring' in capsys.readouterr().out


def test_local_save_deck_shrink_requires_confirm(
    local_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # Build a 100-card Commander deck (no prior -> never trips).
    full = _write_deck_json(tmp_path, 'EDH', 100, 'Commander')
    _run(monkeypatch, 'save-deck', '--from-json', str(full))
    capsys.readouterr()

    # Now attempt to save a 98-card version -> shrink under target -> aborts.
    shrunk = _write_deck_json(tmp_path, 'EDH', 98, 'Commander')
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'save-deck', '--from-json', str(shrunk))
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert '--confirm' in err
    assert 'below its target of 100' in err

    # With --confirm it proceeds.
    _run(monkeypatch, 'save-deck', '--from-json', str(shrunk), '--confirm')
    assert 'save-deck: EDH' in capsys.readouterr().out


def test_local_save_deck_build_never_trips(
    local_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # Creating a deck below target (0 -> 40) is a BUILD, not a shrink -> no confirm.
    building = _write_deck_json(tmp_path, 'WIP', 40, 'Commander')
    _run(monkeypatch, 'save-deck', '--from-json', str(building))
    assert 'save-deck: WIP' in capsys.readouterr().out
