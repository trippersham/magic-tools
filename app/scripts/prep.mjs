// Offline enrichment step — deterministic, lake-backed.
//
// Reads src/data/{deck.json, changeset.json} and writes src/data/enriched.json so
// the Astro build has zero network dependency. The full card image (image_normal)
// is now a deterministic column in the make-magic lake (oracle_cards), so this
// step resolves every card (deck + adds + considerations) in a SINGLE lake read —
// zero live Scryfall fetches for cards present in the lake.
//
// Field sourcing:
//   - cmc / mana_cost / type_line / colors: taken from deck.json (authored) when
//     present; otherwise from the lake row (adds/considerations aren't authored
//     with those fields). No per-card Scryfall re-derivation.
//   - image_normal + scryfall_uri: read from the lake (one batched query over all
//     names). A card MISSING image_normal in the lake (a true miss, or a DFC whose
//     image lives on card_faces) is backfilled via a single live `get-card` for
//     THAT card only; backfilled names are logged.
//   - usd (price): OPT-IN and OFF by default (price is volatile). Standard runs
//     write usd:null (the info pane renders "—"). Pass --prices or PREP_PRICES=1 to
//     live-fetch price per card via the get-card façade.
//
// The enriched.json shape (keys per card) is unchanged, so the app needs no change.
//
// Run:  (point at the worktree façade + the synced lake)
//   export MAKE_MAGIC_DATA_DIR="$HOME/.local/share/make-magic"
//   export SCRYFALL_CACHE="<repo>/plugins/make-magic/scripts/scryfall_cache.py"
//   node scripts/prep.mjs            # deterministic, no price
//   node scripts/prep.mjs --prices   # + live price per card (opt-in)
//
// PIPELINE_DIR overrides the make-magic pipeline project used for the lake read.

import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DATA = join(__dirname, '..', 'src', 'data');
const REPO_ROOT = resolve(__dirname, '..', '..');

const HOME = process.env.HOME ?? '';
const WANT_PRICES = process.argv.includes('--prices') || process.env.PREP_PRICES === '1';

// The make-magic pipeline project (carries the lake-backed card resolver). The
// batched lake read runs `uv run --project <PIPELINE_DIR>` so it uses the pipeline's
// own environment; no network, no per-card subprocess fan-out.
const PIPELINE_DIR =
  process.env.PIPELINE_DIR || join(REPO_ROOT, 'plugins', 'make-magic', 'pipeline');

// The live-fallback façade (only invoked for a card the lake is missing, or for
// price when --prices is passed). Override with SCRYFALL_CACHE if the plugin
// version differs from the worktree.
const SCRYFALL =
  process.env.SCRYFALL_CACHE ||
  `${HOME}/.claude/plugins/cache/magic-tools/make-magic/0.6.1/scripts/scryfall_cache.py`;

// --- batched, zero-network lake read ------------------------------------- //

// One `uv run` for ALL names: resolve each against the lake (exact + DFC face
// match) via the package resolver's lake-only path (no live fallback here), and
// return name -> {name, cmc, mana_cost, type_line, colors, scryfall_uri,
// image_normal}. A lake miss yields null for that name.
const LAKE_READER = `
import sys, json
from pipeline.collection.resolver import DuckDBCardResolver

names = json.load(sys.stdin)
r = DuckDBCardResolver()
out = {}
for n in names:
    c = r._resolve_from_lake(n)
    out[n] = None if c is None else {
        "name": c.name,
        "cmc": c.mana_value,
        "mana_cost": c.mana_cost,
        "type_line": c.type_line,
        "colors": list(c.colors or []),
        "scryfall_uri": c.scryfall_uri,
        "image_normal": c.image_normal,
    }
print(json.dumps(out))
`;

function lakeBatch(names) {
  if (!names.length) return {};
  try {
    const out = execFileSync(
      'uv',
      ['run', '--project', PIPELINE_DIR, 'python', '-c', LAKE_READER],
      { encoding: 'utf8', input: JSON.stringify(names), stdio: ['pipe', 'pipe', 'ignore'] },
    );
    return JSON.parse(out);
  } catch {
    // Fail open: an unavailable lake degrades to per-card backfill for everything.
    return {};
  }
}

// --- live fallback (per-card, only on a lake miss or for opt-in price) ---- //

