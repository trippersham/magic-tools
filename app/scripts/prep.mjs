// Offline enrichment step. Reconstructed after loss.
// Reads src/data/{deck.json, changeset.json}, resolves every card (deck + adds +
// considerations) to its full Scryfall image_uris.normal + prices.usd via the
// make-magic scryfall_cache.py façade, and writes src/data/enriched.json so the
// Astro build has zero network dependency.
//
// Run:  (load make-magic env first)
//   set -a; . "$HOME/Code/mtg-deck-designer/.env"; set +a
//   export MAKE_MAGIC_DATA_DIR="$HOME/.local/share/make-magic"
//   node scripts/prep.mjs
//
// Override the façade path with SCRYFALL_CACHE if the plugin version differs.

import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DATA = join(__dirname, '..', 'src', 'data');

const HOME = process.env.HOME ?? '';
const SCRYFALL =
  process.env.SCRYFALL_CACHE ||
  `${HOME}/.claude/plugins/cache/magic-tools/make-magic/0.6.1/scripts/scryfall_cache.py`;

function run(args) {
  try {
    const out = execFileSync('uv', ['run', '--script', SCRYFALL, ...args], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    return JSON.parse(out);
  } catch {
    return null;
  }
}

function fetchCard(name) {
  // Exact-name lookup first; a few real cards 404 on that endpoint but resolve
  // via a `!"exact"` search — mirror the original fallback.
  let c = run(['get-card', name]);
  if (!c || (!c.image_uris && !c.card_faces)) {
    const s = run(['search', `!"${name}"`]);
    const arr = Array.isArray(s?.data) ? s.data : Array.isArray(s) ? s : [];
    if (arr.length) c = arr[0];
  }
  return c;
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

function enrich(name, extra) {
  const c = fetchCard(name);
  return {
    name: c?.name ?? name,
    requested_name: name,
    qty: extra.qty ?? 1,
    role: extra.role ?? null,
    cmc: c?.cmc ?? c?.mana_value ?? extra.mana_value ?? 0,
    mana_cost: c?.mana_cost ?? extra.mana_cost ?? '',
    type_line: c?.type_line ?? extra.type_line ?? '',
    colors: c?.colors ?? extra.colors ?? [],
    image_normal: imageOf(c),
    scryfall_uri: c?.scryfall_uri ?? null,
    usd: usdOf(c),
    reason: extra.reason ?? null,
    cut: extra.cut ?? null,
  };
}

const deck = JSON.parse(readFileSync(join(DATA, 'deck.json'), 'utf8'));
const cs = JSON.parse(readFileSync(join(DATA, 'changeset.json'), 'utf8'));

const deckEnriched = deck.map((c) =>
  enrich(c.name, {
    qty: c.quantity,
    role: c.role,
    mana_value: c.mana_value,
    mana_cost: c.mana_cost,
    type_line: c.type_line,
    colors: c.colors,
  }),
);
const addsEnriched = cs.adds.map((a) => enrich(a.name, { qty: 1, reason: a.reason, cut: a.cut }));
const considEnriched = cs.considerations.map((c) => enrich(c.name, { qty: 1, reason: c.reason }));

const enriched = {
  deck: deckEnriched,
  adds: addsEnriched,
  considerations: considEnriched,
  changeset: {
    adds: cs.adds.map((a) => a.name),
    drops: cs.drops.map((d) => d.name),
    considerations: cs.considerations.map((c) => c.name),
  },
};

const out = join(DATA, 'enriched.json');
writeFileSync(out, JSON.stringify(enriched, null, 2));

const missing = [...addsEnriched, ...considEnriched, ...deckEnriched]
  .filter((c) => !c.image_normal)
  .map((c) => c.name);
console.log(
  `Wrote ${out}: ${deckEnriched.length} deck + ${addsEnriched.length} add + ${considEnriched.length} consideration cards.`,
);
if (missing.length) console.log(`Unresolved images (${missing.length}): ${missing.join(', ')}`);
