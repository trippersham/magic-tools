import { describe, it, expect } from 'vitest';
import { computeBlockFit, computeBlockCoords, type LayerDim } from './focus-fit';

// Realistic dims: min-size enabler cards ≈ 120×167, hub cards larger, outcome pills short.
const ENABLER = { cardW: 120, cardH: 167 };
const HUB = { cardW: 178, cardH: 248 };
const OUTCOME = { cardW: 140, cardH: 48 };

const OPTS = { availW: 1408, availH: 748, gap: 22, colGap: 22, layerGap: 80 };

describe('computeBlockFit — block-wrap + fits predicate', () => {
  it('a small layer stays a single column', () => {
    const layers: LayerDim[] = [{ count: 3, ...ENABLER }];
    const { perLayer } = computeBlockFit(layers, OPTS);
    // availH 748 / (167+22)=3.96 → 3 rows/col; 3 cards → 1 column.
    expect(perLayer[0].rowsPerCol).toBe(3);
    expect(perLayer[0].nCols).toBe(1);
  });

  it('a tall layer wraps to the right number of sub-columns for the height', () => {
    const layers: LayerDim[] = [{ count: 9, ...ENABLER }];
    const { perLayer } = computeBlockFit(layers, OPTS);
    // 9 cards, 3 rows/col → ceil(9/3)=3 columns.
    expect(perLayer[0].rowsPerCol).toBe(3);
    expect(perLayer[0].nCols).toBe(3);
    // block width = 3·120 + 2·22 = 404; height = 3·167 + 2·22 = 545.
    expect(perLayer[0].blockW).toBe(404);
    expect(perLayer[0].blockH).toBe(545);
  });

  it('rowsPerCol is per-layer: a taller card stacks fewer per column', () => {
    const { perLayer } = computeBlockFit([{ count: 4, ...HUB }], OPTS);
    // 748 / (248+22) = 2.77 → 2 rows/col; 4 hubs → 2 columns.
    expect(perLayer[0].rowsPerCol).toBe(2);
    expect(perLayer[0].nCols).toBe(2);
  });

  it('totalWidth = Σ blockW + (numLayers−1)·layerGap', () => {
    const layers: LayerDim[] = [
      { count: 9, ...ENABLER }, // block 404
      { count: 1, ...HUB }, // block 178
      { count: 5, cardW: 150, cardH: 209 }, // 748/231=3 rows → ceil(5/3)=2 cols → 322
      { count: 2, ...OUTCOME }, // 1 col → 140
    ];
    const { perLayer, totalWidth } = computeBlockFit(layers, OPTS);
    const sum = perLayer.reduce((s, b) => s + b.blockW, 0);
    expect(totalWidth).toBe(sum + 3 * OPTS.layerGap);
  });

  it('Exquisite-shaped DAG FITS at 1440×900 (Mode A) with a multi-column Layer 0', () => {
    const layers: LayerDim[] = [
      { count: 9, ...ENABLER },
      { count: 1, ...HUB },
      { count: 5, cardW: 150, cardH: 209 },
      { count: 2, ...OUTCOME },
    ];
    const res = computeBlockFit(layers, OPTS);
    expect(res.fits).toBe(true);
    expect(res.perLayer[0].nCols).toBeGreaterThanOrEqual(2);
    expect(res.totalWidth).toBeLessThanOrEqual(OPTS.availW);
  });

  it('Queza-shaped DAG (18 enablers) does NOT fit at 1440×900 → Mode B (too wide)', () => {
    const layers: LayerDim[] = [
      { count: 18, ...ENABLER }, // ceil(18/3)=6 cols → 830
      { count: 1, ...HUB },
      { count: 2, cardW: 150, cardH: 209 },
      { count: 3, ...OUTCOME },
    ];
    const res = computeBlockFit(layers, OPTS);
    expect(res.fits).toBe(false);
    expect(res.totalWidth).toBeGreaterThan(OPTS.availW);
  });

  it('per-column widths: one wide hub card in a min-size flood widens only its own column', () => {
    // 6 cards, 3 rows/col → 2 columns. A single 200-wide card among 120-wide ones.
    const uniform: LayerDim = { count: 6, cardW: 200, cardH: 167 };
    const perCol: LayerDim = {
      count: 6,
      cardW: 200,
      cardH: 167,
      widths: [120, 120, 120, 200, 120, 120], // the 200 sits in column 1
    };
    const u = computeBlockFit([uniform], OPTS).perLayer[0];
    const p = computeBlockFit([perCol], OPTS).perLayer[0];
    // Uniform: both columns forced to 200 → 2·200 + 22 = 422.
    expect(u.blockW).toBe(422);
    // Per-column: col0 = max(120,120,120)=120, col1 = max(200,120,120)=200 → 342.
    expect(p.blockW).toBe(342);
    expect(p.blockW).toBeLessThan(u.blockW);
  });

  it('fits=false when a single layer block is wider than availW even wrapped', () => {
    // One layer, but so many cards it needs more horizontal room than availW.
    const res = computeBlockFit([{ count: 40, ...ENABLER }], OPTS);
    expect(res.fits).toBe(false);
  });

  it('fits=false when the viewport is too short for even one card', () => {
    const res = computeBlockFit([{ count: 2, ...ENABLER }], { ...OPTS, availH: 100 });
    expect(res.fits).toBe(false);
    // still clamps rowsPerCol to ≥1 (never divides by zero / infinite columns).
    expect(res.perLayer[0].rowsPerCol).toBe(1);
  });

  it('a tiny (phone) viewport forces Mode B for a wrapped enabler flood', () => {
    // 390-wide phone: availW ≈ 358. A 9-enabler layer0 can't fit its block.
    const res = computeBlockFit(
      [
        { count: 9, ...ENABLER },
        { count: 1, ...HUB },
        { count: 5, cardW: 150, cardH: 209 },
        { count: 2, ...OUTCOME },
      ],
      { ...OPTS, availW: 358, availH: 692 },
    );
    expect(res.fits).toBe(false);
  });
});

