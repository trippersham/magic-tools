---
name: managing-inventory
description: >
  Manage MTG card inventory, decks, and trades in Airtable with Scryfall API enrichment.
  Use for ANY Airtable operation on the Magic Inventory base — adding cards, loading decks,
  recording trades, updating prices, backfilling metadata (CMC, art, oracle text), querying
  deck contents, or analyzing inventory. TRIGGER when: user mentions "add cards", "load deck",
  "record trade", "update prices", "backfill metadata", "what cards are in [deck]", "deck stats",
  cardlist files, Scryfall data, deck management, or any card inventory question.
---

# MTG Inventory Manager

<efficient-query-constraint>
## Query Efficiency Constraint

**Always use the `fields` parameter on `list_records` and `search_records`.** Fetching all 25+ fields per card record consumes tokens rapidly and slows responses. Specify only the fields needed for the current operation.

<rationale>
Each Cards record contains 25+ fields. A 100-card deck fetch without `fields` returns ~50KB of JSON. With targeted fields, the same query returns ~5KB. Token cost directly impacts response quality and latency.
</rationale>

**Alternative to n+1 queries:** Use `filterByFormula` to fetch related records in a single paginated call instead of looping through record IDs.

```
// WRONG: n+1 pattern (DO NOT DO THIS)
for each card_id in deck.Cards:
    get_record(card_id)  // 100 separate API calls

// CORRECT: single formula-filtered query
list_records(
  tableId: Cards,
  fields: ["Card Name", "Mana Cost", "CMC", "Card Type"],
  filterByFormula: "FIND('Ozai', ARRAYJOIN({Decks}))"
)  // One paginated call returns all cards
```

<reference file="query-patterns.md" section="Common Field Sets">
For pre-defined field sets by use case (Play Mechanics, Inventory, Deck Building).
</reference>
</efficient-query-constraint>

<red-flags>
If you catch yourself thinking:
- "I'll just fetch all records and filter in code"
- "I need to call get_record for each card ID in the deck"
- "I don't know which fields I need, so I'll get all of them"

**STOP.** Use `filterByFormula` with targeted `fields`. Read `references/query-patterns.md` for the correct pattern.
</red-flags>

---

## Base Configuration

**Base ID:** `appw7QPMoqktrgDc1`

| Table | ID | Purpose |
|-------|----|---------|
| Cards | `tbl3UgZZPJGQhEFo8` | Normalized inventory — 1 row per card title |
| Decks | `tblIfqVuVHNQza1K3` | Deck configurations |
| Trades | `tblgqqIvTuz0l5SZM` | Card movement tracking |
| Magic Cards | `tbliSupwHYSUcAY7l` | Legacy (do not modify) |

**Schema tools:** When calling `list_tables` or `describe_table`, use `detailLevel: "identifiersOnly"` to minimize token cost.

---

## Prerequisites

- **Airtable connector** — enable via `/mcp` in Claude Code, authenticate with your Airtable account
- **uv** — scripts use PEP 723 inline metadata; run with `uv run --script`
- **Scryfall API** — free, no auth required

---

<operation-router>
## Operation Router

