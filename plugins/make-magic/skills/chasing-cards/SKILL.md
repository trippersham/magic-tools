---
name: chasing-cards
description: >
  Track MTG card spoilers and monitor prices on cards of interest. TRIGGER when: user asks to
  "check spoilers", "sync spoilers", "what's been spoiled for [set]", "update chase list",
  "price check chase cards", "what should I chase from [set]", "add to chase list",
  "remove from chase list", "show chase cards for [deck]", or any question about pre-release
  cards, spoiler tracking, or price monitoring on wanted cards.
user-invocable: true
---

# Chase Cards

<strategy-alignment-constraint>

## Strategy Alignment Constraint

<constraint-rationale>
Adding cards to Chase Cards without verifying they fit a deck's actual strategy creates noise that undermines the system's value. A card that looks generically powerful may not advance any deck's specific game plan. The Decks table has a Strategy field (`fldvJRaoYfRZiM8zw`) that defines what each deck optimizes for -- all chase recommendations must be validated against this field before adding to Chase Cards.
</constraint-rationale>

**ALWAYS** verify strategy alignment before adding cards to Chase Cards:
1. Read the target deck's Strategy field via `mcp__airtable__get_record`
2. Confirm the recommended card advances that specific strategy
3. Only then add to Chase Cards with appropriate Target Decks links

**If no deck strategy matches:** Do NOT add to Chase Cards. Report the card as "interesting but no current home" and ask if the user wants to track it anyway.

</strategy-alignment-constraint>

<red-flags>
If you catch yourself thinking:
- "This card is generically powerful, any deck would want it"
- "I'll add it to Chase Cards and figure out the deck later"
- "The card fits the deck's colors, that's close enough"

**STOP.** Read the deck's Strategy field first. Strategy alignment is not optional.
</red-flags>

## Prerequisites

- **Airtable MCP** -- authenticated via `/mcp` in Claude Code
- **Scripts** -- run with `uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/<script>.py`
- **Scryfall cache** -- the cache persists across sessions in `spoiler_cache.db`

## Operation Router

Determine the operation, then follow the matching section below:

1. **Discover new spoilers** for a set -- **Sync Spoilers**
2. **Generate chase recommendations** across decks for a set -- **Generate Chase Recommendations**
3. **Add/remove/update cards** in Chase Cards table -- **Manage Chase Cards Table**
4. **Check current prices** on chase cards -- **Price Monitoring**

---

## 1. Sync Spoilers

Discover new cards for sets in spoiler season.

```bash
# Sync one or more sets
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py sync <set_code> [<set_code>...]

# Check current sync state
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py status

# List all cards (or filter to new/unconfirmed)
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py list --new
```

<output-capture-pattern>

**stdout:** JSON with sync results -- new cards found, confirmation counts
**stderr:** Progress messages, rate limit warnings

```bash
result=$(uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py sync msh 2>/dev/null)
echo "$result" | jq '.new_cards'
```

</output-capture-pattern>

**After syncing:** Report new cards found since last sync. Ask if user wants to generate chase recommendations (workflow 2).

<reference file="spoiler-sources.md" section="Sync Engine">
For source hierarchy, phase details, state tracking, and known limitations, see references/spoiler-sources.md.
</reference>

---

## 2. Generate Chase Recommendations

For a set (spoiled or released), recommend chase targets across all decks.

1. **Load all decks with strategies:**
   ```
   mcp__airtable__list_records on tblIfqVuVHNQza1K3 (Decks)
   fields: Name, Strategy, Color Identity, Commander
   ```

2. **Run card tagger for the set:**
   ```bash
   uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/card_tagger.py tag-set <set_code> --threshold 0.7
   ```

3. **Filter results** to cards above confidence threshold

4. **Present recommendations** grouped by deck:
   ```
   ## Deck: [Name]
   Strategy: [strategy text]
   
   - Card Name (confidence: X.XX) -- [rationale for strategy fit]
   ```

5. **On user approval:** Push approved cards to Chase Cards table (workflow 3)

<strategy-validation>
For each recommendation, verify the card genuinely advances the deck's Strategy -- not just color identity compatibility. If the strategy fit is unclear, flag it for user review rather than auto-approving.
</strategy-validation>

---

## 3. Manage Chase Cards Table

CRUD operations on `tblXsNtGgT7UQLPXZ`.

### Add Cards

1. **Check if card exists:**
   ```
   mcp__airtable__search_records
   baseId: appw7QPMoqktrgDc1
   tableId: tblXsNtGgT7UQLPXZ
   searchTerm: <card name>
   fieldId: fldf14LO7VRoTZ8PK
   ```

2. **If new:** Create record with Scryfall metadata
   ```
   mcp__airtable__create_record
   baseId: appw7QPMoqktrgDc1
   tableId: tblXsNtGgT7UQLPXZ
   fields:
     fldf14LO7VRoTZ8PK: <Card Name>
     fldlhTQDqpLKxutjK: [<Set Name>]
     fldG2rGqiG8UydPy6: <type_line>
     fld5UtegtLhSWGRNV: <mana_cost>
     fldMnldyhgYlEPcFA: <cmc>
     fldHSHDka12X4BiHL: <oracle_text>
     fldFvNmj0fqOnOVP3: <image_uris.art_crop>
     fldUjrCkvkBKcWqgh: <scryfall_uri>
     fld8PpkaGej0qDA8x: <prices.usd>
     fldEkalJKqK2ZecEv: [<color_identity>]
     flduoZZRmVfpD6aSG: [<deck_record_ids>]
     fldnl2R4B7B9X2MlJ: <power/toughness>
   ```

