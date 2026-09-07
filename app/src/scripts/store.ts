// localStorage-backed curation store (v4.1). Per-INSTANCE state: each name holds
// a list of instances (one per copy), each carrying its own membership + note.
// Seeds from the server-rendered per-name counts (nameDiffMap SSOT) captured off
// the DOM. Pure logic lives in lib/curation.ts; this is the DOM/storage wrapper.

import {
  buildInstanceSeed,
  buildInstanceExport,
  nameCounts as nameCountsPure,
  instanceMembership as instanceMembershipPure,
  storageKey,
  type Instance,
  type InstanceState,
  type Membership,
  type InstanceExport,
  type NameCounts,
} from '../lib/curation';
import { stackBadge } from '../lib/instances';

/** Stacked delta badge from raw u/r/a counts. */
function stackBadgeFor(u: number, r: number, a: number) {
  return stackBadge({ untouchedQty: u, droppedQty: r, addedQty: a });
}

interface Baseline {
  deckName: string;
  changeset: { adds: string[]; drops: string[]; considerations: string[] };
}

let key = '';
let state: InstanceState = {};
/** File baseline snapshot, captured once before any saved state is applied. */
let baselineState: InstanceState = {};

function cloneState(src: InstanceState): InstanceState {
  const out: InstanceState = {};
  for (const [k, list] of Object.entries(src)) out[k] = list.map((i) => ({ ...i }));
  return out;
}

function readBaseline(): Baseline {
  const el = document.getElementById('baseline-data');
  try {
    return JSON.parse(el?.textContent || '{}') as Baseline;
  } catch {
    return { deckName: 'deck', changeset: { adds: [], drops: [], considerations: [] } };
  }
}

/**
 * Seed the instance state straight from the server-rendered DOM. Each master
 * card (not an expanded clone) carries its netted u/r/a counts; considerations
 * carry their copy qty. This is exactly the nameDiffMap seed the SSR rendered.
 */
function seedFromDom(): InstanceState {
  const names: { name: string; u: number; r: number; a: number }[] = [];
  const considerations: { name: string; qty: number }[] = [];
  document
    .querySelectorAll<HTMLElement>('.card[data-name]:not([data-clone-of])')
    .forEach((el) => {
      const name = el.dataset.name!;
      if (el.closest('[data-considerations]')) {
        considerations.push({ name, qty: Number(el.dataset.qty) || 1 });
        return;
      }
      names.push({
        name,
        u: Number(el.dataset.untouchedQty) || 0,
        r: Number(el.dataset.droppedQty) || 0,
        a: Number(el.dataset.addedQty) || 0,
      });
    });
  return buildInstanceSeed({ names, considerations });
}

/** Validate a parsed localStorage payload is a `Record<name, Instance[]>`. */
function isInstanceState(v: unknown): v is InstanceState {
  if (!v || typeof v !== 'object') return false;
  for (const list of Object.values(v as Record<string, unknown>)) {
    if (!Array.isArray(list)) return false;
    for (const inst of list) {
      if (!inst || typeof inst !== 'object' || typeof (inst as Instance).membership !== 'string') {
        return false;
      }
    }
  }
  return true;
}

function loadSaved(): InstanceState | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return isInstanceState(parsed) ? (parsed as InstanceState) : null;
  } catch {
    return null;
  }
}

function persist(): void {
  try {
    localStorage.setItem(key, JSON.stringify(state));
  } catch {
    /* storage full / unavailable — in-memory state still works */
  }
}

/* ------------------------------------------------------------------ *
 * Derived reads
 * ------------------------------------------------------------------ */

export function nameCounts(name: string): NameCounts {
  return nameCountsPure(state, name);
}

export function instancesOf(name: string): Instance[] {
  return state[name] ?? [];
}

export function instanceMembership(name: string, i: number): Membership {
  return instanceMembershipPure(state, name, i);
}

export function instanceCount(name: string): number {
  return state[name]?.length ?? 0;
}

/**
 * Whole-name membership (backward-compat / info-pane). A singleton reports its
 * one instance; a uniform group reports that shared membership; a mixed group
 * falls back to 'untouched' (deck resident) — mixed consumers read nameCounts.
 */
export function getMembership(name: string): Membership {
  const list = state[name];
  if (!list || list.length === 0) return 'untouched';
  if (list.length === 1) return list[0].membership;
  const first = list[0].membership;
  return list.every((i) => i.membership === first) ? first : 'untouched';
}

export function getNote(name: string): string | null {
  return state[name]?.[0]?.note ?? null;
}

export function getInstanceNote(name: string, i: number): string | null {
  return state[name]?.[i]?.note ?? null;
}

