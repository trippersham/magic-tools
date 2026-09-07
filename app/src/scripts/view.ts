// View switcher + density toggle + floating info pane (v2.2 / v2.3).
// Membership filtering re-shows already-rendered cards. The considerations
// section is a separate pool: its cards stay visible regardless of view
// (except when dismissed). Dismissed cards are hidden everywhere.

import { getMembership, getNote, instancesOf } from './store';

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
    // A genuinely mixed master tile (>1 of untouched/drop/add present) belongs to
    // MULTIPLE view buckets at once; the binary data-membership can't express
    // that, so derive visibility from the per-copy counts. Pure tiles (singletons,
    // expanded copies, whole-name drag re-tags) keep honoring data-membership.
    const u = Number(el.dataset.untouchedQty) || 0;
    const r = Number(el.dataset.droppedQty) || 0;
    const a = Number(el.dataset.addedQty) || 0;
    const present = (u > 0 ? 1 : 0) + (r > 0 ? 1 : 0) + (a > 0 ? 1 : 0);
    if (present > 1) {
      const anyMatch =
        (u > 0 && visible.has('untouched')) ||
        (r > 0 && visible.has('drop')) ||
        (a > 0 && visible.has('add'));
      el.hidden = !anyMatch;
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
      if (card.hidden || card.classList.contains('is-stacked-hidden')) {
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

type DensityMode = 'art' | 'compact' | 'full';

function applyDensity(mode: DensityMode): void {
  document.documentElement.dataset.density = mode;
  const stacks = document.querySelectorAll<HTMLElement>('.stack');
  stacks.forEach((stack) => {
    if (mode === 'full') {
      // Full: no overlap — every card fully visible; columns wrap (CSS).
      stack.style.setProperty('--overlap', '0px');
      return;
    }
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
}

/* ------------------------------------------------------------------ *
 * Instance expansion vs stacking (v3.2). Each server-rendered master card
 * carries data-{untouched,dropped,added}-qty. When its governing Stack toggle
 * is OFF, the master is hidden and replaced by one single-copy tile per copy
 * (neutral / red / green). When ON, the master (with its ×N badge) shows.
 *
 * Drag is delegated (window pointerdown), so generated tiles need no drag
 * wiring; only the per-card info-pane listeners are re-attached via wireCard.
 * ------------------------------------------------------------------ */

let stackDuplicates = true;
let stackBasicLands = true;
let currentDensity: DensityMode = 'art';

function tileCounts(master: HTMLElement): { u: number; r: number; a: number } {
  return {
    u: Number(master.dataset.untouchedQty) || 0,
    r: Number(master.dataset.droppedQty) || 0,
    a: Number(master.dataset.addedQty) || 0,
  };
}

function isCloneNext(master: HTMLElement): boolean {
  const n = master.nextElementSibling as HTMLElement | null;
  return !!n && n.dataset.cloneOf === master.dataset.name;
}

type TileKind = 'neutral' | 'drop' | 'add';

function kindOfMembership(m: string): TileKind {
  return m === 'drop' ? 'drop' : m === 'add' ? 'add' : 'neutral';
}

/**
 * A single-copy tile for instance `index` of a master (v4). Carries
 * data-instance-index so a drag re-tags exactly that instance.
 */
function makeClone(master: HTMLElement, kind: TileKind, index: number): HTMLElement {
  const c = master.cloneNode(true) as HTMLElement;
  c.dataset.cloneOf = master.dataset.name ?? '';
  c.dataset.instanceIndex = String(index);
  c.dataset.qty = '1';
  c.dataset.untouchedQty = kind === 'neutral' ? '1' : '0';
  c.dataset.droppedQty = kind === 'drop' ? '1' : '0';
  c.dataset.addedQty = kind === 'add' ? '1' : '0';
  // Each expanded copy is a single, pure tile: rewrite its membership so the
  // border, the view filter, and data-membership all agree.
  c.dataset.membership = kind === 'neutral' ? 'untouched' : kind;
  // Single-copy tile: strip the ×N badge.
  c.querySelector('.qty-badge')?.remove();
  c.classList.remove('add', 'drop', 'neutral', 'mixed', 'is-stacked-hidden');
  c.classList.add(kind === 'add' ? 'add' : kind === 'drop' ? 'drop' : 'neutral');
  return c;
}

/** One tile per INSTANCE (v4.3 expanded), each mapped to its instance index. */
function buildInstanceTiles(master: HTMLElement): HTMLElement[] {
  const name = master.dataset.name ?? '';
  const list = instancesOf(name);
  return list.map((inst, i) => makeClone(master, kindOfMembership(inst.membership), i));
}

function expandMaster(master: HTMLElement): void {
  if (isCloneNext(master)) return; // already expanded
  const tiles = buildInstanceTiles(master);
  const frag = document.createDocumentFragment();
  tiles.forEach((t) => frag.appendChild(t));
  master.after(frag);
  master.classList.add('is-stacked-hidden');
  tiles.forEach(wireCard); // re-attach info-pane listeners (drag is delegated)
}

function collapseMaster(master: HTMLElement): void {
  let n = master.nextElementSibling as HTMLElement | null;
  while (n && n.dataset.cloneOf === master.dataset.name) {
    const next = n.nextElementSibling as HTMLElement | null;
    n.remove();
    n = next;
  }
  master.classList.remove('is-stacked-hidden');
}

function collapseAll(): void {
  document
    .querySelectorAll<HTMLElement>('.card[data-name]:not([data-clone-of])')
    .forEach(collapseMaster);
}

/** Expand or collapse every master according to the current Stack toggles. */
function applyStacking(): void {
  document
    .querySelectorAll<HTMLElement>('.card[data-name]:not([data-clone-of])')
    .forEach((master) => {
      const { u, r, a } = tileCounts(master);
      const total = u + r + a;
      const basic = master.dataset.basic === 'true';
      const shouldStack = basic ? stackBasicLands : stackDuplicates;
      if (shouldStack || total <= 1) {
        collapseMaster(master);
      } else {
        expandMaster(master);
      }
    });
  applyDensity(currentDensity);
  updateColumnCounts();
  applyView(currentView);
}

export function setStacking(dup: boolean, basics: boolean): void {
  stackDuplicates = dup;
  stackBasicLands = basics;
  applyStacking();
}

export function setDensity(mode: DensityMode): void {
  currentDensity = mode;
  applyDensity(mode);
}

/* ------------------------------------------------------------------ *
 * v4.4 — the grip gesture. While dragging a stacked multi master, its copies
 * fan out in a tight left/right ARC (like spreading a hand of cards) — purely a
 * visual "you're interacting" affordance, driven by drag distance. Releasing
 * PAST threshold settles to the fully-expanded VERTICAL column (fanSettleOpen);
 * below threshold it rubber-bands back to the stack (fanClose). The pointer math
 * lives in drag.ts; this module owns the layout + spring. Transient: one group
 * fanned at a time; interacting outside it, or a completed re-tag, collapses it.
 * ------------------------------------------------------------------ */

const FAN_SPRING_MS = 300;
const FAN_SPRING = 'cubic-bezier(0.22, 1.0, 0.36, 1)'; // ease-out settle (open)
const FAN_BACK_MS = 220;
const FAN_BACK = 'cubic-bezier(0.4, 0.0, 0.2, 1)'; // ease back into the stack
const FAN_GAP = 10; // px between fully-expanded fanned cards
// Arc affordance during drag. Ease-in so it emerges gently rather than popping.
const FAN_EASE_POW = 2.0;
const FAN_ARC_TOTAL_DEG = 26; // tight total spread across the hand at full progress
const FAN_ARC_MAX_DEG = 5; // per-card cap so big stacks don't over-rotate
const FAN_ARC_LIFT = 10; // px the fanned tops lift as they splay

let fannedName: string | null = null;
let fanTiles: HTMLElement[] = [];
let fanCollapsedMargin = -ART_OVERLAP; // margin-top at progress 0 (stacked)
let fanTopMargin = 0; // fixed margin-top of the first fanned tile (the master's slot)

export function prefersReducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function isFanned(): boolean {
  return fannedName !== null;
}

/** Is this element one of the currently-fanned tiles? */
export function isFanElement(el: HTMLElement | null): boolean {
  return !!el && !!el.closest('.card')?.classList.contains('fan-tile');
}

function clearFanTransition(): void {
  fanTiles.forEach((t) => {
    t.style.transition = '';
    t.style.transitionDelay = '';
  });
}

/** Ease-in: gentle at the start so the arc emerges rather than pops. */
function easeInFan(p: number): number {
  return Math.pow(p, FAN_EASE_POW);
}

/** Signed rotation (deg) for tile i of n at full spread — centered fan. */
function arcAngle(i: number, n: number): number {
  if (n <= 1) return 0;
  const per = Math.min(FAN_ARC_TOTAL_DEG / (n - 1), FAN_ARC_MAX_DEG);
  return (i - (n - 1) / 2) * per;
}

/**
 * Begin the grip fan: expand the master in place, all copies STACKED (fully
 * overlapping) and upright — visually identical to the single stacked master.
 * Dragging then rotates them into a hand-arc. Returns false if nothing to fan.
 */
export function fanBegin(master: HTMLElement): boolean {
  const name = master.dataset.name;
  if (!name) return false;
  if (fannedName) fanClose();

  const cardH = master.getBoundingClientRect().height || 329;
  fanCollapsedMargin = -cardH; // every copy fully hidden behind the one above
  fanTopMargin = parseFloat(getComputedStyle(master).marginTop) || 0;

  expandMaster(master); // inserts per-instance tiles after master, hides master
  const tiles: HTMLElement[] = [];
  let n = master.nextElementSibling as HTMLElement | null;
  while (n && n.dataset.cloneOf === name) {
    n.classList.add('fan-tile');
    tiles.push(n);
    n = n.nextElementSibling as HTMLElement | null;
  }
  if (tiles.length <= 1) {
    collapseMaster(master);
    return false;
  }

  fanTiles = tiles;
  fannedName = name;
  // Seed stacked + upright: first tile holds the master's slot, the rest overlap
  // it completely; the arc is applied via transform (layout stays a tidy stack).
  tiles.forEach((t, i) => {
    t.style.transition = 'none';
    t.style.transformOrigin = '50% 118%'; // pivot below the card, like a held hand
    t.style.transform = 'rotate(0deg)';
    t.style.marginTop = i === 0 ? `${fanTopMargin}px` : `${fanCollapsedMargin}px`;
    t.style.zIndex = String(20 + i);
  });
  return true;
}

/** Drive the hand-arc by drag progress 0..1 (0 = stacked, 1 = full tight fan). */
export function fanSetProgress(p: number): void {
  const e = easeInFan(Math.max(0, Math.min(1, p)));
  clearFanTransition();
  const n = fanTiles.length;
  fanTiles.forEach((tile, i) => {
    const deg = arcAngle(i, n) * e;
    const lift = -Math.abs(Math.sin((deg * Math.PI) / 180)) * FAN_ARC_LIFT;
    tile.style.transform = `translateY(${lift}px) rotate(${deg}deg)`;
  });
}

/** Release past threshold → settle to the fully-expanded column (stays open),
 *  cascading the last copies out so the motion continues the emergence. */
export function fanSettleOpen(): void {
  if (!fannedName) return;
  const reduce = prefersReducedMotion();
  fanTiles.forEach((tile, i) => {
    // Un-rotate the hand-arc and, for the copies beneath the first, spread down
    // into the real vertical column. Both animate together into the unstack.
    tile.style.transition = reduce
      ? ''
      : `transform ${FAN_SPRING_MS}ms ${FAN_SPRING}, margin-top ${FAN_SPRING_MS}ms ${FAN_SPRING}`;
    tile.style.transitionDelay = reduce ? '' : `${Math.min(i * 10, 120)}ms`;
    tile.style.transform = 'rotate(0deg)';
    if (i !== 0) tile.style.marginTop = `${FAN_GAP}px`;
  });
  if (!reduce) {
    window.setTimeout(() => {
      clearFanTransition();
      fanTiles.forEach((t) => (t.style.transform = ''));
    }, FAN_SPRING_MS + 160);
  } else {
    fanTiles.forEach((t) => (t.style.transform = ''));
  }
}

/** Collapse the fan back to the stacked master (below-threshold release, or a
 *  transient close via outside-interaction / completed re-tag). `after` runs once
 *  the spring-back has finished and the clones are gone — a completed re-tag uses
 *  it to relocate/re-render only AFTER the rubber-back has played. */
export function fanClose(after?: () => void): void {
  if (!fannedName) {
    after?.();
    return;
  }
  const name = fannedName;
  const tiles = fanTiles;
  fannedName = null;
  fanTiles = [];

  const master = Array.from(
    document.querySelectorAll<HTMLElement>('.card[data-name]:not([data-clone-of])'),
  ).find((m) => m.dataset.name === name && m.classList.contains('is-stacked-hidden'));

  const finish = () => {
    tiles.forEach((t) => {
      t.classList.remove('fan-tile');
      t.style.transition = '';
      t.style.transitionDelay = '';
      t.style.transform = '';
      t.style.transformOrigin = '';
      t.style.marginTop = '';
      t.style.zIndex = '';
    });
    if (master) collapseMaster(master); // remove tiles, show the stacked master
    applyStacking(); // re-assert the correct stacked/expanded state + overlap
    after?.();
  };

  if (prefersReducedMotion() || tiles.length === 0) {
    finish();
    return;
  }
  // Rubber-band the arc back to the upright stack (margins are already stacked).
  tiles.forEach((tile) => {
    tile.style.transition = `transform ${FAN_BACK_MS}ms ${FAN_BACK}`;
    tile.style.transform = 'rotate(0deg)';
  });
  window.setTimeout(finish, FAN_BACK_MS);
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
  // Collapse any expanded copies first so only master nodes are re-bucketed;
  // stacking is re-applied afterward. Keeps the DOM canonical across regroups.
  collapseAll();
  // Regroup every visual stacks view (main deck + considerations pool).
  document.querySelectorAll<HTMLElement>('.stacks-view').forEach((wrap) => regroupWrap(wrap, mode));

  // Re-expand per the current toggles, re-apply overlap + counts + view filter.
  applyStacking();
}

export { applyGrouping, applyView };

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
    // A stacked-hidden master is represented by its expanded clones — skip it so
    // it isn't double-counted alongside them.
    let qty = 0;
    let count = 0;
    cards.forEach((card) => {
      if (card.classList.contains('is-stacked-hidden')) return;
      qty += Number(card.dataset.qty ?? '1') || 1;
      count += 1;
    });
    const c = col.querySelector<HTMLElement>('.count');
    if (c) c.textContent = String(qty);
    col.dataset.empty = count === 0 ? 'true' : '';
  });
}

function initView(): void {
  // Group-by / density / stacking / mode are now driven by the dropdown facets
  // (see facets.ts). The six-view switcher stays a segmented control here.
  document.querySelectorAll<HTMLElement>('[data-view-btn]').forEach((btn) => {
    btn.addEventListener('click', () => applyView(btn.dataset.viewBtn as ViewName));
  });

  document.querySelectorAll<HTMLElement>('.card[data-name]').forEach(wireCard);

  document.addEventListener('click', () => {
    pinned = null;
    hidePane();
  });

  document.addEventListener('curation-changed', () => {
    // Physically move re-tagged cards between the main stacks and the
    // Considerations section (FLIP-animated). Collapse expansions first so the
    // relocation moves canonical master nodes, then re-apply stacking + view.
    const rerender = () => {
      collapseAll();
      relocateCards();
      applyStacking();
    };
    // If a re-tag came from an OPEN fan, let its spring-back rubber-band play to
    // completion first, THEN re-render (v4.4). Otherwise re-render immediately.
    if (isFanned()) fanClose(rerender);
    else rerender();
  });

  window.addEventListener('resize', () => {
    if (currentDensity === 'compact') applyDensity('compact');
  });

  const hash = window.location.hash.slice(1) as ViewName;
  const start: ViewName = hash in VIEW_MEMBERSHIPS ? hash : 'all';
  applyView(start);
  // Baseline look until facets restore persisted state (art / stacked).
  applyStacking();
}

export { initView };
