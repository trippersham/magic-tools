// Drag-to-tag controller (v4). Pointer Events. Two entry gestures:
//
//   • Singleton / expanded tile / fanned tile → a normal per-INSTANCE drag: the
//     page fades, a radial donut of edge targets appears, and the release lands
//     the pointer in a segment whose action drives a pure curation transition on
//     THAT instance.
//   • Stacked multi master → the progressive fan gesture (v4.4): grip and drag;
//     the group fans open VERTICALLY in place, tracking drag distance; release
//     past the threshold springs to the fully-expanded column, release below it
//     springs back to the stack. No shake — the fan follows the finger.
//
// Presentation only — the transition logic is in curation.ts, the fan layout in
// view.ts. Instances are identity-free: dragging tile i re-tags instance i.

import {
  donutSegments,
  hitTest,
  applyAction,
  type Action,
  type Membership,
  type Segment,
  type DonutGeometry,
} from '../lib/curation';
import {
  instanceMembership,
  setInstanceMembership,
  setInstanceNote,
  getInstanceNote,
  instanceCount,
} from './store';
import {
  suppressPaneForDrag,
  fanBegin,
  fanSetProgress,
  fanSettleOpen,
  fanClose,
  isFanned,
  isFanElement,
} from './view';

const START_THRESHOLD = 6; // px before a press becomes a per-instance drag

/* Gesture physics (hand-rolled, tune in iteration). */
const UNSTACK_FACTOR = 0.65; // full-fan at ≈ 0.65 × card-height of drag distance

/** Donut geometry, sized to the viewport: the empty center (deadzone) hole has a
 *  diameter of 50% of the window's smaller dimension. */
function geo(): DonutGeometry {
  return { radius: 200, deadzone: 0.25 * Math.min(window.innerWidth, window.innerHeight) };
}

function center(): { x: number; y: number } {
  return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
}

/* ------------------------------------------------------------------ *
 * Per-instance drag
 * ------------------------------------------------------------------ */

interface DragCtx {
  card: HTMLElement;
  name: string;
  index: number;
  membership: Membership;
  startX: number;
  startY: number;
  cx: number;
  cy: number;
  pointerId: number;
  active: boolean;
}

let ctx: DragCtx | null = null;

