/**
 * Pure AppState transitions for the Proxy Art Picker.
 *
 * Every function is `(state, ...args) => newState` — never mutates input.
 *
 * Design decisions:
 * - `importSeed(state, newCards)` takes pre-resolved CardEntry[] and merges
 *   by oracleId. The `identifiers` param from the plan is dropped — the caller
 *   resolves identifiers via scryfall before calling this.
 * - `applyRestore(rows)` synthesises localId as `restore-<counter>` and
 *   oracleId as the empty string (unknown until scryfall resolves later).
 * - Buy rows use `ppId === ''` (empty string), consistent with csv.ts.
 * - `signature` uses JSON.stringify of the exportable projection. This is
 *   stable given the same card + selection order (order-sensitive by design).
 */

import type { AppState, CardEntry, PpArt, Selection } from './types';
import type { RestoreRow } from './csv';

// ---------------------------------------------------------------------------
// Constructors
// ---------------------------------------------------------------------------

export function emptyState(): AppState {
  return { schemaVersion: 1, cards: [], lastExportSignature: null };
}

// ---------------------------------------------------------------------------
// Card-level transitions
// ---------------------------------------------------------------------------

export function addCard(state: AppState, card: CardEntry): AppState {
  return { ...state, cards: [...state.cards, card] };
}

export function removeCard(state: AppState, localId: string): AppState {
  return { ...state, cards: state.cards.filter((c) => c.localId !== localId) };
}

// ---------------------------------------------------------------------------
// Selection transitions
// ---------------------------------------------------------------------------

export function toggleSelection(
  state: AppState,
  localId: string,
  ppId: string,
): AppState {
  return mapCard(state, localId, (card) => {
    const existing = card.selections.find((s) => s.ppId === ppId);
    if (existing) {
      return {
        ...card,
        selections: card.selections.filter((s) => s.ppId !== ppId),
      };
    }
    return {
      ...card,
      selections: [...card.selections, { ppId, quantity: 1 }],
    };
  });
}

export function setQuantity(
  state: AppState,
  localId: string,
  ppId: string,
  n: number,
): AppState {
  const clamped = Math.max(1, n);
  return mapCard(state, localId, (card) => {
    const idx = card.selections.findIndex((s) => s.ppId === ppId);
    if (idx === -1) return card; // no-op
    const updated = [...card.selections];
    updated[idx] = { ...updated[idx], quantity: clamped };
    return { ...card, selections: updated };
  });
}

export function setArts(
  state: AppState,
  localId: string,
  arts: PpArt[],
  status: CardEntry['artsStatus'],
): AppState {
  return mapCard(state, localId, (card) => ({
    ...card,
    arts,
    artsStatus: status,
  }));
}

// ---------------------------------------------------------------------------
// Import / merge
// ---------------------------------------------------------------------------

/**
 * Additive merge: append `newCards` whose oracleId is not already present.
 * Existing cards with the same oracleId are kept as-is (no duplication).
 */
export function importSeed(
  state: AppState,
  newCards: CardEntry[],
): AppState {
  const existingIds = new Set(state.cards.map((c) => c.oracleId));
  const toAdd = newCards.filter((c) => !existingIds.has(c.oracleId));
  return { ...state, cards: [...state.cards, ...toAdd] };
}

/**
 * Replace state with rows from a restored CSV.
 * Groups rows by cardName; each group becomes a CardEntry.
 * localId is synthesised as `restore-<n>`; oracleId is '' (resolved later).
 */
/** Extract the set code from a Scryfall printing URL (…/card/<set>/<cn>/…) as a
 *  best-effort collapsed-row label until the full art list is lazily loaded. */
function setCodeFromLink(link: string): string {
  const m = /\/card\/([^/]+)\//.exec(link || '');
  return m ? m[1] : '';
}

export function applyRestore(rows: RestoreRow[]): AppState {
  const groups = new Map<string, RestoreRow[]>();
  for (const row of rows) {
    const existing = groups.get(row.cardName);
    if (existing) {
      existing.push(row);
    } else {
      groups.set(row.cardName, [row]);
    }
  }

  let counter = 0;
  const cards: CardEntry[] = [];
  for (const [name, groupRows] of groups) {
    const selections: Selection[] = [];
    // Synthesize a minimal PpArt per selection so a restore -> export BEFORE
    // the row is lazily reloaded still carries the Scryfall link + confidence
    // (and, for buy rows, the link) instead of blanking those CSV columns.
    // On expand, setArts() replaces these with the full fetched art list.
    const arts: PpArt[] = [];
    for (const r of groupRows) {
      selections.push({ ppId: r.ppId, quantity: r.quantity });
      arts.push({
        ppId: r.ppId,
        setName: setCodeFromLink(r.scryfallLink),
        score: r.confidence, // 0-100; toCsv divides by 100
        imageUrl: '',
        scryfallUri: r.scryfallLink,
        kind: 'official',
        stolenValor: 'none',
      });
    }
    cards.push({
      localId: `restore-${counter++}`,
      name,
      oracleId: '',
      arts,
      artsStatus: 'unloaded',
      selections,
      collapsed: false,
    });
  }

  return { schemaVersion: 1, cards, lastExportSignature: null };
}

// ---------------------------------------------------------------------------
// Export projection + dirty tracking
// ---------------------------------------------------------------------------

export interface ExportableRow {
  cardName: string;
  ppId: string;
  quantity: number;
}

/** Flatten selections into rows; exclude cards with zero selections. */
export function exportableRows(state: AppState): ExportableRow[] {
  const rows: ExportableRow[] = [];
  for (const card of state.cards) {
    for (const sel of card.selections) {
      rows.push({ cardName: card.name, ppId: sel.ppId, quantity: sel.quantity });
    }
  }
  return rows;
}

/** Stable string hash of the exportable projection. */
export function signature(state: AppState): string {
  return JSON.stringify(exportableRows(state));
}

export function isDirty(state: AppState, lastSig: string | null): boolean {
  if (lastSig === null) return state.cards.some((c) => c.selections.length > 0);
  return signature(state) !== lastSig;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function mapCard(
  state: AppState,
  localId: string,
  fn: (card: CardEntry) => CardEntry,
): AppState {
  const idx = state.cards.findIndex((c) => c.localId === localId);
  if (idx === -1) return state;
  const updated = [...state.cards];
  updated[idx] = fn(updated[idx]);
  return { ...state, cards: updated };
}
