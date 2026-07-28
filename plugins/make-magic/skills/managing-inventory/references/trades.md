# Managing Trades (via the `collection` CLI)

Trades are recorded through the backend-agnostic CLI — identical in local YAML and Airtable
mode. No `mcp__airtable__*` write step.

## Source/Destination Model

Trades track card movement between locations using a category + specificity pattern:

- **From (Source)** / **To (Destination)** — category: Library, Deck, Store, Person
- **From (Deck)** / **To (Deck)** — the specific Deck (only when the category is "Deck")
- **Cards into Destination** — cards entering the destination
- **Cards out of Destination** — cards leaving the destination

## Record a Trade

First add any brand-new cards to inventory (`add-card "<name>" --qty <n>`), then log the trade.

**Flag form** (no deck specificity — `from_source` / `to_destination` are categories only):
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection log-trade \
  --from-source Library --to-destination Deck \
  --card-in "Lavaleaper" --card-out "Horizon Stone" \
  --status Completed --notes "swap into Ozai"
```
`--card-in` / `--card-out` are repeatable.

**JSON form** — use this when you need `to_deck` / `from_deck` specificity (the flag form has no
`--to-deck`):
```bash
echo '{
  "from_source": "Library",
  "to_destination": "Deck",
  "to_deck": "Ozai",
  "cards_in": ["Lavaleaper"],
  "cards_out": ["Horizon Stone"],
  "status": "Completed"
}' | ${CLAUDE_PLUGIN_ROOT}/scripts/collection log-trade --from-json -
```

**Example** — swap Horizon Stone for Lavaleaper in the Ozai deck from Library:
- `from_source` = "Library"
- `to_destination` = "Deck", `to_deck` = "Ozai"
- `cards_in` = ["Lavaleaper"], `cards_out` = ["Horizon Stone"]

## Reading trades

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection list-trades
```

## Trade Lifecycle

Trades follow a **Draft -> Planned -> Completed** status progression (pass via `--status` or the
JSON `status` field):

| Status | Meaning |
|--------|---------|
| Draft | Exploring options, cards not yet committed |
| Planned | Cards decided, awaiting execution |
| Completed | Trade executed, inventory updated |

**On completion**, reflect the move in the affected deck (`save-deck` with the updated
`cards[]`) and in inventory (`set-quantity` for counts that changed).
