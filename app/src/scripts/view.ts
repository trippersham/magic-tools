// View switcher + density toggle + floating info pane (v2.2 / v2.3).
// Membership filtering re-shows already-rendered cards. The considerations
// section is a separate pool: its cards stay visible regardless of view
// (except when dismissed). Dismissed cards are hidden everywhere.

import { getMembership, getNote } from './store';

type Membership = 'untouched' | 'drop' | 'add' | 'consideration' | 'dismissed';
type ViewName = 'all' | 'current' | 'proposed' | 'adds' | 'drops' | 'changes';

const VIEW_MEMBERSHIPS: Record<ViewName, Membership[]> = {
  // 'consideration' included in All so a de-proposed add (which physically lives
  // in the main stacks) stays visible after leaving the proposed set.
  all: ['untouched', 'drop', 'add', 'consideration'],
  current: ['untouched', 'drop'],
  proposed: ['untouched', 'add'],
  adds: ['add'],
  drops: ['drop'],
  changes: ['drop', 'add'],
};

let currentView: ViewName = 'all';

function applyView(view: ViewName): void {
  currentView = view;
  const visible = new Set(VIEW_MEMBERSHIPS[view]);

  document.querySelectorAll<HTMLElement>('[data-membership]').forEach((el) => {
    const m = el.dataset.membership as Membership;
    if (m === 'dismissed') {
      el.hidden = true;
      return;
    }
    // Considerations-section cards are their own pool — always visible.
    if (el.closest('[data-considerations]')) {
      el.hidden = false;
      return;
    }
    el.hidden = !visible.has(m);
  });

  // Hide type columns (in the main stacks + text view) with no visible cards.
  document.querySelectorAll<HTMLElement>('[data-column]').forEach((col) => {
    if (col.closest('[data-considerations]')) {
      col.hidden = false;
      return;
    }
    const anyVisible = col.querySelector<HTMLElement>('[data-membership]:not([hidden])');
    col.hidden = !anyVisible;
  });

  resetFirstVisibleMargins();

  document.querySelectorAll<HTMLElement>('[data-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.panel !== view;
  });

  document.querySelectorAll<HTMLElement>('[data-view-btn]').forEach((btn) => {
    const active = btn.dataset.viewBtn === view;
    btn.setAttribute('aria-pressed', String(active));
    btn.classList.toggle('is-active', active);
  });

  document.documentElement.dataset.activeView = view;
  try {
    history.replaceState(null, '', `#${view}`);
  } catch {
    /* ignore */
  }
}

/**
 * Fanned stacks pull every card up with a negative top margin, relying on the
 * FIRST card having it zeroed. When leading cards are hidden, reset the margin
 * on the first *visible* card of each stack (CSS has no :first-visible).
 */
function resetFirstVisibleMargins(): void {
  document.querySelectorAll<HTMLElement>('.stack').forEach((stack) => {
    let seenVisible = false;
    stack.querySelectorAll<HTMLElement>('.card').forEach((card) => {
      if (card.hidden) {
        card.style.marginTop = '';
        return;
      }
      card.style.marginTop = seenVisible ? '' : '0';
      seenVisible = true;
    });
  });
}

/* ------------------------------------------------------------------ *
 * Density toggle (v2.2) — swaps --overlap; clamps exposed height >=44px
 * on coarse-pointer / narrow viewports so the touch target stays usable.
 * ------------------------------------------------------------------ */

const ART_OVERLAP = 178;
// Compact packs cards down to just their title bar. We target an EXPOSED sliver
// height (not a fixed overlap) so it stays tight regardless of card size.
const COMPACT_EXPOSED_DESKTOP = 34; // just a hair taller than the title line
const MIN_EXPOSED = 44; // accessibility touch-target floor on coarse/narrow

function coarseOrNarrow(): boolean {
  return (
    window.matchMedia('(pointer: coarse)').matches ||
    window.matchMedia('(max-width: 640px)').matches
  );
}

function applyDensity(mode: 'art' | 'compact'): void {
  document.documentElement.dataset.density = mode;
  const stacks = document.querySelectorAll<HTMLElement>('.stack');
  stacks.forEach((stack) => {
    if (mode === 'art') {
      stack.style.setProperty('--overlap', `${ART_OVERLAP}px`);
      return;
    }
    // Compact: leave only a title-bar sliver exposed. On coarse/narrow the sliver
    // must stay >=44px (touch target); desktop packs tighter, to the title line.
    const cardEl = stack.querySelector<HTMLElement>('.card');
    const cardH = cardEl ? cardEl.getBoundingClientRect().height : 329;
    const exposed = coarseOrNarrow() ? MIN_EXPOSED : COMPACT_EXPOSED_DESKTOP;
    const overlap = Math.max(0, cardH - exposed);
    stack.style.setProperty('--overlap', `${overlap}px`);
  });
  resetFirstVisibleMargins();

  document.querySelectorAll<HTMLElement>('[data-density-btn]').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.densityBtn === mode);
  });
}

