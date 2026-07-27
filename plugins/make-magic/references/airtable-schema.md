# Airtable Schema — Magic Inventory

## Base: Magic Inventory (`appw7QPMoqktrgDc1`)

### Tables

| Table | ID | Purpose |
|-------|----|---------|
| Magic Cards | `tbliSupwHYSUcAY7l` | Legacy inventory (do not modify) |
| Inventory Cards | `tbl3UgZZPJGQhEFo8` | Normalized — 1 row per card title |
| Decks | `tblIfqVuVHNQza1K3` | Deck configurations |
| Trades | `tblgqqIvTuz0l5SZM` | Card movement tracking |
| Chase Cards | `tblXsNtGgT7UQLPXZ` | Pre-release / wanted cards tracking |

### Inventory Cards table fields (`tbl3UgZZPJGQhEFo8`)

> Note: the live table is named **"Inventory Cards"**. Elsewhere in these docs "Cards" is used as a shorthand label for this same table.

| Field | Type | ID | Notes |
|-------|------|----|-------|
| Card Name | singleLineText (primary) | `fldltxh7GLqkkSYgT` | |
| Sets | multipleSelects | `fldrJmmWZfkGYnjKT` | Auto-creates choices on update |
| Number Owned | number | `flddWBnI3V5eNJnNe` | |
| Foil Count | number | `fld3nfd1I2TkxPvrA` | |
| Condition | multipleSelects | `flduMD0BHO9gqe6ti` | |
| Sources | multipleSelects | `fld9bpabbOvFElUs2` | e.g. "Edge of Eternities Commander" |
| Card Type | singleLineText | `fldMuUcJRwHQZ6FZf` | Scryfall `type_line` |
| Mana Cost | singleLineText | `fldbmWKz2BQOnzHe4` | e.g. `{2}{W}{U}` |
| CMC | number | `fldSPOJRQ6xh5I26J` | Converted mana cost from Scryfall `cmc` field. Lands = 0 |
| Power / Toughness | singleLineText | `fldtNvnnaXa5oVCMS` | e.g. `2/4` |
| Oracle Text | multilineText | `fldNka9DJRBTvu88U` | |
| Card Art | url | `fldLXgSCz5ZKOHLgc` | Scryfall `image_uris.art_crop` (double-faced: `card_faces[0].image_uris.art_crop`) |
| Scryfall URL | url | `fld0LU8D4aaGDPgHq` | Scryfall `scryfall_uri` |
| Price (TCGPlayer) | currency | `fldehSQtf4SWRwqzG` | Scryfall `prices.usd` |
| Price Last Updated | lastModifiedTime | `fldMMjDkaMLLVb4t2` | Watches Price (TCGPlayer) only |
| Color Identity | multipleSelects | `fldKMDR2jgjYq725E` | W/U/B/R/G/Colorless |
| Is Land / Is Creature / Is Non-Creature | formula (boolean) | various | |
| Repeat Number in Decks | number | `fldIk3d5BclFeZL3n` | Extra copies beyond the first in decks. See Multi-Copy Cards below |
| Number in Decks | formula | `fldYpXHYeMSfOJLyq` | `COUNTA(Decks) + Repeat Number in Decks` |
| Number in Library | formula | `fldTDvyPNOrN3MZ4D` | `Number Owned - Number in Decks` |
| Decks | multipleRecordLinks -> Decks | `fld7JS3yjDpokRlba` | |
| Decks (as Commander) | multipleRecordLinks -> Decks | `fld6QRELImbLqapBO` | Inverse of Decks.Commander |
| Trades (In) / Trades (Out) | multipleRecordLinks -> Trades | various | |

### Decks table fields

| Field | Type | Notes |
|-------|------|-------|
| Name | primary | |
| Owner | text | |
| Format | singleSelect | |
| Commander | link -> Inventory Cards | |
| Cards | link -> Inventory Cards | Non-commander, non-basic-land cards |
| Plains/Islands/Swamps/Mountains/Forests/Wastes | number | Basic land counts |
| Repeat Cards Count | number | Extra copies in deck beyond the linked records. See Multi-Copy Cards below |
| Deck Size | formula | `Linked Cards + Commander + Basic Lands + Repeat Cards Count` |
| Notes | multilineText | |
| Strategy | text | `fldvJRaoYfRZiM8zw` — Source of truth for what the deck AIMS to be (human-authored aspiration, prose). See strategy-schema.md for convention |
| Focus Otags | multipleSelects or multilineText | The otags/buckets the deck CARES about — its intended functional identity in the tag vocabulary (bucket names and/or otag slugs). A CURATED subset (not the wide mechanical union the cards carry). Skill/reasoning-authored (or human) by building-decks (Operation 5) and written via the Airtable MCP; **the deterministic pipeline READS it but NEVER writes it**. Distinct from Strategy (prose aim) and Assessment (reality). See quadrant-theory.md |
| Assessment | multilineText (long text) | Reasoning-authored by building-decks (Operation 5) and written via the Airtable MCP. What the deck ACTUALLY is, isn't, and needs — the Quadrant pre-mortem synthesis measuring actual card otags against Focus Otags (coverage of focus, thin/unprotected focus, off-focus noise) plus functional profile and structural gaps. Distinct from Strategy and Focus Otags; not engine-emitted. See quadrant-theory.md |
| Chase Cards | link <- Chase Cards.Target Decks | `fldfoTmUWn5WpuT6u` — Inverse link, auto-populated |
| Color Identity | text | `fldIXcQuMKd7PLyr9` — Deck's color identity (e.g. "WUR", "BG") |
| Creatures / Nonbasic Lands / Non-Creature Spells | rollup | Via Is* helper fields |
| Trades (From) / Trades (To) | link -> Trades | |

