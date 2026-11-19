// Client entry for the /combos OVERVIEW graph (Phase 2 — static overview only;
// focus/click behaviour is Phase 3).
//
// Reads the combo graph embedded as JSON, runs the headless force layout
// (graph-overview.ts), then positions each server-rendered node container by
// setting `style.transform = translate(x,y)` — matching the app's idiom in
// view.ts / drag.ts (absolute tiles moved by transform). Finally it draws the
// SVG edge layer from resting node centres, behind the nodes.

import { computeOverviewLayout, type GraphInput, type GraphNodeInput } from './graph-overview';
import { initFocus, type OverviewSnapshot } from './graph-focus';

interface RawNode {
  id: string;
  kind: 'card' | 'outcome';
  name?: string;
  degree?: number;
  outcomeType?: string;
}
interface RawEdge {
  source: string;
  target: string;
  kind: 'combo' | 'produces';
  comboId?: string;
}
interface GraphData {
  nodes: RawNode[];
  edges: RawEdge[];
}

// Last overview state — captured so the focus interaction (graph-focus.ts) can
// rubber-band every node back to its EXACT overview slot on dismiss.
let overviewSnapshot: OverviewSnapshot = {
  transforms: new Map(),
  centre: new Map(),
  width: 0,
  stageH: 0,
};
let focusWired = false;

/** Outcome-type → tint. Shared with the CSS capsule classes (keep in sync). */
export const OUTCOME_COLORS: Record<string, string> = {
  drain: '#dc2626',
  draw: '#3b82f6',
  tokens: '#d4a017',
  lifegain: '#ec4899',
  mana: '#06b6d4',
  damage: '#f97316',
  other: '#a78bfa',
};

const SVG_NS = 'http://www.w3.org/2000/svg';

function readGraph(): GraphData | null {
  const el = document.getElementById('combo-graph-data');
  if (!el?.textContent) return null;
  try {
    return JSON.parse(el.textContent) as GraphData;
  } catch {
    return null;
  }
}

function nodeEls(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.graph-node[data-node-id]'));
}

