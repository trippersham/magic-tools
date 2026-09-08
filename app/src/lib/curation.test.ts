import { describe, it, expect } from 'vitest';
import {
  validActions,
  applyAction,
  donutSegments,
  hitTest,
  buildExport,
  buildInitialState,
  changesetHash,
  storageKey,
  type Membership,
  type CardState,
  type DonutGeometry,
} from './curation';

const GEO: DonutGeometry = { radius: 100, deadzone: 20 };

describe('validActions', () => {
  it('untouched has 2 actions (remove, annotate)', () => {
    expect(validActions('untouched')).toEqual(['remove', 'annotate']);
  });
  it('add has 2 actions (remove, annotate)', () => {
    expect(validActions('add')).toEqual(['remove', 'annotate']);
  });
  it('drop has 2 actions (add, annotate)', () => {
    expect(validActions('drop')).toEqual(['add', 'annotate']);
  });
  it('consideration has 3 actions (add, dismiss, annotate)', () => {
    expect(validActions('consideration')).toEqual(['add', 'dismiss', 'annotate']);
  });
  it('dismissed has 0 actions', () => {
    expect(validActions('dismissed')).toEqual([]);
  });
});

describe('applyAction transition table', () => {
  it('untouched + remove → drop', () => {
    expect(applyAction('untouched', 'remove')).toEqual({ membership: 'drop' });
  });
  it('add + remove → consideration', () => {
    expect(applyAction('add', 'remove')).toEqual({ membership: 'consideration' });
  });
  it('drop + add → untouched', () => {
    expect(applyAction('drop', 'add')).toEqual({ membership: 'untouched' });
  });
  it('consideration + add → add', () => {
    expect(applyAction('consideration', 'add')).toEqual({ membership: 'add' });
  });
  it('consideration + dismiss → dismissed', () => {
    expect(applyAction('consideration', 'dismiss')).toEqual({ membership: 'dismissed' });
  });
  it('annotate keeps membership and flags the note input', () => {
    for (const s of ['untouched', 'add', 'drop', 'consideration'] as Membership[]) {
      expect(applyAction(s, 'annotate')).toEqual({ membership: s, annotate: true });
    }
  });
  it('throws on an action invalid for the state', () => {
    expect(() => applyAction('untouched', 'add')).toThrow();
    expect(() => applyAction('drop', 'remove')).toThrow();
    expect(() => applyAction('untouched', 'dismiss')).toThrow();
    expect(() => applyAction('dismissed', 'add')).toThrow();
  });
});

describe('donutSegments geometry — semantic sides', () => {
  it('untouched: Remove on the left (red), Note on the right', () => {
    const segs = donutSegments('untouched');
    const remove = segs.find((s) => s.action === 'remove')!;
    const note = segs.find((s) => s.action === 'annotate')!;
    expect(remove.region).toBe('left');
    expect(remove.color).toBe('red');
    expect(note.region).toBe('right');
    expect(note.color).toBe('gray');
  });
  it('add: Remove on the left, Note on the right', () => {
    const segs = donutSegments('add');
    expect(segs.find((s) => s.action === 'remove')!.region).toBe('left');
    expect(segs.find((s) => s.action === 'annotate')!.region).toBe('right');
  });
  it('drop: Add on the right (green), Note on the left', () => {
    const segs = donutSegments('drop');
    const add = segs.find((s) => s.action === 'add')!;
    expect(add.region).toBe('right');
    expect(add.color).toBe('green');
    expect(segs.find((s) => s.action === 'annotate')!.region).toBe('left');
  });
  it('consideration: dismiss red top-left, add green top-right, annotate gray bottom', () => {
    const segs = donutSegments('consideration');
    expect(segs).toHaveLength(3);
    const dismiss = segs.find((s) => s.action === 'dismiss')!;
    const add = segs.find((s) => s.action === 'add')!;
    const note = segs.find((s) => s.action === 'annotate')!;
    expect(dismiss).toMatchObject({ region: 'top-left', color: 'red' });
    expect(add).toMatchObject({ region: 'top-right', color: 'green' });
    expect(note).toMatchObject({ region: 'bottom', color: 'gray' });
  });
  it('dismissed has no segments', () => {
    expect(donutSegments('dismissed')).toEqual([]);
  });
});

