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

Manage a Magic: The Gathering card inventory, decks, and trades, enriched via Scryfall API.

<primary-constraint>
## The data surface: the `collection` CLI (both backends)

**Every** read and write of Inventory, Decks, and Trades goes through **one backend-agnostic
CLI** — the same surface whether the source of record is local YAML or Airtable. You do not
juggle record IDs, `fields` lists, `filterByFormula`, or batch limits at the skill layer; the
CLI and the adapter behind it handle all of that.

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection <verb> [args...]
```

The active backend auto-resolves; force it with `MAKE_MAGIC_BACKEND=local` or `=airtable`. (The
wrapper forwards to `uv run --project <pipeline> python -m pipeline.collection.run <verb>`.)

Verbs this skill uses: `status`, `list-inventory`, `add-card <name> [--qty --condition --foil
--set --source]`, `set-quantity <name> <n>`, `remove-card <name>`, `list-decks`, `get-deck
<name>`, `save-deck --from-json <path|->`, `list-trades`, `log-trade [...]`.

**Mode banner — run this first.** Open any workflow by announcing the source of record:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection status
```
It prints e.g. `{"backend": "local", "source_of_record": "local (collection/ YAML)"}` (or
`airtable (records adapter)`). State it to the user, then proceed — the steps below are
identical either way.
</primary-constraint>

<red-flags>
## Red Flags: Stop and Reconsider

If you catch yourself about to:
- **Write inventory/deck/trade changes with `mcp__airtable__create_record` / `update_records` / `delete_records`** -- Stop. The CLI verbs (`add-card`, `set-quantity`, `remove-card`, `save-deck`, `log-trade`) are the ONLY write path, and they work in both backends.
- **Loop `get-deck` per card to read a decklist** -- Stop. `get-deck "<deck>"` returns the whole deck including its `cards[]` in one call.
- **Reach for a record ID or a `filterByFormula`** -- Stop. Address decks and cards by name through the CLI. (Server-side `filterByFormula` prefilters are an Airtable-only *read* optimization for ad-hoc human exploration — see the Optional / ad-hoc appendix — never a skill write.)
</red-flags>

---

## Prerequisites

- **uv** -- the CLI and helper scripts run via `uv run` (PEP 723 inline metadata for scripts)
- **A populated backend** -- local mode reads `collection/` YAML under `MAKE_MAGIC_DATA_DIR`;
  Airtable mode needs the base cloned and the connector enabled via `/mcp` (see plugin README)
