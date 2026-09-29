// CARD-FIRST focus navigation for the combo web (Phase 5 redesign).
//
// The graph is primarily a card navigator. Clicking ANY card node — from the
// resting overview OR while already focused on another card — re-roots the
// focused DAG on the clicked card: computeFocusLayout(clickedId) → analytic LR
// longest-path layout, then
// every tile FLIP-animates from its current slot to the new one (the app's shared
// `transition: transform` idiom, staggered by rank so it reads left→right). Nodes
// outside the focused component are fully HIDDEN (opacity 0 + inert) so the view
// shows ONLY the focused DAG — no faded ghost pile. Navigation happens through the
// visible cards; dismiss rubber-bands every hidden node back to its overview slot.
// The clicked root gets a "you are here" treatment.
//
// There is no persistent side panel any more. A single combo LINE is explained
// on demand by tapping its EDGE, which opens a transient bottom sheet
// (combo-sheet.ts) overlaying the graph. Reset to the overview: empty-stage
// click, Esc, or the ⤺ Overview button — each rubber-bands every node back to the
// EXACT overview positions captured by overview-client.

import { computeFocusLayout, type FocusGraph, type FocusTriggers } from './layers';
import { computeBlockFit, computeBlockCoords, type LayerDim } from './focus-fit';
import { OUTCOME_COLORS } from './overview-client';
import { openSheet, closeSheet, isSheetOpen, initSheet } from './combo-sheet';
import {
  initMobilePager,
  enterMobileFocus,
  exitMobileFocus,
  isMobilePagerActive,
} from './graph-focus-mobile';

// Viewport padding (≥16px from every edge) + inter-block gaps for the Mode-A block
// DAG and the fit decision. GAP/COL_GAP are the ≥22px min gaps; LAYER_GAP is the
// rank separation between adjacent layer blocks.
const FOCUS_PAD = 16;
const FOCUS_GAP = 22;
const FOCUS_COL_GAP = 22;
const FOCUS_LAYER_GAP = 64;

// Tween timings/easings (mirrors the app's spring idiom in view.ts / drag.ts).
const FOCUS_MS = 560;
const FOCUS_EASE = 'cubic-bezier(0.22, 1, 0.36, 1)'; // smooth settle
const SPRING_MS = 640;
const SPRING_EASE = 'cubic-bezier(0.34, 1.56, 0.64, 1)'; // overshoot rubber-band
const RANK_STAGGER_MS = 70; // per-rank delay → left→right cascade

// Focus DAG layout boxes are sized to the LARGEST rendered card (min floor 120px
// × up to 1.6 hub scale ≈ 192×268) plus slack, so the analytic per-rank spacing
// guarantees the ≥ min-gap between even the biggest tiles with no overlap.
const FOCUS_TILE_W = 200;
const FOCUS_TILE_H = 276;
// The min readable card floor (matches .graph-node.card --tile-base:120px and the
// MTG art aspect ratio) — the smallest cell the block-fit math ever stacks.
const FOCUS_MIN_CARD_W = 120;
const FOCUS_MIN_CARD_H = 167;
const FOCUS_MIN_SCALE = 1; // never shrink a card below its rendered (floored) size
const FOCUS_MAX_SCALE = 1.25; // gentle zoom-in to fill, never a hard blow-up

/** Overview state captured by overview-client so dismiss can rubber-band back. */
export interface OverviewSnapshot {
  /** Exact overview transform string per node id (translate + scale). */
  transforms: Map<string, string>;
  /** Overview pixel centres per node id (for redrawing overview edges). */
  centre: Map<string, { x: number; y: number }>;
  width: number;
  stageH: number;
}

interface RawNode {
  id: string;
  kind: 'card' | 'outcome' | 'trigger';
  name?: string;
  degree?: number;
  outcomeType?: string;
}
interface GraphData {
  nodes: RawNode[];
  edges: { source: string; target: string; kind: 'combo' | 'produces' | 'trigger'; comboId?: string }[];
  /** Optional Layer-0 trigger metadata (see layers.ts FocusTriggers). */
  triggers?: FocusTriggers & { fauxEdge?: Record<string, string[]> };
}

