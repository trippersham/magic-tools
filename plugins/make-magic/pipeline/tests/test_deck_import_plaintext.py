"""Plaintext / Forge ``.dck`` deck-import adapter (Phase 1).

OFFLINE + deterministic: fixtures on disk under ``tests/fixtures/deck_import/``,
an isolated tmp data root (``MAKE_MAGIC_DATA_DIR``) for the permanent paste cache,
no network. Covers:

    - ``matches``: True for a ``.dck`` path, ``-`` (stdin), and raw list text;
      False for archidekt/edhrec/moxfield deck-host URLs (later adapters own those);
    - ``fetch`` on each good fixture -> a ``RawDeck`` with the right entries,
      quantities, commander role, sideboard role; ``.dck`` name from ``Name=``;
    - Moxfield ``*CMDR*`` promotes to commander; ``*F*``/other ``*..*`` tags stripped;
    - a zero-card-line blob -> ``CollectionError``;
    - ``import_deck(<fixture path>)`` routes here through the registry -> a ``Deck``;
    - the paste cache is PERMANENT: a second ``fetch`` of the same text does NOT
      re-parse (a spy proves it); ``refresh=True`` re-parses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection.errors import CollectionError
from pipeline.contracts import Deck
from pipeline.sources.deck_import import DeckImporter, import_deck
from pipeline.sources.deck_import.plaintext import PlaintextImporter

FIXTURES = Path(__file__).parent / 'fixtures' / 'deck_import'


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the permanent paste cache at an isolated tmp data root."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


# --------------------------------------------------------------------------- #
# Protocol + registration
# --------------------------------------------------------------------------- #


def test_importer_satisfies_protocol() -> None:
    assert isinstance(PlaintextImporter(), DeckImporter)


def test_registered_under_source_key() -> None:
    from pipeline.sources import deck_import

    assert isinstance(deck_import._IMPORTERS.get('plaintext'), PlaintextImporter)


# --------------------------------------------------------------------------- #
# matches
# --------------------------------------------------------------------------- #


def test_matches_dck_path() -> None:
    assert PlaintextImporter().matches(str(FIXTURES / 'forge_deck.dck')) is True


def test_matches_stdin_sentinel() -> None:
    assert PlaintextImporter().matches('-') is True


def test_matches_existing_file_path(tmp_path: Path) -> None:
    p = tmp_path / 'deck.txt'
    p.write_text('1 Sol Ring\n', encoding='utf-8')
    assert PlaintextImporter().matches(str(p)) is True


def test_matches_raw_list_text() -> None:
    text = '1 Sol Ring\n1 Arcane Signet\n4 Lightning Bolt\n'
    assert PlaintextImporter().matches(text) is True


@pytest.mark.parametrize(
    'url',
    [
        'https://archidekt.com/decks/1',
        'https://edhrec.com/commanders/krenko-mob-boss',
        'https://moxfield.com/decks/xk8ZPcJTOkqRDLllh0LA9g',
    ],
)
def test_matches_false_for_known_hosts(url: str) -> None:
    assert PlaintextImporter().matches(url) is False


@pytest.mark.parametrize(
    'url',
    [
        'https://example.com/not-a-deck',
        'http://anything',
        'HTTPS://Example.COM/Deck/1',
        '  https://example.com/x  ',
    ],
)
def test_matches_false_for_unknown_host_urls(url: str) -> None:
    # An http(s) URL is the domain of the host adapters; an unrecognized-host URL
    # must reach NO importer (it must NOT be greedily parsed as a 1-line decklist).
    assert PlaintextImporter().matches(url) is False


def test_matches_true_for_non_url_card_line_with_url_word() -> None:
    # A URL only excludes when the WHOLE ref is the URL; a genuine decklist that
    # merely contains a card mentioning http still matches (multi-line, real cards).
    text = '1 Sol Ring\nsee https://example.com for details\n'
    assert PlaintextImporter().matches(text) is True


def test_matches_false_for_prose() -> None:
    # A prose blob with no card lines is not a decklist.
    assert PlaintextImporter().matches(FIXTURES.joinpath('malformed.txt').read_text(encoding='utf-8')) is False


def test_registry_rejects_unknown_host_url() -> None:
    # Against the REAL (fully-registered) registry: an unknown-host URL matches no
    # adapter -> get_importer raises ValueError naming the supported sources. This
    # is the bug's root: plaintext must NOT greedily claim a bare URL string.
    from pipeline.sources.deck_import import get_importer

    with pytest.raises(ValueError, match=r'no importer matches.*supported sources'):
        get_importer('https://example.com/not-a-deck')


# --------------------------------------------------------------------------- #
# fetch — plain list with a Commander: section
# --------------------------------------------------------------------------- #


def test_fetch_plain_list(data_dir: Path) -> None:
    raw = PlaintextImporter().fetch(str(FIXTURES / 'plain_list.txt'))
    assert raw.source == 'plaintext'
    by_name = {c.name: c for c in raw.cards}
    assert by_name['Krenko, Mob Boss'].role == 'commander'
    assert by_name['Sol Ring'].quantity == 1
    assert by_name['Arcane Signet'].quantity == 1  # 1x form
    assert by_name['Lightning Bolt'].quantity == 4
    assert by_name['Goblin Bushwhacker'].quantity == 1  # bare name -> qty 1
    assert by_name['Mountain'].quantity == 20
    # Only the commander carries the commander role; the rest are maindeck.
    assert [c.name for c in raw.cards if c.role == 'commander'] == ['Krenko, Mob Boss']
    assert all(c.role is None for c in raw.cards if c.name != 'Krenko, Mob Boss')


# --------------------------------------------------------------------------- #
# fetch — Forge .dck
# --------------------------------------------------------------------------- #


def test_fetch_forge_dck(data_dir: Path) -> None:
    raw = PlaintextImporter().fetch(str(FIXTURES / 'forge_deck.dck'))
    assert raw.name == 'Krenko Goblins'  # from Name=
    by_name = {c.name: c for c in raw.cards}
    assert by_name['Krenko, Mob Boss'].role == 'commander'
    assert by_name['Lightning Bolt'].quantity == 4
    assert by_name['Mountain'].quantity == 20
    assert by_name['Goblin Bushwhacker'].role == 'sideboard'
    assert by_name['Sol Ring'].role is None


# --------------------------------------------------------------------------- #
# fetch — Moxfield export inline markers
# --------------------------------------------------------------------------- #


def test_fetch_moxfield_markers(data_dir: Path) -> None:
    raw = PlaintextImporter().fetch(str(FIXTURES / 'moxfield_export.txt'))
    by_name = {c.name: c for c in raw.cards}
    # *CMDR* promotes + is stripped from the name.
    assert 'Krenko, Mob Boss' in by_name
    assert by_name['Krenko, Mob Boss'].role == 'commander'
    # *F* (foil) and other tags are stripped from names, role unaffected.
    assert 'Sol Ring' in by_name
    assert by_name['Sol Ring'].role is None
    assert by_name['Lightning Bolt'].quantity == 4
    # No name retains a leftover '*'.
    assert all('*' not in c.name for c in raw.cards)


# --------------------------------------------------------------------------- #
# fetch — malformed blob (zero card lines)
# --------------------------------------------------------------------------- #


def test_fetch_malformed_raises(data_dir: Path) -> None:
    with pytest.raises(CollectionError, match='no card lines'):
        PlaintextImporter().fetch(str(FIXTURES / 'malformed.txt'))


def test_fetch_raw_text_treated_as_deck(data_dir: Path) -> None:
    # A raw multi-line string (not a file path) is parsed as the deck text itself.
    text = 'Commander:\n1 Krenko, Mob Boss\n\n1 Sol Ring\n'
    raw = PlaintextImporter().fetch(text)
    by_name = {c.name: c for c in raw.cards}
    assert by_name['Krenko, Mob Boss'].role == 'commander'
    assert by_name['Sol Ring'].role is None


# --------------------------------------------------------------------------- #
# normalize + end-to-end import_deck through the registry
# --------------------------------------------------------------------------- #


def test_import_deck_routes_to_plaintext(data_dir: Path) -> None:
    deck = import_deck(str(FIXTURES / 'forge_deck.dck'))
    assert isinstance(deck, Deck)
    assert deck.name == 'Krenko Goblins'
    assert [c.name for c in deck.commanders] == ['Krenko, Mob Boss']
    assert [c.name for c in deck.sideboard] == ['Goblin Bushwhacker']
    by_name = {c.name: c for c in deck.cards}
    assert by_name['Mountain'].quantity == 20


# --------------------------------------------------------------------------- #
# cache: permanent paste (no re-parse on hit; refresh re-parses)
# --------------------------------------------------------------------------- #


def test_permanent_cache_hit_does_not_reparse(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = '1 Sol Ring\n1 Arcane Signet\n4 Lightning Bolt\n'
    imp = PlaintextImporter()

    calls = {'n': 0}
    original = imp._parse

    def spy(deck_text: str) -> object:
        calls['n'] += 1
        return original(deck_text)

    monkeypatch.setattr(imp, '_parse', spy)

    first = imp.fetch(text)
    assert calls['n'] == 1
    second = imp.fetch(text)
    assert calls['n'] == 1  # permanent cache hit -> no re-parse
    assert {(c.name, c.quantity) for c in first.cards} == {(c.name, c.quantity) for c in second.cards}


def test_refresh_reparses(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = '1 Sol Ring\n'
    imp = PlaintextImporter()

    calls = {'n': 0}
    original = imp._parse

    def spy(deck_text: str) -> object:
        calls['n'] += 1
        return original(deck_text)

    monkeypatch.setattr(imp, '_parse', spy)

    imp.fetch(text)
    assert calls['n'] == 1
    imp.fetch(text, refresh=True)
    assert calls['n'] == 2  # refresh forces a re-parse
