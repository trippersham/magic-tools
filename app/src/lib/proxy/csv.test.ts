import { describe, it, expect } from 'vitest';
import { toCsv, fromCsv } from './csv';
import { applyRestore } from './state';
import type { AppState, CardEntry, PpArt } from './types';

// Regression: restoring an exported CSV then re-exporting BEFORE lazily reloading
// a row must preserve the Scryfall link + confidence (and buy-row link), not blank
// them. applyRestore synthesizes minimal arts to carry those through.
describe('restore -> export preserves link + confidence (no lazy reload)', () => {
  it('round-trips scryfall link + confidence + buy row through applyRestore', () => {
    const original =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\n' +
      'Underground Sea,https://scryfall.com/card/3ed/290/underground-sea,1f35877c,0.705,2\r\n' +
      'Niv-Mizzet,https://scryfall.com/card/frc/10/niv-mizzet,,,1\r\n';
    const parsed = fromCsv(original);
    expect(parsed.mode).toBe('restore');
    if (parsed.mode !== 'restore') throw new Error('expected restore');
    const restored = applyRestore(parsed.rows);
    const reExported = toCsv(restored);
    // Both data rows survive with link + confidence intact (buy row keeps its link, blank pp cols)
    expect(reExported).toContain(
      'Underground Sea,https://scryfall.com/card/3ed/290/underground-sea,1f35877c,0.705,2',
    );
    expect(reExported).toContain(
      'Niv-Mizzet,https://scryfall.com/card/frc/10/niv-mizzet,,,1',
    );
  });
});

function mkArt(overrides: Partial<PpArt> = {}): PpArt {
  return {
    ppId: 'art-1',
    setName: 'Revised',
    score: 74.8,
    imageUrl: 'https://example.com/img.jpg',
    kind: 'official',
    stolenValor: 'none',
    scryfallUri: 'https://scryfall.com/card/3ed/290/underground-sea',
    ...overrides,
  };
}

function mkCard(overrides: Partial<CardEntry> = {}): CardEntry {
  return {
    localId: 'c1',
    name: 'Underground Sea',
    oracleId: 'oracle-us',
    arts: [mkArt()],
    artsStatus: 'loaded',
    selections: [{ ppId: 'art-1', quantity: 2 }],
    collapsed: false,
    ...overrides,
  };
}

function mkState(cards: CardEntry[]): AppState {
  return { schemaVersion: 1, cards, lastExportSignature: null };
}

