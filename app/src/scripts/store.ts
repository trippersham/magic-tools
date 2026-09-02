// localStorage-backed curation store (v2.7 / v2.8). Holds the resolved
// per-card membership + notes, keyed by deck name + baseline changeset hash so
// a data change invalidates stale curation. Pure logic lives in lib/curation.ts.

import {
  buildExport,
  storageKey,
  type CardState,
  type Membership,
  type CurationExport,
} from '../lib/curation';

interface Baseline {
  deckName: string;
  changeset: { adds: string[]; drops: string[]; considerations: string[] };
}

let key = '';
let state: Record<string, CardState> = {};
/** File baseline snapshot, captured once before any saved state is applied. */
let baselineState: Record<string, CardState> = {};

function cloneStates(src: Record<string, CardState>): Record<string, CardState> {
  const out: Record<string, CardState> = {};
  for (const [k, v] of Object.entries(src)) out[k] = { ...v };
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

/** Baseline membership straight from the server-rendered DOM. */
function initialFromDom(): Record<string, CardState> {
  const st: Record<string, CardState> = {};
  document.querySelectorAll<HTMLElement>('.card[data-name]').forEach((el) => {
    const name = el.dataset.name!;
    st[name] = { membership: (el.dataset.membership as Membership) ?? 'untouched', note: null };
  });
  return st;
}

function loadSaved(): Record<string, CardState> | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Record<string, CardState>;
    return parsed && typeof parsed === 'object' ? parsed : null;
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

/** Reflect the current state onto the DOM (borders via data-membership, hide dismissed). */
export function applyStateToDom(): void {
  document.querySelectorAll<HTMLElement>('.card[data-name]').forEach((el) => {
    const name = el.dataset.name!;
    const s = state[name];
    if (!s) return;
    el.dataset.membership = s.membership;
    el.classList.remove('add', 'drop', 'neutral', 'consideration');
    el.classList.add(borderClass(s.membership));
    el.classList.toggle('is-dismissed', s.membership === 'dismissed');
    el.classList.toggle('has-note', !!(s.note && s.note.trim()));
  });
  document.dispatchEvent(new CustomEvent('curation-changed'));
}

function borderClass(m: Membership): string {
  if (m === 'add') return 'add';
  if (m === 'drop') return 'drop';
  if (m === 'consideration') return 'consideration';
  return 'neutral';
}

export function getMembership(name: string): Membership {
  return state[name]?.membership ?? 'untouched';
}

export function getNote(name: string): string | null {
  return state[name]?.note ?? null;
}

export function setMembership(name: string, membership: Membership): void {
  const prev = state[name] ?? { membership: 'untouched', note: null };
  state[name] = { ...prev, membership };
  persist();
  applyStateToDom();
}

export function setNote(name: string, note: string | null): void {
  const prev = state[name] ?? { membership: 'untouched', note: null };
  state[name] = { ...prev, note };
  persist();
  applyStateToDom();
}

export function exportCuration(): CurationExport {
  return buildExport(state);
}

/** Count of dismissed cards (for the minimal recovery affordance). */
export function dismissedCount(): number {
  return Object.values(state).filter((s) => s.membership === 'dismissed').length;
}

/** Restore all dismissed cards back to considerations (minimal recovery, v2.10). */
export function restoreDismissed(): void {
  for (const [name, s] of Object.entries(state)) {
    if (s.membership === 'dismissed') state[name] = { ...s, membership: 'consideration' };
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
  state = cloneStates(baselineState);
  applyStateToDom();
}

export function initStore(): void {
  const baseline = readBaseline();
  key = storageKey(baseline.deckName, baseline.changeset);
  // Snapshot the file baseline from the untouched server-rendered DOM FIRST,
  // so reset can always return to it regardless of any persisted state.
  baselineState = initialFromDom();
  state = loadSaved() ?? cloneStates(baselineState);
  applyStateToDom();
}