/** Escape a value going into an SVG attribute. */
function escAttr(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** Layout + positioning + edge draw. Re-run on resize so it stays responsive. */
function layout(graph: GraphData): void {
  const stage = document.getElementById('combo-stage');
  const svg = document.getElementById('edge-layer');
  if (!stage || !svg) return;

  const width = stage.clientWidth || window.innerWidth;
  const height = stage.clientHeight || window.innerHeight;

  // Measure each rendered container's box so the layout's collision matches the
  // real DOM footprint (card tiles are scaled by degree via CSS).
  const size = new Map<string, { w: number; h: number }>();
  for (const el of nodeEls()) {
    const r = el.getBoundingClientRect();
    size.set(el.dataset.nodeId!, { w: r.width, h: r.height });
  }

  const layoutNodes: GraphNodeInput[] = graph.nodes.map((n) => ({
    id: n.id,
    kind: n.kind,
    degree: n.degree,
  }));
  const input: GraphInput = { nodes: layoutNodes, edges: graph.edges };

  // The force layout is now WIDTH-BOUNDED and never downscaled: cards render at
  // their full (min-floored) size, no two nodes overlap, nothing touches a side
  // edge, and when the web is too wide to fit it grows vertically. So we place
  // each node at its computed centre at scale 1 and grow the stage's height to
  // fit, letting `#combo-stage` scroll vertically (never horizontally).
  const pad = 16;
  const positions = computeOverviewLayout(input, {
    width,
    height,
    sizeFor: (n) => size.get(n.id) ?? { w: 120, h: 167 },
    padding: 22,
    pad,
  });

  const centre = new Map<string, { x: number; y: number }>();
  const transforms = new Map<string, string>();
  let maxBottom = 0;
  for (const el of nodeEls()) {
    const id = el.dataset.nodeId!;
    const p = positions[id];
    const s = size.get(id) ?? { w: 120, h: 167 };
    if (!p) continue;
    const c = { x: p.x, y: p.y };
    const transform = `translate(${c.x - s.w / 2}px, ${c.y - s.h / 2}px) scale(1)`;
    // Only paint the overview transform when NOT currently focused, so a resize
    // during focus doesn't yank tiles out of the focused DAG.
    if (document.body.dataset.comboMode !== 'focus') {
      el.style.transform = transform;
      el.style.transformOrigin = 'center center';
    }
    centre.set(id, c);
    transforms.set(id, transform);
    maxBottom = Math.max(maxBottom, c.y + s.h / 2);
  }

  const stageH = Math.max(height, maxBottom + pad);
  stage.style.height = `${stageH}px`;

  // Capture the resting overview state for the focus/dismiss interaction.
  overviewSnapshot = { transforms, centre, width, stageH };

  if (document.body.dataset.comboMode !== 'focus') {
    drawEdges(svg as unknown as SVGSVGElement, graph, centre, width, stageH);
  }

  if (!focusWired) {
    focusWired = true;
    initFocus(graph, () => overviewSnapshot, () => {
      const s = document.getElementById('edge-layer');
      if (s) drawEdges(s as unknown as SVGSVGElement, graph, overviewSnapshot.centre, overviewSnapshot.width, overviewSnapshot.stageH);
    });
  }
}

function drawEdges(
  svg: SVGSVGElement,
  graph: GraphData,
  centre: Map<string, { x: number; y: number }>,
  width: number,
  height: number,
): void {
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(height));

  // Arrow markers, one per outcome tint (so produces edges point at the outcome).
  const usedTints = new Set<string>();
  for (const e of graph.edges) {
    if (e.kind !== 'produces') continue;
    const t = graph.nodes.find((n) => n.id === e.target);
    usedTints.add(t?.outcomeType ?? 'other');
  }
  const defs = usedTints.size
    ? `<defs>${[...usedTints]
        .map((tint) => {
          const color = OUTCOME_COLORS[tint] ?? OUTCOME_COLORS.other;
          return (
            `<marker id="arrow-${tint}" viewBox="0 0 10 10" refX="9" refY="5" ` +
            `markerWidth="7" markerHeight="7" orient="auto-start-reverse">` +
            `<path d="M0,0 L10,5 L0,10 z" fill="${color}"/></marker>`
          );
        })
        .join('')}</defs>`
    : '';

  // Transparent wide hit overlay per edge so taps land on the thin visible line.
  const hitFor = (e: RawEdge, a: { x: number; y: number }, b: { x: number; y: number }): string =>
    e.comboId
      ? `<line class="edge-hit" data-combo-id="${escAttr(e.comboId)}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="transparent" stroke-width="16" stroke-linecap="round"/>`
      : '';

  const lines = graph.edges
    .map((e) => {
      const a = centre.get(e.source);
      const b = centre.get(e.target);
      if (!a || !b) return '';
      if (e.kind === 'combo') {
        return (
          `<line class="edge edge-combo" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" ` +
          `stroke="#52525b" stroke-width="1.25" stroke-opacity="0.55"/>` +
          hitFor(e, a, b)
        );
      }
      // produces → tinted, heavier, directional toward the outcome.
      const t = graph.nodes.find((n) => n.id === e.target);
      const tint = t?.outcomeType ?? 'other';
      const color = OUTCOME_COLORS[tint] ?? OUTCOME_COLORS.other;
      return (
        `<line class="edge edge-produces" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" ` +
        `stroke="${color}" stroke-width="2.25" stroke-opacity="0.7" ` +
        `marker-end="url(#arrow-${tint})"/>` +
        hitFor(e, a, b)
      );
    })
    .join('');

  svg.innerHTML = defs + lines;
}

function init(): void {
  const graph = readGraph();
  if (!graph) return;
  layout(graph);
  let raf = 0;
  window.addEventListener('resize', () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => layout(graph));
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
