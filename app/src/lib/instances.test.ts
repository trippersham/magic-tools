import { describe, it, expect } from 'vitest';
import { expandTiles, stackBadge } from './instances';

describe('expandTiles', () => {
  it('pure untouched → all neutral (basics case: 15 Plains)', () => {
    const tiles = expandTiles({ untouchedQty: 15, droppedQty: 0, addedQty: 0 });
    expect(tiles).toHaveLength(15);
    expect(tiles.every((t) => t === 'neutral')).toBe(true);
  });

  it('pure drop → all red', () => {
    expect(expandTiles({ untouchedQty: 0, droppedQty: 1, addedQty: 0 })).toEqual(['drop']);
  });

  it('pure add → all green', () => {
    expect(expandTiles({ untouchedQty: 0, droppedQty: 0, addedQty: 2 })).toEqual(['add', 'add']);
  });

  it('partial swap → U neutral + R red + A green, in order', () => {
    expect(expandTiles({ untouchedQty: 2, droppedQty: 1, addedQty: 1 })).toEqual([
      'neutral',
      'neutral',
      'drop',
      'add',
    ]);
  });
});

describe('stackBadge', () => {
  it('unchanged → ×N', () => {
    expect(stackBadge({ untouchedQty: 15, droppedQty: 0, addedQty: 0 })).toEqual({
      text: '×15',
      kind: 'unchanged',
    });
  });

  it('pure add → ×N (+A) green', () => {
    expect(stackBadge({ untouchedQty: 4, droppedQty: 0, addedQty: 1 })).toEqual({
      text: '×4 (+1)',
      kind: 'add',
    });
  });

  it('pure drop → ×N → ×(N−R) (−R) red', () => {
    expect(stackBadge({ untouchedQty: 1, droppedQty: 2, addedQty: 0 })).toEqual({
      text: '×3 → ×1 (−2)',
      kind: 'drop',
    });
  });

  it('mixed → ×N → ×(N−R+A) amber', () => {
    expect(stackBadge({ untouchedQty: 4, droppedQty: 3, addedQty: 2 })).toEqual({
      text: '×7 → ×6',
      kind: 'mixed',
    });
  });
});
