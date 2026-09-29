// Offline combo-graph prep — deterministic, lake/CLI-backed.
//
// Invokes the SOURCE make-magic `deck-combos` CLI (the widened projection lives
// in source, NOT the frozen plugin cache) and normalizes its JSON into
// src/data/combos.json, the graph data layer consumed by the Astro build.
//
// The CLI emits granular `produces[]` features per combo; we collapse those into
// canonical outcome types via src/scripts/combo/outcomes.ts (the single source of
// truth for bucketing, shared with the app + tests). Card imagery/labels are
// joined from src/data/enriched.json when present (else null). No combos are
// invented — only what the CLI returns.
//
// Run:
//   set -a; . /Users/trippwickersham/Code/mtg-deck-designer/.env; set +a
//   export MAKE_MAGIC_DATA_DIR="$HOME/.local/share/make-magic"
//   node scripts/prep-combos.mjs ["<deck name>"]
//
// Env overrides: MAKE_MAGIC_SOURCE (path to the make-magic plugin dir).

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

// --- self re-exec with type-stripping so we can import outcomes.ts ---------- //
// outcomes.ts is TypeScript; Node needs --experimental-strip-types to import it.
// Re-exec once (guarded by an env sentinel) if the flag isn't already active.
if (!process.env.__PREP_COMBOS_REEXEC) {
  const r = spawnSync(
    process.execPath,
    ['--experimental-strip-types', '--no-warnings', fileURLToPath(import.meta.url), ...process.argv.slice(2)],
    { stdio: 'inherit', env: { ...process.env, __PREP_COMBOS_REEXEC: '1' } },
  );
  process.exit(r.status ?? 1);
}

const __dirname = dirname(fileURLToPath(import.meta.url));
const DATA = join(__dirname, '..', 'src', 'data');

const { outcomeTypesFor, outcomeTypeIdsFor, OUTCOME_TYPES } = await import(
  pathToFileURL(join(__dirname, '..', 'src', 'scripts', 'combo', 'outcomes.ts')).href
);

const HOME = process.env.HOME ?? '';
const SOURCE =
  process.env.MAKE_MAGIC_SOURCE || '/Users/trippwickersham/Code/magic-tools/plugins/make-magic';
const DECK = process.argv[2] || 'Paul Atreides, Kwisatz Haderach (Queza)';

// --- invoke the SOURCE deck-combos CLI ------------------------------------- //

function fetchCombos(deck) {
  // Override: read a pre-computed deck-combos JSON (e.g. detected directly from
  // the app's deck.json) instead of invoking the store-backed CLI. Lets the graph
  // reflect a locked-in list without mutating the make-magic deck store.
  const override = process.env.DECK_COMBOS_JSON;
  if (override && existsSync(override)) {
    return JSON.parse(readFileSync(override, 'utf8'));
  }
  const bin = join(SOURCE, 'scripts', 'collection');
  const out = execFileSync(bin, ['deck-combos', deck], {
    encoding: 'utf8',
    maxBuffer: 32 * 1024 * 1024,
    env: {
      ...process.env,
      MAKE_MAGIC_DATA_DIR: process.env.MAKE_MAGIC_DATA_DIR || `${HOME}/.local/share/make-magic`,
    },
  });
  return JSON.parse(out);
}

// --- enriched.json join (image + labels by card name) ---------------------- //

function loadCardIndex() {
  const idx = new Map();
  const path = join(DATA, 'enriched.json');
  if (!existsSync(path)) return idx;
  const e = JSON.parse(readFileSync(path, 'utf8'));
  for (const list of [e.deck, e.adds, e.considerations]) {
    for (const c of list ?? []) {
      if (!idx.has(c.name)) {
        idx.set(c.name, { image: c.image_normal ?? null, labels: c.labels ?? [] });
      }
    }
  }
  return idx;
}

