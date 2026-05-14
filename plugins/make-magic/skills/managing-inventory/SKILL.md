---
name: managing-inventory
description: >-
  Manage a Magic: The Gathering card inventory in Airtable with Scryfall API enrichment.
  Covers: (1) Adding cards and backfilling metadata/prices, (2) Loading decks from
  cardlist files, (3) Recording trades between locations with lifecycle tracking.
  Use when user says "add a card", "load a deck", "record a trade", "update prices",
  "backfill metadata", or references cardlist files, Scryfall data, or Airtable
  operations on the Magic Inventory base. Do NOT trigger for card photo analysis
  or image recognition tasks.
user-invocable: "true"
argument-hint: "[cards|decks|trades]"
---

# MTG Inventory Manager

Manage a Magic: The Gathering card inventory in Airtable, enriched via Scryfall API.

## Prerequisites

- **Airtable connector** — enable via `/mcp` in Claude Code, authenticate with your Airtable account
- **Airtable base** — clone the shared base template (see plugin README)
- **uv** — scripts use PEP 723 inline metadata; run with `uv run --script`

## Workflows

Route to the appropriate reference based on the user's request:

| Task | Reference |
|------|-----------|
| Add individual cards to collection | [references/cards.md](references/cards.md) |
| Backfill or update metadata/prices | [references/cards.md](references/cards.md) |
| Load a deck from a cardlist file | [references/decks.md](references/decks.md) |
| Record a trade between locations | [references/trades.md](references/trades.md) |
| Airtable table/field schema | [references/airtable-schema.md](references/airtable-schema.md) |

## Scryfall API

- **Exact name:** `GET /cards/named?exact={url-encoded-name}`
- **All printings:** `GET /cards/search?q=!"{name}"&unique=prints`
- **Rate limit:** 120ms between requests. On HTTP 429, wait 65s then retry.
- **Double-faced cards:** `image_uris` is null at top level; use `card_faces[0].image_uris`. `cmc` and `prices` are always top-level.
- **Headers:** `User-Agent: MTGInventoryManager/1.0` and `Accept: application/json` required.

## Field Mapping: Scryfall to Airtable

| Scryfall | Airtable |
|----------|----------|
| `type_line` | Card Type |
| `mana_cost` | Mana Cost |
| `cmc` | CMC |
| `oracle_text` | Oracle Text |
| `power` + `toughness` | Power / Toughness (as "P/T") |
| `image_uris.art_crop` | Card Art |
| `scryfall_uri` | Scryfall URL |
| `prices.usd` | Price (TCGPlayer) |
| `set_name` | Sets (multiSelect) |

## Shared Gotchas

- `mcp__airtable__create_record` is singular — no batch create via MCP
- multiSelect fields auto-create choices (no pre-creation needed)
- Double-faced cards: use `card_faces[0]` for image_uris, mana_cost, oracle_text
- Use background agents for bulk operations to avoid blocking
