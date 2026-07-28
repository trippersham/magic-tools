# Spoiler Sources

## Source Hierarchy

1. **MythicSpoiler** — fast early detection via HTML scraping
2. **Scryfall** — authoritative metadata, slower to update

MythicSpoiler often has cards hours or days before Scryfall. The sync engine uses MythicSpoiler for initial discovery and Scryfall for authoritative backfill.

## Sync Engine

### How it works

The spoiler sync script (`${CLAUDE_PLUGIN_ROOT}/scripts/spoiler_sync.py`) is a thin façade over
the pipeline's spoiler lineage. It runs two phases:

1. **Phase 1: MythicSpoiler scraping** (`pipeline.sources.spoilers`) — scrapes set index pages +
   newspoilers.html for card entries and lands them in the lake's `raw/spoilers` Parquet (append-
   deduped on `(set_code, slug)`). Cards discovered here have slugs and images but no structured
   metadata.
2. **Phase 2: Reconciliation** (`pipeline.transforms.spoilers`) — reconciles each slug to a
   Scryfall identity via the lake-backed **card resolver** (the card dim), writing the reconciled
   previews to `normalized/spoilers` (the `Spoiler` contract). A confirmed match carries an
   `oracle_id` + `confirmed=True`; an unreconciled preview stays `oracle_id=None,
   confirmed=False`.

### State tracking

State lives in the **pipeline lake** (DuckDB/Parquet under `MAKE_MAGIC_DATA_DIR`) — there is **no
SQLite `spoiler_cache.db`**:
- `raw/spoilers` — every scraped preview row (slug, image, set code), append-deduped across runs.
- `normalized/spoilers` — the reconciled `Spoiler` records: slug, set_code, name,
  `oracle_id` (null until Scryfall-confirmed), source (`mythicspoiler`/`scryfall`),
  `first_seen_cursor`, `confirmed`.
- **"New since last sync"** derives from the lake — slugs present in the current scrape but absent
  from the prior `normalized/spoilers` table (a snapshot-diff), replacing the old SQLite `meta`
  watermark and seen-slug/seen-id sets. Each newly-seen row is stamped with the spoiler cursor as
  its `first_seen_cursor`; that cursor is preserved on re-runs.

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
- **MythicSpoiler unreachable** — the scrape fails open, keeping the last `raw/spoilers` snapshot
  (no crash); on a first-ever run with a dead network you simply get an empty table
- **MythicSpoiler 404s** are normal for sets not yet on the site
- **Reconciliation** uses slug-to-name conversion handed to the card resolver, which can fail on
  unusual card names (the preview stays unconfirmed until a later run resolves it)
- **Scryfall rate limiting** — the resolver rate-limits its live fallbacks; already-landed cards
  resolve offline from the lake