// --- TRIGGER Layer-0 (HYBRID model, verified enabler classification) ------- //
//
// The focus view gains a "true Layer 0" to the LEFT of the clicked card: the
// specific deck cards / mechanics that TRIGGER the focused card. The Layer-0
// mechanic set for a card C is the UNION of two sources:
//   (a) COMBO PREREQ — the trigger mechanic of every combo C participates in
//       (prereq "way to gain life" ⇒ gain life; "way to draw a card" ⇒ draw a card;
//       Peer hard-cast + the self-contained Sensei's-Top combo ⇒ no enabler trigger).
//   (b) CARD OWN-TRIGGER — the mechanic C's OWN ability fires on (e.g. Orcish
//       Bowmasters / Underworld Dreams / Sheoldred fire on an opponent DRAWING;
//       Exquisite Blood fires on an opponent LOSING LIFE).
// So a card can carry MULTIPLE Layer-0 capsules (Sheoldred = draw + opponent-draws;
// Exquisite = opponent-loses-life + gain-life).
//
// A card is an ENABLER for a mechanic when it CAUSES that action — NOT when it
// merely triggers ON it (payoffs / doublers / punishers are excluded). All four
// enabler lists are VERIFIED (oracle-text classification is error-prone) — the
// tables below are the committed source of truth and are asserted to resolve to
// real card imagery before the graph ships. The derived per-card union is
// cross-checked against EXPECTED_LAYER0 (the verified card→mechanics mapping) so
// the shipped `layer0Mechanics` can never silently diverge.

const TRIGGER_MECHANICS = {
  'draw a card': {
    mechanic: 'draw a card',
    label: 'Draw a card',
    sublabel: 'Draw Step',
    icon: '📜',
    enablers: [
      'Brainstorm', "Night's Whisper", 'Rhystic Study', 'Mystic Remora',
      'Esper Sentinel', 'Consecrated Sphinx', 'Phyrexian Arena',
      'Windfall', 'Timetwister', 'Echo of Eons', 'Peer into the Abyss',
      "Sensei's Divining Top", 'Notion Thief',
      'Drogskol Reaver', "Lich's Mastery",
      'Well of Lost Dreams', 'Raven Eagle',
    ],
  },
  'gain life': {
    mechanic: 'gain life',
    label: 'Gain life',
    sublabel: null,
    icon: '❤️',
    enablers: [
      'Children of Korlis', 'Drogskol Reaver', 'Elixir of Immortality',
      'Exquisite Blood', 'Gixian Puppeteer', 'Queza, Augur of Agonies',
      'Raven Eagle', 'Sheoldred, the Apocalypse', 'Soul Warden',
      'Vito, Thorn of the Dusk Rose',
    ],
  },
  'opponent draws': {
    mechanic: 'opponent draws',
    label: 'An opponent draws',
    sublabel: 'forced / symmetric draw',
    icon: '🃏',
    enablers: [
      'Echo of Eons', 'Peer into the Abyss',
      'Timetwister', 'Windfall',
    ],
  },
  'opponent loses life': {
    mechanic: 'opponent loses life',
    label: 'An opponent loses life',
    sublabel: 'drain / damage',
    icon: '🩸',
    enablers: [
      "Bolas's Citadel", 'Cliffhaven Vampire', 'Epicure of Blood',
      'Gixian Puppeteer', 'Marauding Blight-Priest', 'Orcish Bowmasters',
      'Peer into the Abyss', 'Psychosis Crawler', 'Queza, Augur of Agonies',
      'Raven Eagle', 'Sanguine Bond', 'Sheoldred, the Apocalypse',
      'Underworld Dreams', 'Vito, Thorn of the Dusk Rose',
    ],
  },
  // 5th mechanic (v2 role model). ENABLERS = cards that cause a creature to enter
  // under your control (every creature body + token/army makers). VERIFIED list
  // (trigger-roles-v2.json → enablers["creature enters"]); a Soul-Warden flood of
  // ~17 tiles is expected and handled by the fit-aware pager.
  'creature enters': {
    mechanic: 'creature enters',
    label: 'A creature enters',
    sublabel: 'ETB',
    icon: '🐾',
    enablers: [
      'Children of Korlis', 'Cliffhaven Vampire', 'Consecrated Sphinx',
      'Drogskol Reaver', 'Epicure of Blood', 'Esper Sentinel',
      'Gixian Puppeteer', 'Grand Abolisher', 'Marauding Blight-Priest',
      'Notion Thief', 'Orcish Bowmasters', 'Psychosis Crawler',
      'Queza, Augur of Agonies', 'Raven Eagle', 'Sheoldred, the Apocalypse',
      'Soul Warden', 'Vito, Thorn of the Dusk Rose',
    ],
  },
};