- **Scryfall API** -- free, no auth required (the CLI's resolver uses it to hydrate metadata)

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
3. Read the current inventory to see what's already owned: `${CLAUDE_PLUGIN_ROOT}/scripts/collection list-inventory`
4. Build a `Deck` JSON (commander, `cards[]` with per-card `quantity`, basic-land counts) — the CLI's resolver hydrates each card's Scryfall metadata from its name
5. Persist the deck in one call: `${CLAUDE_PLUGIN_ROOT}/scripts/collection save-deck --from-json <path|->`
6. For loose copies entering the collection, `add-card "<name>" --qty <n>` (or `set-quantity` to correct a count)

`save-deck` handles the commander/Cards split, card links, and (Airtable mode) the multi-copy
Repeat-field bookkeeping from each `DeckCard.quantity` — you never write those fields by hand.

**Gotchas (now handled by the CLI/adapter, kept for awareness):**
- Per-card multiplicity travels as `DeckCard.quantity` in the `save-deck` JSON; the adapter maps it onto the Airtable Repeat fields, or stores it directly in YAML.
- Commander vs Cards: the `Deck` model keeps the commander distinct from the 99 — `save-deck` links each to the correct field.
- Double-faced cards: the resolver reads `card_faces[0]` when top-level Scryfall fields are null, so metadata does not come back empty.

<reference file="decks.md" section="Load a Deck from a Cardlist">
Full deck-loading workflow with the `save-deck` JSON shape and gotchas.
</reference>

---

### 2. Query Deck Contents

**To list all cards in a deck and read its overview** — one call:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<DeckName>"
```
Returns the deck JSON — `name`, `strategy`, `commander`, `color_identity`, and the full
`cards[]` (each with `name`, `mana_cost`, `type_line`, `mana_value`, `oracle_text`). Group /
filter / total in your reasoning. No `filterByFormula`, no linked-record crawl.

To just enumerate deck names: `${CLAUDE_PLUGIN_ROOT}/scripts/collection list-decks`.

<reference file="decks.md" section="Query Deck Contents">
For grouping the returned cards[] by type / CMC and other read patterns.
</reference>

---

### 3. Record a Trade

Trades track card movement between locations using a Source/Destination model.

<reference file="trades.md" section="Source/Destination Model">
Full trade model with lifecycle and examples.
</reference>

**Summary:**
1. Add any brand-new cards to inventory first: `add-card "<name>" --qty <n>` (resolver hydrates metadata)
2. Record the trade in one call — flag form:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection log-trade \
     --from-source Library --to-destination Deck \
     --card-in "Lavaleaper" --card-out "Horizon Stone" --status Completed
   ```
   For deck specificity, add `--from-deck` / `--to-deck` flags:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/collection log-trade \
     --from-source Library --to-destination Deck --to-deck "Ozai" \
     --card-in "Lavaleaper" --card-out "Horizon Stone" --status Completed
   ```
   (A `--from-json -` form accepting a full Trade JSON on stdin also works for scripted/bulk entry.)
3. Reflect the move in the deck (`save-deck` with the updated `cards[]`) and inventory (`set-quantity`) as needed

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

In the local backend, card metadata (CMC, art, oracle text, price) is hydrated on the fly by
the CLI's resolver from the Scryfall cache — there is no persisted-metadata column to backfill,
so the batch-backfill dance is an **Airtable-mode-only** concern. When you *are* on Airtable and
want to refresh the base's stored metadata columns, that bulk refresh is a maintenance operation
that reads via `/mcp` and re-hydrates from Scryfall — see the Optional / ad-hoc appendix and
`references/cards.md`. The skill's canonical card writes remain `add-card` / `set-quantity` /
`remove-card` (single-card, both backends).

**Price note:** If `prices.usd` is null for a printing, the resolver / `scryfall_batch.py` tries
alternate printings via `/cards/search?q=!"CARD NAME"&unique=prints` -- cheapest non-null USD.

---

### 5. Add Individual Cards

One call — the resolver fetches Scryfall metadata from the name; you pass ownership details:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection add-card "Llanowar Elves" \
  --qty 2 --condition NM --set DOM --source "Draft"
```
`--condition`, `--set`, `--source` are repeatable. To correct an existing count use
`set-quantity "<name>" <n>`; to drop a card use `remove-card "<name>"`.

<reference file="cards.md" section="Add Individual Cards">
Single-card workflow and the add-card / set-quantity / remove-card option details.
</reference>

---

### 6. Deck Analysis

Read the deck once, then compute the analysis over the returned `cards[]`:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection get-deck "<DeckName>"
```
- **CMC distribution** — bucket the nonland `cards[]` by `mana_value`.
- **Creature vs non-creature** — split `cards[]` on `type_line` (contains "Creature"; lands via "Land").

For the deterministic curve/ramp/interaction/otag fact sheet (used by the building-decks
Quadrant workflow), run `${CLAUDE_PLUGIN_ROOT}/scripts/collection factsheet "<DeckName>"`.

<reference file="decks.md" section="Deck Analysis">
Grouping the returned cards[] by type / CMC / color identity.
</reference>

---

## Scryfall metadata (hydrated by the resolver)

The CLI's resolver populates each card's metadata from its name, so you rarely call Scryfall by
hand. For reference, the fields it maps:

| Scryfall | Model field (Card / OwnedCard) |
|----------|-------------------------------|
| `type_line` | `type_line` |
| `mana_cost` | `mana_cost` |
| `cmc` | `mana_value` |
| `oracle_text` | `oracle_text` |
| `color_identity` | `color_identity` |
| `keywords` | `keywords` |
| `produced_mana` | `produced_mana` |

Direct Scryfall access (when hand-fetching): exact name `GET /cards/named?exact={name}`; all
printings `GET /cards/search?q=!"{name}"&unique=prints`; rate limit 100ms (on HTTP 429 wait 65s);
double-faced cards keep `image_uris`/`oracle_text`/`mana_cost` under `card_faces[0]` while `cmc`
and `prices` stay top-level (the resolver handles this).

---

## Optional / ad-hoc (Airtable-only, read-mostly)

When the active backend is Airtable **and** you (a human) are connected via `/mcp`, you may run
`mcp__airtable__*` **reads** (`list_records`, `search_records`, `get_record`, `describe_table`)
directly against the base for exploration — a big server-side `filterByFormula` prefilter over
inventory, verifying a field id, eyeballing raw rows, or a one-off bulk metadata refresh. That
is out-of-band exploration/maintenance, not a skill step.

**Rule: skills WRITE only through the `collection` CLI** (`add-card`, `set-quantity`,
`remove-card`, `save-deck`, `log-trade`). No executable step in this skill may
create/update/delete via `mcp__airtable__*`. The efficiency patterns (targeted `fields`,
`filterByFormula`, `detailLevel`, batching) and the table/field ids for those ad-hoc reads live
in the appendices below.

---

<references>
## Reference Index

| When you need to... | Read |
|---------------------|------|
| Execute deck loading / query / analysis via the CLI | [decks.md](references/decks.md) |
| Execute card operations (add, quantity, remove) via the CLI | [cards.md](references/cards.md) |
| Execute trade workflows via the CLI | [trades.md](references/trades.md) |
| (Optional / ad-hoc) Field sets and `filterByFormula` patterns for MCP reads | [query-patterns.md](references/query-patterns.md) |
| (Optional / ad-hoc) Efficient Airtable MCP reads (detailLevel, pagination, edge cases) | [airtable-patterns.md](references/airtable-patterns.md) |
| (Optional / ad-hoc) Airtable table/field IDs, schema, multi-copy internals | [airtable-schema.md](references/airtable-schema.md) |
</references>
