/**
 * Golden pipeline test for scripts/prep-combos.mjs.
 *
 * Runs the real script over a FROZEN fixture deck and asserts exact
 * counts/shapes on the output. The fixture deck never changes, so every
 * assertion is deterministic and hand-computed.
 *
 * Env hooks used:
 *   DECK_COMBOS_JSON  — fixture deck-combos projection (skips the CLI)
 *   PREP_DATA_DIR     — fixture dir for deck.json + enriched.json
 *   PREP_OUT          — temp output path (never clobbers src/data/combos.json)
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync, unlinkSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';

const __dirname = dirname(fileURLToPath(import.meta.url));
const APP_ROOT = join(__dirname, '..', '..', '..');
const FIXTURE_DIR = join(__dirname, '__fixtures__', 'pipeline');
const DECK_COMBOS = join(FIXTURE_DIR, 'deck-combos.json');
const REAL_COMBOS = join(APP_ROOT, 'src', 'data', 'combos.json');

// Temp output — deterministic name so concurrent runs don't collide.
const OUT_PATH = join(tmpdir(), `prep-combos-golden-${process.pid}.json`);

let data: any;
let realCombosMtimeBefore: number;

beforeAll(() => {
  // Snapshot the real combos.json mtime so we can verify it was NOT touched.
  realCombosMtimeBefore = statSync(REAL_COMBOS).mtimeMs;

  execFileSync(
    process.execPath,
    ['scripts/prep-combos.mjs'],
    {
      cwd: APP_ROOT,
      encoding: 'utf8',
      timeout: 30_000,
      env: {
        ...process.env,
        DECK_COMBOS_JSON: DECK_COMBOS,
        PREP_DATA_DIR: FIXTURE_DIR,
        PREP_OUT: OUT_PATH,
        __PREP_COMBOS_REEXEC: '1', // skip the self-re-exec dance
      },
    },
  );

  data = JSON.parse(readFileSync(OUT_PATH, 'utf8'));
});

afterAll(() => {
  try { unlinkSync(OUT_PATH); } catch { /* best-effort cleanup */ }
});

// ---------------------------------------------------------------------------
// Structural contract
// ---------------------------------------------------------------------------

