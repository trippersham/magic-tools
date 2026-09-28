import { describe, it, expect } from 'vitest';
import {
  emptyState,
  addCard,
  removeCard,
  toggleSelection,
  setQuantity,
  setArts,
  importSeed,
  applyRestore,
  exportableRows,
  signature,
  isDirty,
} from './state';
import type { AppState, CardEntry, PpArt } from './types';

function mkArt(overrides: Partial<PpArt> = {}): PpArt {
  return {
    ppId: 'art-1',
    setName: 'Revised',
    score: 74.8,
    imageUrl: 'https://example.com/img.jpg',
    kind: 'official',
    stolenValor: 'none',
    ...overrides,
  };
}

function mkCard(overrides: Partial<CardEntry> = {}): CardEntry {
  return {
    localId: 'c1',
    name: 'Underground Sea',
    oracleId: 'oracle-us',
    arts: [],
    artsStatus: 'unloaded',
    selections: [],
    collapsed: false,
    ...overrides,
  };
}

describe('emptyState', () => {
  it('returns a valid empty AppState', () => {
    const s = emptyState();
    expect(s.schemaVersion).toBe(1);
    expect(s.cards).toEqual([]);
    expect(s.lastExportSignature).toBeNull();
  });
});

describe('addCard', () => {
  it('appends a card', () => {
    const s0 = emptyState();
    const card = mkCard();
    const s1 = addCard(s0, card);
    expect(s1.cards).toHaveLength(1);
    expect(s1.cards[0].localId).toBe('c1');
    // immutability
    expect(s0.cards).toHaveLength(0);
  });
});

describe('removeCard', () => {
  it('removes by localId', () => {
    const s0 = addCard(emptyState(), mkCard({ localId: 'c1' }));
    const s1 = addCard(s0, mkCard({ localId: 'c2', name: 'Timetwister', oracleId: 'o2' }));
    const s2 = removeCard(s1, 'c1');
    expect(s2.cards).toHaveLength(1);
    expect(s2.cards[0].localId).toBe('c2');
  });
});

describe('toggleSelection', () => {
  it('adds a selection when absent', () => {
    const s0 = addCard(emptyState(), mkCard());
    const s1 = toggleSelection(s0, 'c1', 'art-1');
    expect(s1.cards[0].selections).toEqual([{ ppId: 'art-1', quantity: 1 }]);
  });

  it('removes a selection when present', () => {
    let s = addCard(emptyState(), mkCard());
    s = toggleSelection(s, 'c1', 'art-1');
    s = toggleSelection(s, 'c1', 'art-1');
    expect(s.cards[0].selections).toEqual([]);
  });

  it('is a no-op for unknown localId', () => {
    const s0 = addCard(emptyState(), mkCard());
    const s1 = toggleSelection(s0, 'unknown', 'art-1');
    expect(s1).toEqual(s0);
  });
});

describe('setQuantity', () => {
  it('sets quantity on an existing selection', () => {
    let s = addCard(emptyState(), mkCard());
    s = toggleSelection(s, 'c1', 'art-1');
    s = setQuantity(s, 'c1', 'art-1', 5);
    expect(s.cards[0].selections[0].quantity).toBe(5);
  });

  it('clamps to min 1', () => {
    let s = addCard(emptyState(), mkCard());
    s = toggleSelection(s, 'c1', 'art-1');
    s = setQuantity(s, 'c1', 'art-1', 0);
    expect(s.cards[0].selections[0].quantity).toBe(1);
    s = setQuantity(s, 'c1', 'art-1', -3);
    expect(s.cards[0].selections[0].quantity).toBe(1);
  });

  it('no-op if selection absent', () => {
    const s0 = addCard(emptyState(), mkCard());
    const s1 = setQuantity(s0, 'c1', 'art-1', 5);
    expect(s1.cards[0].selections).toEqual([]);
  });
});

describe('setArts', () => {
  it('sets arts and artsStatus', () => {
    let s = addCard(emptyState(), mkCard());
    const arts = [mkArt({ ppId: 'a1' }), mkArt({ ppId: 'a2' })];
    s = setArts(s, 'c1', arts, 'loaded');
    expect(s.cards[0].arts).toEqual(arts);
    expect(s.cards[0].artsStatus).toBe('loaded');
  });
});

