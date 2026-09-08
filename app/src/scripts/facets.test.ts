import { describe, it, expect } from 'vitest';
import { normalizeFacets, DEFAULTS } from './facets';

describe('normalizeFacets — groupBy round-trip + labels fallback', () => {
  it('round-trips groupBy: labels when labels exist', () => {
    const s = normalizeFacets({ groupBy: 'labels' }, true);
    expect(s.groupBy).toBe('labels');
  });

  it('round-trips groupBy: color regardless of labels', () => {
    expect(normalizeFacets({ groupBy: 'color' }, false).groupBy).toBe('color');
    expect(normalizeFacets({ groupBy: 'color' }, true).groupBy).toBe('color');
  });

  it('falls back groupBy: labels → type when no labels exist', () => {
    const s = normalizeFacets({ groupBy: 'labels' }, false);
    expect(s.groupBy).toBe('type');
  });

  it('fills defaults for missing fields', () => {
    const s = normalizeFacets({}, true);
    expect(s).toEqual(DEFAULTS);
  });

  it('leaves type/color untouched when labels absent', () => {
    expect(normalizeFacets({ groupBy: 'type' }, false).groupBy).toBe('type');
  });
});
