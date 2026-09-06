"""Archidekt deck-import adapter (Phase 2).

OFFLINE + deterministic: real Archidekt API responses captured to disk under
``tests/fixtures/deck_import/`` (``curl`` at capture time), an isolated tmp data
root (``MAKE_MAGIC_DATA_DIR``) for the TTL cache, and the network GET injected so
NO live call is made in a test. The injection seam is
:meth:`ArchidektImporter._fetch_json` — a test monkeypatches it (per-instance) to
return the parsed fixture JSON; P3/P4 reuse the same "monkeypatch the one HTTP
method" shape.

Covers:

    - ``matches``: True for an archidekt.com deck URL and a bare numeric id;
      False for a ``.dck`` path, an edhrec URL, and a moxfield URL;
    - id extraction from the several ref forms;
    - ``fetch`` on the Myrel fixture (deck 10126962) -> a ``RawDeck`` of 99
      maindeck + 1 commander (``Myrel, Shield of Argive``), ``edhBracket`` in
      ``meta``, and the ``Maybeboard`` / ``Tokens & Extras`` categories SKIPPED;
    - ``normalize`` -> a ``Deck`` (delegates to the shared helper);
    - the size-sanity warning FIRES (caplog) on the off-100 Light-Paws fixture
      (deck 3714827 -> 122 main + 1 cmd = 123) and does NOT fire on Myrel (100);
    - ``import_deck('https://archidekt.com/decks/10126962/')`` routes here through
      the registry -> a ``Deck``;
    - a non-200 / missing deck -> ``CollectionError`` (clean, no traceback).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import store
from pipeline.collection.errors import CollectionError
from pipeline.contracts import Deck
from pipeline.sources.deck_import import DeckImporter, import_deck
from pipeline.sources.deck_import.archidekt import ArchidektImporter

FIXTURES = Path(__file__).parent / 'fixtures' / 'deck_import'
MYREL = FIXTURES / 'archidekt_myrel_10126962.json'
LIGHTPAWS = FIXTURES / 'archidekt_lightpaws_3714827.json'


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the TTL cache at an isolated tmp data root."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _importer_with(fixture: Path) -> ArchidektImporter:
    """An ArchidektImporter whose one HTTP method serves ``fixture`` (no network)."""
    imp = ArchidektImporter()
    payload = _load(fixture)
    imp._fetch_json = lambda deck_id: payload  # type: ignore[method-assign]
    return imp


# --------------------------------------------------------------------------- #
# Protocol + registration
# --------------------------------------------------------------------------- #


def test_importer_satisfies_protocol() -> None:
    assert isinstance(ArchidektImporter(), DeckImporter)


def test_registered_under_source_key() -> None:
    from pipeline.sources import deck_import

    assert isinstance(deck_import._IMPORTERS.get('archidekt'), ArchidektImporter)


# --------------------------------------------------------------------------- #
# matches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    'ref',
    [
        'https://archidekt.com/decks/10126962/',
        'https://archidekt.com/decks/10126962/myrel-tribal-tokens',
        'archidekt.com/decks/10126962',
        'http://www.archidekt.com/decks/3714827/',
        '10126962',  # bare numeric id
    ],
)
def test_matches_archidekt_refs(ref: str) -> None:
    assert ArchidektImporter().matches(ref) is True


@pytest.mark.parametrize(
    'ref',
    [
        'some_deck.dck',
        '/tmp/decklist.dck',
        'https://edhrec.com/commanders/krenko-mob-boss',
        'https://moxfield.com/decks/xk8ZPcJTOkqRDLllh0LA9g',
        '1 Sol Ring',  # a plaintext card line, not a bare id
        'krenko-mob-boss',  # a non-numeric slug
    ],
)
def test_matches_false_for_non_archidekt(ref: str) -> None:
    assert ArchidektImporter().matches(ref) is False


# --------------------------------------------------------------------------- #
# id extraction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('ref', 'expected'),
    [
        ('https://archidekt.com/decks/10126962/', '10126962'),
        ('https://archidekt.com/decks/10126962/myrel-tribal', '10126962'),
        ('archidekt.com/decks/3714827', '3714827'),
        ('10126962', '10126962'),
    ],
)
def test_extract_id(ref: str, expected: str) -> None:
    assert ArchidektImporter()._extract_id(ref) == expected


def test_extract_id_rejects_non_archidekt() -> None:
    with pytest.raises(CollectionError, match='not an Archidekt'):
        ArchidektImporter()._extract_id('https://example.com/decks/1')


# --------------------------------------------------------------------------- #
# fetch — the maindeck / commander / skip rule (Myrel: 99 + 1)
# --------------------------------------------------------------------------- #


def test_fetch_myrel_99_main_1_commander(data_dir: Path) -> None:
    raw = _importer_with(MYREL).fetch('https://archidekt.com/decks/10126962/')
    assert raw.source == 'archidekt'
    assert raw.source_ref == '10126962'
    assert raw.name == 'Myrel, Shield of Argive: Tribal & Tokens'

    commanders = [c for c in raw.cards if c.role == 'commander']
    maindeck = [c for c in raw.cards if c.role is None]
    assert [c.name for c in commanders] == ['Myrel, Shield of Argive']
    assert sum(c.quantity for c in commanders) == 1
    assert sum(c.quantity for c in maindeck) == 99


def test_fetch_myrel_edhbracket_in_meta(data_dir: Path) -> None:
    raw = _importer_with(MYREL).fetch('10126962')
    assert raw.meta.get('edhBracket') == 2


def test_fetch_skips_excluded_categories(data_dir: Path) -> None:
    # Maybeboard + Tokens & Extras are includedInDeck:false. The naive
    # "everything except Maybeboard" parse yields 171 lines; the category rule
    # tames it to 100 (99 main + 1 cmd). Asserting the total proves the skip.
    raw = _importer_with(MYREL).fetch('10126962')
    total = sum(c.quantity for c in raw.cards)
    assert total == 100, f'category rule should tame Myrel to 100, got {total}'


# --------------------------------------------------------------------------- #
# normalize + size-sanity warning
# --------------------------------------------------------------------------- #


def test_normalize_myrel_builds_deck(data_dir: Path) -> None:
    imp = _importer_with(MYREL)
    deck = imp.normalize(imp.fetch('10126962'))
    assert isinstance(deck, Deck)
    assert [c.name for c in deck.commanders] == ['Myrel, Shield of Argive']
    assert sum(c.quantity for c in deck.maindeck) == 99


def test_normalize_myrel_no_size_warning(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    imp = _importer_with(MYREL)
    with caplog.at_level(logging.WARNING, logger='pipeline.sources.deck_import'):
        imp.normalize(imp.fetch('10126962'))
    assert not [r for r in caplog.records if 'size' in r.getMessage().lower()]


def test_normalize_lightpaws_fires_size_warning(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    imp = _importer_with(LIGHTPAWS)
    with caplog.at_level(logging.WARNING, logger='pipeline.sources.deck_import'):
        deck = imp.normalize(imp.fetch('3714827'))
    # Light-Paws under the category rule is 122 main + 1 cmd = 123 -> off 100.
    assert sum(c.quantity for c in deck.maindeck) + sum(c.quantity for c in deck.commanders) == 123
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('size' in m.lower() for m in warnings), warnings


# --------------------------------------------------------------------------- #
# end-to-end import_deck through the registry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('ref', ['10126962', 'https://archidekt.com/decks/10126962/'])
def test_registry_dispatches_to_archidekt(ref: str) -> None:
    # A bare numeric id and an archidekt URL both route here (not to plaintext,
    # which is registered first but explicitly rejects a bare id).
    from pipeline.sources.deck_import import get_importer

    assert get_importer(ref).source == 'archidekt'


def test_import_deck_routes_to_archidekt(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.sources import deck_import

    payload = _load(MYREL)
    registered = deck_import._IMPORTERS['archidekt']
    monkeypatch.setattr(registered, '_fetch_json', lambda deck_id: payload)

    deck = import_deck('https://archidekt.com/decks/10126962/')
    assert isinstance(deck, Deck)
    assert [c.name for c in deck.commanders] == ['Myrel, Shield of Argive']


# --------------------------------------------------------------------------- #
# error path — non-200 / missing deck
# --------------------------------------------------------------------------- #


def test_fetch_404_raises_collection_error(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    imp = ArchidektImporter()

    def _boom(url: str, *args: object, **kwargs: object) -> httpx.Response:
        request = httpx.Request('GET', url)
        return httpx.Response(404, request=request)

    monkeypatch.setattr(httpx, 'get', _boom)
    with pytest.raises(CollectionError, match=r'not found|unreachable|404'):
        imp.fetch('99999999')


def test_fetch_network_error_raises_collection_error(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    imp = ArchidektImporter()

    def _boom(url: str, *args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError('unreachable')

    monkeypatch.setattr(httpx, 'get', _boom)
    with pytest.raises(CollectionError, match=r'unreachable|reach'):
        imp.fetch('10126962')