/* ------------------------------------------------------------------ *
 * Mutations (instance-addressed)
 * ------------------------------------------------------------------ */

export function setInstanceMembership(name: string, i: number, membership: Membership): void {
  const list = state[name];
  if (!list || !list[i]) return;
  list[i] = { ...list[i], membership };
  persist();
  applyStateToDom();
}

export function setInstanceNote(name: string, i: number, note: string | null): void {
  const list = state[name];
  if (!list || !list[i]) return;
  list[i] = { ...list[i], note };
  persist();
  applyStateToDom();
}

/** Whole-name setter (singleton path) — retags instance 0. */
export function setMembership(name: string, membership: Membership): void {
  setInstanceMembership(name, 0, membership);
}

export function setNote(name: string, note: string | null): void {
  setInstanceNote(name, 0, note);
}

/* ------------------------------------------------------------------ *
 * DOM reflection — keep each master's data-* counts + border + badge in sync
 * with the instance state so view filtering / relocation / metadata (all of
 * which read the DOM data attributes) see the live per-instance netting.
 * ------------------------------------------------------------------ */

function primaryMembership(c: NameCounts, total: number): Membership {
  if (total > 0 && c.dismissed === total) return 'dismissed';
  if (c.consideration > 0 && c.u + c.r + c.a === 0) return 'consideration';
  const kind = stackBadgeFor(c.u, c.r, c.a).kind;
  if (kind === 'drop') return 'drop';
  if (kind === 'add') return 'add';
  return 'untouched';
}

function borderFor(membership: Membership, c: NameCounts): string {
  if (membership === 'consideration') return 'consideration';
  const urA = c.u + c.r + c.a;
  if (urA > 1) {
    const kind = stackBadgeFor(c.u, c.r, c.a).kind;
    return kind === 'add' ? 'add' : kind === 'drop' ? 'drop' : kind === 'mixed' ? 'mixed' : 'neutral';
  }
  if (membership === 'add') return 'add';
  if (membership === 'drop') return 'drop';
  return 'neutral';
}

export function applyStateToDom(): void {
  document
    .querySelectorAll<HTMLElement>('.card[data-name]:not([data-clone-of])')
    .forEach((master) => {
      const name = master.dataset.name!;
      const list = state[name];
      if (!list) return;
      const c = nameCountsPure(state, name);
      const total = list.length;
      const membership = primaryMembership(c, total);

      master.dataset.untouchedQty = String(c.u);
      master.dataset.droppedQty = String(c.r);
      master.dataset.addedQty = String(c.a);
      master.dataset.qty = String(total);
      master.dataset.membership = membership;

      master.classList.remove('add', 'drop', 'neutral', 'mixed', 'consideration');
      if (membership !== 'dismissed') master.classList.add(borderFor(membership, c));

      // Update the stacked ×N delta badge (present only when total > 1).
      const badgeEl = master.querySelector<HTMLElement>('.qty-badge');
      if (badgeEl) {
        const badge = stackBadgeFor(c.u, c.r, c.a);
        badgeEl.textContent = badge.text;
        badgeEl.classList.remove(
          'qty-badge--unchanged',
          'qty-badge--add',
          'qty-badge--drop',
          'qty-badge--mixed',
        );
        badgeEl.classList.add(`qty-badge--${badge.kind}`);
      }

      master.classList.toggle('is-dismissed', membership === 'dismissed');
      master.classList.toggle('has-note', list.some((i) => !!(i.note && i.note.trim())));
    });
  document.dispatchEvent(new CustomEvent('curation-changed'));
}

/* ------------------------------------------------------------------ *
 * Export / recovery / reset
 * ------------------------------------------------------------------ */

export function exportCuration(): InstanceExport {
  return buildInstanceExport(state);
}

export function dismissedCount(): number {
  let n = 0;
  for (const list of Object.values(state)) for (const i of list) if (i.membership === 'dismissed') n++;
  return n;
}

export function restoreDismissed(): void {
  for (const list of Object.values(state)) {
    list.forEach((inst, idx) => {
      if (inst.membership === 'dismissed') list[idx] = { ...inst, membership: 'consideration' };
    });
  }
  persist();
  applyStateToDom();
}

export function resetToProposed(): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
  state = cloneState(baselineState);
  applyStateToDom();
}

export function initStore(): void {
  const baseline = readBaseline();
  // Instance schema uses its own key suffix so stale v2 name-level state is ignored.
  key = `${storageKey(baseline.deckName, baseline.changeset)}:inst`;
  baselineState = seedFromDom();
  state = loadSaved() ?? cloneState(baselineState);
  applyStateToDom();
}
