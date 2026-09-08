import { describe, it, expect } from 'vitest';
import { labelColumnsOf, hasAnyLabels } from './grouping';
import type { TaggedCard } from './types';

function card(name: string, labels?: string[], cmc = 0): TaggedCard {
  return {
    name,
    qty: 1,
    cmc,
    mana_cost: null,
    type_line: 'Artifact',
    colors: [],
    image_normal: null,
    usd: null,
    reason: null,
    cut: null,
    labels,
    membership: 'untouched',
  };
}

describe('labelColumnsOf — label bucketing', () => {
  it('places a multi-label card in each of its label columns', () => {
    const cards = [card('Goldvein Pick', ['Equipment', 'Card draw'])];
    const cols = labelColumnsOf(cards, ['Equipment', 'Card draw']);
    const equip = cols.find((c) => c.group === 'Equipment');
    const draw = cols.find((c) => c.group === 'Card draw');
    expect(equip?.cards.map((c) => c.name)).toEqual(['Goldvein Pick']);
    expect(draw?.cards.map((c) => c.name)).toEqual(['Goldvein Pick']);
  });

  it('sends cards with no labels to an Unlabeled column, ordered last', () => {
    const cards = [card('Mystery'), card('Sol Ring', ['Ramp / fast mana'])];
    const cols = labelColumnsOf(cards, ['Ramp / fast mana']);
    expect(cols.map((c) => c.group)).toEqual(['Ramp / fast mana', 'Unlabeled']);
    expect(cols[cols.length - 1].group).toBe('Unlabeled');
    expect(cols[1].cards.map((c) => c.name)).toEqual(['Mystery']);
  });

  it('orders columns by label_order first, then first-seen for the rest', () => {
    const cards = [
      card('A', ['Zeta']), // not in order → first-seen
      card('B', ['Equipment']), // in order
      card('C', ['Alpha']), // not in order → first-seen (after Zeta)
      card('D', ['Aura']), // in order
    ];
    const order = ['Aura', 'Equipment'];
    const cols = labelColumnsOf(cards, order);
    expect(cols.map((c) => c.group)).toEqual(['Aura', 'Equipment', 'Zeta', 'Alpha']);
  });

  it('yields zero label columns when no card has any label', () => {
    const cards = [card('A'), card('B')];
    const cols = labelColumnsOf(cards, ['Equipment']);
    // Only the Unlabeled bucket — no real label columns.
    expect(cols.filter((c) => c.group !== 'Unlabeled')).toHaveLength(0);
    expect(cols.map((c) => c.group)).toEqual(['Unlabeled']);
  });

  it('produces no columns at all for an empty card list', () => {
    expect(labelColumnsOf([], ['Equipment'])).toEqual([]);
  });

  it('sorts within a column by mana value then name', () => {
    const cards = [
      card('Zed', ['X'], 1),
      card('Alpha', ['X'], 3),
      card('Beta', ['X'], 1),
    ];
    const cols = labelColumnsOf(cards, ['X']);
    expect(cols[0].cards.map((c) => c.name)).toEqual(['Beta', 'Zed', 'Alpha']);
  });
});

describe('hasAnyLabels', () => {
  it('is false when no card carries a non-empty labels array', () => {
    expect(hasAnyLabels([card('A'), card('B', [])])).toBe(false);
  });
  it('is true when at least one card has a label', () => {
    expect(hasAnyLabels([card('A'), card('B', ['Equipment'])])).toBe(true);
  });
});