describe('computeBlockCoords — analytic block centres', () => {
  it('fills top-to-bottom then the next sub-column; columns centred about y=0', () => {
    const layers = [['a', 'b', 'c', 'd', 'e']]; // one layer, 5 nodes
    const dims: LayerDim[] = [{ count: 5, ...ENABLER }];
    const { perLayer } = computeBlockFit(
      layers.map((l, i) => ({ count: l.length, cardW: dims[i].cardW, cardH: dims[i].cardH })),
      OPTS,
    );
    // 3 rows/col → col0=[a,b,c], col1=[d,e]
    const coords = computeBlockCoords(layers, dims, perLayer, {
      gap: OPTS.gap,
      colGap: OPTS.colGap,
      layerGap: OPTS.layerGap,
    });
    // a,b,c share one x; d,e share a larger x.
    expect(coords.a.x).toBe(coords.b.x);
    expect(coords.a.x).toBe(coords.c.x);
    expect(coords.d.x).toBe(coords.e.x);
    expect(coords.d.x).toBeGreaterThan(coords.a.x);
    // Column 0 (3 cards) centred about 0: top card negative y, mirror of bottom.
    expect(coords.a.y).toBeLessThan(0);
    expect(coords.a.y).toBeCloseTo(-coords.c.y, 5);
    expect(coords.b.y).toBeCloseTo(0, 5);
    // Column 1 (2 cards) centred about 0: symmetric pair.
    expect(coords.d.y).toBeCloseTo(-coords.e.y, 5);
  });

  it('places later layers to the right by blockW + layerGap', () => {
    const layers = [['a'], ['b']];
    const dims: LayerDim[] = [
      { count: 1, ...ENABLER },
      { count: 1, ...HUB },
    ];
    const blocks = computeBlockFit(
      layers.map((l, i) => ({ count: l.length, cardW: dims[i].cardW, cardH: dims[i].cardH })),
      OPTS,
    ).perLayer;
    const coords = computeBlockCoords(layers, dims, blocks, {
      gap: OPTS.gap,
      colGap: OPTS.colGap,
      layerGap: OPTS.layerGap,
    });
    // layer0 centre at cardW/2 = 60; layer1 centre at blockW0 + layerGap + cardW1/2.
    expect(coords.a.x).toBe(ENABLER.cardW / 2);
    expect(coords.b.x).toBe(blocks[0].blockW + OPTS.layerGap + HUB.cardW / 2);
  });
});