3. **If exists:** Update to add new Target Decks links
   ```
   mcp__airtable__update_records
   baseId: appw7QPMoqktrgDc1
   tableId: tblXsNtGgT7UQLPXZ
   records: [{"id": "<record_id>", "fields": {"flduoZZRmVfpD6aSG": [<existing_ids>, <new_deck_id>]}}]
   ```

### Remove Cards

1. **Find the record** via `search_records`

2. **If removing from one deck:** Update Target Decks to remove that link
   ```
   mcp__airtable__update_records
   records: [{"id": "<record_id>", "fields": {"flduoZZRmVfpD6aSG": [<remaining_deck_ids>]}}]
   ```

3. **If removing entirely:** Delete the record
   ```
   mcp__airtable__delete_records
   baseId: appw7QPMoqktrgDc1
   tableId: tblXsNtGgT7UQLPXZ
   recordIds: [<record_id>]
   ```

### Bulk Update from Recommendations

1. **List current Chase Cards** for the target decks
2. **Diff** against new recommendations
3. **Add** new cards, **update** Target Decks on existing cards
4. **Optionally remove** cards that dropped below threshold (confirm with user first)
5. **Report** changes made

<batch-constraints>
- `create_record` is singular -- for >5 new cards, use background agents
- `update_records` batches up to 10 -- always batch multiple updates
- `multipleSelects` fields auto-create choices (no pre-creation needed)
</batch-constraints>

<reference file="airtable-patterns.md" section="Write Optimization">
For batch update patterns and common gotchas, see references/airtable-patterns.md.
</reference>

---

## 4. Price Monitoring

Check current prices on chase cards and report movements.

1. **Page through Chase Cards:**
   ```
   mcp__airtable__list_records
   baseId: appw7QPMoqktrgDc1
   tableId: tblXsNtGgT7UQLPXZ
   fields: [fldf14LO7VRoTZ8PK, fld8PpkaGej0qDA8x, flduoZZRmVfpD6aSG]
   ```

2. **For each card, fetch current price:**
   ```bash
   uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py get-card "<card name>"
   ```

3. **Compare to stored price** and classify:
   - **Price drop** (>20%): Good buy opportunity
   - **Price spike** (>50%): Consider priority acquisition
   - **Stable**: No action needed

4. **Report significant movements** grouped by category

5. **Update stale prices** in Airtable:
   ```
   mcp__airtable__update_records
   records: [{"id": "<id>", "fields": {"fld8PpkaGej0qDA8x": <new_price>}}, ...]
   ```
   (batch up to 10 per call)

<dfc-handling>
For double-faced cards: top-level `image_uris`, `oracle_text`, and `mana_cost` are null. Use `card_faces[0]` for these fields. `cmc` and `prices` remain at top level.
</dfc-handling>

---

## Key Field IDs

**Chase Cards table (`tblXsNtGgT7UQLPXZ`):**

| Field | ID | Type |
|-------|----|------|
| Card Name | `fldf14LO7VRoTZ8PK` | primary |
| Sets | `fldlhTQDqpLKxutjK` | multipleSelects |
| Card Type | `fldG2rGqiG8UydPy6` | text |
| Mana Cost | `fld5UtegtLhSWGRNV` | text |
| CMC | `fldMnldyhgYlEPcFA` | number |
| Oracle Text | `fldHSHDka12X4BiHL` | multilineText |
| Card Art | `fldFvNmj0fqOnOVP3` | url |
| Scryfall URL | `fldUjrCkvkBKcWqgh` | url |
| Price | `fld8PpkaGej0qDA8x` | currency |
| Color Identity | `fldEkalJKqK2ZecEv` | multipleSelects |
| Target Decks | `flduoZZRmVfpD6aSG` | link -> Decks |
| P/T | `fldnl2R4B7B9X2MlJ` | text |

**Decks table (`tblIfqVuVHNQza1K3`):**

| Field | ID | Notes |
|-------|----|-------|
| Strategy | `fldvJRaoYfRZiM8zw` | Source of truth for deck game plan |
| Color Identity | `fldIXcQuMKd7PLyr9` | Deck colors |
| Chase Cards | `fldfoTmUWn5WpuT6u` | Inverse link (auto-populated, read-only) |

<reference file="airtable-schema.md" section="Chase Cards table fields">
For complete field definitions and relationship details, see references/airtable-schema.md.
</reference>

---

## Reference Guides

| When you need to... | Read |
|---------------------|------|
| Understand spoiler source hierarchy and sync engine | references/spoiler-sources.md |
| Look up Airtable field IDs or table relationships | references/airtable-schema.md |
| Apply batch update patterns or avoid common gotchas | references/airtable-patterns.md |
