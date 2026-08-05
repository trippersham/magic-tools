"""Tests for the collection CLI dispatcher verbs.

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
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _StubResolver())
    return root


@pytest.fixture()
def unonboarded_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp data dir with NO backend env / creds — the un-onboarded state."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.delenv('MAKE_MAGIC_BACKEND', raising=False)
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _StubResolver())
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

    # `commanders` is a derived list[DeckCard] — it must serialize, since
    # DeckCard isn't natively JSON-serializable by the handler's plain json.dumps.
    _run(monkeypatch, 'get-deck', 'Gruul Aggro', '--field', 'commanders')
    commanders = json.loads(capsys.readouterr().out)
    assert [c['name'] for c in commanders] == ['Grumgully, the Generous']

    _run(monkeypatch, 'list-decks')
    assert 'Gruul Aggro' in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# list-decks: draft-aware union (source [synced] + local ephemeral [ephemeral])
# + archive lifecycle verbs
# --------------------------------------------------------------------------- #


def _save_a_source_deck(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    """Save a deck through the CLI so it lands in the LocalYamlStore source."""
    deck_json = tmp_path / f'{name}.json'
    deck_json.write_text(json.dumps({'name': name, 'cards': [{'name': 'Sol Ring', 'role': 'commander'}]}))
    _run(monkeypatch, 'save-deck', '--from-json', str(deck_json))


def test_archive_verbs_registered() -> None:
    assert 'archive-deck' in cli.VERBS
    assert 'unarchive-deck' in cli.VERBS


def test_list_decks_unions_source_synced_and_local_ephemeral(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    from pipeline.contracts import Deck, DeckCard
    from pipeline.decks import DecksStore

    # A source-backed deck (saved through the source) + a purely-local ephemeral draft.
    _save_a_source_deck(monkeypatch, tmp_path, 'Sourced')
    DecksStore().create_ephemeral(
        Deck(name='Draft', cards=[DeckCard(name='Sol Ring', role='commander')])
    )
    capsys.readouterr()

    _run(monkeypatch, 'list-decks', '--json')
    rows = json.loads(capsys.readouterr().out)
    by_name = {r['name']: r['status'] for r in rows}
    assert by_name['Sourced'] == 'synced'
    assert by_name['Draft'] == 'ephemeral'

    # Line output carries the marker suffix but keeps the name readable at line start.
    _run(monkeypatch, 'list-decks')
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(ln.startswith('Sourced') and '[synced]' in ln for ln in lines)
    assert any(ln.startswith('Draft') and '[ephemeral]' in ln for ln in lines)


def test_list_decks_hides_archived_ephemeral_by_default(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from pipeline.contracts import Deck, DeckCard
    from pipeline.decks import DecksStore

    s = DecksStore()
    # create_ephemeral mints each draft's deck_uuid; name-addressed archive-deck
    # resolves NAME -> uuid via the store shim, so seeding by name is enough.
    s.create_ephemeral(Deck(name='Kept Draft', cards=[DeckCard(name='Sol Ring', role='commander')]))
    s.create_ephemeral(Deck(name='Junk Draft', cards=[DeckCard(name='Sol Ring', role='commander')]))
    _run(monkeypatch, 'archive-deck', 'Junk Draft')
    capsys.readouterr()

    _run(monkeypatch, 'list-decks', '--json')
    names = {r['name'] for r in json.loads(capsys.readouterr().out)}
    assert 'Kept Draft' in names
    assert 'Junk Draft' not in names

    # --archived surfaces it with the archived marker.
    _run(monkeypatch, 'list-decks', '--json', '--archived')
    rows = {r['name']: r['status'] for r in json.loads(capsys.readouterr().out)}
    assert rows['Junk Draft'] == 'ephemeral'
    _run(monkeypatch, 'list-decks', '--archived')
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(ln.startswith('Junk Draft') and '[ephemeral,archived]' in ln for ln in lines)


def test_archive_unarchive_roundtrip(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from pipeline.contracts import Deck, DeckCard
    from pipeline.decks import DecksStore

    DecksStore().create_ephemeral(Deck(name='Junk', cards=[DeckCard(name='Sol Ring', role='commander')]))
    _run(monkeypatch, 'archive-deck', 'Junk')
    assert 'Junk' in capsys.readouterr().out
    _run(monkeypatch, 'list-decks', '--json')
    assert 'Junk' not in {r['name'] for r in json.loads(capsys.readouterr().out)}

    _run(monkeypatch, 'unarchive-deck', 'Junk')
    assert 'Junk' in capsys.readouterr().out
    _run(monkeypatch, 'list-decks', '--json')
    assert 'Junk' in {r['name'] for r in json.loads(capsys.readouterr().out)}


def test_chase_add_list_remove(data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _run(monkeypatch, 'add-chase', 'The One Ring', '--for-deck', 'gruul')
    capsys.readouterr()
    _run(monkeypatch, 'list-chase')
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['name'] == 'The One Ring'
    _run(monkeypatch, 'remove-chase', 'The One Ring')
    capsys.readouterr()
    _run(monkeypatch, 'list-chase')
    assert json.loads(capsys.readouterr().out) == []


def test_log_trade_then_list(data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _run(monkeypatch, 'log-trade', '--from-source', 'Library', '--to-destination', 'Deck', '--status', 'Draft')
    capsys.readouterr()
    _run(monkeypatch, 'list-trades')
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['from_source'] == 'Library'
    assert rows[0]['status'] == 'Draft'


def test_log_trade_deck_flags(data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """--from-deck / --to-deck wire to the Trade contract (no --from-json)."""
    _run(
        monkeypatch,
        'log-trade',
        '--from-source',
        'Deck',
        '--to-destination',
        'Deck',
        '--from-deck',
        'Gruul Aggro',
        '--to-deck',
        'Mono Blue',
    )
    capsys.readouterr()
    _run(monkeypatch, 'list-trades')
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['from_deck'] == 'Gruul Aggro'
    assert rows[0]['to_deck'] == 'Mono Blue'


# --------------------------------------------------------------------------- #
# onboarding: status nags un-onboarded, onboard persists + silences the nag
# --------------------------------------------------------------------------- #


def test_status_nags_when_unonboarded(
    unonboarded_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'status')
    out = json.loads(capsys.readouterr().out)
    assert out['backend'] == 'local'  # defaults to local, nothing hard-blocks
    assert out['needs_onboarding'] is True
    assert 'onboard' in out['message']


def test_onboard_persists_and_silences_nag(
    unonboarded_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'onboard', '--backend', 'local')
    assert 'local' in capsys.readouterr().out
    _run(monkeypatch, 'status')
    out = json.loads(capsys.readouterr().out)
    assert out['backend'] == 'local'
    assert out['needs_onboarding'] is False
    assert 'message' not in out


def test_onboard_verb_registered() -> None:
    assert 'onboard' in cli.VERBS
    assert 'copy' in cli.VERBS


# --------------------------------------------------------------------------- #
# copy verb: local -> local is trivially exercisable; --to airtable needs confirm
# --------------------------------------------------------------------------- #


def test_copy_to_airtable_refuses_without_confirm(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'copy', '--from', 'local', '--to', 'airtable')
    assert ei.value.code != 0
    err = capsys.readouterr().err
    assert '--confirm' in err


def test_copy_local_to_local(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _run(monkeypatch, 'add-card', 'Sol Ring', '--qty', '2')
    capsys.readouterr()
    # copy local -> local into a SECOND data dir (dest env override).
    dest_dir = tmp_path / 'dest'
    _run(monkeypatch, 'copy', '--from', 'local', '--to', 'local', '--dest-data-dir', str(dest_dir))
    report = json.loads(capsys.readouterr().out)
    assert report['inventory'] == 1


def test_unknown_verb_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('sys.argv', ['collection', 'bogus'])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2


def test_unknown_deck_prints_clean_error_no_traceback(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An expected failure (unknown deck) surfaces `error: …` on stderr + exit 1,
    NOT a raw FileNotFoundError traceback."""
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'get-deck', 'Nope')
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith('error: ')
    assert 'Traceback' not in err


