import { describe, it, expect, vi } from 'vitest';
import { computeDiff, currentDeck, proposedDeck, nameDiffMap } from './diff';
import type { DeckCard, Changeset } from './types';

function card(name: string, quantity = 1): DeckCard {
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

const qtyTotal = (cards: DeckCard[]) => cards.reduce((a, c) => a + c.quantity, 0);
const names = (cards: DeckCard[]) => new Set(cards.map((c) => c.name));

describe('nameDiffMap — quantity netting', () => {
  it('nets proposedQty = deckQty − dropQty + addQty', () => {
    const deck = [card('Plains', 15), card('Sol Ring'), card('Silent Arbiter')];
    const cs: Changeset = { adds: ['Tome of Legends'], drops: ['Silent Arbiter'], considerations: [] };
    const m = nameDiffMap(deck, cs);
    expect(m.get('Plains')!.proposedQty).toBe(15);
    expect(m.get('Silent Arbiter')!.proposedQty).toBe(0);
    expect(m.get('Tome of Legends')!.proposedQty).toBe(1);
  });

  it('add-to-existing grows proposedQty (name in deck AND adds)', () => {
    const deck = [card('Hare Apparent', 4)];
    const cs: Changeset = {
      adds: ['Hare Apparent'],
      drops: [],
      considerations: [],
      addQty: { 'Hare Apparent': 3 },
    };
    const nd = nameDiffMap(deck, cs).get('Hare Apparent')!;
    expect(nd.deckQty).toBe(4);
    expect(nd.addedQty).toBe(3);
    expect(nd.untouchedQty).toBe(4);
    expect(nd.droppedQty).toBe(0);
    expect(nd.proposedQty).toBe(7);
  });

  it('partial drop → some untouched + some dropped', () => {
    const deck = [card('Petitioners', 6)];
    const cs: Changeset = {
      adds: [],
      drops: ['Petitioners'],
      considerations: [],
      dropQty: { Petitioners: 2 },
    };
    const nd = nameDiffMap(deck, cs).get('Petitioners')!;
    expect(nd.untouchedQty).toBe(4);
    expect(nd.droppedQty).toBe(2);
    expect(nd.proposedQty).toBe(4);
  });

  it('full drop → 0 untouched, 0 proposed', () => {
    const deck = [card('Silent Arbiter', 1)];
    const cs: Changeset = { adds: [], drops: ['Silent Arbiter'], considerations: [] };
    const nd = nameDiffMap(deck, cs).get('Silent Arbiter')!;
    expect(nd.untouchedQty).toBe(0);
    expect(nd.droppedQty).toBe(1);
    expect(nd.proposedQty).toBe(0);
  });

  it('over-drop clamps to owned copies and warns', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const deck = [card('Plains', 3)];
    const cs: Changeset = {
      adds: [],
      drops: ['Plains'],
      considerations: [],
      dropQty: { Plains: 5 },
    };
    const nd = nameDiffMap(deck, cs).get('Plains')!;
    expect(nd.dropQty).toBe(5); // requested (raw) preserved
    expect(nd.droppedQty).toBe(3); // clamped to owned
    expect(nd.untouchedQty).toBe(0);
    expect(nd.proposedQty).toBe(0);
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });

  it('mixed add + drop on the same name nets both', () => {
    const deck = [card('Mountain', 7)];
    const cs: Changeset = {
      adds: ['Mountain'],
      drops: ['Mountain'],
      considerations: [],
      addQty: { Mountain: 2 },
      dropQty: { Mountain: 3 },
    };
    const nd = nameDiffMap(deck, cs).get('Mountain')!;
    expect(nd.untouchedQty).toBe(4);
    expect(nd.droppedQty).toBe(3);
    expect(nd.addedQty).toBe(2);
    expect(nd.proposedQty).toBe(6);
  });
});

describe('computeDiff', () => {
  const deck = [card('Plains', 15), card('Sol Ring'), card('Silent Arbiter')];
  const cs: Changeset = {
    adds: ['Tome of Legends'],
    drops: ['Silent Arbiter'],
    considerations: ['Skullclamp'],
  };
  const diff = computeDiff(deck, cs);

  it('emits qty-weighted untouched / drops / adds sets', () => {
    expect(qtyTotal(diff.untouched)).toBe(16); // Plains 15 + Sol Ring 1
    expect(qtyTotal(diff.drops)).toBe(1); // Silent Arbiter
    expect(qtyTotal(diff.adds)).toBe(1); // Tome of Legends
  });

  it('a fully dropped name is absent from untouched', () => {
    expect(names(diff.untouched).has('Silent Arbiter')).toBe(false);
    expect(names(diff.drops).has('Silent Arbiter')).toBe(true);
  });

  it('considerations pass through as a disjoint set', () => {
    expect(diff.considerations.map((c) => c.name)).toEqual(['Skullclamp']);
  });

  it('currentDeck total = deck total (qty-weighted)', () => {
    expect(qtyTotal(currentDeck(diff))).toBe(qtyTotal(deck));
  });

  it('proposedDeck total = deck − drops + adds (qty-weighted, not size-preserving)', () => {
    expect(qtyTotal(proposedDeck(diff))).toBe(qtyTotal(deck) - 1 + 1);
  });

  it('add-to-existing produces a qty-weighted add tile without duplicating the deck copy', () => {
    const deck2 = [card('Hare Apparent', 4)];
    const cs2: Changeset = {
      adds: ['Hare Apparent'],
      drops: [],
      considerations: [],
      addQty: { 'Hare Apparent': 3 },
    };
    const d2 = computeDiff(deck2, cs2);
    expect(qtyTotal(d2.untouched)).toBe(4);
    expect(qtyTotal(d2.adds)).toBe(3);
    expect(qtyTotal(proposedDeck(d2))).toBe(7);
  });

  it('honors explicit addCards over name-only stubs', () => {
    const rich = [{ ...card('Tome of Legends'), type_line: 'Artifact', quantity: 1 }];
    const d2 = computeDiff(deck, cs, rich);
    expect(d2.adds.find((c) => c.name === 'Tome of Legends')?.type_line).toBe('Artifact');
  });
});