| # | User Intent | Section |
|---|-------------|---------|
| 1 | Load a deck from a cardlist file | [Load a Deck](#1-load-a-deck-from-a-cardlist) |
| 2 | Query cards in a deck / deck stats | [Query Deck Contents](#2-query-deck-contents) |
| 3 | Record a trade between locations | [Record a Trade](#3-record-a-trade) |
| 4 | Backfill/update card metadata | [Backfill Metadata](#4-backfill--update-metadata) |
| 5 | Add individual cards | [Add Cards](#5-add-individual-cards) |
| 6 | Analyze deck composition | [Deck Analysis](#6-deck-analysis) |
</operation-router>

---

### 1. Load a Deck from a Cardlist

Cardlists live in `cardlists/<deck-name>.txt` (format: `<qty> <card name>` per line).

<reference file="decks.md" section="Load a Deck from a Cardlist">
For detailed step-by-step workflow.
</reference>

**Quick steps:**
1. Parse the cardlist inline — one line per entry, `<qty> <card name>`
2. Confirm the commander with the user
3. Check existing cards via `search_records` with `fields: ["Card Name", "Number Owned"]`
4. Fetch Scryfall metadata for new cards via batch script
5. Create card records with Scryfall fields
6. Create/update Deck record with land counts and card links

**Gotchas:**
- `create_record` is singular — no batch create via MCP
- Commander links via Decks.Commander field, NOT the Cards link field
- Double-faced cards: use `card_faces[0]` for image_uris, mana_cost, oracle_text
- Use background agents for decks with many new cards

<reference file="airtable-schema.md" section="Multi-Copy Cards">
For non-singleton decks (60-card, Draft), see the Repeat Cards Count pattern.
</reference>

---

### 2. Query Deck Contents

**To list all cards in a deck:**

```
mcp__airtable__list_records(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tbl3UgZZPJGQhEFo8",
  fields: ["Card Name", "Mana Cost", "Card Type", "CMC", "Oracle Text", "Power / Toughness"],
  filterByFormula: "FIND('DeckName', ARRAYJOIN({Decks}))"
)
```

**To get deck overview:**

```
mcp__airtable__list_records(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tblIfqVuVHNQza1K3",
  fields: ["Name", "Format", "Owner", "Deck Size", "Commander"],
  filterByFormula: "{Name}='DeckName'"
)
```

<reference file="query-patterns.md" section="Formula Patterns">
For complex queries: cards by type within deck, CMC distribution, missing metadata.
</reference>

---

### 3. Record a Trade

Trades track card movement between locations using a category + specificity model.

<reference file="trades.md" section="Source/Destination Model">
Full model explanation with examples.
</reference>

**Quick pattern:**
- **From (Source)** / **To (Destination)** — category: Library, Deck, Store, Person
- **From (Deck)** / **To (Deck)** — link when category = "Deck"
- **Cards into/out of Destination** — card links

**Example:** Swap Horizon Stone for Lavaleaper in Ozai deck from Library:
- From (Source) = "Library"
- To (Destination) = "Deck", To (Deck) = Ozai
- Cards into Destination = [Lavaleaper]
- Cards out of Destination = [Horizon Stone]

---

### 4. Backfill / Update Metadata

<reference file="cards.md" section="Backfill / Update Metadata">
Full batch update workflow with script commands.
</reference>

**Quick steps:**
1. Page through Cards with `list_records` and `fields: ["Card Name"]` + offset
2. Write names + record IDs to `/tmp/backfill-input.json`
3. Run: `uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_batch.py /tmp/backfill-input.json /tmp/backfill-output.json`
4. Batch update via `update_records` (max 10 per call)

**Price updates:** If `prices.usd` is null, fetch alternate printings: `GET /cards/search?q=!"CARD NAME"&unique=prints` — pick cheapest non-null USD.

---

### 5. Add Individual Cards

1. Fetch: `GET /cards/named?exact={url-encoded-name}`
2. Create record with metadata fields
3. Set Number Owned, Sets, Sources, Condition

<reference file="cards.md" section="Add Individual Cards">
Detailed field mapping from Scryfall to Airtable.
</reference>

---

### 6. Deck Analysis

**CMC distribution:**

```
mcp__airtable__list_records(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tbl3UgZZPJGQhEFo8",
  fields: ["Card Name", "CMC", "Card Type"],
  filterByFormula: "AND(FIND('DeckName', ARRAYJOIN({Decks})), NOT({Is Land}))"
)
```

Then group by CMC in response.

**Creature vs non-creature breakdown:**

```
mcp__airtable__list_records(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tbl3UgZZPJGQhEFo8",
  fields: ["Card Name", "Is Creature", "Is Non-Creature Spell", "Is Land"],
  filterByFormula: "FIND('DeckName', ARRAYJOIN({Decks}))"
)
```

<reference file="query-patterns.md" section="Formula Patterns">
More formula patterns: color identity analysis, price totals, missing fields.
</reference>

---

## Scryfall API

- **Exact name:** `GET /cards/named?exact={name}`
- **All printings:** `GET /cards/search?q=!"{name}"&unique=prints`
- **Rate limit:** 100ms between requests. On HTTP 429, wait 65s then retry.
- **Double-faced cards:** `image_uris` is null at top level; use `card_faces[0].image_uris`

---

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

---

<references>
## Reference Index

| When you need to... | Read |
|---------------------|------|
| Field sets and formula patterns for efficient queries | [query-patterns.md](references/query-patterns.md) |
| Full table/field schema with types | [airtable-schema.md](references/airtable-schema.md) |
| Detailed card operations (add, backfill) | [cards.md](references/cards.md) |
| Deck loading workflow and multi-copy handling | [decks.md](references/decks.md) |
| Trade recording with source/destination model | [trades.md](references/trades.md) |
</references>
