// Pure, quantity-aware diff engine (v3.1). Nets adds/drops per name over the
// deck multiset — it NO LONGER assumes `adds ∩ deck = ∅`. A name present in both
// the deck and the adds list is valid (add more copies).
//
//   proposedQty(n) = deckQty(n) − dropQty(n) + addQty(n)
//   untouchedQty   = deckQty − dropQty
//   droppedQty     = dropQty            (clamped so dropQty ≤ deckQty)
//   addedQty       = addQty             (uncapped)

import type { DeckCard, Changeset } from './types';

/** Per-name quantity netting — the single source of truth for copy counts. */
export interface NameDiff {
  name: string;
  deckQty: number;
  dropQty: number;
  addQty: number;
  untouchedQty: number;
  droppedQty: number;
  addedQty: number;
  proposedQty: number;
}

export interface Diff {
  untouched: DeckCard[];
  drops: DeckCard[];
  adds: DeckCard[];
  considerations: DeckCard[];
  /** Per-name netted quantities, keyed by card name. */
  perName: Map<string, NameDiff>;
}

function stub(name: string, quantity = 1): DeckCard {
  return {
    name,
    quantity,
    mana_value: 0,
    mana_cost: null,
    type_line: 'Artifact',
    colors: [],
    color_identity: [],
  };
}

/** A card carrying either the raw-deck `quantity` or the enriched `qty`. */
type QtyCard = { name: string; quantity?: number; qty?: number };

function qtyOf(c: QtyCard): number {
  return c.quantity ?? c.qty ?? 1;
}

/**
 * Net the changeset against the deck multiset, per name. Warns and clamps when a
 * drop exceeds owned copies. Accepts raw deck cards (`quantity`) or enriched
 * cards (`qty`) so rendering and the diff share one computation.
 */
export function nameDiffMap(deck: QtyCard[], changeset: Changeset): Map<string, NameDiff> {
  const deckQty = new Map<string, number>();
  for (const c of deck) deckQty.set(c.name, (deckQty.get(c.name) ?? 0) + qtyOf(c));

  const countList = (names: string[], override?: Record<string, number>): Map<string, number> => {
    const m = new Map<string, number>();
    for (const n of names) m.set(n, (m.get(n) ?? 0) + 1);
    if (override) for (const [n, q] of Object.entries(override)) m.set(n, q);
    return m;
  };
  const dropQ = countList(changeset.drops, changeset.dropQty);
  const addQ = countList(changeset.adds, changeset.addQty);

  const names = new Set<string>([...deckQty.keys(), ...dropQ.keys(), ...addQ.keys()]);
  const out = new Map<string, NameDiff>();
  for (const name of names) {
    const dq = deckQty.get(name) ?? 0;
    const rawDrop = dropQ.get(name) ?? 0;
    const addQty = addQ.get(name) ?? 0;
    let dropQty = rawDrop;
    if (dropQty > dq) {
      // eslint-disable-next-line no-console
      console.warn(
        `[diff] drop of ${rawDrop}× "${name}" exceeds ${dq} owned; clamping to ${dq}.`,
      );
      dropQty = dq;
    }
    const untouchedQty = dq - dropQty;
    const proposedQty = dq - dropQty + addQty;
    out.set(name, {
      name,
      deckQty: dq,
      dropQty: rawDrop,
      addQty,
      untouchedQty,
      droppedQty: dropQty,
      addedQty: addQty,
      proposedQty,
    });
  }
  return out;
}

/**
 * Compute the quantity-aware diff. `addCards` (real enriched adds) is optional;
 * when absent, adds are name-only stubs. Considerations are a passthrough set.
 * Quantities on the emitted cards reflect the per-name netting.
 */
export function computeDiff(
  deck: DeckCard[],
  changeset: Changeset,
  addCards?: DeckCard[],
): Diff {
  const perName = nameDiffMap(deck, changeset);
  const deckByName = new Map(deck.map((c) => [c.name, c]));
  const addByName = new Map((addCards ?? []).map((c) => [c.name, c]));

  const untouched: DeckCard[] = [];
  const drops: DeckCard[] = [];
  const adds: DeckCard[] = [];

  for (const nd of perName.values()) {
    const base = deckByName.get(nd.name);
    if (nd.untouchedQty > 0 && base) {
      untouched.push({ ...base, quantity: nd.untouchedQty });
    }
    if (nd.droppedQty > 0 && base) {
      drops.push({ ...base, quantity: nd.droppedQty });
    }
    if (nd.addedQty > 0) {
      const rich = addByName.get(nd.name);
      adds.push(rich ? { ...rich, quantity: nd.addedQty } : stub(nd.name, nd.addedQty));
    }
  }

  const considerations = (changeset.considerations ?? []).map((n) => stub(n));
  return { untouched, drops, adds, considerations, perName };
}

/** The deck as it stands today: untouched + the cards being dropped (qty-weighted). */
export function currentDeck(d: Diff): DeckCard[] {
  return [...d.untouched, ...d.drops];
}

/** The deck as proposed: untouched + the cards being added (qty-weighted). */
export function proposedDeck(d: Diff): DeckCard[] {
  return [...d.untouched, ...d.adds];
}
