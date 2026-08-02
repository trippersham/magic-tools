"""Edge contracts — Pydantic v2 boundary models (the ONLY place Pydantic lives).

These are the objects a skill / MCP tool / future UI receives. When a consumer
needs a language-agnostic contract (MCP tool inputSchema/outputSchema, TS types,
auto-forms), generate JSON Schema on demand via ``Model.model_json_schema()`` —
the model is the single source of truth.
"""

from __future__ import annotations

from pipeline.contracts.models import (
    DECK_CARD_ROLES,
    ROLE_COMMANDER,
    ROLE_SIDEBOARD,
    Card,
    ChaseCard,
    Deck,
    DeckCard,
    FactSheet,
    FactSheetCard,
    FactSheetCardAdvantage,
    FactSheetCoverage,
    FactSheetFocusRelative,
    FactSheetInteraction,
    FactSheetMana,
    FactSheetShape,
    FactSheetStructural,
    OwnedCard,
    Spoiler,
    Trade,
)

__all__ = (
    'DECK_CARD_ROLES',
    'ROLE_COMMANDER',
    'ROLE_SIDEBOARD',
    'Card',
    'ChaseCard',
    'Deck',
    'DeckCard',
    'FactSheet',
    'FactSheetCard',
    'FactSheetCardAdvantage',
    'FactSheetCoverage',
    'FactSheetFocusRelative',
    'FactSheetInteraction',
    'FactSheetMana',
    'FactSheetShape',
    'FactSheetStructural',
    'OwnedCard',
    'Spoiler',
    'Trade',
)
