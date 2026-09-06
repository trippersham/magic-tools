"""Archidekt deck-import adapter (Phase 2).

Turns an Archidekt deck reference — ``https://archidekt.com/decks/{id}/...``,
``archidekt.com/decks/{id}``, or a bare numeric id — into a
:class:`~pipeline.sources.deck_import.raw.RawDeck` by reading the public JSON API
(``GET https://archidekt.com/api/decks/{id}/``). It is URL-addressable, so the
cache is TTL-bound (NOT permanent): a re-import within the shared pull-TTL is a
cache hit, and ``refresh=True`` / ``invalidate`` forces a re-fetch.

The maindeck rule (validated against real decks — implement exactly). Archidekt
models a deck as a flat ``cards`` list where each entry carries a set of
``categories`` (category-name strings), plus a top-level ``categories`` list of
``{name, includedInDeck, isPremier}``. We build:

    - ``excl`` = every category with ``includedInDeck: false`` (maybeboard, tokens,
      cuts, considering, wishlists — anything the owner marked as not in the deck);
    - ``prem`` = every ``isPremier`` category (the command zone / commander).

Then for each card entry (``cats`` = its category set): a ``cats & prem`` hit is a
commander; else a ``cats & excl`` hit is SKIPPED; else it is maindeck. Quantity is
the entry's ``quantity``. (Verified: Myrel deck 10126962 -> 99 main + 1 commander;
the naive "exclude only 'Maybeboard'" gives 171.) The top-level ``edhBracket`` and
the deck ``name`` ride along in ``RawDeck.meta`` / ``RawDeck.name``.

The single HTTP boundary is :meth:`ArchidektImporter._fetch_json` — a test
monkeypatches that one method to serve a captured fixture (no live call). The rest
of ``fetch`` (id extraction, the category rule, the cache) is pure/offline and
exercised directly. ``normalize`` delegates to the shared
:func:`~pipeline.sources.deck_import._normalize_rawdeck` and then runs the shared
size-sanity check (:func:`~pipeline.sources.deck_import._warn_if_off_size`).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from pipeline.collection.errors import CollectionError
from pipeline.contracts import ROLE_COMMANDER
from pipeline.sources.deck_import.raw import RawDeck, RawEntry, load_or_fetch

if TYPE_CHECKING:
    from pipeline.contracts import Deck

__all__ = ('ArchidektImporter',)

#: The public deck-JSON endpoint (formatted with the numeric id).
_API_URL = 'https://archidekt.com/api/decks/{deck_id}/'
#: A browser-like UA (matching ``resolver.py``) so the API answers a script call.
_USER_AGENT = 'Mozilla/5.0 (make-magic-plugin/2.0)'
#: HTTP timeout (seconds) — matches the resolver's client.
_TIMEOUT = 30

#: An archidekt deck URL: capture the numeric id after ``/decks/``.
_URL_ID_RE = re.compile(r'archidekt\.com/decks/(?P<id>\d+)', re.IGNORECASE)
#: A bare numeric id (the whole ref is digits) — the terse addressing form.
_BARE_ID_RE = re.compile(r'^\d+$')


class ArchidektImporter:
    """Import a Commander deck from Archidekt's public JSON API (TTL-cached)."""

    source = 'archidekt'

    def matches(self, ref: str) -> bool:
        """True iff ``ref`` is an Archidekt deck URL or a bare numeric id.

        The archidekt.com host is unambiguous. A bare *all-digits* ref (e.g.
        ``10126962``) is also claimed here as Archidekt's terse addressing form —
        a deliberate, documented choice: a plaintext ``.dck`` path, a decklist
        line (``1 Sol Ring``), and a non-numeric slug all contain non-digit
        characters and therefore do NOT match, so the plaintext adapter keeps
        them. (The registry tries plaintext first for anything ambiguous only if
        it were reordered; today plaintext already excludes the archidekt host and
        a bare id has no card line, so ownership is clean either way.)
        """
        lowered = ref.strip().lower()
        if 'archidekt.com/decks/' in lowered:
            return True
        return bool(_BARE_ID_RE.match(ref.strip()))

    def fetch(self, ref: str, *, refresh: bool = False) -> RawDeck:
        """Resolve ``ref`` -> a TTL-cached ``RawDeck`` (the HTTP + parse boundary).

        Extracts the numeric id, then routes the GET+parse through
        :func:`~pipeline.sources.deck_import.raw.load_or_fetch` keyed by the id
        (``permanent=False`` — URL-addressable, so TTL applies). A non-200 or a
        missing deck surfaces as a :class:`CollectionError` (deck not found /
        unreachable), never a traceback.
        """
        deck_id = self._extract_id(ref)

        def _do_fetch() -> RawDeck:
            payload = self._fetch_json(deck_id)
            return self._parse(deck_id, payload)

        return load_or_fetch(self.source, deck_id, _do_fetch, refresh=refresh, permanent=False)

    def normalize(self, raw: RawDeck) -> Deck:
        """Turn the ``RawDeck`` into a canonical ``Deck``; warn if off-size.

        Delegates the shape to the shared helper, then runs the shared
        size-sanity check so an off-100 commander parse is surfaced (a WARNING
        log) rather than silently shipped.
        """
        from pipeline.sources.deck_import import _normalize_rawdeck, _warn_if_off_size

        deck = _normalize_rawdeck(raw)
        _warn_if_off_size(deck)
        return deck

    # ----------------------------------------------------------------------- #
    # id extraction
    # ----------------------------------------------------------------------- #

    def _extract_id(self, ref: str) -> str:
        """Extract the numeric deck id from any accepted ref form.

        Accepts a full/partial archidekt URL (id after ``/decks/``) or a bare
        numeric id. A ref that is neither is a loud :class:`CollectionError` (this
        should not happen behind :meth:`matches`, but keeps ``fetch`` honest if
        called directly).
        """
        ref = ref.strip()
        match = _URL_ID_RE.search(ref)
        if match is not None:
            return match.group('id')
        if _BARE_ID_RE.match(ref):
            return ref
        raise CollectionError(f'not an Archidekt deck reference: {ref!r}')

    # ----------------------------------------------------------------------- #
    # HTTP boundary (the one injectable seam) + the category parse
    # ----------------------------------------------------------------------- #

    def _fetch_json(self, deck_id: str) -> dict[str, Any]:
        """GET the Archidekt deck JSON for ``deck_id`` (the injectable HTTP seam).

        The single network call — tests monkeypatch THIS method to serve a
        captured fixture, so no test ever hits the wire. A 404 (or any non-200)
        and a network/timeout failure both surface as a :class:`CollectionError`
        with a clear, tracebackless message.
        """
        url = _API_URL.format(deck_id=deck_id)
        try:
            resp = httpx.get(url, headers={'User-Agent': _USER_AGENT}, timeout=_TIMEOUT, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise CollectionError(f'could not reach Archidekt for deck {deck_id!r}: {exc}') from exc
        if resp.status_code == 404:
            raise CollectionError(f'Archidekt deck {deck_id!r} not found (404)')
        if resp.status_code != 200:
            raise CollectionError(f'Archidekt returned HTTP {resp.status_code} for deck {deck_id!r}')
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CollectionError(f'Archidekt returned an unreadable response for deck {deck_id!r}') from exc
        if not isinstance(payload, dict):
            raise CollectionError(f'Archidekt returned an unexpected shape for deck {deck_id!r}')
        return payload

    def _parse(self, deck_id: str, payload: dict[str, Any]) -> RawDeck:
        """Apply the maindeck/commander/skip category rule -> a ``RawDeck``.

        See the module docstring for the rule. ``edhBracket`` (may be null) rides
        in ``meta``; the deck ``name`` becomes ``RawDeck.name``.
        """
        raw_categories = payload.get('categories') or []
        excl = {c['name'] for c in raw_categories if not c.get('includedInDeck', True)}
        prem = {c['name'] for c in raw_categories if c.get('isPremier')}

        entries: list[RawEntry] = []
        for card in payload.get('cards') or []:
            cats = set(card.get('categories') or [])
            role: str | None
            if cats & prem:
                role = ROLE_COMMANDER
            elif cats & excl:
                continue  # maybeboard / tokens / cuts — not in the deck
            else:
                role = None  # maindeck
            name = card['card']['oracleCard']['name']
            quantity = int(card.get('quantity', 1))
            entries.append(RawEntry(name=name, quantity=quantity, role=role))

        return RawDeck(
            name=payload.get('name') or f'Archidekt deck {deck_id}',
            cards=entries,
            source=self.source,
            source_ref=deck_id,
            meta={'edhBracket': payload.get('edhBracket')},
            fetched_at=datetime.now(tz=UTC),
        )
