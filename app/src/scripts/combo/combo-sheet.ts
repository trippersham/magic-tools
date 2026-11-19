// Transient BOTTOM SHEET that explains ONE combo line (Phase 5 redesign).
//
// Tapping an EDGE (a combo↔combo or card→outcome connector) resolves the edge's
// single `comboId` to its combo and slides a bottom drawer up over the graph
// showing ONLY that one line: the pieces, ordered steps, prerequisites, produced
// outcomes (win flagged), and a Commander Spellbook link. It overlays the graph
// (never reflows it) and is dismissible four ways — swipe-down (Pointer Events,
// mirroring drag.ts), scrim tap, the ✕, and Esc — each returning focus to the
// graph. Height is capped with internal scroll so the DAG stays visible above.

// Outcome-type → tint. Duplicated from overview-client.ts to keep this module
// free of an import cycle. Keep in sync.
const OUTCOME_COLORS: Record<string, string> = {
  drain: '#dc2626',
  draw: '#3b82f6',
  tokens: '#d4a017',
  lifegain: '#ec4899',
  mana: '#06b6d4',
  damage: '#f97316',
  other: '#a78bfa',
};

// Gesture / animation timings (mirror drag.ts / graph-mobile idioms).
const SHEET_MS = 380;
const SHEET_EASE = 'cubic-bezier(0.22, 1, 0.36, 1)';
const START_THRESHOLD = 8; // px before a press becomes a swipe
const DISMISS_PX = 90; // absolute down-drag past which we dismiss

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

let detail: DetailData | null = null;
let scrim: HTMLElement | null = null;
let sheet: HTMLElement | null = null;
let onClosed: (() => void) | null = null;

function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function ensureEls(): void {
  if (sheet) return;
  scrim = document.createElement('div');
  scrim.id = 'combo-sheet-scrim';
  scrim.addEventListener('click', () => closeSheet());

  sheet = document.createElement('aside');
  sheet.id = 'combo-sheet';
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-label', 'Combo line');
  sheet.dataset.open = 'false';

  document.body.appendChild(scrim);
  document.body.appendChild(sheet);
}

function comboById(id: string): ComboDetail | null {
  return detail?.combos.find((c) => c.id === id) ?? null;
}

function renderCombo(c: ComboDetail): string {
  const outcomeByType = new Map((detail?.outcomeTypes ?? []).map((t) => [t.id, t]));
  const pieces = c.cards
    .map((n) => `<li class="sheet-piece">${esc(n)}</li>`)
    .join('');
  const outcomeChips = c.outcomeTypes
    .map((t) => {
      const ot = outcomeByType.get(t);
      const color = OUTCOME_COLORS[t] ?? OUTCOME_COLORS.other;
      return `<span class="sheet-outcome" style="--outcome-color:${color}">${esc(ot?.icon ?? '✨')} ${esc(ot?.label ?? t)}</span>`;
    })
    .join('');
  const produces = c.produces
    .map(
      (p) =>
        `<li${p.win ? ' data-win="true"' : ''}>${p.win ? '🏆 ' : ''}${esc(p.name)}</li>`,
    )
    .join('');
  const prereqs = c.prerequisites.length
    ? `<div class="sheet-block"><h4>Prerequisites</h4><ul class="sheet-prereq">${c.prerequisites
        .map((p) => `<li>${esc(p)}</li>`)
        .join('')}</ul></div>`
    : '';
  const steps = c.steps.map((s) => `<li>${esc(s)}</li>`).join('');

  return (
    `<div class="sheet-grip" aria-hidden="true"></div>` +
    `<header class="sheet-head">` +
    `<h2>${esc(c.cards.join(' + '))}</h2>` +
    `<button type="button" class="sheet-close" aria-label="Close">✕</button>` +
    `</header>` +
    `<div class="sheet-body">` +
    (outcomeChips ? `<div class="sheet-outcomes">${outcomeChips}</div>` : '') +
    `<div class="sheet-block"><h4>Pieces</h4><ul class="sheet-pieces">${pieces}</ul></div>` +
    prereqs +
    `<div class="sheet-block"><h4>Steps</h4><ol class="sheet-steps">${steps}</ol></div>` +
    (produces ? `<div class="sheet-block"><h4>Produces</h4><ul class="sheet-produces">${produces}</ul></div>` : '') +
    `<a class="sheet-spellbook" href="${esc(c.spellbookUrl)}" target="_blank" rel="noopener noreferrer">View on Commander Spellbook ↗</a>` +
    `</div>`
  );
}

