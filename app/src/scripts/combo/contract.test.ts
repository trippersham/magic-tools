import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { OUTCOME_TYPES } from './outcomes';

const __dirname = dirname(fileURLToPath(import.meta.url));
const combosPath = join(__dirname, '..', '..', 'data', 'combos.json');
const data = JSON.parse(readFileSync(combosPath, 'utf8'));

describe('combos.json contract', () => {
  it('declares the top-level shape', () => {
    expect(typeof data.generated_for).toBe('string');
    expect(typeof data.combo_data_available).toBe('boolean');
    expect(Array.isArray(data.outcomeTypes)).toBe(true);
    expect(Array.isArray(data.nodes)).toBe(true);
    expect(Array.isArray(data.edges)).toBe(true);
    expect(Array.isArray(data.combos)).toBe(true);
  });

  it('every outcomeTypes entry is a known canonical type with id/label/icon/win', () => {
    for (const t of data.outcomeTypes) {
      expect(OUTCOME_TYPES[t.id]).toBeDefined();
      expect(t.label).toBeTruthy();
      expect(t.icon).toBeTruthy();
      expect(typeof t.win).toBe('boolean');
    }
  });

  it('every node kind is card or outcome', () => {
    for (const n of data.nodes) {
      expect(['card', 'outcome']).toContain(n.kind);
    }
  });

  it('every edge source/target resolves to a node id', () => {
    const ids = new Set(data.nodes.map((n: { id: string }) => n.id));
    for (const e of data.edges) {
      expect(ids.has(e.source)).toBe(true);
      expect(ids.has(e.target)).toBe(true);
    }
  });

  it('every node outcomeType exists in outcomeTypes[]', () => {
    const declared = new Set(data.outcomeTypes.map((t: { id: string }) => t.id));
    for (const n of data.nodes) {
      if (n.kind === 'outcome') {
        expect(n.outcomeType).toBeTruthy();
        expect(declared.has(n.outcomeType)).toBe(true);
      }
    }
  });

  it('every combos[].outcomeTypes id exists in outcomeTypes[]', () => {
    const declared = new Set(data.outcomeTypes.map((t: { id: string }) => t.id));
    for (const c of data.combos) {
      for (const id of c.outcomeTypes) {
        expect(declared.has(id)).toBe(true);
      }
    }
  });

  it('each card node degree equals the number of combos it participates in', () => {
    const counts = new Map<string, number>();
    for (const c of data.combos) {
      for (const name of c.cards) {
        counts.set(name, (counts.get(name) ?? 0) + 1);
      }
    }
    const cardNodes = data.nodes.filter((n: { kind: string }) => n.kind === 'card');
    for (const n of cardNodes) {
      expect(n.degree).toBe(counts.get(n.name));
    }
    // graph coverage: a card node exists for every card that appears in a combo
    expect(cardNodes.length).toBe(counts.size);
  });

  it('outcome node ids and card node ids follow the id scheme', () => {
    for (const n of data.nodes) {
      if (n.kind === 'card') expect(n.id).toBe(`card:${n.name}`);
      if (n.kind === 'outcome') expect(n.id).toBe(`outcome:${n.outcomeType}`);
    }
  });

  it('every combo references cards that exist as card nodes', () => {
    const cardIds = new Set(
      data.nodes.filter((n: { kind: string }) => n.kind === 'card').map((n: { id: string }) => n.id),
    );
    for (const c of data.combos) {
      for (const name of c.cards) {
        expect(cardIds.has(`card:${name}`)).toBe(true);
      }
    }
  });

  describe('hybrid trigger Layer-0 contract', () => {
    it('declares all 5 trigger mechanics with labels, icons, and resolvable enablers', () => {
      const m = data.triggers.mechanics;
      expect(Object.keys(m).sort()).toEqual([
        'creature enters',
        'draw a card',
        'gain life',
        'opponent draws',
        'opponent loses life',
      ]);
      // Assert the durable CONTRACT on the shipped data, not a per-deck snapshot:
      // every declared mechanic has a label + icon and at least one enabler, and
      // every enabler resolves to real imagery (the loud-MISS guard — a broken
      // regen or missing image would leave a null and fail here). Exact per-deck
      // counts/shapes are covered deterministically by the fixture-deck pipeline
      // golden test, so this check never breaks on deck tuning.
      for (const [, def] of Object.entries<any>(m)) {
        expect(def.label).toBeTruthy();
        expect(def.icon).toBeTruthy();
        expect(def.enablers.length).toBeGreaterThan(0);
        for (const en of def.enablers) expect(en.image).toBeTruthy();
      }
    });

    it('cast a spell is deliberately NOT modeled (out of scope — would flood Layer 0)', () => {
      expect(data.triggers.mechanics['cast a spell']).toBeUndefined();
      for (const mechs of Object.values<string[]>(data.triggers.layer0Mechanics)) {
        expect(mechs).not.toContain('cast a spell');
      }
    });

    it('models amplifiers as a first-class role, resolved to imagery, referencing defined mechanics', () => {
      const amp = data.triggers.amplifiers;
      expect(amp).toBeDefined();
      // Verified amplifier lists (trigger-roles-v2.json).
      expect(amp['draw a card'].map((a: any) => a.name)).toEqual([
        "Alhammarret's Archive",
      ]);
      expect(amp['gain life'].map((a: any) => a.name)).toEqual(["Alhammarret's Archive"]);
      for (const [mech, list] of Object.entries<any[]>(amp)) {
        expect(data.triggers.mechanics[mech]).toBeDefined();
        for (const a of list) expect(a.image).toBeTruthy();
      }
    });

    it('amplifiers remain CLI-detected combo nodes (roles ADD, never SUBTRACT membership)', () => {
      const cardNames = new Set(
        data.nodes.filter((n: any) => n.kind === 'card').map((n: any) => n.name),
      );
      // The draw-doubler stays a combo node …
      expect(cardNames.has("Alhammarret's Archive")).toBe(true);
      // … and its CLI combo with Peer is intact.
      expect(
        data.combos.some(
          (c: any) => c.id === '1561-4435' && c.cards.includes("Alhammarret's Archive"),
        ),
      ).toBe(true);
    });

    it("Consecrated Sphinx's own opponent-draws trigger gives it a Layer 0 (degree-0 own-trigger)", () => {
      expect(new Set(data.triggers.layer0Mechanics['Consecrated Sphinx'])).toEqual(
        new Set(['opponent draws']),
      );
    });

    it('layer0Mechanics is the authoritative per-card union (spot-checks) with defined mechanics', () => {
      const l0 = data.triggers.layer0Mechanics;
      const asSet = (a: string[]) => new Set(a);
      expect(asSet(l0['Psychosis Crawler'])).toEqual(asSet(['draw a card']));
      expect(asSet(l0['Orcish Bowmasters'])).toEqual(asSet(['opponent draws']));
      expect(asSet(l0['Exquisite Blood'])).toEqual(asSet(['opponent loses life', 'gain life']));
      expect(asSet(l0['Sheoldred, the Apocalypse'])).toEqual(
        asSet(['draw a card', 'opponent draws']),
      );
      expect(asSet(l0['Queza, Augur of Agonies'])).toEqual(asSet(['draw a card']));
      // Peer into the Abyss has no own trigger and only null-trigger combos → absent.
      expect(l0['Peer into the Abyss']).toBeUndefined();
      // Every referenced mechanic is a defined mechanic.
      for (const mechs of Object.values<string[]>(l0)) {
        for (const mech of mechs) expect(data.triggers.mechanics[mech]).toBeDefined();
      }
    });
  });
});
