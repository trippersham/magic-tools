---
name: managing-inventory
description: >
  Manage MTG card inventory, decks, and trades in Airtable with Scryfall API enrichment.
  TRIGGER when: user asks to add cards, load a deck, record a trade, update prices,
  backfill card metadata (CMC, art, prices), or any Airtable CRUD operation on the
  Magic Inventory base. Also trigger when user references cardlist files, Scryfall data,
  or deck management. "load this deck", "add these cards", "record a trade",
  "update prices", "backfill metadata", "increment owned count".
  SKIP when: user asks about deck strategy, card evaluation, Chase Cards prioritization,
  or what cards to add/remove. Those are strategy tasks, not inventory CRUD.
---

# MTG Inventory Manager

Manage a Magic: The Gathering card inventory in Airtable, enriched via Scryfall API.

<primary-constraint>
## Airtable Efficiency: Batch Operations Over N+1 Queries

**Constraint:** Always prefer batch operations over individual record calls.

<rationale>
N+1 query patterns are the most common efficiency failure in this skill. Each Airtable MCP call has network overhead and token cost for the response. Making 50 individual `get_record` calls when a single `list_records` with `filterByFormula` returns all 50 is 50x the overhead.
</rationale>

**Do this:**
- Use `search_records` with `filterByFormula` to check multiple cards at once: `OR(FIND("Card A", {Card Name}), FIND("Card B", {Card Name}), ...)`
- Batch updates with `update_records` (up to 10 records per call)
- Use `list_records` with `fields` parameter to request only needed fields

**Not this:**
- Individual `get_record` calls in a loop to check if cards exist
- Separate `update_records` calls for each record
- Loading full records when you only need one or two fields

<reference file="airtable-patterns.md" section="Query Optimization">
Full efficiency patterns including detailLevel, filterByFormula, and batch updates.
</reference>
</primary-constraint>

<red-flags>
## Red Flags: Stop and Reconsider

If you catch yourself about to:
- **Loop over card names calling `get_record` for each** -- Stop. Use `search_records` with `filterByFormula` containing an OR of FIND clauses.
- **Call `update_records` once per record** -- Stop. Batch up to 10 records per call.
- **Fetch full records just to check existence** -- Stop. Use `search_records` with `filterByFormula`; it returns matching records directly.
- **Load all records then filter client-side** -- Stop. Push the filter to Airtable via `filterByFormula`.
</red-flags>

## Prerequisites

- **Airtable connector** -- enable via `/mcp` in Claude Code, authenticate with your Airtable account
- **Airtable base** -- clone the shared base template (see plugin README)
- **Scryfall API** -- free, no auth required
- **uv** -- scripts use PEP 723 inline metadata; run with `uv run --script`

<reference file="airtable-schema.md" section="Tables">
Table IDs, field IDs, and full schema reference.
</reference>

## Operation Router

| User wants to... | Go to |
|------------------|-------|
| Load a deck from a cardlist file | [1. Load a Deck](#1-load-a-deck-from-a-cardlist) |
| Record card movement between locations | [2. Record a Trade](#2-record-a-trade) |
| Update prices/art/metadata across all cards | [3. Backfill Metadata](#3-backfill--update-metadata) |
| Add a single card to inventory | [4. Add Individual Cards](#4-add-individual-cards) |

## Workflows

### 1. Load a Deck from a Cardlist

Cardlists live in `cardlists/<deck-name>.txt` (format: `<qty> <card name>` per line).

<reference file="decks.md" section="Load a Deck from a Cardlist">
Full deck loading workflow with steps and gotchas.
</reference>

**Summary:**
1. Parse the cardlist inline -- one line per entry, `<qty> <card name>`
2. Confirm the commander with the user
3. Check existing cards via `search_records` with batched `filterByFormula` (NOT individual lookups)
4. Fetch Scryfall metadata for new cards via `${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_batch.py`
5. Create card records, increment Number Owned for existing cards
6. Create/update Deck record with commander link, card links, basic land counts
7. Handle multi-copy cards via Repeat fields (see airtable-schema.md)
8. Verify Deck Size formula matches expected total

**Gotchas:**
- `create_record` is singular -- no batch create via MCP. Use background agents for >5 new cards.
- Commander links via `Decks.Commander` field, NOT the Cards link field. The model defaults to linking via Cards because it seems simpler; Commander is a separate relationship.
- Double-faced cards: `image_uris` is null at top level; use `card_faces[0].image_uris`. The model often forgets this and gets null values.
- `multiSelect` fields auto-create choices on update; no pre-creation needed.

### 2. Record a Trade

Trades track card movement between locations using a Source/Destination model.

<reference file="trades.md" section="Source/Destination Model">
Full trade model with lifecycle and examples.
</reference>

**Summary:**
1. Create card records for any new cards (Scryfall fetch as in workflow 1)
2. Create Trade record with Date, Status, Source/Destination, and card links
3. Update affected deck Cards link fields
4. Update Number Owned if cards enter/leave the collection

**Source/Destination model:** Source and Destination are categories (Library, Deck, Store, Person). The Deck fields provide specificity when the category is "Deck".

**Example:** Swap Horizon Stone for Lavaleaper in Ozai deck from Library:
- From (Source) = "Library"
- To (Destination) = "Deck", To (Deck) = Ozai
- Cards into Destination = [Lavaleaper]
- Cards out of Destination = [Horizon Stone]

### 3. Backfill / Update Metadata

For bulk updates (prices, art, CMC, etc.) across all cards:

<reference file="cards.md" section="Backfill / Update Metadata">
Full backfill workflow.
</reference>

**Summary:**
1. Page through all Cards records (`list_records` with offset)
2. Write names + record IDs to `/tmp/backfill-input.json`
3. Run `uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_batch.py /tmp/backfill-input.json /tmp/backfill-output.json`
4. Build update batches (max 10 per `update_records`)
5. Push batches. Use background agents for large backfills.

**Price updates:** If `prices.usd` is null, try alternate printings via `/cards/search?q=!"CARD NAME"&unique=prints` -- pick cheapest non-null USD printing.

### 4. Add Individual Cards

<reference file="cards.md" section="Add Individual Cards">
Single card creation workflow.
</reference>

1. Fetch: `GET https://api.scryfall.com/cards/named?exact=<url-encoded-name>`
2. Create record with all metadata fields (see Field Mapping below)
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

<references>
## Reference Guide

| When you need to... | Read |
|---------------------|------|
| Check table/field IDs, schema details, or multi-copy handling | [airtable-schema.md](references/airtable-schema.md) |
| Optimize Airtable queries or batch operations | [airtable-patterns.md](references/airtable-patterns.md) |
| Execute deck loading workflow in detail | [decks.md](references/decks.md) |
| Execute card operations (add, backfill) in detail | [cards.md](references/cards.md) |
| Execute trade workflows in detail | [trades.md](references/trades.md) |
</references>