describe('hitTest', () => {
  it('dead-center release inside the deadzone returns null', () => {
    expect(hitTest('untouched', 0, 0, GEO)).toBeNull();
    expect(hitTest('drop', 5, -5, GEO)).toBeNull();
  });
  it('untouched: left → remove, right → annotate', () => {
    expect(hitTest('untouched', -50, 0, GEO)).toBe('remove');
    expect(hitTest('untouched', 50, 0, GEO)).toBe('annotate');
  });
  it('drop: right → add, left → annotate (consistent with semantic sides)', () => {
    expect(hitTest('drop', 50, 0, GEO)).toBe('add');
    expect(hitTest('drop', -50, 0, GEO)).toBe('annotate');
  });
  it('consideration: top-left → dismiss, top-right → add, bottom → annotate', () => {
    expect(hitTest('consideration', -40, -40, GEO)).toBe('dismiss');
    expect(hitTest('consideration', 40, -40, GEO)).toBe('add');
    expect(hitTest('consideration', 0, 40, GEO)).toBe('annotate');
  });
  it('dismissed always returns null (no segments)', () => {
    expect(hitTest('dismissed', 50, 50, GEO)).toBeNull();
  });
});

describe('buildExport round-trip', () => {
  it('sorts memberships into export buckets and carries notes', () => {
    const states: Record<string, CardState> = {
      Keep: { membership: 'untouched', note: null },
      NewCard: { membership: 'add', note: 'good' },
      OldCard: { membership: 'drop', note: null },
      Maybe: { membership: 'consideration', note: '  ' },
      Nope: { membership: 'dismissed', note: 'too slow' },
    };
    const out = buildExport(states);
    expect(out.adds).toEqual(['NewCard']);
    expect(out.drops).toEqual(['OldCard']);
    expect(out.considerations).toEqual(['Maybe']);
    expect(out.dismissed).toEqual(['Nope']);
    expect(out.notes).toEqual({ NewCard: 'good', Nope: 'too slow' });
  });
  it('omits untouched cards and blank/whitespace notes', () => {
    const out = buildExport({ A: { membership: 'untouched', note: '   ' } });
    expect(out.adds).toEqual([]);
    expect(out.notes).toEqual({});
  });
});

describe('buildInitialState', () => {
  it('maps deck/adds/considerations to their baseline memberships', () => {
    const st = buildInitialState({
      deck: [{ name: 'Keep' }, { name: 'Cut' }],
      adds: [{ name: 'New' }],
      considerations: [{ name: 'Maybe' }],
      changeset: { adds: ['New'], drops: ['Cut'], considerations: ['Maybe'] },
    });
    expect(st.Keep.membership).toBe('untouched');
    expect(st.Cut.membership).toBe('drop');
    expect(st.New.membership).toBe('add');
    expect(st.Maybe.membership).toBe('consideration');
  });
});

describe('changesetHash / storageKey stability', () => {
  const a = { adds: ['A', 'B'], drops: ['X'], considerations: ['M'] };
  it('is order-independent', () => {
    const b = { adds: ['B', 'A'], drops: ['X'], considerations: ['M'] };
    expect(changesetHash(a)).toBe(changesetHash(b));
  });
  it('is stable across repeated calls', () => {
    expect(changesetHash(a)).toBe(changesetHash(a));
  });
  it('changes when the changeset content changes', () => {
    const c = { adds: ['A', 'B', 'C'], drops: ['X'], considerations: ['M'] };
    expect(changesetHash(a)).not.toBe(changesetHash(c));
  });
  it('produces an 8-char hex hash', () => {
    expect(changesetHash(a)).toMatch(/^[0-9a-f]{8}$/);
  });
  it('storageKey embeds deck name and baseline hash', () => {
    expect(storageKey('Ardenn', a)).toBe(`deck-diff:v2:Ardenn:${changesetHash(a)}`);
  });
});
