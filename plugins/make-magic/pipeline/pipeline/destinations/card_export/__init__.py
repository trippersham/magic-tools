"""Card-export destination — render + validate a SINGLE card for a target format.

A deck-export adapter is really "render each card, then join" — but the per-card
step is where target-specific truth lives: how a card's line is written AND
whether the target can actually use it. Factoring that into a :class:`CardExporter`
port lets every deck adapter (``forge_dck`` now; ``arena``/``moxfield``/``text``
later) COMPOSE its paired card exporter, so card rendering and validation run
identically across destinations instead of each re-implementing them (gap 0.4).

The first adapter is :class:`ForgeDckCardExporter`: it renders the ``<qty> <name>``
line and validates a card two ways — resolution (does it have a Scryfall
``oracle_id``?) and target availability (does Forge's card DB actually have the
name?, via an injected :class:`CardAvailability` oracle such as
:class:`~pipeline.sim.forge_card_index.ForgeCardIndex`). The availability oracle
is a structural port so tests inject a tiny fake and never need real Forge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pipeline.destinations.deck_export.validation import (
    CardIssue,
    IssueKind,
    Severity,
)

if TYPE_CHECKING:
    from pipeline.contracts import Card, DeckCard

__all__ = (
    'CardAvailability',
    'CardExporter',
    'ForgeDckCardExporter',
    'get_card_exporter',
)


@runtime_checkable
class CardAvailability(Protocol):
    """Structural port for "does this target contain this card name?"

    :class:`~pipeline.sim.forge_card_index.ForgeCardIndex` satisfies it; tests
    pass a tiny hand-built stand-in.
    """

    def has(self, card_name: str) -> bool: ...


@runtime_checkable
class CardExporter(Protocol):
    """Render + validate a single card for one target format.

    ``format`` pairs with a :class:`~pipeline.destinations.deck_export.DeckExporter`
    of the same slug. :meth:`render` produces the per-card text; :meth:`validate`
    returns a :class:`CardIssue` (or ``None`` when the card is fine).
    """

    format: str

    def render(self, card: DeckCard) -> str: ...

    def validate(self, card: Card) -> CardIssue | None: ...


class ForgeDckCardExporter:
    """The ``forge_dck`` card adapter: ``<qty> <name>`` render + Forge validation.

    ``availability`` is optional: without it, :meth:`validate` performs only the
    destination-agnostic resolution check (``oracle_id``); with it, it also runs
    the Forge target-availability check (the blocking one). Absence outranks
    unresolution — a card missing from Forge is the load-breaking problem, so it
    is reported even if the card is also name-only.
    """

    format = 'forge_dck'

    def __init__(self, availability: CardAvailability | None = None) -> None:
        self._availability = availability

    def render(self, card: DeckCard) -> str:
        """One maindeck/commander line: ``<quantity> <name>`` (name verbatim)."""
        return f'{card.quantity} {card.name}'

    def validate(self, card: Card) -> CardIssue | None:
        """Classify a card for Forge: ABSENT (blocking) > UNRESOLVED (warning) > ok."""
        name = card.name
        if self._availability is not None and not self._availability.has(name):
            return CardIssue(
                card_name=name,
                kind=IssueKind.ABSENT_FROM_TARGET,
                severity=Severity.BLOCKING,
                detail='not in Forge card DB',
            )
        if card.oracle_id is None:
            return CardIssue(
                card_name=name,
                kind=IssueKind.UNRESOLVED,
                severity=Severity.WARNING,
                detail='name-only (no Scryfall oracle_id)',
            )
        return None


#: Registry of card exporters, keyed by format slug (mirrors ``get_exporter``).
_CARD_EXPORTERS: dict[str, type[ForgeDckCardExporter]] = {
    ForgeDckCardExporter.format: ForgeDckCardExporter,
}


def get_card_exporter(format: str, *, availability: CardAvailability | None = None) -> CardExporter:
    """Return the :class:`CardExporter` for ``format`` (optionally with availability).

    Raises ``ValueError`` for an unknown format, naming the supported ones.
    """
    try:
        exporter_cls = _CARD_EXPORTERS[format]
    except KeyError:
        raise ValueError(
            f'unknown card-export format {format!r}; supported formats: {sorted(_CARD_EXPORTERS)}'
        ) from None
    return exporter_cls(availability=availability)