// AMPLIFIERS (v2 role model) — the THIRD causal role. An amplifier multiplies /
// replaces instances of a mechanic ("draw two cards instead", "gain twice that
// much life", doublers) — it PRESUPPOSES the mechanic rather than producing it.
// A payoff's Layer 0 surfaces enablers AND amplifiers of its mechanic(s); the
// amplifier is tagged (×2 / "AMPLIFIES") and NEVER dropped. Amplifiers remain
// CLI-detected combo nodes — tagging one here only ALSO surfaces it in Layer 0;
// it never removes it from the graph or from combo membership (roles ADD, never
// SUBTRACT). VERIFIED (trigger-roles-v2.json → amplifiers).
const AMPLIFIERS = {
  'draw a card': ["Alhammarret's Archive", "Teferi's Ageless Insight"],
  'gain life': ["Alhammarret's Archive"],
};

// Each card's OWN trigger mechanic(s) — what its ability FIRES ON (source (b) of
// the hybrid union). VERIFIED (trigger-enablers-hybrid.json → card_own_triggers).
// Cards with no own trigger are omitted (empty).
const CARD_OWN_TRIGGERS = {
  'Queza, Augur of Agonies': ['draw a card'],
  'Consecrated Sphinx': ['opponent draws'],
  'Sheoldred, the Apocalypse': ['draw a card', 'opponent draws'],
  'Underworld Dreams': ['opponent draws'],
  'Psychosis Crawler': ['draw a card'],
  'Sanguine Bond': ['gain life'],
  'Exquisite Blood': ['opponent loses life'],
  'Vito, Thorn of the Dusk Rose': ['gain life'],
  'Drogskol Reaver': ['gain life'],
  'Epicure of Blood': ['gain life'],
  'Marauding Blight-Priest': ['gain life'],
  'Cliffhaven Vampire': ['gain life'],
  'Vizkopa Guildmage': ['gain life'],
  'Bloodthirsty Conqueror': ['gain life'],
  'Enduring Tenacity': ['gain life'],
  'Well of Lost Dreams': ['gain life'],
  'Gixian Puppeteer': ['draw a card'],
  'Raven Eagle': ['draw a card'],
  'Orcish Bowmasters': ['opponent draws'],
  "Lich's Mastery": ['gain life'],
  // v2: Soul Warden's own ability fires on "another creature enters" → its
  // Layer 0 becomes the creature-enters flood (it was empty before). Soul Warden
  // is not a combo participant (degree 0), so this own-trigger is its ONLY source.
  'Soul Warden': ['creature enters'],
};

// The VERIFIED final per-card Layer-0 mechanic set (= card_own_triggers ∪ the
// trigger of every combo the card is in). This is the authoritative mapping the
// focus view consumes; the derived union below is asserted EQUAL to it so a
// divergent derivation fails the build rather than shipping. (Order = own-triggers
// first, then combo-prereq mechanics not already present — matches the verified file.)
const EXPECTED_LAYER0 = {
  'Queza, Augur of Agonies': ['draw a card'],
  'Consecrated Sphinx': ['opponent draws'],
  'Sheoldred, the Apocalypse': ['draw a card', 'opponent draws'],
  'Underworld Dreams': ['opponent draws'],
  'Psychosis Crawler': ['draw a card'],
  'Sanguine Bond': ['gain life'],
  'Exquisite Blood': ['opponent loses life', 'gain life'],
  'Vito, Thorn of the Dusk Rose': ['gain life'],
  'Drogskol Reaver': ['gain life', 'draw a card'],
  'Epicure of Blood': ['gain life'],
  'Marauding Blight-Priest': ['gain life'],
  'Cliffhaven Vampire': ['gain life'],
  'Vizkopa Guildmage': ['gain life'],
  'Bloodthirsty Conqueror': ['gain life'],
  'Enduring Tenacity': ['gain life'],
  'Well of Lost Dreams': ['gain life'],
  'Gixian Puppeteer': ['draw a card'],
  'Raven Eagle': ['draw a card'],
  'Orcish Bowmasters': ['opponent draws'],
  "Lich's Mastery": ['gain life', 'draw a card'],
  'Soul Warden': ['creature enters'],
};