/** Open the bottom sheet for a single combo id (from a clicked edge). */
export function openSheet(comboId: string): void {
  if (!detail) return;
  const c = comboById(comboId);
  if (!c) return;
  ensureEls();
  if (!sheet || !scrim) return;

  sheet.innerHTML = renderCombo(c);
  sheet.querySelector('.sheet-close')?.addEventListener('click', () => closeSheet());

  // Force a frame so the slide-up transition runs from the closed state.
  sheet.dataset.open = 'false';
  scrim.dataset.open = 'false';
  sheet.style.transition = 'none';
  sheet.style.transform = '';
  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  void sheet.offsetHeight;
  sheet.style.transition = '';
  scrim.dataset.open = 'true';
  sheet.dataset.open = 'true';

  wireSwipe(sheet);
}

/** Close the sheet (any dismiss path). Returns focus intent to the graph. */
export function closeSheet(): void {
  if (!sheet || sheet.dataset.open !== 'true') return;
  sheet.dataset.open = 'false';
  if (scrim) scrim.dataset.open = 'false';
  sheet.style.transform = '';
  onClosed?.();
}

export function isSheetOpen(): boolean {
  return sheet?.dataset.open === 'true';
}

/* ------------------------------------------------------------------ *
 * Swipe-down to dismiss (Pointer Events — mirrors drag.ts).
 * ------------------------------------------------------------------ */

interface Swipe {
  startX: number;
  startY: number;
  active: boolean;
  pointerId: number;
}
let swipe: Swipe | null = null;

function wireSwipe(el: HTMLElement): void {
  el.onpointerdown = (e) => {
    // Don't start a swipe from the scrollable body when it isn't at the top;
    // only the grip/header reliably initiates a dismiss drag.
    const t = e.target as HTMLElement;
    const fromHandle = Boolean(t.closest('.sheet-grip') || t.closest('.sheet-head'));
    const body = el.querySelector<HTMLElement>('.sheet-body');
    const atTop = !body || body.scrollTop <= 0;
    if (!fromHandle && !atTop) return;
    swipe = { startX: e.clientX, startY: e.clientY, active: false, pointerId: e.pointerId };
  };
  el.onpointermove = (e) => {
    if (!swipe) return;
    const dx = e.clientX - swipe.startX;
    const dy = e.clientY - swipe.startY;
    if (!swipe.active) {
      if (Math.hypot(dx, dy) < START_THRESHOLD) return;
      if (Math.abs(dy) <= Math.abs(dx)) {
        swipe = null; // horizontal — not a dismiss
        return;
      }
      swipe.active = true;
      try {
        el.setPointerCapture(swipe.pointerId);
      } catch {
        /* ignore */
      }
    }
    if (dy > 0) {
      el.style.transition = 'none';
      el.style.transform = `translateY(${dy}px)`;
    }
  };
  const end = (e: PointerEvent): void => {
    if (!swipe) return;
    const dy = e.clientY - swipe.startY;
    const wasActive = swipe.active;
    swipe = null;
    el.style.transition = '';
    if (wasActive && dy >= DISMISS_PX) {
      closeSheet();
    } else {
      el.style.transform = ''; // spring back to open
    }
  };
  el.onpointerup = end;
  el.onpointercancel = end;
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */

/** Provide the combo detail data + a callback fired whenever the sheet closes. */
export function initSheet(data: DetailData | null, closed: () => void): void {
  detail = data;
  onClosed = closed;
}

// Expose timings for CSS-independent consumers / tests.
export const SHEET_TIMINGS = { SHEET_MS, SHEET_EASE } as const;
