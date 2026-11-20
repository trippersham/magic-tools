import { describe, it, expect } from 'vitest';
import { buildFocusDag, computeFocusLayout, type FocusGraph } from './layers';
import combos from '../../data/combos.json';

// The real deck graph (nodes/edges carry `comboId` on each edge).
const graph = combos as unknown as FocusGraph;

const EXQUISITE = 'card:Exquisite Blood';
const PEER = 'card:Peer into the Abyss';
const TOP = "card:Sensei's Divining Top";
const SANGUINE = 'card:Sanguine Bond';

// A minimal synthetic 2-card combo (clicked → partner → outcome).
const twoCard: FocusGraph = {
  nodes: [
    { id: 'card:A', kind: 'card' },
    { id: 'card:B', kind: 'card' },
    { id: 'outcome:drain', kind: 'outcome' },
  ],
  edges: [
    { source: 'card:A', target: 'card:B', kind: 'combo', comboId: 'x' },
    { source: 'card:A', target: 'outcome:drain', kind: 'produces', comboId: 'x' },
    { source: 'card:B', target: 'outcome:drain', kind: 'produces', comboId: 'x' },
  ],
};

// A multi-combo fixture mirroring the Sheoldred shape: a root sharing three
// 2-card combos (partners P1/P2/M) AND one 3-card combo (R, M, T as a transitive
// triangle R→M, R→T, M→T). Card ranks must come from combo-chain depth ONLY, so
// all direct partners (P1, P2, M) pack onto rank 1 and only the 3-card terminal T
// (reached via R→M→T) sits alone on rank 2; outcomes occupy the single max rank.
const multiCombo: FocusGraph = {
  nodes: [
    { id: 'card:R', kind: 'card' },
    { id: 'card:P1', kind: 'card' },
    { id: 'card:P2', kind: 'card' },
    { id: 'card:M', kind: 'card' },
    { id: 'card:T', kind: 'card' },
    { id: 'outcome:drain', kind: 'outcome' },
    { id: 'outcome:draw', kind: 'outcome' },
  ],
  edges: [
    { source: 'card:R', target: 'card:P1', kind: 'combo', comboId: 'c1' },
    { source: 'card:P1', target: 'outcome:drain', kind: 'produces', comboId: 'c1' },
    { source: 'card:R', target: 'card:P2', kind: 'combo', comboId: 'c2' },
    { source: 'card:P2', target: 'outcome:draw', kind: 'produces', comboId: 'c2' },
    // 3-card combo as a transitive triangle; M is a direct partner, T the terminal.
    { source: 'card:R', target: 'card:M', kind: 'combo', comboId: 'c3' },
    { source: 'card:R', target: 'card:T', kind: 'combo', comboId: 'c3' },
    { source: 'card:M', target: 'card:T', kind: 'combo', comboId: 'c3' },
    { source: 'card:T', target: 'outcome:drain', kind: 'produces', comboId: 'c3' },
  ],
};

// ── Synthetic fixtures for DECOUPLED layout assertions ──────────────────────
// These reproduce the exact graph shapes the old real-data tests checked, so
// future deck tuning never turns the suite red.

/** Hub with 3 combo partners + a gain-life trigger Layer 0 (2 enablers). */
const hubWithL0: FocusGraph = {
  nodes: [
    { id: 'card:Hub', kind: 'card' },
    { id: 'card:P1', kind: 'card' },
    { id: 'card:P2', kind: 'card' },
    { id: 'card:P3', kind: 'card' },
    { id: 'card:E1', kind: 'card' },
    { id: 'card:E2', kind: 'card' },
    { id: 'outcome:drain', kind: 'outcome' },
  ],
  edges: [
    { source: 'card:Hub', target: 'card:P1', kind: 'combo', comboId: 'c1' },
    { source: 'card:P1', target: 'outcome:drain', kind: 'produces', comboId: 'c1' },
    { source: 'card:Hub', target: 'card:P2', kind: 'combo', comboId: 'c2' },
    { source: 'card:P2', target: 'outcome:drain', kind: 'produces', comboId: 'c2' },
    { source: 'card:Hub', target: 'card:P3', kind: 'combo', comboId: 'c3' },
    { source: 'card:P3', target: 'outcome:drain', kind: 'produces', comboId: 'c3' },
  ],
  triggers: {
    mechanics: {
      'gain life': {
        mechanic: 'gain life',
        label: 'Gain Life',
        icon: '❤️',
        enablers: [{ name: 'E1' }, { name: 'E2' }],
      },
    },
    comboMechanic: { c1: 'gain life', c2: 'gain life', c3: 'gain life' },
    layer0Mechanics: { Hub: ['gain life'] },
  },
};