### Trades table fields

| Field | Type | Notes |
|-------|------|-------|
| ID | formula | Uses Count fields for card counts |
| Date | date | |
| From (Source) | singleSelect | Category: Library, Deck, Store, Person |
| From (Deck) | link -> Decks | Specificity when Source = "Deck" |
| To (Destination) | singleSelect | Category: Library, Deck, Store, Person |
| To (Deck) | link -> Decks | Specificity when Destination = "Deck" |
| Cards into Destination | link -> Inventory Cards | |
| Cards out of Destination | link -> Inventory Cards | |
| Cards into Destination (Count) | count | Count of Cards into Destination |
| Cards out of Destination (Count) | count | Count of Cards out of Destination |
| Status | singleSelect | Draft / Planned / Completed |
| Completed Date | date | |
| Reason / Notes | text | |

**Source/Destination model:** Source and Destination are categories (Library, Deck, Store, Person). The Deck fields provide specificity when the category is "Deck". Example: swapping a card from Library into a deck -> From (Source) = "Library", To (Destination) = "Deck", To (Deck) = [the deck].

**Note:** Look up current deck records at runtime via `mcp__airtable__list_records` on the Decks table.

### Chase Cards table fields

| Field | Type | ID | Notes |
|-------|------|----|-------|
| Card Name | singleLineText (primary) | `fldf14LO7VRoTZ8PK` | |
| Sets | multipleSelects | `fldlhTQDqpLKxutjK` | Set names from Scryfall |
| Card Type | singleLineText | `fldG2rGqiG8UydPy6` | Scryfall `type_line` |
| Mana Cost | singleLineText | `fld5UtegtLhSWGRNV` | e.g. `{3}{R}` |
| CMC | number | `fldMnldyhgYlEPcFA` | |
| Power / Toughness | singleLineText | `fldnl2R4B7B9X2MlJ` | e.g. `2/4` |
| Oracle Text | multilineText | `fldHSHDka12X4BiHL` | |
| Card Art | url | `fldFvNmj0fqOnOVP3` | Scryfall `image_uris.art_crop` |
| Scryfall URL | url | `fldUjrCkvkBKcWqgh` | |
| Price (TCGPlayer) | currency | `fld8PpkaGej0qDA8x` | |
| Price Last Updated | lastModifiedTime | `fldGkQ71BWcPuie2T` | Watches Price field |
| Color Identity | multipleSelects | `fldEkalJKqK2ZecEv` | W/U/B/R/G/Colorless |
| Target Decks | multipleRecordLinks -> Decks | `flduoZZRmVfpD6aSG` | Which decks want this card |
| Is Land / Is Creature / Is Non-Creature | formula | various | Same pattern as Inventory Cards table |
| Created At | createdTime | `fldWqJ6dAj2mXNx4V` | |
| Last Modified | lastModifiedTime | `fldtYh0qTTObjRkJ7` | |

### Multi-Copy Cards (Non-Singleton Decks)

Airtable link fields only support one link per record — you cannot link the same Card record to a Deck multiple times. For Commander decks (99 singletons + 1 commander), this is fine. For 60-card decks with playsets (e.g., 4x Lightning Bolt), use the repeat fields:

**On the Deck:** `Repeat Cards Count` = total extra copies beyond the linked records. For example, if a deck has 4x Wretched Throng and 3x Inspiring Overseer, Repeat Cards Count = (4-1) + (3-1) = 5.

**On each Card:** `Repeat Number in Decks` = extra copies of that card across all decks beyond the one counted by the link. For Wretched Throng (4 in one deck), Repeat Number in Decks = 3.

**When loading a non-singleton deck:**
1. Link each unique non-basic-land card once to the Deck's Cards field (as normal)
2. Set basic land counts (as normal)
3. For cards with qty > 1: sum (qty - 1) across the deck → set Deck's `Repeat Cards Count`
4. For each card with qty > 1: add (qty - 1) to its `Repeat Number in Decks` field (accumulates across decks)
5. In the Deck's `Notes` field, list each multi-copy card and its quantity so the actual deck composition is recoverable. Example:
   ```
   Non-Commander deck (60 cards). Multi-copy cards:
   4x Wretched Throng
   3x Inspiring Overseer
   4x Tranquil Cove
   3x Backup Agent
   ```

**When removing a card from a non-singleton deck:**
- If the card had qty > 1 in that deck, subtract (qty - 1) from both `Repeat Cards Count` on the Deck and `Repeat Number in Decks` on the Card
- Then unlink the card from the Deck as normal
