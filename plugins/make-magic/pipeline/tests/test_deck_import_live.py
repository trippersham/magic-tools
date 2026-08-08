"""LIVE deck-import: hit the REAL Archidekt + EDHREC APIs to catch upstream drift.

Deselected by default (`addopts = -m "not live and not forge"` in pyproject) so
the normal suite stays OFFLINE + fast + deterministic. Run it explicitly (no
creds needed — these are PUBLIC endpoints):

    cd plugins/make-magic/pipeline
    uv run --extra dev pytest -m live tests/test_deck_import_live.py -q

The point is drift detection: the offline fixture tests prove our parsers against
a captured shape; these prove that shape still matches what Archidekt/EDHREC serve
today (a category-rule change, a renamed JSON key, a moved endpoint would break
the real import while the fixtures stay green). Each test writes into an isolated
tmp data root (via ``MAKE_MAGIC_DATA_DIR``) so the live cache never pollutes real
``data/``.

Moxfield is deliberately NOT hit live: it sits behind a Cloudflare WAF that 403s
automated reads, so a live GET would be flaky (and the whole point of the adapter
is to raise an actionable paste error on that block). The one Moxfield live test
asserts exactly that — the block degrades to a clean :class:`CollectionError`, not
a traceback — without depending on a 200 ever coming back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection.errors import CollectionError
from pipeline.contracts import Deck
from pipeline.sources.deck_import import import_deck


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the import cache at an isolated tmp data root via the env override."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


def _maindeck_total(deck: Deck) -> int:
    """Sum of maindeck quantities (basics carry their real counts)."""
    return sum(c.quantity for c in deck.maindeck)


@pytest.mark.live
def test_live_archidekt_myrel(data_dir: Path) -> None:
    """A real Archidekt fetch resolves the known Myrel deck: commander + ~99 main."""
    deck = import_deck('https://archidekt.com/decks/10126962/')

    assert [c.name for c in deck.commanders] == ['Myrel, Shield of Argive']
    total = _maindeck_total(deck)
    assert 95 <= total <= 101, f'Archidekt maindeck total {total} out of the sane 95..101 band'


@pytest.mark.live
def test_live_edhrec_ayara(data_dir: Path) -> None:
    """A real EDHREC fetch resolves Ayara: commander + ~99 main, basics carry counts."""
    deck = import_deck('https://edhrec.com/commanders/ayara-first-of-locthwain')

    assert [c.name for c in deck.commanders] == ['Ayara, First of Locthwain']
    total = _maindeck_total(deck)
    assert 95 <= total <= 101, f'EDHREC maindeck total {total} out of the sane 95..101 band'

    # The basics-carry-counts guarantee: a mono-black average deck runs many Swamps,
    # so a Swamp entry with quantity > 1 must survive the bucket-flatten (qty 1 would
    # mean the flatten collapsed basics to singletons — a real drift bug).
    swamps = [c for c in deck.maindeck if c.name == 'Swamp']
    assert swamps, 'no Swamp entry in the EDHREC average deck (basics dropped?)'
    assert any(c.quantity > 1 for c in swamps), 'Swamp quantity collapsed to 1 (basics-carry-counts broke)'


@pytest.mark.live
def test_live_moxfield_best_effort_or_actionable(data_dir: Path) -> None:
    """Moxfield is best-effort: either a clean paste-error OR a valid parsed deck.

    Moxfield sits behind a Cloudflare WAF that USUALLY 403s automated reads, so the
    expected live outcome is the adapter degrading that block to an actionable
    :class:`CollectionError` (pointing at the paste fallback) — never a traceback.
    But the WAF is intermittent: when the ``api2.moxfield.com`` endpoint does serve
    a 200, the adapter must parse it into a valid ``Deck``. This test accepts BOTH
    branches — what it forbids is the third outcome (an unhandled exception / a
    silently-empty parse), which is the real drift signal. It does NOT hit any live
    creds and keeps the suite non-flaky by not pinning a single WAF state.
    """
    try:
        deck = import_deck('https://moxfield.com/decks/xk8ZPcJTOkqRDLllh0LA9g')
    except CollectionError as exc:
        # The WAF blocked us — the message must be the actionable paste guidance.
        assert 'paste' in str(exc).lower() or 'moxfield' in str(exc).lower()
        return
    # The WAF relented and served a 200 — the parse must be a real, non-empty deck.
    assert deck.cards, 'Moxfield returned a 200 but the parse produced an empty deck'
    assert deck.commanders or deck.maindeck, 'parsed deck has neither commander nor maindeck'