/** Root with 1 combo partner, draw-trigger L0 with 3 enablers + 2 amplifiers.
 *  Partner is also listed as an enabler → must be deduped from L0 flood. */
const drawFloodWithAmps: FocusGraph = {
  nodes: [
    { id: 'card:Root', kind: 'card' },
    { id: 'card:Partner', kind: 'card' },
    { id: 'card:Enabler1', kind: 'card' },
    { id: 'card:Enabler2', kind: 'card' },
    { id: 'card:Enabler3', kind: 'card' },
    { id: 'card:Amp1', kind: 'card' },
    { id: 'card:Amp2', kind: 'card' },
    { id: 'outcome:draw', kind: 'outcome' },
  ],
  edges: [
    { source: 'card:Root', target: 'card:Partner', kind: 'combo', comboId: 'c1' },
    { source: 'card:Partner', target: 'outcome:draw', kind: 'produces', comboId: 'c1' },
  ],
  triggers: {
    mechanics: {
      'draw a card': {
        mechanic: 'draw a card',
        label: 'Draw',
        icon: '📜',
        enablers: [
          { name: 'Partner' }, // deduped: already in DAG as combo partner
          { name: 'Enabler1' },
          { name: 'Enabler2' },
          { name: 'Enabler3' },
        ],
      },
    },
    comboMechanic: { c1: 'draw a card' },
    layer0Mechanics: { Root: ['draw a card'] },
    amplifiers: {
      'draw a card': [{ name: 'Amp1' }, { name: 'Amp2' }],
    },
  },
};

/** Hub with 4 partners, all null-trigger combos → no Layer 0. */
const hubNoTrigger: FocusGraph = {
  nodes: [
    { id: 'card:Hub', kind: 'card' },
    { id: 'card:P1', kind: 'card' },
    { id: 'card:P2', kind: 'card' },
    { id: 'card:P3', kind: 'card' },
    { id: 'card:P4', kind: 'card' },
    { id: 'outcome:drain', kind: 'outcome' },
  ],
  edges: [
    { source: 'card:Hub', target: 'card:P1', kind: 'combo', comboId: 'c1' },
    { source: 'card:P1', target: 'outcome:drain', kind: 'produces', comboId: 'c1' },
    { source: 'card:Hub', target: 'card:P2', kind: 'combo', comboId: 'c2' },
    { source: 'card:P2', target: 'outcome:drain', kind: 'produces', comboId: 'c2' },
    { source: 'card:Hub', target: 'card:P3', kind: 'combo', comboId: 'c3' },
    { source: 'card:P3', target: 'outcome:drain', kind: 'produces', comboId: 'c3' },
    { source: 'card:Hub', target: 'card:P4', kind: 'combo', comboId: 'c4' },
    { source: 'card:P4', target: 'outcome:drain', kind: 'produces', comboId: 'c4' },
  ],
  triggers: {
    mechanics: {},
    comboMechanic: { c1: null, c2: null, c3: null, c4: null },
    layer0Mechanics: {},
  },
};

/** Root with dual L0 triggers (draw + opponent-draws), 2 direct partners + a
 *  3-card combo that pushes a Deep card one rank further. */
