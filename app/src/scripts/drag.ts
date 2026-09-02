// Drag-to-tag controller (v2.6 / v2.8). Pointer Events (mouse + touch): press a
// card and drag; the page fades, a radial donut of edge targets appears with a
// dead-center no-op, and the release lands the pointer in a segment whose action
// drives a pure curation transition. Presentation only — logic is in curation.ts.

import {
  donutSegments,
  hitTest,
  applyAction,
  type Action,
  type Membership,
  type Segment,
  type DonutGeometry,
} from '../lib/curation';
import { getMembership, setMembership, setNote, getNote } from './store';
import { suppressPaneForDrag } from './view';

const START_THRESHOLD = 6; // px before a press becomes a drag

/** Donut geometry, sized to the viewport: the empty center (deadzone) hole has a
 *  diameter of 50% of the window's smaller dimension. */
function geo(): DonutGeometry {
  return { radius: 200, deadzone: 0.25 * Math.min(window.innerWidth, window.innerHeight) };
}

interface DragCtx {
  card: HTMLElement;
  name: string;
  membership: Membership;
  startX: number;
  startY: number;
  cx: number;
  cy: number;
  pointerId: number;
  active: boolean;
}

let ctx: DragCtx | null = null;

function center(): { x: number; y: number } {
  return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
}

function onPointerDown(e: PointerEvent): void {
  if (e.button !== 0 && e.pointerType === 'mouse') return;
  const card = (e.target as HTMLElement).closest<HTMLElement>('.card[data-name]');
  if (!card) return;
  const name = card.dataset.name!;
  const membership = getMembership(name);
  if (donutSegments(membership).length === 0) return; // dismissed / nothing to do

  const c = center();
  ctx = {
    card,
    name,
    membership,
    startX: e.clientX,
    startY: e.clientY,
    cx: c.x,
    cy: c.y,
    pointerId: e.pointerId,
    active: false,
  };
  window.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointercancel', onPointerCancel);
}

function beginDrag(): void {
  if (!ctx) return;
  ctx.active = true;
  document.body.dataset.dragging = 'true';
  suppressPaneForDrag();
  ctx.card.classList.add('is-dragging');
  try {
    ctx.card.setPointerCapture(ctx.pointerId);
  } catch {
    /* ignore */
  }
  renderDonut(ctx.membership);
}

function onPointerMove(e: PointerEvent): void {
  if (!ctx) return;
  const dxs = e.clientX - ctx.startX;
  const dys = e.clientY - ctx.startY;
  if (!ctx.active) {
    if (Math.hypot(dxs, dys) < START_THRESHOLD) return;
    beginDrag();
  }
  // Move the card visually with the pointer.
  ctx.card.style.transform = `translate(${dxs}px, ${dys}px) scale(1.04)`;
  // Highlight the hovered segment (pointer relative to donut center).
  const action = hitTest(ctx.membership, e.clientX - ctx.cx, e.clientY - ctx.cy, geo());
  highlight(action);
}

function onPointerUp(e: PointerEvent): void {
  if (!ctx) return;
  const cur = ctx;
  const wasActive = cur.active;
  cleanupListeners();

  if (!wasActive) {
    resetCard(cur.card);
    ctx = null;
    return;
  }

  const action = hitTest(cur.membership, e.clientX - cur.cx, e.clientY - cur.cy, geo());
  endDragVisuals(cur.card);
  ctx = null;

  if (!action) return; // dead-center / outside -> cancel, no-op

  cur.card.dataset.dragMoved = 'true'; // suppress the trailing click->pane
  if (action === 'annotate') {
    openNote(cur.name);
    return;
  }
  const result = applyAction(cur.membership, action);
  setMembership(cur.name, result.membership);
}

function onPointerCancel(): void {
  if (!ctx) return;
  const cur = ctx;
  cleanupListeners();
  endDragVisuals(cur.card);
  ctx = null;
}

function cleanupListeners(): void {
  window.removeEventListener('pointermove', onPointerMove);
  window.removeEventListener('pointerup', onPointerUp);
  window.removeEventListener('pointercancel', onPointerCancel);
}

function endDragVisuals(card: HTMLElement): void {
  document.body.dataset.dragging = '';
  hideDonut();
  resetCard(card);
}

function resetCard(card: HTMLElement): void {
  card.classList.remove('is-dragging');
  card.style.transform = '';
}

/* ------------------------------------------------------------------ *
 * Donut rendering — a full-screen SVG annulus: wedge segments sweep from
 * the empty dead-center hole out to the window edges.
 * ------------------------------------------------------------------ */

