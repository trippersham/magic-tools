"""Tests for the collection CLI dispatcher verbs (Phase 2A, verb surface).

OFFLINE: a tmp MAKE_MAGIC_DATA_DIR + local backend + a stub resolver (patched in
so no scripts/ import or Scryfall cache is needed). Asserts the FULL verb surface
the three skills call dispatches correctly and prints stable/parseable output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection import run as cli
from pipeline.contracts import Card


class _StubResolver:
    def get_card(self, name: str) -> Card | None:
        return None


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.setattr(cli, '_load_resolver', lambda: _StubResolver())
    return root


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


def test_all_expected_verbs_registered() -> None:
    expected = {
        'get-deck',
        'list-decks',
        'save-deck',
        'set-strategy',
        'set-assessment',
        'set-focus-otags',
        'list-inventory',
        'add-card',
        'set-quantity',
        'remove-card',
        'list-chase',
        'add-chase',
        'remove-chase',
        'list-trades',
        'log-trade',
        'factsheet',
        'status',
    }
    assert expected <= set(cli.VERBS)


def test_status_prints_backend(data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _run(monkeypatch, 'status')
    out = json.loads(capsys.readouterr().out)
    assert out['backend'] == 'local'


def test_add_card_then_list_inventory(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'add-card', 'Sol Ring', '--qty', '2', '--condition', 'NM')
    capsys.readouterr()
    _run(monkeypatch, 'list-inventory')
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['name'] == 'Sol Ring'
    assert rows[0]['owned'] == 2


def test_set_quantity_and_remove_card(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'add-card', 'Sol Ring')
    _run(monkeypatch, 'set-quantity', 'Sol Ring', '5')
    capsys.readouterr()
    _run(monkeypatch, 'list-inventory')
    assert json.loads(capsys.readouterr().out)[0]['owned'] == 5
    _run(monkeypatch, 'remove-card', 'Sol Ring')
    capsys.readouterr()
    _run(monkeypatch, 'list-inventory')
    assert json.loads(capsys.readouterr().out) == []


def test_save_deck_then_get_and_field_verbs(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    deck_json = tmp_path / 'deck.json'
    deck_json.write_text(
        json.dumps(
            {
                'name': 'Gruul Aggro',
                'strategy': 'Go-wide.',
                'cards': [
                    {'name': 'Grumgully, the Generous', 'role': 'commander'},
                    {'name': 'Sol Ring'},
                ],
            }
        )
    )
    _run(monkeypatch, 'save-deck', '--from-json', str(deck_json))
    _run(monkeypatch, 'set-assessment', 'Gruul Aggro', 'Reality synthesis.')
    _run(monkeypatch, 'set-focus-otags', 'Gruul Aggro', 'sacrifice', 'tokens')
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Gruul Aggro', '--field', 'assessment')
    assert capsys.readouterr().out.strip() == 'Reality synthesis.'

    _run(monkeypatch, 'get-deck', 'Gruul Aggro', '--field', 'focus_otags')
    assert json.loads(capsys.readouterr().out) == ['sacrifice', 'tokens']

    _run(monkeypatch, 'list-decks')
    assert 'Gruul Aggro' in capsys.readouterr().out


def test_chase_add_list_remove(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'add-chase', 'The One Ring', '--for-deck', 'gruul')
    capsys.readouterr()
    _run(monkeypatch, 'list-chase')
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['name'] == 'The One Ring'
    _run(monkeypatch, 'remove-chase', 'The One Ring')
    capsys.readouterr()
    _run(monkeypatch, 'list-chase')
    assert json.loads(capsys.readouterr().out) == []


def test_log_trade_then_list(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'log-trade', '--from-source', 'Library', '--to-destination', 'Deck', '--status', 'Draft')
    capsys.readouterr()
    _run(monkeypatch, 'list-trades')
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['from_source'] == 'Library'
    assert rows[0]['status'] == 'Draft'


def test_unknown_verb_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('sys.argv', ['collection', 'bogus'])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2