const chainWithDualTrigger: FocusGraph = {
  nodes: [
    { id: 'card:Root', kind: 'card' },
    { id: 'card:P1', kind: 'card' },
    { id: 'card:P2', kind: 'card' },
    { id: 'card:P3', kind: 'card' },
    { id: 'card:Deep', kind: 'card' },
    { id: 'card:DrawEn', kind: 'card' },
    { id: 'card:OppEn', kind: 'card' },
    { id: 'outcome:drain', kind: 'outcome' },
    { id: 'outcome:draw', kind: 'outcome' },
  ],
  edges: [
    { source: 'card:Root', target: 'card:P1', kind: 'combo', comboId: 'c1' },
    { source: 'card:P1', target: 'outcome:drain', kind: 'produces', comboId: 'c1' },
    { source: 'card:Root', target: 'card:P2', kind: 'combo', comboId: 'c2' },
    { source: 'card:P2', target: 'outcome:draw', kind: 'produces', comboId: 'c2' },
    // 3-card combo: Root → P3, Root → Deep, P3 → Deep (transitive triangle)
    { source: 'card:Root', target: 'card:P3', kind: 'combo', comboId: 'c3' },
    { source: 'card:Root', target: 'card:Deep', kind: 'combo', comboId: 'c3' },
    { source: 'card:P3', target: 'card:Deep', kind: 'combo', comboId: 'c3' },
    { source: 'card:Deep', target: 'outcome:drain', kind: 'produces', comboId: 'c3' },
  ],
  triggers: {
    mechanics: {
      'draw a card': {
        mechanic: 'draw a card',
        label: 'Draw',
        icon: '📜',
        enablers: [{ name: 'DrawEn' }],
      },
      'opponent draws': {
        mechanic: 'opponent draws',
        label: 'Opponent Draws',
        icon: '👁️',
        enablers: [{ name: 'OppEn' }],
      },
    },
    comboMechanic: { c1: 'draw a card', c2: 'draw a card', c3: null },
    layer0Mechanics: { Root: ['draw a card', 'opponent draws'] },
  },
};

/** Root with 1 combo partner that is ALSO an enabler → must be deduped out of
 *  L0. Two other enablers flood Layer 0 normally. */
const rootWithPartnerDedup: FocusGraph = {
  nodes: [
    { id: 'card:Root', kind: 'card' },
    { id: 'card:Partner', kind: 'card' },
    { id: 'card:E1', kind: 'card' },
    { id: 'card:E2', kind: 'card' },
    { id: 'outcome:damage', kind: 'outcome' },
  ],
  edges: [
    { source: 'card:Root', target: 'card:Partner', kind: 'combo', comboId: 'c1' },
    { source: 'card:Partner', target: 'outcome:damage', kind: 'produces', comboId: 'c1' },
  ],
  triggers: {
    mechanics: {
      'opponent draws': {
        mechanic: 'opponent draws',
        label: 'Opponent Draws',
        icon: '👁️',
        enablers: [{ name: 'Partner' }, { name: 'E1' }, { name: 'E2' }],
      },
    },
    comboMechanic: { c1: 'opponent draws' },
    layer0Mechanics: { Root: ['opponent draws'] },
  },
};

/** Degree-0 card (no combos) with own-trigger L0 — tests that the root renders
 *  even with zero combos and that its own-trigger floods Layer 0. */
const degreeZeroOwn: FocusGraph = {
  nodes: [
    { id: 'card:Lonely', kind: 'card' },
    { id: 'card:CreatureA', kind: 'card' },
    { id: 'card:CreatureB', kind: 'card' },
  ],
  edges: [], // no combos at all
  triggers: {
    mechanics: {
      'creature enters': {
        mechanic: 'creature enters',
        label: 'Creature ETB',
        icon: '🐉',
        enablers: [{ name: 'CreatureA' }, { name: 'CreatureB' }],
      },
    },
    comboMechanic: {},
    layer0Mechanics: { Lonely: ['creature enters'] },
  },
};

