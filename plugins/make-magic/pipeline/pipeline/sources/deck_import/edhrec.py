"""EDHREC deck-import adapter (Phase 3).

Turns an EDHREC average-decks reference into a
:class:`~pipeline.sources.deck_import.raw.RawDeck` by reading the public JSON API
(``GET https://json.edhrec.com/pages/average-decks/{slug}[/{tag}].json``). EDHREC
average decks are URL-addressable, so the cache is TTL-bound (NOT permanent): a
re-import within the shared pull-TTL is a cache hit, and ``refresh=True`` /
``invalidate`` forces a re-fetch.

Accepted ref forms (all resolve to a ``slug`` — optionally ``slug/tag``):

    - ``https://edhrec.com/average-decks/{slug}`` (and ``.../{slug}/{tag}``) — the
      slug (and trailing tag) are used directly;
    - ``https://edhrec.com/commanders/{slug}`` (and ``.../{slug}/{tag}``) — the
      commanders view of the same commander; the ``/commanders/`` segment is
      dropped so it maps onto ``average-decks/{slug}`` (a trailing tag carries over
      to ``average-decks/{slug}/{tag}``);
    - ``https://json.edhrec.com/pages/average-decks/{slug}[/{tag}].json`` — the raw
      JSON endpoint; the ``pages/average-decks/`` prefix and ``.json`` suffix are
      peeled to recover ``slug[/tag]``.

:func:`edhrec_slug` derives a slug from a bare commander NAME (``lower``, drop
apostrophes, non-alphanumeric runs -> a single ``-``, trim leading/trailing ``-``;
e.g. ``K'rrik, Son of Yawgmoth`` -> ``krrik-son-of-yawgmoth``). It is used when a
URL only yields a commander context and is exported for P5's ``--source edhrec``
bare-name path. Note :meth:`EdhrecImporter.matches` deliberately does NOT claim a
bare name/slug — a bare string is ambiguous with a plaintext card name, so a bare
name reaches this adapter only via an explicit ``source='edhrec'`` override.

Parse rule (validated against a captured Ayara fixture — implement exactly).
``data['deck']`` carries ``commander`` (a list of commander names) and ``cards``
(a dict ``card-type -> [[name, qty], ...]`` where basics carry their real counts,
e.g. ``['Swamp', 28]``). Every ``cards`` bucket is flattened into maindeck
``RawEntry``s (quantities preserved), and each ``deck.commander`` name becomes a
commander ``RawEntry``. ``RawDeck.name`` is the (first) commander name, falling
back to the slug. A missing/empty ``deck`` (a bad slug) or a non-200 both surface
as a :class:`CollectionError` (unknown commander / unreachable), never a
traceback.

The single HTTP boundary is :meth:`EdhrecImporter._fetch_json` — a test
monkeypatches that one method to serve the captured fixture (no live call).
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

__all__ = ('EdhrecImporter', 'edhrec_slug')

#: The public JSON endpoint (formatted with ``slug`` or ``slug/tag``).
_API_URL = 'https://json.edhrec.com/pages/average-decks/{slug}.json'
#: A browser-like UA (matching ``resolver.py`` / the Archidekt adapter).
_USER_AGENT = 'Mozilla/5.0 (make-magic-plugin/2.0)'
#: HTTP timeout (seconds) — matches the resolver's client.
_TIMEOUT = 30

#: A single trailing/leading apostrophe form to strip in :func:`edhrec_slug`.
_APOSTROPHES = ("'", '’')  # noqa: RUF001 — the curly form is load-bearing (real EDHREC names use it)
#: A run of non-alphanumeric characters, collapsed to one ``-`` in a slug.
_NON_ALNUM_RE = re.compile(r'[^a-z0-9]+')


def edhrec_slug(name: str) -> str:
    """Derive an EDHREC slug from a commander ``name``.

    Rule: lowercase, drop apostrophes (both the straight and curly forms), collapse
    every run of non-alphanumeric characters to a single ``-``, then strip a
    leading/trailing ``-``. So ``K'rrik, Son of Yawgmoth`` -> ``krrik-son-of-yawgmoth``
    and ``Ayara, First of Locthwain`` -> ``ayara-first-of-locthwain``.
    """
    lowered = name.lower()
    for apostrophe in _APOSTROPHES:
        lowered = lowered.replace(apostrophe, '')
    return _NON_ALNUM_RE.sub('-', lowered).strip('-')


class EdhrecImporter:
    """Import an average Commander deck from EDHREC's public JSON API (TTL-cached)."""

    source = 'edhrec'

    def matches(self, ref: str) -> bool:
        """True iff ``ref`` is an ``edhrec.com`` / ``json.edhrec.com`` URL.

        Kept to the two EDHREC hosts on purpose: a bare commander name/slug is
        ambiguous with a plaintext card name, so this adapter never claims
        arbitrary bare text — a bare name reaches it only via an explicit
        ``source='edhrec'`` override (P5), which routes past :meth:`matches`.
        """
        lowered = ref.strip().lower()
        return 'edhrec.com' in lowered

    def fetch(self, ref: str, *, refresh: bool = False) -> RawDeck:
        """Resolve ``ref`` -> a TTL-cached ``RawDeck`` (the HTTP + parse boundary).

        Extracts ``slug[/tag]``, then routes the GET+parse through
        :func:`~pipeline.sources.deck_import.raw.load_or_fetch` keyed by
        ``slug[/tag]`` (``permanent=False`` — URL-addressable, so TTL applies). A
        non-200 or a missing ``deck`` surfaces as a :class:`CollectionError`
        (unreachable / unknown commander), never a traceback.
        """
        slug = self._extract_slug(ref)

        def _do_fetch() -> RawDeck:
            payload = self._fetch_json(slug)
            return self._parse(slug, payload)

        return load_or_fetch(self.source, slug, _do_fetch, refresh=refresh, permanent=False)

    def normalize(self, raw: RawDeck) -> Deck:
        """Turn the ``RawDeck`` into a canonical ``Deck``; warn if off-size.

        Delegates the shape to the shared helper, then runs the shared
        size-sanity check for symmetry with the other API adapters — an EDHREC
        average deck is a ~100-card Commander list, so an off-100 parse is
        surfaced (a WARNING log) rather than silently shipped.
        """
        from pipeline.sources.deck_import import _normalize_rawdeck, _warn_if_off_size

        deck = _normalize_rawdeck(raw)
        _warn_if_off_size(deck)
        return deck

    # ----------------------------------------------------------------------- #
    # slug / tag extraction
    # ----------------------------------------------------------------------- #

    def _extract_slug(self, ref: str) -> str:
        """Extract ``slug`` (or ``slug/tag``) from any accepted EDHREC ref form.

        Maps ``commanders/{slug}`` onto ``average-decks/{slug}`` and peels the
        ``json.edhrec.com`` ``pages/average-decks/....json`` wrapper. A ref that is
        not an EDHREC URL is a loud :class:`CollectionError` (this should not
        happen behind :meth:`matches`, but keeps ``fetch`` honest if called
        directly).
        """
        cleaned = ref.strip()
        # Peel a scheme + host so the remaining path is uniform to slice.
        cleaned = re.sub(r'^[a-z]+://', '', cleaned, flags=re.IGNORECASE)
        lowered = cleaned.lower()
        if 'json.edhrec.com' in lowered:
            path = cleaned.split('json.edhrec.com', 1)[1]
        elif 'edhrec.com' in lowered:
            path = cleaned.split('edhrec.com', 1)[1]
        else:
            raise CollectionError(f'not an EDHREC deck reference: {ref!r}')

        path = path.strip('/')
        if path.lower().endswith('.json'):
            path = path[: -len('.json')]
        # Normalize the recognized path prefixes down to ``slug[/tag]``.
        for prefix in ('pages/average-decks/', 'average-decks/', 'commanders/'):
            if path.lower().startswith(prefix):
                path = path[len(prefix) :]
                break
        else:
            raise CollectionError(f'not an EDHREC average-decks/commanders reference: {ref!r}')

        slug = path.strip('/')
        if not slug:
            raise CollectionError(f'no commander slug in EDHREC reference: {ref!r}')
        return slug

    # ----------------------------------------------------------------------- #
    # HTTP boundary (the one injectable seam) + the deck.cards parse
    # ----------------------------------------------------------------------- #

    def _fetch_json(self, slug: str) -> dict[str, Any]:
        """GET the EDHREC average-decks JSON for ``slug`` (the injectable HTTP seam).

        The single network call — tests monkeypatch THIS method to serve a
        captured fixture, so no test ever hits the wire. A 404 (or any non-200)
        and a network/timeout failure both surface as a :class:`CollectionError`
        with a clear, tracebackless message.
        """
        url = _API_URL.format(slug=slug)
        try:
            resp = httpx.get(url, headers={'User-Agent': _USER_AGENT}, timeout=_TIMEOUT, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise CollectionError(f'could not reach EDHREC for {slug!r}: {exc}') from exc
        if resp.status_code == 404:
            raise CollectionError(f'EDHREC has no average deck for {slug!r} (404) — unknown commander/slug')
        if resp.status_code != 200:
            raise CollectionError(f'EDHREC returned HTTP {resp.status_code} for {slug!r}')
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CollectionError(f'EDHREC returned an unreadable response for {slug!r}') from exc
        if not isinstance(payload, dict):
            raise CollectionError(f'EDHREC returned an unexpected shape for {slug!r}')
        return payload

    def _parse(self, slug: str, payload: dict[str, Any]) -> RawDeck:
        """Flatten ``deck.cards`` + ``deck.commander`` -> a ``RawDeck``.

        See the module docstring for the rule. A missing/empty ``deck`` (or empty
        ``cards``) is a bad slug -> :class:`CollectionError`. The tag (if the ref
        carried one) and the base slug ride in ``meta``.
        """
        base_slug, _, tag = slug.partition('/')
        deck = payload.get('deck')
        if not isinstance(deck, dict):
            raise CollectionError(f'EDHREC has no average deck for {slug!r} — unknown commander/slug')

        buckets = deck.get('cards')
        commanders = deck.get('commander') or []
        if not isinstance(buckets, dict) or not buckets:
            raise CollectionError(f'EDHREC returned an empty deck for {slug!r} — unknown commander/slug')

        entries: list[RawEntry] = []
        for name in commanders:
            entries.append(RawEntry(name=str(name), quantity=1, role=ROLE_COMMANDER))
        for bucket in buckets.values():
            for pair in bucket or []:
                name, quantity = pair[0], int(pair[1])
                entries.append(RawEntry(name=str(name), quantity=quantity, role=None))

        deck_name = str(commanders[0]) if commanders else base_slug
        return RawDeck(
            name=deck_name,
            cards=entries,
            source=self.source,
            source_ref=slug,
            meta={'slug': base_slug, 'tag': tag or None},
            fetched_at=datetime.now(tz=UTC),
        )