interface OutcomeType {
  id: string;
  label: string;
  icon: string;
  win: boolean;
}
interface ComboDetail {
  id: string;
  cards: string[];
  steps: string[];
  prerequisites: string[];
  produces: { name: string; status?: string; win?: boolean }[];
  outcomeTypes: string[];
  spellbookUrl: string;
}
interface DetailData {
  combos: ComboDetail[];
  outcomeTypes: OutcomeType[];
}

type Snapshot = () => OverviewSnapshot;

let focusedId: string | null = null;

function nodeEls(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.graph-node[data-node-id]'));
}

function readDetail(): DetailData | null {
  const el = document.getElementById('combo-detail-data');
  if (!el?.textContent) return null;
  try {
    return JSON.parse(el.textContent) as DetailData;
  } catch {
    return null;
  }
}

/** Escape untrusted text before injecting into SVG attributes. */
function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ------------------------------------------------------------------ *
 * Focus relayout (re-root on the clicked card)
 * ------------------------------------------------------------------ */

function applyFocus(clickedId: string, graph: GraphData, snap: OverviewSnapshot): void {
  const stage = document.getElementById('combo-stage');
  const svg = document.getElementById('edge-layer') as unknown as SVGSVGElement | null;
  if (!stage || !svg) return;

  const focusGraph: FocusGraph = {
    nodes: graph.nodes.map((n) => ({ id: n.id, kind: n.kind })),
    edges: graph.edges,
    triggers: graph.triggers,
  };
  const { ranks, layers, edges, nodeIds } = computeFocusLayout(
    clickedId,
    focusGraph,
    {
      nodeWidth: FOCUS_TILE_W,
      nodeHeight: FOCUS_TILE_H,
      ranksep: FOCUS_LAYER_GAP,
      nodesep: FOCUS_GAP,
    },
  );
  const focusSet = new Set(nodeIds);

  // Enabler cards in THIS focus that are themselves combo participants earn a
  // dotted faux-edge fading off to the LEFT (they chain further; already
  // clickable to re-root). Membership is the verified `fauxEdge` list per mechanic,
  // restricted to the enabler nodes actually present in Layer 0 of this focus.
  const fauxLeft = new Set<string>();
  if (graph.triggers) {
    const fauxNames = new Set<string>();
    for (const names of Object.values(graph.triggers.fauxEdge ?? {})) {
      for (const nm of names) fauxNames.add(`card:${nm}`);
    }
    const layer0Ids = new Set<string>();
    for (const e of edges) if (e.kind === 'trigger') layer0Ids.add(e.source);
    for (const id of layer0Ids) if (fauxNames.has(id)) fauxLeft.add(id);
  }

  // AMPLIFIER set for THIS focus: the amplifier cards of the clicked card's
  // mechanic(s) that are present in the focus (whether they landed in Layer 0 or
  // are already a combo partner). The renderer badges these tiles (×2) — even a
  // partner-rank amplifier keeps its tag (roles add, never subtract).
  const ampSet = new Set<string>();
  if (graph.triggers) {
    const clickedName = clickedId.replace(/^card:/, '');
    const mechs = graph.triggers.layer0Mechanics?.[clickedName] ?? [];
    const ampNames = new Set<string>();
    for (const m of mechs) {
      for (const amp of graph.triggers.amplifiers?.[m] ?? []) ampNames.add(`card:${amp.name}`);
    }
    for (const id of focusSet) if (ampNames.has(id) && id !== clickedId) ampSet.add(id);
  }

  // Common focus-mode chrome (shared by desktop + mobile).
  stage.dataset.mode = 'focus';
  document.body.dataset.comboMode = 'focus';
  const resetBtn = document.getElementById('combo-reset');
  if (resetBtn) resetBtn.hidden = false;
  focusedId = clickedId;

  // ── FIT DECISION (per focused card, at the CURRENT viewport). Measure each
  // layer's real card cell (max tile w/h in the layer — min-size enabler floods vs.
  // larger hub cards differ), then ask focus-fit whether the whole block DAG fits.
  // The stage's CSS height is the visible viewport (100vh − header/legend), but the
  // overview layout may GROW stage.style.height to fit its tall force graph. So the
  // true available HEIGHT is the visible slice — window.innerHeight minus the stage's
  // top offset (header + legend) — NOT the grown stage box; using the grown box would
  // let Mode A pick a layout taller than the viewport (requiring a scroll). Width is
  // never grown, so stage.clientWidth is the available width.
  const width = stage.clientWidth || snap.width;
  const stageTop = Math.max(0, stage.getBoundingClientRect().top);
  const height = Math.max(
    FOCUS_MIN_CARD_H + FOCUS_PAD * 2,
    (window.innerHeight || snap.stageH) - stageTop,
  );
  // Measure each node's real unscaled cell (offset box, floored at the min card so
  // even the smallest tile keeps its ≥120px readable footprint). Keep a per-node map
  // (used for an exact bbox) AND per-layer aggregates (max h + per-node widths in
  // fill order) so a lone hub card in a min-size flood widens only its own sub-column.
  const nodeDim = new Map<string, { w: number; h: number }>();
  const dimOf = (id: string): { w: number; h: number } => {
    const cached = nodeDim.get(id);
    if (cached) return cached;
    const el = document.querySelector<HTMLElement>(
      `.graph-node[data-node-id="${CSS.escape(id)}"]`,
    );
    const w = Math.max(FOCUS_MIN_CARD_W, el?.offsetWidth || 0);
    const h = Math.max(FOCUS_MIN_CARD_H, el?.offsetHeight || 0);
    const d = { w, h };
    nodeDim.set(id, d);
    return d;
  };
  const layerDims: LayerDim[] = layers.map((l) => {
    const widths = l.map((id) => dimOf(id).w);
    const cardW = widths.length ? Math.max(...widths) : FOCUS_MIN_CARD_W;
    const cardH = l.length
      ? Math.max(...l.map((id) => dimOf(id).h))
      : FOCUS_MIN_CARD_H;
    return { count: l.length, cardW, cardH, widths };
  });
  const availWfit = Math.max(FOCUS_MIN_CARD_W, width - FOCUS_PAD * 2);
  const availHfit = Math.max(FOCUS_MIN_CARD_H, height - FOCUS_PAD * 2);
  const fit = computeBlockFit(layerDims, {
    availW: availWfit,
    availH: availHfit,
    gap: FOCUS_GAP,
    colGap: FOCUS_COL_GAP,
    layerGap: FOCUS_LAYER_GAP,
  });

  // ── MODE B: the block DAG can't fit → page the LR DAG one rank-column at a time
  // (graph-focus-mobile, now un-gated from any viewport width — pure fit fallback).
  if (!fit.fits) {
    // The synthesized DAG edges are self-describing (buildFocusDag threads the
    // real comboId onto every combo AND produces edge), so use it DIRECTLY — no
    // global endpoint lookup, which could resolve to a combo the root isn't in.
    const outcomeTypeById = new Map(graph.nodes.map((n) => [n.id, n.outcomeType]));
    const edgeInfos = edges.map((e) => ({
      source: e.source,
      target: e.target,
      kind: e.kind,
      comboId: e.comboId,
      role: e.role,
      tint: outcomeTypeById.get(e.target) ?? 'other',
    }));
    const rankById = new Map<string, number>(
      nodeIds.map((id) => [id, ranks[id]?.rank ?? 0]),
    );
    const nameById = new Map<string, string>(
      graph.nodes.map((n) => [n.id, n.name ?? n.id.replace(/^card:|^outcome:/, '')]),
    );
    // Trigger-mechanic nodes aren't in graph.nodes — name them from the def so the
    // enlarge overlay / a11y have a label.
    for (const [mech, def] of Object.entries(graph.triggers?.mechanics ?? {})) {
      nameById.set(`trigger:${mech}`, def.label);
    }
    enterMobileFocus({
      stage,
      clickedId,
      columns: layers,
      nameById,
      edges: edgeInfos,
      rankById,
      fauxLeft,
      ampSet,
    });
    return;
  }

  // ── MODE A: block DAG. A prior Mode-B pager (e.g. before a resize-driven flip)
  // must be torn down first so the stage's scroll/height state is clean.
  if (isMobilePagerActive()) exitMobileFocus();

  // Analytic block coordinates (each layer a rectangular block; layers wrap into
  // sub-columns when tall) → fit-to-view into the stage viewport. Because the fit
  // check already proved it fits at min size, the scale floors at 1 (never shrinks
  // a card below its readable size) and only ever gently zooms UP to fill.
  const coords = computeBlockCoords(layers, layerDims, fit.perLayer, {
    gap: FOCUS_GAP,
    colGap: FOCUS_COL_GAP,
    layerGap: FOCUS_LAYER_GAP,
  });
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const id of nodeIds) {
    const c = coords[id];
    const { w, h } = dimOf(id);
    minX = Math.min(minX, c.x - w / 2);
    maxX = Math.max(maxX, c.x + w / 2);
    minY = Math.min(minY, c.y - h / 2);
    maxY = Math.max(maxY, c.y + h / 2);
  }
  const contentW = maxX - minX || 1;
  const contentH = maxY - minY || 1;
  const marginX = FOCUS_PAD; // ≥ viewport-edge padding
  const marginY = FOCUS_PAD;
  const availW = Math.max(FOCUS_MIN_CARD_W, width - marginX * 2);
  const availH = Math.max(FOCUS_MIN_CARD_H, height - marginY * 2);
  // Fit-to-view but FLOORED at the min card size: never scale below 1 (which would
  // shrink cards under their readable floor). The fit check guarantees it fits at 1.
  const fitScale = Math.min(availW / contentW, availH / contentH);
  const scale = Math.min(FOCUS_MAX_SCALE, Math.max(FOCUS_MIN_SCALE, fitScale));
  const drawnW = contentW * scale;
  const drawnH = contentH * scale;
  const offsetX = drawnW <= availW ? marginX + (availW - drawnW) / 2 : marginX;
  const offsetY = drawnH <= availH ? marginY + (availH - drawnH) / 2 : marginY;
  const focusStageH = Math.max(height, drawnH + marginY * 2);
  stage.style.height = `${focusStageH}px`;

  const focusCentre = new Map<string, { x: number; y: number }>();
  const focusHalfW = new Map<string, number>(); // on-screen half-width per node (for faux stubs)
  const map = (id: string): { x: number; y: number } => {
    const c = coords[id];
    return {
      x: offsetX + (c.x - minX) * scale,
      y: offsetY + (c.y - minY) * scale,
    };
  };

  // The clicked card's overview centre → the point non-focused nodes shrink into.
  const sink = snap.centre.get(clickedId) ?? { x: width / 2, y: height / 2 };

  for (const el of nodeEls()) {
    const id = el.dataset.nodeId!;
    const r = el.getBoundingClientRect();
    const w = r.width || FOCUS_TILE_W;
    const h = r.height || FOCUS_TILE_H;
    if (focusSet.has(id)) {
      const c = map(id);
      focusCentre.set(id, c);
      focusHalfW.set(id, (w * scale) / 2);
      const rank = ranks[id]?.rank ?? 0;
      el.style.transition = `transform ${FOCUS_MS}ms ${FOCUS_EASE}, opacity ${FOCUS_MS}ms ease`;
      el.style.transitionDelay = `${rank * RANK_STAGGER_MS}ms`;
      el.style.opacity = '1';
      el.style.pointerEvents = 'auto'; // Layer-0 tiles start inert; make them live.
      el.style.zIndex = id === clickedId ? '6' : '4';
      el.style.transform = `translate(${c.x - w / 2}px, ${c.y - h / 2}px) scale(${scale})`;
      el.dataset.focusState = id === clickedId ? 'root' : 'member';
      if (ampSet.has(id)) el.dataset.amp = 'true';
      else delete el.dataset.amp;
    } else {
      // Every node OUTSIDE this focus (Layer-0-only enablers that don't feed THIS
      // card, plus every overview node not in the focused component) is fully HIDDEN
      // + inert. Navigation happens only through the VISIBLE cards; hidden nodes must
      // never linger as a faded "ghost card" pile. We keep the transform so the tile
      // stays offscreen-collapsed under the clicked card (out of view, opacity 0),
      // never stacked visibly.
      el.style.transition = `opacity ${FOCUS_MS}ms ease`;
      el.style.transitionDelay = '0ms';
      el.style.opacity = '0';
      el.style.pointerEvents = 'none';
      el.style.zIndex = '0';
      el.style.transform = `translate(${sink.x - w / 2}px, ${sink.y - h / 2}px) scale(0.3)`;
      delete el.dataset.focusState;
      delete el.dataset.amp;
    }
  }

  drawFocusEdges(svg, graph, edges, focusCentre, width, focusStageH, fauxLeft, focusHalfW);
  stage.dataset.mode = 'focus';
  document.body.dataset.comboMode = 'focus';
  const reset = document.getElementById('combo-reset');
  if (reset) reset.hidden = false;
  focusedId = clickedId;
}

