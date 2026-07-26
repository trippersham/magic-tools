# Query Patterns

Efficient querying patterns for the Magic Inventory Airtable base. Use these patterns to minimize token cost and API calls.

---

## Common Field Sets

Pre-defined field arrays by use case. Copy these directly into the `fields` parameter.

### Play Mechanics
For understanding how cards play — casting, abilities, stats.

```json
["Card Name", "Mana Cost", "Card Type", "CMC", "Oracle Text", "Power / Toughness"]
```

### Inventory
For tracking ownership and condition.

```json
["Card Name", "Number Owned", "Foil Count", "Condition", "Sets"]
```

### Deck Building
For evaluating cards for deck construction.

```json
["Card Name", "Mana Cost", "CMC", "Card Type", "Is Creature", "Is Non-Creature Spell", "Is Land"]
```

### Card Identity
For quick lookups and existence checks.

```json
["Card Name"]
```

### Card Display
For UI rendering with images.

```json
["Card Name", "Card Art", "Mana Cost", "Card Type"]
```

### Price Analysis
For value tracking and trade evaluation.

```json
["Card Name", "Price (TCGPlayer)", "Price Last Updated", "Sets"]
```

### Deck Overview
For listing decks with summary info.

```json
["Name", "Format", "Owner", "Deck Size", "Commander"]
```

### Deck Stats
For deck composition analysis.

```json
["Name", "Creatures", "Nonbasic Lands", "Non-Creature Spells", "CMC Average (from Cards)"]
```

---

## Formula Patterns

Airtable formula syntax for `filterByFormula` parameter.

### Cards by Deck

```
FIND('DeckName', ARRAYJOIN({Decks}))
```

Returns all cards linked to the specified deck. Replace `DeckName` with the exact deck name.

### Cards by Type Within Deck

```
AND(FIND('DeckName', ARRAYJOIN({Decks})), FIND('Creature', {Card Type}))
```

Creatures only:
```
AND(FIND('DeckName', ARRAYJOIN({Decks})), {Is Creature})
```

Non-creature spells:
```
AND(FIND('DeckName', ARRAYJOIN({Decks})), {Is Non-Creature Spell})
```

Lands:
```
AND(FIND('DeckName', ARRAYJOIN({Decks})), {Is Land})
```

### Cards by CMC

All cards with CMC 3:
```
{CMC}=3
```

Cards in a deck with CMC 3 or less:
```
AND(FIND('DeckName', ARRAYJOIN({Decks})), {CMC}<=3)
```

High-cost cards (CMC 6+):
```
AND(FIND('DeckName', ARRAYJOIN({Decks})), {CMC}>=6, NOT({Is Land}))
```

### Missing Metadata

Cards without prices:
```
{Price (TCGPlayer)}=BLANK()
```

Cards without art:
```
{Card Art}=BLANK()
```

Cards without oracle text:
```
{Oracle Text}=BLANK()
```

### Batch Existence Check

To check if cards exist before creating:
```
OR({Card Name}='Lightning Bolt', {Card Name}='Counterspell', {Card Name}='Sol Ring')
```

Note: For large batches (10+ cards), use `search_records` with a single card name, then aggregate results. OR formulas become slow with many conditions.

### Cards Available for Trading

Cards in library (not in any deck):
```
{Number in Library}>0
```

Cards with multiple copies:
```
{Number Owned}>1
```

---

## Anti-Patterns

### DO NOT: Fetch All Fields

```
// WRONG
mcp__airtable__list_records(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tbl3UgZZPJGQhEFo8"
)
```

**Why it fails:** Returns 25+ fields per record. A 100-card query returns ~50KB of JSON, consuming significant context.

**Correct:**
```
mcp__airtable__list_records(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tbl3UgZZPJGQhEFo8",
  fields: ["Card Name", "Mana Cost", "CMC"]
)
```

### DO NOT: N+1 Record Fetches

```
// WRONG: calling get_record in a loop
deck = get_record(deck_id)
for card_id in deck.fields.Cards:
    card = get_record(card_id)  // 100 separate calls!
```

**Why it fails:** Each `get_record` is a separate API call. A 100-card deck requires 100 calls instead of 1.

**Correct:**
```
// Single formula-filtered query
mcp__airtable__list_records(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tbl3UgZZPJGQhEFo8",
  fields: ["Card Name", "Mana Cost", "CMC"],
  filterByFormula: "FIND('DeckName', ARRAYJOIN({Decks}))"
)
```

### DO NOT: Filter in Code

```
// WRONG: fetching all then filtering
all_cards = list_records(Cards)
creatures = [c for c in all_cards if 'Creature' in c.fields.Card_Type]
```

**Why it fails:** Downloads all records, consuming tokens and time, then discards most of them.

**Correct:**
```
mcp__airtable__list_records(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tbl3UgZZPJGQhEFo8",
  fields: ["Card Name", "Card Type"],
  filterByFormula: "FIND('Creature', {Card Type})"
)
```

---

## Schema Tool Guidance

When calling `list_tables` or `describe_table`, use minimal detail levels:

```
mcp__airtable__list_tables(
  baseId: "appw7QPMoqktrgDc1",
  detailLevel: "identifiersOnly"
)
```

```
mcp__airtable__describe_table(
  baseId: "appw7QPMoqktrgDc1",
  tableId: "tbl3UgZZPJGQhEFo8",
  detailLevel: "identifiersOnly"
)
```

**Why:** The SKILL.md already contains the table IDs and key field names. Schema tools are only needed for edge cases or to discover new fields. `identifiersOnly` returns field names without full type definitions, saving tokens.
