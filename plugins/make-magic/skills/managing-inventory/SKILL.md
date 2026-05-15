---
name: managing-inventory
description: >
  Manage MTG card inventory, decks, and trades in Airtable with Scryfall API enrichment.
  TRIGGER when: user asks to add cards, load a deck, record a trade, update prices,
  backfill card metadata (CMC, art, prices), or any Airtable operation on the Magic Inventory base.
  Also trigger when user references cardlist files, Scryfall data, or deck management.
---

# MTG Inventory Manager

Manage a Magic: The Gathering card inventory in Airtable, enriched via Scryfall API. For full table/field IDs, read `references/airtable-schema.md`.

## Prerequisites

- **Airtable connector** — enable via `/mcp` in Claude Code, authenticate with your Airtable account
- **Airtable base** — clone the shared base template (see plugin README)
- **Scryfall API** — free, no auth required
- **uv** — scripts use PEP 723 inline metadata; run with `uv run --script`

## Workflows

### 1. Load a Deck from a Cardlist

Cardlists live in `cardlists/<deck-name>.txt` (format: `<qty> <card name>` per line).

1. Parse the cardlist inline — format is simple: one line per entry, `<qty> <card name>`. Claude can parse this directly.
2. Confirm the commander with the user
3. Check which cards already exist in Cards table (`mcp__airtable__search_records`)
4. Fetch Scryfall metadata for new cards: write `[{"name": "...", "id": ""}]` to `/tmp/<deck>-to-fetch.json`, then run `uv run --script scripts/scryfall_batch.py /tmp/<deck>-to-fetch.json /tmp/<deck>-scryfall.json`
5. Create card records via `mcp__airtable__create_record` with all Scryfall fields (see Field Mapping below). Set Number Owned = 1, Sets, Sources as appropriate
6. For existing cards: increment Number Owned
7. Create/update the Deck record: Name, Format, Commander (link), Cards (link all unique non-commander non-basic-land cards), basic land counts (Plains/Islands/Swamps/Mountains/Forests/Wastes fields)
8. For non-singleton decks (cards with qty > 1): set `Repeat Cards Count` on Deck and `Repeat Number in Decks` on each multi-copy Card. See "Multi-Copy Cards" in `references/airtable-schema.md`
9. Verify Deck Size formula matches expected total

**Gotchas:**
- `create_record` is singular — no batch create via MCP
- multiSelect fields auto-create choices (no pre-creation needed) — use `typecast=True` in pyairtable
- Double-faced cards: use `card_faces[0]` for image_uris, mana_cost, oracle_text
- Commander links via Decks.Commander field, NOT the Cards link field
- Use background agents for decks with many new cards to avoid blocking
- Non-singleton decks (60-card, Draft, etc.) need Repeat Cards Count — see `references/airtable-schema.md`

### 2. Record a Trade

Trades track card movement between locations.

1. Create card records for any new cards (Scryfall fetch as above)
2. Create Trade record:
   - **Date**, **Status** (Draft/Planned/Completed)
   - **From (Source)** — category: Library, Deck, Store, Person
   - **From (Deck)** — link to Deck record (only when Source = "Deck")
   - **To (Destination)** — category: Library, Deck, Store, Person
   - **To (Deck)** — link to Deck record (only when Destination = "Deck")
   - **Cards into Destination** / **Cards out of Destination** — links to Cards
3. Update affected deck Cards link fields
4. Update Number Owned if cards enter/leave the collection

**Source/Destination model:** These are WHERE categories. Deck fields add specificity.
Example — swap Horizon Stone for Lavaleaper in Ozai: From (Source) = "Library", To (Destination) = "Deck", To (Deck) = Ozai, Cards into Destination = [Lavaleaper], Cards out of Destination = [Horizon Stone].

### 3. Backfill / Update Metadata

For bulk updates (prices, art, CMC, etc.) across all cards:

1. Page through all Cards records (`mcp__airtable__list_records` with offset)
2. Write names + record IDs to `/tmp/backfill-input.json`
3. Run `uv run --script scripts/scryfall_batch.py /tmp/backfill-input.json /tmp/backfill-output.json`
4. Build update batches (max 10 per `mcp__airtable__update_records`)
5. Push batches. Use background agents for large backfills.

**Price updates:** If `prices.usd` is null, try alternate printings: `https://api.scryfall.com/cards/search?q=!"CARD NAME"&unique=prints` — pick cheapest non-null USD printing.

### 4. Add Individual Cards

1. Fetch: `https://api.scryfall.com/cards/named?exact=<url-encoded-name>`
2. Create record with all metadata fields
3. Set Number Owned, Sets, Sources, Condition

## Scryfall API

- **Exact name:** `GET /cards/named?exact={name}`
- **All printings:** `GET /cards/search?q=!"{name}"&unique=prints`
- **Rate limit:** 100ms between requests. On HTTP 429, wait 65s then retry.
- **Double-faced cards:** `image_uris` is null at top level; use `card_faces[0].image_uris`. `cmc` and `prices` are always top-level.

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
