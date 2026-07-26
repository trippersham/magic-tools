# Airtable MCP Efficiency Patterns

## Query Optimization

### Use detail levels to reduce token cost

- `list_tables` with `detailLevel: tableIdentifiersOnly` when you only need table names/IDs
- `describe_table` with `detailLevel: identifiersOnly` for field name/ID lookups — only use `full` when you need types/configs

### Use targeted lookups instead of client-side filtering

- `search_records` with `filterByFormula` for targeted lookups: `FIND("Storm-Kiln Artist", {Card Name})` instead of listing all records and filtering client-side
- When reading a single deck's strategy: `get_record` with the deck record ID, not `list_records` + filter
- When checking if a card exists in Chase Cards: `search_records` with `FIND("card name", {Card Name})`, not a full list

### Minimize fields returned

- Request only the fields you need via the `fields` parameter on `list_records` and `search_records`
- For deck strategy evaluation: request only `Name`, `Strategy`, `Color Identity`, `Commander` fields

## Write Optimization

### Batch updates

- `update_records` supports batches of up to 10 records per call — always batch when updating multiple records
- `create_record` is singular (no batch create via MCP) — use background agents for bulk creation of >5 records
- `multipleSelects` fields auto-create choices on update (no pre-creation needed for Sets, Color Identity, etc.)

## Common Gotchas

### Double-faced cards (DFC)

Scryfall `image_uris` is null at the top level for DFCs. Use `card_faces[0].image_uris.art_crop` instead. Same for `oracle_text` and `mana_cost` — check `card_faces[0]` when the top-level field is missing.

### Commander linking

Commanders link via `Decks.Commander` field, NOT the `Cards` link field. The `Cards` field is for the 99 non-commander, non-basic-land cards.

### Chase Cards ↔ Decks relationship

Chase Cards link to Decks via the `Target Decks` field on Chase Cards. The inverse `Chase Cards` field on the Decks table is auto-populated — do not write to it directly.

### Formula fields are read-only

`Is Land`, `Is Creature`, `Is Non-Creature Spell`, `Deck Size`, `Number in Decks`, `Number in Library` — all computed by Airtable. Do not attempt to set them.

### Price Last Updated

`lastModifiedTime` field configured to watch only the Price field. It updates automatically when Price changes — do not set manually.
