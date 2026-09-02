// Pure diff engine. Reconstructed from diff.test.ts usage after the original was
// lost. Splits (deck, changeset) into disjoint sets and carries considerations.

import type { DeckCard, Changeset } from './types';

export interface Diff {
  untouched: DeckCard[];
  drops: DeckCard[];
  adds: DeckCard[];
  considerations: DeckCard[];
}

function stub(name: string): DeckCard {
  return {
    name,
    quantity: 1,
    mana_value: 0,
    mana_cost: null,
    type_line: 'Artifact',
    colors: [],
    color_identity: [],
  };
}

/**
 * Compute the diff. `addCards` (real enriched adds) is optional; when absent,
 * adds are name-only qty-1 stubs. Considerations are a passthrough set.
 */
export function computeDiff(
  deck: DeckCard[],
  changeset: Changeset,
  addCards?: DeckCard[],
): Diff {
  const dropSet = new Set(changeset.drops);
  const untouched = deck.filter((c) => !dropSet.has(c.name));
  const drops = deck.filter((c) => dropSet.has(c.name));
  const adds = addCards ?? changeset.adds.map(stub);
  const considerations = (changeset.considerations ?? []).map(stub);
  return { untouched, drops, adds, considerations };
}

/** The deck as it stands today: untouched + the cards being dropped. */
export function currentDeck(d: Diff): DeckCard[] {
  return [...d.untouched, ...d.drops];
}

/** The deck as proposed: untouched + the cards being added. */
export function proposedDeck(d: Diff): DeckCard[] {
  return [...d.untouched, ...d.adds];
}
