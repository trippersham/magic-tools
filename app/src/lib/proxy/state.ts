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
    // A newly-picked art defaults to the card's target quantity (so a "need 2"
    // card auto-orders 2 of the chosen art); user can still adjust the stepper.
    const qty = card.selections.length === 0 ? Math.max(1, card.targetQty || 1) : 1;
    return {
      ...card,
      selections: [...card.selections, { ppId, quantity: qty }],
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

export function setTargetQty(state: AppState, localId: string, n: number): AppState {
  const clamped = Math.max(1, Math.floor(n));
  return mapCard(state, localId, (card) => ({ ...card, targetQty: clamped }));
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
// Ordering
// ---------------------------------------------------------------------------

/**
 * A card is "pp-backed" if any selection references a real printingproxies
 * print — i.e. a ppId that is neither blank (a buy-only row) nor an `sf:`
 * Scryfall fallback. These are the arts we can actually order from pp.
 */
export function hasPpArt(card: CardEntry): boolean {
  return card.selections.some((s) => s.ppId !== '' && !s.ppId.startsWith('sf:'));
}

/**
 * Stable partition that sinks Scryfall-only picks to the bottom: cards with an
 * orderable pp art (and still-unchosen cards, which belong in the working area)
 * stay on top; a card whose every selection is a Scryfall/blank row goes last.
 * Relative order within each group is preserved.
 */
export function sortScryfallLast(cards: CardEntry[]): CardEntry[] {
  const rank = (c: CardEntry): number =>
    c.selections.length > 0 && !hasPpArt(c) ? 1 : 0;
  return cards
    .map((card, i) => ({ card, i }))
    .sort((a, b) => rank(a.card) - rank(b.card) || a.i - b.i)
    .map(({ card }) => card);
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
    const arts: PpArt[] = [];
    let blankTarget = 0; // target carried by a fully-blank (unchosen) row
    for (const r of groupRows) {
      const hasPp = r.ppId !== '';
      const hasLink = r.scryfallLink !== '';
      if (!hasPp && !hasLink) {
        // Fully-blank row = a card that has NO selection yet (in-progress export).
        // Record its target quantity but add no selection, so it shows as "not
        // chosen" and the user can still pick art on expand.
        blankTarget = Math.max(blankTarget, r.quantity);
        continue;
      }
      // A real pick (has PP ID) or a buy row (blank PP ID but has a link).
      selections.push({ ppId: r.ppId, quantity: r.quantity });
      // Synthesize a minimal PpArt so a restore -> export before the row is lazily
      // reloaded still carries the link + confidence. setArts() replaces these on expand.
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
    const selectedTotal = selections.reduce((n, s) => n + s.quantity, 0);
    cards.push({
      localId: `restore-${counter++}`,
      name,
      oracleId: '',
      arts,
      artsStatus: 'unloaded',
      selections,
      collapsed: true, // start collapsed; expanding a row lazy-loads its art grid
      // target = ordered copies (sum of selections) or, for an unchosen card, the blank row's qty
      targetQty: Math.max(1, selectedTotal || blankTarget),
    });
  }

  return { schemaVersion: 1, cards: sortScryfallLast(cards), lastExportSignature: null };
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
