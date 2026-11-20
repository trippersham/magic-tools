// Column-pager for the focus view (Phase 6; generalized in Phase 7 as Mode B).
//
// This is the fit-aware FALLBACK: whenever the block DAG (Mode A) can't fit the
// current viewport — a tiny phone, OR a desktop focus too wide/flooded to fit —
// graph-focus.ts routes here instead. It is no longer gated on a max-width media
// query; the mode is decided purely by the focus-fit check.
//
// When the full left→right DAG is wider than the screen and its outcome column
// clips off, rather than rotate the layout (rejected), we KEEP
// the LR orientation and PAGE it: the DAG's ranks become paginated columns and we
// show exactly ONE column at a time — its card(s) rendered full-size, centred
// horizontally, stacked vertically and scrolling vertically WITHIN the layer (the
// stage owns the vertical scroll; the body never grows sideways). ‹ › arrow
// buttons and a left/right swipe page between columns; a small "n / m" indicator
// shows position. Tapping a card still re-roots (graph-focus.ts rebuilds the DAG
// and calls enterMobileFocus again at column 0).
//
// Because only one column shows, we render the REAL DAG edges — the same SVG
// edge + `.edge-hit` system as desktop — WINDOWED to the current column: for each
// visible card we draw its combo/produces edges as real line segments extending
// from the card's LEFT or RIGHT edge toward the direction of the connected
// neighbour column (left = previous column, right = next/outcome column). The far
// end sits off toward the screen edge (that neighbour column is paged out) and
// clipping there is fine. Each segment carries the same `data-combo-id` hit
// overlay, so tapping it opens the one-line bottom sheet via the identical stage
// handler as desktop edge taps.
//
// The column source is layers.ts (`computeFocusLayout(...).layers` = nodeId[][] by
// rank) — reused as-is, threaded in via the context; this module never touches the
// pure graph math or `window` inside it.

import { OUTCOME_COLORS } from './overview-client';

const PAGE_MS = 420;
const PAGE_EASE = 'cubic-bezier(0.22, 1, 0.36, 1)';
const GAP = 22; // vertical gap between stacked cards in a column
const PAD_TOP = 18;
const SWIPE_THRESHOLD = 8; // px before a press is judged a swipe
const SWIPE_COMMIT = 56; // px horizontal travel that commits a page

/** A focus-DAG edge enriched for windowed mobile rendering. */
export interface FocusEdgeInfo {
  source: string;
  target: string;
  kind: 'combo' | 'produces' | 'trigger';
  comboId?: string;
  /** Set on a Layer-0 trigger edge whose source AMPLIFIES the mechanic (×2). */
  role?: 'amplifier';
  /** outcome-type of the produces target (drives the tint + arrow marker). */
  tint: string;
}

export interface MobileFocusCtx {
  stage: HTMLElement;
  clickedId: string;
  /** Rank-ordered columns (layers.ts `layers`). */
  columns: string[][];
  /** id → display name (for the enlarge overlay alt text). */
  nameById: Map<string, string>;
  /** The focus DAG's edges (real lines are drawn from these, windowed per column). */
  edges: FocusEdgeInfo[];
  /** id → rank index, so an edge's direction (left/right) can be resolved. */
  rankById: Map<string, number>;
  /** Layer-0 enabler ids that chain further left (dotted faux-edge). */
  fauxLeft?: Set<string>;
  /** Node ids to badge as amplifiers (×2) of the clicked card's mechanic(s). */
  ampSet?: Set<string>;
}

interface State extends MobileFocusCtx {
  col: number;
  prevOverflow: string;
  prevHeight: string;
}

let state: State | null = null;
let wired = false;
let lastSwipeEnd = 0;

function nodeEls(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.graph-node[data-node-id]'));
}

function pagerEls(): {
  pager: HTMLElement | null;
  prev: HTMLElement | null;
  next: HTMLElement | null;
  ind: HTMLElement | null;
} {
  return {
    pager: document.getElementById('combo-pager'),
    prev: document.getElementById('combo-prev'),
    next: document.getElementById('combo-next'),
    ind: document.getElementById('combo-pager-ind'),
  };
}

