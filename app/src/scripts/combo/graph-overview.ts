// Force-directed OVERVIEW layout for the combo web (Phase 2).
//
// The deck's combo graph is laid out headlessly with d3-force: a many-body
// repulsion spreads the nodes, links pull combo/produces neighbours together,
// and a centering force keeps the whole web on-screen. The simulation is run to
// rest (`.stop()` + a fixed number of `.tick()`s) so the caller gets a resting
// `{x,y}` per node with no animation loop.
//
// d3's built-in `forceCollide` is CIRCLE-based, which lets rectangular card
// tiles overlap at their corners. So after the force pass we run a small, PURE,
// rectangle-aware separation post-pass (`resolveOverlaps`) that guarantees no
// two tile boxes overlap (AABB). That function is exported and unit-tested.
//
// Determinism: initial positions are seeded by node index (a phyllotaxis-style
// spiral using the golden angle) rather than Math.random, so repeated runs
// return identical layouts.

import {
  forceSimulation,
  forceManyBody,
  forceLink,
  forceCenter,
  type Simulation,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from 'd3-force';

/** A node the layout can place. `degree` drives card-tile sizing (hub scale). */
export interface GraphNodeInput {
  id: string;
  kind: 'card' | 'outcome';
  degree?: number;
}

export interface GraphEdgeInput {
  source: string;
  target: string;
  kind: 'combo' | 'produces';
}

export interface GraphInput {
  nodes: GraphNodeInput[];
  edges: GraphEdgeInput[];
}

/** A center-anchored axis-aligned box: (x,y) is the CENTER; w,h the full size. */
export interface Box {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface LayoutOptions {
  width: number;
  height: number;
  /** Tile box size for a node (card tiles scale by degree; outcomes are pills). */
  sizeFor: (node: GraphNodeInput) => { w: number; h: number };
  /** Headless simulation ticks to run to rest. */
  iterations?: number;
  /** Minimum gap enforced between separated boxes (px). */
  padding?: number;
  /** Minimum padding kept between any node and the stage/viewport edges (px). */
  pad?: number;
  /** Base link distance (px) before per-node size padding. */
  linkDistance?: number;
  /** Many-body charge (negative = repulsion). */
  charge?: number;
}

/* ------------------------------------------------------------------ *
 * Rectangle collision — pure, exported, unit-tested.
 * ------------------------------------------------------------------ */

/** True when two center-anchored boxes share a strictly-positive area. */
export function boxesOverlap(a: Box, b: Box): boolean {
  return Math.abs(a.x - b.x) * 2 < a.w + b.w && Math.abs(a.y - b.y) * 2 < a.h + b.h;
}

/**
 * Iteratively separate any overlapping boxes so none share area (AABB), leaving
 * already-separated boxes untouched. Pure: operates on copies, returns new
 * boxes. Each overlapping pair is pushed apart along the axis of LEAST
 * penetration (minimum translation vector) by half the overlap each, plus
 * padding — the cheapest resolution, which keeps the force layout's shape.
 */
export function resolveOverlaps(
  boxes: readonly Box[],
  opts: { padding?: number; iterations?: number } = {},
): Box[] {
  const padding = opts.padding ?? 4;
  const maxIter = opts.iterations ?? 200;
  const out = boxes.map((b) => ({ ...b }));

  for (let iter = 0; iter < maxIter; iter++) {
    let moved = false;
    for (let i = 0; i < out.length; i++) {
      for (let j = i + 1; j < out.length; j++) {
        const a = out[i];
        const b = out[j];
        // Signed penetration on each axis (positive = overlapping).
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const overlapX = (a.w + b.w) / 2 + padding - Math.abs(dx);
        const overlapY = (a.h + b.h) / 2 + padding - Math.abs(dy);
        if (overlapX <= 0 || overlapY <= 0) continue; // separated on some axis
        moved = true;
        // Resolve along the axis of least penetration.
        if (overlapX < overlapY) {
          const push = overlapX / 2;
          // dx===0 → deterministic tie-break by id order (a left, b right).
          const dir = dx === 0 ? (a.id < b.id ? -1 : 1) : Math.sign(dx);
          a.x -= dir * push;
          b.x += dir * push;
        } else {
          const push = overlapY / 2;
          const dir = dy === 0 ? (a.id < b.id ? -1 : 1) : Math.sign(dy);
          a.y -= dir * push;
          b.y += dir * push;
        }
      }
    }
    if (!moved) break;
  }
  return out;
}

/**
 * Width-BOUNDED rectangle separation. Like `resolveOverlaps`, but every box's
 * CENTRE-x is clamped to `[pad + w/2, width - pad - w/2]` (so its left/right edge
 * keeps `pad` px off the viewport edges), and overlaps are pushed apart. When two
 * boxes cannot be separated horizontally within the width, the push falls back to
 * the y-axis — so a graph too wide to fit stays inside the viewport width and
 * grows/scrolls VERTICALLY instead of overflowing sideways. Pure.
 *
 * Guarantees at convergence: no two boxes overlap (each separated pair clears by
 * ≥ `gap` on its separating axis), and every box sits within the width padding.
 */
export function separateWithinWidth(
  boxes: readonly Box[],
  opts: { width: number; pad?: number; gap?: number; iterations?: number },
): Box[] {
  const pad = opts.pad ?? 16;
  const gap = opts.gap ?? 20;
  const maxIter = opts.iterations ?? 800;
  const out = boxes.map((b) => ({ ...b }));

  const clampX = (b: Box): number => {
    const min = pad + b.w / 2;
    const max = opts.width - pad - b.w / 2;
    if (min > max) return opts.width / 2; // wider than the available width → centre
    return Math.min(Math.max(b.x, min), max);
  };

  for (const b of out) b.x = clampX(b);
  for (let iter = 0; iter < maxIter; iter++) {
    let moved = false;
    for (let i = 0; i < out.length; i++) {
      for (let j = i + 1; j < out.length; j++) {
        const a = out[i];
        const b = out[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const overlapX = (a.w + b.w) / 2 + gap - Math.abs(dx);
        const overlapY = (a.h + b.h) / 2 + gap - Math.abs(dy);
        if (overlapX <= 0 || overlapY <= 0) continue; // already clear on an axis
        moved = true;
        // Prefer the axis of least penetration, but never resolve on x if the
        // width-clamp would swallow the push (no room) — fall back to y then.
        let useY = overlapX >= overlapY;
        if (!useY) {
          const push = overlapX / 2;
          const dir = dx === 0 ? (a.id < b.id ? -1 : 1) : Math.sign(dx);
          const na = clampX({ ...a, x: a.x - dir * push });
          const nb = clampX({ ...b, x: b.x + dir * push });
          if (Math.abs(nb - na) + 1e-6 < (a.w + b.w) / 2 + gap) {
            useY = true; // clamp ate the separation → resolve vertically instead
          } else {
            a.x = na;
            b.x = nb;
          }
        }
        if (useY) {
          const push = overlapY / 2;
          const dir = dy === 0 ? (a.id < b.id ? -1 : 1) : Math.sign(dy);
          a.y -= dir * push;
          b.y += dir * push;
        }
      }
    }
    for (const b of out) b.x = clampX(b);
    if (!moved) break;
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * Headless force layout.
 * ------------------------------------------------------------------ */

interface SimNode extends SimulationNodeDatum {
  id: string;
  kind: 'card' | 'outcome';
  degree?: number;
  w: number;
  h: number;
  /** Bounding radius (half-diagonal) — used to size link distance + charge. */
  radius: number;
}

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5)); // ≈ 2.399963 rad

/**
 * Run the force simulation headlessly and return a resting `{x,y}` per node id.
 * Positions are then rectangle-separated so no two tile boxes overlap.
 */
export function computeOverviewLayout(
  graph: GraphInput,
  options: LayoutOptions,
): Record<string, Point> {
  const { width, height, sizeFor } = options;
  const iterations = options.iterations ?? 300;
  const padding = options.padding ?? 20; // min inter-node gap
  const pad = options.pad ?? 16; // min gap to the viewport edges
  const linkDistance = options.linkDistance ?? 150;
  const charge = options.charge ?? -900;

  const cx = width / 2;
  const cy = height / 2;

  // Seed positions deterministically in a phyllotaxis spiral around the center.
  const spread = Math.min(width, height) / 4;
  const nodes: SimNode[] = graph.nodes.map((n, i) => {
    const size = sizeFor(n);
    const radius = Math.hypot(size.w, size.h) / 2;
    const angle = i * GOLDEN_ANGLE;
    const r = spread * Math.sqrt((i + 1) / graph.nodes.length);
    return {
      id: n.id,
      kind: n.kind,
      degree: n.degree,
      w: size.w,
      h: size.h,
      radius,
      x: cx + r * Math.cos(angle),
      y: cy + r * Math.sin(angle),
    };
  });

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links: SimulationLinkDatum<SimNode>[] = graph.edges
    .filter((e) => byId.has(e.source) && byId.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  const sim: Simulation<SimNode, SimulationLinkDatum<SimNode>> = forceSimulation(nodes)
    .force(
      'link',
      forceLink<SimNode, SimulationLinkDatum<SimNode>>(links)
        .id((d) => d.id)
        // Longer for bigger endpoints so hub tiles don't crowd their neighbours.
        .distance((l) => {
          const s = l.source as SimNode;
          const t = l.target as SimNode;
          return linkDistance + s.radius + t.radius;
        })
        .strength(0.4),
    )
    // Repulsion scales with tile size so large hubs clear more space.
    .force(
      'charge',
      forceManyBody<SimNode>().strength((d) => charge - d.radius * 4),
    )
    .force('center', forceCenter(cx, cy))
    .stop();

  // Headless: run to rest with a fixed tick budget (no animation loop).
  for (let i = 0; i < iterations; i++) sim.tick();

  // Rectangle-aware separation, BOUNDED to the viewport width: tile boxes never
  // overlap (with ≥ `padding` gap), never come within `pad` of a side edge, and
  // when the web is too wide to fit it grows vertically instead of overflowing.
  const boxes: Box[] = nodes.map((n) => ({
    id: n.id,
    x: n.x ?? cx,
    y: n.y ?? cy,
    w: n.w,
    h: n.h,
  }));
  const separated = separateWithinWidth(boxes, { width, pad, gap: padding, iterations: 900 });

  // Vertical placement: centre the content column when it fits the viewport
  // height, otherwise top-align it at `pad` and let the stage scroll down. X is
  // already bounded, so we never touch it (that would break the width bound).
  let minY = Infinity;
  let maxY = -Infinity;
  for (const b of separated) {
    minY = Math.min(minY, b.y - b.h / 2);
    maxY = Math.max(maxY, b.y + b.h / 2);
  }
  const contentH = maxY - minY || 1;
  const availH = height - pad * 2;
  const targetTop = contentH <= availH ? (height - contentH) / 2 : pad;
  const dy = targetTop - minY;

  const out: Record<string, Point> = {};
  for (const b of separated) out[b.id] = { x: b.x, y: b.y + dy };
  return out;
}
