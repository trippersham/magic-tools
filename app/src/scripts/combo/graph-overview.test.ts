import { describe, it, expect } from 'vitest';
import {
  boxesOverlap,
  resolveOverlaps,
  separateWithinWidth,
  computeOverviewLayout,
  type Box,
  type GraphInput,
} from './graph-overview';

/** AABB overlap with a strict (>0) area of intersection. */
function overlapsStrict(a: Box, b: Box): boolean {
  return (
    Math.abs(a.x - b.x) * 2 < a.w + b.w && Math.abs(a.y - b.y) * 2 < a.h + b.h
  );
}

describe('boxesOverlap (AABB, center-anchored)', () => {
  it('detects two fully coincident boxes as overlapping', () => {
    const a: Box = { id: 'a', x: 0, y: 0, w: 100, h: 100 };
    const b: Box = { id: 'b', x: 0, y: 0, w: 100, h: 100 };
    expect(boxesOverlap(a, b)).toBe(true);
  });

  it('detects partial overlap', () => {
    const a: Box = { id: 'a', x: 0, y: 0, w: 100, h: 100 };
    const b: Box = { id: 'b', x: 40, y: 40, w: 100, h: 100 };
    expect(boxesOverlap(a, b)).toBe(true);
  });

  it('reports no overlap for clearly separated boxes', () => {
    const a: Box = { id: 'a', x: 0, y: 0, w: 100, h: 100 };
    const b: Box = { id: 'b', x: 500, y: 500, w: 100, h: 100 };
    expect(boxesOverlap(a, b)).toBe(false);
  });

  it('treats edge-touching boxes as non-overlapping (no shared area)', () => {
    const a: Box = { id: 'a', x: 0, y: 0, w: 100, h: 100 };
    const b: Box = { id: 'b', x: 100, y: 0, w: 100, h: 100 };
    expect(boxesOverlap(a, b)).toBe(false);
  });
});

describe('resolveOverlaps (rectangle separation post-pass)', () => {
  it('separates a pile of coincident boxes so none overlap', () => {
    const boxes: Box[] = [
      { id: 'a', x: 0, y: 0, w: 120, h: 160 },
      { id: 'b', x: 5, y: 5, w: 120, h: 160 },
      { id: 'c', x: -5, y: -5, w: 120, h: 160 },
      { id: 'd', x: 2, y: -3, w: 120, h: 160 },
    ];
    const out = resolveOverlaps(boxes, { padding: 4 });
    for (let i = 0; i < out.length; i++) {
      for (let j = i + 1; j < out.length; j++) {
        expect(overlapsStrict(out[i], out[j])).toBe(false);
      }
    }
  });

  it('separates a partially-overlapping cluster of mixed sizes', () => {
    const boxes: Box[] = [
      { id: 'big', x: 100, y: 100, w: 200, h: 260 },
      { id: 'm1', x: 140, y: 130, w: 120, h: 160 },
      { id: 'm2', x: 90, y: 160, w: 120, h: 160 },
      { id: 'pill1', x: 120, y: 120, w: 140, h: 48 },
      { id: 'pill2', x: 160, y: 90, w: 140, h: 48 },
    ];
    const out = resolveOverlaps(boxes, { padding: 6 });
    for (let i = 0; i < out.length; i++) {
      for (let j = i + 1; j < out.length; j++) {
        expect(overlapsStrict(out[i], out[j])).toBe(false);
      }
    }
  });

  it('leaves already-separated boxes untouched', () => {
    const boxes: Box[] = [
      { id: 'a', x: 0, y: 0, w: 100, h: 100 },
      { id: 'b', x: 400, y: 0, w: 100, h: 100 },
    ];
    const out = resolveOverlaps(boxes, { padding: 4 });
    expect(out.find((b) => b.id === 'a')).toEqual(boxes[0]);
    expect(out.find((b) => b.id === 'b')).toEqual(boxes[1]);
  });

  it('is pure — does not mutate the input boxes', () => {
    const boxes: Box[] = [
      { id: 'a', x: 0, y: 0, w: 120, h: 160 },
      { id: 'b', x: 5, y: 5, w: 120, h: 160 },
    ];
    const snapshot = JSON.parse(JSON.stringify(boxes));
    resolveOverlaps(boxes, { padding: 4 });
    expect(boxes).toEqual(snapshot);
  });
});