/** Derive a combo's trigger mechanic from its prerequisites (prereq-only rule). */
function triggerMechanicFor(combo) {
  const pre = (combo.prerequisites ?? []).join(' \n ').toLowerCase();
  if (/way to gain life/.test(pre)) return 'gain life';
  if (/way to draw a card/.test(pre)) return 'draw a card';
  return null;
}

/** Resolve a card's image/labels from enriched.json, else fetch via the make-magic
 *  scryfall_cache façade (mirrors enrich-fallback.py). Every enabler needs a real
 *  image — a miss is logged loudly. */
function resolveEnabler(name, cardIndex) {
  const hit = cardIndex.get(name);
  if (hit?.image) return { name, image: hit.image, labels: hit.labels ?? [] };
  // Façade fallback (only fires if enriched.json lacks the card).
  const facade = join(
    HOME,
    '.claude/plugins/cache/magic-tools/make-magic/0.7.0/scripts/scryfall_cache.py',
  );
  if (existsSync(facade)) {
    try {
      const out = execFileSync('uv', ['run', '--script', facade, 'get-card', name], {
        encoding: 'utf8',
        timeout: 30000,
      });
      const c = JSON.parse(out);
      const img =
        c.image_uris?.normal ?? (c.card_faces ?? [])[0]?.image_uris?.normal ?? null;
      if (img) {
        console.warn(`  [trigger] resolved enabler "${name}" via façade fallback`);
        return { name, image: img, labels: hit?.labels ?? [] };
      }
    } catch {
      /* fall through to the loud miss below */
    }
  }
  console.error(`  [trigger] MISSING image for enabler "${name}" — layer-0 tile will be blank`);
  return { name, image: null, labels: hit?.labels ?? [] };
}

// --- normalize ------------------------------------------------------------- //

const raw = fetchCombos(DECK);
const rawCombos = raw.combos ?? [];
const cardIndex = loadCardIndex();

// Deck-membership truth set: the verified Layer-0 tables are static (deck-agnostic),
// so a card cut from the deck can still linger in an enabler/own-trigger list and
// surface a ghost tile. Filter every Layer-0 surface to cards actually in deck.json.
const deckNames = new Set(
  JSON.parse(readFileSync(join(DATA, 'deck.json'), 'utf8')).map((c) => c.name),
);
const inDeck = (name) => deckNames.has(name);

// Combo records + outcome-type accumulation.
const combos = rawCombos.map((c) => {
  const id = c.variant_id ?? c.variantId ?? c.id;
  const produces = (c.produces ?? []).map((p) => ({ name: p.name, status: p.status, win: p.win }));
  const record = {
    id,
    variantId: id,
    cards: c.cards ?? [],
    steps: c.steps ?? [],
    prerequisites: c.prerequisites ?? [],
    produces,
    outcomeTypes: outcomeTypeIdsFor(produces),
    spellbookUrl: c.spellbookUrl ?? `https://commanderspellbook.com/combo/${id}/`,
  };
  record.triggerMechanic = triggerMechanicFor(record);
  return record;
});

// Deduped outcomeTypes across all combos, in canonical registry order.
const usedOutcomeIds = new Set();
for (const c of combos) for (const t of c.outcomeTypes) usedOutcomeIds.add(t);
const outcomeTypes = Object.values(OUTCOME_TYPES).filter((t) => usedOutcomeIds.has(t.id));

// Card degree = number of combos each card participates in.
const degree = new Map();
for (const c of combos) for (const name of c.cards) degree.set(name, (degree.get(name) ?? 0) + 1);

// Nodes: one per card, one per used outcome type.
const nodes = [];
for (const [name, deg] of degree) {
  const info = cardIndex.get(name) ?? { image: null, labels: [] };
  nodes.push({
    id: `card:${name}`,
    kind: 'card',
    name,
    image: info.image,
    labels: info.labels,
    degree: deg,
  });
}
for (const t of outcomeTypes) {
  nodes.push({ id: `outcome:${t.id}`, kind: 'outcome', outcomeType: t.id });
}

