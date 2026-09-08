# deck-diff

A static [Astro](https://astro.build) viewer for reviewing a proposed upgrade to
a Magic: The Gathering deck. It renders a **deck** plus an authored **changeset**
(adds / drops / considerations) as a side-by-side diff with curve/pip/type
metadata, and an interactive curation layer (a radial "donut" gesture UI backed
by a pure state machine) for accepting, rejecting, or annotating each proposed
change and exporting the resolved result.

The interesting logic lives as pure, framework-free modules in `src/lib/`
(`diff.ts`, `metadata.ts`, `curation.ts`) and is unit-tested with Vitest.

## Data

- `src/data/deck.json` — the deck cards.
- `src/data/changeset.json` — the authored v2 changeset (`adds` / `drops` /
  `considerations`, each with a reason).
- `src/data/enriched.json` — generated (image + price enrichment), gitignored.

## Commands

```sh
pnpm install          # install dependencies
node scripts/prep.mjs # (re)generate src/data/enriched.json — see below
pnpm dev              # local dev server
pnpm build            # static production build (works offline once enriched.json exists)
pnpm preview          # serve the built site
pnpm test             # run the Vitest suite (vitest run)
```

### Regenerating `enriched.json`

`scripts/prep.mjs` enriches the deck via the make-magic backend. It needs the
make-magic environment loaded first:

```sh
set -a; . "/Users/trippwickersham/Code/mtg-deck-designer/.env"; set +a
export MAKE_MAGIC_DATA_DIR="$HOME/.local/share/make-magic" MAKE_MAGIC_BACKEND=airtable
node scripts/prep.mjs
```

`pnpm build` succeeds offline as long as `enriched.json` already exists.