function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Strip our injected overlays (enlarge button) from every node. */
function clearOverlays(): void {
  document.querySelectorAll('.mobile-zoom-btn').forEach((el) => el.remove());
}

function shortName(id: string): string {
  const n = state?.nameById.get(id) ?? id.replace(/^card:|^outcome:/, '');
  return n;
}

/** Add the ⤢ enlarge button to one visible card node (tap = re-root, so enlarge
 *  needs its own button). Combo lines are drawn as REAL SVG edges, not chips. */
function decorate(el: HTMLElement): void {
  if (!state) return;
  const id = el.dataset.nodeId!;
  if (el.dataset.nodeKind !== 'card') return;
  const zoom = document.createElement('button');
  zoom.type = 'button';
  zoom.className = 'mobile-zoom-btn';
  zoom.setAttribute('aria-label', 'Enlarge card');
  zoom.textContent = '⤢';
  zoom.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const img = el.querySelector<HTMLImageElement>('img.card-img');
    if (img?.src) openZoom(img.src, shortName(id));
  });
  el.appendChild(zoom);
}

/* ------------------------------------------------------------------ *
 * Enlarge overlay (mobile)
 * ------------------------------------------------------------------ */

let zoomOverlay: HTMLElement | null = null;
function ensureZoom(): HTMLElement {
  if (zoomOverlay) return zoomOverlay;
  const el = document.createElement('div');
  el.id = 'combo-zoom-overlay';
  el.dataset.open = 'false';
  el.innerHTML = '<img alt="" />';
  el.addEventListener('click', () => closeZoom());
  document.body.appendChild(el);
  zoomOverlay = el;
  return el;
}
function openZoom(src: string, alt: string): void {
  const el = ensureZoom();
  const img = el.querySelector('img')!;
  img.src = src;
  img.alt = alt;
  el.dataset.open = 'true';
}
function closeZoom(): void {
  if (zoomOverlay) zoomOverlay.dataset.open = 'false';
}

/* ------------------------------------------------------------------ *
 * Column render
 * ------------------------------------------------------------------ */

function renderColumn(index: number): void {
  if (!state) return;
  const { stage, columns, clickedId } = state;
  state.col = index;
  clearOverlays();

  const visible = new Set(columns[index] ?? []);
  const centreX = (stage.clientWidth || window.innerWidth) / 2;
  const viewportH = stage.clientHeight || window.innerHeight;

  // Measure layout heights (offsetHeight is pre-transform, so it is stable
  // regardless of any scale we apply). Sum to decide vertical centring vs scroll.
  const ordered = (columns[index] ?? []).filter((id) =>
    document.querySelector(`.graph-node[data-node-id="${CSS.escape(id)}"]`),
  );
  const measured = ordered.map((id) => {
    const el = document.querySelector<HTMLElement>(`.graph-node[data-node-id="${CSS.escape(id)}"]`)!;
    return { id, el, w: el.offsetWidth, h: el.offsetHeight };
  });
  const total = measured.reduce((s, m) => s + m.h, 0) + GAP * Math.max(0, measured.length - 1);
  const startY = total < viewportH ? Math.max(PAD_TOP, (viewportH - total) / 2) : PAD_TOP;

  for (const el of nodeEls()) {
    el.style.transition = `transform ${PAGE_MS}ms ${PAGE_EASE}, opacity ${PAGE_MS}ms ease`;
    el.style.transitionDelay = '0ms';
    if (!visible.has(el.dataset.nodeId!)) {
      // Park hidden nodes CENTRED (like the visible cards: x = centreX - w/2) and
      // invisible + inert. Centring on their own box keeps even the widest parked
      // node (a full outcome pill) inside [0, vw] — parking at a bare `centreX`
      // let a wide node's right edge bleed ~8px past the viewport, which the
      // stage's overflow-x:hidden masked but still grew scrollWidth past clientWidth.
      const pw = el.offsetWidth || 0;
      el.style.opacity = '0';
      el.style.pointerEvents = 'none';
      el.style.zIndex = '0';
      el.style.transform = `translate(${centreX - pw / 2}px, 0px) scale(0.6)`;
      delete el.dataset.focusState;
      delete el.dataset.amp;
    }
  }

  const pos = new Map<string, { x: number; y: number; w: number; h: number }>();
  let y = startY;
  for (const m of measured) {
    const x = centreX - m.w / 2;
    m.el.style.opacity = '1';
    m.el.style.pointerEvents = 'auto';
    m.el.style.zIndex = m.id === clickedId ? '6' : '5';
    m.el.style.transform = `translate(${x}px, ${y}px) scale(1)`;
    m.el.dataset.focusState = m.id === clickedId ? 'root' : 'member';
    if (state.ampSet?.has(m.id)) m.el.dataset.amp = 'true';
    else delete m.el.dataset.amp;
    decorate(m.el);
    pos.set(m.id, { x, y, w: m.w, h: m.h });
    y += m.h + GAP;
  }

  // Draw the REAL DAG edges for this column, windowed to the viewport.
  const contentH = Math.max(viewportH, y - GAP + PAD_TOP);
  drawMobileEdges(index, pos, stage.clientWidth || window.innerWidth, contentH);

  // Update arrows + indicator.
  const { pager, prev, next, ind } = pagerEls();
  if (pager) pager.hidden = false;
  if (prev) prev.toggleAttribute('hidden', index === 0);
  if (next) next.toggleAttribute('hidden', index >= columns.length - 1);
  if (ind) ind.textContent = `${index + 1} / ${columns.length}`;

  stage.scrollTop = 0;
}

