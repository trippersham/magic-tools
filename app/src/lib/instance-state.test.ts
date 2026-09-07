// v4.6 — pure per-instance curation state (seed, nameCounts, per-instance
// mutation via applyAction, export quantities + per-instance notes, reset).

import { describe, it, expect } from 'vitest';
import {
  buildInstanceSeed,
  nameCounts,
  instanceMembership,
  buildInstanceExport,
  applyAction,
  type InstanceState,
} from './curation';

function seed(): InstanceState {
  return buildInstanceSeed({
    names: [
      { name: 'Plains', u: 15, r: 0, a: 0 }, // basic multiple, untouched
      { name: 'Sol Ring', u: 0, r: 0, a: 1 }, // pure add singleton
      { name: 'Gold Myr', u: 0, r: 1, a: 0 }, // pure drop singleton
      { name: 'Hare Apparent', u: 2, r: 1, a: 1 }, // mixed
    ],
    considerations: [{ name: 'Fellwar Stone', qty: 1 }],
  });
}

describe('buildInstanceSeed', () => {
  it('seeds u untouched + r drop + a add instances per name (from nameDiffMap)', () => {
    const st = seed();
    expect(st['Plains']).toHaveLength(15);
    expect(st['Plains'].every((i) => i.membership === 'untouched')).toBe(true);
    expect(st['Sol Ring']).toEqual([{ membership: 'add', note: null }]);
    expect(st['Gold Myr']).toEqual([{ membership: 'drop', note: null }]);
    // mixed: order untouched, drop, add
    expect(st['Hare Apparent'].map((i) => i.membership)).toEqual([
      'untouched',
      'untouched',
      'drop',
      'add',
    ]);
  });

  it('seeds one consideration instance per consideration name', () => {
    const st = seed();
    expect(st['Fellwar Stone']).toEqual([{ membership: 'consideration', note: null }]);
  });
});

describe('nameCounts derivation', () => {
  it('nets memberships into {u,r,a,consideration,dismissed}', () => {
    const st = seed();
    expect(nameCounts(st, 'Plains')).toEqual({ u: 15, r: 0, a: 0, consideration: 0, dismissed: 0 });
    expect(nameCounts(st, 'Hare Apparent')).toEqual({
      u: 2,
      r: 1,
      a: 1,
      consideration: 0,
      dismissed: 0,
    });
    expect(nameCounts(st, 'Fellwar Stone').consideration).toBe(1);
  });
});

describe('instance-addressed mutation via applyAction (per instance)', () => {
  it('setting instance i → correct nameCounts (Plains 15 → untouched 14 / drop 1)', () => {
    const st = seed();
    // Remove one Plains copy (untouched + remove → drop).
    const before = instanceMembership(st, 'Plains', 3);
    expect(before).toBe('untouched');
    st['Plains'][3].membership = applyAction('untouched', 'remove').membership;
    expect(nameCounts(st, 'Plains')).toMatchObject({ u: 14, r: 1 });
  });

  it('the transition table applies unchanged to one instance', () => {
    expect(applyAction('untouched', 'remove').membership).toBe('drop');
    expect(applyAction('add', 'remove').membership).toBe('consideration');
    expect(applyAction('consideration', 'add').membership).toBe('add');
    expect(applyAction('consideration', 'dismiss').membership).toBe('dismissed');
  });
});

describe('buildInstanceExport quantities + notes', () => {
  it('emits {name, qty} buckets from the instance counts', () => {
    const st = seed();
    // Drop 2 of the 15 Plains.
    st['Plains'][0].membership = 'drop';
    st['Plains'][1].membership = 'drop';
    const out = buildInstanceExport(st);
    expect(out.drops).toContainEqual({ name: 'Plains', qty: 2 });
    expect(out.drops).toContainEqual({ name: 'Gold Myr', qty: 1 });
    expect(out.adds).toContainEqual({ name: 'Sol Ring', qty: 1 });
    expect(out.adds).toContainEqual({ name: 'Hare Apparent', qty: 1 });
    expect(out.considerations).toContainEqual({ name: 'Fellwar Stone', qty: 1 });
  });

  it('dismissing a consideration moves it to the dismissed bucket', () => {
    const st = seed();
    st['Fellwar Stone'][0].membership = 'dismissed';
    const out = buildInstanceExport(st);
    expect(out.dismissed).toContainEqual({ name: 'Fellwar Stone', qty: 1 });
    expect(out.considerations).toEqual([]);
  });

  it('per-instance notes key `name#i`; a singleton note keys off the bare name', () => {
    const st = seed();
    st['Plains'][2].note = 'cut this one';
    st['Sol Ring'][0].note = 'best card';
    const out = buildInstanceExport(st);
    expect(out.notes['Plains#2']).toBe('cut this one');
    expect(out.notes['Sol Ring']).toBe('best card'); // singleton → bare name
    expect(out.notes['Sol Ring#0']).toBeUndefined();
  });
});

describe('reset restores the file-derived seed', () => {
  it('a fresh seed equals the baseline regardless of prior mutation', () => {
    const st = seed();
    st['Plains'][0].membership = 'drop';
    st['Sol Ring'][0].membership = 'consideration';
    const fresh = seed();
    expect(nameCounts(fresh, 'Plains')).toMatchObject({ u: 15, r: 0 });
    expect(instanceMembership(fresh, 'Sol Ring', 0)).toBe('add');
  });
});
