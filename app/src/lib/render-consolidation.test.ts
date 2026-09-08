// Non-singleton fixture test (v3.2 consolidation). Locks in that the SAME
// per-name diff (nameDiffMap/computeDiff) drives quantity AND membership across
// every consumer: the stacked badge, the expanded tiles, and the metadata
// buckets all agree for a Hare-Apparent-style partial drop + an add-to-existing.

import { describe, it, expect } from 'vitest';
import { nameDiffMap } from './diff';
import { stackBadge, expandTiles } from './instances';
import { stats } from './metadata';
import type { Changeset, DeckCard } from './types';

function card(name: string, quantity: number, type_line = 'Creature'): DeckCard {
  return {
    name,
    quantity,
    mana_value: 2,
    mana_cost: '{1}{W}',
    type_line,
    colors: ['W'],
    color_identity: ['W'],
  };
}

// Synthetic changeset exercising every non-singleton path in one deck:
//   Hare Apparent — deckQty 4, drop 2 (PARTIAL drop) → 2 survive
//   Plains        — deckQty 8, add 4 to existing (ADD-TO-EXISTING) → 12
//   Swords        — deckQty 3, drop 1 + add 2 on same name (MIXED)
const deck = [card('Hare Apparent', 4), card('Plains', 8, 'Basic Land'), card('Swords', 3)];
const changeset: Changeset = {
  adds: ['Plains', 'Swords'],
  drops: ['Hare Apparent', 'Swords'],
  considerations: [],
  addQty: { Plains: 4, Swords: 2 },
  dropQty: { 'Hare Apparent': 2, Swords: 1 },
};

describe('non-singleton render consolidation', () => {
  const perName = nameDiffMap(deck, changeset);

  it('partial drop keeps surviving copies (proposedQty = deckQty − dropQty + addQty)', () => {
    const hare = perName.get('Hare Apparent')!;
    expect(hare.untouchedQty).toBe(2);
    expect(hare.droppedQty).toBe(2);
    expect(hare.addedQty).toBe(0);
    expect(hare.proposedQty).toBe(2);
  });

  it('add-to-existing counts once and grows proposedQty', () => {
    const plains = perName.get('Plains')!;
    expect(plains.untouchedQty).toBe(8);
    expect(plains.addedQty).toBe(4);
    expect(plains.proposedQty).toBe(12);
  });

  it('stacked badge text + kind derive from the same netted counts', () => {
    const hare = perName.get('Hare Apparent')!;
    expect(stackBadge(hare)).toEqual({ text: '×4 → ×2 (−2)', kind: 'drop' });

    const plains = perName.get('Plains')!;
    expect(stackBadge(plains)).toEqual({ text: '×8 (+4)', kind: 'add' });

    const swords = perName.get('Swords')!;
    expect(swords.untouchedQty).toBe(2);
    expect(swords.droppedQty).toBe(1);
    expect(swords.addedQty).toBe(2);
    expect(stackBadge(swords)).toEqual({ text: '×3 → ×4', kind: 'mixed' });
  });

  it('expanded tiles carry the correct per-copy membership counts', () => {
    const hare = perName.get('Hare Apparent')!;
    const tiles = expandTiles(hare);
    expect(tiles.filter((t) => t === 'neutral')).toHaveLength(2);
    expect(tiles.filter((t) => t === 'drop')).toHaveLength(2);
    expect(tiles.filter((t) => t === 'add')).toHaveLength(0);

    const swords = expandTiles(perName.get('Swords')!);
    expect(swords).toEqual(['neutral', 'neutral', 'drop', 'add', 'add']);
  });

  // Mirror metadataLive's netted bucketing: current = u+r copies, proposed = u+a.
  it('metadata Current/Proposed/Δ net correctly (no double-count, survivors kept)', () => {
    const base = new Map(deck.map((c) => [c.name, c]));
    const current: { type_line: string; cmc: number; mana_cost: string | null; qty: number }[] = [];
    const proposed: typeof current = [];
    for (const nd of perName.values()) {
      const c = base.get(nd.name)!;
      const entry = (qty: number) => ({ type_line: c.type_line, cmc: c.mana_value, mana_cost: c.mana_cost, qty });
      if (nd.untouchedQty + nd.droppedQty > 0) current.push(entry(nd.untouchedQty + nd.droppedQty));
      if (nd.untouchedQty + nd.addedQty > 0) proposed.push(entry(nd.untouchedQty + nd.addedQty));
    }
    const cur = stats(current);
    const prop = stats(proposed);
    // Current: 4 Hare + 8 Plains + 3 Swords = 15 (8 lands)
    expect(cur.total).toBe(15);
    expect(cur.landCount).toBe(8);
    // Proposed: 2 Hare + 12 Plains + 4 Swords = 18 (12 lands) — survivors kept,
    // Plains counted once at its grown qty.
    expect(prop.total).toBe(18);
    expect(prop.landCount).toBe(12);
    expect(prop.total - cur.total).toBe(3); // net +3
  });
});