/* ------------------------------------------------------------------ *
 * Windowed edge rendering — the SAME SVG edge + `.edge-hit` system as desktop,
 * but each visible card's edges are drawn as a real segment running from the
 * card's LEFT/RIGHT edge toward the paged-out neighbour column (far end off the
 * screen edge). Segments start at the card's side edge, never across the art.
 * ------------------------------------------------------------------ */

const EDGE_INSET = 6; // px from the viewport edge the far end sits at
const FAN_GAP = 30; // vertical spread between multiple same-side edges of a card

function drawMobileEdges(
  index: number,
  pos: Map<string, { x: number; y: number; w: number; h: number }>,
  vw: number,
  contentH: number,
): void {
  const svg = document.getElementById('edge-layer') as unknown as SVGSVGElement | null;
  if (!svg || !state) return;
  const { edges, rankById } = state;

  const tintColor = (t: string): string => OUTCOME_COLORS[t] ?? OUTCOME_COLORS.other;

  // Incident edges per visible node, so we can fan multiple same-side lines out.
  interface Seg {
    id: string;
    dir: -1 | 1;
    e: FocusEdgeInfo;
    visibleIsSource: boolean;
  }
  const byNodeDir = new Map<string, Seg[]>();
  for (const e of edges) {
    for (const [nodeId, isSource] of [
      [e.source, true],
      [e.target, false],
    ] as [string, boolean][]) {
      if (!pos.has(nodeId)) continue;
      const other = isSource ? e.target : e.source;
      const otherRank = rankById.get(other) ?? index;
      const dir: -1 | 1 = otherRank < index ? -1 : otherRank > index ? 1 : isSource ? 1 : -1;
      const key = `${nodeId}|${dir}`;
      if (!byNodeDir.has(key)) byNodeDir.set(key, []);
      byNodeDir.get(key)!.push({ id: nodeId, dir, e, visibleIsSource: isSource });
    }
  }

  const usedTints = new Set<string>();
  const parts: string[] = [];
  for (const [, segs] of byNodeDir) {
    const p = pos.get(segs[0].id)!;
    const cnt = segs.length;
    segs.forEach((seg, i) => {
      const dir = seg.dir;
      const nearX = dir < 0 ? p.x : p.x + p.w;
      const nearY = p.y + p.h / 2;
      const farX = dir < 0 ? EDGE_INSET : vw - EDGE_INSET;
      const farY = nearY + (i - (cnt - 1) / 2) * FAN_GAP;
      // produces arrow must point AT the outcome (edge target): if the visible
      // node is the source (card), the arrow is at the far end; if the visible
      // node is the outcome, draw far→near so the arrow lands on it.
      let ax: number;
      let ay: number;
      let bx: number;
      let by: number;
      if (seg.e.kind === 'produces' && !seg.visibleIsSource) {
        ax = farX; ay = farY; bx = nearX; by = nearY;
      } else {
        ax = nearX; ay = nearY; bx = farX; by = farY;
      }
      const dx = Math.max(24, Math.abs(bx - ax) * 0.5) * (bx >= ax ? 1 : -1);
      const d = `M${ax},${ay} C${ax + dx},${ay} ${bx - dx},${by} ${bx},${by}`;
      const cid = seg.e.comboId;
      const hit = cid
        ? `<path class="edge-hit" data-combo-id="${esc(cid)}" d="${d}" fill="none" stroke="transparent" stroke-width="20" stroke-linecap="round"/>`
        : '';
      if (seg.e.kind === 'trigger') {
        // Layer-0 → clicked feed: neutral solid (enabler) or amber dashed (amplifier).
        if (seg.e.role === 'amplifier') {
          parts.push(
            `<path class="edge edge-trigger edge-amplifier focus-edge" d="${d}" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-opacity="0.9" stroke-dasharray="7 4"/>`,
          );
        } else {
          parts.push(
            `<path class="edge edge-trigger focus-edge" d="${d}" fill="none" stroke="#a1a1aa" stroke-width="2" stroke-opacity="0.7"/>`,
          );
        }
      } else if (seg.e.kind === 'combo') {
        parts.push(
          `<path class="edge edge-combo focus-edge" d="${d}" fill="none" stroke="#71717a" stroke-width="2" stroke-opacity="0.85"/>` +
            hit,
        );
      } else {
        const tint = seg.e.tint;
        usedTints.add(tint);
        const color = tintColor(tint);
        parts.push(
          `<path class="edge edge-produces focus-edge" d="${d}" fill="none" stroke="${color}" stroke-width="2.5" stroke-opacity="0.95" marker-end="url(#mfarrow-${esc(tint)})"/>` +
            hit,
        );
      }
    });
  }

  // Dotted faux-edges for visible combo-participant enablers — a short dashed stub
  // fading off to the LEFT (chains further; already clickable to re-root).
  const fauxLeft = state.fauxLeft ?? new Set<string>();
  const STUB = 42;
  let fauxDefs = '';
  const fauxParts: string[] = [];
  for (const [id, p] of pos) {
    if (!fauxLeft.has(id)) continue;
    const y0 = p.y + p.h / 2;
    const x0 = p.x;
    fauxParts.push(
      `<path class="edge faux-edge" d="M${x0},${y0} L${Math.max(EDGE_INSET, x0 - STUB)},${y0}" fill="none" stroke="url(#mfaux-fade)" stroke-width="2.5" stroke-dasharray="2 5" stroke-linecap="round"/>`,
    );
  }
  if (fauxParts.length) {
    fauxDefs =
      `<linearGradient id="mfaux-fade" x1="1" y1="0" x2="0" y2="0">` +
      `<stop offset="0" stop-color="#c4b5fd" stop-opacity="0.85"/>` +
      `<stop offset="1" stop-color="#c4b5fd" stop-opacity="0"/></linearGradient>`;
  }

  const markerDefs = [...usedTints]
    .map((tint) => {
      const color = tintColor(tint);
      return (
        `<marker id="mfarrow-${esc(tint)}" viewBox="0 0 10 10" refX="9" refY="5" ` +
        `markerWidth="7" markerHeight="7" orient="auto-start-reverse">` +
        `<path d="M0,0 L10,5 L0,10 z" fill="${color}"/></marker>`
      );
    })
    .join('');
  const defs =
    markerDefs || fauxDefs ? `<defs>${markerDefs}${fauxDefs}</defs>` : '';

  svg.setAttribute('viewBox', `0 0 ${vw} ${contentH}`);
  svg.setAttribute('width', String(vw));
  svg.setAttribute('height', String(contentH));
  svg.style.width = `${vw}px`;
  svg.style.height = `${contentH}px`;
  svg.innerHTML = defs + parts.join('') + fauxParts.join('');
}

