# Airtable Schema — Magic Inventory

## Base: Magic Inventory (`appw7QPMoqktrgDc1`)

### Tables

| Table | ID | Purpose |
|-------|----|---------|
| Magic Cards | `tbliSupwHYSUcAY7l` | Legacy inventory (do not modify) |
| Cards | `tbl3UgZZPJGQhEFo8` | Normalized — 1 row per card title |
| Decks | `tblIfqVuVHNQza1K3` | Deck configurations |
| Trades | `tblgqqIvTuz0l5SZM` | Card movement tracking |

### Cards table fields

| Field | Type | Notes |
|-------|------|-------|
| Card Name | primary | |
| Sets | multiSelect | Auto-creates choices on update |
| Number Owned | number | |
| Foil Count | number | |
| Condition | multiSelect | |
| Sources | multiSelect | e.g. "Edge of Eternities Commander" |
| Card Type | text | Scryfall `type_line` |
| Mana Cost | text | e.g. `{2}{W}{U}` |
| CMC | number | Converted mana cost from Scryfall `cmc` field. Lands = 0 |
| Power / Toughness | text | e.g. `2/4` |
| Oracle Text | long text | |
| Card Art | url | Scryfall `image_uris.art_crop` (double-faced: `card_faces[0].image_uris.art_crop`) |
| Scryfall URL | url | Scryfall `scryfall_uri` |
| Price (TCGPlayer) | currency | Scryfall `prices.usd` |
| Price Last Updated | lastModifiedTime | Watches Price (TCGPlayer) only |
| Is Land / Is Creature / Is Non-Creature Spell | formula (boolean) | |
| Number in Decks | formula | |
| Number in Library | formula | |
| Decks | link -> Decks | |
| Trades (In) / Trades (Out) | link -> Trades | |
| Commander of | inverse link from Decks.Commander | |

### Decks table fields

| Field | Type | Notes |
|-------|------|-------|
| Name | primary | |
| Owner | text | |
| Format | singleSelect | |
| Commander | link -> Cards | |
| Cards | link -> Cards | Non-commander, non-basic-land cards |
| Plains/Islands/Swamps/Mountains/Forests/Wastes | number | Basic land counts |
| Deck Size | formula | cards + commander + lands |
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
| Cards into Destination | link -> Cards | |
| Cards out of Destination | link -> Cards | |
| Cards into Destination (Count) | count | Count of Cards into Destination |
| Cards out of Destination (Count) | count | Count of Cards out of Destination |
| Status | singleSelect | Draft / Planned / Completed |
| Completed Date | date | |
| Reason / Notes | text | |

**Source/Destination model:** Source and Destination are categories (Library, Deck, Store, Person). The Deck fields provide specificity when the category is "Deck". Example: swapping a card from Library into a deck -> From (Source) = "Library", To (Destination) = "Deck", To (Deck) = [the deck].

**Note:** Look up current deck records at runtime via `mcp__airtable__list_records` on the Decks table.
