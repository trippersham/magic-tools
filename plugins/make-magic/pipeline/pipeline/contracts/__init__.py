"""Edge contracts — Pydantic v2 boundary models (the ONLY place Pydantic lives).

These are the objects a skill / MCP tool / future UI receives. Their generated
JSON Schema (see export_schemas.py) is the single source of truth for MCP tool
inputSchema/outputSchema and future auto-forms.
"""

from __future__ import annotations

from pipeline.contracts.models import (
    Card,
    Deck,
    DeckLine,
    FactSheet,
    FactSheetCard,
    FactSheetCardAdvantage,
    FactSheetCoverage,
    FactSheetFocusRelative,
    FactSheetInteraction,
    FactSheetMana,
    FactSheetShape,
    FactSheetStructural,
    InventoryRow,
    TradeRow,
)

__all__ = [
    "Card",
    "Deck",
    "DeckLine",
    "FactSheet",
    "FactSheetCard",
    "FactSheetCardAdvantage",
    "FactSheetCoverage",
    "FactSheetFocusRelative",
    "FactSheetInteraction",
    "FactSheetMana",
    "FactSheetShape",
    "FactSheetStructural",
    "InventoryRow",
    "TradeRow",
]
