// Card-centric FOCUS layering for the combo web (Phase 3).
//
// Clicking a card node turns the overview into a left→right layered DAG rooted
// at that card: the union of every combo the clicked card is in. This module is
// the PURE core of that interaction — it computes the focused connected
// component, orients it into a directed acyclic graph flowing away from the
// clicked card, and assigns ranks + pixel coordinates ANALYTICALLY (longest
// chain-path from the root over combo edges; outcomes pinned to a final column).
// No external layout engine. graph-focus.ts consumes the result to drive the
// FLIP relayout; keeping the graph math here makes it unit-testable without any DOM.
//
// Topology we build (regardless of the raw edge directions in combos.json):
//   clicked card → partner card(s) → outcome node(s)
// Combo (card↔card) edges are oriented AWAY from the clicked root by BFS
// distance (equal-distance ties keep their original source→target direction, so
// a ragged chain such as Top→Sheoldred→Bolas's Citadel earns an extra rank).
// Each combo's outcomes are fed from that combo's TERMINAL card(s) — the ones
// with no outgoing combo edge to another card of the same combo — so outcomes
// always land on the graph's max (right-most) rank.

export interface FocusNode {
  id: string;
  kind: 'card' | 'outcome' | 'trigger';
}

export interface FocusEdge {
  source: string;
  target: string;
  kind: 'combo' | 'produces' | 'trigger';
  comboId?: string;
}

/** One trigger-mechanic definition (e.g. "draw a card") + its enabler CARD NAMES. */
export interface TriggerMechanicDef {
  mechanic: string;
  label: string;
  icon: string;
  sublabel?: string | null;
  enablers: { name: string; chainsLeft?: boolean }[];
}

/**
 * Trigger metadata for the "true Layer 0" of the focus DAG: the cards / mechanics
 * that CAUSE each combo to fire, placed to the LEFT of the clicked card. Optional —
 * when absent the DAG behaves exactly as before (clicked card at rank 0).
 */
export interface FocusTriggers {
  /** mechanic key → its def (enabler card names + faux-edge flags). */
  mechanics: Record<string, TriggerMechanicDef>;
  /** comboId → mechanic key (or null: no enabler trigger, e.g. Peer hard-cast).
   *  Retained for provenance; Layer-0 composition now reads `layer0Mechanics`. */
  comboMechanic: Record<string, string | null>;
  /**
   * HYBRID authoritative map: CARD NAME → the mechanics whose enabler flood forms
   * that card's Layer 0. It is the UNION of the card's OWN trigger ability and the
   * trigger of every combo it participates in — so a card can have MULTIPLE
   * Layer-0 capsules (e.g. Sheoldred = draw a card + opponent draws; Exquisite
   * Blood = opponent loses life + gain life). Cards without a Layer 0 are absent.
   */
  layer0Mechanics?: Record<string, string[]>;
  /**
   * AMPLIFIERS (v2 role model): mechanic key → CARD NAMES that MULTIPLY/replace
   * instances of that mechanic (doublers) rather than produce them. A payoff's
   * Layer 0 surfaces these ALONGSIDE the enablers, visually tagged (×2). They are
   * NEVER dropped and remain combo nodes — roles ADD, never SUBTRACT.
   */
  amplifiers?: Record<string, { name: string }[]>;
}

export interface FocusGraph {
  nodes: FocusNode[];
  edges: FocusEdge[];
  /** Optional Layer-0 trigger metadata (see FocusTriggers). */
  triggers?: FocusTriggers;
}

/** A directed edge in the synthesized focus DAG. Self-describing: it carries the
 *  comboId of the combo it belongs to, so an edge tap resolves to the correct
 *  combo WITHOUT a global endpoint lookup (which could match a non-focus combo). */
export interface DagEdge {
  source: string;
  target: string;
  kind: 'combo' | 'produces' | 'trigger';
  comboId?: string;
  /** Set on a Layer-0 'trigger' edge whose source AMPLIFIES the mechanic (doubler)
   *  rather than enables it — the renderer badges the tile / styles the edge. */
  role?: 'amplifier';
}

export interface FocusDag {
  clickedId: string;
  nodeIds: string[];
  edges: DagEdge[];
}

export interface RankInfo {
  rank: number;
  order: number;
}

export interface FocusLayout extends FocusDag {
  /** nodeId → { rank (0 = clicked), order (position within the rank) }. */
  ranks: Record<string, RankInfo>;
  /** Node ids grouped by rank, index = rank; each inner array sorted by order. */
  layers: string[][];
  /** Analytic layout coordinates (centre x,y) per node — for pixel mapping. */
  coords: Record<string, { x: number; y: number }>;
}

export interface FocusLayoutOptions {
  nodeWidth: number;
  nodeHeight: number;
  /** Rank-to-rank gap (px along the flow axis). */
  ranksep?: number;
  /** Within-rank node gap (px). */
  nodesep?: number;
}