function pageBy(delta: number): void {
  if (!state) return;
  const next = state.col + delta;
  if (next < 0 || next >= state.columns.length) return;
  renderColumn(next);
}

/* ------------------------------------------------------------------ *
 * Enter / exit
 * ------------------------------------------------------------------ */

export function enterMobileFocus(ctx: MobileFocusCtx): void {
  const prevOverflow = ctx.stage.style.overflow;
  const prevHeight = ctx.stage.style.height;
  // Stage becomes the vertical scroll container for the current layer; horizontal
  // paging is via buttons/swipe, never native horizontal scroll of the body.
  ctx.stage.style.overflowX = 'hidden';
  ctx.stage.style.overflowY = 'auto';
  ctx.stage.style.height = ''; // revert to the fixed CSS viewport height
  ctx.stage.dataset.mobilePager = 'true';

  state = { ...ctx, col: 0, prevOverflow, prevHeight };
  renderColumn(0);
}

export function exitMobileFocus(): void {
  clearOverlays();
  closeZoom();
  const { pager } = pagerEls();
  if (pager) pager.hidden = true;
  if (state) {
    state.stage.style.overflow = state.prevOverflow;
    state.stage.style.overflowX = '';
    state.stage.style.overflowY = '';
    state.stage.style.height = state.prevHeight;
    delete state.stage.dataset.mobilePager;
    state.stage.scrollTop = 0;
  }
  // Reset the inline SVG sizing we applied for the windowed edges so the overview
  // redraw (graph-focus.ts) reclaims the full stage coordinate space.
  const svg = document.getElementById('edge-layer');
  if (svg) {
    svg.style.width = '';
    svg.style.height = '';
  }
  state = null;
}