// Angular span (radians, screen coords: 0 = +x/right, +y = down, clockwise) for
// each region — matching hitTest's boundaries exactly so the wedge the pointer
// sees is the wedge that fires.
const REGION_ARC: Record<string, [number, number]> = {
  right: [-Math.PI / 2, Math.PI / 2],
  left: [Math.PI / 2, (3 * Math.PI) / 2],
  bottom: [0, Math.PI],
  'top-left': [Math.PI, (3 * Math.PI) / 2],
  'top-right': [(3 * Math.PI) / 2, 2 * Math.PI],
};

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Annulus-sector path from innerR..outerR spanning [a0, a1] about (cx, cy). */
function sectorPath(
  cx: number,
  cy: number,
  ri: number,
  ro: number,
  a0: number,
  a1: number,
): string {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const pt = (r: number, a: number) => `${cx + r * Math.cos(a)},${cy + r * Math.sin(a)}`;
  return (
    `M${pt(ro, a0)} A${ro},${ro} 0 ${large} 1 ${pt(ro, a1)} ` +
    `L${pt(ri, a1)} A${ri},${ri} 0 ${large} 0 ${pt(ri, a0)} Z`
  );
}

function renderDonut(membership: Membership): void {
  const donut = document.getElementById('drag-donut');
  if (!donut) return;
  const segs = donutSegments(membership);
  const c = center();
  const w = window.innerWidth;
  const h = window.innerHeight;
  const ri = geo().deadzone;
  // Outer radius exceeds the farthest corner so the ring bleeds to every edge.
  const ro = Math.hypot(w, h);
  // Labels sit midway between the hole edge and the nearest window edge.
  const labelR = ri + (Math.min(w, h) / 2 - ri) / 2;

  const wedges = segs
    .map((s: Segment) => {
      const [a0, a1] = REGION_ARC[s.region];
      const mid = (a0 + a1) / 2;
      const lx = c.x + labelR * Math.cos(mid);
      const ly = c.y + labelR * Math.sin(mid);
      return (
        `<path class="donut-wedge seg-${s.color}" data-action="${s.action}" d="${sectorPath(c.x, c.y, ri, ro, a0, a1)}"/>` +
        `<text class="donut-label" data-action="${s.action}" x="${lx}" y="${ly}">${s.label}</text>`
      );
    })
    .join('');

  donut.innerHTML =
    `<svg class="donut-svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" xmlns="${SVG_NS}">` +
    wedges +
    `<circle class="donut-hole" cx="${c.x}" cy="${c.y}" r="${ri}"/>` +
    `<text class="donut-cancel" x="${c.x}" y="${c.y}">cancel</text>` +
    `</svg>`;
  donut.hidden = false;
}

function highlight(action: Action | null): void {
  const donut = document.getElementById('drag-donut');
  if (!donut) return;
  donut.querySelectorAll<SVGElement>('[data-action]').forEach((el) => {
    el.classList.toggle('is-hot', el.getAttribute('data-action') === action);
  });
  donut.querySelector<SVGElement>('.donut-hole')?.classList.toggle('is-hot', action === null);
  donut.querySelector<SVGElement>('.donut-cancel')?.classList.toggle('is-hot', action === null);
}

function hideDonut(): void {
  const donut = document.getElementById('drag-donut');
  if (donut) {
    donut.hidden = true;
    donut.innerHTML = '';
  }
}

/* ------------------------------------------------------------------ *
 * Note popover (annotate)
 * ------------------------------------------------------------------ */

function openNote(name: string): void {
  const pop = document.getElementById('note-popover');
  if (!pop) return;
  const input = pop.querySelector<HTMLTextAreaElement>('.np-input')!;
  input.value = getNote(name) ?? '';
  pop.dataset.name = name;
  pop.hidden = false;
  input.focus();
}

function initNotePopover(): void {
  const pop = document.getElementById('note-popover');
  if (!pop) return;
  const input = pop.querySelector<HTMLTextAreaElement>('.np-input')!;
  pop.querySelector<HTMLButtonElement>('.np-save')!.addEventListener('click', () => {
    const name = pop.dataset.name;
    if (name) setNote(name, input.value.trim() || null);
    pop.hidden = true;
  });
  pop.querySelector<HTMLButtonElement>('.np-cancel')!.addEventListener('click', () => {
    pop.hidden = true;
  });
}

export function initDrag(): void {
  window.addEventListener('pointerdown', onPointerDown);
  window.addEventListener('resize', () => {
    if (ctx?.active) {
      const c = center();
      ctx.cx = c.x;
      ctx.cy = c.y;
      renderDonut(ctx.membership);
    }
  });
  initNotePopover();
}
