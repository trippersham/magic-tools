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
Adding cards to Chase Cards without verifying they fit a deck's actual strategy creates noise that undermines the system's value. A card that looks generically powerful may not advance any deck's specific game plan. Each deck has a Strategy field that defines what it optimizes for -- all chase recommendations must be validated against it before adding to Chase Cards.
</constraint-rationale>

**ALWAYS** verify strategy alignment before adding cards to Chase Cards:
1. Read the target deck's Strategy via `${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>" --field strategy`
2. Confirm the recommended card advances that specific strategy
3. Only then add to Chase Cards via `add-chase "<card>" --for-deck "<deck>"`

**If no deck strategy matches:** Do NOT add to Chase Cards. Report the card as "interesting but no current home" and ask if the user wants to track it anyway.

</strategy-alignment-constraint>

<red-flags>
If you catch yourself thinking:
- "This card is generically powerful, any deck would want it"
- "I'll add it to Chase Cards and figure out the deck later"
- "The card fits the deck's colors, that's close enough"

**STOP.** Read the deck's Strategy field first. Strategy alignment is not optional.
</red-flags>

## The data surface: the `collection` CLI (both backends)

Every read and write of Decks and the Chase list goes through **one backend-agnostic CLI** —
the same surface whether the source of record is local YAML or Airtable:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection <verb> [args...]
```

The active backend auto-resolves; force it with `MAKE_MAGIC_BACKEND=local` or `=airtable`. (The
wrapper forwards to `uv run --project <pipeline> python -m pipeline.collection.run <verb>`.)

Verbs this skill uses: `status`, `list-decks`, `get-deck <name> [--field strategy]`,
`list-chase`, `add-chase <name> [--for-deck --priority --status --target-price]`,
`remove-chase <name>`.

> **Airtable-mode caveat.** `--priority`, `--status`, and `--target-price` have **no columns on
> the live Airtable base**, so in Airtable mode the CLI writes Card Name + Target Decks and
> silently skips those three. Local mode retains all of them.

**Mode banner — run this first.** Open any workflow by announcing the source of record:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection status
```
It prints e.g. `{"backend": "local", "source_of_record": "local (collection/ YAML)"}` (or
`airtable (records adapter)`). State it to the user, then proceed — the steps below are
identical either way.

## Prerequisites

- **uv** -- the CLI and helper scripts run via `uv run` (PEP 723 inline metadata for scripts)
- **A populated backend** -- local mode reads `collection/` YAML under `MAKE_MAGIC_DATA_DIR`;
  Airtable mode needs the connector enabled via `/mcp`
- **Scryfall cache** -- the spoiler cache persists across sessions in `spoiler_cache.db`

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
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection list-decks
   # then for each name, read its strategy + color identity:
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<deck>"
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

CRUD on the Chase list via the `collection` CLI — identical in local and Airtable mode. The
resolver hydrates Scryfall metadata (type, cmc, oracle text, art, price, color identity) from
the card name automatically, so you pass a name, not a field map.

### Add Cards

1. **Read the current list** to see what's already tracked:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection list-chase
   ```
   Each entry carries `name`, `for_decks`, and (local mode) `priority` / `status` /
   `target_price`. Match by `name` to decide add-vs-relink.

2. **Add a card** (new, or to link an additional deck):
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection add-chase "<Card Name>" --for-deck "<deck>"
   ```
   Optional (local-mode only, skipped in Airtable): `--priority <n>`, `--status <s>`,
   `--target-price <usd>`. To track a card for several decks, run `add-chase` once per
   `--for-deck` (or list the card under each deck as your workflow dictates).

### Remove Cards

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection remove-chase "<Card Name>"
```
`remove-chase` drops the card from the Chase list. (Removing a card from *one* deck's interest
while keeping it for another is a re-add: `remove-chase` then `add-chase --for-deck <the deck
to keep>` — confirm the intent with the user first.)

### Bulk Update from Recommendations

1. **`list-chase`** to snapshot current state
2. **Diff** against new recommendations (by card name)
3. **`add-chase`** the new cards (one call each; the resolver hydrates metadata)
4. **Optionally `remove-chase`** cards that dropped below threshold (confirm with user first)
5. **Report** changes made

> For a large batch of new cards, dispatch background agents that each run `add-chase` — the
> CLI is the write path in every case; there is no `mcp__airtable__*` write step.

---

## 4. Price Monitoring

Check current prices on chase cards and report movements.

1. **Read the Chase list:**
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection list-chase
   ```
   Each entry carries `name`, `for_decks`, and (local mode) `target_price`.

2. **For each card, fetch current price:**
   ```bash
   uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/scryfall_cache.py get-card "<card name>"
   ```

3. **Compare to the target/last-known price** and classify:
   - **Price drop** (>20%): Good buy opportunity
   - **Price spike** (>50%): Consider priority acquisition
   - **Stable**: No action needed

4. **Report significant movements** grouped by category

> **Persisted price on the Chase row.** In Airtable mode a `Price (TCGPlayer)` column exists;
> the CLI does not expose a chase-price setter, so treat live prices as a **read-and-report**
> product of this workflow. If you want to refresh the stored Airtable price ad-hoc, that is a
> human `/mcp` write, out of band from the skill (see the Optional / ad-hoc appendix) — the
> skill itself does not write prices via `mcp__airtable__*`. In local mode, `target_price` is
> the acquisition target you set at `add-chase` time, not a live-price cache.

<dfc-handling>
For double-faced cards: top-level `image_uris`, `oracle_text`, and `mana_cost` are null. Use `card_faces[0]` for these fields. `cmc` and `prices` remain at top level.
</dfc-handling>

---

## Optional / ad-hoc (Airtable-only, read-mostly)

When the active backend is Airtable **and** you (a human) are connected via `/mcp`, you may run
`mcp__airtable__*` **reads** (`search_records`, `list_records`, `get_record`) directly against
the base to poke at raw Chase/Decks rows or refresh a stored price by hand. That is out-of-band
exploration, not a skill step.

**Rule: skills WRITE only through the `collection` CLI** (`add-chase`, `remove-chase`). No
executable step in this skill may create/update/delete via `mcp__airtable__*`. The table/field
ids below are for that read-mostly MCP exploration only.

### Key Field IDs (for ad-hoc MCP reads)

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
| Price (TCGPlayer) | `fld8PpkaGej0qDA8x` | currency |
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
| (Optional / ad-hoc) Airtable field IDs / table relationships for MCP reads | references/airtable-schema.md |
| (Optional / ad-hoc) Efficient Airtable MCP reads / common gotchas | references/airtable-patterns.md |