// Edges:
//   - combo edges: PAIRWISE among a combo's cards (2-card => 1 edge; 3-card => 3
//     edges = all pairs). Pairwise chosen over a star so the graph has no
//     synthetic hub node and every co-occurrence is a real edge.
//   - produces edges: each participating card -> each outcome-type node.
const edges = [];
for (const c of combos) {
  for (let i = 0; i < c.cards.length; i++) {
    for (let j = i + 1; j < c.cards.length; j++) {
      edges.push({
        source: `card:${c.cards[i]}`,
        target: `card:${c.cards[j]}`,
        kind: 'combo',
        comboId: c.id,
      });
    }
  }
  for (const name of c.cards) {
    for (const t of c.outcomeTypes) {
      edges.push({ source: `card:${name}`, target: `outcome:${t}`, kind: 'produces', comboId: c.id });
    }
  }
}

const combo_data_available = raw.combo_data_available ?? combos.length > 0;

// --- TRIGGER Layer-0 emission --------------------------------------------- //
//
// `triggers` carries (a) the mechanic defs with their VERIFIED enabler card lists
// (image/labels resolved), (b) the derived per-combo mechanic, and (c) which
// enablers are combo participants (dotted faux-edge). The focus math (layers.ts)
// reads this to synthesize Layer 0 at focus time.
const comboMechanic = {};
for (const c of combos) comboMechanic[c.id] = c.triggerMechanic;

// The combo-participant card set (already graph nodes) — enablers outside it need
// their own extra tiles so the focus DAG can render them on the left.
const comboCardNames = new Set(degree.keys());

// Faux-edge enablers = enablers that are THEMSELVES combo participants (they chain
// further left; already clickable to re-root). Derived from combo membership per
// mechanic — reproduces the previously hand-maintained draw/gain lists exactly and
// extends cleanly to the two new mechanics.
const FAUX_EDGE_ENABLERS = {};
for (const [key, def] of Object.entries(TRIGGER_MECHANICS)) {
  FAUX_EDGE_ENABLERS[key] = def.enablers.filter((n) => comboCardNames.has(n));
}

const mechanics = {};
const enablerNodeNames = new Set();
for (const [key, def] of Object.entries(TRIGGER_MECHANICS)) {
  const faux = new Set(FAUX_EDGE_ENABLERS[key] ?? []);
  const enablers = def.enablers.filter(inDeck).map((name) => {
    const r = resolveEnabler(name, cardIndex);
    if (!comboCardNames.has(name)) enablerNodeNames.add(name);
    return { name, image: r.image, labels: r.labels, chainsLeft: faux.has(name) };
  });
  mechanics[key] = {
    mechanic: def.mechanic,
    label: def.label,
    sublabel: def.sublabel,
    icon: def.icon,
    enablers,
  };
}

// AMPLIFIERS payload: mechanic key → resolved amplifier tiles (image/labels).
// Every amplifier must reference a DEFINED mechanic. Amplifiers that are already
// combo nodes render via their existing tile; a hypothetical non-combo amplifier
// still needs an enabler-only tile so Layer 0 can surface it.
const amplifiers = {};
for (const [key, names] of Object.entries(AMPLIFIERS)) {
  if (!mechanics[key]) throw new Error(`amplifier list references unknown mechanic ${key}`);
  amplifiers[key] = names.filter(inDeck).map((name) => {
    const r = resolveEnabler(name, cardIndex);
    if (!comboCardNames.has(name)) enablerNodeNames.add(name);
    return { name, image: r.image, labels: r.labels };
  });
}

// Sanity: every combo assigned a mechanic must reference a defined mechanic, and
// every non-null mechanic must exist in the verified table.
for (const [cid, m] of Object.entries(comboMechanic)) {
  if (m != null && !mechanics[m]) throw new Error(`combo ${cid} has unknown mechanic ${m}`);
}