/* ------------------------------------------------------------------ *
 * Group-by facet — switch the column axis between card Type and Color.
 * Cards are physically re-bucketed into new columns (nodes MOVED, so drag
 * listeners survive); within a column the sort stays mana-value then name.
 * ------------------------------------------------------------------ */

type GroupMode = 'type' | 'color';
let currentGrouping: GroupMode = 'type';

const TYPE_ORDER = [
  'Commander', 'Creature', 'Instant', 'Sorcery', 'Artifact',
  'Enchantment', 'Equipment', 'Planeswalker', 'Land', 'Other',
];
const COLOR_ORDER = ['White', 'Blue', 'Black', 'Red', 'Green', 'Multicolor', 'Colorless', 'Land'];
const COLOR_NAME: Record<string, string> = { W: 'White', U: 'Blue', B: 'Black', R: 'Red', G: 'Green' };

// Mirrors lib/grouping.ts groupOf() so Type mode reproduces the server layout.
function typeGroupOf(card: HTMLElement): string {
  if (card.dataset.role === 'commander') return 'Commander';
  const t = card.dataset.typeLine ?? '';
  if (/\bLand\b/.test(t)) return 'Land';
  if (/\bEquipment\b/.test(t)) return 'Equipment';
  if (/\bCreature\b/.test(t)) return 'Creature';
  if (/\bPlaneswalker\b/.test(t)) return 'Planeswalker';
  if (/\bInstant\b/.test(t)) return 'Instant';
  if (/\bSorcery\b/.test(t)) return 'Sorcery';
  if (/\bArtifact\b/.test(t)) return 'Artifact';
  if (/\bEnchantment\b/.test(t)) return 'Enchantment';
  return 'Other';
}

function colorGroupOf(card: HTMLElement): string {
  const t = card.dataset.typeLine ?? '';
  if (/\bLand\b/.test(t)) return 'Land';
  const colors = (card.dataset.colors ?? '').split(',').filter(Boolean);
  if (colors.length === 0) return 'Colorless';
  if (colors.length > 1) return 'Multicolor';
  return COLOR_NAME[colors[0]] ?? 'Colorless';
}

function buildColumn(group: string, cards: HTMLElement[]): HTMLElement {
  const sec = document.createElement('section');
  sec.className = 'column';
  sec.dataset.column = group;
  const h2 = document.createElement('h2');
  h2.className = 'column-title';
  h2.innerHTML = `${escapeHtml(group)} <span class="count"></span>`;
  const stack = document.createElement('div');
  stack.className = 'stack';
  cards.forEach((c) => stack.appendChild(c)); // MOVE node — listeners preserved
  sec.appendChild(h2);
  sec.appendChild(stack);
  return sec;
}

function regroupWrap(wrap: HTMLElement, mode: GroupMode): void {
  const cards = Array.from(wrap.querySelectorAll<HTMLElement>('.card[data-name]'));
  const buckets = new Map<string, HTMLElement[]>();
  for (const card of cards) {
    const g = mode === 'type' ? typeGroupOf(card) : colorGroupOf(card);
    if (!buckets.has(g)) buckets.set(g, []);
    buckets.get(g)!.push(card);
  }
  const order = mode === 'type' ? TYPE_ORDER : COLOR_ORDER;
  // Detach existing columns; the card nodes survive because buckets holds refs.
  wrap.replaceChildren();
  for (const g of order) {
    const cs = buckets.get(g);
    if (!cs || cs.length === 0) continue;
    cs.sort(
      (a, b) =>
        (Number(a.dataset.cmc) || 0) - (Number(b.dataset.cmc) || 0) ||
        (a.dataset.name ?? '').localeCompare(b.dataset.name ?? ''),
    );
    wrap.appendChild(buildColumn(g, cs));
  }
}

function applyGrouping(mode: GroupMode): void {
  currentGrouping = mode;
  // Regroup every visual stacks view (main deck + considerations pool).
  document.querySelectorAll<HTMLElement>('.stacks-view').forEach((wrap) => regroupWrap(wrap, mode));

  document.querySelectorAll<HTMLElement>('[data-group-btn]').forEach((btn) => {
    const active = btn.dataset.groupBtn === mode;
    btn.setAttribute('aria-pressed', String(active));
    btn.classList.toggle('is-active', active);
  });

  // New .stack elements need their overlap re-applied, then re-filter for the view.
  applyDensity((document.documentElement.dataset.density as 'art' | 'compact') ?? 'art');
  updateColumnCounts();
  applyView(currentView);
}