describe('buildFocusDag — card-centric connected component', () => {
  it('2-card combo: clicked→partner→outcome, oriented away from the root', () => {
    const dag = buildFocusDag('card:A', twoCard);
    expect(new Set(dag.nodeIds)).toEqual(new Set(['card:A', 'card:B', 'outcome:drain']));
    // combo edge oriented root→partner
    expect(dag.edges).toContainEqual(
      expect.objectContaining({ source: 'card:A', target: 'card:B', kind: 'combo' }),
    );
    // outcome fed from the deepest (partner) card, never from the root directly
    expect(dag.edges).toContainEqual(
      expect.objectContaining({ source: 'card:B', target: 'outcome:drain', kind: 'produces' }),
    );
    expect(
      dag.edges.some((e) => e.source === 'card:A' && e.target === 'outcome:drain'),
    ).toBe(false);
  });

  it('every synthesized DAG edge carries its own comboId (combo AND produces)', () => {
    const dag = buildFocusDag('card:A', twoCard);
    for (const e of dag.edges) expect(e.comboId).toBe('x');
    // produces edges specifically are self-describing (regression: they used to drop it)
    const prod = dag.edges.filter((e) => e.kind === 'produces');
    expect(prod.length).toBeGreaterThan(0);
    for (const e of prod) expect(e.comboId).toBe('x');
  });

  it('only pulls in combos the CLICKED card belongs to (card-centric union)', () => {
    // Clicking Peer must NOT drag in Sheoldred's other (non-Peer) combos.
    const dag = buildFocusDag(PEER, graph);
    expect(dag.nodeIds).toContain(PEER);
    // Lich's Mastery only shares combos with Sheoldred/Queza, never with Peer.
    expect(dag.nodeIds).not.toContain("card:Lich's Mastery");
  });

  describe('produces-edge comboId is a FOCUS combo (never a global-lookup mismatch)', () => {
    const SHEOLDRED = 'card:Sheoldred, the Apocalypse';
    const DROGSKOL = 'card:Drogskol Reaver';

    // Combos the ROOT participates in, with their card membership (from the raw graph).
    function focusMembership(root: string): Map<string, Set<string>> {
      const focused = new Set<string>();
      for (const e of graph.edges) {
        if (e.comboId && (e.source === root || e.target === root)) focused.add(e.comboId);
      }
      const members = new Map<string, Set<string>>();
      for (const e of graph.edges) {
        if (!e.comboId || !focused.has(e.comboId)) continue;
        for (const ep of [e.source, e.target]) {
          if (ep.startsWith('card:')) {
            if (!members.has(e.comboId)) members.set(e.comboId, new Set());
            members.get(e.comboId)!.add(ep);
          }
        }
      }
      return members;
    }

    it('Drogskol→outcome under Sheoldred resolves to the Sheoldred combo (444-2498), NOT 444-1786', () => {
      const dag = buildFocusDag(SHEOLDRED, graph);
      const drogProduces = dag.edges.filter(
        (e) => e.kind === 'produces' && e.source === DROGSKOL,
      );
      // (Drogskol is terminal in the Sheoldred combo 444-2498, feeding draw + lifegain.)
      expect(drogProduces.length).toBeGreaterThan(0);
      for (const e of drogProduces) {
        // BEFORE the fix these edges dropped comboId and the global endpoint lookup
        // returned 444-1786 (Drogskol + Queza) — a combo NOT on the Sheoldred screen.
        expect(e.comboId).toBe('444-2498');
        expect(e.comboId).not.toBe('444-1786');
      }
    });

    it('EVERY produces edge under Sheoldred names a combo the root is in whose source card is a member', () => {
      const members = focusMembership(SHEOLDRED);
      const dag = buildFocusDag(SHEOLDRED, graph);
      const produces = dag.edges.filter((e) => e.kind === 'produces');
      expect(produces.length).toBeGreaterThan(0);
      for (const e of produces) {
        expect(e.comboId).toBeDefined();
        // (a) the root participates in that combo …
        expect(members.has(e.comboId!)).toBe(true);
        // (b) … and the tapped (source) card participates in it too.
        expect(members.get(e.comboId!)!.has(e.source)).toBe(true);
      }
    });

    it('EVERY combo edge under Sheoldred is self-describing with a focus comboId', () => {
      const members = focusMembership(SHEOLDRED);
      const dag = buildFocusDag(SHEOLDRED, graph);
      const combo = dag.edges.filter((e) => e.kind === 'combo');
      expect(combo.length).toBeGreaterThan(0);
      for (const e of combo) {
        expect(e.comboId).toBeDefined();
        expect(members.has(e.comboId!)).toBe(true);
        // both endpoints of a combo edge are member cards of that combo
        expect(members.get(e.comboId!)!.has(e.source)).toBe(true);
        expect(members.get(e.comboId!)!.has(e.target)).toBe(true);
      }
    });
  });
});

