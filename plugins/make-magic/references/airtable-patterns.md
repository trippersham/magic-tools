# Airtable MCP Efficiency Patterns — Optional / ad-hoc (Airtable-only, read-mostly)

> **Scope / status.** These are efficiency patterns for **human ad-hoc exploration** of
> the Airtable base via the `/mcp` connector — how to keep an exploratory `list_records` /
> `search_records` read cheap when you are poking at raw rows. They are **not** a skill
> data path.
>
> **Skills READ and WRITE through the backend-agnostic `collection` CLI**, which already
> resolves and shapes the data for you (local YAML *or* Airtable). No skill's executable
> step may create/update/delete via `mcp__airtable__*`. Reach for the patterns below only
> when you (a human) are connected via `/mcp` and want to hand-run a read against the live
> base — never to persist a change on the skill's behalf.

## Query Optimization (read-only exploration)

### Use detail levels to reduce token cost

- `list_tables` with `detailLevel: tableIdentifiersOnly` when you only need table names/IDs
- `describe_table` with `detailLevel: identifiersOnly` for field name/ID lookups — only use `full` when you need types/configs

### Use targeted lookups instead of client-side filtering

- `search_records` with `filterByFormula` for targeted lookups: `FIND("Storm-Kiln Artist", {Card Name})` instead of listing all records and filtering client-side
- When reading a single deck's strategy ad-hoc: `get_record` with the deck record ID, not `list_records` + filter (or just run the CLI `get-deck <name> --field strategy`, which does the right thing in either backend)
- When checking if a card exists in Chase Cards: `search_records` with `FIND("card name", {Card Name})`, not a full list

### Minimize fields returned

- Request only the fields you need via the `fields` parameter on `list_records` and `search_records`
- For deck strategy evaluation: request only `Name`, `Strategy`, `Color Identity`, `Commander` fields

## Write Optimization

> Writes are **not** an ad-hoc MCP concern for these skills — every create/update/delete
> goes through the `collection` CLI (`add-card`, `set-quantity`, `remove-card`,
> `add-chase`, `remove-chase`, `save-deck`, `set-strategy`, `set-assessment`,
> `set-focus-otags`, `log-trade`). The Airtable adapter behind the CLI handles batching,
> singleton-create limits, and choice auto-creation internally. The notes below document
> the *underlying* Airtable behavior for humans debugging the base directly.

- `update_records` supports batches of up to 10 records per call
- `create_record` is singular (no batch create via MCP)
- `multipleSelects` fields auto-create choices on update (no pre-creation needed for Sets, Color Identity, etc.)

## Common Gotchas (base internals, for human debugging)

### Double-faced cards (DFC)

Scryfall `image_uris` is null at the top level for DFCs. Use `card_faces[0].image_uris.art_crop` instead. Same for `oracle_text` and `mana_cost` — check `card_faces[0]` when the top-level field is missing. (The CLI's resolver handles this for you; this matters only when you read raw Scryfall JSON.)

### Commander linking

Commanders link via `Decks.Commander` field, NOT the `Cards` link field. The `Cards` field is for the 99 non-commander, non-basic-land cards. (The CLI's `save-deck` respects this split.)

### Chase Cards ↔ Decks relationship

Chase Cards link to Decks via the `Target Decks` field on Chase Cards. The inverse `Chase Cards` field on the Decks table is auto-populated — do not write to it directly. (The CLI's `add-chase --for-deck` writes the correct side.)

### Formula fields are read-only

`Is Land`, `Is Creature`, `Is Non-Creature Spell`, `Deck Size`, `Number in Decks`, `Number in Library` — all computed by Airtable. Do not attempt to set them.

### Price Last Updated

`lastModifiedTime` field configured to watch only the Price field. It updates automatically when Price changes — do not set manually.