export function isMobilePagerActive(): boolean {
  return state !== null;
}

/** True briefly after a horizontal swipe so the ensuing click is suppressed. */
export function justSwiped(): boolean {
  return performance.now() - lastSwipeEnd < 320;
}

/* ------------------------------------------------------------------ *
 * Wiring: arrow buttons + horizontal swipe (Pointer Events, mirrors drag.ts).
 * ------------------------------------------------------------------ */

interface Swipe {
  startX: number;
  startY: number;
  active: boolean;
  pointerId: number;
}
let swipe: Swipe | null = null;

export function initMobilePager(): void {
  if (wired) return;
  wired = true;

  document.getElementById('combo-prev')?.addEventListener('click', (ev) => {
    ev.stopPropagation();
    pageBy(-1);
  });
  document.getElementById('combo-next')?.addEventListener('click', (ev) => {
    ev.stopPropagation();
    pageBy(1);
  });

  const stage = document.getElementById('combo-stage');
  if (stage) {
    stage.addEventListener('pointerdown', (e) => {
      if (!state) return;
      swipe = { startX: e.clientX, startY: e.clientY, active: false, pointerId: e.pointerId };
    });
    stage.addEventListener('pointermove', (e) => {
      if (!swipe) return;
      const dx = e.clientX - swipe.startX;
      const dy = e.clientY - swipe.startY;
      if (!swipe.active) {
        if (Math.hypot(dx, dy) < SWIPE_THRESHOLD) return;
        if (Math.abs(dx) <= Math.abs(dy)) {
          swipe = null; // vertical → let the layer scroll
          return;
        }
        swipe.active = true;
      }
    });
    const end = (e: PointerEvent): void => {
      if (!swipe) return;
      const dx = e.clientX - swipe.startX;
      const wasActive = swipe.active;
      swipe = null;
      if (wasActive) {
        lastSwipeEnd = performance.now();
        if (Math.abs(dx) >= SWIPE_COMMIT) pageBy(dx < 0 ? 1 : -1);
      }
    };
    stage.addEventListener('pointerup', end);
    stage.addEventListener('pointercancel', end);

    // Suppress the click that trails a real swipe (so it doesn't re-root a card).
    stage.addEventListener(
      'click',
      (ev) => {
        if (justSwiped()) {
          ev.stopImmediatePropagation();
          ev.preventDefault();
        }
      },
      true,
    );
  }

  // Re-centre the current column on resize/orientation change.
  window.addEventListener('resize', () => {
    if (state) renderColumn(state.col);
  });

  // Esc closes the enlarge overlay first if it's open.
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && zoomOverlay?.dataset.open === 'true') {
      closeZoom();
      ev.stopPropagation();
    }
  });
}