describe('importSeed', () => {
  it('appends new cards', () => {
    const s0 = addCard(emptyState(), mkCard({ localId: 'c1', oracleId: 'o1' }));
    const newCards = [
      mkCard({ localId: 'c2', name: 'Timetwister', oracleId: 'o2' }),
    ];
    const s1 = importSeed(s0, newCards);
    expect(s1.cards).toHaveLength(2);
  });

  it('de-dupes by oracleId — keeps existing', () => {
    const s0 = addCard(
      emptyState(),
      mkCard({ localId: 'c1', oracleId: 'o1', name: 'Underground Sea' }),
    );
    const newCards = [
      mkCard({ localId: 'c-dup', oracleId: 'o1', name: 'Underground Sea DUPE' }),
      mkCard({ localId: 'c2', oracleId: 'o2', name: 'Timetwister' }),
    ];
    const s1 = importSeed(s0, newCards);
    expect(s1.cards).toHaveLength(2);
    // Existing card kept (original name, not DUPE)
    expect(s1.cards[0].name).toBe('Underground Sea');
    expect(s1.cards[1].oracleId).toBe('o2');
  });
});

describe('applyRestore', () => {
  it('builds fresh state from rows', () => {
    const rows = [
      { cardName: 'Underground Sea', scryfallLink: 'https://x', ppId: 'a1', confidence: 74.8, quantity: 2 },
      { cardName: 'Underground Sea', scryfallLink: 'https://y', ppId: 'a2', confidence: 66.5, quantity: 1 },
      { cardName: 'Timetwister', scryfallLink: 'https://z', ppId: 'tw1', confidence: 50.0, quantity: 1 },
    ];
    const s = applyRestore(rows);
    expect(s.cards).toHaveLength(2);
    expect(s.cards[0].name).toBe('Underground Sea');
    expect(s.cards[0].selections).toEqual([
      { ppId: 'a1', quantity: 2 },
      { ppId: 'a2', quantity: 1 },
    ]);
    expect(s.cards[1].name).toBe('Timetwister');
    expect(s.cards[1].selections).toEqual([{ ppId: 'tw1', quantity: 1 }]);
  });

  it('preserves buy rows (blank ppId)', () => {
    const rows = [
      { cardName: 'Foo', scryfallLink: 'https://x', ppId: '', confidence: null, quantity: 3 },
    ];
    const s = applyRestore(rows);
    expect(s.cards[0].selections).toEqual([{ ppId: '', quantity: 3 }]);
  });

  it('is unloaded but synthesizes a minimal art per selection carrying link + score', () => {
    const rows = [
      {
        cardName: 'Foo',
        scryfallLink: 'https://scryfall.com/card/3ed/290/foo',
        ppId: 'a1',
        confidence: 50,
        quantity: 1,
      },
    ];
    const s = applyRestore(rows);
    // Still lazily reloadable on expand...
    expect(s.cards[0].artsStatus).toBe('unloaded');
    // ...but the synthesized art preserves the link + confidence so a
    // restore -> export before reload doesn't blank those CSV columns.
    expect(s.cards[0].arts).toHaveLength(1);
    expect(s.cards[0].arts[0]).toMatchObject({
      ppId: 'a1',
      score: 50,
      scryfallUri: 'https://scryfall.com/card/3ed/290/foo',
    });
  });
});

describe('exportableRows', () => {
  it('flattens selections, excludes cards with zero selections', () => {
    let s = addCard(emptyState(), mkCard({ localId: 'c1', name: 'A' }));
    s = addCard(s, mkCard({ localId: 'c2', name: 'B' }));
    s = toggleSelection(s, 'c1', 'art-1');
    const rows = exportableRows(s);
    expect(rows).toHaveLength(1);
    expect(rows[0].cardName).toBe('A');
    expect(rows[0].ppId).toBe('art-1');
    expect(rows[0].quantity).toBe(1);
  });
});

describe('signature + isDirty', () => {
  it('isDirty is false after capturing signature', () => {
    let s = addCard(emptyState(), mkCard());
    s = toggleSelection(s, 'c1', 'art-1');
    const sig = signature(s);
    expect(isDirty(s, sig)).toBe(false);
  });

  it('isDirty is true after mutation', () => {
    let s = addCard(emptyState(), mkCard());
    s = toggleSelection(s, 'c1', 'art-1');
    const sig = signature(s);
    const s2 = setQuantity(s, 'c1', 'art-1', 5);
    expect(isDirty(s2, sig)).toBe(true);
  });

  it('signature is stable across no-op (empty state)', () => {
    const s1 = emptyState();
    const s2 = emptyState();
    expect(signature(s1)).toBe(signature(s2));
  });

  it('signature changes when a selection is added', () => {
    let s = addCard(emptyState(), mkCard());
    const sig1 = signature(s);
    s = toggleSelection(s, 'c1', 'art-1');
    const sig2 = signature(s);
    expect(sig1).not.toBe(sig2);
  });
});