// --- HYBRID per-card Layer-0 mechanic set (union of the two sources) -------- //
//
// layer0Mechanics[cardName] = CARD_OWN_TRIGGERS[card] ∪ { triggerMechanic of every
// combo the card is in }. Own-triggers come first (preserving their order), then
// combo-prereq mechanics not already present. The result is asserted EQUAL to the
// VERIFIED EXPECTED_LAYER0 so a drift in either source fails the build loudly.
const comboMechsByCard = new Map();
for (const c of combos) {
  if (!c.triggerMechanic) continue;
  for (const name of c.cards) {
    if (!comboMechsByCard.has(name)) comboMechsByCard.set(name, new Set());
    comboMechsByCard.get(name).add(c.triggerMechanic);
  }
}
const allCardNamesForL0 = new Set(
  [...Object.keys(CARD_OWN_TRIGGERS), ...comboMechsByCard.keys()].filter(inDeck),
);
const layer0Mechanics = {};
for (const name of allCardNamesForL0) {
  const own = CARD_OWN_TRIGGERS[name] ?? [];
  const merged = [...own];
  for (const m of comboMechsByCard.get(name) ?? []) {
    if (!merged.includes(m)) merged.push(m);
  }
  if (merged.length) layer0Mechanics[name] = merged;
}

// Guard: the derived union MUST match the verified mapping (as sets, per card).
const eqSet = (a, b) => a.length === b.length && new Set(a).size === new Set([...a, ...b]).size;
const derivedNames = new Set(Object.keys(layer0Mechanics));
const expectedNames = new Set(Object.keys(EXPECTED_LAYER0).filter(inDeck));
for (const name of new Set([...derivedNames, ...expectedNames])) {
  const d = layer0Mechanics[name] ?? [];
  const e = EXPECTED_LAYER0[name] ?? [];
  if (!eqSet(d, e)) {
    throw new Error(
      `layer0Mechanics for "${name}" diverged from verified EXPECTED_LAYER0: ` +
        `derived ${JSON.stringify(d)} vs expected ${JSON.stringify(e)}`,
    );
  }
  // Every mechanic referenced must be a defined mechanic.
  for (const m of d) if (!mechanics[m]) throw new Error(`card ${name} references unknown mechanic ${m}`);
}

const triggers = { mechanics, amplifiers, comboMechanic, layer0Mechanics, fauxEdge: FAUX_EDGE_ENABLERS };

// Trigger-mechanic nodes (kind 'trigger') — rendered as left-side capsules.
const triggerNodes = Object.values(mechanics).map((m) => ({
  id: `trigger:${m.mechanic}`,
  kind: 'trigger',
  mechanic: m.mechanic,
  label: m.label,
  sublabel: m.sublabel,
  icon: m.icon,
}));

// Enabler card tiles that are NOT already combo-participant nodes (full flood).
const enablerNodes = [...enablerNodeNames].sort().map((name) => {
  const info = cardIndex.get(name) ?? { image: null, labels: [] };
  return {
    id: `card:${name}`,
    kind: 'card',
    name,
    image: info.image,
    labels: info.labels,
    degree: 0,
    enablerOnly: true,
  };
});

const output = {
  generated_for: raw.deck ?? DECK,
  combo_data_available,
  outcomeTypes,
  nodes,
  edges,
  combos,
  triggers,
  triggerNodes,
  enablerNodes,
};

const outPath = join(DATA, 'combos.json');
writeFileSync(outPath, JSON.stringify(output, null, 2));

const cardNodes = nodes.filter((n) => n.kind === 'card').length;
const outcomeNodes = nodes.filter((n) => n.kind === 'outcome').length;
const threeCard = combos.filter((c) => c.cards.length === 3).length;
const triggeredCombos = combos.filter((c) => c.triggerMechanic).length;
console.log(
  `Wrote ${outPath}: combos ${combos.length}, cards ${cardNodes}, outcomes ${outcomeNodes}, ` +
    `edges ${edges.length}, 3-card ${threeCard}. ` +
    `triggers: ${Object.keys(mechanics).length} mechanics, ` +
    `${Object.values(amplifiers).reduce((s, a) => s + a.length, 0)} amplifier tags, ${triggeredCombos}/${combos.length} combos triggered, ` +
    `${triggerNodes.length} mechanic nodes, ${enablerNodes.length} enabler-only tiles, ` +
    `${Object.keys(layer0Mechanics).length} cards with a Layer-0 set.`,
);
