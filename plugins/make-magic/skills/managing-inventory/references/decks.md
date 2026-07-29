# Managing Decks (via the `collection` CLI)

All deck operations go through the backend-agnostic CLI — identical in local YAML and Airtable
mode. Address decks **by name**; there are no record IDs, `filterByFormula`, or linked-record
crawls at this layer, and no `mcp__airtable__*` write step.

## Load a Deck from a Cardlist

Cardlists live in `cardlists/<deck-name>.txt` (format: `<qty> <card name>` per line). Parse
inline — no script needed.

### Steps

1. Parse the cardlist — one line per entry, `<qty> <card name>`
2. Confirm the commander with the user
3. Read current inventory to see what's already owned:
   `${CLAUDE_PLUGIN_ROOT}/scripts/collection list-inventory`
4. Build a `Deck` JSON and persist it in one call:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection save-deck --from-json - <<'JSON'
   {
     "name": "<Deck Name>",
     "cards": [
       {"name": "Llanowar Elves", "quantity": 1},
       {"name": "Lightning Bolt", "quantity": 4}
     ]
   }
   JSON
   ```
   The resolver hydrates each card's Scryfall metadata from its name. Per-card multiplicity
   travels as `DeckCard.quantity` — the adapter maps it onto the Airtable Repeat fields, or
   stores it directly in YAML.
5. For loose copies entering the collection, `add-card "<name>" --qty <n>` (or `set-quantity`
   to correct a count)

> The `Deck` model keeps the commander distinct from the 99 non-commander cards; `save-deck`
> links each to the correct field. Basic-land counts and repeat-field bookkeeping are handled by
> the adapter from the JSON you pass — you never write those Airtable fields by hand.

## Query Deck Contents

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<Deck Name>"   # full JSON incl. cards[]
${CLAUDE_PLUGIN_ROOT}/scripts/collection list-decks               # enumerate names
```
`get-deck` returns `name`, `strategy`, `commander`, `color_identity`, and the full `cards[]`
(each with `name`, `mana_cost`, `type_line`, `mana_value`, `oracle_text`) in one call.

## Deck Analysis

Compute analysis over the `cards[]` returned by `get-deck`:

- **CMC distribution** — bucket nonland cards by `mana_value`.
- **Creature vs non-creature** — split on `type_line` (contains "Creature"; lands via "Land").
- **Deterministic fact sheet** (curve / ramp / interaction / otag buckets) —
  `${CLAUDE_PLUGIN_ROOT}/scripts/collection factsheet "<Deck Name>"`.

### Gotchas

- Commander is a distinct field, not one of the 99 `cards[]` — set it on the `Deck` JSON, not as a card entry.
- Double-faced cards resolve via `card_faces[0]` automatically; metadata does not come back empty.