describe('separateWithinWidth (width-bounded separation)', () => {
  const WIDTH = 390;
  const PAD = 16;
  const GAP = 20;

  // AABB clearance along the separating axis; ≥0 means no shared area.
  const clearance = (a: Box, b: Box): number =>
    Math.max(
      Math.abs(a.x - b.x) - (a.w + b.w) / 2,
      Math.abs(a.y - b.y) - (a.h + b.h) / 2,
    );

  it('bounds every box within the width padding and grows vertically (narrow viewport)', () => {
    // 10 cards far too wide to sit in one 390px row → must stack vertically.
    const boxes: Box[] = Array.from({ length: 10 }, (_, i) => ({
      id: `c${i}`,
      x: 195,
      y: 300,
      w: 140,
      h: 195,
    }));
    const out = separateWithinWidth(boxes, { width: WIDTH, pad: PAD, gap: GAP });
    for (const b of out) {
      expect(b.x - b.w / 2).toBeGreaterThanOrEqual(PAD - 1e-6); // left ≥ pad
      expect(b.x + b.w / 2).toBeLessThanOrEqual(WIDTH - PAD + 1e-6); // right ≤ width-pad
    }
    for (let i = 0; i < out.length; i++) {
      for (let j = i + 1; j < out.length; j++) {
        expect(overlapsStrict(out[i], out[j])).toBe(false);
        expect(clearance(out[i], out[j])).toBeGreaterThanOrEqual(GAP - 1e-3);
      }
    }
  });

  it('keeps ≥ gap between separated boxes and never touches an edge (mixed sizes)', () => {
    const boxes: Box[] = [
      { id: 'hub', x: 100, y: 100, w: 192, h: 268 },
      { id: 'a', x: 120, y: 130, w: 120, h: 167 },
      { id: 'b', x: 90, y: 160, w: 120, h: 167 },
      { id: 'pill', x: 150, y: 120, w: 230, h: 48 },
    ];
    const out = separateWithinWidth(boxes, { width: WIDTH, pad: PAD, gap: GAP });
    for (const b of out) {
      expect(b.x - b.w / 2).toBeGreaterThanOrEqual(PAD - 1e-6);
      expect(b.x + b.w / 2).toBeLessThanOrEqual(WIDTH - PAD + 1e-6);
    }
    for (let i = 0; i < out.length; i++) {
      for (let j = i + 1; j < out.length; j++) {
        expect(overlapsStrict(out[i], out[j])).toBe(false);
      }
    }
  });
});

describe('computeOverviewLayout (headless, deterministic)', () => {
  const graph: GraphInput = {
    nodes: [
      { id: 'card:A', kind: 'card', degree: 6 },
      { id: 'card:B', kind: 'card', degree: 1 },
      { id: 'card:C', kind: 'card', degree: 3 },
      { id: 'outcome:drain', kind: 'outcome' },
      { id: 'outcome:draw', kind: 'outcome' },
    ],
    edges: [
      { source: 'card:A', target: 'card:B', kind: 'combo' },
      { source: 'card:A', target: 'outcome:drain', kind: 'produces' },
      { source: 'card:C', target: 'outcome:draw', kind: 'produces' },
    ],
  };

  const sizeFor = (n: { kind: string; degree?: number }) =>
    n.kind === 'card' ? { w: 90 * (1 + ((n.degree ?? 1) - 1) / 10), h: 125 } : { w: 140, h: 48 };

  it('returns one finite position per node', () => {
    const pos = computeOverviewLayout(graph, { width: 1200, height: 800, sizeFor });
    expect(Object.keys(pos).sort()).toEqual(graph.nodes.map((n) => n.id).sort());
    for (const id of Object.keys(pos)) {
      expect(Number.isFinite(pos[id].x)).toBe(true);
      expect(Number.isFinite(pos[id].y)).toBe(true);
    }
  });

  it('is deterministic across runs (no Math.random)', () => {
    const a = computeOverviewLayout(graph, { width: 1200, height: 800, sizeFor });
    const b = computeOverviewLayout(graph, { width: 1200, height: 800, sizeFor });
    expect(a).toEqual(b);
  });

  it('produces a non-overlapping final layout', () => {
    const pos = computeOverviewLayout(graph, { width: 1200, height: 800, sizeFor });
    const boxes: Box[] = graph.nodes.map((n) => {
      const s = sizeFor(n);
      return { id: n.id, x: pos[n.id].x, y: pos[n.id].y, w: s.w, h: s.h };
    });
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        expect(overlapsStrict(boxes[i], boxes[j])).toBe(false);
      }
    }
  });
});
