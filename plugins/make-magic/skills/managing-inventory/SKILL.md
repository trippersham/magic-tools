---
name: managing-inventory
description: >
  Manage MTG card inventory, decks, and trades in Airtable (enriched via Scryfall).
  TRIGGER when the user asks to add cards, load a deck, record a trade, update prices,
  backfill metadata (CMC, art, oracle text), query deck contents, or get deck stats — any
  create/read/update on the Magic Inventory base, or references to cardlist files or Scryfall
  data. SKIP for deck strategy, card evaluation, or Chase Cards prioritization — those are
  strategy tasks, not inventory operations.
---

# MTG Inventory Manager

Manage a Magic: The Gathering card inventory in Airtable, enriched via Scryfall API.

<primary-constraint>
## Airtable Efficiency: Batch + Targeted Fields Over N+1 Queries

**Always** (1) pass the `fields` parameter so a query returns only the columns you need — never all 25+; (2) use `filterByFormula` to fetch related records in one paginated call instead of looping `get_record` over linked IDs; (3) batch writes with `update_records` (up to 10 per call).

Why: N+1 loops and full-field fetches are the top efficiency failures here — a 100-card deck fetch is ~50KB of JSON unfiltered vs ~5KB with targeted `fields`, and 50 `get_record` calls collapse to a single `filterByFormula` query.

<reference file="query-patterns.md" section="Common Field Sets">
Pre-defined field sets by use case, formula patterns (cards by deck / type / CMC / missing-metadata), and the full anti-pattern catalog.
</reference>

<reference file="airtable-patterns.md" section="Query Optimization">
detailLevel, targeted lookups, batch writes, and common gotchas (DFC, Commander linking, formula fields, price updates).
</reference>
</primary-constraint>

<red-flags>
## Red Flags: Stop and Reconsider

If you catch yourself about to:
- **Loop over card names calling `get_record` for each** -- Stop. Use `search_records` with `filterByFormula` containing an OR of FIND clauses.
- **Call `update_records` once per record** -- Stop. Batch up to 10 records per call.
- **Fetch full records just to check existence** -- Stop. Use `search_records` with `filterByFormula`; it returns matching records directly.
- **Load all records then filter client-side** -- Stop. Push the filter to Airtable via `filterByFormula`.
- **Fetch all fields because you're not sure which you need** -- Stop. Pick a field set from `references/query-patterns.md` and pass it via `fields`.

Read `references/query-patterns.md` for the correct patterns.
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

<reference file="airtable-schema.md" section="Tables">
Table IDs, field IDs, and full schema reference.
</reference>

---

## Prerequisites

- **Airtable connector** -- enable via `/mcp` in Claude Code, authenticate with your Airtable account
- **Airtable base** -- clone the shared base template (see plugin README)
- **Scryfall API** -- free, no auth required
- **uv** -- scripts use PEP 723 inline metadata; run with `uv run --script`

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

## Workflows

### 1. Load a Deck from a Cardlist

Cardlists live in `cardlists/<deck-name>.txt` (format: `<qty> <card name>` per line).

<reference file="decks.md" section="Load a Deck from a Cardlist">
Full deck loading workflow with steps and gotchas.
</reference>

**Summary:**
1. Parse the cardlist inline -- one line per entry, `<qty> <card name>`
2. Confirm the commander with the user
3. Check existing cards via `search_records` with batched `filterByFormula` (NOT individual lookups) and `fields: ["Card Name", "Number Owned"]`
4. Fetch Scryfall metadata for new cards via `${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_batch.py`
5. Create card records, increment Number Owned for existing cards
6. Create/update Deck record with commander link, card links, and basic land counts
7. Handle multi-copy cards via Repeat fields (see airtable-schema.md)
8. Verify Deck Size formula matches expected total

**Gotchas:**
- `create_record` is singular -- no batch create via MCP. Use background agents for >5 new cards.
- Commander links via `Decks.Commander` field, NOT the Cards link field. The model defaults to linking via Cards because it seems simpler; Commander is a separate relationship.
- Double-faced cards: `image_uris` is null at top level; use `card_faces[0].image_uris` (also `mana_cost`, `oracle_text`). The model often forgets this and gets null values.
- `multiSelect` fields auto-create choices on update; no pre-creation needed.

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

For bulk updates (prices, art, CMC, etc.) across all cards:

<reference file="cards.md" section="Backfill / Update Metadata">
Full backfill workflow with script commands.
</reference>

**Summary:**
1. Page through all Cards records (`list_records` with `fields: ["Card Name"]` + offset)
2. Write names + record IDs to `/tmp/backfill-input.json`
3. Run `uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_batch.py /tmp/backfill-input.json /tmp/backfill-output.json`
4. Build update batches (max 10 per `update_records`)
5. Push batches. Use background agents for large backfills.

**Price updates:** If `prices.usd` is null, try alternate printings via `/cards/search?q=!"CARD NAME"&unique=prints` -- pick cheapest non-null USD printing.

---

### 5. Add Individual Cards

1. Fetch: `GET https://api.scryfall.com/cards/named?exact=<url-encoded-name>`
2. Create record with all metadata fields (see Field Mapping below)
3. Set Number Owned, Sets, Sources, Condition

<reference file="cards.md" section="Add Individual Cards">
Single card creation workflow with detailed field mapping from Scryfall to Airtable.
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
- **Double-faced cards:** `image_uris` is null at top level; use `card_faces[0].image_uris`. `cmc` and `prices` are always top-level.

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
| Optimize queries / batch operations (detailLevel, pagination, edge cases) | [airtable-patterns.md](references/airtable-patterns.md) |
| Check table/field IDs, schema details, or multi-copy handling | [airtable-schema.md](references/airtable-schema.md) |
| Execute deck loading workflow in detail | [decks.md](references/decks.md) |
| Execute card operations (add, backfill) in detail | [cards.md](references/cards.md) |
| Execute trade workflows in detail | [trades.md](references/trades.md) |
</references>
