# Managing Cards

## Add Individual Cards

1. Fetch from Scryfall: `GET /cards/named?exact={url-encoded-name}`
2. Create record via `mcp__airtable__create_record` on the Cards table with all metadata fields (see Field Mapping in SKILL.md)
3. Set Number Owned, Sets, Sources, Condition as appropriate

## Backfill / Update Metadata

For bulk updates (prices, art, CMC, etc.) across all cards:

1. Page through all Cards records (`mcp__airtable__list_records` with offset)
2. Write names + record IDs to `/tmp/backfill-input.json` as `[{"name": "...", "id": "recXXX"}, ...]`
3. Run: `uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_batch.py /tmp/backfill-input.json /tmp/backfill-output.json`
4. Build update batches (max 10 per `mcp__airtable__update_records`)
5. Push batches. Use background agents for large backfills.

## Price Updates

If `prices.usd` is null for a card, try alternate printings:

```
GET /cards/search?q=!"CARD NAME"&unique=prints
```

Pick the cheapest non-null USD printing from the results.
