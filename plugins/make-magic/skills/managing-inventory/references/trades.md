# Managing Trades

## Source/Destination Model

Trades track card movement between locations using a category + specificity pattern:

- **From (Source)** / **To (Destination)** — category: Library, Deck, Store, Person
- **From (Deck)** / **To (Deck)** — link to specific Deck record (only when category = "Deck")
- **Cards into Destination** — cards entering the destination
- **Cards out of Destination** — cards leaving the destination

**Example:** Swap Horizon Stone for Lavaleaper in Ozai deck from Library:
- From (Source) = "Library"
- To (Destination) = "Deck", To (Deck) = Ozai
- Cards into Destination = [Lavaleaper]
- Cards out of Destination = [Horizon Stone]

## Record a Trade

1. Create card records for any new cards (see [cards.md](cards.md) for Scryfall fetch)
2. Create Trade record:
   - Date, Status (Draft / Planned / Completed)
   - From (Source), From (Deck) if applicable
   - To (Destination), To (Deck) if applicable
   - Cards into Destination / Cards out of Destination (links to Cards)
3. Update affected deck Cards link fields
4. Update Number Owned if cards enter/leave the collection

## Trade Lifecycle

Trades follow a **Draft -> Planned -> Completed** status progression:

| Status | Meaning |
|--------|---------|
| Draft | Exploring options, cards not yet committed |
| Planned | Cards decided, awaiting execution |
| Completed | Trade executed, inventory updated |

**On completion:**
- Set Completed Date
- Update deck card lists (add/remove linked cards)
- Adjust Number Owned on affected card records
- Verify Deck Size formula still matches expected totals
