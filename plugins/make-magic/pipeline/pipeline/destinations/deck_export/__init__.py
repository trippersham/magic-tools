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

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pipeline.contracts import Deck
from pipeline.destinations.deck_export.forge_dck import ForgeDckExporter
from pipeline.destinations.deck_export.naming import safe_deck_stem
from pipeline.destinations.deck_export.validation import (
    CardIssue,
    DeckExportError,
    ExportResult,
    IssueKind,
    Severity,
    ValidationReport,
    export_checked,
)

if TYPE_CHECKING:
    from pipeline.destinations.card_export import CardAvailability

__all__ = (
    'CardIssue',
    'DeckExportError',
    'DeckExporter',
    'ExportResult',
    'ForgeDckExporter',
    'IssueKind',
    'Severity',
    'ValidationReport',
    'export_checked',
    'get_exporter',
    'safe_deck_stem',
)


@runtime_checkable
class DeckExporter(Protocol):
    """The narrow port every deck-export adapter satisfies.

    ``format`` is the adapter's registry key (a stable format slug, e.g.
    ``'forge_dck'``); :meth:`export` renders a :class:`~pipeline.contracts.Deck`
    into that format's file text (LENIENT — always renders); :meth:`validate`
    reports per-card issues for that target. Pair with :func:`export_checked` for
    a fail-before-emit gate.
    """

    format: str

    def export(self, deck: Deck) -> str:
        """Render ``deck`` into this format's file text (lenient)."""
        ...

    def validate(self, deck: Deck) -> ValidationReport:
        """Report per-card resolution/availability issues for this target."""
        ...


#: The registry of known exporters, keyed by format slug. Adding an adapter here
#: (and to ``__all__`` if exported) is the ONLY way to make a new format
#: resolvable via :func:`get_exporter`.
_EXPORTERS: dict[str, type[ForgeDckExporter]] = {
    ForgeDckExporter.format: ForgeDckExporter,
}


def get_exporter(format: str, *, availability: CardAvailability | None = None) -> DeckExporter:
    """Return the :class:`DeckExporter` for ``format``.

    ``availability`` (optional) is a target card-availability oracle (e.g. a
    :class:`~pipeline.sim.forge_card_index.ForgeCardIndex`) injected into the
    adapter so :meth:`DeckExporter.validate` can flag cards ABSENT from the
    target; omit it for render-only or resolution-only validation. Raises
    ``ValueError`` for an unknown format, naming the ones that ARE supported.
    """
    try:
        exporter_cls = _EXPORTERS[format]
    except KeyError:
        raise ValueError(f'unknown deck-export format {format!r}; supported formats: {sorted(_EXPORTERS)}') from None
    return exporter_cls(availability=availability)