function getCardRaw(name) {
  try {
    const out = execFileSync('uv', ['run', '--script', SCRYFALL, 'get-card', name], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    return JSON.parse(out);
  } catch {
    return null;
  }
}

function imageOf(c) {
  if (!c) return null;
  if (c.image_uris?.normal) return c.image_uris.normal;
  if (c.card_faces?.[0]?.image_uris?.normal) return c.card_faces[0].image_uris.normal;
  return null;
}

function usdOf(c) {
  if (!c?.prices) return null;
  if (c.prices.usd != null) return Number(c.prices.usd);
  if (c.prices.usd_foil != null) return Number(c.prices.usd_foil);
  return null;
}

// --- enrichment ---------------------------------------------------------- //

const backfilled = [];

function enrich(name, extra, lake) {
  const rec = lake[name] ?? null;

  // Authored deck values win; adds/considerations fall back to the lake row.
  const cmc = extra.mana_value ?? rec?.cmc ?? 0;
  const mana_cost = extra.mana_cost ?? rec?.mana_cost ?? '';
  const type_line = extra.type_line ?? rec?.type_line ?? '';
  const colors = extra.colors ?? rec?.colors ?? [];

  let resolvedName = rec?.name ?? name;
  let image_normal = rec?.image_normal ?? null;
  let scryfall_uri = rec?.scryfall_uri ?? null;
  let usd = null;

  // Backfill the image only when the lake is missing it (true miss or a DFC whose
  // image lives on card_faces). One live fetch for THIS card only.
  if (image_normal == null) {
    const raw = getCardRaw(name);
    if (raw) {
      resolvedName = raw.name ?? resolvedName;
      image_normal = imageOf(raw);
      scryfall_uri = scryfall_uri ?? raw.scryfall_uri ?? null;
      if (WANT_PRICES) usd = usdOf(raw);
    }
    backfilled.push(name);
  } else if (WANT_PRICES) {
    // Price is not a lake column — live-fetch it per card only when opted in.
    usd = usdOf(getCardRaw(name));
  }

  return {
    name: resolvedName,
    requested_name: name,
    qty: extra.qty ?? 1,
    role: extra.role ?? null,
    cmc,
    mana_cost,
    type_line,
    colors,
    image_normal,
    scryfall_uri,
    usd,
    reason: extra.reason ?? null,
    cut: extra.cut ?? null,
  };
}

const deck = JSON.parse(readFileSync(join(DATA, 'deck.json'), 'utf8'));
const cs = JSON.parse(readFileSync(join(DATA, 'changeset.json'), 'utf8'));

// A single lake read covering every name (deck + adds + considerations).
const allNames = [
  ...deck.map((c) => c.name),
  ...cs.adds.map((a) => a.name),
  ...cs.considerations.map((c) => c.name),
];
const lake = lakeBatch(allNames);

const deckEnriched = deck.map((c) =>
  enrich(
    c.name,
    {
      qty: c.quantity,
      role: c.role,
      mana_value: c.mana_value,
      mana_cost: c.mana_cost,
      type_line: c.type_line,
      colors: c.colors,
    },
    lake,
  ),
);
// v3.1: adds/drops carry an optional `qty` (default 1). Enriched add cards keep
// their copy count; per-name qty maps ride on the changeset so the diff engine
// nets copies without re-parsing the authored file.
const addsEnriched = cs.adds.map((a) =>
  enrich(a.name, { qty: a.qty ?? 1, reason: a.reason, cut: a.cut }, lake),
);
const considEnriched = cs.considerations.map((c) =>
  enrich(c.name, { qty: 1, reason: c.reason }, lake),
);

const qtyMap = (list) => {
  const m = {};
  for (const e of list) if (e.qty != null && e.qty !== 1) m[e.name] = e.qty;
  return m;
};

const enriched = {
  deck: deckEnriched,
  adds: addsEnriched,
  considerations: considEnriched,
  changeset: {
    adds: cs.adds.map((a) => a.name),
    drops: cs.drops.map((d) => d.name),
    considerations: cs.considerations.map((c) => c.name),
    addQty: qtyMap(cs.adds),
    dropQty: qtyMap(cs.drops),
  },
};

const out = join(DATA, 'enriched.json');
writeFileSync(out, JSON.stringify(enriched, null, 2));

console.log(
  `Wrote ${out}: ${deckEnriched.length} deck + ${addsEnriched.length} add + ${considEnriched.length} consideration cards.`,
);
console.log(
  backfilled.length
    ? `Live-backfilled image for ${backfilled.length} card(s) absent from the lake: ${backfilled.join(', ')}`
    : 'Live-backfilled image for 0 cards — every card resolved from the lake (zero live Scryfall fetches).',
);
console.log(WANT_PRICES ? 'Prices: live-fetched per card (--prices).' : 'Prices: skipped (usd=null); pass --prices to enable.');

const missing = [...addsEnriched, ...considEnriched, ...deckEnriched]
  .filter((c) => !c.image_normal)
  .map((c) => c.name);
if (missing.length) console.log(`Unresolved images (${missing.length}): ${missing.join(', ')}`);