type NodeKind = 'card' | 'outcome' | 'trigger';

function kindOf(id: string, kinds: Map<string, NodeKind>): NodeKind {
  return (
    kinds.get(id) ??
    (id.startsWith('outcome:') ? 'outcome' : id.startsWith('trigger:') ? 'trigger' : 'card')
  );
}

/**
 * Build the directed focus DAG rooted at `clickedId`: the union of every combo
 * the clicked card belongs to. Pure — no layout engine, no DOM.
 */
export function buildFocusDag(clickedId: string, graph: FocusGraph): FocusDag {
  const kinds = new Map(graph.nodes.map((n) => [n.id, n.kind]));

  // 1. Combos the CLICKED card participates in (card-centric union).
  const focusedComboIds = new Set<string>();
  for (const e of graph.edges) {
    if (e.comboId == null) continue;
    if (e.source === clickedId || e.target === clickedId) focusedComboIds.add(e.comboId);
  }

  // 2. Every edge of those combos.
  const focusEdges = graph.edges.filter((e) => e.comboId != null && focusedComboIds.has(e.comboId));

  // 3. Undirected combo adjacency among cards (remember original direction for ties).
  interface UndirectedComboEdge {
    a: string;
    b: string;
    origSource: string;
    origTarget: string;
    comboId: string;
  }
  const comboEdges: UndirectedComboEdge[] = [];
  const adj = new Map<string, Set<string>>();
  const nodeSet = new Set<string>();
  for (const e of focusEdges) {
    nodeSet.add(e.source);
    nodeSet.add(e.target);
    if (e.kind !== 'combo') continue;
    comboEdges.push({
      a: e.source,
      b: e.target,
      origSource: e.source,
      origTarget: e.target,
      comboId: e.comboId!,
    });
    if (!adj.has(e.source)) adj.set(e.source, new Set());
    if (!adj.has(e.target)) adj.set(e.target, new Set());
    adj.get(e.source)!.add(e.target);
    adj.get(e.target)!.add(e.source);
  }

  // 4. BFS distance from the clicked card over the undirected combo graph.
  const dist = new Map<string, number>();
  dist.set(clickedId, 0);
  const queue: string[] = [clickedId];
  while (queue.length) {
    const cur = queue.shift()!;
    const d = dist.get(cur)!;
    for (const nb of adj.get(cur) ?? []) {
      if (!dist.has(nb)) {
        dist.set(nb, d + 1);
        queue.push(nb);
      }
    }
  }
  const distOf = (id: string): number => dist.get(id) ?? Number.POSITIVE_INFINITY;

  // 5. Orient combo edges away from the root; equal distance keeps orig direction.
  const dagEdges: DagEdge[] = [];
  const seen = new Set<string>();
  const pushEdge = (
    source: string,
    target: string,
    kind: 'combo' | 'produces',
    comboId: string,
  ): void => {
    const key = `${kind}:${source}->${target}`;
    if (seen.has(key) || source === target) return;
    seen.add(key);
    dagEdges.push({ source, target, kind, comboId });
  };
  // Oriented combo adjacency, used to detect per-combo terminal cards.
  const outCombo = new Map<string, Set<string>>();
  for (const ce of comboEdges) {
    const da = distOf(ce.a);
    const db = distOf(ce.b);
    let source: string;
    let target: string;
    if (da < db) [source, target] = [ce.a, ce.b];
    else if (db < da) [source, target] = [ce.b, ce.a];
    else [source, target] = [ce.origSource, ce.origTarget]; // tie → original
    pushEdge(source, target, 'combo', ce.comboId);
    if (!outCombo.has(source)) outCombo.set(source, new Set());
    outCombo.get(source)!.add(target);
  }

  // 6. Feed each combo's outcomes from that combo's TERMINAL card(s) — cards with
  //    no outgoing combo edge to another card of the SAME combo — so outcomes sit
  //    on the max rank. (Falls back to the deepest card if a combo has no terminal.)
  const cardsByCombo = new Map<string, Set<string>>();
  const outcomesByCombo = new Map<string, Set<string>>();
  for (const e of focusEdges) {
    const cid = e.comboId!;
    if (!cardsByCombo.has(cid)) cardsByCombo.set(cid, new Set());
    if (!outcomesByCombo.has(cid)) outcomesByCombo.set(cid, new Set());
    for (const ep of [e.source, e.target]) {
      if (kindOf(ep, kinds) === 'outcome') outcomesByCombo.get(cid)!.add(ep);
      else cardsByCombo.get(cid)!.add(ep);
    }
  }
  for (const [cid, cards] of cardsByCombo) {
    const outcomes = outcomesByCombo.get(cid) ?? new Set<string>();
    if (outcomes.size === 0) continue;
    let terminals = [...cards].filter((c) => {
      const outs = outCombo.get(c);
      if (!outs) return true;
      // terminal iff it points to no OTHER card of this same combo
      return ![...outs].some((t) => cards.has(t));
    });
    if (terminals.length === 0) {
      const maxD = Math.max(...[...cards].map(distOf));
      terminals = [...cards].filter((c) => distOf(c) === maxD);
    }
    for (const t of terminals) {
      // Thread the REAL comboId (cid) so the produces edge is self-describing: a
      // tap opens THIS combo, never a same-endpoint combo the root isn't in.
      for (const o of outcomes) pushEdge(t, o, 'produces', cid);
    }
  }

  // 7. TRUE LAYER 0 (HYBRID) — the trigger mechanics of the CLICKED CARD, to its
  //    LEFT. The mechanic set is the authoritative per-card union carried by
  //    `layer0Mechanics` (card OWN trigger ∪ the trigger of every combo it is in),
  //    so a card can surface MULTIPLE capsules (e.g. Sheoldred = draw + opponent
  //    draws). Layer 0 gets, per mechanic: the mechanic node + ALL its enabler
  //    cards (full flood). Enabler cards are DEDUPED across the card's mechanics (a
  //    card enabling two of them appears ONCE, via the `nodeSet`/edge-key sets) and
  //    EXCLUDE the clicked card itself and any card already in the focused DAG at
  //    another rank (a partner is never duplicated into Layer 0). Every Layer-0
  //    node points rightward INTO the clicked card via a neutral 'trigger' edge.
  if (graph.triggers && kindOf(clickedId, kinds) === 'card') {
    const { mechanics, layer0Mechanics, amplifiers } = graph.triggers;
    const clickedName = clickedId.replace(/^card:/, '');
    const wanted = new Set<string>(layer0Mechanics?.[clickedName] ?? []);
    const alreadyInDag = new Set(nodeSet); // clicked + partners + outcomes
    const addTriggerEdge = (source: string, role?: 'amplifier'): void => {
      const key = `trigger:${source}->${clickedId}`;
      if (seen.has(key) || source === clickedId) return;
      seen.add(key);
      dagEdges.push({ source, target: clickedId, kind: 'trigger', ...(role ? { role } : {}) });
    };
    for (const mech of wanted) {
      const def = mechanics[mech];
      if (!def) continue;
      const trigId = `trigger:${mech}`;
      nodeSet.add(trigId);
      addTriggerEdge(trigId);
      // (a) ENABLERS — full flood, deduped against partners already in the DAG.
      for (const en of def.enablers) {
        const enId = `card:${en.name}`;
        if (enId === clickedId) continue; // never the clicked card
        if (alreadyInDag.has(enId)) continue; // already a partner/outcome in this focus
        nodeSet.add(enId);
        addTriggerEdge(enId);
      }
      // (b) AMPLIFIERS — the same Layer 0, tagged (×2). Deduped against the clicked
      // card and any card already in the focused DAG (a combo partner is never
      // duplicated into Layer 0 — the renderer still badges its existing node via
      // the amplifier set it derives independently). Amplifiers remain combo nodes;
      // surfacing one here NEVER removes it from the graph (roles add, not subtract).
      for (const amp of amplifiers?.[mech] ?? []) {
        const ampId = `card:${amp.name}`;
        if (ampId === clickedId) continue;
        if (alreadyInDag.has(ampId)) continue; // already a partner → badge in place, don't dupe
        nodeSet.add(ampId);
        addTriggerEdge(ampId, 'amplifier');
      }
    }
  }

  // Guarantee the clicked card is a node even with NO combos and NO Layer 0 (e.g.
  // Soul Warden, degree 0 with only an own-trigger flood): it is the root of the
  // focus and must always render.
  if (kindOf(clickedId, kinds) === 'card') nodeSet.add(clickedId);

  const nodeIds = [...nodeSet];
  return { clickedId, nodeIds, edges: dagEdges };
}

