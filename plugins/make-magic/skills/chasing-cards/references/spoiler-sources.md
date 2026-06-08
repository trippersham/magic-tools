# Spoiler Sources

## Source Hierarchy

1. **MythicSpoiler** — fast early detection via HTML scraping
2. **Scryfall** — authoritative metadata, slower to update

MythicSpoiler often has cards hours or days before Scryfall. The sync engine uses MythicSpoiler for initial discovery and Scryfall for authoritative backfill.

## Sync Engine

### How it works

The spoiler sync script (`${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py`) runs in three phases:

1. **Phase 1: MythicSpoiler scraping** — scrapes set index pages + newspoilers.html for new card entries. Cards discovered here have slugs and images but no structured metadata.
2. **Phase 2: Scryfall API** — queries Scryfall for all cards in the target sets using ETag-based caching. Provides full metadata (oracle text, mana cost, type line, etc.).
3. **Phase 3: Reconciliation** — attempts to match unconfirmed MythicSpoiler entries to their Scryfall counterparts using fuzzy name matching.

### State tracking

Local SQLite database (`spoiler_cache.db`) tracks:
- All known cards with source attribution (first_seen_source: mythicspoiler or scryfall)
- Confirmation status (confirmed_by_scryfall flag)
- MythicSpoiler slug ↔ Scryfall ID linkage
- ETag for Scryfall API caching
- Sync timestamps

### CLI Commands

```bash
# Sync specific sets
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py sync msh msc

# Check current state
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py status

# List all cards (or just new/unconfirmed)
uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py list --new
```

## Set Code Conventions

- Scryfall and MythicSpoiler both use 3-letter set codes (lowercase)
- Examples: `msh` (Marvel Secret Heroes), `msc` (Marvel Secret Commander), `blb` (Bloomburrow)
- Check Scryfall's set list for official codes: the `set` field in card data

## Known Limitations

- **MythicSpoiler HTML structure** can change without notice — scraping may break
- **MythicSpoiler 404s** are normal for sets not yet on the site
- **Fuzzy matching** in Phase 3 uses slug-to-name conversion which can fail on unusual card names
- **Scryfall rate limiting** — 100ms between requests, 429 responses trigger 30-65s cooldown
- **ETag caching** — Scryfall returns 304 Not Modified when no changes, saving bandwidth
