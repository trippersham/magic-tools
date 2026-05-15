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
| Repeat Number in Decks | number | Extra copies beyond the first in decks (for multi-copy cards). See Multi-Copy Cards below |
| Number in Decks | formula | `COUNTA(Decks) + Repeat Number in Decks` |
| Number in Library | formula | `Number Owned - Number in Decks` |
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
| Repeat Cards Count | number | Extra copies in deck beyond the linked records. See Multi-Copy Cards below |
| Deck Size | formula | `Linked Cards + Commander + Basic Lands + Repeat Cards Count` |
| Notes | multilineText | |
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
