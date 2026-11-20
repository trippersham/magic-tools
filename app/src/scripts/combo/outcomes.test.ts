import { describe, it, expect } from 'vitest';
import {
  OUTCOME_TYPES,
  bucketFeature,
  outcomeTypesFor,
  outcomeTypeIdsFor,
  type ProducesFeature,
} from './outcomes';

const f = (name: string, win = false, status = 'S'): ProducesFeature => ({ name, status, win });

describe('bucketFeature', () => {
  it('maps a win lifeloss feature to drain', () => {
    expect(bucketFeature(f('Infinite lifeloss', true))).toBe('drain');
  });

  it('maps "loses the game" (win) to drain', () => {
    expect(bucketFeature(f('Target opponent loses the game', true))).toBe('drain');
  });

  it('maps win damage-to-opponent to drain', () => {
    expect(bucketFeature(f('Near-infinite damage', true))).toBe('drain');
  });

  it('maps card draw / draw triggers to draw', () => {
    expect(bucketFeature(f('Infinite card draw'))).toBe('draw');
    expect(bucketFeature(f('Infinite draw triggers'))).toBe('draw');
    expect(bucketFeature(f('Near-infinite card draw for target opponent'))).toBe('draw');
  });

  it('maps mana to mana', () => {
    expect(bucketFeature(f('Infinite colorless mana'))).toBe('mana');
  });

  it('maps tokens / +1/+1 counters / storm count to tokens', () => {
    expect(bucketFeature(f('Infinite +1/+1 counters on a creature'))).toBe('tokens');
    expect(bucketFeature(f('Near-infinite storm count'))).toBe('tokens');
    expect(bucketFeature(f('Infinite tokens'))).toBe('tokens');
  });

  it('maps non-win damage to damage (not drain)', () => {
    expect(bucketFeature(f('Infinite damage to any target', false))).toBe('damage');
  });

  it('maps non-win lifegain to lifegain', () => {
    expect(bucketFeature(f('Infinite lifegain', false))).toBe('lifegain');
    expect(bucketFeature(f('Infinite lifegain triggers', false))).toBe('lifegain');
  });

  it('falls back to "other" for an unknown feature and does not throw', () => {
    expect(() => bucketFeature(f('Some wholly unknown effect'))).not.toThrow();
    expect(bucketFeature(f('Some wholly unknown effect'))).toBe('other');
  });
});

describe('outcomeTypesFor', () => {
  it('handles a multi-feature combo: lifegain + lifeloss includes drain', () => {
    const produces: ProducesFeature[] = [
      f('Infinite lifegain triggers', false),
      f('Infinite lifeloss', true),
      f('Infinite lifegain', false),
    ];
    const ids = outcomeTypeIdsFor(produces);
    expect(ids).toContain('drain');
    expect(ids).toContain('lifegain');
  });

  it('splits win from non-win and puts win outcomes first', () => {
    const produces: ProducesFeature[] = [
      f('Infinite lifegain', false),
      f('Infinite lifeloss', true),
    ];
    const types = outcomeTypesFor(produces);
    expect(types[0].win).toBe(true);
    expect(types[0].id).toBe('drain');
    // drain is a win type, lifegain is not
    expect(types.find((t) => t.id === 'lifegain')?.win).toBe(false);
  });

  it('dedups repeated features mapping to the same type', () => {
    const produces: ProducesFeature[] = [
      f('Infinite card draw'),
      f('Infinite draw triggers'),
      f('Near-infinite card draw'),
    ];
    const types = outcomeTypesFor(produces);
    expect(types.filter((t) => t.id === 'draw')).toHaveLength(1);
  });

  it('every returned type has an id/label/icon/win and exists in the registry', () => {
    const produces: ProducesFeature[] = [f('Infinite lifeloss', true), f('Infinite card draw')];
    for (const t of outcomeTypesFor(produces)) {
      expect(OUTCOME_TYPES[t.id]).toBeDefined();
      expect(t.label).toBeTruthy();
      expect(t.icon).toBeTruthy();
      expect(typeof t.win).toBe('boolean');
    }
  });

  it('is order-stable for identical input', () => {
    const produces: ProducesFeature[] = [
      f('Infinite card draw'),
      f('Infinite lifeloss', true),
      f('Near-infinite storm count'),
    ];
    expect(outcomeTypeIdsFor(produces)).toEqual(outcomeTypeIdsFor(produces));
  });
});