/* ------------------------------------------------------------------ *
 * Floating info pane (v2.3)
 * ------------------------------------------------------------------ */

let hovered: HTMLElement | null = null;
let pinned: HTMLElement | null = null;

function isDragging(): boolean {
  return document.body.dataset.dragging === 'true';
}

function renderPips(manaCost: string): string {
  const syms = manaCost.match(/\{[^}]+\}/g) ?? [];
  if (syms.length === 0) return '';
  return syms
    .map((raw) => {
      const s = raw.slice(1, -1);
      const cls = /^[WUBRGC]$/.test(s) ? `pip-sym pip-${s}` : 'pip-sym pip-generic';
      return `<span class="${cls}">${s}</span>`;
    })
    .join('');
}

function showPane(card: HTMLElement): void {
  if (isDragging()) return;
  const pane = document.getElementById('info-pane');
  if (!pane) return;
  const name = card.dataset.name ?? '';
  const membership = getMembership(name);
  const reasonText =
    card.dataset.reason && card.dataset.reason.trim()
      ? card.dataset.reason
      : 'Kept — not part of this change.';
  const note = getNote(name);
  const cut = card.dataset.cut && card.dataset.cut.trim() ? card.dataset.cut : '';

  const usdRaw = card.dataset.usd ?? '';
  const price = usdRaw === '' ? '—' : `$${Number(usdRaw).toFixed(2)}`;
  const mana = renderPips(card.dataset.manaCost ?? '');

  const reasonEl = pane.querySelector<HTMLElement>('.ip-reason')!;
  reasonEl.innerHTML =
    `<span class="ip-name">${escapeHtml(name)}</span>` +
    `<span class="ip-state ip-state-${membership}">${membership}</span>` +
    `<span class="ip-text">${escapeHtml(reasonText)}</span>` +
    (cut ? `<span class="ip-cut">cuts: ${escapeHtml(cut)}</span>` : '') +
    (note ? `<span class="ip-note">📝 ${escapeHtml(note)}</span>` : '');
  pane.querySelector<HTMLElement>('.ip-mana')!.innerHTML = mana || '<span class="ip-nomana">—</span>';
  pane.querySelector<HTMLElement>('.ip-price')!.textContent = price;

  pane.hidden = false;
  position(pane, card);
}

function position(pane: HTMLElement, card: HTMLElement): void {
  const r = card.getBoundingClientRect();
  pane.style.visibility = 'hidden';
  pane.hidden = false;
  const ph = pane.getBoundingClientRect().height;
  const pw = pane.getBoundingClientRect().width;
  const gap = 8;
  let top = r.bottom + gap;
  if (top + ph > window.innerHeight - 8) top = r.top - gap - ph; // flip above
  if (top < 8) top = 8;
  let left = r.left + r.width / 2 - pw / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  pane.style.top = `${top + window.scrollY}px`;
  pane.style.left = `${left + window.scrollX}px`;
  pane.style.visibility = 'visible';
}

function hidePane(): void {
  const pane = document.getElementById('info-pane');
  if (pane) pane.hidden = true;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!,
  );
}

function wireCard(card: HTMLElement): void {
  card.addEventListener('mouseenter', () => {
    hovered = card;
  });
  card.addEventListener('transitionend', (e) => {
    if (e.propertyName !== 'transform') return;
    if (hovered === card && !pinned && !isDragging()) showPane(card);
  });
  card.addEventListener('mouseleave', () => {
    if (hovered === card) hovered = null;
    if (pinned !== card) hidePane();
  });
  card.addEventListener('click', (e) => {
    // Suppress the click that ends a drag.
    if (isDragging() || card.dataset.dragMoved === 'true') {
      card.dataset.dragMoved = '';
      return;
    }
    e.stopPropagation();
    if (pinned === card) {
      pinned = null;
      hidePane();
    } else {
      pinned = card;
      showPane(card);
    }
  });
}

export function suppressPaneForDrag(): void {
  pinned = null;
  hovered = null;
  hidePane();
}

/* ------------------------------------------------------------------ *
 * Card relocation (add <-> consideration) with FLIP animation.
 * A card that leaves the proposed set (add -> consideration) physically
 * flies down into the Considerations section; promoting one (consideration
 * -> add) flies it back up into its type column in the main stacks.
 * ------------------------------------------------------------------ */

function mainStacksWrap(): HTMLElement | null {
  return (
    Array.from(document.querySelectorAll<HTMLElement>('.stacks-view')).find(
      (w) => !w.closest('[data-considerations]'),
    ) ?? null
  );
}
function considStacksWrap(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-considerations] .stacks-view');
}

