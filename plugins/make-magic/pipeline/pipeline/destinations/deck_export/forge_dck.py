"""Forge ``.dck`` adapter — render a :class:`~pipeline.contracts.Deck` to INI text.

Emits the INI shape MTG Forge's headless ``sim`` parses (empirically verified in
``~/mtg-sim-lab/forge_backend.py`` against Forge 2.0.13):

    [metadata]
    Name=<deck name>
    Deck Type=Constructed        # or "Commander" when the deck has commander(s)
    [Commander]                  # emitted ONLY when commanders exist
    <qty> <Commander Card Name>
    [Main]
    <qty> <Card Name>
    ...
    [Sideboard]

Rendering rules:
    - Every non-commander ``DeckCard`` becomes one ``<quantity> <name>`` line in
      ``[Main]`` (basics arrive as a single DeckCard with ``quantity>1`` — one
      line, not repeated). Set codes are omitted (the DeckCard carries none).
    - Commanders (``deck.commanders`` / ``role == 'commander'``) go in
      ``[Commander]`` and are EXCLUDED from ``[Main]``; their presence flips
      ``Deck Type`` to ``Commander``.
    - DFC / split names (``A // B``) are written verbatim — Forge matches the
      full combined name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline.contracts import Deck, DeckCard
from pipeline.destinations.deck_export.validation import ValidationReport

if TYPE_CHECKING:
    from pipeline.destinations.card_export import CardAvailability

__all__ = ('ForgeDckExporter',)


class ForgeDckExporter:
    """Render a :class:`~pipeline.contracts.Deck` into a Forge ``.dck`` INI string.

    Satisfies the :class:`~pipeline.destinations.deck_export.DeckExporter` port:
    exposes ``format == 'forge_dck'``, :meth:`export`, and :meth:`validate`. The
    per-card render + validation are delegated to a composed
    :class:`~pipeline.destinations.card_export.ForgeDckCardExporter` (gap 0.4), so
    an ``availability`` oracle (a Forge card index) — when injected — drives the
    blocking "card absent from Forge" check consistently.
    """

    format = 'forge_dck'

    def __init__(self, availability: CardAvailability | None = None) -> None:
        # Imported lazily to avoid a package import cycle (card_export imports the
        # shared validation vocabulary from this package).
        from pipeline.destinations.card_export import ForgeDckCardExporter

        self._card = ForgeDckCardExporter(availability=availability)

    def validate(self, deck: Deck) -> ValidationReport:
        """Validate every card in ``deck`` for Forge, collecting the issues.

        Iterates ``deck.cards`` (commanders included — they carry ``role ==
        'commander'`` but are still cards Forge must have), runs the composed
        card exporter's per-card check, and returns a :class:`ValidationReport`.
        With no ``availability`` injected this reports only ``UNRESOLVED``
        (name-only) warnings; with a Forge card index it also flags
        ``ABSENT_FROM_TARGET`` (blocking).
        """
        issues = tuple(issue for card in deck.cards if (issue := self._card.validate(card)) is not None)
        return ValidationReport(deck_name=deck.name, issues=issues)

    def export(self, deck: Deck) -> str:
        """Render ``deck`` to Forge ``.dck`` INI text (no trailing newline).

        Commanders are derived via :attr:`Deck.commanders` (``role ==
        'commander'``) and excluded from ``[Main]``; their presence sets
        ``Deck Type=Commander``.
        """
        commanders = deck.commanders
        deck_type = 'Commander' if commanders else 'Constructed'

        lines = ['[metadata]', f'Name={deck.name}', f'Deck Type={deck_type}']

        if commanders:
            lines.append('[Commander]')
            # Forge parses EVERY deck line as `<qty> <name>` — a bare name fails
            # to load the commander. Use the same `<qty> <name>` form as [Main]
            # (empirically verified against Forge 2.0.13 via forge_backend.py).
            lines.extend(self._card_line(c) for c in commanders)

        lines.append('[Main]')
        lines.extend(self._card_line(card) for card in deck.cards if card.role != 'commander')

        lines.append('[Sideboard]')
        return '\n'.join(lines)

    def _card_line(self, card: DeckCard) -> str:
        """Render one card line, delegating to the composed card exporter."""
        return self._card.render(card)