describe('golden pipeline – structural contract', () => {
  it('did NOT touch the real combos.json', () => {
    expect(statSync(REAL_COMBOS).mtimeMs).toBe(realCombosMtimeBefore);
  });

  it('top-level shape', () => {
    expect(data.generated_for).toBe('Golden Fixture Deck');
    expect(data.combo_data_available).toBe(true);
    expect(Array.isArray(data.outcomeTypes)).toBe(true);
    expect(Array.isArray(data.nodes)).toBe(true);
    expect(Array.isArray(data.edges)).toBe(true);
    expect(Array.isArray(data.combos)).toBe(true);
    expect(data.triggers).toBeDefined();
    expect(Array.isArray(data.triggerNodes)).toBe(true);
    expect(Array.isArray(data.enablerNodes)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Exact combo counts + shapes
// ---------------------------------------------------------------------------

describe('golden pipeline – combos', () => {
  it('produces exactly 3 combos', () => {
    expect(data.combos).toHaveLength(3);
  });

  it('combo 1 (Sanguine Bond + Exquisite Blood) has correct transform', () => {
    const c = data.combos.find((c: any) => c.id === 'test-combo-1');
    expect(c).toBeDefined();
    expect(c.cards).toEqual(['Sanguine Bond', 'Exquisite Blood']);
    expect(c.triggerMechanic).toBe('gain life');
    expect(c.outcomeTypes).toEqual(['drain', 'lifegain']);
    expect(c.prerequisites).toEqual(['You have a way to gain life.']);
    expect(c.spellbookUrl).toBe('https://commanderspellbook.com/combo/test-combo-1/');
  });

  it('combo 2 (Vito + Exquisite Blood) has correct transform', () => {
    const c = data.combos.find((c: any) => c.id === 'test-combo-2');
    expect(c).toBeDefined();
    expect(c.cards).toEqual(['Vito, Thorn of the Dusk Rose', 'Exquisite Blood']);
    expect(c.triggerMechanic).toBe('gain life');
    expect(c.outcomeTypes).toEqual(['drain']);
  });

  it('combo 3 (Archive + Peer) has null triggerMechanic (no matching prereq)', () => {
    const c = data.combos.find((c: any) => c.id === 'test-combo-3');
    expect(c).toBeDefined();
    expect(c.cards).toEqual(["Alhammarret's Archive", 'Peer into the Abyss']);
    expect(c.triggerMechanic).toBeNull();
    expect(c.outcomeTypes).toEqual(['draw']);
  });
});

// ---------------------------------------------------------------------------
// Outcome types
// ---------------------------------------------------------------------------

describe('golden pipeline – outcome types', () => {
  it('uses exactly drain, lifegain, draw (in canonical registry order)', () => {
    // Registry order: drain, draw, lifegain (per OUTCOME_TYPES key order).
    // But the code filters by usedOutcomeIds in Object.values order.
    const ids = data.outcomeTypes.map((t: any) => t.id);
    expect(new Set(ids)).toEqual(new Set(['drain', 'draw', 'lifegain']));
    expect(ids).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------
// Nodes + edges
// ---------------------------------------------------------------------------

describe('golden pipeline – nodes & edges', () => {
  it('has exactly 5 card nodes (including out-of-deck Peer)', () => {
    const cardNodes = data.nodes.filter((n: any) => n.kind === 'card');
    expect(cardNodes).toHaveLength(5);
    const names = new Set(cardNodes.map((n: any) => n.name));
    expect(names).toEqual(new Set([
      'Sanguine Bond',
      'Exquisite Blood',
      'Vito, Thorn of the Dusk Rose',
      "Alhammarret's Archive",
      'Peer into the Abyss',
    ]));
  });

  it('card degrees are exact', () => {
    const byName = Object.fromEntries(
      data.nodes.filter((n: any) => n.kind === 'card').map((n: any) => [n.name, n.degree]),
    );
    expect(byName['Sanguine Bond']).toBe(1);
    expect(byName['Exquisite Blood']).toBe(2);
    expect(byName['Vito, Thorn of the Dusk Rose']).toBe(1);
    expect(byName["Alhammarret's Archive"]).toBe(1);
    expect(byName['Peer into the Abyss']).toBe(1);
  });

  it('has exactly 3 outcome nodes', () => {
    const outcomeNodes = data.nodes.filter((n: any) => n.kind === 'outcome');
    expect(outcomeNodes).toHaveLength(3);
  });

  it('has exactly 11 edges (3 combo + 8 produces)', () => {
    expect(data.edges).toHaveLength(11);
    const combo = data.edges.filter((e: any) => e.kind === 'combo');
    const produces = data.edges.filter((e: any) => e.kind === 'produces');
    expect(combo).toHaveLength(3);
    expect(produces).toHaveLength(8);
  });

  it('card images resolve from enriched.json fixture (no facade)', () => {
    for (const n of data.nodes.filter((n: any) => n.kind === 'card')) {
      expect(n.image).toMatch(/^https:\/\/fixture\.test\//);
    }
  });
});

// ---------------------------------------------------------------------------
// inDeck filter
// ---------------------------------------------------------------------------

describe('golden pipeline – inDeck filter', () => {
  it('Peer into the Abyss is a combo card node but NOT in any mechanic enabler list', () => {
    // Peer IS a node (degree 1 from combo 3).
    const peerNode = data.nodes.find((n: any) => n.name === 'Peer into the Abyss');
    expect(peerNode).toBeDefined();
    expect(peerNode.degree).toBe(1);

    // But Peer is NOT in deck.json, so it must be filtered from all mechanic enabler lists.
    for (const [, def] of Object.entries<any>(data.triggers.mechanics)) {
      const enablerNames = def.enablers.map((e: any) => e.name);
      expect(enablerNames).not.toContain('Peer into the Abyss');
    }
  });

  it('Peer into the Abyss is NOT in layer0Mechanics (not inDeck)', () => {
    expect(data.triggers.layer0Mechanics['Peer into the Abyss']).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Trigger mechanics – exact enabler counts
// ---------------------------------------------------------------------------

describe('golden pipeline – mechanic enabler counts', () => {
  it("'draw a card' has 0 enablers (no deck cards in its enabler table)", () => {
    expect(data.triggers.mechanics['draw a card'].enablers).toHaveLength(0);
  });

  it("'gain life' has exactly 5 enablers", () => {
    const names = data.triggers.mechanics['gain life'].enablers.map((e: any) => e.name);
    expect(names).toHaveLength(5);
    expect(new Set(names)).toEqual(new Set([
      'Children of Korlis',
      'Exquisite Blood',
      'Queza, Augur of Agonies',
      'Soul Warden',
      'Vito, Thorn of the Dusk Rose',
    ]));
  });

  it("'opponent draws' has 0 enablers", () => {
    expect(data.triggers.mechanics['opponent draws'].enablers).toHaveLength(0);
  });

  it("'opponent loses life' has exactly 3 enablers", () => {
    const names = data.triggers.mechanics['opponent loses life'].enablers.map((e: any) => e.name);
    expect(names).toHaveLength(3);
    expect(new Set(names)).toEqual(new Set([
      'Queza, Augur of Agonies',
      'Sanguine Bond',
      'Vito, Thorn of the Dusk Rose',
    ]));
  });

  it("'creature enters' has exactly 4 enablers", () => {
    const names = data.triggers.mechanics['creature enters'].enablers.map((e: any) => e.name);
    expect(names).toHaveLength(4);
    expect(new Set(names)).toEqual(new Set([
      'Children of Korlis',
      'Queza, Augur of Agonies',
      'Soul Warden',
      'Vito, Thorn of the Dusk Rose',
    ]));
  });

  it('all enabler images resolve from fixture (no null = no facade)', () => {
    for (const def of Object.values<any>(data.triggers.mechanics)) {
      for (const e of def.enablers) {
        expect(e.image).toMatch(/^https:\/\/fixture\.test\//);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// Amplifiers
// ---------------------------------------------------------------------------

describe('golden pipeline – amplifiers', () => {
  it("'draw a card' amplifier is Alhammarret's Archive with resolved image", () => {
    const amp = data.triggers.amplifiers['draw a card'];
    expect(amp).toHaveLength(1);
    expect(amp[0].name).toBe("Alhammarret's Archive");
    expect(amp[0].image).toBe('https://fixture.test/archive.jpg');
  });

  it("'gain life' amplifier is Alhammarret's Archive", () => {
    const amp = data.triggers.amplifiers['gain life'];
    expect(amp).toHaveLength(1);
    expect(amp[0].name).toBe("Alhammarret's Archive");
  });
});

// ---------------------------------------------------------------------------
// Layer-0 mechanics (hybrid union)
// ---------------------------------------------------------------------------

describe('golden pipeline – layer0Mechanics', () => {
  it('Soul Warden (degree-0, own-trigger only) has layer0 = [creature enters]', () => {
    expect(data.triggers.layer0Mechanics['Soul Warden']).toEqual(['creature enters']);
  });

  it('Exquisite Blood union = own [opponent loses life] + combo {gain life}', () => {
    expect(new Set(data.triggers.layer0Mechanics['Exquisite Blood'])).toEqual(
      new Set(['opponent loses life', 'gain life']),
    );
  });

  it('Queza has layer0 = [draw a card] (own-trigger only, not in any combo)', () => {
    expect(data.triggers.layer0Mechanics['Queza, Augur of Agonies']).toEqual(['draw a card']);
  });

  it('Sanguine Bond has layer0 = [gain life]', () => {
    expect(data.triggers.layer0Mechanics['Sanguine Bond']).toEqual(['gain life']);
  });

  it('Vito has layer0 = [gain life]', () => {
    expect(data.triggers.layer0Mechanics['Vito, Thorn of the Dusk Rose']).toEqual(['gain life']);
  });

  it('exactly 5 cards have layer0Mechanics', () => {
    expect(Object.keys(data.triggers.layer0Mechanics)).toHaveLength(5);
  });
});

// ---------------------------------------------------------------------------
// Enabler-only nodes (degree-0 cards that are enablers but not combo participants)
// ---------------------------------------------------------------------------

describe('golden pipeline – enabler-only nodes', () => {
  it('exactly 3 enabler-only nodes', () => {
    expect(data.enablerNodes).toHaveLength(3);
  });

  it('enabler-only nodes are Children, Queza, Soul Warden (sorted)', () => {
    const names = data.enablerNodes.map((n: any) => n.name);
    expect(names).toEqual([
      'Children of Korlis',
      'Queza, Augur of Agonies',
      'Soul Warden',
    ]);
  });

  it('all enabler-only nodes have degree 0 and enablerOnly flag', () => {
    for (const n of data.enablerNodes) {
      expect(n.degree).toBe(0);
      expect(n.enablerOnly).toBe(true);
      expect(n.kind).toBe('card');
    }
  });
});

// ---------------------------------------------------------------------------
// Trigger nodes (mechanic capsules)
// ---------------------------------------------------------------------------

describe('golden pipeline – trigger nodes', () => {
  it('has exactly 5 trigger nodes (one per mechanic)', () => {
    expect(data.triggerNodes).toHaveLength(5);
    const ids = new Set(data.triggerNodes.map((n: any) => n.id));
    expect(ids).toEqual(new Set([
      'trigger:draw a card',
      'trigger:gain life',
      'trigger:opponent draws',
      'trigger:opponent loses life',
      'trigger:creature enters',
    ]));
  });
});

// ---------------------------------------------------------------------------
// chainsLeft (faux-edge flag on enablers that are also combo participants)
// ---------------------------------------------------------------------------

describe('golden pipeline – chainsLeft flag', () => {
  it('gain life: Exquisite Blood and Vito chain left (combo participants)', () => {
    const enablers = data.triggers.mechanics['gain life'].enablers;
    const byName = Object.fromEntries(enablers.map((e: any) => [e.name, e.chainsLeft]));
    expect(byName['Exquisite Blood']).toBe(true);
    expect(byName['Vito, Thorn of the Dusk Rose']).toBe(true);
    expect(byName['Children of Korlis']).toBe(false);
    expect(byName['Soul Warden']).toBe(false);
    expect(byName['Queza, Augur of Agonies']).toBe(false);
  });
});
