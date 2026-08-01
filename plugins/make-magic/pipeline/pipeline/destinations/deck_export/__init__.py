"""Deck-export destination — render a :class:`~pipeline.contracts.Deck` to a file.

A GENERAL export surface behind a narrow ``typing.Protocol`` port
(:class:`DeckExporter`): each concrete adapter renders a ``Deck`` into ONE
external deck-file format's text. The first adapter targets MTG Forge's ``.dck``
INI (:class:`~pipeline.destinations.deck_export.forge_dck.ForgeDckExporter`) so a
sim backend can consume our decks; more formats (e.g. ``.txt`` / MTGO / Arena)
can register later without touching callers.

:func:`get_exporter` is the registry/factory: a caller asks for a format string
and gets the matching adapter, or a loud ``ValueError`` for an unknown one — the
registry is the single source of truth for which formats are supported.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pipeline.contracts import Deck
from pipeline.destinations.deck_export.forge_dck import ForgeDckExporter

__all__ = ('DeckExporter', 'ForgeDckExporter', 'get_exporter')


@runtime_checkable
class DeckExporter(Protocol):
    """The narrow port every deck-export adapter satisfies.

    ``format`` is the adapter's registry key (a stable format slug, e.g.
    ``'forge_dck'``); :meth:`export` renders a :class:`~pipeline.contracts.Deck`
    into that format's file text.
    """

    format: str

    def export(self, deck: Deck) -> str:
        """Render ``deck`` into this format's file text."""
        ...


#: The registry of known exporters, keyed by format slug. Adding an adapter here
#: (and to ``__all__`` if exported) is the ONLY way to make a new format
#: resolvable via :func:`get_exporter`.
_EXPORTERS: dict[str, type[DeckExporter]] = {
    ForgeDckExporter.format: ForgeDckExporter,
}


def get_exporter(format: str) -> DeckExporter:
    """Return the :class:`DeckExporter` for ``format``.

    Raises ``ValueError`` for an unknown format (the registry is the closed set
    of supported formats), naming the ones that ARE supported.
    """
    try:
        exporter_cls = _EXPORTERS[format]
    except KeyError:
        raise ValueError(
            f'unknown deck-export format {format!r}; supported formats: {sorted(_EXPORTERS)}'
        ) from None
    return exporter_cls()
