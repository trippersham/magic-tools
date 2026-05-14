# Managing Decks

## Load a Deck from a Cardlist

Cardlists live in `cardlists/<deck-name>.txt` (format: `<qty> <card name>` per line). Parse inline — no script needed.

### Steps

1. Parse the cardlist — one line per entry, `<qty> <card name>`
2. Confirm the commander with the user
3. Check which cards already exist in Cards table (`mcp__airtable__search_records`)
4. Fetch Scryfall metadata for new cards:
   - Write `[{"name": "...", "id": ""}]` to `/tmp/<deck>-to-fetch.json`
   - Run: `uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_batch.py /tmp/<deck>-to-fetch.json /tmp/<deck>-scryfall.json`
5. Create card records via `mcp__airtable__create_record` with all Scryfall fields (see Field Mapping in SKILL.md). Set Number Owned = 1, Sets, Sources as appropriate
6. For existing cards: increment Number Owned
7. Create/update the Deck record:
   - Name, Format, Commander (link), Cards (link all non-commander non-basic-land cards)
   - Basic land counts: Plains, Islands, Swamps, Mountains, Forests, Wastes fields
8. Verify Deck Size formula matches expected total

### Gotchas

- Commander links via Decks.Commander field, NOT the Cards link field
- Use background agents for decks with many new cards to avoid blocking