/**
 * Build the focus DAG and lay it out left→right into layered ranks.
 *
 * CARD ranks are the **longest chain-path length from the clicked root over the
 * card↔card COMBO edges ONLY** (topological longest-path; root = 0). The
 * card→outcome `produces` edges deliberately do NOT influence card ranks — that
 * was the old bug: a network-simplex ranker pulled every terminal partner
 * down next to the outcome column, stranding lone intermediate cards. Ranking by
 * combo-chain depth instead packs all direct partners together at rank 1; only a
 * genuinely deeper hop (e.g. the 3rd card of a 3-card combo, reached via
 * root→partner→card) lands at rank 2+.
 *
 * ALL outcome nodes are pinned to a single final column at rank = maxCardRank + 1.
 * Coordinates are assigned analytically from the pinned ranks (x from rank, cards
 * centred vertically within their rank) — fully controllable analytic ranking.
 */
export function computeFocusLayout(
  clickedId: string,
  graph: FocusGraph,
  options: FocusLayoutOptions,
): FocusLayout {
  const dag = buildFocusDag(clickedId, graph);
  const { nodeWidth, nodeHeight } = options;
  const ranksep = options.ranksep ?? 120;
  const nodesep = options.nodesep ?? 24;

  const kinds = new Map<string, NodeKind>(graph.nodes.map((n) => [n.id, n.kind]));
  const kindAt = (id: string): NodeKind => kindOf(id, kinds);

  // Incoming COMBO-edge adjacency (card→card), oriented away from the root by
  // buildFocusDag. produces edges are excluded so outcomes can't pull card ranks.
  const comboIn = new Map<string, string[]>();
  for (const id of dag.nodeIds) comboIn.set(id, []);
  for (const e of dag.edges) {
    if (e.kind === 'combo') comboIn.get(e.target)!.push(e.source);
  }

  // Longest-path rank from the clicked root over combo edges (cards only).
  const memo = new Map<string, number>();
  const inProgress = new Set<string>();
  const cardRank = (id: string): number => {
    if (id === clickedId) return 0;
    const cached = memo.get(id);
    if (cached !== undefined) return cached;
    if (inProgress.has(id)) return 1; // cycle guard (data is acyclic, belt & braces)
    inProgress.add(id);
    const ins = comboIn.get(id) ?? [];
    let best = -Infinity;
    for (const u of ins) {
      if (kindAt(u) !== 'card') continue;
      best = Math.max(best, cardRank(u) + 1);
    }
    if (!Number.isFinite(best)) best = 1; // no combo parent → treat as a direct partner
    inProgress.delete(id);
    memo.set(id, best);
    return best;
  };

  // TRUE LAYER 0: nodes that feed the clicked card via a 'trigger' edge (enabler
  //  cards) plus the mechanic nodes themselves sit at the leftmost rank. When any
  //  exist, the whole card/outcome stack shifts RIGHT by one so the clicked card is
  //  Layer 1 (rank 1). With no triggers the layout is unchanged (clicked = rank 0).
  const layer0 = new Set<string>();
  for (const e of dag.edges) {
    if (e.kind === 'trigger') layer0.add(e.source);
  }
  const offset = layer0.size > 0 ? 1 : 0;

  // Card ranks (chain depth) over the NON-layer-0 cards only; clicked = 0 (→ offset).
  const cardIds = dag.nodeIds.filter((id) => kindAt(id) === 'card' && !layer0.has(id));
  let maxCardRank = 0;
  for (const id of cardIds) maxCardRank = Math.max(maxCardRank, offset + cardRank(id));
  const outcomeRank = maxCardRank + 1;

  const rawRank = new Map<string, number>();
  for (const id of dag.nodeIds) {
    if (layer0.has(id)) rawRank.set(id, 0);
    else if (kindAt(id) === 'outcome') rawRank.set(id, outcomeRank);
    else rawRank.set(id, offset + cardRank(id));
  }

  // Compact ranks so `layers` is a gap-free 0..k array (ranks[id].rank == index).
  const present = [...new Set(rawRank.values())].sort((a, b) => a - b);
  const dense = new Map(present.map((r, i) => [r, i]));

  const buckets = new Map<number, string[]>();
  for (const id of dag.nodeIds) {
    const r = dense.get(rawRank.get(id)!)!;
    if (!buckets.has(r)) buckets.set(r, []);
    buckets.get(r)!.push(id);
  }

  const ranks: Record<string, RankInfo> = {};
  const layers: string[][] = [];
  const coords: Record<string, { x: number; y: number }> = {};
  const stepX = nodeWidth + ranksep;
  const stepY = nodeHeight + nodesep;
  const sortedRanks = [...buckets.keys()].sort((a, b) => a - b);
  // Within a rank: trigger-mechanic node(s) head the column (a header for the
  // enabler flood beneath them), then everything else alphabetically.
  const orderKey = (id: string): [number, string] => [kindAt(id) === 'trigger' ? 0 : 1, id];
  for (const r of sortedRanks) {
    const ids = buckets.get(r)!.sort((a, b) => {
      const [ka, sa] = orderKey(a);
      const [kb, sb] = orderKey(b);
      if (ka !== kb) return ka - kb;
      return sa < sb ? -1 : sa > sb ? 1 : 0;
    });
    layers[r] = ids;
    const n = ids.length;
    const totalH = n * nodeHeight + (n - 1) * nodesep;
    const startY = -totalH / 2;
    ids.forEach((id, order) => {
      ranks[id] = { rank: r, order };
      coords[id] = { x: r * stepX + nodeWidth / 2, y: startY + order * stepY + nodeHeight / 2 };
    });
  }

  return { ...dag, ranks, layers, coords };
}
