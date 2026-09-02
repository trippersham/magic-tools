import { describe, it, expect } from 'vitest';
import { computeDiff, currentDeck, proposedDeck } from './diff';
import type { DeckCard, Changeset } from './types';
import deckJson from '../data/deck.json';
import changesetJson from '../data/changeset.json';

// The v2 authored changeset (objects with reasons).
const v2 = changesetJson as {
  adds: { name: string }[];
  drops: { name: string }[];
  considerations: { name: string }[];
};

// Name-only changeset that the diff math keys off.
const changeset: Changeset = {
  adds: v2.adds.map((a) => a.name),
  drops: v2.drops.map((d) => d.name),
  considerations: v2.considerations.map((c) => c.name),
};

const addNames = new Set(changeset.adds);

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

// deck.json ships as the *proposed* deck (it already contains the adds). Build a
// realistic *current* deck: the untouched real cards plus stubs for the drops,
// so that drops ⊆ deck and adds ∩ deck = ∅ hold as they would pre-upgrade.
const realDeck = deckJson as DeckCard[];
const untouchedReal = realDeck.filter((c) => !addNames.has(c.name));
const deck: DeckCard[] = [...untouchedReal, ...changeset.drops.map(stub)];

const names = (cards: DeckCard[]) => new Set(cards.map((c) => c.name));

describe('computeDiff', () => {
  const diff = computeDiff(deck, changeset);

  it('drops are a subset of the deck', () => {
    const deckNames = names(deck);
    for (const d of diff.drops) expect(deckNames.has(d.name)).toBe(true);
    expect(diff.drops.length).toBe(changeset.drops.length);
  });

  it('adds are disjoint from the deck', () => {
    const deckNames = names(deck);
    for (const a of diff.adds) expect(deckNames.has(a.name)).toBe(false);
  });

  it('adds and drops are disjoint from each other', () => {
    const dropNames = names(diff.drops);
    for (const a of diff.adds) expect(dropNames.has(a.name)).toBe(false);
  });

  it('considerations pass through, disjoint from deck / adds / drops', () => {
    const deckNames = names(deck);
    const addN = names(diff.adds);
    const dropN = names(diff.drops);
    expect(diff.considerations.map((c) => c.name)).toEqual(changeset.considerations);
    for (const c of diff.considerations) {
      expect(deckNames.has(c.name)).toBe(false);
      expect(addN.has(c.name)).toBe(false);
      expect(dropN.has(c.name)).toBe(false);
    }
  });

  it('untouched contains no dropped card', () => {
    const dropN = names(diff.drops);
    for (const u of diff.untouched) expect(dropN.has(u.name)).toBe(false);
  });

  it('untouched + drops partition the deck exactly', () => {
    expect(diff.untouched.length + diff.drops.length).toBe(deck.length);
    const recombined = names([...diff.untouched, ...diff.drops]);
    expect(recombined).toEqual(names(deck));
  });

  it('proposedCount = deck.length − drops + adds (do not assume size-preserving)', () => {
    const proposed = proposedDeck(diff);
    expect(proposed.length).toBe(deck.length - diff.drops.length + diff.adds.length);
  });

  it('currentDeck reconstructs the original deck set', () => {
    expect(names(currentDeck(diff))).toEqual(names(deck));
    expect(currentDeck(diff).length).toBe(deck.length);
  });

  it('proposedDeck = untouched + adds', () => {
    expect(names(proposedDeck(diff))).toEqual(names([...diff.untouched, ...diff.adds]));
  });

  it('honors explicit addCards over name-only stubs', () => {
    const rich = changeset.adds.map((n) => ({ ...stub(n), quantity: 4, type_line: 'Creature' }));
    const d2 = computeDiff(deck, changeset, rich);
    expect(d2.adds.every((c) => c.quantity === 4)).toBe(true);
    expect(d2.adds.length).toBe(changeset.adds.length);
  });
});