describe('computeFocusLayout — analytic longest-path LR ranks', () => {
  const opts = { nodeWidth: 96, nodeHeight: 132, ranksep: 120, nodesep: 24 };

  it('2-card combo: clicked rank 0, partner rank 1, outcome terminal rank 2', () => {
    const { ranks, layers } = computeFocusLayout('card:A', twoCard, opts);
    expect(ranks['card:A'].rank).toBe(0);
    expect(ranks['card:B'].rank).toBe(1);
    expect(ranks['outcome:drain'].rank).toBe(2);
    expect(layers.length).toBe(3);
    expect(layers[0]).toEqual(['card:A']);
  });

  it('hub with gain-life Layer 0: clicked at rank 1, N partners at rank 2, outcomes terminal', () => {
    const { ranks, layers } = computeFocusLayout('card:Hub', hubWithL0, opts);
    const maxRank = Math.max(...Object.values(ranks).map((r) => r.rank));
    // Layer 0 (rank 0) is the gain-life trigger: the mechanic node + gain enablers.
    expect(layers[0]).toContain('trigger:gain life');
    const rank0Cards = layers[0].filter((id) => id.startsWith('card:'));
    expect(rank0Cards.length).toBe(2); // E1, E2
    expect(new Set(rank0Cards)).toEqual(new Set(['card:E1', 'card:E2']));
    // clicked card is now the unique rank-1 node (Layer 1)
    const rank1 = Object.entries(ranks).filter(([, r]) => r.rank === 1).map(([id]) => id);
    expect(rank1).toEqual(['card:Hub']);
    // 3 partner cards at rank 2 (shifted right by the Layer 0)
    const rank2Cards = Object.entries(ranks)
      .filter(([id, r]) => r.rank === 2 && id.startsWith('card:'))
      .map(([id]) => id);
    expect(rank2Cards.length).toBe(3);
    expect(new Set(rank2Cards)).toEqual(new Set(['card:P1', 'card:P2', 'card:P3']));
    // outcome node(s) occupy the max rank, and every max-rank node is an outcome
    const maxRankNodes = Object.entries(ranks)
      .filter(([, r]) => r.rank === maxRank)
      .map(([id]) => id);
    expect(maxRankNodes.length).toBeGreaterThan(0);
    expect(maxRankNodes.every((id) => id.startsWith('outcome:'))).toBe(true);
    expect(maxRankNodes).toContain('outcome:drain');
    // layers grouped by rank cover every focused node exactly once
    expect(layers.flat().sort()).toEqual([...new Set(layers.flat())].sort());
    expect(layers.flat().length).toBe(Object.keys(ranks).length);
  });

  it('draw-trigger Layer 0: enabler flood with partner dedup and amplifiers, root at rank 1', () => {
    const { ranks, layers } = computeFocusLayout('card:Root', drawFloodWithAmps, opts);
    // Layer 0 = the "draw a card" mechanic node + draw enabler cards.
    expect(layers[0]).toContain('trigger:draw a card');
    const l0Cards = layers[0].filter((id) => id.startsWith('card:'));
    // 3 enablers (Partner deduped — already in DAG as combo partner) + 2 amplifiers = 5.
    expect(l0Cards.length).toBe(5);
    expect(l0Cards).toContain('card:Enabler1');
    expect(l0Cards).toContain('card:Enabler2');
    expect(l0Cards).toContain('card:Enabler3');
    expect(l0Cards).toContain('card:Amp1');
    expect(l0Cards).toContain('card:Amp2');
    // Root is NOT duplicated into Layer 0; it is the sole rank-1 node.
    expect(l0Cards).not.toContain('card:Root');
    expect(ranks['card:Root'].rank).toBe(1);
    // Partner already in the DAG is excluded from the flood (no duplication).
    expect(l0Cards).not.toContain('card:Partner');
    // Outcomes remain rightmost.
    const maxRank = Math.max(...Object.values(ranks).map((r) => r.rank));
    expect(layers[maxRank].every((id) => id.startsWith('outcome:'))).toBe(true);
  });

  it('focusing Exquisite Blood yields a gain-life Layer 0 with Queza present as an enabler', () => {
    const QUEZA = 'card:Queza, Augur of Agonies';
    const { ranks, layers } = computeFocusLayout(EXQUISITE, graph, opts);
    expect(layers[0]).toContain('trigger:gain life');
    // Queza is a gain-life enabler and is NOT an Exquisite partner ⇒ it lands in Layer 0.
    expect(layers[0]).toContain(QUEZA);
    expect(ranks[QUEZA].rank).toBe(0);
    // Vito IS an Exquisite partner ⇒ it is NOT duplicated into Layer 0.
    expect(layers[0]).not.toContain('card:Vito, Thorn of the Dusk Rose');
  });

  it('Peer into the Abyss has NO Layer 0 (all its combos are null-trigger): stays rank 0', () => {
    const { ranks, layers } = computeFocusLayout(PEER, graph, opts);
    // No trigger nodes anywhere, and nothing to the left of Peer.
    expect(layers[0]).toEqual([PEER]);
    expect(ranks[PEER].rank).toBe(0);
    expect(layers.flat().some((id) => id.startsWith('trigger:'))).toBe(false);
  });

  it('null-trigger hub: N partners at rank 1 converging on outcome(s), no Layer 0', () => {
    const { ranks, layers } = computeFocusLayout('card:Hub', hubNoTrigger, opts);
    const rank0 = Object.entries(ranks).filter(([, r]) => r.rank === 0).map(([id]) => id);
    expect(rank0).toEqual(['card:Hub']);
    const rank1Cards = Object.entries(ranks).filter(
      ([id, r]) => r.rank === 1 && id.startsWith('card:'),
    );
    expect(rank1Cards.length).toBe(4);
    expect(new Set(rank1Cards.map(([id]) => id))).toEqual(
      new Set(['card:P1', 'card:P2', 'card:P3', 'card:P4']),
    );
    const maxRank = Math.max(...Object.values(ranks).map((r) => r.rank));
    const maxRankNodes = Object.entries(ranks)
      .filter(([, r]) => r.rank === maxRank)
      .map(([id]) => id);
    expect(maxRankNodes.every((id) => id.startsWith('outcome:'))).toBe(true);
    // No trigger nodes anywhere.
    expect(layers.flat().some((id) => id.startsWith('trigger:'))).toBe(false);
  });

  it("ragged 3-card line (Top→Sheoldred→Bolas's Citadel) adds an extra rank", () => {
    const { ranks } = computeFocusLayout(TOP, graph, opts);
    expect(ranks[TOP].rank).toBe(0);
    // Top is only in the one 3-card combo, so partners are Sheoldred + Bolas.
    expect(ranks['card:Sheoldred, the Apocalypse'].rank).toBe(1);
    expect(ranks["card:Bolas's Citadel"].rank).toBe(2); // pushed a rank deeper
    const maxRank = Math.max(...Object.values(ranks).map((r) => r.rank));
    expect(maxRank).toBe(3); // outcomes terminal, one rank beyond the deepest card
    const maxRankNodes = Object.entries(ranks)
      .filter(([, r]) => r.rank === maxRank)
      .map(([id]) => id);
    expect(maxRankNodes.every((id) => id.startsWith('outcome:'))).toBe(true);
  });

  it('ranks cards by combo-chain depth: direct partners pack on rank 1, 3-card terminal alone on rank 2', () => {
    const { ranks, layers } = computeFocusLayout('card:R', multiCombo, opts);
    // Root alone at rank 0.
    expect(ranks['card:R'].rank).toBe(0);
    expect(layers[0]).toEqual(['card:R']);
    // All three direct partners (including M, the 3-card intermediate) on rank 1.
    expect(ranks['card:P1'].rank).toBe(1);
    expect(ranks['card:P2'].rank).toBe(1);
    expect(ranks['card:M'].rank).toBe(1);
    expect(new Set(layers[1])).toEqual(new Set(['card:P1', 'card:P2', 'card:M']));
    // The 3-card terminal (reached via R→M→T) sits ALONE one rank deeper.
    expect(ranks['card:T'].rank).toBe(2);
    expect(layers[2]).toEqual(['card:T']);
    // Outcomes pinned to the single final column (max rank), all outcome nodes.
    const maxRank = Math.max(...Object.values(ranks).map((r) => r.rank));
    expect(maxRank).toBe(3);
    expect(layers[maxRank].every((id) => id.startsWith('outcome:'))).toBe(true);
    expect(new Set(layers[maxRank])).toEqual(new Set(['outcome:drain', 'outcome:draw']));
  });

  it('dual-trigger L0 with 3-card chain: exact target layers (draw + opponent-draws L0, partners packed, deep card alone)', () => {
    const { layers, ranks } = computeFocusLayout('card:Root', chainWithDualTrigger, opts);
    // Layer 0 (HYBRID): Root fires on draw AND opponent-draws → BOTH capsules present.
    expect(layers[0]).toContain('trigger:draw a card');
    expect(layers[0]).toContain('trigger:opponent draws');
    expect(layers[0].filter((id) => id.startsWith('card:')).length).toBeGreaterThan(0);
    // Root is now Layer 1 (rank 1), alone.
    expect(layers[1]).toEqual(['card:Root']);
    // Three direct partners together on rank 2 (shifted right by Layer 0).
    expect(new Set(layers[2])).toEqual(
      new Set(['card:P1', 'card:P2', 'card:P3']),
    );
    // The 3rd card of the 3-card combo alone on rank 3.
    expect(layers[3]).toEqual(['card:Deep']);
    // Outcomes on the single final rank.
    const maxRank = Math.max(...Object.values(ranks).map((r) => r.rank));
    expect(maxRank).toBe(4);
    expect(layers[maxRank].every((id) => id.startsWith('outcome:'))).toBe(true);
  });

  // ── HYBRID Layer 0 (union of combo-prereq trigger + card OWN trigger) ──────
  describe('hybrid Layer 0 — union of combo-prereq and card own-trigger', () => {
    it('Psychosis Crawler → a "draw a card" capsule + draw enablers (was empty before)', () => {
      const { layers } = computeFocusLayout('card:Psychosis Crawler', graph, opts);
      expect(layers[0]).toContain('trigger:draw a card');
      // Own-trigger only (Psychosis is in no triggered combo) — still floods draw enablers.
      const l0Cards = layers[0].filter((id) => id.startsWith('card:'));
      expect(l0Cards.length).toBeGreaterThan(0);
      expect(l0Cards).toContain('card:Brainstorm');
      // Exactly one capsule (single mechanic).
      expect(layers[0].filter((id) => id.startsWith('trigger:'))).toEqual(['trigger:draw a card']);
    });

    it('combo partner that is also an enabler is deduped out of Layer-0 flood', () => {
      const { layers, ranks } = computeFocusLayout('card:Root', rootWithPartnerDedup, opts);
      expect(layers[0]).toContain('trigger:opponent draws');
      // Exactly the opponent-draws mechanic, no other capsule.
      expect(layers[0].filter((id) => id.startsWith('trigger:'))).toEqual([
        'trigger:opponent draws',
      ]);
      // Partner is Root's ONLY combo partner AND an opponent-draws enabler → it is
      // deduped OUT of the Layer-0 flood (it already sits in the DAG at rank 2).
      const l0Cards = new Set(layers[0].filter((id) => id.startsWith('card:')));
      expect(l0Cards.has('card:Partner')).toBe(false);
      expect(ranks['card:Partner'].rank).toBe(2);
      // The other 2 enablers flood Layer 0.
      expect(l0Cards.has('card:E1')).toBe(true);
      expect(l0Cards.has('card:E2')).toBe(true);
      expect(l0Cards.size).toBe(2);
      // Root is Layer 1.
      expect(ranks['card:Root'].rank).toBe(1);
    });

    it('Exquisite Blood → BOTH "gain life" and "opponent loses life" capsules, enablers deduped', () => {
      const { layers } = computeFocusLayout(EXQUISITE, graph, opts);
      const capsules = layers[0].filter((id) => id.startsWith('trigger:')).sort();
      expect(capsules).toEqual(['trigger:gain life', 'trigger:opponent loses life']);
      // Dedup: Queza enables BOTH gain-life and opponent-loses-life but appears ONCE.
      const l0Cards = layers[0].filter((id) => id === 'card:Queza, Augur of Agonies');
      expect(l0Cards.length).toBe(1);
      // Every Layer-0 card id is unique (no enabler duplicated across the two mechanics).
      const allCards = layers[0].filter((id) => id.startsWith('card:'));
      expect(allCards.length).toBe(new Set(allCards).size);
    });

    it('Sheoldred → BOTH "draw a card" and "opponent draws" capsules', () => {
      const { layers } = computeFocusLayout('card:Sheoldred, the Apocalypse', graph, opts);
      const capsules = layers[0].filter((id) => id.startsWith('trigger:')).sort();
      expect(capsules).toEqual(['trigger:draw a card', 'trigger:opponent draws']);
      // Layer-0 cards deduped across the two mechanics (opponent-draws enablers are a
      // subset of draw enablers) — no id appears twice.
      const allCards = layers[0].filter((id) => id.startsWith('card:'));
      expect(allCards.length).toBe(new Set(allCards).size);
    });
  });

  // ── v2 role model: AMPLIFIERS + creature-enters + cast-a-spell out of scope ──
  describe('amplifiers as a first-class Layer-0 role', () => {
    it('draw payoff Layer 0 includes amplifiers tagged with role:amplifier; plain enablers are not', () => {
      const layout = computeFocusLayout('card:Root', drawFloodWithAmps, opts);
      // Both amplifiers land in Layer 0 (they are not combo partners).
      expect(layout.layers[0]).toContain('card:Amp1');
      expect(layout.layers[0]).toContain('card:Amp2');
      // Their feeding trigger edges carry role:'amplifier'.
      const ampEdges = layout.edges.filter((e) => e.kind === 'trigger' && e.role === 'amplifier');
      const ampSources = new Set(ampEdges.map((e) => e.source));
      expect(ampSources.has('card:Amp1')).toBe(true);
      expect(ampSources.has('card:Amp2')).toBe(true);
      // Plain draw enablers are NOT tagged as amplifiers.
      const enablerEdge = layout.edges.find(
        (e) => e.kind === 'trigger' && e.source === 'card:Enabler1',
      );
      expect(enablerEdge?.role).toBeUndefined();
    });

    it('Exquisite Blood (gain payoff) Layer 0 includes Alhammarret as a gain amplifier; it stays a combo node', () => {
      const ALHAMMARRET = "card:Alhammarret's Archive";
      const layout = computeFocusLayout(EXQUISITE, graph, opts);
      expect(layout.layers[0]).toContain(ALHAMMARRET);
      const ampEdge = layout.edges.find(
        (e) => e.kind === 'trigger' && e.source === ALHAMMARRET && e.role === 'amplifier',
      );
      expect(ampEdge).toBeDefined();
      // Roles ADD, never SUBTRACT: Alhammarret remains a combo node (with Peer, 1561-4435).
      expect(graph.nodes.some((n) => n.id === ALHAMMARRET && n.kind === 'card')).toBe(true);
      const combo = (combos as any).combos.find((c: any) => c.id === '1561-4435');
      expect(combo.cards).toContain("Alhammarret's Archive");
    });

    it('an amplifier already in the focused DAG (a partner) is NOT duplicated into Layer 0', () => {
      const ALHAMMARRET = "card:Alhammarret's Archive";
      // Focusing Peer: Alhammarret IS Peer's combo partner (rank 1), so it must not
      // also appear via any Layer 0 flood — Peer has no Layer 0 anyway.
      const layout = computeFocusLayout(PEER, graph, opts);
      const alhammarretNodes = layout.nodeIds.filter((id) => id === ALHAMMARRET);
      expect(alhammarretNodes.length).toBe(1);
    });
  });

  describe('degree-0 card with own-trigger Layer 0', () => {
    it('degree-0 card focus yields its own-trigger capsule + enablers, root at rank 1', () => {
      const layout = computeFocusLayout('card:Lonely', degreeZeroOwn, opts);
      // The root is always present even with no combos.
      expect(layout.nodeIds).toContain('card:Lonely');
      expect(layout.ranks['card:Lonely'].rank).toBe(1);
      // A "creature enters" capsule heads Layer 0 …
      expect(layout.layers[0]).toContain('trigger:creature enters');
      // … with creature enabler tiles flooding beneath it.
      const l0Cards = layout.layers[0].filter((id) => id.startsWith('card:'));
      expect(l0Cards.length).toBe(2);
      expect(l0Cards).toContain('card:CreatureA');
      expect(l0Cards).toContain('card:CreatureB');
      // Root is never duplicated into its own Layer 0.
      expect(l0Cards).not.toContain('card:Lonely');
    });
  });

  it('cast a spell is never a mechanic anywhere in the graph (out of scope)', () => {
    expect(graph.triggers?.mechanics['cast a spell']).toBeUndefined();
    for (const mechs of Object.values(graph.triggers?.layer0Mechanics ?? {})) {
      expect(mechs).not.toContain('cast a spell');
    }
  });

  it('order within a rank is contiguous 0..n-1', () => {
    const { ranks, layers } = computeFocusLayout(PEER, graph, opts);
    for (const layer of layers) {
      const orders = layer.map((id) => ranks[id].order).sort((a, b) => a - b);
      expect(orders).toEqual(layer.map((_, i) => i));
    }
  });
});