function cssEsc(s: string): string {
  return typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(s) : s.replace(/"/g, '\\"');
}

function ensureColumn(wrap: HTMLElement, group: string): HTMLElement {
  let col = wrap.querySelector<HTMLElement>(`.column[data-column="${cssEsc(group)}"]`);
  if (!col) {
    col = document.createElement('section');
    col.className = 'column';
    col.dataset.column = group;
    col.innerHTML =
      `<h2 class="column-title">${escapeHtml(group)} <span class="count"></span></h2><div class="stack"></div>`;
    wrap.appendChild(col);
  }
  return col.querySelector<HTMLElement>('.stack')!;
}

function relocateCards(): void {
  const main = mainStacksWrap();
  const consid = considStacksWrap();
  if (!main || !consid) return;

  document.querySelectorAll<HTMLElement>('.card[data-name]').forEach((card) => {
    const m = card.dataset.membership as Membership;
    let destWrap: HTMLElement | null = null;
    if (m === 'consideration') destWrap = consid;
    else if (m === 'add') destWrap = main;
    else return; // untouched / drop / dismissed never relocate

    const curWrap = card.closest<HTMLElement>('.stacks-view');
    if (!curWrap || curWrap === destWrap) return;

    const group = card.closest<HTMLElement>('[data-column]')?.dataset.column ?? 'Other';
    const destStack = ensureColumn(destWrap, group);

    // FLIP: measure old rect, move node, invert, then play to home.
    const first = card.getBoundingClientRect();
    destStack.appendChild(card);
    resetFirstVisibleMargins();
    const last = card.getBoundingClientRect();
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    if (dx === 0 && dy === 0) return;

    card.classList.add('is-relocating');
    card.style.transition = 'none';
    card.style.transform = `translate(${dx}px, ${dy}px)`;
    void card.getBoundingClientRect(); // force reflow so the invert takes
    requestAnimationFrame(() => {
      card.style.transition = 'transform 0.42s cubic-bezier(0.2, 0.7, 0.2, 1)';
      card.style.transform = '';
    });
    card.addEventListener(
      'transitionend',
      function te(ev: TransitionEvent) {
        if (ev.propertyName !== 'transform') return;
        card.style.transition = '';
        card.classList.remove('is-relocating');
        card.removeEventListener('transitionend', te as EventListener);
      } as EventListener,
    );
  });

  updateColumnCounts();
}

function updateColumnCounts(): void {
  document.querySelectorAll<HTMLElement>('.stacks-view .column').forEach((col) => {
    const cards = col.querySelectorAll<HTMLElement>('.card[data-name]');
    // Quantity-weighted so a basic-land pile (Plains x15) counts as 15, not 1.
    let qty = 0;
    cards.forEach((card) => {
      qty += Number(card.dataset.qty ?? '1') || 1;
    });
    const c = col.querySelector<HTMLElement>('.count');
    if (c) c.textContent = String(qty);
    col.dataset.empty = cards.length === 0 ? 'true' : '';
  });
}

function initView(): void {
  document.querySelectorAll<HTMLElement>('[data-view-btn]').forEach((btn) => {
    btn.addEventListener('click', () => applyView(btn.dataset.viewBtn as ViewName));
  });
  document.querySelectorAll<HTMLElement>('[data-density-btn]').forEach((btn) => {
    btn.addEventListener('click', () => applyDensity(btn.dataset.densityBtn as 'art' | 'compact'));
  });
  document.querySelectorAll<HTMLElement>('[data-group-btn]').forEach((btn) => {
    btn.addEventListener('click', () => applyGrouping(btn.dataset.groupBtn as GroupMode));
  });

  const textToggle = document.querySelector<HTMLInputElement>('[data-text-toggle]');
  textToggle?.addEventListener('change', () => {
    document.documentElement.dataset.mode = textToggle.checked ? 'text' : 'image';
  });

  document.querySelectorAll<HTMLElement>('.card[data-name]').forEach(wireCard);

  document.addEventListener('click', () => {
    pinned = null;
    hidePane();
  });

  document.addEventListener('curation-changed', () => {
    // Physically move re-tagged cards between the main stacks and the
    // Considerations section (FLIP-animated), then re-apply the active view.
    relocateCards();
    applyView(currentView);
  });

  window.addEventListener('resize', () => {
    if (document.documentElement.dataset.density === 'compact') applyDensity('compact');
  });

  const hash = window.location.hash.slice(1) as ViewName;
  const start: ViewName = hash in VIEW_MEMBERSHIPS ? hash : 'all';
  applyView(start);
  applyDensity('art');
}

export { initView };