describe('toCsv', () => {
  it('emits header + one row per selection', () => {
    const state = mkState([mkCard()]);
    const csv = toCsv(state);
    const lines = csv.split('\r\n');
    expect(lines[0]).toBe('Card Name,Scryfall link,PP ID,PP confidence,Quantity');
    expect(lines[1]).toBe(
      'Underground Sea,https://scryfall.com/card/3ed/290/underground-sea,art-1,0.748,2',
    );
    // trailing \r\n means last element is empty
    expect(lines[lines.length - 1]).toBe('');
  });

  it('formats confidence as score/100 to 3 decimals', () => {
    const state = mkState([
      mkCard({
        arts: [mkArt({ ppId: 'a', score: 66.5 })],
        selections: [{ ppId: 'a', quantity: 1 }],
      }),
    ]);
    const csv = toCsv(state);
    expect(csv).toContain(',0.665,');
  });

  it('leaves PP confidence blank when score is null', () => {
    const state = mkState([
      mkCard({
        arts: [mkArt({ ppId: 'a', score: null })],
        selections: [{ ppId: 'a', quantity: 1 }],
      }),
    ]);
    const csv = toCsv(state);
    expect(csv).toContain(',a,,1');
  });

  it('handles buy rows (ppId empty string)', () => {
    const buyArt = mkArt({
      ppId: '',
      score: null,
      scryfallUri: 'https://scryfall.com/card/vma/323/underground-sea',
    });
    const state = mkState([
      mkCard({
        arts: [mkArt(), buyArt],
        selections: [{ ppId: '', quantity: 3 }],
      }),
    ]);
    const csv = toCsv(state);
    const lines = csv.split('\r\n');
    // Buy row: blank PP ID, blank confidence, Scryfall link from the buy art
    expect(lines[1]).toBe(
      'Underground Sea,https://scryfall.com/card/vma/323/underground-sea,,,3',
    );
  });

  it('RFC-4180 quotes fields containing commas', () => {
    const state = mkState([
      mkCard({
        name: 'Orcish Lumberjack, Jr.',
        arts: [mkArt({ ppId: 'x' })],
        selections: [{ ppId: 'x', quantity: 1 }],
      }),
    ]);
    const csv = toCsv(state);
    expect(csv).toContain('"Orcish Lumberjack, Jr."');
  });

  it('RFC-4180 quotes fields containing double-quotes by doubling', () => {
    const state = mkState([
      mkCard({
        name: 'Test "Card"',
        arts: [mkArt({ ppId: 'x' })],
        selections: [{ ppId: 'x', quantity: 1 }],
      }),
    ]);
    const csv = toCsv(state);
    expect(csv).toContain('"Test ""Card"""');
  });

  it('flat-maps multiple cards and selections preserving order', () => {
    const card1 = mkCard({
      localId: 'c1',
      name: 'Card A',
      arts: [mkArt({ ppId: 'a1' }), mkArt({ ppId: 'a2' })],
      selections: [
        { ppId: 'a1', quantity: 1 },
        { ppId: 'a2', quantity: 2 },
      ],
    });
    const card2 = mkCard({
      localId: 'c2',
      name: 'Card B',
      arts: [mkArt({ ppId: 'b1' })],
      selections: [{ ppId: 'b1', quantity: 4 }],
    });
    const state = mkState([card1, card2]);
    const csv = toCsv(state);
    const dataLines = csv.split('\r\n').slice(1).filter(Boolean);
    expect(dataLines).toHaveLength(3);
    expect(dataLines[0]).toContain('Card A');
    expect(dataLines[1]).toContain('Card A');
    expect(dataLines[2]).toContain('Card B');
  });
});

describe('fromCsv', () => {
  it('detects restore mode from header with PP ID and Quantity', () => {
    const text =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\nUnderground Sea,https://x,art-1,0.748,2\r\n';
    const result = fromCsv(text);
    expect(result.mode).toBe('restore');
  });

  it('parses restore rows with decimal confidence', () => {
    const text =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\nFoo,https://x,p1,0.665,3\r\n';
    const result = fromCsv(text);
    if (result.mode !== 'restore') throw new Error('expected restore');
    expect(result.rows[0].confidence).toBeCloseTo(66.5, 1);
    expect(result.rows[0].quantity).toBe(3);
  });

  it('parses legacy percentage confidence (74.8%)', () => {
    const text =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\nFoo,https://x,p1,74.8%,1\r\n';
    const result = fromCsv(text);
    if (result.mode !== 'restore') throw new Error('expected restore');
    expect(result.rows[0].confidence).toBeCloseTo(74.8, 1);
  });

  it('blank confidence → null', () => {
    const text =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\nFoo,https://x,p1,,1\r\n';
    const result = fromCsv(text);
    if (result.mode !== 'restore') throw new Error('expected restore');
    expect(result.rows[0].confidence).toBeNull();
  });

  it('blank ppId → buy row', () => {
    const text =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\nFoo,https://x,,,2\r\n';
    const result = fromCsv(text);
    if (result.mode !== 'restore') throw new Error('expected restore');
    expect(result.rows[0].ppId).toBe('');
    expect(result.rows[0].quantity).toBe(2);
  });

  it('detects seed mode from header without PP columns', () => {
    const text = 'Card Name\r\nUnderground Sea\r\nTimetwister\r\n';
    const result = fromCsv(text);
    expect(result.mode).toBe('seed');
    if (result.mode !== 'seed') throw new Error('expected seed');
    expect(result.identifiers).toEqual(['Underground Sea', 'Timetwister']);
  });

  it('seed mode handles bare lines (no header match)', () => {
    const text = 'Underground Sea\nTimetwister\n';
    const result = fromCsv(text);
    expect(result.mode).toBe('seed');
    if (result.mode !== 'seed') throw new Error('expected seed');
    expect(result.identifiers).toEqual(['Underground Sea', 'Timetwister']);
  });

  it('collects malformed lines into report.skipped', () => {
    const text =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\n\r\nFoo,https://x,p1,0.5,1\r\n';
    const result = fromCsv(text);
    expect(result.report.skipped.length).toBeGreaterThanOrEqual(0);
    expect(result.report.added).toBe(1);
  });

  it('handles RFC-4180 quoted fields with embedded commas', () => {
    const text =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\n"Orcish Lumberjack, Jr.",https://x,p1,0.500,1\r\n';
    const result = fromCsv(text);
    if (result.mode !== 'restore') throw new Error('expected restore');
    expect(result.rows[0].cardName).toBe('Orcish Lumberjack, Jr.');
  });

  it('handles \n line endings (not just \r\n)', () => {
    const text =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\nFoo,https://x,p1,0.500,1\n';
    const result = fromCsv(text);
    if (result.mode !== 'restore') throw new Error('expected restore');
    expect(result.rows).toHaveLength(1);
  });
});

