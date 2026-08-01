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

from pipeline.contracts import Deck, DeckCard

__all__ = ('ForgeDckExporter',)


class ForgeDckExporter:
    """Render a :class:`~pipeline.contracts.Deck` into a Forge ``.dck`` INI string.

    Satisfies the :class:`~pipeline.destinations.deck_export.DeckExporter` port:
    exposes ``format == 'forge_dck'`` and :meth:`export`.
    """

    format = 'forge_dck'

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

    @staticmethod
    def _card_line(card: DeckCard) -> str:
        """Render one maindeck card as ``<quantity> <name>`` (name verbatim)."""
        return f'{card.quantity} {card.name}'
