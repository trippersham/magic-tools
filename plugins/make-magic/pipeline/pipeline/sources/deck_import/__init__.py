"""Deck-import source — resolve an external deck reference to a canonical ``Deck``.

The read-side inverse of :mod:`pipeline.destinations.deck_export` (``Deck`` -> a
file's text): each concrete adapter turns one external deck source (an Archidekt
/ EDHREC / Moxfield URL, a file path, ``-``/stdin, or pasted text) into our
canonical :class:`~pipeline.contracts.Deck`, through a cached intermediate
:class:`~pipeline.sources.deck_import.raw.RawDeck` stage (the read-side analog of
the ``sources/`` "fetch -> cache into ``raw/``" convention).

The narrow port is :class:`DeckImporter`: ``fetch`` is the I/O + WAF + paste
boundary that produces the cached ``RawDeck``; ``normalize`` is PURE — it turns a
source-shaped ``RawDeck`` into the one canonical ``Deck`` (assemble ``DeckCard``s,
pass roles through the validator, set the name) and deliberately does **not**
resolve card names against the lake (name-only ``DeckCard``s are correct — the
read-path resolver enriches them). :func:`get_importer` is the registry/factory:
a caller hands a ref (or an explicit ``source=``) and gets the matching adapter,
or a loud ``ValueError`` naming the supported sources — the registry is the single
source of truth for which sources are supported.

Adapters register at import time by appending an instance under its ``source``
key to ``_IMPORTERS`` (see the bottom of this module) — the single registration
seam every phase follows: import the adapter class, instantiate it once, assign
``_IMPORTERS[importer.source] = importer``. Adding an entry there is the only way
to make a source resolvable via :func:`get_importer`.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from pipeline.contracts import Deck, DeckCard
from pipeline.sources.deck_import.raw import RawDeck, RawEntry

_log = logging.getLogger(__name__)

#: Commander-format size tolerance band for the size-sanity warning. A canonical
#: Commander deck is exactly 100 (99 maindeck + 1 commander); real lists carry a
#: partner/companion or a stray extra, so the band is a little wide on purpose —
#: it flags a deck that is *wildly* off (a mis-parse), not one off by a card.
_COMMANDER_SIZE_MIN = 98
_COMMANDER_SIZE_MAX = 101

__all__ = (
    'DeckImporter',
    'RawDeck',
    'RawEntry',
    'get_importer',
    'import_deck',
)


def _warn_if_off_size(deck: Deck) -> None:
    """Log a WARNING when a commander-format ``deck`` is wildly off 100 cards.

    The design's "surfaced, never silently shipped" size-sanity check: a source
    parse that lands a maindeck far from 99 (a mis-applied category rule, a source
    that dumped its maybeboard into the main list) is almost always a bug, so we
    surface it — as a stderr log via the module logger, NOT a raise (the deck is
    still returned; the CLI also reports size in P5, and a legitimately off-size
    list must not be un-importable).

    Only meaningful for a deck that HAS a commander (Commander format — the one
    format with a fixed 100-card target here); a deck with no commander is left
    alone (its target is format-dependent and not this helper's concern). Generic
    on purpose so a later API adapter (P3) can reuse it.
    """
    commanders = deck.commanders
    if not commanders:
        return
    total = sum(c.quantity for c in deck.maindeck) + sum(c.quantity for c in commanders)
    if not (_COMMANDER_SIZE_MIN <= total <= _COMMANDER_SIZE_MAX):
        _log.warning(
            'imported deck %r has an off-size commander deck: %d cards '
            '(expected %d-%d) — the source parse may be wrong; review before shipping.',
            deck.name,
            total,
            _COMMANDER_SIZE_MIN,
            _COMMANDER_SIZE_MAX,
        )


@runtime_checkable
class DeckImporter(Protocol):
    """The narrow port every deck-import adapter satisfies.

    ``source`` is the adapter's registry key (a stable source slug, e.g.
    ``'archidekt'``); :meth:`matches` sniffs whether a ref belongs to this source
    (URL host / file / ``-`` / raw text); :meth:`fetch` turns the ref into a
    cached :class:`~pipeline.sources.deck_import.raw.RawDeck` (the I/O + WAF + paste
    boundary); :meth:`normalize` turns that ``RawDeck`` into a canonical
    :class:`~pipeline.contracts.Deck` (PURE — no I/O, no card resolution).
    """

    source: str

    def matches(self, ref: str) -> bool:
        """True iff ``ref`` belongs to this source (host sniff / file / '-' / text)."""
        ...

    def fetch(self, ref: str, *, refresh: bool = False) -> RawDeck:
        """Resolve ``ref`` to a cached ``RawDeck`` (I/O + WAF boundary; caches).

        ``refresh=True`` forces a re-fetch: the adapter threads it into
        :func:`~pipeline.sources.deck_import.raw.load_or_fetch`.
        """
        ...

    def normalize(self, raw: RawDeck) -> Deck:
        """Turn a source-shaped ``RawDeck`` into the canonical ``Deck`` (pure)."""
        ...


#: The registry of known importers, keyed by source slug. Adding an adapter here
#: (via the registration block at the bottom of this module) is the only way to
#: make a new source resolvable via :func:`get_importer`. Populated at import
#: time; later phases append their adapter under its ``source`` key.
_IMPORTERS: dict[str, DeckImporter] = {}


def get_importer(ref: str, *, source: str | None = None) -> DeckImporter:
    """Return the :class:`DeckImporter` for ``ref`` (or an explicit ``source``).

    With ``source`` given, look that adapter up directly (an unknown source is a
    loud ``ValueError`` naming the supported sources). Otherwise dispatch by
    sniffing: the first registered adapter whose :meth:`DeckImporter.matches`
    returns True wins; if none match, raise a ``ValueError`` naming the supported
    sources. The registry is the single source of truth for what is supported.
    """
    supported = sorted(_IMPORTERS)
    if source is not None:
        try:
            return _IMPORTERS[source]
        except KeyError:
            raise ValueError(f'unknown source {source!r}; supported sources: {supported}') from None
    for importer in _IMPORTERS.values():
        if importer.matches(ref):
            return importer
    raise ValueError(f'no importer matches {ref!r}; supported sources: {supported}')


def import_deck(ref: str, *, source: str | None = None, refresh: bool = False) -> Deck:
    """Resolve ``ref`` to a canonical :class:`~pipeline.contracts.Deck`.

    Dispatches to the matching adapter (or the ``source=`` override), then
    ``fetch`` -> ``normalize``. ``fetch`` caches internally, so a fresh re-import
    performs no network call; ``refresh=True`` forces the adapter's cache to
    re-fetch (adapters thread it into
    :func:`~pipeline.sources.deck_import.raw.load_or_fetch`). ``normalize`` is
    pure and does not resolve card names — name-only ``DeckCard``s land, and the
    read-path resolver enriches them.
    """
    importer = get_importer(ref, source=source)
    raw = importer.fetch(ref, refresh=refresh)
    return importer.normalize(raw)


def _normalize_rawdeck(raw: RawDeck) -> Deck:
    """The shared ``RawDeck`` -> ``Deck`` body every adapter's ``normalize`` delegates to.

    Assembles a name-only :class:`~pipeline.contracts.DeckCard` per
    :class:`~pipeline.sources.deck_import.raw.RawEntry` (name, quantity, role) and
    sets ``Deck.name`` from ``raw.name``. Roles must already be canonical
    (``commander`` / ``sideboard`` / ``None``); they pass straight through the
    ``DeckCard`` role validator, which is the loud gate for an unknown role.

    It derives nothing else and — by contract — resolves NO card names against the
    lake/resolver: name-only ``DeckCard``s are intended (the read-path resolver
    enriches them), so this function keeps the "importer canonicalizes shape, not
    card data" invariant. Duplicate names pass through un-merged (two
    ``RawEntry(name='Forest')`` yield two ``DeckCard``s); qty aggregation is a
    resolver/store concern, not this shape stage's.
    """
    cards = [DeckCard(name=entry.name, quantity=entry.quantity, role=entry.role) for entry in raw.cards]
    return Deck(name=raw.name, cards=cards)


# --------------------------------------------------------------------------- #
# Adapter registration — the single seam every phase follows. Import the adapter,
# instantiate it once, and register the instance under its ``source`` key. The
# import is deferred to the bottom of the module so an adapter can safely import
# ``_normalize_rawdeck`` from here without a circular-import hazard.
# --------------------------------------------------------------------------- #


def _register(importer: DeckImporter) -> None:
    """Register ``importer`` under its ``source`` key (loud on a duplicate slug)."""
    if importer.source in _IMPORTERS:
        raise ValueError(f'duplicate importer source {importer.source!r}')
    _IMPORTERS[importer.source] = importer


from pipeline.sources.deck_import.archidekt import ArchidektImporter  # noqa: E402
from pipeline.sources.deck_import.edhrec import EdhrecImporter  # noqa: E402
from pipeline.sources.deck_import.moxfield import MoxfieldImporter  # noqa: E402
from pipeline.sources.deck_import.plaintext import PlaintextImporter  # noqa: E402

_register(PlaintextImporter())
_register(ArchidektImporter())
_register(EdhrecImporter())
_register(MoxfieldImporter())
