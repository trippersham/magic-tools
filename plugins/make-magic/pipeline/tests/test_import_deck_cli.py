"""Tests for the ``collection import-deck`` CLI verb (Phase 5).

OFFLINE + deterministic: a tmp ``MAKE_MAGIC_DATA_DIR`` + local backend (so the
ephemeral draft lands in a real tmp DecksStore), a stub card resolver (so
``--commander`` canonicalization needs no Scryfall/scripts edge), and every HTTP
boundary injected (Archidekt/EDHREC/Moxfield ``_fetch*`` seams monkeypatched to
serve a captured fixture). Plaintext / ``-`` need no network.

Drives the verb through ``run.main`` (the dispatcher + exception-translation
path) and directly via ``run._import_deck`` where a return value / state is the
assertion. Covers: plaintext file import lands a draft with the right summary; the
``-`` stdin path; ``--name`` / ``--commander`` overrides; the ``--source edhrec``
bare-name path; the Moxfield WAF actionable error (exit 1, no traceback); an
unknown ref -> a translated ``CollectionError`` (exit 1); and ``--refresh``
threading through to the underlying fetch.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from pipeline import store
from pipeline.collection import run as cli
from pipeline.contracts import Card
from pipeline.decks import DecksStore

FIXTURES = Path(__file__).parent / 'fixtures' / 'deck_import'
PLAIN_LIST = FIXTURES / 'plain_list.txt'
FORGE_DCK = FIXTURES / 'forge_deck.dck'
AYARA = FIXTURES / 'edhrec_ayara.json'


class _StubResolver:
    """A resolver that canonicalizes the two card names the tests exercise."""

    _CANON: ClassVar[dict[str, str]] = {'krenko, mob boss': 'Krenko, Mob Boss', 'sol ring': 'Sol Ring'}

    def get_card(self, name: str) -> Card | None:
        canon = self._CANON.get(name.strip().lower())
        return Card(name=canon) if canon is not None else None


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp data root + local backend + stub resolver (no network, no scripts edge)."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _StubResolver())
    return root


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _only_draft() -> Any:
    """The single ephemeral draft in the tmp store (fails loudly on 0 / >1)."""
    decks = DecksStore().list()
    assert len(decks) == 1, f'expected exactly one draft, found {len(decks)}'
    return decks[0]


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


def test_import_deck_verb_registered() -> None:
    assert 'import-deck' in cli.VERBS


# --------------------------------------------------------------------------- #
# plaintext file + stdin
# --------------------------------------------------------------------------- #


def test_import_plaintext_file_lands_draft(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'import-deck', str(PLAIN_LIST))
    out = capsys.readouterr().out
    deck = _only_draft()
    # 5 distinct maindeck entries (Sol Ring, Arcane Signet, Lightning Bolt,
    # Goblin Bushwhacker, Mountain) + 1 commander (Krenko).
    assert len(deck.maindeck) == 5
    assert len(deck.commanders) == 1
    assert deck.commanders[0].name == 'Krenko, Mob Boss'
    assert out.startswith('Imported [ephemeral]: ')
    # The summary sums QUANTITIES, not distinct entries: 1 Sol Ring + 1 Arcane
    # Signet + 4 Lightning Bolt + 1 Goblin Bushwhacker + 20 Mountain = 27 cards.
    assert '27 maindeck' in out
    assert '1 commander' in out


def test_import_dck_reports_sideboard(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'import-deck', str(FORGE_DCK))
    out = capsys.readouterr().out
    deck = _only_draft()
    assert len(deck.sideboard) == 1
    assert 'sideboard' in out


def test_import_stdin_dash(data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    pasted = '1 Sol Ring\n1 Krenko, Mob Boss *CMDR*\n'
    monkeypatch.setattr('sys.stdin', io.StringIO(pasted))
    _run(monkeypatch, 'import-deck', '-')
    deck = _only_draft()
    assert len(deck.commanders) == 1
    assert deck.commanders[0].name == 'Krenko, Mob Boss'
    assert len(deck.maindeck) == 1


# --------------------------------------------------------------------------- #
# --name / --commander overrides
# --------------------------------------------------------------------------- #


def test_name_override(data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _run(monkeypatch, 'import-deck', str(PLAIN_LIST), '--name', 'My Cool Deck')
    deck = _only_draft()
    assert deck.name == 'My Cool Deck'


def test_commander_promotes_existing_maindeck_card(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # A no-commander list (raw lines) where Sol Ring is present in the maindeck;
    # --commander "sol ring" canonicalizes to "Sol Ring" and PROMOTES that entry.
    pasted = '1 Sol Ring\n1 Lightning Bolt\n'
    monkeypatch.setattr('sys.stdin', io.StringIO(pasted))
    _run(monkeypatch, 'import-deck', '-', '--commander', 'sol ring')
    deck = _only_draft()
    assert len(deck.commanders) == 1
    assert deck.commanders[0].name == 'Sol Ring'
    # promoted, not duplicated: Sol Ring must not also remain in the maindeck.
    assert all(c.name != 'Sol Ring' for c in deck.maindeck)


def test_commander_adds_when_absent(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    pasted = '1 Lightning Bolt\n1 Mountain\n'
    monkeypatch.setattr('sys.stdin', io.StringIO(pasted))
    _run(monkeypatch, 'import-deck', '-', '--commander', 'krenko, mob boss')
    deck = _only_draft()
    assert len(deck.commanders) == 1
    assert deck.commanders[0].name == 'Krenko, Mob Boss'


# --------------------------------------------------------------------------- #
# --source edhrec bare name (the P5 bare-name path)
# --------------------------------------------------------------------------- #


def test_source_edhrec_bare_name(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    payload = _load(AYARA)
    # Inject the one EDHREC HTTP seam on the REGISTERED singleton (the instance
    # import_deck dispatches to) so no live call is made; assert the derived slug
    # reaches it (proving the bare-name -> edhrec_slug path).
    from pipeline.sources.deck_import import _IMPORTERS

    seen: dict[str, str] = {}

    def _fake_fetch_json(slug: str) -> dict[str, Any]:
        seen['slug'] = slug
        return payload

    monkeypatch.setattr(_IMPORTERS['edhrec'], '_fetch_json', _fake_fetch_json)
    _run(monkeypatch, 'import-deck', '--source', 'edhrec', 'Ayara, First of Locthwain')
    deck = _only_draft()
    assert seen['slug'] == 'ayara-first-of-locthwain'
    assert len(deck.commanders) == 1
    assert deck.commanders[0].name == 'Ayara, First of Locthwain'


# --------------------------------------------------------------------------- #
# error paths (clean one-liners via main() -> SystemExit(1), no traceback)
# --------------------------------------------------------------------------- #


def test_moxfield_waf_actionable_error(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import httpx

    from pipeline.sources.deck_import import _IMPORTERS

    block = (FIXTURES / 'moxfield_waf_403.html').read_text(encoding='utf-8')

    def _fake_fetch(deck_id: str) -> httpx.Response:
        return httpx.Response(status_code=403, text=block)

    monkeypatch.setattr(_IMPORTERS['moxfield'], '_fetch', _fake_fetch)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'import-deck', 'https://moxfield.com/decks/abc123DEF456ghi789JKL0')
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert 'Moxfield blocks automated reads' in err
    assert 'Traceback' not in err
    assert DecksStore().list() == []


def test_unknown_ref_translates_to_collection_error(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # A comment-only garbage ref matches NO adapter (no host, not a file, no card
    # line) -> get_importer raises ValueError -> translated to a clean
    # CollectionError naming the supported sources.
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'import-deck', '// not a deck')
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith('error: ')
    assert 'Traceback' not in err
    # names the supported sources
    assert 'archidekt' in err and 'edhrec' in err and 'moxfield' in err and 'plaintext' in err
    assert DecksStore().list() == []


def test_fetch_validationerror_is_not_masked_as_collection_error(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A genuine adapter/model bug during fetch/normalize (here a pydantic
    # ValidationError, which subclasses ValueError) must PROPAGATE as itself — not be
    # caught and re-raised as a clean "unknown source" CollectionError. Only the
    # importer-SELECTION step translates ValueError; fetch/normalize bugs traceback.
    from pydantic import ValidationError

    from pipeline.contracts import DeckCard
    from pipeline.sources.deck_import import _IMPORTERS

    class _BuggyImporter:
        # Registered under a VALID source key ('edhrec') so it is SELECTED by
        # get_importer without argparse rejecting --source; its fetch then raises.
        source = 'edhrec'

        def matches(self, ref: str) -> bool:
            return False

        def fetch(self, ref: str, *, refresh: bool = False) -> Any:
            # Simulate a model bug (a bad quantity fails the ge=1 guard) so a real
            # pydantic ValidationError escapes the adapter.
            return DeckCard(name='X', quantity=0)  # invalid: quantity ge=1

        def normalize(self, raw: Any) -> Any:  # pragma: no cover - never reached
            raise AssertionError('normalize should not run once fetch raises')

    monkeypatch.setitem(_IMPORTERS, 'edhrec', _BuggyImporter())  # type: ignore[arg-type]
    # NOT a SystemExit(1)/CollectionError: the ValidationError propagates raw
    # (a genuine defect must traceback, not be masked as "unknown source").
    with pytest.raises(ValidationError):
        cli._import_deck(['--source', 'edhrec', 'Ayara, First of Locthwain'])
    assert DecksStore().list() == []


# --------------------------------------------------------------------------- #
# --refresh threads through to fetch
# --------------------------------------------------------------------------- #


def test_refresh_threads_through(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from pipeline.sources.deck_import import _IMPORTERS

    seen: dict[str, bool] = {}

    def _fake_fetch(ref: str, *, refresh: bool = False) -> Any:
        from datetime import UTC, datetime

        from pipeline.sources.deck_import.raw import RawDeck, RawEntry

        seen['refresh'] = refresh
        return RawDeck(
            name='Ayara, First of Locthwain',
            cards=[RawEntry(name='Swamp', quantity=28, role=None)],
            source='edhrec',
            source_ref='ayara-first-of-locthwain',
            fetched_at=datetime.now(tz=UTC),
        )

    monkeypatch.setattr(_IMPORTERS['edhrec'], 'fetch', _fake_fetch)
    _run(monkeypatch, 'import-deck', '--source', 'edhrec', 'Ayara, First of Locthwain', '--refresh')
    assert seen['refresh'] is True