describe('round-trip', () => {
  it('fromCsv(toCsv(state)) reconstructs rows identically', () => {
    const buyArt = mkArt({
      ppId: '',
      score: null,
      scryfallUri: 'https://scryfall.com/card/vma/323/underground-sea',
    });
    const card1 = mkCard({
      localId: 'c1',
      name: 'Underground Sea',
      oracleId: 'o1',
      arts: [
        mkArt({ ppId: 'a1', score: 74.8, scryfallUri: 'https://scryfall.com/card/3ed/290/underground-sea' }),
        mkArt({ ppId: 'a2', score: 66.5, scryfallUri: 'https://scryfall.com/card/vma/100/underground-sea' }),
        buyArt,
      ],
      selections: [
        { ppId: 'a1', quantity: 2 },
        { ppId: 'a2', quantity: 1 },
        { ppId: '', quantity: 3 },
      ],
    });
    const card2 = mkCard({
      localId: 'c2',
      name: 'Timetwister',
      oracleId: 'o2',
      arts: [mkArt({ ppId: 'tw1', score: 50.0, scryfallUri: 'https://scryfall.com/card/vma/3/timetwister' })],
      selections: [{ ppId: 'tw1', quantity: 1 }],
    });
    const state = mkState([card1, card2]);
    const csv = toCsv(state);
    const result = fromCsv(csv);

    expect(result.mode).toBe('restore');
    if (result.mode !== 'restore') throw new Error('expected restore');

    // 4 total rows: 3 from card1, 1 from card2
    expect(result.rows).toHaveLength(4);

    // card1 selection 1
    expect(result.rows[0].cardName).toBe('Underground Sea');
    expect(result.rows[0].ppId).toBe('a1');
    expect(result.rows[0].quantity).toBe(2);
    expect(result.rows[0].confidence).toBeCloseTo(74.8, 1);

    // card1 selection 2
    expect(result.rows[1].ppId).toBe('a2');
    expect(result.rows[1].quantity).toBe(1);
    expect(result.rows[1].confidence).toBeCloseTo(66.5, 1);

    // card1 buy row
    expect(result.rows[2].ppId).toBe('');
    expect(result.rows[2].quantity).toBe(3);
    expect(result.rows[2].confidence).toBeNull();

    // card2
    expect(result.rows[3].cardName).toBe('Timetwister');
    expect(result.rows[3].ppId).toBe('tw1');
    expect(result.rows[3].quantity).toBe(1);
    expect(result.rows[3].confidence).toBeCloseTo(50.0, 1);
  });

  it('0.665 and 74.8% parse to equivalent internal scores', () => {
    const decimalText =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\nA,https://x,p1,0.665,1\r\n';
    const percentText =
      'Card Name,Scryfall link,PP ID,PP confidence,Quantity\r\nA,https://x,p1,66.5%,1\r\n';
    const r1 = fromCsv(decimalText);
    const r2 = fromCsv(percentText);
    if (r1.mode !== 'restore' || r2.mode !== 'restore') throw new Error('expected restore');
    expect(r1.rows[0].confidence).toBeCloseTo(r2.rows[0].confidence!, 3);
  });
});