function startInstanceDrag(e: PointerEvent, card: HTMLElement, name: string, index: number): void {
  const membership = instanceMembership(name, index);
  if (donutSegments(membership).length === 0) return; // dismissed / nothing to do

  const c = center();
  ctx = {
    card,
    name,
    index,
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
  ctx.card.style.transform = `translate(${dxs}px, ${dys}px) scale(1.04)`;
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

  if (!action) {
    // Dead-center / outside → cancel. A tile dragged from an open fan just
    // rubber-bands the fan closed.
    if (isFanned()) fanClose();
    return;
  }

  cur.card.dataset.dragMoved = 'true'; // suppress the trailing click->pane
  if (action === 'annotate') {
    openNote(cur.name, cur.index);
    return;
  }
  const result = applyAction(cur.membership, action);
  setInstanceMembership(cur.name, cur.index, result.membership);
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
 * The progressive fan gesture (stacked multi masters). Grip + drag fans the
 * group open vertically in place, tracking drag distance; release decides
 * settle-open (past threshold) vs spring-back (below). The layout/spring lives
 * in view.ts (fanBegin / fanSetProgress / fanSettleOpen / fanClose).
 * ------------------------------------------------------------------ */

interface GripCtx {
  master: HTMLElement;
  name: string;
  startX: number;
  startY: number;
  threshold: number;
  began: boolean; // fanBegin() has fired (crossed START_THRESHOLD)
}

let grip: GripCtx | null = null;

function startGrip(e: PointerEvent, master: HTMLElement, name: string): void {
  const h = master.getBoundingClientRect().height || 329;
  grip = {
    master,
    name,
    startX: e.clientX,
    startY: e.clientY,
    threshold: h * UNSTACK_FACTOR,
    began: false,
  };
  // Track via window listeners (no pointer capture — the master is hidden once
  // the fan begins, which would drop a captured pointer).
  window.addEventListener('pointermove', onGripMove);
  window.addEventListener('pointerup', onGripUp);
  window.addEventListener('pointercancel', onGripCancel);
}

function gripDist(e: PointerEvent): number {
  return Math.hypot(e.clientX - grip!.startX, e.clientY - grip!.startY);
}

function onGripMove(e: PointerEvent): void {
  if (!grip) return;
  const dist = gripDist(e);
  if (!grip.began) {
    if (dist < START_THRESHOLD) return;
    if (!fanBegin(grip.master)) {
      cleanupGrip();
      grip = null;
      return;
    }
    grip.began = true;
  }
  // Fan tracks the finger: 0 = stacked, 1 (at threshold) = fully expanded.
  fanSetProgress(dist / grip.threshold);
}

function onGripUp(e: PointerEvent): void {
  if (!grip) return;
  const g = grip;
  const dist = gripDist(e);
  cleanupGrip();
  grip = null;
  if (!g.began) return; // never crossed START_THRESHOLD → nothing happened
  if (dist >= g.threshold) fanSettleOpen(); // past threshold → stays expanded
  else fanClose(); // below threshold → springs back to the stack
}

function onGripCancel(): void {
  if (!grip) return;
  const began = grip.began;
  cleanupGrip();
  grip = null;
  if (began) fanClose();
}

function cleanupGrip(): void {
  window.removeEventListener('pointermove', onGripMove);
  window.removeEventListener('pointerup', onGripUp);
  window.removeEventListener('pointercancel', onGripCancel);
}

/* ------------------------------------------------------------------ *
 * Pointer entry — routes a press to a per-instance drag or the gesture, and
 * enforces the transient/modal fan (interacting outside it closes it).
 * ------------------------------------------------------------------ */

function onPointerDown(e: PointerEvent): void {
  if (e.button !== 0 && e.pointerType === 'mouse') return;
  const target = e.target as HTMLElement;

  // Transient/modal: a press outside the open fan springs it closed (and is
  // consumed as the dismiss — no drag/gesture starts on that press).
  if (isFanned() && !isFanElement(target)) {
    fanClose();
    return;
  }

  const card = target.closest<HTMLElement>('.card[data-name]');
  if (!card) return;
  const name = card.dataset.name!;

  // An expanded copy or a fanned tile carries its instance index → drag it.
  if (card.dataset.instanceIndex !== undefined) {
    startInstanceDrag(e, card, name, Number(card.dataset.instanceIndex));
    return;
  }

  // A master. Multi + still stacked → the shake/unstack gesture (fork 2).
  const total = instanceCount(name) || Number(card.dataset.qty) || 1;
  const stacked = !card.classList.contains('is-stacked-hidden');
  if (total > 1 && stacked) {
    startGrip(e, card, name);
    return;
  }

  // Singleton (fork 3) → straight to the donut on instance 0. No shake step.
  startInstanceDrag(e, card, name, 0);
}

/* ------------------------------------------------------------------ *
 * Donut rendering — a full-screen SVG annulus: wedge segments sweep from
 * the empty dead-center hole out to the window edges.
 * ------------------------------------------------------------------ */

const REGION_ARC: Record<string, [number, number]> = {
  right: [-Math.PI / 2, Math.PI / 2],
  left: [Math.PI / 2, (3 * Math.PI) / 2],
  bottom: [0, Math.PI],
  'top-left': [Math.PI, (3 * Math.PI) / 2],
  'top-right': [(3 * Math.PI) / 2, 2 * Math.PI],
};

const SVG_NS = 'http://www.w3.org/2000/svg';

function sectorPath(cx: number, cy: number, ri: number, ro: number, a0: number, a1: number): string {
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
  const ro = Math.hypot(w, h);
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
 * Note popover (annotate) — per-instance keyed
 * ------------------------------------------------------------------ */

function openNote(name: string, index: number): void {
  const pop = document.getElementById('note-popover');
  if (!pop) return;
  const input = pop.querySelector<HTMLTextAreaElement>('.np-input')!;
  input.value = getInstanceNote(name, index) ?? '';
  pop.dataset.name = name;
  pop.dataset.instanceIndex = String(index);
  pop.hidden = false;
  input.focus();
}

function initNotePopover(): void {
  const pop = document.getElementById('note-popover');
  if (!pop) return;
  const input = pop.querySelector<HTMLTextAreaElement>('.np-input')!;
  pop.querySelector<HTMLButtonElement>('.np-save')!.addEventListener('click', () => {
    const name = pop.dataset.name;
    const index = Number(pop.dataset.instanceIndex) || 0;
    if (name) setInstanceNote(name, index, input.value.trim() || null);
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
    if (isFanned()) fanClose();
  });
  initNotePopover();
}