function drawFocusEdges(
  svg: SVGSVGElement,
  graph: GraphData,
  edges: { source: string; target: string; kind: 'combo' | 'produces' | 'trigger'; comboId?: string; role?: 'amplifier' }[],
  centre: Map<string, { x: number; y: number }>,
  width: number,
  height: number,
  fauxLeft: Set<string> = new Set(),
  halfW: Map<string, number> = new Map(),
): void {
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const tintFor = (id: string): string =>
    OUTCOME_COLORS[graph.nodes.find((n) => n.id === id)?.outcomeType ?? 'other'] ??
    OUTCOME_COLORS.other;

  // Each synthesized DAG edge is self-describing: buildFocusDag threads the real
  // comboId onto every combo AND produces edge. Use it DIRECTLY. The old global
  // `graph.edges.find(...)` fallback is REMOVED: 13 of 42 card→outcome endpoint
  // pairs belong to >1 combo, so the first global match could open a combo the
  // focus root isn't even in (e.g. Drogskol→draw resolving to the Queza combo).
  const comboIdFor = (e: { comboId?: string }): string | undefined => e.comboId;

  const usedTints = new Set<string>();
  for (const e of edges) {
    if (e.kind === 'produces') usedTints.add(graph.nodes.find((n) => n.id === e.target)?.outcomeType ?? 'other');
  }
  const defs = usedTints.size
    ? `<defs>${[...usedTints]
        .map((tint) => {
          const color = OUTCOME_COLORS[tint] ?? OUTCOME_COLORS.other;
          return (
            `<marker id="farrow-${tint}" viewBox="0 0 10 10" refX="9" refY="5" ` +
            `markerWidth="7" markerHeight="7" orient="auto-start-reverse">` +
            `<path d="M0,0 L10,5 L0,10 z" fill="${color}"/></marker>`
          );
        })
        .join('')}</defs>`
    : '';

  const paths = edges
    .map((e) => {
      const a = centre.get(e.source);
      const b = centre.get(e.target);
      if (!a || !b) return '';
      // Left→right cubic connector (horizontal control points read as a DAG flow).
      const dx = Math.max(30, (b.x - a.x) * 0.5);
      const d = `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
      const cid = comboIdFor(e);
      const hitAttr = cid ? ` data-combo-id="${esc(cid)}"` : '';
      // Transparent wide hit overlay so taps land on a thin line.
      const hit = cid
        ? `<path class="edge-hit"${hitAttr} d="${d}" fill="none" stroke="transparent" stroke-width="18" stroke-linecap="round"/>`
        : '';
      // Layer-0 → clicked-card feed: a neutral SOLID edge (not a combo line — no
      // hit overlay, no sheet). Signals "this trigger starts the focused card".
      if (e.kind === 'trigger') {
        // Amplifier feed → an amber dashed edge (distinct from the neutral enabler
        // feed) reinforcing the ×2 tile badge.
        if (e.role === 'amplifier') {
          return `<path class="edge edge-trigger edge-amplifier focus-edge" d="${d}" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-opacity="0.9" stroke-dasharray="7 4"/>`;
        }
        return `<path class="edge edge-trigger focus-edge" d="${d}" fill="none" stroke="#a1a1aa" stroke-width="2" stroke-opacity="0.65"/>`;
      }
      if (e.kind === 'combo') {
        return (
          `<path class="edge edge-combo focus-edge" d="${d}" fill="none" stroke="#71717a" stroke-width="2" stroke-opacity="0.8"/>` +
          hit
        );
      }
      const tint = graph.nodes.find((n) => n.id === e.target)?.outcomeType ?? 'other';
      const color = tintFor(e.target);
      return (
        `<path class="edge edge-produces focus-edge" d="${d}" fill="none" stroke="${color}" stroke-width="2.5" stroke-opacity="0.9" marker-end="url(#farrow-${tint})"/>` +
        hit
      );
    })
    .join('');

  // Dotted faux-edges: a short dashed stub from a combo-participant enabler's LEFT
  // edge fading off into the left margin — it chains further left (already clickable
  // to re-root). Purely decorative (no hit target).
  const STUB = 46; // px the stub extends left of the tile
  const fauxDefs =
    fauxLeft.size > 0
      ? `<linearGradient id="faux-fade" x1="1" y1="0" x2="0" y2="0">` +
        `<stop offset="0" stop-color="#c4b5fd" stop-opacity="0.85"/>` +
        `<stop offset="1" stop-color="#c4b5fd" stop-opacity="0"/></linearGradient>`
      : '';
  const fauxPaths = [...fauxLeft]
    .map((id) => {
      const c = centre.get(id);
      if (!c) return '';
      const hw = halfW.get(id) ?? FOCUS_TILE_W / 2;
      const x0 = c.x - hw; // tile's left edge
      const x1 = x0 - STUB;
      return `<path class="edge faux-edge" d="M${x0},${c.y} L${x1},${c.y}" fill="none" stroke="url(#faux-fade)" stroke-width="2.5" stroke-dasharray="2 5" stroke-linecap="round"/>`;
    })
    .join('');

  svg.innerHTML = `<defs>${defs.replace(/^<defs>|<\/defs>$/g, '')}${fauxDefs}</defs>` + paths + fauxPaths;
}

/* ------------------------------------------------------------------ *
 * Dismiss — rubber-band back to the exact overview.
 * ------------------------------------------------------------------ */

let getSnapshot: Snapshot | null = null;
let currentGraph: GraphData | null = null;
let redrawOverviewEdges: (() => void) | null = null;

function dismissFocus(): void {
  if (!focusedId || !getSnapshot) return;
  if (isMobilePagerActive()) exitMobileFocus();
  const snap = getSnapshot();

  for (const el of nodeEls()) {
    const id = el.dataset.nodeId!;
    const t = snap.transforms.get(id);
    el.style.transition = `transform ${SPRING_MS}ms ${SPRING_EASE}, opacity ${FOCUS_MS}ms ease`;
    el.style.transitionDelay = '0ms';
    if (el.dataset.layer0 === 'true') {
      // Layer-0-only tiles have no overview slot — keep them hidden + inert.
      el.style.opacity = '0';
      el.style.pointerEvents = 'none';
    } else {
      el.style.opacity = '1';
      el.style.pointerEvents = '';
    }
    el.style.zIndex = el.dataset.nodeKind === 'outcome' ? '2' : '1';
    if (t) el.style.transform = t;
    delete el.dataset.focusState;
    delete el.dataset.amp;
  }

  const stage = document.getElementById('combo-stage');
  if (stage) {
    delete stage.dataset.mode;
    // Restore the overview stage height (focus may have grown it for vertical scroll).
    stage.style.height = `${snap.stageH}px`;
    stage.scrollTop = 0;
  }
  delete document.body.dataset.comboMode;
  const reset = document.getElementById('combo-reset');
  if (reset) reset.hidden = true;
  focusedId = null;
  redrawOverviewEdges?.();
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ *
 * Desktop hover-enlarge — a floating readable preview of the hovered card,
 * mirroring the deck-diff screen's "hover raises the card to read it" idiom.
 * It's a fixed overlay (pointer-events:none) so it never clips against the
 * stage's overflow and never blocks the click-to-re-root interaction.
 * ------------------------------------------------------------------ */

function initCardZoom(): void {
  const stage = document.getElementById('combo-stage');
  if (!stage) return;
  let overlay: HTMLElement | null = null;
  let current: HTMLElement | null = null;

  const ensure = (): HTMLElement => {
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'combo-card-zoom';
    overlay.dataset.show = 'false';
    overlay.innerHTML = '<img alt="" />';
    document.body.appendChild(overlay);
    return overlay;
  };

  const show = (node: HTMLElement): void => {
    if (isMobilePagerActive()) return; // pager (Mode B) uses the ⤢ button, never hover
    const img = node.querySelector<HTMLImageElement>('img.card-img');
    if (!img?.src) return;
    const el = ensure();
    const im = el.querySelector('img')!;
    im.src = img.src;
    const r = node.getBoundingClientRect();
    const PREVIEW_W = 300;
    const PREVIEW_H = PREVIEW_W * (680 / 488);
    // Prefer the right of the card; flip left if it would overflow the viewport.
    let left = r.right + 16;
    if (left + PREVIEW_W > window.innerWidth - 8) left = r.left - PREVIEW_W - 16;
    if (left < 8) left = Math.max(8, (window.innerWidth - PREVIEW_W) / 2);
    let top = r.top + r.height / 2 - PREVIEW_H / 2;
    top = Math.max(8, Math.min(top, window.innerHeight - PREVIEW_H - 8));
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    el.dataset.show = 'true';
  };
  const hide = (): void => {
    if (overlay) overlay.dataset.show = 'false';
  };

  stage.addEventListener('mouseover', (ev) => {
    const node = (ev.target as HTMLElement).closest<HTMLElement>('.graph-node[data-node-kind="card"]');
    if (node && node !== current) {
      current = node;
      show(node);
    }
  });
  stage.addEventListener('mouseout', (ev) => {
    const node = (ev.target as HTMLElement).closest<HTMLElement>('.graph-node[data-node-kind="card"]');
    const to = ev.relatedTarget as Node | null;
    if (node && (!to || !node.contains(to))) {
      current = null;
      hide();
    }
  });
}

export function initFocus(graph: GraphData, snapshot: Snapshot, onRestoreEdges: () => void): void {
  getSnapshot = snapshot;
  currentGraph = graph;
  redrawOverviewEdges = onRestoreEdges;
  initSheet(readDetail(), () => {
    /* sheet closed → focus/graph already underneath, nothing to restore */
  });
  initMobilePager();
  initCardZoom();

  // Re-apply the focused card's layout on resize: the fit decision is viewport-
  // dependent, so a resize can FLIP the mode (Mode A block DAG ⇄ Mode B pager).
  // applyFocus recomputes the fit and tears down the pager when flipping to A.
  let resizeRaf = 0;
  window.addEventListener('resize', () => {
    if (!focusedId || !currentGraph || !getSnapshot) return;
    cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(() => {
      if (focusedId && currentGraph && getSnapshot) applyFocus(focusedId, currentGraph, getSnapshot());
    });
  });

  document.getElementById('combo-stage')?.addEventListener('click', (ev) => {
    const target = ev.target as HTMLElement;

    // 1. EDGE hit → open the one-line bottom sheet (overlay; never dismiss focus).
    const hit = target.closest<SVGElement>('[data-combo-id]');
    if (hit) {
      const cid = (hit as unknown as { dataset: DOMStringMap }).dataset.comboId;
      if (cid) {
        openSheet(cid);
        ev.stopPropagation();
        return;
      }
    }

    // 2. CARD node → re-root the focused DAG on it (overview OR already-focused).
    const node = target.closest<HTMLElement>('.graph-node[data-node-kind="card"]');
    if (node) {
      const id = node.dataset.nodeId!;
      if (focusedId === id) return; // already rooted here
      if (!currentGraph || !getSnapshot) return;
      applyFocus(id, currentGraph, getSnapshot());
      ev.stopPropagation();
      return;
    }

    // 3. Empty stage space while focused → reset to the overview.
    if (focusedId) dismissFocus();
  });

  // ⤺ Overview button.
  document.getElementById('combo-reset')?.addEventListener('click', (ev) => {
    ev.stopPropagation();
    dismissFocus();
  });

  // Esc: close the sheet first if it's open, else dismiss focus.
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    if (isSheetOpen()) {
      closeSheet();
      return;
    }
    if (focusedId) dismissFocus();
  });
}
