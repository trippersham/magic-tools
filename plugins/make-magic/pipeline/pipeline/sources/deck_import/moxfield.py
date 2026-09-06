"""Moxfield deck-import adapter (Phase 4) — best-effort read + WAF paste fallback.

Turns a Moxfield deck reference — ``https://moxfield.com/decks/{id}`` — into a
:class:`~pipeline.sources.deck_import.raw.RawDeck` by *attempting* the public JSON
API (``GET https://api2.moxfield.com/v2/decks/all/{id}``).

**The primary, load-bearing behavior is the graceful WAF failure, not a
successful parse.** Confirmed this session (and in the 2026-07-01 plan): that
endpoint answers a plain HTTP client (``httpx`` included) with a Cloudflare **403
"you have been blocked" HTML** page, not deck JSON. So on the WAF block — any
non-200, or a body that is HTML rather than JSON — this adapter raises a
:class:`CollectionError` with an exact, actionable one-line message (:data:`_WAF_MESSAGE`)
that tells the user how to get their deck in anyway (paste the export via
``import-deck -``, or use an Archidekt/EDHREC URL). No traceback.

If a 200 JSON *ever* returns (the WAF relents, or a user is on a good network),
the happy path stays correct: the Moxfield v2 deck schema is parsed into a
``RawDeck``. That schema is documented here as **UNVERIFIED-against-live** because
the WAF blocks capture; it is exercised only by a hand-written synthetic fixture.
Assumed shape: top-level ``name``; ``mainboard`` = a dict ``cardName ->
{"quantity": int, "card": {"name": str}}`` (maindeck); ``commanders`` = same shape
(role commander); ``sideboard`` = same shape (role sideboard). A ``boards``
wrapper (``boards.mainboard`` / ``boards.commanders`` / ``boards.sideboard``) is
tolerated defensively, preferring the top-level keys when both exist.

The single HTTP boundary is :meth:`MoxfieldImporter._fetch` — a test monkeypatches
that one method to serve a captured fixture ``Response`` (the real 403 body, or the
synthetic 200 JSON), so no test ever hits the wire.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from pipeline.collection.errors import CollectionError
from pipeline.contracts import ROLE_COMMANDER, ROLE_SIDEBOARD
from pipeline.sources.deck_import.raw import RawDeck, RawEntry, load_or_fetch

if TYPE_CHECKING:
    from pipeline.contracts import Deck

__all__ = ('MoxfieldImporter',)

#: The public deck-JSON endpoint (formatted with the public deck id).
_API_URL = 'https://api2.moxfield.com/v2/decks/all/{deck_id}'
#: A browser-like UA (matching ``resolver.py`` / the sibling adapters).
_USER_AGENT = 'Mozilla/5.0 (make-magic-plugin/2.0)'
#: HTTP timeout (seconds) — matches the resolver's client.
_TIMEOUT = 30

#: A moxfield deck URL: capture the public id after ``/decks/``.
_URL_ID_RE = re.compile(r'moxfield\.com/decks/(?P<id>[^/?#]+)', re.IGNORECASE)

#: The EXACT actionable message raised on the Cloudflare WAF block (or any
#: non-JSON / non-200 body). One line, no traceback — the primary deliverable of
#: this adapter. Keep this string verbatim: it is the user's recovery path.
_WAF_MESSAGE = (
    'Moxfield blocks automated reads (Cloudflare). Open the deck in your browser '
    "-> Export -> copy the list, then: pipe it to 'collection import-deck -' "
    '(plaintext), or use the Archidekt/EDHREC URL instead.'
)


class MoxfieldImporter:
    """Import a Commander deck from Moxfield (best-effort; WAF -> actionable error)."""

    source = 'moxfield'

    def matches(self, ref: str) -> bool:
        """True iff ``ref`` is a ``moxfield.com`` deck URL.

        Kept to the host on purpose: Moxfield public ids are opaque
        base64url-ish strings, so (unlike Archidekt's numeric ids) there is no
        safe "bare id" form to claim without colliding with a plaintext card name
        — a bare id would have to arrive via an explicit ``source='moxfield'``
        override.
        """
        return 'moxfield.com/decks/' in ref.strip().lower()

    def fetch(self, ref: str, *, refresh: bool = False) -> RawDeck:
        """Resolve ``ref`` -> a TTL-cached ``RawDeck`` (the HTTP + parse boundary).

        Extracts the public id, then routes the GET+parse through
        :func:`~pipeline.sources.deck_import.raw.load_or_fetch` keyed by the id
        (``permanent=False`` — URL-addressable, so TTL applies). The Cloudflare
        WAF block (the expected outcome) surfaces as a :class:`CollectionError`
        carrying :data:`_WAF_MESSAGE`, never a traceback.
        """
        deck_id = self._extract_id(ref)

        def _do_fetch() -> RawDeck:
            resp = self._fetch(deck_id)
            payload = self._read_json_or_block(resp)
            return self._parse(deck_id, payload)

        return load_or_fetch(self.source, deck_id, _do_fetch, refresh=refresh, permanent=False)

    def normalize(self, raw: RawDeck) -> Deck:
        """Turn the ``RawDeck`` into a canonical ``Deck``; warn if off-size.

        Delegates the shape to the shared helper, then runs the shared
        size-sanity check for symmetry with the other API adapters — a Moxfield
        deck is (usually) a ~100-card Commander list, so an off-100 parse is
        surfaced (a WARNING log) rather than silently shipped.
        """
        from pipeline.sources.deck_import import _normalize_rawdeck, _warn_if_off_size

        deck = _normalize_rawdeck(raw)
        _warn_if_off_size(deck)
        return deck

    # ----------------------------------------------------------------------- #
    # id extraction
    # ----------------------------------------------------------------------- #

    def _extract_id(self, ref: str) -> str:
        """Extract the public deck id from a Moxfield deck URL.

        A ref that is not a Moxfield deck URL is a loud :class:`CollectionError`
        (this should not happen behind :meth:`matches`, but keeps ``fetch`` honest
        if called directly).
        """
        match = _URL_ID_RE.search(ref.strip())
        if match is None:
            raise CollectionError(f'not a Moxfield deck reference: {ref!r}')
        return match.group('id')

    # ----------------------------------------------------------------------- #
    # HTTP boundary (the one injectable seam) + the block detection + parse
    # ----------------------------------------------------------------------- #

    def _fetch(self, deck_id: str) -> httpx.Response:
        """GET the Moxfield deck response for ``deck_id`` (the injectable HTTP seam).

        The single network call — tests monkeypatch THIS method to serve a
        captured fixture ``Response`` (the real 403 Cloudflare body, or the
        synthetic 200 JSON), so no test ever hits the wire. A network/timeout
        failure surfaces as the actionable :class:`CollectionError` (the WAF is
        the expected outcome; an unreachable host is a variant of "cannot read
        automatically").
        """
        url = _API_URL.format(deck_id=deck_id)
        try:
            return httpx.get(url, headers={'User-Agent': _USER_AGENT}, timeout=_TIMEOUT, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise CollectionError(_WAF_MESSAGE) from exc

    def _read_json_or_block(self, resp: httpx.Response) -> dict[str, Any]:
        """Return the deck JSON, or raise the actionable WAF error on a block.

        The Cloudflare block is the expected path: it answers non-200 with an HTML
        body. We treat as a block anything that is not a 200 whose body parses as a
        JSON object — a non-200, an HTML/challenge body, or an unexpected shape all
        surface :data:`_WAF_MESSAGE`. Only a clean 200 JSON object proceeds to the
        parser.
        """
        if resp.status_code != 200:
            raise CollectionError(_WAF_MESSAGE)
        try:
            payload = resp.json()
        except ValueError as exc:  # HTML / challenge body served with a 200
            raise CollectionError(_WAF_MESSAGE) from exc
        if not isinstance(payload, dict):
            raise CollectionError(_WAF_MESSAGE)
        return payload

    def _parse(self, deck_id: str, payload: dict[str, Any]) -> RawDeck:
        """Parse the (assumed, UNVERIFIED-live) Moxfield v2 deck schema -> ``RawDeck``.

        See the module docstring for the assumed shape. Each of ``mainboard`` /
        ``commanders`` / ``sideboard`` is a dict whose *values* are
        ``{"quantity": int, "card": {"name": str}}``; we flatten each dict's values
        into role-tagged :class:`RawEntry`s. A ``boards`` wrapper is tolerated
        defensively, preferring a top-level board when both are present.
        """
        boards = payload.get('boards') if isinstance(payload.get('boards'), dict) else {}

        def _board(key: str) -> dict[str, Any]:
            candidate = payload.get(key)
            if not isinstance(candidate, dict):
                candidate = boards.get(key) if isinstance(boards, dict) else None
            return candidate if isinstance(candidate, dict) else {}

        entries: list[RawEntry] = []
        for entry in _board('commanders').values():
            entries.append(self._entry(entry, ROLE_COMMANDER))
        for entry in _board('mainboard').values():
            entries.append(self._entry(entry, None))
        for entry in _board('sideboard').values():
            entries.append(self._entry(entry, ROLE_SIDEBOARD))

        return RawDeck(
            name=str(payload.get('name') or f'Moxfield deck {deck_id}'),
            cards=entries,
            source=self.source,
            source_ref=deck_id,
            meta={'format': payload.get('format')},
            fetched_at=datetime.now(tz=UTC),
        )

    def _entry(self, entry: Any, role: str | None) -> RawEntry:
        """Build one :class:`RawEntry` from a Moxfield board value.

        Uses the nested ``card.name`` (falling back to a top-level ``name`` if a
        variant shape ever omits the wrapper) and the entry ``quantity``.
        """
        card = entry.get('card') if isinstance(entry, dict) else None
        name = (card.get('name') if isinstance(card, dict) else None) or entry.get('name')
        quantity = int(entry.get('quantity', 1)) if isinstance(entry, dict) else 1
        return RawEntry(name=str(name), quantity=quantity, role=role)
