"""Moxfield deck-import adapter (Phase 4) — WAF-block-first, best-effort parse.

OFFLINE + deterministic. Two fixtures under ``tests/fixtures/deck_import/``:

    - ``moxfield_waf_403.html`` — a REAL Cloudflare block page captured at build
      time via ``curl -H 'User-Agent: Mozilla/5.0'
      https://api2.moxfield.com/v2/decks/all/0Wb6uchzlEqmeVLHl4mVUg`` (status 403,
      HTML ``<title>Attention Required! | Cloudflare</title>``). This IS the
      primary, load-bearing behavior — Moxfield blocks automated reads, so the
      adapter's job is a clean, actionable failure, not a successful parse.
    - ``moxfield_synthetic_200.json`` — a SYNTHETIC hand-written Moxfield v2 deck
      JSON (a live 200 capture is impossible because the WAF blocks it). Clearly
      synthetic; documents the assumed (UNVERIFIED-live) v2 schema so the happy
      path stays correct if the WAF ever relents.

The injection seam is :meth:`MoxfieldImporter._fetch` (the one method that touches
``httpx``) — tests monkeypatch it to return an ``httpx.Response`` built from a
fixture, so NO test ever hits the wire.

Covers:

    - the WAF path: a 403 Cloudflare HTML body -> ``fetch`` raises the exact
      actionable :class:`CollectionError` (asserted verbatim; a ``CollectionError``,
      not a bare ``ValueError`` / traceback);
    - ``matches``: True for a moxfield.com deck URL; False for archidekt / edhrec /
      a ``.dck`` path / a plaintext card line;
    - the happy path: the synthetic 200 JSON parses mainboard / commanders /
      sideboard into a ``RawDeck`` (quantities + roles), and ``import_deck``
      end-to-end returns a ``Deck``;
    - ``import_deck('https://moxfield.com/decks/xyz')`` with the 403 injected ->
      the actionable ``CollectionError`` (proves registry routing + the failure).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pipeline import store
from pipeline.collection.errors import CollectionError
from pipeline.contracts import Deck
from pipeline.sources.deck_import import DeckImporter, import_deck
from pipeline.sources.deck_import.moxfield import _WAF_MESSAGE, MoxfieldImporter

FIXTURES = Path(__file__).parent / 'fixtures' / 'deck_import'
WAF_403 = FIXTURES / 'moxfield_waf_403.html'
SYNTHETIC_200 = FIXTURES / 'moxfield_synthetic_200.json'

#: The EXACT actionable message the spec mandates (one line, no traceback).
EXPECTED_WAF_MESSAGE = (
    'Moxfield blocks automated reads (Cloudflare). Open the deck in your browser '
    "-> Export -> copy the list, then: pipe it to 'collection import-deck -' "
    '(plaintext), or use the Archidekt/EDHREC URL instead.'
)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the TTL cache at an isolated tmp data root."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _response(status: int, *, text: str = '', json_body: object = None) -> httpx.Response:
    """Build a fixture-backed ``httpx.Response`` for the ``_fetch`` seam."""
    request = httpx.Request('GET', 'https://api2.moxfield.com/v2/decks/all/x')
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=request)
    return httpx.Response(status, text=text, request=request)


def _waf_importer() -> MoxfieldImporter:
    """A MoxfieldImporter whose ``_fetch`` serves the REAL 403 Cloudflare body."""
    imp = MoxfieldImporter()
    body = WAF_403.read_text(encoding='utf-8')
    imp._fetch = lambda deck_id: _response(403, text=body)  # type: ignore[method-assign]
    return imp


def _happy_importer() -> MoxfieldImporter:
    """A MoxfieldImporter whose ``_fetch`` serves the SYNTHETIC 200 JSON."""
    import json

    imp = MoxfieldImporter()
    payload = json.loads(SYNTHETIC_200.read_text(encoding='utf-8'))
    imp._fetch = lambda deck_id: _response(200, json_body=payload)  # type: ignore[method-assign]
    return imp


# --------------------------------------------------------------------------- #
# Protocol + registration + the exact message constant
# --------------------------------------------------------------------------- #


def test_importer_satisfies_protocol() -> None:
    assert isinstance(MoxfieldImporter(), DeckImporter)


def test_registered_under_source_key() -> None:
    from pipeline.sources import deck_import

    assert isinstance(deck_import._IMPORTERS.get('moxfield'), MoxfieldImporter)


def test_waf_message_matches_spec_verbatim() -> None:
    assert _WAF_MESSAGE == EXPECTED_WAF_MESSAGE


# --------------------------------------------------------------------------- #
# matches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    'ref',
    [
        'https://moxfield.com/decks/abc',
        'https://www.moxfield.com/decks/0Wb6uchzlEqmeVLHl4mVUg',
        'moxfield.com/decks/xk8ZPcJTOkqRDLllh0LA9g',
    ],
)
def test_matches_moxfield_refs(ref: str) -> None:
    assert MoxfieldImporter().matches(ref) is True


@pytest.mark.parametrize(
    'ref',
    [
        'https://archidekt.com/decks/10126962/',
        'https://edhrec.com/commanders/krenko-mob-boss',
        'some_deck.dck',
        '1 Sol Ring',  # a plaintext card line
    ],
)
def test_matches_false_for_non_moxfield(ref: str) -> None:
    assert MoxfieldImporter().matches(ref) is False


# --------------------------------------------------------------------------- #
# id extraction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ('ref', 'expected'),
    [
        ('https://moxfield.com/decks/abc', 'abc'),
        ('https://www.moxfield.com/decks/0Wb6uchzlEqmeVLHl4mVUg/', '0Wb6uchzlEqmeVLHl4mVUg'),
        ('moxfield.com/decks/xk8ZPcJTOkqRDLllh0LA9g', 'xk8ZPcJTOkqRDLllh0LA9g'),
    ],
)
def test_extract_id(ref: str, expected: str) -> None:
    assert MoxfieldImporter()._extract_id(ref) == expected


# --------------------------------------------------------------------------- #
# WAF path — the PRIMARY, load-bearing behavior (real Cloudflare 403)
# --------------------------------------------------------------------------- #


def test_fetch_waf_403_raises_actionable_collection_error(data_dir: Path) -> None:
    imp = _waf_importer()
    with pytest.raises(CollectionError) as excinfo:
        imp.fetch('https://moxfield.com/decks/0Wb6uchzlEqmeVLHl4mVUg')
    # Exactly the spec string — a CollectionError (not a bare ValueError/traceback).
    assert excinfo.value.message == EXPECTED_WAF_MESSAGE
    assert str(excinfo.value) == EXPECTED_WAF_MESSAGE


def test_fetch_non_json_html_raises_actionable_error(data_dir: Path) -> None:
    # Any non-200 HTML body (a generic block/challenge) surfaces the same message.
    imp = MoxfieldImporter()
    imp._fetch = lambda deck_id: _response(403, text='<html>blocked</html>')  # type: ignore[method-assign]
    with pytest.raises(CollectionError, match='Moxfield blocks automated reads'):
        imp.fetch('moxfield.com/decks/abc')


def test_fetch_200_html_body_still_raises(data_dir: Path) -> None:
    # A 200 whose body is HTML (not JSON) is still a block/challenge -> actionable.
    imp = MoxfieldImporter()
    imp._fetch = lambda deck_id: _response(200, text='<!DOCTYPE html><html>...</html>')  # type: ignore[method-assign]
    with pytest.raises(CollectionError) as excinfo:
        imp.fetch('moxfield.com/decks/abc')
    assert excinfo.value.message == EXPECTED_WAF_MESSAGE


def test_import_deck_moxfield_waf_actionable(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Registry routing + the user-facing failure end-to-end.
    from pipeline.sources import deck_import

    body = WAF_403.read_text(encoding='utf-8')
    registered = deck_import._IMPORTERS['moxfield']
    monkeypatch.setattr(registered, '_fetch', lambda deck_id: _response(403, text=body))
    with pytest.raises(CollectionError) as excinfo:
        import_deck('https://moxfield.com/decks/xyz')
    assert excinfo.value.message == EXPECTED_WAF_MESSAGE


# --------------------------------------------------------------------------- #
# Happy path — SYNTHETIC 200 (WAF-blocked live capture; schema UNVERIFIED-live)
# --------------------------------------------------------------------------- #


def test_fetch_synthetic_200_parses_boards(data_dir: Path) -> None:
    raw = _happy_importer().fetch('https://moxfield.com/decks/synthetic')
    assert raw.source == 'moxfield'
    assert raw.source_ref == 'synthetic'
    assert raw.name == 'Krenko Goblin Tribal (synthetic)'

    commanders = [c for c in raw.cards if c.role == 'commander']
    sideboard = [c for c in raw.cards if c.role == 'sideboard']
    maindeck = [c for c in raw.cards if c.role is None]

    assert [c.name for c in commanders] == ['Krenko, Mob Boss']
    assert [c.name for c in sideboard] == ['Blood Moon']
    # qty>1 preserved (Mountain x30) alongside the singletons.
    assert {c.name: c.quantity for c in maindeck} == {
        'Sol Ring': 1,
        'Goblin Chieftain': 1,
        'Mountain': 30,
    }


def test_import_deck_synthetic_200_returns_deck(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from pipeline.sources import deck_import

    payload = json.loads(SYNTHETIC_200.read_text(encoding='utf-8'))
    registered = deck_import._IMPORTERS['moxfield']
    monkeypatch.setattr(registered, '_fetch', lambda deck_id: _response(200, json_body=payload))

    deck = import_deck('https://moxfield.com/decks/synthetic')
    assert isinstance(deck, Deck)
    assert [c.name for c in deck.commanders] == ['Krenko, Mob Boss']
    assert {c.name for c in deck.sideboard} == {'Blood Moon'}


def test_fetch_synthetic_boards_wrapper_shape(data_dir: Path) -> None:
    # Defensive: tolerate a ``boards.mainboard`` wrapper if a variant returns one.
    imp = MoxfieldImporter()
    payload: dict[str, object] = {
        'name': 'Wrapped',
        'boards': {
            'mainboard': {'Sol Ring': {'quantity': 2, 'card': {'name': 'Sol Ring'}}},
            'commanders': {'Krenko, Mob Boss': {'quantity': 1, 'card': {'name': 'Krenko, Mob Boss'}}},
        },
    }
    imp._fetch = lambda deck_id: _response(200, json_body=payload)  # type: ignore[method-assign]
    raw = imp.fetch('moxfield.com/decks/wrapped')
    assert {c.name: c.quantity for c in raw.cards if c.role is None} == {'Sol Ring': 2}
    assert [c.name for c in raw.cards if c.role == 'commander'] == ['Krenko, Mob Boss']