def test_unknown_field_prints_clean_error(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """`get-deck --field <bogus>` gives a clean, actionable error (not AttributeError)."""
    deck_json = tmp_path / 'd.json'
    deck_json.write_text('{"name":"D","cards":[{"name":"Sol Ring","role":"commander"}]}')
    _run(monkeypatch, 'save-deck', '--from-json', str(deck_json))
    capsys.readouterr()
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'get-deck', 'D', '--field', 'bogusfield')
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert 'unknown deck field' in err
    assert 'Traceback' not in err


def test_genuine_keyerror_is_not_swallowed(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real defect (raw KeyError from a verb handler) must NOT be flattened to a
    clean `error:` line — it should propagate so the bug is visible."""

    def _boom(_argv: list[str]) -> None:
        raise KeyError('internal invariant broken')

    monkeypatch.setitem(cli.VERBS, 'list-decks', _boom)
    with pytest.raises(KeyError, match='internal invariant broken'):
        _run(monkeypatch, 'list-decks')


def test_genuine_runtimeerror_is_not_swallowed(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw RuntimeError from a bug is not masked by the CLI wrapper."""

    def _boom(_argv: list[str]) -> None:
        raise RuntimeError('unexpected')

    monkeypatch.setitem(cli.VERBS, 'list-decks', _boom)
    with pytest.raises(RuntimeError, match='unexpected'):
        _run(monkeypatch, 'list-decks')


def test_save_deck_bad_json_prints_clean_error(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """Bad user JSON for save-deck is user input -> clean `error:` line, no traceback."""
    bad = tmp_path / 'bad.json'
    bad.write_text('{"name": 123, "cards": "not-a-list"')  # invalid + malformed
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'save-deck', '--from-json', str(bad))
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith('error: ')
    assert 'Traceback' not in err
