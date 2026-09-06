"""EDHREC deck-import adapter (Phase 3).

OFFLINE + deterministic: a real EDHREC average-decks JSON response captured to
disk under ``tests/fixtures/deck_import/`` (``curl`` at capture time), an isolated
tmp data root (``MAKE_MAGIC_DATA_DIR``) for the TTL cache, and the network GET
injected so NO live call is made in a test. The injection seam is
:meth:`EdhrecImporter._fetch_json` — a test monkeypatches it (per-instance) to
return the parsed fixture JSON, mirroring the Archidekt (P2) test shape.

Covers:

    - ``matches``: True for edhrec.com / json.edhrec.com URLs; False for an
      archidekt URL, a moxfield URL, a ``.dck`` path, and a plaintext card line;
    - :func:`edhrec_slug`: name -> slug rule (lower, strip apostrophes, non-alnum
      runs -> ``-``, trim) with the load-bearing examples;
    - slug/tag extraction from every accepted URL form (``average-decks`` direct,
      ``commanders`` -> ``average-decks`` mapping, the ``json.edhrec.com`` URL, and
      a trailing tag);
    - ``fetch`` on the Ayara fixture -> a ``RawDeck`` with the ``deck.cards``
      buckets flattened to maindeck, ``Swamp`` qty 28 PRESERVED, commander
      ``Ayara, First of Locthwain``;
    - ``normalize`` -> a ``Deck`` (delegates to the shared helper);
    - ``import_deck('https://edhrec.com/commanders/ayara-first-of-locthwain')``
      routes here through the registry -> a ``Deck``;
    - a bad slug (missing/empty ``deck``) -> ``CollectionError``;
    - a non-200 -> ``CollectionError`` (clean, no traceback).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import store
from pipeline.collection.errors import CollectionError
from pipeline.contracts import Deck
from pipeline.sources.deck_import import DeckImporter, import_deck
from pipeline.sources.deck_import.edhrec import EdhrecImporter, edhrec_slug

FIXTURES = Path(__file__).parent / 'fixtures' / 'deck_import'
AYARA = FIXTURES / 'edhrec_ayara.json'


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the TTL cache at an isolated tmp data root."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _importer_with(fixture: Path) -> EdhrecImporter:
    """An EdhrecImporter whose one HTTP method serves ``fixture`` (no network)."""
    imp = EdhrecImporter()
    payload = _load(fixture)
    imp._fetch_json = lambda slug: payload  # type: ignore[method-assign]
    return imp


# --------------------------------------------------------------------------- #
# Protocol + registration
# --------------------------------------------------------------------------- #


def test_importer_satisfies_protocol() -> None:
    assert isinstance(EdhrecImporter(), DeckImporter)


def test_registered_under_source_key() -> None:
    from pipeline.sources import deck_import

    assert isinstance(deck_import._IMPORTERS.get('edhrec'), EdhrecImporter)


# --------------------------------------------------------------------------- #
# matches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    'ref',
    [
        'https://edhrec.com/average-decks/ayara-first-of-locthwain',
        'https://edhrec.com/commanders/ayara-first-of-locthwain',
        'https://edhrec.com/commanders/baral-chief-of-compliance/budget',
        'edhrec.com/average-decks/ayara-first-of-locthwain',
        'https://json.edhrec.com/pages/average-decks/ayara-first-of-locthwain.json',
    ],
)
def test_matches_edhrec_refs(ref: str) -> None:
    assert EdhrecImporter().matches(ref) is True


@pytest.mark.parametrize(
    'ref',
    [
        'https://archidekt.com/decks/10126962/',
        'https://moxfield.com/decks/xk8ZPcJTOkqRDLllh0LA9g',
        'some_deck.dck',
        '1 Sol Ring',
        'ayara-first-of-locthwain',  # a bare slug/name is not claimed by matches
    ],
)
def test_matches_false_for_non_edhrec(ref: str) -> None:
    assert EdhrecImporter().matches(ref) is False


# --------------------------------------------------------------------------- #
# edhrec_slug
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ("K'rrik, Son of Yawgmoth", 'krrik-son-of-yawgmoth'),
        ('Ayara, First of Locthwain', 'ayara-first-of-locthwain'),
        ('Baral, Chief of Compliance', 'baral-chief-of-compliance'),
        ('Krenko, Mob Boss', 'krenko-mob-boss'),
        ('K’rrik, Son of Yawgmoth', 'krrik-son-of-yawgmoth'),  # noqa: RUF001 — curly apostrophe is the point
    ],
)
def test_edhrec_slug(name: str, expected: str) -> None:
    assert edhrec_slug(name) == expected


# --------------------------------------------------------------------------- #
# slug / tag extraction from every accepted URL form
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('ref', 'expected'),
    [
        ('https://edhrec.com/average-decks/ayara-first-of-locthwain', 'ayara-first-of-locthwain'),
        ('edhrec.com/average-decks/ayara-first-of-locthwain', 'ayara-first-of-locthwain'),
        ('https://edhrec.com/commanders/ayara-first-of-locthwain', 'ayara-first-of-locthwain'),
        (
            'https://json.edhrec.com/pages/average-decks/ayara-first-of-locthwain.json',
            'ayara-first-of-locthwain',
        ),
        ('https://edhrec.com/average-decks/baral-chief-of-compliance/budget', 'baral-chief-of-compliance/budget'),
        ('https://edhrec.com/commanders/baral-chief-of-compliance/budget', 'baral-chief-of-compliance/budget'),
    ],
)
def test_extract_slug(ref: str, expected: str) -> None:
    assert EdhrecImporter()._extract_slug(ref) == expected


def test_extract_slug_rejects_non_edhrec() -> None:
    with pytest.raises(CollectionError, match='not an EDHREC'):
        EdhrecImporter()._extract_slug('https://example.com/foo')


# --------------------------------------------------------------------------- #
# fetch — bucket flatten + Swamp 28 + commander
# --------------------------------------------------------------------------- #


def test_fetch_ayara_flattens_buckets(data_dir: Path) -> None:
    raw = _importer_with(AYARA).fetch('https://edhrec.com/commanders/ayara-first-of-locthwain')
    assert raw.source == 'edhrec'
    assert raw.source_ref == 'ayara-first-of-locthwain'

    commanders = [c for c in raw.cards if c.role == 'commander']
    maindeck = [c for c in raw.cards if c.role is None]
    assert [c.name for c in commanders] == ['Ayara, First of Locthwain']
    # Every non-commander bucket flattened into maindeck, real basic counts kept.
    assert sum(c.quantity for c in maindeck) == 99
    swamp = [c for c in maindeck if c.name == 'Swamp']
    assert len(swamp) == 1
    assert swamp[0].quantity == 28


def test_fetch_ayara_slug_meta(data_dir: Path) -> None:
    raw = _importer_with(AYARA).fetch('https://edhrec.com/average-decks/ayara-first-of-locthwain')
    assert raw.meta.get('slug') == 'ayara-first-of-locthwain'
    assert raw.meta.get('tag') is None


def test_fetch_tag_recorded_in_meta_and_ref(data_dir: Path) -> None:
    # The commanders-URL-with-tag maps to the average-decks slug/tag; the tag
    # rides in source_ref and meta even though the served fixture is Ayara.
    raw = _importer_with(AYARA).fetch('https://edhrec.com/commanders/baral-chief-of-compliance/budget')
    assert raw.source_ref == 'baral-chief-of-compliance/budget'
    assert raw.meta.get('slug') == 'baral-chief-of-compliance'
    assert raw.meta.get('tag') == 'budget'


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #


def test_normalize_ayara_builds_deck(data_dir: Path) -> None:
    imp = _importer_with(AYARA)
    deck = imp.normalize(imp.fetch('https://edhrec.com/average-decks/ayara-first-of-locthwain'))
    assert isinstance(deck, Deck)
    assert [c.name for c in deck.commanders] == ['Ayara, First of Locthwain']
    assert sum(c.quantity for c in deck.maindeck) == 99


# --------------------------------------------------------------------------- #
# end-to-end import_deck through the registry
# --------------------------------------------------------------------------- #


def test_registry_dispatches_to_edhrec() -> None:
    from pipeline.sources.deck_import import get_importer

    assert get_importer('https://edhrec.com/commanders/ayara-first-of-locthwain').source == 'edhrec'


def test_import_deck_routes_to_edhrec(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.sources import deck_import

    payload = _load(AYARA)
    registered = deck_import._IMPORTERS['edhrec']
    monkeypatch.setattr(registered, '_fetch_json', lambda slug: payload)

    deck = import_deck('https://edhrec.com/commanders/ayara-first-of-locthwain')
    assert isinstance(deck, Deck)
    assert [c.name for c in deck.commanders] == ['Ayara, First of Locthwain']


# --------------------------------------------------------------------------- #
# error paths — bad slug (empty deck) / non-200
# --------------------------------------------------------------------------- #


def test_fetch_bad_slug_raises_collection_error(data_dir: Path) -> None:
    imp = EdhrecImporter()
    # EDHREC serves a page with no ``deck`` block for an unknown commander/slug.
    imp._fetch_json = lambda slug: {'header': {}, 'panels': {}}  # type: ignore[method-assign]
    with pytest.raises(CollectionError, match=r'unknown|not found|no deck|empty'):
        imp.fetch('https://edhrec.com/average-decks/not-a-real-commander')


def test_fetch_empty_cards_raises_collection_error(data_dir: Path) -> None:
    imp = EdhrecImporter()
    imp._fetch_json = lambda slug: {'deck': {'commander': [], 'cards': {}}}  # type: ignore[method-assign]
    with pytest.raises(CollectionError, match=r'unknown|not found|no deck|empty'):
        imp.fetch('https://edhrec.com/average-decks/not-a-real-commander')


def test_fetch_404_raises_collection_error(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    imp = EdhrecImporter()

    def _boom(url: str, *args: object, **kwargs: object) -> httpx.Response:
        request = httpx.Request('GET', url)
        return httpx.Response(404, request=request)

    monkeypatch.setattr(httpx, 'get', _boom)
    with pytest.raises(CollectionError, match=r'not found|unreachable|404'):
        imp.fetch('https://edhrec.com/average-decks/not-a-real-commander')


def test_fetch_network_error_raises_collection_error(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    imp = EdhrecImporter()

    def _boom(url: str, *args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError('unreachable')

    monkeypatch.setattr(httpx, 'get', _boom)
    with pytest.raises(CollectionError, match=r'unreachable|reach'):
        imp.fetch('https://edhrec.com/average-decks/ayara-first-of-locthwain')
