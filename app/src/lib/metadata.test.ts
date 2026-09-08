import { describe, it, expect } from 'vitest';
import { curveBucket, primaryType, countPips, stats, delta } from './metadata';
import type { MetaCard } from './types';

describe('curveBucket', () => {
  it('buckets 0..6 by floor', () => {
    expect(curveBucket(0)).toBe('0');
    expect(curveBucket(1)).toBe('1');
    expect(curveBucket(3)).toBe('3');
    expect(curveBucket(6)).toBe('6');
  });
  it('floors fractional mana values', () => {
    expect(curveBucket(2.9)).toBe('2');
    expect(curveBucket(3.0)).toBe('3');
  });
  it('clamps 7 and above to "7+"', () => {
    expect(curveBucket(7)).toBe('7+');
    expect(curveBucket(9)).toBe('7+');
    expect(curveBucket(12.5)).toBe('7+');
  });
});

describe('primaryType', () => {
  it('resolves legendary creature to Creature', () => {
    expect(primaryType('Legendary Creature — Kor Scout')).toBe('Creature');
  });
  it('resolves artifact equipment to Artifact', () => {
    expect(primaryType('Artifact — Equipment')).toBe('Artifact');
  });
  it('resolves land types', () => {
    expect(primaryType('Basic Land — Plains')).toBe('Land');
  });
  it('falls back to Other for unknown lines', () => {
    expect(primaryType('Conspiracy')).toBe('Other');
  });
});

describe('countPips', () => {
  it('returns zeros for null / empty cost', () => {
    expect(countPips(null)).toEqual({ W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 });
    expect(countPips(undefined)).toEqual({ W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 });
  });
  it('counts multiple pips of one color', () => {
    expect(countPips('{2}{W}{W}')).toEqual({ W: 2, U: 0, B: 0, R: 0, G: 0, C: 0 });
  });
  it('counts mixed colors and ignores generic numbers', () => {
    expect(countPips('{1}{U}{B}{R}')).toEqual({ W: 0, U: 1, B: 1, R: 1, G: 0, C: 0 });
  });
  it('counts the generic colorless {C} pip', () => {
    expect(countPips('{C}{C}{G}')).toEqual({ W: 0, U: 0, B: 0, R: 0, G: 1, C: 2 });
  });
});

const CARDS: MetaCard[] = [
  { qty: 1, cmc: 3, mana_cost: '{2}{W}', type_line: 'Legendary Creature — Kor Scout' },
  { qty: 2, cmc: 2, mana_cost: '{W}{W}', type_line: 'Creature — Soldier' },
  { qty: 1, cmc: 8, mana_cost: '{6}{R}{R}', type_line: 'Artifact' },
  { qty: 4, mana_value: 0, mana_cost: null, type_line: 'Basic Land — Plains' },
];

describe('stats', () => {
  const s = stats(CARDS);

  it('counts total quantity across all cards including lands', () => {
    expect(s.total).toBe(1 + 2 + 1 + 4);
  });
  it('excludes lands from the curve', () => {
    // non-land qty = 1 + 2 + 1 = 4
    const curveTotal = Object.values(s.curve).reduce((a, b) => a + b, 0);
    expect(curveTotal).toBe(4);
    expect(s.curve['3']).toBe(1);
    expect(s.curve['2']).toBe(2);
    expect(s.curve['7+']).toBe(1);
  });
  it('tracks land count separately', () => {
    expect(s.landCount).toBe(4);
  });
  it('computes quantity-weighted avgCmc over non-lands only', () => {
    // (3*1 + 2*2 + 8*1) / (1+2+1) = 15/4 = 3.75
    expect(s.avgCmc).toBe(3.75);
  });
  it('accumulates quantity-weighted pips, lands contributing none', () => {
    expect(s.pips.W).toBe(1 * 1 + 2 * 2); // {2}{W} once + {W}{W} twice = 5
    expect(s.pips.R).toBe(2); // {6}{R}{R} once
  });
  it('accumulates quantity-weighted type counts', () => {
    expect(s.typeCounts.Creature).toBe(3);
    expect(s.typeCounts.Artifact).toBe(1);
    expect(s.typeCounts.Land).toBe(4);
  });
  it('yields avgCmc 0 for a land-only set', () => {
    expect(stats([{ qty: 1, type_line: 'Basic Land — Island' }]).avgCmc).toBe(0);
  });
});

describe('delta', () => {
  it('reports signed after − before across fields', () => {
    const before = stats(CARDS);
    const after = stats([...CARDS, { qty: 1, cmc: 1, mana_cost: '{R}', type_line: 'Instant' }]);
    const d = delta(before, after);
    expect(d.total).toBe(1);
    expect(d.curve['1']).toBe(1);
    expect(d.pips.R).toBe(1);
    expect(d.typeCounts.Instant).toBe(1);
    expect(d.avgCmc).toBeLessThan(0); // adding a 1-drop lowers the average
  });
  it('is zero for identical stats', () => {
    const s = stats(CARDS);
    const d = delta(s, s);
    expect(d.total).toBe(0);
    expect(d.avgCmc).toBe(0);
    expect(d.landCount).toBe(0);
    expect(d.pips.W).toBe(0);
  });
});
